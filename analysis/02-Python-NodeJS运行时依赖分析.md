# Hermes Agent 运行时依赖分析（Python + Node.js）

## 结论：双运行时架构

本项目**同时依赖 Python 和 Node.js 两个运行时**，但两者的角色不同：

| 运行时 | 角色 | 必需性 |
|--------|------|--------|
| **Python 3.11-3.13** | 代理核心引擎（CLI、Gateway、Agent Loop、工具执行） | **必需** — 整个 agent 核心是 Python |
| **Node.js ≥20.19** | Desktop 应用（Electron）、浏览器工具、TUI | **条件必需** — 仅 Desktop/浏览器/TUI 场景需要 |

---

## 集成实现流程图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          安装阶段 (Install Time)                              │
│                                                                             │
│  install.sh / install.ps1                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  Stage 1: 安装 uv (Python 包管理器)                                    │  │
│  │    └─ 下载到 $HERMES_HOME/bin/uv                                      │  │
│  │                                                                       │  │
│  │  Stage 2: 安装 Python 3.11                                            │  │
│  │    └─ uv python install 3.11  (由 uv 自动管理)                         │  │
│  │                                                                       │  │
│  │  Stage 3: 安装 Git                                                     │  │
│  │    └─ Windows: 下载 MinGit 到 $HERMES_HOME/git                        │  │
│  │                                                                       │  │
│  │  Stage 4: 检测/安装 Node.js                                            │  │
│  │    ├─ 已有系统 Node → 使用系统版本                                      │  │
│  │    └─ 无 Node → 下载便携版到 $HERMES_HOME/node                         │  │
│  │                                                                       │  │
│  │  Stage 5: 克隆 Hermes 仓库                                             │  │
│  │    └─ git clone → $HERMES_HOME/hermes-agent                            │  │
│  │                                                                       │  │
│  │  Stage 6: 创建 Python venv                                             │  │
│  │    └─ uv venv venv --python 3.11                                      │  │
│  │                                                                       │  │
│  │  Stage 7: 安装 Python 依赖                                             │  │
│  │    └─ uv pip install -e ".[all]"                                      │  │
│  │    └─ 注册入口点: hermes = "hermes_cli.main:main"                      │  │
│  │                                                                       │  │
│  │  Stage 8: 安装 Node.js 依赖                                            │  │
│  │    └─ npm install (browser tools, workspace deps)                     │  │
│  │                                                                       │  │
│  │  Stage 9: [可选] 构建 Desktop 应用                                      │  │
│  │    └─ npm run dist:win → Hermes.exe                                   │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          运行阶段 (Runtime)                                  │
│                                                                             │
│  ┌─────────────────────┐        ┌──────────────────────────────────────┐   │
│  │  CLI 模式            │        │  Desktop 模式                          │   │
│  │                     │        │                                      │   │
│  │  $ hermes           │        │  Hermes.exe (Electron/Node.js)       │   │
│  │    │                │        │    │                                 │   │
│  │    ▼                │        │    ▼                                 │   │
│  │  Python venv        │        │  electron/main.cjs                   │   │
│  │  hermes_cli.main    │        │    │                                 │   │
│  │    :main()          │        │    ├─ resolveHermesBackend()          │   │
│  │    │                │        │    │   ├─ 1. HERMES_DESKTOP_HERMES_ROOT│   │
│  │    ▼                │        │    │   ├─ 2. 源码树 (开发模式)          │   │
│  │  AIAgent            │        │    │   ├─ 3. 已安装的 venv              │   │
│  │  (run_agent.py)     │        │    │   ├─ 4. PATH 上的 hermes 命令      │   │
│  │    │                │        │    │   ├─ 5. 系统 Python + hermes_cli  │   │
│  │    ▼                │        │    │   └─ 6. bootstrap-needed → 安装    │   │
│  │  Agent Loop         │        │    │                                 │   │
│  │  (模型调用→工具执行) │        │    ▼                                 │   │
│  │                     │        │  spawn(python, -m hermes_cli.main    │   │
│  │                     │        │    dashboard --host 127.0.0.1 ...)   │   │
│  │                     │        │    │                                 │   │
│  │                     │        │    ▼                                 │   │
│  │                     │        │  Python 后端进程                       │   │
│  │                     │        │  (FastAPI + uvicorn on 随机端口)       │   │
│  │                     │        │    │                                 │   │
│  │                     │        │    ▼                                 │   │
│  │                     │        │  WebSocket / REST API                │   │
│  │                     │        │    │                                 │   │
│  │                     │        │    ▼                                 │   │
│  │                     │        │  React 前端 (Electron BrowserWindow)  │   │
│  └─────────────────────┘        └──────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Python 运行时集成

### 1. Python 环境管理

**关键类/文件：** `hermes_cli/managed_uv.py`

Hermes 通过 **uv**（Astral 的 Rust Python 包管理器）自管理 Python 运行时，不依赖系统 Python：

```python
# hermes_cli/managed_uv.py
def managed_uv_path() -> Path:
    """$HERMES_HOME/bin/uv (POSIX) 或 $HERMES_HOME\bin\uv.exe (Windows)"""
    home = get_hermes_home()
    if platform.system() == "Windows":
        return home / "bin" / "uv.exe"
    return home / "bin" / "uv"
```

### 2. 入口点注册

**关键文件：** `pyproject.toml` 第 296-299 行

```toml
[project.scripts]
hermes = "hermes_cli.main:main"        # CLI 主入口
hermes-agent = "run_agent:main"        # 直接运行 agent
hermes-acp = "acp_adapter.entry:main"  # ACP 服务器模式
```

安装后，`hermes` 命令指向 venv 中的 Python 脚本，调用 `hermes_cli.main:main()`。

### 3. Windows UTF-8 引导

**关键文件：** `hermes_bootstrap.py`

每个 Python 入口点的第一行都是 `import hermes_bootstrap`，在 Windows 上设置 UTF-8 编码：

```python
# hermes_bootstrap.py — 每个入口点的第一行导入
def apply_windows_utf8_bootstrap() -> bool:
    os.environ.setdefault("PYTHONUTF8", "1")           # 子进程继承
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
```

### 4. 安装流程（Python 侧）

**关键文件：** `setup-hermes.sh` / `scripts/install.ps1`

```
uv 安装 → Python 3.11 安装 → venv 创建 → uv pip install -e ".[all]"
```

依赖按精确版本锁定（`==X.Y.Z`，无范围），防止供应链攻击。

### 5. 懒加载依赖

**关键文件：** `hermes_cli/dep_ensure.py`

非 Python 运行时依赖（Node.js、浏览器、ripgrep、ffmpeg）在 Python 中检测，按需通过 install.sh 安装：

```python
# hermes_cli/dep_ensure.py
_DEP_CHECKS = {
    "node": lambda: shutil.which("node") is not None,
    "browser": lambda: agent_browser_runnable(...) or _has_system_browser(),
    "ripgrep": lambda: shutil.which("rg") is not None,
    "ffmpeg": lambda: shutil.which("ffmpeg") is not None,
}
```

---

## Node.js 运行时集成

### 1. Node.js 在 Desktop 应用中的角色

**关键文件：** `apps/desktop/electron/main.cjs`

Desktop 应用是 **Electron 应用**（Node.js 运行时），它不直接运行 agent 逻辑，而是**生成 Python 子进程**作为后端：

```javascript
// electron/main.cjs — createPythonBackend()
function createPythonBackend(root, label, dashboardArgs, options = {}) {
  const python = findPythonForRoot(root);
  const venvRoot = path.join(root, "venv");
  const venvPython = getVenvPython(venvRoot);
  const command = IS_WINDOWS && fileExists(venvPython)
    ? getNoConsoleVenvPython(venvRoot)    // venv\Scripts\pythonw.exe
    : toNoConsolePython(python);          // pythonw.exe (无控制台窗口)
  return {
    kind: "python",
    command,
    args: ["-m", "hermes_cli.main", ...dashboardArgs],  // 启动 Python 后端
    // ...
  };
}
```

### 2. 后端解析优先级链

**关键函数：** `resolveHermesBackend()` in `main.cjs` 第 11229 行

```
1. HERMES_DESKTOP_HERMES_ROOT 环境变量 → 指定源码树
2. 开发模式下的源码仓库根目录 (SOURCE_REPO_ROOT)
3. 已完成的 bootstrap 安装 (isBootstrapComplete) → venv 中的 Python
4. PATH 上的 hermes 命令 (verifyHermesCli 探测 --version)
5. 系统 Python + hermes_cli 模块 (canImportHermesCli 探测)
6. 以上都失败 → "bootstrap-needed" → 触发首次安装
```

对应代码：

```javascript
// electron/main.cjs — resolveHermesBackend()
function resolveHermesBackend(dashboardArgs) {
  // 1. 环境变量覆盖
  const overrideRoot = process.env.HERMES_DESKTOP_HERMES_ROOT;
  if (overrideRoot && isHermesSourceRoot(overrideRoot)) { ... }

  // 2. 开发模式源码树
  if (!IS_PACKAGED && isHermesSourceRoot(SOURCE_REPO_ROOT)) { ... }

  // 3. 已安装的 venv
  if (isBootstrapComplete()) {
    return createActiveBackend(dashboardArgs);  // venv\Scripts\pythonw.exe
  }

  // 4. PATH 上的 hermes 命令
  if (process.env.HERMES_DESKTOP_IGNORE_EXISTING !== "1") {
    const hermesCommand = findOnPath("hermes");
    if (hermesCommand && verifyHermesCli(hermesCommand)) { ... }
  }

  // 5. 系统 Python + hermes_cli
  const python = findSystemPython();
  if (python && canImportHermesCli(python)) { ... }

  // 6. 需要首次安装
  return { kind: "bootstrap-needed", ... };
}
```

### 3. 首次启动 Bootstrap 流程

**关键文件：** `apps/desktop/electron/bootstrap-runner.cjs`

当 `resolveHermesBackend()` 返回 `bootstrap-needed` 时，Electron 主进程运行 `install.ps1`/`install.sh`：

```javascript
// bootstrap-runner.cjs — runBootstrap()
// 调用 scripts/install.ps1 (Windows) 或 scripts/install.sh (POSIX)
// 按阶段执行：uv → python → git → node → repository → venv → deps → node-deps
// 流式输出进度事件到前端 UI
```

### 4. Python 后端启动

**关键文件：** `main.cjs` 第 12941 行

```javascript
// electron/main.cjs — spawnPoolBackend()
const dashboardArgs = ["--profile", profile, "dashboard",
                        "--no-open", "--host", "127.0.0.1", "--port", "0"];
const backend = await ensureRuntime(resolveHermesBackend(dashboardArgs));

const child = spawn(backend.command, backend.args, {
  env: {
    ...process.env,
    HERMES_HOME,
    HERMES_DESKTOP: "1",                    // 标记为 Desktop 启动
    HERMES_DASHBOARD_SESSION_TOKEN: token,   // WebSocket 认证 token
    // ...
  },
  stdio: ["ignore", "pipe", "pipe"]
});
```

Python 侧对应 `hermes dashboard` 命令，启动 FastAPI + uvicorn 服务器，Electron 前端通过 WebSocket/REST 与之通信。

### 5. Node.js 安装流程

**关键文件：** `scripts/install.ps1` 第 918-1043 行

```
检测系统 Node.js
  ├─ 已有且版本满足 (≥20.19) → 使用系统版本
  ├─ 版本过旧 → 安装 Hermes 管理的便携版
  └─ 不存在 → 下载便携版到 $HERMES_HOME\node
       └─ 从 nodejs.org/dist/latest-v22.x/ 获取
       └─ 解压到 $HERMES_HOME\node\ (用户级，无需管理员权限)
```

---

## 两种运行时的协作关系

```
┌──────────────────────────────────────────────────────────┐
│                Desktop 应用进程架构                         │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  Electron 主进程 (Node.js 运行时)                    │  │
│  │  electron/main.cjs                                  │  │
│  │  ├── 窗口管理、IPC、原生 OS 集成                      │  │
│  │  ├── resolveHermesBackend() → 找到 Python           │  │
│  │  ├── spawn(python -m hermes_cli.main dashboard)     │  │
│  │  └── WebSocket 代理 → 转发消息到 Python 后端         │  │
│  └───────────────────────┬────────────────────────────┘  │
│                          │ stdio + WebSocket              │
│  ┌───────────────────────▼────────────────────────────┐  │
│  │  Python 后端子进程 (Python 运行时)                    │  │
│  │  hermes_cli.main dashboard                          │  │
│  │  ├── FastAPI + uvicorn (HTTP server)                │  │
│  │  ├── AIAgent (run_agent.py) — agent 核心引擎         │  │
│  │  │   ├── conversation_loop — 会话循环                │  │
│  │  │   ├── tool_executor — 工具执行                     │  │
│  │  │   └── memory_manager — 记忆管理                    │  │
│  │  └── WebSocket gateway → 流式输出到前端               │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  Electron 渲染进程 (Chromium)                        │  │
│  │  React 前端 (dist/index.html)                       │  │
│  │  └── 通过 WebSocket 与 Python 后端通信                │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

**核心设计思想：** Node.js（Electron）只负责 **UI 外壳和进程管理**，所有 AI agent 逻辑都在 **Python 子进程**中运行。两个运行时通过 localhost WebSocket 通信，实现了清晰的关注点分离。
