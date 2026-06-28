# macOS 上 Node.js 运行时的集成分析

> 对应 query：请分析本项目是在MacOS怎么完成Node.js运行时的集成的，包括Node.js运行时的安装、Node.js运行时支持Python相关脚本运行等

本项目在 macOS 上**不打包 Node.js 运行时**，而是在首次启动时通过 `scripts/install.sh` 动态下载官方 Node.js 二进制并集成到 `HERMES_HOME`。Node.js 主要服务于**浏览器工具（agent-browser + Playwright Chromium）**，而 Python Agent 后端本身不依赖 Node。

## 一、Node.js 运行时的安装链路

### 流程图：Node.js 运行时安装全过程

```
┌─────────────────────────────────────────────────────────────────┐
│ Hermes.app 首次启动                                             │
│ → runBootstrap() 调用 install.sh                                │
│ → Stage 1: prerequisites                                        │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
   ┌───────────────────────────────────────────────────────────┐
   │  check_node()  (scripts/install.sh)                       │
   │                                                           │
   │  先修复 npm 全局前缀（幂等，每次运行都执行）:              │
   │  configure_managed_node_npm_prefix()                      │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  步骤 1: 检查系统 Node                                     │
   │  command -v node && node_satisfies_build "$(node --version)"│
   │                                                           │
   │  node_satisfies_build 要求:                               │
   │    major==20 && minor>=19  → 满足                         │
   │    major>=22 && minor>=12  → 满足                         │
   │  (与 package.json engines 字段一致)                       │
   └────────────┬─────────────────────────────────┬────────────┘
                │ 满足                            │ 不满足
                ▼                                 ▼
   ┌──────────────────────┐    ┌──────────────────────────────────┐
   │ HAS_NODE=true        │    │ 步骤 2: 检查 Hermes 自管 Node    │
   │ 直接复用系统 Node     │    │ ~/.hermes/node/bin/node          │
   └──────────────────────┘    │ node_satisfies_build?            │
                               └────────────┬─────────────────────┘
                                            │ 满足
                                            ▼
                               ┌──────────────────────┐
                               │ export PATH=         │
                               │   ~/.hermes/node/bin │
                               │   :$PATH             │
                               │ HAS_NODE=true        │
                               └──────────────────────┘
                                            │ 不满足
                                            ▼
                               ┌──────────────────────────────────┐
                               │  install_node()                   │
                               │  (scripts/install.sh)             │
                               └────────────┬─────────────────────┘
                                            │
                                            ▼
   ┌───────────────────────────────────────────────────────────┐
   │  install_node()  (macOS 路径)                              │
   │                                                           │
   │  1. 解析架构: uname -m                                     │
   │     x86_64 → x64                                          │
   │     arm64 → arm64                                         │
   │                                                           │
   │  2. 解析 OS: macos → darwin                               │
   │                                                           │
   │  3. 从 nodejs.org 解析最新 v22.x.x tarball:               │
   │     curl https://nodejs.org/dist/latest-v22.x/            │
   │     grep node-v22.x.x-darwin-<arch>.tar.xz                │
   │                                                           │
   │  4. 下载到临时目录                                         │
   │                                                           │
   │  5. 解压并移动到 ~/.hermes/node/                           │
   │     rm -rf ~/.hermes/node                                 │
   │     mv extracted ~/.hermes/node                           │
   │                                                           │
   │  6. 符号链接到命令目录 (get_command_link_dir):             │
   │     ~/.local/bin/node  → ~/.hermes/node/bin/node          │
   │     ~/.local/bin/npm   → ~/.hermes/node/bin/npm           │
   │     ~/.local/bin/npx   → ~/.hermes/node/bin/npx           │
   │                                                           │
   │  7. configure_managed_node_npm_prefix()                   │
   │                                                           │
   │  8. export PATH=~/.hermes/node/bin:$PATH                  │
   │                                                           │
   │  → HAS_NODE=true                                          │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  configure_managed_node_npm_prefix()                       │
   │  (scripts/install.sh)                                     │
   │                                                           │
   │  问题: npm 默认全局前缀 = Node 安装目录本身                │
   │    → npm install -g 的 bin 落到 ~/.hermes/node/bin/        │
   │    → 该目录不在 PATH 上（只有 link_dir 在）                │
   │    → Node 升级时 ~/.hermes/node 被清空，全局包丢失         │
   │                                                           │
   │  修复: 写 ~/.hermes/node/etc/npmrc:                       │
   │    prefix=~/.local    (link_dir 的父目录)                  │
   │                                                           │
   │  效果: npm install -g 的 bin 落到 ~/.local/bin/            │
   │    （与 node/npm/npx 链接同目录，已在 PATH 上）             │
   │    且不随 Node 升级丢失                                    │
   │                                                           │
   │  作用域: 仅影响 Hermes 自管 Node（prefix-local npmrc）     │
   │  不污染用户 ~/.npmrc 或其他 Node 安装                      │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  Stage 5: node-deps                                        │
   │  install_node_deps() (scripts/install.sh)                 │
   │                                                           │
   │  1. cd ~/.hermes/hermes-agent/ (INSTALL_DIR)              │
   │     npm install --silent                                   │
   │     → 安装 package.json 依赖到 node_modules/               │
   │       (含 agent-browser, @streamdown/math 等)              │
   │                                                           │
   │  2. 安装 Playwright Chromium:                             │
   │     npx playwright install chromium                       │
   │     → 下载 Chromium 浏览器引擎                             │
   │                                                           │
   │  3. 安装 TUI 依赖 (若 ui-tui/package.json 存在):          │
   │     cd ui-tui && npm install                              │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  后续: ensure_browser() 按需安装 agent-browser             │
   │  (scripts/install.sh, dep_ensure.py 触发)                 │
   │                                                           │
   │  npm install -g --prefix ~/.hermes/node \                 │
   │    agent-browser@^0.26.0 \                                │
   │    @askjo/camofox-browser@^1.5.2                          │
   │                                                           │
   │  → bin 落到 ~/.local/bin/ (因 npm prefix 已配置)           │
   │                                                           │
   │  然后: agent-browser install                              │
   │  → 下载 agent-browser 自己的 Chromium（若未检测到系统浏览器）│
   └───────────────────────────────────────────────────────────┘
```

## 二、Node.js 运行时的最终目录布局

```
~/.hermes/                                    ← HERMES_HOME
├── bin/
│   └── uv                                    ← Python 包管理器
├── node/                                     ← Hermes 自管 Node.js
│   ├── bin/
│   │   ├── node                              ← Node.js v22.x.x
│   │   ├── npm
│   │   ├── npx
│   │   └── agent-browser                     ← (npm -g 安装的 CLI)
│   ├── etc/
│   │   └── npmrc                             ← prefix=~/.local
│   ├── lib/node_modules/                     ← 全局 npm 包
│   │   ├── agent-browser/
│   │   └── @askjo/camofox-browser/
│   └── ...                                   ← Node.js 标准目录
├── hermes-agent/                             ← Agent 代码 (INSTALL_DIR)
│   ├── node_modules/                         ← 项目级 npm 依赖
│   │   ├── agent-browser/                    ← (package.json 依赖)
│   │   ├── @streamdown/math/
│   │   └── ...
│   ├── package.json
│   ├── package-lock.json
│   ├── ui-tui/
│   │   └── node_modules/                     ← TUI 依赖
│   └── ...
└── logs/

~/.local/bin/                                 ← 命令链接目录 (在 PATH 上)
├── node       → ~/.hermes/node/bin/node      ← 符号链接
├── npm        → ~/.hermes/node/bin/npm
├── npx        → ~/.hermes/node/bin/npx
├── hermes     → (shim 调用 venv/bin/hermes)
└── agent-browser → (npm -g 安装的 CLI)
```

## 三、Node.js 如何支持 Python 脚本运行

**关键点：Node.js 不直接支持 Python 脚本运行**。两者是平行的运行时，通过以下方式协作：

### 1. Electron 后端进程的 PATH 注入（核心集成点）

`apps/desktop/electron/backend-env.cjs` 为 Python 后端进程构造 PATH 时，**显式将 Node bin 目录放在最前面**：

```
PATH 构造顺序 (backend-env.cjs 的 buildDesktopBackendPath):
  ① ~/.hermes/node/bin          ← Node.js bin（agent-browser 等）
  ② ~/.hermes/hermes-agent/venv/bin  ← Python venv bin（hermes 命令）
  ③ <继承的系统 PATH>
  ④ /opt/homebrew/bin           ← Apple Silicon Homebrew
  ⑤ /opt/homebrew/sbin
  ⑥ /usr/local/sbin
  ⑦ /usr/local/bin              ← Intel Homebrew
  ⑧ /usr/sbin, /usr/bin, /sbin, /bin
```

这样 Python Agent 后端通过 `subprocess` 调用 `agent-browser` 或 `node` 时，能直接在 PATH 上找到。

### 2. Python Agent 调用 Node 工具的方式

Python 侧通过 `hermes_cli/dep_ensure.py` 检测和触发 Node 依赖安装：

```
┌──────────────────────────────────────────────────────────────────┐
│  Python Agent 运行时需要 Node 工具（如浏览器工具）               │
│  → dep_ensure.py 检测                                           │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
   ┌───────────────────────────────────────────────────────────┐
   │  ensure_dependency("node")  (hermes_cli/dep_ensure.py)    │
   │                                                           │
   │  检测: shutil.which("node") is not None                   │
   │  若已装 → 返回 True                                        │
   │                                                           │
   │  若未装 → 查找 install.sh:                                 │
   │    ① hermes_cli/scripts/install.sh (wheel 内)             │
   │    ② <repo_root>/scripts/install.sh (git checkout)        │
   │                                                           │
   │  → 交互式提示: "Node.js is not installed. Install now?"   │
   │                                                           │
   │  → bash install.sh --ensure node                          │
   │    (调用 install.sh 的 --ensure 模式，只装指定依赖)        │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  ensure_mode()  (scripts/install.sh)                      │
   │                                                           │
   │  dep="node" → check_node()                                │
   │    → install_node() (若需要)                              │
   │                                                           │
   │  dep="browser" → check_node() + ensure_browser()          │
   │    → npm install -g agent-browser                         │
   │    → agent-browser install (下载 Chromium)                │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  安装完成后，Python Agent 通过 subprocess 调用:            │
   │                                                           │
   │  • agent-browser (浏览器工具)                             │
   │    → shutil.which("agent-browser")                        │
   │      → ~/.local/bin/agent-browser (因 npm prefix 配置)    │
   │      或 ~/.hermes/node/bin/agent-browser                  │
   │                                                           │
   │  • node (执行 JS 脚本)                                    │
   │    → shutil.which("node")                                 │
   │      → ~/.local/bin/node → ~/.hermes/node/bin/node        │
   │                                                           │
   │  检测逻辑 (hermes_cli/dep_ensure.py):                    │
   │    _has_hermes_agent_browser() 检查:                      │
   │    ~/.hermes/node/bin/agent-browser                       │
   │    或 ~/.hermes/node_modules/.bin/agent-browser           │
   └───────────────────────────────────────────────────────────┘
```

### 3. 浏览器工具的调用链（Node 与 Python 协作）

```
┌─────────────────────────────────────────────────────────────┐
│  Python Agent 决定使用浏览器工具                            │
│  (如浏览网页、截图、自动化操作)                              │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
   ┌───────────────────────────────────────────────────────────┐
   │  dep_ensure.ensure_dependency("browser")                  │
   │  检测: agent_browser_runnable() 或 _has_system_browser()  │
   │  (hermes_cli/dep_ensure.py)                              │
   │                                                           │
   │  检测优先级:                                              │
   │    1. PATH 上的 agent-browser CLI (能实际运行)            │
   │    2. 系统浏览器 (google-chrome/chromium)                 │
   │    3. ~/.hermes/node/bin/agent-browser                    │
   │       或 ~/.hermes/node_modules/.bin/agent-browser        │
   └──────────────────────────┬────────────────────────────────┘
                              │ 已就绪
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  Python subprocess 调用 agent-browser                     │
   │                                                           │
   │  subprocess.Popen(["agent-browser", ...])                 │
   │  → 实际执行 ~/.hermes/node/bin/node                       │
   │    运行 agent-browser 的 JS 入口                           │
   │                                                           │
   │  agent-browser 内部:                                      │
   │    → 启动 Playwright/CDP 控制 Chromium                    │
   │    → 通过 stdio JSON 协议与 Python 通信                   │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
   ┌───────────────────────────────────────────────────────────┐
   │  Chromium 浏览器引擎                                       │
   │  来源:                                                     │
   │    ① agent-browser install 下载的                          │
   │    ② npx playwright install chromium 下载的                │
   │    ③ 系统浏览器 (Chrome/Chromium)                          │
   └───────────────────────────────────────────────────────────┘
```

## 四、关键设计要点

1. **Node.js 是可选依赖**：`scripts/install.sh` 的 `check_node` 明确注释 "for browser tools"。Node 缺失时 Agent 仍可运行，只是浏览器工具不可用（`hermes_cli/dep_ensure.py`："browser tool needs agent-browser"）。

2. **Hermes 自管 Node，与系统 Node 隔离**：Node 装在 `~/.hermes/node/`，通过符号链接暴露到 `~/.local/bin/`。不污染系统 Node，也不被系统 Node 升级影响。`configure_managed_node_npm_prefix` 只写 `~/.hermes/node/etc/npmrc`（prefix-local），不碰用户 `~/.npmrc`。

3. **npm 全局前缀重定向**（`scripts/install.sh` 的 `configure_managed_node_npm_prefix`）：默认 npm 全局 bin 落在 Node 目录内（不在 PATH 上，且升级时被清空）。重定向 `prefix=~/.local` 让全局包 bin 落到 `~/.local/bin/`（已在 PATH 上，且跨 Node 升级存活）。

4. **版本要求与 engines 一致**：`node_satisfies_build` 要求 `^20.19 || >=22.12`，与根 `package.json` 的 `engines` 字段一致，保证 npm workspace 依赖能正确解析。

5. **两套 Node 依赖安装路径**：
   - **项目级**（`npm install` in INSTALL_DIR）：装 `package.json` 的 `dependencies`（agent-browser、@streamdown/math 等）到 `node_modules/`
   - **全局级**（`npm install -g --prefix ~/.hermes/node`）：装 `agent-browser` CLI 到 `~/.hermes/node/bin/`，供 Python 侧 `shutil.which("agent-browser")` 发现

6. **Python 与 Node 的协作模式是进程间通信**：Python 通过 `subprocess` spawn Node 进程（agent-browser），通过 stdio JSON 协议交换数据。Node 不是 Python 的"宿主"或"解释器"，而是平行的工具运行时。

7. **懒加载安装**（`hermes_cli/dep_ensure.py`）：Node 和浏览器依赖在**首次使用时才检测并提示安装**，而非 Agent 启动时强制。`ensure_dependency` 在 TTY 环境下交互式询问，非交互环境直接调用 install.sh `--ensure` 模式。

8. **Electron 后端 PATH 注入顺序**（`apps/desktop/electron/backend-env.cjs` 的 `buildDesktopBackendPath`）：`~/.hermes/node/bin` 排在 `venv/bin` 之前，确保 Python 后端 subprocess 调用 `agent-browser` 时优先命中 Hermes 自管版本，而非系统可能存在的旧版。