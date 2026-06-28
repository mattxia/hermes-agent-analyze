# macOS 上 Python 运行时的集成分析

> 对应 query：请分析本项目是在MacOS怎么完成Python运行时的集成的，包括Python运行时的安装、Python运行时支持Python相关脚本运行等

本项目在 macOS 上**不打包 Python 运行时**，而是在首次启动 Hermes.app 时通过 `scripts/install.sh` 动态拉取并集成。核心策略是：**用 uv（Astral 的 Python 包管理器）管理 Python 解释器本身 + 创建隔离 venv + 安装依赖**，最终由 Electron 主进程 spawn venv 内的 Python 运行 Agent 后端。

## 一、Python 运行时的安装链路

### 流程图：Python 运行时安装全过程

```
┌─────────────────────────────────────────────────────────────────┐
│ Hermes.app 首次启动                                             │
│ resolveHermesBackend() → kind="bootstrap-needed"               │
│ → runBootstrap() 调用 install.sh                                │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
   ┌───────────────────────────────────────────────────────────┐
   │  Stage 1: prerequisites                                    │
   │  install_uv()  ← 关键第一步：先装 uv 包管理器              │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  install_uv() (scripts/install.sh)                        │
   │                                                           │
   │  1. 目标路径: $HERMES_HOME/bin/uv  (~/.hermes/bin/uv)     │
   │     (Hermes 自己拥有 uv，不依赖系统 PATH)                  │
   │                                                           │
   │  2. 若已存在且可执行 → 直接复用                            │
   │                                                           │
   │  3. 否则下载 astral.sh 官方安装器                          │
   │     curl -LsSf https://astral.sh/uv/install.sh            │
   │     UV_UNMANAGED_INSTALL="$HERMES_HOME/bin" sh installer  │
   │                                                           │
   │  → UV_CMD=~/.hermes/bin/uv                                │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  check_python() (scripts/install.sh)                      │
   │                                                           │
   │  策略：让 uv 管理 Python，不需要 sudo                      │
   │                                                           │
   │  1. uv python find 3.11                                   │
   │     → 若系统已有合适 Python（Homebrew/系统/python.org）     │
   │       uv 会发现并复用，PYTHON_PATH 指向它                  │
   │                                                           │
   │  2. 若找不到 → uv python install 3.11                     │
   │     uv 下载独立 Python 发行版到 uv 管理目录                 │
   │     (无需 sudo，不污染系统 Python)                          │
   │                                                           │
   │  → PYTHON_PATH=<uv 发现/安装的 python3.11>                │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  Stage 2: repository                                       │
   │  clone_repo() → git clone 到 ~/.hermes/hermes-agent/       │
   │  (锁定 install-stamp.json 中的 commit)                     │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  Stage 3: venv  (scripts/install.sh 的 setup_venv)        │
   │  setup_venv()                                              │
   │                                                           │
   │  $UV_CMD venv venv --python "$PYTHON_VERSION"             │
   │  → 在 ~/.hermes/hermes-agent/venv/ 创建虚拟环境            │
   │  → uv 一步完成：创建 venv + 绑定 Python 3.11              │
   │                                                           │
   │  关键：export UV_PYTHON="$INSTALL_DIR/venv/bin/python"    │
   │  防止后续 uv 命令因继承的 UV_PYTHON 环境变量               │
   │  重建 venv 到错误版本（如 3.14）                           │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  Stage 4: python-deps  (scripts/install.sh 的 install_deps)│
   │  install_deps()                                            │
   │                                                           │
   │  多层降级策略（保证健壮性）：                               │
   │                                                           │
   │  Tier 0 (最佳): uv sync --extra all --locked              │
   │    + UV_PROJECT_ENVIRONMENT="$INSTALL_DIR/venv"           │
   │    → 用 uv.lock 的 SHA256 哈希验证每个传递依赖             │
   │    → 抵御供应链攻击（如 mistralai 2.4.6 蠕虫事件）         │
   │                                                           │
   │  Tier 1 (回退): uv pip install -e '.[all]'                │
   │    → 从 PyPI 重新解析，无哈希验证                          │
   │                                                           │
   │  Tier 2: [all] 减去已知损坏的 extras                       │
   │  Tier 3: 仅基础包 (bare .)                                 │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  Stage 5: node-deps                                        │
   │  install_node_deps() → 浏览器工具的 Node 依赖              │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  Stage 6: path  (scripts/install.sh 的 setup_path)        │
   │  setup_path()                                              │
   │                                                           │
   │  HERMES_BIN=$INSTALL_DIR/venv/bin/hermes                  │
   │  (pip install -e . 生成的 console_script 入口点)           │
   │                                                           │
   │  在 ~/.local/bin/hermes 创建 shim:                         │
   │    #!/usr/bin/env bash                                     │
   │    unset PYTHONPATH                                        │
   │    unset PYTHONHOME                                        │
   │    exec "$HERMES_BIN" "$@"                                │
   │                                                           │
   │  清除 PYTHONPATH/PYTHONHOME 防止继承的环境变量             │
   │  让 launcher 导入错误 checkout 的模块                      │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  Stage 7: config → 复制配置模板                            │
   │  Stage 8: setup → 配置向导（非交互模式跳过）               │
   │  Stage 9: gateway → 网关（跳过）                           │
   │  Stage 10: complete → 写 .install_method                  │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  writeBootstrapMarker()                                    │
   │  写入 ~/.hermes/hermes-agent/.hermes-bootstrap-complete    │
   │  → 标记 Python 运行时集成完成                              │
   └───────────────────────────────────────────────────────────┘
```

## 二、Python 运行时的最终目录布局

```
~/.hermes/                                    ← HERMES_HOME
├── bin/
│   └── uv                                    ← uv 包管理器（Hermes 自管）
├── hermes-agent/                             ← INSTALL_DIR / ACTIVE_HERMES_ROOT
│   ├── .git/                                 ← git 仓库（锁定 commit）
│   ├── .hermes-bootstrap-complete            ← 引导完成标记
│   ├── .install_method                       ← "git"
│   ├── venv/                                 ← Python 虚拟环境
│   │   ├── bin/
│   │   │   ├── python                        ← venv Python（指向 uv 管理的 3.11）
│   │   │   ├── hermes                        ← console_script 入口点
│   │   │   ├── hermes-agent                  ← console_script 入口点
│   │   │   └── hermes-acp                    ← console_script 入口点
│   │   ├── lib/python3.11/site-packages/     ← 已安装依赖
│   │   │   ├── agent/                        ← Agent 核心代码（editable install）
│   │   │   ├── hermes_cli/
│   │   │   ├── gateway/
│   │   │   ├── tools/
│   │   │   ├── fastapi/
│   │   │   ├── uvicorn/
│   │   │   └── ...
│   │   └── pyvenv.cfg                        ← venv 配置（含 home = <uv python 路径>）
│   ├── locales/                              ← i18n catalogs
│   ├── skills/                               ← 技能定义
│   ├── optional-skills/
│   ├── pyproject.toml
│   ├── uv.lock                               ← 哈希锁文件
│   └── ...
├── bootstrap-cache/
│   └── install-<commit>.sh                   ← 缓存的安装脚本
└── logs/
    ├── desktop.log                           ← 桌面端日志
    └── bootstrap-<ts>.log                    ← 引导日志
```

## 三、Electron 如何调用 Python 运行时

### 后端解析与启动（`apps/desktop/electron/main.cjs`）

引导完成后，每次启动 Hermes.app 的流程：

```
┌──────────────────────────────────────────────────────────────────┐
│ Hermes.app 启动                                                  │
│ resolveHermesBackend() (apps/desktop/electron/main.cjs)          │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
   ┌───────────────────────────────────────────────────────────┐
   │  解析阶梯（按优先级，命中即返回）                          │
   │                                                           │
   │  ① HERMES_DESKTOP_HERMES_ROOT 环境变量（开发覆盖）        │
   │  ② isBootstrapComplete() = true → createActiveBackend()   │
   │  ③ PATH 上的 hermes（--version 探针验活）                  │
   │  ④ 系统 Python + canImportHermesCli 探针                  │
   │  ⑤ 都没命中 → bootstrap-needed（首启引导）                │
   └──────────────────────────┬────────────────────────────────┘
                              │ ② 命中（正常已安装路径）
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  createActiveBackend() (apps/desktop/electron/main.cjs)   │
   │                                                           │
   │  VENV_ROOT = ~/.hermes/hermes-agent/venv                  │
   │  venvPython = getVenvPython(VENV_ROOT)                    │
   │    → macOS: venv/bin/python                               │
   │    → Windows: venv/Scripts/python.exe                     │
   │                                                           │
   │  command = getNoConsoleVenvPython(VENV_ROOT)              │
   │    → macOS: 直接返回 venv/bin/python                      │
   │    → Windows: 优先 pythonw.exe（无控制台窗口）            │
   │                                                           │
   │  args = ["-m", "hermes_cli.main", ...dashboardArgs]       │
   │                                                           │
   │  env = buildDesktopBackendEnv({                           │
   │    hermesHome: ~/.hermes,                                 │
   │    pythonPathEntries: [ACTIVE_HERMES_ROOT],  ← 让 Agent   │
   │    venvRoot: VENV_ROOT                        代码可被     │
   │  })                                            import     │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  ensureRuntime() → 校验 venv 存在                         │
   │  (apps/desktop/electron/main.cjs)                         │
   │                                                           │
   │  ✓ isHermesSourceRoot(ACTIVE_HERMES_ROOT)                 │
   │  ✓ fileExists(venv/bin/python)                            │
   │  → backend.command = venv/bin/python                      │
   │  → backend.label = "Hermes at ~/.hermes/hermes-agent      │
   │                      (venv: .../venv)"                    │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  spawn 后端进程                                           │
   │                                                           │
   │  child_process.spawn(                                     │
   │    "/Users/<user>/.hermes/hermes-agent/venv/bin/python",  │
   │    ["-m", "hermes_cli.main", "dashboard",                │
   │     "--no-open", "--tui", "--host", "127.0.0.1",         │
   │     "--port", "<N>"],                                    │
   │    {                                                      │
   │      env: {                                              │
   │        PATH: "...:/opt/homebrew/bin:~/.hermes/hermes-     │
   │               agent/venv/bin:...",                        │
   │        PYTHONPATH: "~/.hermes/hermes-agent",             │
   │        HERMES_HOME: "~/.hermes",                         │
   │        ...process.env                                    │
   │      },                                                   │
   │      stdio: ['ignore', 'pipe', 'pipe']                   │
   │    }                                                      │
   │  )                                                        │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  waitForDashboardPort(child, 90s)                         │
   │  (apps/desktop/electron/backend-ready.cjs)                │
   │                                                           │
   │  监听 child stdout，等待一行：                             │
   │    "HERMES_DASHBOARD_READY port=<actual_port>"            │
   │                                                           │
   │  超时 90s（冷启动容忍，Python 导入链慢）                   │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  渲染层连接 ws://127.0.0.1:<port>                         │
   │  显示聊天 UI                                              │
   └───────────────────────────────────────────────────────────┘
```

## 四、Python 脚本如何被支持运行

### 1. 后端服务（dashboard 模式）

Electron 调用的是：

```bash
~/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main dashboard --no-open --tui --host 127.0.0.1 --port <N>
```

进入 `hermes_cli/main.py` 的 `cmd_dashboard` → 调用 `hermes_cli/web_server.py` 的 `start_server` → 启动 FastAPI + uvicorn，绑定端口后打印 `HERMES_DASHBOARD_READY port=<N>`。

### 2. CLI 入口点（console_scripts）

`pyproject.toml` 定义三个入口点，由 `pip install -e .` 生成到 `venv/bin/`：

| 命令 | 入口 | 用途 |
|------|------|------|
| `hermes` | `hermes_cli.main:main` | 主 CLI（交互式聊天/dashboard/gateway 等） |
| `hermes-agent` | `run_agent:main` | Agent 运行器 |
| `hermes-acp` | `acp_adapter.entry:main` | ACP 服务器（编辑器集成） |

### 3. PATH 环境构造（`apps/desktop/electron/backend-env.cjs`）

macOS 上 Electron 为后端进程构造的 PATH（关键，因为从 Dock/Finder 启动的 app 只继承 `/usr/bin:/bin:/usr/sbin:/sbin`）：

```
PATH = <以下按顺序去重拼接>:
  ~/.hermes/node/bin          ← Hermes 管理的 Node（浏览器工具）
  ~/.hermes/hermes-agent/venv/bin  ← venv 的 python/hermes 命令
  <继承的 PATH>               ← 系统 PATH
  /opt/homebrew/bin           ← Apple Silicon Homebrew
  /opt/homebrew/sbin
  /usr/local/sbin
  /usr/local/bin              ← Intel Homebrew
  /usr/sbin
  /usr/bin
  /sbin
  /bin

PYTHONPATH = ~/.hermes/hermes-agent  ← 让 editable install 的源码可被 import
```

### 4. 运行时更新（`hermes_cli/managed_uv.py`）

`hermes update` 命令通过 `managed_uv.py` 自更新 uv 二进制和 Python 运行时，保持与 install.sh 同一位置（`$HERMES_HOME/bin/uv`），确保安装与更新路径一致。

## 五、关键设计要点

1. **uv 是 Python 运行时的管理者**：Hermes 不直接安装 Python，而是先装 uv，再用 uv 的 `python install` 能力按需下载独立 Python 发行版。这避免了 macOS 上系统 Python 版本不可控、需要 sudo、污染系统等问题。

2. **三层隔离**：uv（工具）→ uv 管理的 Python 3.11（解释器）→ venv（项目环境）。互不污染，可独立更新。

3. **哈希验证优先**（`scripts/install.sh` 的 install_deps）：`uv sync --locked` 用 `uv.lock` 的 SHA256 验证每个传递依赖，是唯一能抵御供应链攻击（如 mistralai 2.4.6 蠕虫）的安装路径。失败时多层降级保证可用性。

4. **UV_PYTHON 防漂移**：install.sh 多处 `export UV_PYTHON="$INSTALL_DIR/venv/bin/python"`，防止继承的 `UV_PYTHON=3.14` 环境变量让后续 uv 命令重建 venv 到错误版本。

5. **macOS PATH 扩展**：从 Dock 启动的 app 只继承最小 PATH，`apps/desktop/electron/backend-env.cjs` 显式注入 Homebrew 路径（`/opt/homebrew/bin` for Apple Silicon, `/usr/local/bin` for Intel），保证后端能发现 codex 等用户安装的 CLI 工具。

6. **editable install**：`pip install -e .` 让 venv 的 `site-packages` 指向源码目录，`hermes update` 只需 `git pull` 即可更新代码，无需重装依赖（除非 pyproject.toml 变化）。

7. **shim 清理环境变量**（`scripts/install.sh` 的 setup_path）：`~/.local/bin/hermes` shim 在 exec 前 `unset PYTHONPATH PYTHONHOME`，防止继承的环境变量让 launcher 导入错误 checkout 的模块。
