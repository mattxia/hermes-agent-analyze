# macOS DMG 安装的运行时集成分析

## 核心结论

**DMG 中不包含 Python 和 Node.js 运行时。** DMG 只打包了 Electron shell（约 111MB），所有运行时都在**首次启动时通过 `install.sh` 动态下载安装**到 `~/.hermes/` 目录。

---

## DMG 包含什么

根据 `apps/desktop/package.json` 的 `build` 配置：

```
Hermes-0.17.0-mac.dmg 内部:
├── Hermes.app/
│   ├── Contents/
│   │   ├── MacOS/Hermes                    ← Electron 主程序 (Node.js 运行时内嵌)
│   │   ├── Resources/
│   │   │   ├── app.asar                     ← 打包的 JS 代码 (main.cjs + React 前端)
│   │   │   ├── app.asar.unpacked/
│   │   │   │   └── dist/                    ← Vite 构建的 React 静态资源
│   │   │   ├── install-stamp.json           ← 构建时写入的 git commit 锁定
│   │   │   ├── native-deps/
│   │   │   │   └── node-pty/                ← node-pty 原生二进制 (darwin-arm64/x64)
│   │   │   │       └── prebuilds/darwin-arm64/pty.node
│   │   │   └── icon.ico
│   │   └── Frameworks/                      ← Chromium + Electron 框架
│   └── ...
└── Applications (快捷方式)
```

**关键：** `install-stamp.json` 记录了构建时的 git commit SHA，首次启动时用它来下载对应版本的 `install.sh`。

---

## 首次启动完整流程

```
┌─────────────────────────────────────────────────────────────────────────┐
│  用户双击 Hermes.dmg → 拖拽到 Applications → 启动 Hermes.app              │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  1. Electron 主进程启动 (main.cjs)                                       │
│                                                                         │
│  读取 install-stamp.json:                                                │
│    { commit: "a1b2c3d...", branch: "main", builtAt: "2026-..." }        │
│                                                                         │
│  解析路径:                                                               │
│    HERMES_HOME = ~/.hermes                                              │
│    ACTIVE_HERMES_ROOT = ~/.hermes/hermes-agent                          │
│    VENV_ROOT = ~/.hermes/hermes-agent/venv                              │
│    BOOTSTRAP_COMPLETE_MARKER = ~/.hermes/hermes-agent/.hermes-bootstrap-complete │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  2. resolveHermesBackend() — 查找已有 Python 后端                        │
│                                                                         │
│  优先级链:                                                               │
│    ① HERMES_DESKTOP_HERMES_ROOT 环境变量? → 否                          │
│    ② 开发模式源码树? → 否 (IS_PACKAGED=true)                            │
│    ③ isBootstrapComplete()? → 检查 ~/.hermes/hermes-agent/.hermes-bootstrap-complete │
│       → 文件不存在 → 否 (首次启动)                                       │
│    ④ PATH 上的 hermes 命令? → 否 (首次启动)                              │
│    ⑤ 系统 python3 + hermes_cli? → 否 (首次启动)                         │
│    ⑥ 返回 { kind: "bootstrap-needed" } → 触发首次安装                    │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  3. ensureRuntime() → runBootstrap() — 首次安装引导                      │
│                                                                         │
│  3a. 获取 install.sh:                                                    │
│      resolveInstallScript() 优先级:                                      │
│        ① 开发模式本地文件 → 否 (打包模式)                                │
│        ② ~/.hermes/bootstrap-cache/install-{commit}.sh (缓存) → 否      │
│        ③ 从 GitHub 下载:                                                │
│           https://raw.githubusercontent.com/NousResearch/hermes-agent/   │
│           {commit}/scripts/install.sh                                   │
│           → 保存到 ~/.hermes/bootstrap-cache/install-{commit}.sh        │
│        ④ 回退: 已安装的 agent 中的 install.sh                            │
│                                                                         │
│  3b. 获取 manifest (阶段列表):                                            │
│      bash install.sh --manifest --dir ~/.hermes/hermes-agent            │
│        --hermes-home ~/.hermes --commit {commit}                        │
│      → 输出 JSON: { stages: [prerequisites, repository, venv,           │
│          python-deps, node-deps, path, config, setup, gateway, complete]│
│      → 前端 UI 显示进度条和阶段列表                                      │
│                                                                         │
│  3c. 逐阶段执行 (每阶段独立 bash 进程):                                   │
│      bash install.sh --stage {name} --non-interactive --json ...        │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  4. install.sh 逐阶段执行 (macOS)                                        │
│                                                                         │
│  ┌─ Stage: prerequisites ─────────────────────────────────────────────┐ │
│  │  • install_uv: 下载 uv 到 ~/.local/bin/uv                          │ │
│  │  • check_python: uv python install 3.11                            │ │
│  │  • check_git: brew install git 或 xcode-select --install           │ │
│  │  • check_node:                                                     │ │
│  │    ├─ 系统 node ≥20.19 → 使用                                      │ │
│  │    └─ 无 → 下载 node-v22.x-darwin-{arm64|x64}.tar.xz              │ │
│  │           → 解压到 ~/.hermes/node/                                 │ │
│  │           → 符号链接到 ~/.local/bin/{node,npm,npx}                 │ │
│  │  • install_system_packages: brew install ripgrep ffmpeg            │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│  ┌─ Stage: repository ────────────────────────────────────────────────┐ │
│  │  git clone https://github.com/NousResearch/hermes-agent.git        │ │
│  │    → ~/.hermes/hermes-agent/                                       │ │
│  │  git checkout {commit}  (锁定到 DMG 构建时的版本)                   │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│  ┌─ Stage: venv ──────────────────────────────────────────────────────┐ │
│  │  uv venv venv --python 3.11                                        │ │
│  │    → ~/.hermes/hermes-agent/venv/bin/python                        │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│  ┌─ Stage: python-deps ───────────────────────────────────────────────┐ │
│  │  uv pip install -e ".[all]"                                        │ │
│  │    → 注册入口点: venv/bin/hermes → hermes_cli.main:main            │ │
│  │    → 安装 openai, pydantic, fastapi, uvicorn 等核心依赖              │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│  ┌─ Stage: node-deps ─────────────────────────────────────────────────┐ │
│  │  npm install (浏览器工具 + workspace 依赖)                           │ │
│  │    → ~/.hermes/hermes-agent/node_modules/                          │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│  ┌─ Stage: path ──────────────────────────────────────────────────────┐ │
│  │  ln -sf ~/.hermes/hermes-agent/venv/bin/hermes ~/.local/bin/hermes │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│  ┌─ Stage: config ────────────────────────────────────────────────────┐ │
│  │  复制配置模板、种子技能到 ~/.hermes/skills/                         │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│  ┌─ Stage: setup / gateway (跳过 — 非交互模式) ──────────────────────┐ │
│  │  → 由 Electron 前端 onboarding UI 接管 (API key, model 选择等)      │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│  ┌─ Stage: complete ──────────────────────────────────────────────────┐ │
│  │  写入 echo "git" > ~/.hermes/hermes-agent/.install_method          │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  5. Bootstrap 完成 → 写入完成标记                                        │
│                                                                         │
│  writeBootstrapMarker() 写入:                                            │
│    ~/.hermes/hermes-agent/.hermes-bootstrap-complete                     │
│                                                                         │
│  ensureRuntime() 重新调用 resolveHermesBackend()                        │
│    → isBootstrapComplete() = true                                       │
│    → createActiveBackend():                                             │
│      command = ~/.hermes/hermes-agent/venv/bin/python                   │
│      args = ["-m", "hermes_cli.main", "dashboard",                      │
│              "--no-open", "--host", "127.0.0.1", "--port", "0"]         │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  6. 启动 Python 后端 + 连接前端                                          │
│                                                                         │
│  spawn(venv/bin/python, ["-m", "hermes_cli.main", "dashboard", ...])    │
│                                                                         │
│  Python 后端:                                                            │
│    FastAPI + uvicorn → 监听 127.0.0.1:{随机端口}                         │
│    输出端口到 stdout → Electron 读取                                     │
│                                                                         │
│  Electron:                                                               │
│    BrowserWindow 加载 React 前端                                         │
│    前端通过 WebSocket 连接 Python 后端                                    │
│    用户看到 onboarding 向导 (选 provider/model/API key)                  │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 安装后的目录结构

```
~/.hermes/                               ← HERMES_HOME
├── hermes-agent/                        ← ACTIVE_HERMES_ROOT (git clone)
│   ├── .hermes-bootstrap-complete       ← Bootstrap 完成标记
│   ├── .install_method                  ← "git"
│   ├── venv/                            ← Python 虚拟环境 (uv 创建)
│   │   ├── bin/
│   │   │   ├── python                   ← Python 3.11
│   │   │   ├── hermes                   ← CLI 入口 (pip 注册)
│   │   │   └── ...
│   │   └── lib/python3.11/site-packages/
│   │       ├── hermes_cli/
│   │       ├── openai/
│   │       ├── fastapi/
│   │       └── ... (所有 Python 依赖)
│   ├── node_modules/                    ← Node.js 依赖 (npm install)
│   ├── tools/
│   ├── agent/
│   └── ... (完整仓库)
├── node/                                ← 便携 Node.js 22 LTS
│   └── bin/
│       ├── node
│       ├── npm
│       └── npx
├── bin/
│   └── uv                               ← uv 包管理器
├── bootstrap-cache/
│   └── install-{commit}.sh              ← 缓存的安装脚本
├── skills/                              ← 用户技能
└── logs/
    └── desktop.log                      ← 启动日志
```

---

## 关键设计决策

### 1. 为什么 DMG 不打包运行时？

| 方案 | 优势 | 劣势 | Hermes 的选择 |
|------|------|------|---------------|
| DMG 内打包 Python+Node | 离线可用、即装即用 | DMG 体积暴增（Python+依赖 ~500MB，Node ~100MB）；macOS arm64/x64 需分别打包；运行时版本锁定，难以热更新 | **否** |
| 首次启动下载 | DMG 体积小（~111MB）；自动适配 CPU 架构；运行时可热更新 | 首次启动需联网、耗时较长 | **是** |

### 2. install-stamp.json 的作用

**文件：** `apps/desktop/scripts/write-build-stamp.cjs`

构建 DMG 时，git commit SHA 被写入 `install-stamp.json` 并打包进 DMG。首次启动时：

```javascript
// main.cjs — loadInstallStamp()
// 从 process.resourcesPath/install-stamp.json 读取
const INSTALL_STAMP = loadInstallStamp();
// { commit: "a1b2c3d4e5f6...", branch: "main", source: "ci" }

// bootstrap-runner.cjs — resolveInstallScript()
// 用 commit SHA 从 GitHub 下载对应版本的 install.sh
const url = `https://raw.githubusercontent.com/NousResearch/hermes-agent/
             ${commit}/scripts/install.sh`
```

这确保 DMG 安装的 agent 代码版本与 DMG 构建时**完全一致**，不会拉到 main 分支的最新代码。

### 3. 非交互模式 — UI 接管交互阶段

**文件：** `apps/desktop/electron/bootstrap-runner.cjs` 第 548-555 行

`setup` 和 `gateway` 阶段需要用户输入（API key、模型选择），在 Desktop 模式下被跳过：

```javascript
// bootstrap-runner.cjs
const args = isPosix
  ? ['--stage', stage.name, '--non-interactive', '--json', ...]
  : ['-Stage', stage.name, '-NonInteractive', '-Json', ...]

// install.sh 中:
if [ "$NON_INTERACTIVE" = true ] && stage_needs_user_input "$stage"; then
    log_info "Skipping $stage (non-interactive bootstrap)"
    emit_stage_json "$stage" true true  # ok=true, skipped=true
    return 0
fi
```

这些交互步骤由 Electron 前端的 **onboarding 向导**接管，用户在 GUI 中完成配置。

### 4. 后续启动 — 直接使用已安装的运行时

首次启动完成后，后续启动直接走快速路径：

```javascript
// main.cjs — resolveHermesBackend()
// ③ isBootstrapComplete() → true (标记文件存在)
//   → createActiveBackend()
//     → command = ~/.hermes/hermes-agent/venv/bin/python
//     → args = ["-m", "hermes_cli.main", "dashboard", ...]
```

无需再运行 install.sh，直接 spawn Python 子进程。

---

## 完整时序图

```
用户           Electron (Node.js)           install.sh (bash)          GitHub/PyPI/nodejs.org
 │                   │                           │                           │
 │  双击 Hermes.app   │                           │                           │
 │──────────────────▶│                           │                           │
 │                   │                           │                           │
 │                   │  读取 install-stamp.json   │                           │
 │                   │  commit=a1b2c3d...        │                           │
 │                   │                           │                           │
 │                   │  resolveHermesBackend()   │                           │
 │                   │  → bootstrap-needed       │                           │
 │                   │                           │                           │
 │                   │  下载 install.sh           │                           │
 │                   │──────────────────────────────────────────────────────▶│
 │                   │◀──────────────────────────────────────────────────────│  install.sh
 │                   │                           │                           │
 │                   │  bash install.sh --manifest                          │
 │                   │──────────────────────────▶│                           │
 │                   │◀──────────────────────────│  JSON stages list         │
 │                   │                           │                           │
 │  显示进度 UI       │                           │                           │
 │◀──────────────────│                           │                           │
 │                   │                           │                           │
 │                   │  --stage prerequisites    │                           │
 │                   │──────────────────────────▶│                           │
 │                   │                           │  下载 uv                   │
 │                   │                           │──────────────────────────────────────▶│
 │                   │                           │  下载 Python 3.11          │
 │                   │                           │──────────────────────────────────────▶│
 │                   │                           │  brew install git          │
 │                   │                           │  下载 Node.js 22           │
 │                   │                           │──────────────────────────────────────────────▶│
 │                   │◀──────────────────────────│  {ok:true}                │
 │                   │                           │                           │
 │                   │  --stage repository       │                           │
 │                   │──────────────────────────▶│                           │
 │                   │                           │  git clone + checkout      │
 │                   │                           │──────────────────────────────────────▶│
 │                   │◀──────────────────────────│  {ok:true}                │
 │                   │                           │                           │
 │                   │  --stage venv             │                           │
 │                   │──────────────────────────▶│                           │
 │                   │                           │  uv venv venv              │
 │                   │◀──────────────────────────│  {ok:true}                │
 │                   │                           │                           │
 │                   │  --stage python-deps      │                           │
 │                   │──────────────────────────▶│                           │
 │                   │                           │  uv pip install -e .[all]  │
 │                   │                           │──────────────────────────────────────▶│ PyPI
 │                   │◀──────────────────────────│  {ok:true}                │
 │                   │                           │                           │
 │                   │  --stage node-deps        │                           │
 │                   │──────────────────────────▶│                           │
 │                   │                           │  npm install               │
 │                   │◀──────────────────────────│  {ok:true}                │
 │                   │                           │                           │
 │                   │  --stage complete         │                           │
 │                   │──────────────────────────▶│                           │
 │                   │◀──────────────────────────│  {ok:true}                │
 │                   │                           │                           │
 │                   │  写入 .hermes-bootstrap-complete                       │
 │                   │  spawn(python -m hermes_cli.main dashboard)           │
 │                   │                           │                           │
 │  onboarding 向导   │                           │                           │
 │◀──────────────────│  WebSocket ←→ Python 后端                               │
 │                   │                           │                           │
```

**总结：** DMG 本质上是一个**自引导的 Electron 壳**。它内嵌的 Node.js 运行时仅用于启动 Electron UI 和驱动安装流程；Python 和 Node.js（CLI 工具链）运行时都在首次启动时通过 `install.sh` 从网络动态安装到 `~/.hermes/`，之后所有 agent 逻辑都在 Python 子进程中运行。
