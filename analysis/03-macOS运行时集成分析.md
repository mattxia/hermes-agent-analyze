# macOS 上的运行时集成分析

## 结论

macOS 上的架构与 Windows **基本一致**——双运行时（Python + Node.js），Node.js（Electron）做 UI 外壳，Python 子进程做 agent 核心。差异主要体现在以下方面：

| 维度 | Windows | macOS |
|------|---------|-------|
| 安装脚本 | `install.ps1` (PowerShell) | `install.sh` (Bash 3.2 兼容) |
| Git 安装 | MinGit 便携包 | Homebrew 或 Command Line Tools |
| Node.js 安装 | 便携 zip 到 `%LOCALAPPDATA%\hermes\node` | tar.xz 到 `~/.hermes/node` |
| Python venv 路径 | `venv\Scripts\python.exe` | `venv/bin/python` |
| 无控制台启动 | `pythonw.exe` | 直接用 `python`（macOS 无控制台窗口问题） |
| Desktop 产物 | NSIS `.exe` + MSI | DMG + ZIP |
| 代码签名 | 可选 (signtool) | ad-hoc 签名或 Developer ID 公证 |

---

## macOS 安装流程图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    macOS 安装流程 (install.sh)                            │
│                                                                         │
│  $ curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash   │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  1. 检测 OS: uname → "Darwin" → OS="macos"                        │  │
│  │                                                                   │  │
│  │  2. 安装 uv (Python 包管理器)                                       │  │
│  │     curl https://astral.sh/uv/install.sh | sh                     │  │
│  │     → ~/.local/bin/uv                                             │  │
│  │                                                                   │  │
│  │  3. 安装 Python 3.11                                              │  │
│  │     uv python install 3.11                                        │  │
│  │                                                                   │  │
│  │  4. 安装 Git                                                       │  │
│  │     ├─ 有 Homebrew → brew install git                             │  │
│  │     └─ 无 Homebrew → xcode-select --install (Apple CLT)           │  │
│  │         └─ 弹出系统对话框，等待用户点击"安装"                           │  │
│  │                                                                   │  │
│  │  5. 安装 Node.js 22 LTS                                           │  │
│  │     检测架构:                                                      │  │
│  │       uname -m → arm64 (Apple Silicon) 或 x86_64 (Intel)          │  │
│  │     下载: node-v22.x.x-darwin-{arm64|x64}.tar.xz                  │  │
│  │     解压到: ~/.hermes/node/                                       │  │
│  │     符号链接: ~/.local/bin/node, npm, npx → ~/.hermes/node/bin/*   │  │
│  │                                                                   │  │
│  │  6. 安装系统包 (ripgrep, ffmpeg)                                    │  │
│  │     brew install ripgrep ffmpeg                                   │  │
│  │                                                                   │  │
│  │  7. 克隆仓库 → ~/.hermes/hermes-agent                              │  │
│  │                                                                   │  │
│  │  8. 创建 venv: uv venv venv --python 3.11                         │  │
│  │                                                                   │  │
│  │  9. 安装 Python 依赖: uv pip install -e ".[all]"                  │  │
│  │                                                                   │  │
│  │ 10. 安装 Node.js 依赖: npm install (workspace + browser tools)     │  │
│  │                                                                   │  │
│  │ 11. [可选] 构建 Desktop: npm run pack                              │  │
│  │     └─ ad-hoc 签名 + 去隔离属性                                     │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## macOS 上的关键差异实现

### 1. Python venv 路径差异

**文件：** `apps/desktop/electron/main.cjs` 第 10353-10367 行

```javascript
function getVenvPython(venvRoot) {
  // macOS: venv/bin/python    Windows: venv\Scripts\python.exe
  return path.join(venvRoot,
    IS_WINDOWS ? path.join("Scripts", "python.exe")
               : path.join("bin", "python"));
}

function getNoConsoleVenvPython(venvRoot) {
  // macOS: 直接用 python（无控制台窗口问题）
  // Windows: 用 pythonw.exe（避免弹出黑色 cmd 窗口）
  if (!IS_WINDOWS) return getVenvPython(venvRoot);
  const venvPythonw = path.join(venvRoot, "Scripts", "pythonw.exe");
  // ...
}
```

### 2. 系统 Python 探测差异

**文件：** `apps/desktop/electron/main.cjs` 第 10275 行

```javascript
function findSystemPython() {
  if (!IS_WINDOWS) {
    // macOS/Linux: 直接在 PATH 上找 python3 → python
    for (const command of ["python3", "python"]) {
      const candidate = findOnPath(command);
      if (candidate) return candidate;
    }
    return null;
  }
  // Windows: 查注册表 HKLM/HKCU\SOFTWARE\Python\PythonCore\3.11\InstallPath
  // ... (复杂的多路径注册表探测)
}
```

### 3. Git 安装：Homebrew 优先 + CLT 回退

**文件：** `scripts/install.sh` 第 655-672 行

```bash
attempt_install_git() {
    case "$OS" in
        macos)
            # 优先 Homebrew（完全无头）
            if command -v brew >/dev/null 2>&1; then
                brew install git >/dev/null 2>&1
            fi
            # 回退到 Apple Command Line Tools（弹系统对话框）
            if command -v xcode-select >/dev/null 2>&1; then
                xcode-select --install
                # 轮询等待，最长 15 分钟
                while [ "$waited" -lt 900 ]; do
                    command -v git >/dev/null 2>&1 && return 0
                    sleep 5; waited=$((waited + 5))
                done
            fi
            ;;
    esac
}
```

> 注意：macOS 的 `/usr/bin/git` 在未安装 CLT 时是一个 **stub**，会非零退出。install.sh 用 `git --version` 而非 `command -v git` 来检测。

### 4. Node.js 安装：架构感知的便携包

**文件：** `scripts/install.sh` 第 825-930 行

```bash
install_node() {
    local arch=$(uname -m)
    case "$arch" in
        x86_64)        node_arch="x64"    ;;   # Intel Mac
        aarch64|arm64) node_arch="arm64"  ;;   # Apple Silicon (M1/M2/M3)
    esac

    local node_os
    case "$OS" in
        macos) node_os="darwin" ;;
    esac

    # 从 nodejs.org 下载 darwin-arm64 或 darwin-x64 的 tar.xz
    local index_url="https://nodejs.org/dist/latest-v${NODE_VERSION}.x/"
    tarball_name=$(curl -fsSL "$index_url" \
        | grep -oE "node-v${NODE_VERSION}\.[0-9]+\.[0-9]+-${node_os}-${node_arch}\.tar\.xz")

    # 解压到 ~/.hermes/node/
    mv "$extracted_dir" "$HERMES_HOME/node"

    # 符号链接到 ~/.local/bin/（用户级，无需 sudo）
    ln -sf "$HERMES_HOME/node/bin/node" "$node_link_dir/node"
    ln -sf "$HERMES_HOME/node/bin/npm"  "$node_link_dir/npm"
}
```

### 5. Desktop 构建与签名

**文件：** `scripts/install.sh` 第 2689-2697 行

macOS 上的本地构建使用 **ad-hoc 签名**（无需 Apple 开发者账号）：

```bash
# macOS: 让本地构建的 (ad-hoc) app 在原地自更新后仍可启动
# ad-hoc bundle 没有稳定的 Designated Requirement，原地重建后
# 新的 cdhash + 继承的隔离标志会触发 Gatekeeper 报"已损坏"
# 解决：去除隔离属性 + 重新深度 ad-hoc 签名
if [ "$OS" = "macos" ] \
   && [ -z "${CSC_LINK:-}" ] \
   && [ -z "${APPLE_SIGNING_IDENTITY:-}" ] \
   && command -v codesign >/dev/null 2>&1; then
    xattr -cr "$app" 2>/dev/null                    # 去除 com.apple.quarantine
    codesign --force --deep --sign - "$app"          # ad-hoc 重新签名
fi
```

正式发布版本的签名与公证在 `apps/desktop/scripts/notarize.cjs` 中：

```javascript
// apps/desktop/scripts/notarize.cjs
exports.default = async function notarize(context) {
  if (electronPlatformName !== 'darwin') return

  // 方式 1: App Store Connect API Key (推荐)
  // APPLE_API_KEY + APPLE_API_KEY_ID + APPLE_API_ISSUER
  await run('xcrun', ['notarytool', 'submit', zipPath,
    '--key', keyPath, '--key-id', keyId, '--issuer', issuer, '--wait'])

  // 方式 2: Keychain profile
  // APPLE_NOTARY_PROFILE
  await run('xcrun', ['notarytool', 'submit', zipPath,
    '--keychain-profile', profile, '--wait'])

  // 装订公证票据
  await run('xcrun', ['stapler', 'staple', '-v', appPath])
}
```

macOS 的 entitlements（`apps/desktop/electron/entitlements.mac.plist`）允许 JIT、无符号可执行内存和音频输入（语音功能需要）。

### 6. Electron 缓存路径差异

**文件：** `scripts/install.sh` 第 2411 行

```bash
# macOS: ~/Library/Caches/electron
# Linux: ~/.cache/electron (或 $XDG_CACHE_HOME/electron)
if [ "$OS" = "macos" ]; then
    cache_dirs+=("$HOME/Library/Caches/electron")
else
    cache_dirs+=("$HOME/.cache/electron")
fi
```

### 7. 运行时 PTY 差异

**文件：** `apps/desktop/electron/bootstrap-platform.cjs` 第 37 行

```javascript
function bundledRuntimeImportCheck(platform = process.platform) {
  // Windows 用 winpty，macOS/Linux 用 ptyprocess
  return platform === "win32"
    ? "import fastapi, uvicorn, winpty"
    : "import fastapi, uvicorn, ptyprocess"
}
```

对应的 node-pty 原生二进制也不同：
- macOS: `prebuilds/darwin-arm64/pty.node` + `spawn-helper`
- macOS: `prebuilds/darwin-x64/pty.node` + `spawn-helper`

---

## macOS 运行时协作架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│                   macOS Desktop 应用进程架构                                │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Electron 主进程 (Node.js 运行时 — Hermes.app)                       │  │
│  │  Contents/MacOS/Hermes                                              │  │
│  │  ├── resolveHermesBackend()                                         │  │
│  │  │   优先级:                                                        │  │
│  │  │   1. HERMES_DESKTOP_HERMES_ROOT 环境变量                          │  │
│  │  │   2. 开发模式源码树                                               │  │
│  │  │   3. ~/.hermes/hermes-agent/venv/bin/python (已安装)              │  │
│  │  │   4. PATH 上的 hermes 命令                                        │  │
│  │  │   5. 系统 python3 + hermes_cli 模块                              │  │
│  │  │   6. bootstrap-needed → 运行 install.sh                           │  │
│  │  │                                                                 │  │
│  │  ├── spawn(python, -m hermes_cli.main dashboard ...)                │  │
│  │  └── codesign ad-hoc 签名验证 (更新后修复)                            │  │
│  └──────────────────────────┬─────────────────────────────────────────┘  │
│                            │ spawn + WebSocket                            │
│  ┌──────────────────────────▼─────────────────────────────────────────┐  │
│  │  Python 后端子进程 (Python 运行时)                                    │  │
│  │  ~/.hermes/hermes-agent/venv/bin/python                             │  │
│  │  ├── hermes_cli.main dashboard                                      │  │
│  │  ├── FastAPI + uvicorn (127.0.0.1:随机端口)                          │  │
│  │  ├── AIAgent (run_agent.py)                                         │  │
│  │  │   └── terminal_tool → ptyprocess (macOS PTY)                     │  │
│  │  └── WebSocket → 流式输出到 Electron 前端                             │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Electron 渲染进程 (Chromium)                                        │  │
│  │  Contents/Resources/app.asar/dist/index.html                        │  │
│  │  └── React 前端 ← WebSocket → Python 后端                            │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘

目录布局 (macOS):
  ~/.hermes/                          ← HERMES_HOME
  ├── hermes-agent/                   ← 代码仓库
  │   ├── venv/bin/python             ← Python venv (uv 创建)
  │   ├── venv/bin/hermes             ← CLI 入口点 (pip 安装)
  │   └── ...
  ├── node/bin/node                   ← 便携 Node.js 22 LTS
  ├── bin/uv                          ← uv 包管理器
  └── git/                            ← (仅 Windows; macOS 用系统 Git)
```

**核心设计不变：** 无论 macOS 还是 Windows，Node.js（Electron）只负责 UI 外壳和进程管理，所有 AI agent 逻辑都在 Python 子进程中运行。两个运行时通过 localhost WebSocket 通信。
