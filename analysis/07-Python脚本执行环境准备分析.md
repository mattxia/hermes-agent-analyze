# HermesAgent 执行 Python 脚本时的运行环境准备分析

> 对应 query：阅读项目源码，分析 HermesAgent 在运行 python 脚本执行任务的时候，是怎么准备 python 运行需要的环境的，请给出执行流程，包括流程图，对应类和代码

## 核心结论

当用户任务需要运行 Python 脚本时，HermesAgent 有**两条执行路径**，通过**六层准备模型**层层保障 Python 运行时的正确性。

## 一、两条执行路径总览

| 路径 | 触发场景 | 执行方式 | Python 解析方式 |
|---|---|---|---|
| **A. execute_code 工具** | LLM 生成 Python 脚本，需调用 Hermes 工具（PTC 模式） | `subprocess.Popen([python, script.py])` | `_resolve_child_python()` 多策略解析 |
| **B. terminal 工具** | 用户要求运行 `python script.py` 等通用命令 | `bash -c "python script.py"` | 依赖 PATH（shell 快照 + sane PATH） |

---

## 二、整体流程图

```
┌─────────────────────────────────────────────────────────────────────┐
│                    用户任务需要运行 Python 脚本                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                    ┌──────────┴──────────┐
                    │                     │
              execute_code 工具      terminal 工具
              (PTC 沙箱模式)         (通用命令模式)
                    │                     │
                    ▼                     ▼
    ┌───────────────────────────┐  ┌──────────────────────────────┐
    │ 第1层: 安全审查             │  │ 第1层: 命令审查               │
    │ check_execute_code_guard() │  │ check_all_command_guards()   │
    │ (approval.py)              │  │ (approval.py)                │
    └─────────────┬─────────────┘  └──────────────┬───────────────┘
                  │                                │
                  ▼                                ▼
    ┌───────────────────────────┐  ┌──────────────────────────────┐
    │ 第2层: 确定执行后端         │  │ 确定执行后端                  │
    │ _get_env_config()          │  │ _get_env_config()            │
    │ env_type = local/docker/   │  │ env_type = local/docker/     │
    │   ssh/modal/daytona        │  │   ssh/modal/daytona          │
    └─────────────┬─────────────┘  └──────────────┬───────────────┘
                  │                                │
        ┌─────────┴─────────┐                      │
        │                   │                      │
   env_type=local     env_type!=local               │
   (本地UDS路径)      (远程文件RPC路径)              │
        │                   │                      │
        ▼                   ▼                      ▼
┌────────────────┐ ┌────────────────┐  ┌──────────────────────────────┐
│第3层:Python解析 │ │第3层:远端Python │  │第3层:终端环境创建             │
│_resolve_child_  │ │ 检查            │  │_create_environment()         │
│python(mode)     │ │ command -v      │  │ → LocalEnvironment           │
│                 │ │ python3         │  │ → DockerEnvironment          │
│ project模式:    │ └───────┬────────┘  │ → SSHEnvironment etc.        │
│  优先 VIRTUAL_  │         │           └──────────────┬───────────────┘
│  ENV/CONDA_     │         ▼                          │
│  PREFIX 的python│ ┌────────────────┐                  │
│ strict模式:     │ │第4层:创建沙箱目录│                  │
│  sys.executable │ │mkdir -p sandbox │                  ▼
└────────┬────────┘ │ship hermes_tools│  ┌──────────────────────────────┐
         │          │.py + script.py  │  │第4层:会话快照 init_session()  │
         │          └───────┬────────┘  │ 捕获 login shell 环境         │
         │                  │           │ source ~/.profile ~/.bashrc   │
         ▼                  ▼           │ → snapshot.sh                 │
┌────────────────┐ ┌────────────────┐  └──────────────┬───────────────┘
│第4层:环境变量   │ │第4层:启动RPC轮询│                 │
│准备             │ │ _rpc_poll_loop()│                 ▼
│_scrub_child_env│ │ 文件式RPC分发   │  ┌──────────────────────────────┐
│ (清洗密钥)      │ └───────┬────────┘  │第5层:命令包装 _wrap_command() │
│                 │         │           │ source snapshot              │
│ +PYTHONPATH     │         ▼           │ cd → cwd                     │
│ +PYTHONUTF8=1   │ ┌────────────────┐  │ eval 'command'               │
│ +PYTHONIOENCODING││第5层:远端执行    │  │ → pwd 标记                    │
│ +TZ             │ │ env.execute(    │  └──────────────┬───────────────┘
│ +HERMES_RPC_    │ │  python3 script)│                 │
│  SOCKET/DIR     │ └───────┬────────┘                 │
└────────┬────────┘         │                          │
         │                  │                          ▼
         ▼                  │          ┌──────────────────────────────┐
┌────────────────┐          │          │第6层:进程等待 _wait_for_proc()│
│第5层:生成      │          │          │ select() 非阻塞 drain        │
│hermes_tools.py │          │          │ 中断检查 is_interrupted()     │
│ generate_hermes│          │          │ 超时检查                      │
│ _tools_module()│          │          │ 活动回调 touch_activity()     │
│ (RPC桩代码)     │          │          └──────────────┬───────────────┘
└────────┬────────┘          │                          │
         │                  │                          │
         ▼                  ▼                          ▼
┌────────────────┐ ┌────────────────┐  ┌──────────────────────────────┐
│第5层:启动RPC   │ │                │  │ 输出后处理                    │
│ server (UDS/   │ │                │  │ strip_ansi()                  │
│ TCP)           │ │                │  │ redact_sensitive_text()       │
│ _rpc_server_   │ │                │  │ 截断 MAX_STDOUT_BYTES         │
│ loop()         │ │                │  └──────────────────────────────┘
└────────┬────────┘ └────────────────┘
         │
         ▼
┌────────────────┐
│第6层:启动子进程  │
│ subprocess.Popen│
│ ([python,       │
│  script.py],    │
│  env=child_env, │
│  cwd=child_cwd) │
└────────┬────────┘
         │
         ▼
┌────────────────┐
│ 轮询等待退出     │
│ 超时/中断/完成   │
│ 收集 stdout/    │
│ stderr          │
└────────┬────────┘
         │
         ▼
┌────────────────┐
│ 输出后处理       │
│ strip_ansi()    │
│ redact_secrets()│
│ 截断 50KB        │
└────────────────┘
```

---

## 三、六层准备模型详解

### 第1层：安全审查

**对应类/文件：** `tools/approval.py` — `check_execute_code_guard()`

在脚本执行前，对整个 Python 脚本进行安全审查。`execute_code` 运行任意 Python 代码（可调用 `subprocess`、`os.system` 等），不经过 `terminal()` 的命令审查，因此需要一次性整体审批。

```python
# tools/code_execution_tool.py:1112-1120
from tools.approval import check_execute_code_guard
_guard = check_execute_code_guard(code, env_type)
if not _guard.get("approved", False):
    return json.dumps({
        "status": "error",
        "error": _guard.get("message") or "execute_code blocked by approval guard.",
        ...
    })
```

### 第2层：执行后端确定

**对应类/文件：** `tools/terminal_tool.py` — `_get_env_config()`

通过环境变量 `TERMINAL_ENV` 确定执行后端类型，默认为 `local`：

```python
# tools/terminal_tool.py:1221
env_type = os.getenv("TERMINAL_ENV", "local")
```

支持的后端：`local`、`docker`、`singularity`、`modal`、`daytona`、`ssh`。后端类型决定了后续的 Python 解析方式和 RPC 传输方式。

### 第3层：Python 解释器解析

这是**最核心的环境准备步骤**。对应 `tools/code_execution_tool.py` 中的三个关键函数：

#### 3a. 解释器解析 — `_resolve_child_python(mode)`

```python
# tools/code_execution_tool.py:1644-1684
def _resolve_child_python(mode: str) -> str:
    if mode != "project":          # strict 模式：用 hermes 自己的 Python
        return sys.executable

    # project 模式：优先用用户的 venv/conda Python
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        root = os.environ.get(var, "").strip()
        if not root:
            continue
        for subdir in subdirs:      # bin/ (POSIX) 或 Scripts/ (Windows)
            for exe in exe_names:   # python / python3 / python.exe
                candidate = os.path.join(root, subdir, exe)
                if _is_usable_python(candidate):  # 检查 Python 3.8+
                    return candidate

    return sys.executable           # 回退到 hermes 自己的 Python
```

两种模式的区别：
- **`project`（默认）**：使用用户活动虚拟环境的 Python，使 `import pandas` 等项目依赖可用
- **`strict`**：使用 `sys.executable`（hermes-agent 的 Python），隔离且可复现

#### 3b. 工作目录解析 — `_resolve_child_cwd(mode, staging_dir)`

```python
# tools/code_execution_tool.py:1687-1706
def _resolve_child_cwd(mode: str, staging_dir: str) -> str:
    if mode != "project":
        return staging_dir              # strict: 临时目录
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw and os.path.isdir(...):
        return expanded                  # project: 会话工作目录
    return os.getcwd()                   # 回退到当前目录
```

#### 3c. 远端 Python 检查（远程后端专用）

对于 docker/ssh/modal 等远端后端，直接检查远端是否有 `python3`：

```python
# tools/code_execution_tool.py:915-929
py_check = env.execute(
    "command -v python3 >/dev/null 2>&1 && echo OK",
    cwd="/", timeout=15,
)
if "OK" not in py_check.get("output", ""):
    return json.dumps({
        "status": "error",
        "error": f"Python 3 is not available in the {env_type} terminal environment...",
    })
```

### 第4层：环境变量准备

**对应函数：** `_scrub_child_env()` — `tools/code_execution_tool.py:136-197`

子进程环境变量经过严格清洗，防止密钥泄露：

```python
# 清洗规则（按顺序）：
# 1. env_passthrough 声明的变量 → 通过
# 2. 含 KEY/TOKEN/SECRET/PASSWORD 等子串 → 阻止
# 3. 匹配安全前缀 PATH/HOME/PYTHONPATH/VIRTUAL_ENV 等 → 通过
# 4. 操作性 HERMES_* 变量（HERMES_HOME/HERMES_PROFILE 等）→ 通过
# 5. Windows OS 必需变量（SYSTEMROOT/WINDIR/COMSPEC 等）→ 通过
```

清洗后注入的关键变量：

```python
# tools/code_execution_tool.py:1232-1271
child_env["HERMES_RPC_SOCKET"] = rpc_endpoint   # RPC 端点
child_env["PYTHONDONTWRITEBYTECODE"] = "1"      # 不生成 .pyc
child_env["PYTHONIOENCODING"] = "utf-8"          # UTF-8 stdio
child_env["PYTHONUTF8"] = "1"                    # PEP 540 UTF-8 模式
child_env["PYTHONPATH"] = os.pathsep.join([tmpdir, _hermes_root])  # 模块搜索路径
child_env["TZ"] = _tz_name                       # 时区
```

### 第5层：hermes_tools.py 模块生成与 RPC 通信

**对应函数：** `generate_hermes_tools_module()` — `tools/code_execution_tool.py:259-291`

自动生成 `hermes_tools.py` 桩模块，让沙箱脚本可以通过 RPC 调用 Hermes 工具：

```python
# 生成流程
tools_to_generate = sorted(SANDBOX_ALLOWED_TOOLS & set(enabled_tools))
# SANDBOX_ALLOWED_TOOLS = web_search, web_extract, read_file,
#                         write_file, search_files, patch, terminal

# 两种传输模式：
# - UDS (Unix Domain Socket)：本地后端
# - file (文件式 RPC)：远端后端（docker/ssh/modal 等）
```

**本地 UDS 路径：**

```python
# tools/code_execution_tool.py:1198-1207
if _use_tcp_rpc:                    # Windows 回退到 TCP
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
else:                               # POSIX 使用 Unix Domain Socket
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.bind(sock_path)
    os.chmod(sock_path, 0o600)      # 仅 owner 可访问
```

**远端文件 RPC 路径：**

```python
# tools/code_execution_tool.py:937-941
tools_src = generate_hermes_tools_module(list(sandbox_tools), transport="file")
_ship_file_to_remote(env, f"{sandbox_dir}/hermes_tools.py", tools_src)
_ship_file_to_remote(env, f"{sandbox_dir}/script.py", code)
```

### 第6层：子进程启动与监控

**对应代码：** `tools/code_execution_tool.py:1286-1295`

```python
proc = subprocess.Popen(
    [_child_python, _script_path],
    cwd=_child_cwd,
    env=child_env,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    stdin=subprocess.DEVNULL,
    start_new_session=True,                              # 独立进程组
    creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
)
```

监控循环支持：
- **超时终止**：超过 `timeout`（默认 300s）后 kill 进程树
- **协作中断**：`is_interrupted()` 检查用户是否发了新消息
- **活动回调**：`touch_activity_if_due()` 定期通知 gateway 仍在运行
- **输出截断**：stdout 上限 50KB（head 40% + tail 60%），stderr 上限 10KB

---

## 四、终端工具路径（路径 B）的环境准备

当通过 `terminal` 工具运行 `python script.py` 时，环境准备由 `tools/environments/local.py` 的 `LocalEnvironment` 处理：

| 步骤 | 函数 | 作用 |
|---|---|---|
| 会话快照 | `init_session()` (base.py) | 捕获 login shell 环境到 snapshot.sh |
| Shell 初始化 | `_resolve_shell_init_files()` | source `~/.profile`、`~/.bashrc` 等 |
| 环境清洗 | `_make_run_env()` / `_sanitize_subprocess_env()` | 过滤 API 密钥等敏感变量 |
| PATH 增强 | `_append_missing_sane_path_entries()` | 补齐 `/usr/local/bin` 等标准路径 |
| Hermes CLI 可达 | `_prepend_hermes_bin_dir()` | 确保 `hermes` 命令可被找到 |
| HOME 设置 | `apply_subprocess_home_env()` | 设置正确的 HOME 目录 |

---

## 五、底层 Python 运行时管理

### uv 包管理器自管理

**对应文件：** `hermes_cli/managed_uv.py`

Hermes 不依赖系统 Python，通过 uv 自管理 Python 运行时：

```python
# hermes_cli/managed_uv.py:30-40
def managed_uv_path() -> Path:
    home = get_hermes_home()
    if platform.system() == "Windows":
        return home / "bin" / "uv.exe"
    return home / "bin" / "uv"
```

### UTF-8 引导

**对应文件：** `hermes_bootstrap.py`

每个 Python 入口点的第一行都是 `import hermes_bootstrap`，在 Windows 上设置 UTF-8 编码：

```python
# hermes_bootstrap.py:80-81
os.environ.setdefault("PYTHONUTF8", "1")           # 子进程继承
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
```

### 懒依赖安装

**对应文件：** `tools/lazy_deps.py`

可选依赖（如 anthropic SDK、Bedrock 等）在首次使用时懒安装到 venv：

```python
# tools/lazy_deps.py — 安全模型
# 1. Venv 作用域：安装到 sys.executable 的 venv，不碰系统 Python
# 2. 白名单：只有 LAZY_DEPS 中的 spec 可被安装
# 3. 仅 PyPI：不支持 --index-url / git+https:// 等可被劫持的源
# 4. 可退出：security.allow_lazy_installs: false 可禁用
```

### 环境探测

**对应文件：** `tools/env_probe.py`

探测本地 Python 工具链状态（版本、pip 可用性、PEP 668），写入系统提示词，让模型无需碰壁即可知道 Python 环境：

```python
# tools/env_probe.py:48-53
_REMOTE_BACKENDS = frozenset({
    "docker", "singularity", "modal", "daytona", "ssh", "managed_modal",
})
# 仅对 local 后端探测，远端后端有自己的探测逻辑
```

---

## 六、核心类与文件索引

| 类/函数 | 文件 | 职责 |
|---|---|---|
| `execute_code()` | `tools/code_execution_tool.py` | 主入口，分发本地/远端路径 |
| `_resolve_child_python()` | 同上 | Python 解释器解析（project/strict 模式） |
| `_resolve_child_cwd()` | 同上 | 工作目录解析 |
| `_scrub_child_env()` | 同上 | 子进程环境变量清洗 |
| `generate_hermes_tools_module()` | 同上 | 生成 RPC 桩模块 |
| `_rpc_server_loop()` | 同上 | 本地 UDS RPC 服务器 |
| `_rpc_poll_loop()` | 同上 | 远端文件 RPC 轮询 |
| `_execute_remote()` | 同上 | 远端执行路径 |
| `BaseEnvironment` | `tools/environments/base.py` | 环境基类，`execute()`/`init_session()` |
| `LocalEnvironment` | `tools/environments/local.py` | 本地执行环境 |
| `_create_environment()` | `tools/terminal_tool.py` | 工厂函数，创建环境实例 |
| `_get_env_config()` | 同上 | 读取终端环境配置 |
| `managed_uv_path()` / `ensure_uv()` | `hermes_cli/managed_uv.py` | uv 包管理器管理 |
| `apply_windows_utf8_bootstrap()` | `hermes_bootstrap.py` | Windows UTF-8 引导 |
| `ensure()` | `tools/lazy_deps.py` | 懒依赖安装 |
| `check_execute_code_guard()` | `tools/approval.py` | 执行前安全审查 |
| `apply_subprocess_home_env()` | `hermes_constants.py` | 子进程 HOME 设置 |

---

## 总结

HermesAgent 执行 Python 脚本时的环境准备是一个**六层管线**：

1. **安全审查** — 脚本整体审批，防止危险代码
2. **后端确定** — local/docker/ssh/modal 等
3. **Python 解析** — project 模式优先用户 venv，strict 模式用 hermes 自带 Python；远端检查 `python3` 可用性
4. **环境变量** — 清洗密钥 + 注入 PYTHONPATH/PYTHONUTF8/TZ/RPC 端点
5. **模块生成** — 自动生成 `hermes_tools.py` RPC 桩 + 启动 UDS/文件 RPC 服务器
6. **进程监控** — 超时/中断/活动回调 + 输出截断/脱敏

这套设计确保了在任何启动方式、任何后端类型下，Python 脚本都能安全、正确地执行，同时项目依赖和相对路径能自然解析。
