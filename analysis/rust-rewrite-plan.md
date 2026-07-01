# Hermes Agent → Rust 重写建议

## 0. 核心判断：哪些值得用 Rust 重写，哪些不值得

| 维度 | Python 现状 | Rust 收益 | 建议 |
|------|------------|----------|------|
| Agent 主循环（API 调用 → 解析 → tool dispatch） | 同步、I/O 密集，瓶颈在 LLM 延迟 | 几乎无性能收益 | 重写（为统一架构，非为性能） |
| 工具执行（终端/文件/搜索） | subprocess + 线程 | 内存安全、无 GIL 并发 | 重写 |
| Gateway（多平台长连接） | asyncio + aiohttp/httpx | tokio 下更低内存/更稳定长运行 | **最大收益点**，优先重写 |
| CLI 交互（prompt_toolkit + Rich） | 生态成熟 | Rust TUI 生态尚可但不如 Python 丰富 | 可后期重写 |
| 批处理 / RL 环境 | multiprocessing + 专用 loop | 真正的多核并行，无 GIL | 重写有收益 |
| MCP 客户端（stdio + HTTP） | 异步 + 守护线程桥接 | 直接 tokio，无需桥接 | 重写 |

**总体建议：分阶段迁移，Gateway + Core Agent 优先，CLI/皮肤系统最后。**

---

## 1. 推荐的 Crate 布局（Workspace）

```
hermes-agent-rs/
├── Cargo.toml                   # workspace root
├── crates/
│   ├── hermes-core/             # AIAgent 主循环、消息类型
│   ├── hermes-tools/            # ToolRegistry + 所有工具实现
│   ├── hermes-model/            # LLM provider 抽象层（OpenAI/Anthropic/OpenRouter）
│   ├── hermes-state/            # SessionDB（SQLite）、内存/Todo 持久化
│   ├── hermes-gateway/          # Gateway 运行时 + 平台适配 trait
│   ├── hermes-platforms/        # 各平台 adapter 实现（telegram/discord/slack/...）
│   ├── hermes-cli/              # 交互式 CLI
│   ├── hermes-config/           # 配置加载、Profile、常量
│   ├── hermes-mcp/              # MCP 客户端
│   ├── hermes-cron/             # 定时任务
│   └── hermes-batch/            # 批处理运行器
├── src/
│   └── main.rs                  # 统一入口二进制（clap subcommands）
└── tests/
```

### 为什么这样拆

- **hermes-core** 不依赖任何 I/O 适配层，只定义 Agent trait + 消息流转，方便测试与嵌入。
- **hermes-tools** 与 **hermes-model** 分离：工具注册机制不关心用的是哪个 LLM provider。
- **hermes-gateway** 定义 trait，**hermes-platforms** 逐个实现——新平台只加一个文件，不改 gateway 核心。
- **hermes-config** 被所有 crate 引用，对应 Python 的 `hermes_constants` + `hermes_cli/config.py`。

---

## 2. 关键模块的 Rust 映射

### 2.1 AIAgent 主循环（`hermes-core`）

Python 现状：同步 `while` 循环，`openai.chat.completions.create()` → 解析 `tool_calls` → `handle_function_call()`。

```rust
// hermes-core/src/agent.rs
pub struct AgentConfig {
    pub model: String,
    pub max_iterations: u32,
    pub enabled_toolsets: Vec<String>,
    pub disabled_toolsets: Vec<String>,
    pub session_id: String,
    pub platform: Platform,
    // ...
}

pub struct AIAgent {
    config: AgentConfig,
    model_client: Box<dyn ModelClient>,     // hermes-model trait object
    tool_registry: Arc<ToolRegistry>,        // hermes-tools
    session_db: Arc<SessionDB>,              // hermes-state
    todo_store: TodoStore,
    // callbacks 用 channel 替代 Python callable
    event_tx: mpsc::Sender<AgentEvent>,
}

impl AIAgent {
    pub async fn run_conversation(
        &mut self,
        user_message: &str,
        history: &mut Vec<Message>,
    ) -> Result<AgentResponse> {
        let mut iterations = 0;
        loop {
            let response = self.model_client
                .chat_completion(&self.config.model, history)
                .await?;

            if let Some(tool_calls) = response.tool_calls {
                for tc in tool_calls {
                    let result = self.tool_registry
                        .dispatch(&tc.name, &tc.arguments)
                        .await?;
                    history.push(Message::tool_result(tc.id, result));
                }
            } else {
                return Ok(AgentResponse::from(response));
            }

            iterations += 1;
            if iterations >= self.config.max_iterations {
                break;
            }
        }
        // ...
    }
}
```

**关键决策：主循环用 `async`**。虽然 Python 版是同步的，但 Rust 无需"同步/异步桥接"的痛点——直接全 async，Gateway 和 CLI 都能受益。

### 2.2 LLM Provider 抽象（`hermes-model`）

```rust
// hermes-model/src/lib.rs
#[async_trait]
pub trait ModelClient: Send + Sync {
    async fn chat_completion(
        &self,
        model: &str,
        messages: &[Message],
        tools: Option<&[ToolSchema]>,
    ) -> Result<ModelResponse>;

    fn supports_streaming(&self) -> bool;

    async fn chat_completion_stream(
        &self,
        model: &str,
        messages: &[Message],
        tools: Option<&[ToolSchema]>,
    ) -> Result<Pin<Box<dyn Stream<Item = Result<StreamDelta>>>>>;
}

// 实现：
pub struct OpenAIClient { /* reqwest::Client, api_key, base_url */ }
pub struct AnthropicClient { /* ... */ }
pub struct OpenRouterClient { /* ... */ }
```

**Rust 库选择**：
- HTTP: **`reqwest`**（已支持 streaming SSE）
- JSON: **`serde` + `serde_json`**
- 不要用 `openai` 的 Rust SDK（目前生态不稳定），直接封装 REST API 更可控。

### 2.3 工具注册表（`hermes-tools`）

Python 用 module-level `registry.register()` 实现"导入即注册"。Rust 可用 **inventory** crate 或手动 builder：

```rust
// hermes-tools/src/registry.rs
pub struct ToolEntry {
    pub name: &'static str,
    pub toolset: &'static str,
    pub schema: ToolSchema,
    pub handler: Box<dyn ToolHandler>,
    pub check_fn: Option<fn() -> bool>,
    pub requires_env: Vec<&'static str>,
}

#[async_trait]
pub trait ToolHandler: Send + Sync {
    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<String>;
}

pub struct ToolRegistry {
    tools: HashMap<String, ToolEntry>,
}

impl ToolRegistry {
    pub fn builder() -> ToolRegistryBuilder { /* ... */ }

    pub fn get_definitions(
        &self,
        enabled: &HashSet<String>,
    ) -> Vec<ToolSchema> { /* filter by toolset + check_fn */ }

    pub async fn dispatch(
        &self,
        name: &str,
        args: &serde_json::Value,
    ) -> Result<String> { /* lookup + execute */ }
}
```

**Toolset 解析**：Python 的递归 `resolve_toolset` 映射成 Rust 的递归 `HashSet` 收集（toolsets 是静态配置，可在启动时一次性展开）。

### 2.4 SessionDB（`hermes-state`）

```rust
// hermes-state/src/lib.rs — 直接映射 Python 的 SQLite schema
pub struct SessionDB {
    conn: Arc<Mutex<rusqlite::Connection>>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Session {
    pub id: String,
    pub source: String,
    pub user_id: Option<String>,
    pub model: Option<String>,
    pub parent_session_id: Option<String>,
    pub started_at: f64,
    pub ended_at: Option<f64>,
    pub title: Option<String>,
    pub input_tokens: i64,
    pub output_tokens: i64,
    // ...
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Message {
    pub id: i64,
    pub session_id: String,
    pub role: String,       // "system" | "user" | "assistant" | "tool"
    pub content: Option<String>,
    pub tool_calls: Option<String>,
    pub tool_name: Option<String>,
    pub timestamp: f64,
    pub reasoning: Option<String>,
}
```

**Rust 库选择**：
- **`rusqlite`** + `rusqlite::vtab` 进行 FTS5（或直接用 raw SQL）
- WAL + `Arc<Mutex<>>` 的并发模型与 Python 版等价
- 写重试（jitter）逻辑直接移植

### 2.5 Gateway（`hermes-gateway` + `hermes-platforms`）

这是 Rust 收益最大的模块。Python 版用 asyncio，Rust 用 tokio：

```rust
// hermes-gateway/src/platform.rs
#[async_trait]
pub trait PlatformAdapter: Send + Sync {
    async fn connect(&mut self) -> Result<()>;
    async fn disconnect(&mut self) -> Result<()>;
    async fn send(
        &self,
        chat_id: &str,
        content: &str,
        reply_to: Option<&str>,
    ) -> Result<SendResult>;
    async fn get_chat_info(&self, chat_id: &str) -> Result<ChatInfo>;
}

// hermes-platforms/src/telegram.rs
pub struct TelegramAdapter {
    config: PlatformConfig,
    http: reqwest::Client,
    // ...
}

#[async_trait]
impl PlatformAdapter for TelegramAdapter { /* ... */ }
```

**18 个平台适配器** 不需要一次全写。优先级建议：

1. **Telegram** + **Discord**（用户最多）
2. **Slack** + **API Server**（企业需求）
3. 其余按需

### 2.6 配置系统（`hermes-config`）

```rust
// hermes-config/src/lib.rs
#[derive(Debug, Deserialize, Default)]
pub struct HermesConfig {
    pub model: String,
    pub providers: ProvidersConfig,
    pub toolsets: ToolsetsConfig,
    pub agent: AgentConfig,
    pub terminal: TerminalConfig,
    pub display: DisplayConfig,
    pub gateway: GatewayConfig,
    // ...
}

pub fn get_hermes_home() -> PathBuf {
    std::env::var("HERMES_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| dirs::home_dir().unwrap().join(".hermes"))
}

pub fn load_config() -> Result<HermesConfig> {
    let path = get_hermes_home().join("config.yaml");
    let content = std::fs::read_to_string(&path)?;
    let config: HermesConfig = serde_yaml::from_str(&content)?;
    Ok(config)
}
```

**Rust 库选择**：**`serde_yaml`** 或 **`serde_yml`** 加载 YAML，**`dotenvy`** 加载 `.env`。

### 2.7 MCP 客户端（`hermes-mcp`）

Python 版在守护线程里跑 asyncio 循环，Rust 直接用 tokio：

```rust
// hermes-mcp/src/client.rs
pub struct McpClient {
    transport: Box<dyn McpTransport>,
}

#[async_trait]
pub trait McpTransport: Send + Sync {
    async fn send(&self, request: JsonRpcRequest) -> Result<JsonRpcResponse>;
    async fn close(&mut self) -> Result<()>;
}

pub struct StdioTransport { /* tokio::process::Child */ }
pub struct HttpTransport  { /* reqwest::Client */ }
```

无需 Python 版的"sync ↔ async 桥接"，全程 tokio 原生。

### 2.8 CLI（`hermes-cli`）

| Python 库 | Rust 替代 |
|-----------|----------|
| `prompt_toolkit` | **`rustyline`** 或 **`reedline`**（Nushell 的输入库） |
| `rich` | **`ratatui`**（TUI 框架）或 **`console`** crate（简单彩色输出） |
| `argparse` | **`clap`**（derive 模式） |

CLI 可最后迁移——初期可以保留 Python CLI 调用 Rust 核心二进制。

---

## 3. 数据模型与序列化

### 3.1 消息类型（OpenAI 格式）

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "role")]
pub enum Message {
    #[serde(rename = "system")]
    System { content: String },
    #[serde(rename = "user")]
    User { content: MessageContent },
    #[serde(rename = "assistant")]
    Assistant {
        content: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        tool_calls: Option<Vec<ToolCall>>,
        #[serde(skip_serializing_if = "Option::is_none")]
        reasoning: Option<String>,
    },
    #[serde(rename = "tool")]
    Tool {
        tool_call_id: String,
        content: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCall {
    pub id: String,
    pub r#type: String,     // "function"
    pub function: FunctionCall,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FunctionCall {
    pub name: String,
    pub arguments: String,  // JSON string
}
```

### 3.2 Tool Schema（JSON Schema 子集）

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolSchema {
    pub r#type: String,     // "function"
    pub function: FunctionDef,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FunctionDef {
    pub name: String,
    pub description: String,
    pub parameters: serde_json::Value,  // JSON Schema object
}
```

---

## 4. 并发模型建议

| 场景 | Python 现状 | Rust 建议 |
|------|------------|----------|
| Agent 主循环 | 同步 while | `async fn run_conversation()` on tokio |
| 工具并行执行 | 无（顺序执行） | `tokio::JoinSet` 可选并行 |
| 子代理（delegate） | `ThreadPoolExecutor(3)` | `tokio::spawn` + `Semaphore(3)` |
| Gateway 多平台 | `asyncio.gather` | `tokio::JoinSet` per adapter |
| 批处理 | `multiprocessing.Pool` | **`rayon`** 或 `tokio::spawn_blocking` |
| MCP | 守护线程 + asyncio loop | 直接 tokio，无需桥接 |
| 终端工具 | `subprocess` + 线程管道 | `tokio::process::Command` |
| 文件 I/O | 同步 | `tokio::fs` 或 `spawn_blocking` |

**统一 runtime**：全项目一个 `#[tokio::main]`，不需要 Python 那样的 sync/async 桥接。

---

## 5. 依赖选型总览

| 功能 | Rust Crate | 备注 |
|------|-----------|------|
| 异步运行时 | `tokio` | 全局统一 |
| HTTP 客户端 | `reqwest` | 支持 streaming |
| JSON | `serde` + `serde_json` | 核心序列化 |
| YAML 配置 | `serde_yaml` | config.yaml |
| SQLite | `rusqlite` (+ `bundled` feature) | SessionDB, FTS5 |
| CLI 参数 | `clap` (derive) | 子命令入口 |
| CLI 输入 | `reedline` 或 `rustyline` | 交互式 REPL |
| TUI 输出 | `ratatui` 或 `console` + `indicatif` | 替代 Rich |
| 日志 | `tracing` + `tracing-subscriber` | 结构化日志 |
| 错误处理 | `anyhow` + `thiserror` | 应用级/库级 |
| .env 加载 | `dotenvy` | 环境变量 |
| Cron 表达式 | `cron` crate | 替代 croniter |
| WebSocket | `tokio-tungstenite` | Discord/Slack 等 |
| Telegram Bot | `teloxide` | Telegram 适配器 |
| Discord Bot | `serenity` 或 `twilight` | Discord 适配器 |
| 进程管理 | `tokio::process` | 终端工具 |
| UUID | `uuid` | 会话 ID |
| 时间 | `chrono` | 时间戳 |
| SSE/Stream | `eventsource-stream` + `reqwest-eventsource` | LLM streaming |

---

## 6. 分阶段迁移路线图

### Phase 1：核心引擎（~4-6 周）

**目标**：能在 Rust 中跑通一次对话。

- [ ] `hermes-config`：加载 config.yaml + .env + Profile
- [ ] `hermes-model`：OpenAI/Anthropic REST client（chat completion + streaming）
- [ ] `hermes-state`：SessionDB schema + CRUD
- [ ] `hermes-tools`：ToolRegistry + 3 个核心工具（`terminal`、`file_read`、`file_write`）
- [ ] `hermes-core`：AIAgent 主循环
- [ ] 最小 CLI：`clap` 入口 + 简单 stdin/stdout 交互

**验证**：能通过 CLI 发消息 → 调工具 → 得到回复 → 落库。

### Phase 2：工具补全 + Gateway（~4-6 周）

- [ ] 补全所有工具（web_search、browser、code_execution、delegate、mcp 等）
- [ ] `hermes-gateway`：Platform trait + SessionStore + 会话管理
- [ ] `hermes-platforms`：Telegram + Discord 适配器
- [ ] `hermes-mcp`：MCP 客户端（stdio + HTTP）
- [ ] 上下文压缩（辅助 LLM 调用）

**验证**：Telegram 机器人能对话 + 使用工具。

### Phase 3：完善体验（~3-4 周）

- [ ] 完整 CLI（reedline + ratatui spinner + 皮肤系统）
- [ ] 剩余平台适配器（Slack、WhatsApp、Signal 等）
- [ ] Cron 调度
- [ ] 批处理运行器
- [ ] Memory 系统（MEMORY.md / USER.md）
- [ ] Prompt caching 策略

### Phase 4：高级功能 + 优化（持续）

- [ ] Plugin 系统（动态加载 .so/.dll）
- [ ] RL 训练环境
- [ ] 性能调优（连接池、缓存、零拷贝）
- [ ] 完整测试覆盖

---

## 7. 需要特别注意的移植难点

### 7.1 动态工具注册

Python 靠"导入即执行"的 `registry.register()` 实现。Rust 选项：

- **推荐**：`ToolRegistryBuilder` + 显式 `.register()` 调用，在 `main()` 初始化
- **备选**：`inventory` / `linkme` crate 模拟"分布式静态注册"
- **不推荐**：过度使用 `lazy_static!`

### 7.2 Python 回调 → Rust Channel / Event

Python 版大量使用 `callable` 回调（`tool_progress_callback`、`thinking_callback` 等）。Rust 中：

- 用 `tokio::sync::mpsc` channel 发 `AgentEvent` 枚举
- 消费端（CLI/Gateway）各自 `tokio::select!` 监听
- 比回调更安全、更可测试

### 7.3 全局可变状态

Python 版有若干进程全局变量（如 `model_tools._last_resolved_tool_names`）。Rust 中：

- 用 `Arc<RwLock<T>>` 或直接通过 `ToolContext` 传参消除
- 子代理的全局隔离问题在 Rust 中可用 `Clone` + 独立 scope 解决

### 7.4 SQLite 跨平台

`rusqlite` 搭配 `bundled` feature 可编译自带 SQLite，免去系统依赖。FTS5 需确认 `bundled` 包含。

### 7.5 交叉编译与分发

Rust 天然适合静态编译单二进制。可用 `cross` 或 GitHub Actions 产出：
- `x86_64-unknown-linux-musl`（静态链接 Linux）
- `aarch64-apple-darwin`（Apple Silicon）
- `x86_64-pc-windows-msvc`（Windows）

相比 Python 版（需 venv + pip + 系统依赖），分发体验大幅改善。

---

## 8. 不建议用 Rust 重写的部分

| 部分 | 原因 |
|------|------|
| 技能脚本（`skills/` 下的 Python 脚本） | 用户自定义，保留 Python 通过子进程调用更灵活 |
| RL 训练环境 | 与 PyTorch/Atropos 深度耦合，保留 Python 通过 API 对接 Rust agent |
| 测试中的 mock/monkey-patch 逻辑 | Rust 测试用 trait mock（`mockall` crate）替代 |

---

## 9. 总结

| 维度 | 建议 |
|------|------|
| 架构风格 | Workspace 多 crate，trait 抽象边界清晰 |
| 异步模型 | 全程 tokio async，不做 sync/async 桥接 |
| 优先级 | Config → Model → State → Tools → Core → Gateway → CLI |
| 最大收益 | Gateway 长运行稳定性、单二进制分发、无 GIL 并发 |
| 最大风险 | CLI 交互体验（prompt_toolkit/Rich 无等价替代）、18 个平台适配器工作量 |
| 预估总工作量 | 1-2 人 ~4-6 个月达到 Phase 2 可用状态 |
