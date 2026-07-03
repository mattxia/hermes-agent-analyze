# macOS 上 HermesAgent 如何准备 Node.js 环境

## 一、核心结论

HermesAgent 在 macOS 上采用 **“运行时动态准备”** 策略：

- **不** 在应用包内预置 Node.js 运行时。
- 首次启动/安装时通过 `scripts/install.sh` 或 `scripts/lib/node-bootstrap.sh` 按需解析、下载、集成 Node.js。
- Node.js 主要服务于 **TUI（React + Ink）、浏览器工具（agent-browser + Playwright Chromium）**；Python agent 核心逻辑本身不依赖 Node。

## 二、Node.js 环境准备的完整链路

### 1. 版本要求

`scripts/install.sh` 中固定目标大版本：

```bash
NODE_VERSION="22"
```

`node_satisfies_build()` 要求的可用版本为：

- `^20.19`
- `>=22.12`

```bash
if [ "$major" -eq 20 ] && [ "$minor" -ge 19 ]; then return 0; fi
if [ "$major" -ge 22 ] && { [ "$major" -gt 22 ] || [ "$minor" -ge 12 ]; }; then return 0; fi
```

### 2. 环境发现策略（按优先级）

公共辅助脚本 `scripts/lib/node-bootstrap.sh` 中的 `ensure_node()` 定义了发现阶梯：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | PATH 上已有的现代 Node | 尊重用户已有环境 |
| 2 | `~/.hermes/node/bin/node` | 此前 Hermes 自管安装 |
| 3 | `fnm` / `proto` / `nvm` | 用户已使用的 Node 版本管理器 |
| 4 | Homebrew (`node@22`) | macOS 平台包管理器 |
| 5 | nodejs.org 官方 tarball | 最终兜底，零权限、不改动 shell rc |

`install.sh` 的 `check_node()` 也遵循类似逻辑：先检测系统 Node，再回退到 Hermes 自管 Node，最后调用 `install_node()`。

### 3. macOS 官方 tarball 下载与部署

`install_node()` 的执行流程：

- 探测架构：`uname -m`
  - `x86_64` → `x64`（Intel Mac）
  - `arm64` / `aarch64` → `arm64`（Apple Silicon）
- OS 标识：macOS → `darwin`
- 从 `https://nodejs.org/dist/latest-v22.x/` 抓取匹配 `node-v22.*.*-darwin-{arm64|x64}.tar.xz` 的最新包
- 解压到 `$HERMES_HOME/node/`（默认 `~/.hermes/node/`）
- 创建符号链接到用户 PATH 目录：

```bash
ln -sf "$HERMES_HOME/node/bin/node" "$node_link_dir/node"
ln -sf "$HERMES_HOME/node/bin/npm"  "$node_link_dir/npm"
ln -sf "$HERMES_HOME/node/bin/npx"  "$node_link_dir/npx"
```

macOS 普通用户下 `$node_link_dir` 为 `~/.local/bin`。

### 4. npm 全局前缀隔离配置

`configure_managed_node_npm_prefix()` 会写入：

```bash
prefix=~/.local
```

到 `$HERMES_HOME/node/etc/npmrc`。

目的：让 `npm install -g` 安装的全局包 bin 落到 `~/.local/bin/`（已在 PATH 上），而不是 Node 目录内部（既不在 PATH，也会在 Node 升级时被清空）。

### 5. Node.js 依赖安装

在 Node 就绪后，`install_node_deps()` 执行：

```bash
cd "$INSTALL_DIR"
npm install --silent
```

安装根 `package.json` 中的 workspace 依赖（包括 `agent-browser`、`@streamdown/math` 等）。

浏览器工具还需要通过 `ensure_browser()` 全局安装：

```bash
npm install -g --prefix "$HERMES_HOME/node" --silent --ignore-scripts \
    "agent-browser@^0.26.0" \
    "@askjo/camofox-browser@^1.5.2"
```

## 三、Python 侧如何复用 Node 环境

### 1. 可执行文件解析

`hermes_constants.py` 提供：

- `iter_hermes_node_dirs()`：返回 Node 目录查找顺序（macOS/Linux 优先 `~/.hermes/node/bin`，再 `~/.hermes/node`）。
- `find_hermes_node_executable()`：优先返回健康的 Hermes 自管 Node/npm/npx，若损坏会调用 `heal_hermes_managed_node()` 重新下载修复。
- `find_node_executable()`：Hermes 自管 → 系统 PATH。
- `with_hermes_node_path()`：构造把 Hermes Node 目录前置的 PATH 环境。

### 2. 依赖按需触发安装

`hermes_cli/dep_ensure.py` 中：

```python
_DEP_CHECKS = {
    "node": lambda: shutil.which("node") is not None,
    "browser": lambda: (
        agent_browser_runnable(shutil.which("agent-browser"))
        or _has_system_browser()
        or _has_hermes_agent_browser()
    ),
    ...
}
```

当 Python agent 发现 Node/browser 缺失时，会通过 `ensure_dependency()` 交互式调用：

```bash
bash scripts/install.sh --ensure node
bash scripts/install.sh --ensure browser
```

### 3. Electron 主进程向后端进程注入 PATH

`apps/desktop/electron/backend-env.cjs` 的 `buildDesktopBackendPath()` 构造 Python 后端子进程的 PATH：

```javascript
return appendUniquePathEntries([
  hermesNodeBin,   // ~/.hermes/node/bin
  venvBin,         // ~/.hermes/hermes-agent/venv/bin
  currentPath,
  saneEntries      // 包含 /opt/homebrew/bin、/usr/local/bin 等
])
```

这样 Python 后端通过 `subprocess` 调用 `node`、`agent-browser`、`npx` 时，能优先命中 Hermes 自管版本。

Electron 主进程自身也通过 `hermesManagedNodePathEntries()` 和 `pathWithHermesManagedNode()` 把 `~/.hermes/node/bin` 放在 PATH 最前。

## 四、macOS 最终目录布局

```
~/.hermes/                          ← HERMES_HOME
├── node/                           ← Hermes 自管 Node.js 22 LTS
│   ├── bin/
│   │   ├── node
│   │   ├── npm
│   │   ├── npx
│   │   └── agent-browser           ← npm -g 安装的 CLI
│   ├── etc/
│   │   └── npmrc                   ← prefix=~/.local
│   └── lib/node_modules/           ← 全局 npm 包
├── hermes-agent/                   ← 代码仓库
│   ├── node_modules/               ← 项目级 npm 依赖
│   ├── package.json
│   ├── package-lock.json
│   └── venv/bin/python             ← Python 后端
└── logs/

~/.local/bin/                       ← 已在用户 PATH
├── node → ~/.hermes/node/bin/node
├── npm  → ~/.hermes/node/bin/npm
├── npx  → ~/.hermes/node/bin/npx
└── hermes → (调用 venv/bin/hermes 的 shim)
```

## 五、关键设计要点

1. **隔离性**：Hermes 自管 Node 安装在 `~/.hermes/node/`，通过符号链接暴露，不污染系统 Node，也不受系统 Node 升级影响。
2. **可修复性**：Python 侧 `heal_hermes_managed_node()` 和 bash 侧 `heal_managed_node()` 能在自管 Node 损坏时重新下载修复。
3. **用户环境优先**：先尝试系统 Node、fnm、nvm、Homebrew，最后才下载 tarball，尊重用户已有配置。
4. **零 sudo**：默认安装到用户目录，无需管理员权限。
5. **npm 全局前缀重定向**：`prefix=~/.local` 保证全局包 bin 落在 PATH 上并跨 Node 升级存活。
6. **Electron-Python 协作**：Electron 负责 UI 和进程管理，通过构造好的 PATH 让 Python 子进程能够调用 Node 工具。
