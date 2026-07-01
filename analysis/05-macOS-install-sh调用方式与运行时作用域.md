# macOS 上 install.sh 的调用方式与运行时作用域分析

## 一、调用方式：Electron → Node.js child_process.spawn → bash

Electron 主进程通过 **Node.js 的 `child_process.spawn`** 直接调用系统 `bash` 执行 `install.sh`，逐阶段子进程运行。

### 调用链

```
main.cjs: ensureRuntime()
  → resolveHermesBackend() 返回 { kind: "bootstrap-needed" }
  → runBootstrap()                          ← bootstrap-runner.cjs 第613行
      │
      ├─ 1. resolveInstallScript()           ← 从 GitHub 下载 install.sh
      │     下载到 ~/.hermes/bootstrap-cache/install-{commit}.sh
      │
      ├─ 2. fetchManifest()                  ← 获取阶段列表
      │     spawnBash(scriptPath, ["--manifest", "--dir", ..., "--commit", ...])
      │       │
      │       └─ spawn('bash', [scriptPath, ...args])   ← 第393行
      │            stdio: ['ignore', 'pipe', 'pipe']
      │            env: { ...process.env, HERMES_HOME: "~/.hermes" }
      │
      ├─ 3. for each stage in manifest.stages:
      │     runStage()
      │       └─ spawnBash(scriptPath, ["--stage", stage.name, 
      │                                  "--non-interactive", "--json", ...])
      │            │
      │            └─ spawn('bash', [scriptPath, ...args])  ← 新子进程每阶段
      │
      └─ 4. writeBootstrapMarker()           ← 写入完成标记
```

### 核心代码

**spawn 调用** — `apps/desktop/electron/bootstrap-runner.cjs` 第 379-393 行：

```javascript
function spawnBash(scriptPath, args, { emit, stageName, abortSignal, hermesHome } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn('bash', [scriptPath, ...args], {   // ← 直接调系统 bash
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        HERMES_HOME: hermesHome || process.env.HERMES_HOME || ''
      }
    })
    // 逐行读取 stdout/stderr → emit 事件 → 前端进度条
    child.stdout.on('data', chunk => {
      // 按行分割，emit({ type: 'log', stage, line, stream: 'stdout' })
    })
    child.on('close', (code) => resolve({ stdout, stderr, code }))
  })
}
```

**关键点：**
- 用的是 **macOS 自带的 `/bin/bash`**（Bash 3.2），不依赖任何额外安装
- `install.sh` 脚本本身是 **POSIX sh + Bash 兼容**写的，不使用 Bash 4+ 特性
- 每个阶段是**独立 bash 子进程**，失败可重试单阶段，不需要从头开始
- 通过 `--non-interactive --json` 参数让 install.sh 输出结构化 JSON，Electron 解析后更新 UI 进度

---

## 二、运行时作用域：用户级单应用，非系统全局

**结论：所有运行时都安装在 `~/.hermes/` 下，仅供 Hermes 使用，不污染系统全局环境。**

### 安装路径决策

**install.sh 第 395-442 行** `resolve_install_layout()`：

```bash
resolve_install_layout() {
    # macOS（非 root）走默认分支：
    INSTALL_DIR="$HERMES_HOME/hermes-agent"    # = ~/.hermes/hermes-agent

    # 注意：macOS root 也不走 FHS，因为 /usr/local/ 是 Homebrew 领地
    # if [ "$OS" = "linux" ] && [ "$(id -u)" -eq 0 ]; then
    #     INSTALL_DIR="/usr/local/lib/hermes-agent"  ← 仅 Linux root
}
```

### 各运行时安装位置

| 运行时 | 安装路径 | 作用域 | 是否系统全局 |
|--------|---------|--------|-------------|
| **uv** | `~/.hermes/bin/uv` | Hermes 专属 | 否 |
| **Python 3.11** | uv 管理目录（`~/.local/share/uv/python/`） | uv 管理，venv 内引用 | 否 |
| **Python venv** | `~/.hermes/hermes-agent/venv/` | Hermes 专属 | 否 |
| **Node.js 22** | `~/.hermes/node/` | Hermes 专属 | 否 |
| **hermes CLI** | `~/.local/bin/hermes`（符号链接） | 用户级 | 半全局* |
| **Git** | Homebrew `/opt/homebrew/bin/git` 或 Apple CLT | 系统级 | 是** |

> *`~/.local/bin/hermes` 是用户级路径，非系统级。
> **Git 是系统级安装，但它是通用开发工具，且 macOS 自带 stub。

### install.sh 中的隔离机制

**1. uv — 强制安装到 Hermes 目录**

```bash
# install.sh 第 577 行
UV_UNMANAGED_INSTALL="$HERMES_HOME/bin" sh "$_uv_installer"
# UV_UNMANAGED_INSTALL 让 astral 安装器放到 ~/.hermes/bin/ 而非默认 ~/.local/bin/
```

**2. Node.js — 完全隔离在 ~/.hermes/node/**

```bash
# install.sh 第 927-933 行
mv "$extracted_dir" "$HERMES_HOME/node"         # 二进制放在 ~/.hermes/node/

# 仅符号链接到 ~/.local/bin/（用户级，非 /usr/local/bin）
ln -sf "$HERMES_HOME/node/bin/node" "$node_link_dir/node"   # ~/.local/bin/node
ln -sf "$HERMES_HOME/node/bin/npm"  "$node_link_dir/npm"
ln -sf "$HERMES_HOME/node/bin/npx"  "$node_link_dir/npx"
```

**3. npm 全局前缀 — 隔离 npmrc，不碰用户 ~/.npmrc**

```bash
# install.sh 第 476-484 行
configure_managed_node_npm_prefix() {
    [ -x "$HERMES_HOME/node/bin/npm" ] || return 0
    local link_dir
    link_dir="$(get_command_link_dir)"              # ~/.local/bin
    mkdir -p "$HERMES_HOME/node/etc"
    printf 'prefix=%s\n' "$(dirname "$link_dir")" > "$HERMES_HOME/node/etc/npmrc"
    # 写入 ~/.hermes/node/etc/npmrc，而非 ~/.npmrc
    # 用户的其它 Node.js 安装不受影响
}
```

**4. Python venv — 完全隔离**

```bash
# install.sh 创建 venv
uv venv venv --python 3.11
# → ~/.hermes/hermes-agent/venv/bin/python
# 所有 pip 包安装到 venv 内，不碰系统 Python
uv pip install -e ".[all]"
```

### 唯一的"系统级"修改：shell PATH

install.sh 会在用户 shell 配置文件中添加 `~/.local/bin` 到 PATH（如果尚不存在）：

```bash
# install.sh 第 1683-1732 行
# macOS 默认 shell 是 zsh:
case "$LOGIN_SHELL" in
    zsh)
        [ -f "$HOME/.zshrc" ] && SHELL_CONFIGS+=("$HOME/.zshrc")
        [ -f "$HOME/.zprofile" ] && SHELL_CONFIGS+=("$HOME/.zprofile")
        # 如果都不存在，创建 ~/.zshrc（fresh macOS 常见）
        if [ ${#SHELL_CONFIGS[@]} -eq 0 ]; then
            touch "$HOME/.zshrc"
            SHELL_CONFIGS+=("$HOME/.zshrc")
        fi
        ;;
esac

PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
for SHELL_CONFIG in "${SHELL_CONFIGS[@]}"; do
    # 仅当文件中尚未包含 .local/bin 时才追加
    if ! grep -qE 'PATH=.*\.local/bin' "$SHELL_CONFIG"; then
        echo "# Hermes Agent — ensure ~/.local/bin is on PATH" >> "$SHELL_CONFIG"
        echo "$PATH_LINE" >> "$SHELL_CONFIG"
    fi
done
```

这行的目的是让用户在终端中能直接输入 `hermes` 命令。**不修改 `/etc/paths`、`/etc/profile` 等系统级文件**。

---

## 完整路径图

```
~/.hermes/                              ← HERMES_HOME (Hermes 专属根目录)
├── bin/
│   └── uv                              ← uv 包管理器 (Hermes 专属)
├── node/                               ← Node.js 22 LTS (Hermes 专属)
│   ├── bin/
│   │   ├── node                        ← 实际二进制
│   │   ├── npm
│   │   └── npx
│   └── etc/
│       └── npmrc                       ← 隔离的 npm 全局前缀配置
├── hermes-agent/                       ← 代码仓库 + Python 环境
│   ├── venv/                           ← Python 虚拟环境 (完全隔离)
│   │   └── bin/
│   │       ├── python                  ← Python 3.11 (仅 venv 内可用)
│   │       └── hermes                  ← CLI 入口点
│   ├── node_modules/                   ← Node.js 依赖
│   └── ... (源码)
├── bootstrap-cache/
│   └── install-{commit}.sh             ← 缓存的安装脚本
├── skills/                             ← 用户技能
└── logs/
    └── bootstrap-*.log                 ← 安装日志

~/.local/bin/                           ← 用户级 bin 目录 (非系统级)
├── hermes → ~/.hermes/hermes-agent/venv/bin/hermes  (符号链接)
├── node    → ~/.hermes/node/bin/node                  (符号链接)
├── npm     → ~/.hermes/node/bin/npm                   (符号链接)
└── npx     → ~/.hermes/node/bin/npx                   (符号链接)

~/.zshrc                                ← 唯一被修改的用户文件
└── export PATH="$HOME/.local/bin:$PATH"  (仅当尚不存在时追加)

系统全局:                                 ← 未被修改
├── /usr/local/bin/                     ← 未触碰 (Homebrew 领地)
├── /usr/bin/python3                    ← 未触碰 (系统 Python)
├── /etc/paths                          ← 未触碰
└── /etc/profile                        ← 未触碰
```

**总结：** Electron 通过 `child_process.spawn('bash', [install.sh, ...])` 逐阶段调用 install.sh，所有运行时（uv、Python、Node.js）都隔离安装在 `~/.hermes/` 下，仅供 Hermes 使用。唯一的"溢出"是在 `~/.zshrc` 中添加 `~/.local/bin` 到 PATH，让终端能访问 `hermes` 命令——这是用户级修改，非系统全局。
