# HermesAgent 运行 Node.js 脚本的执行路径与运行时准备分析

> 对应 query：如果用户输入的一个任务（query）需要运行 Node.js 相关的脚本，HermesAgent 是如何运行这个 Node.js 脚本的，并做了哪些 Node.js 运行时准备

## 核心结论

当用户任务需要运行 Node.js 脚本时，HermesAgent 有**四条执行路径**，每条路径做了不同的运行时准备。Node.js 运行时的可用性通过**五层准备模型**（安装 → PATH 注入 → 运行时解析 → 懒安装 → 自愈）层层保障，确保在任何启动方式下 `node` / `agent-browser` 都能被正确发现和执行。

---

## 一、四条执行路径总览

| 路径 | 触发场景 | 执行方式 | Node 解析方式 |
|---|---|---|---|
| **A. 终端工具** | 用户要求运行 `node script.js` 等通用命令 | `bash -c "node script.js"` | 依赖 PATH（shell 快照 + sane PATH） |
| **B. 浏览器工具** | 用户要求浏览器自动化 | `subprocess.Popen([agent-browser, ...])` | `_find_agent_browser()` 多级解析 |
| **C. WhatsApp 桥接** | Gateway 需要连接 WhatsApp | `subprocess.Popen([node, bridge.js, ...])` | `find_node_executable("node")` |
| **D. CLI 构建/TUI** | `hermes` 命令构建 Web UI、启动 TUI | `subprocess.run([npm/node, ...])` | `find_node_executable()` + `with_hermes_node_path()` |

---

## 二、路径 A：终端工具（最通用的路径）

当 LLM 判断需要运行 Node.js 脚本时，会生成一个 `terminal` tool call，命令形如 `node app.js` 或 `npm run build`。

### 执行链路

```
用户 query → LLM 生成 terminal tool_call
  → agent/tool_executor.py: execute_tool_calls_sequential/concurrent
    → tools/terminal_tool.py: 接收 command="node app.js"
      → tools/environments/local.py: LocalEnvironment._run_bash()
        → subprocess.Popen([bash, "-c", "node app.js"], env=run_env)
```

关键代码在 `tools/environments/local.py` 的 `_run_bash()`：

```python
def _run_bash(self, cmd_string, *, login=False, timeout=120, ...):
    bash = _find_bash()
    args = [bash, "-l", "-c", cmd_string] if login else [bash, "-c", cmd_string]
    run_env = _make_run_env(self.env)          # ← 关键：构建环境
    proc = subprocess.Popen(args, env=run_env, ...)
```

### 运行时准备：环境构建

Node.js 能否被找到，取决于 `_make_run_env()` 构建的 PATH。该函数在 `tools/environments/local.py` 中：

```python
def _make_run_env(env):
    merged = dict(os.environ | env)               # 1. 合并进程环境
    # 2. 剥离 Hermes provider 凭据（安全）
    # 3. 补全 sane PATH：
    new_path = _append_missing_sane_path_entries(run_env.get("PATH", ""))
    run_env["PATH"] = _prepend_hermes_bin_dir(new_path)  # 4. 前置 hermes bin
```

其中 `_SANE_PATH` 在 macOS 上包含 Homebrew 路径：

```python
_SANE_PATH = (
    "/opt/homebrew/bin:/opt/homebrew/sbin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
```

### 运行时准备：会话快照（Session Snapshot）

`init_session()` 在首次执行时捕获登录 shell 环境（`tools/environments/base.py`）：

```python
def init_session(self):
    bootstrap = (
        f"export -p > {snapshot}\n"           # 导出所有环境变量
        f"declare -f | ... >> {snapshot}\n"   # 导出函数
        f"alias -p >> {snapshot}\n"           # 导出别名
        ...
    )
    proc = self._run_bash(bootstrap, login=True)  # bash -l -c
```

在 macOS 上，登录 shell 会先 source `~/.profile`、`~/.bash_profile`、`~/.bashrc`（`local.py` 的 `_resolve_shell_init_files()`），这样 **nvm/fnm/asdf 等版本管理器添加的 Node.js 路径**就会被捕获到快照中。

此后每条命令执行前都会 source 这个快照：

```python
if self._snapshot_ready:
    parts.append(f"source {snapshot} >/dev/null 2>&1 || true")
parts.append(f"eval '{command}'")          # ← 实际命令
```

**因此终端工具执行 `node` 时，Node 的来源取决于：**

1. Desktop 模式下 Electron 注入的 `~/.hermes/node/bin`（见下文路径 D 的 backend-env.cjs）
2. 登录 shell 快照中 nvm/fnm/Homebrew 添加的 Node 路径
3. `_SANE_PATH` 中的 `/opt/homebrew/bin`（Homebrew 安装的 node）

---

## 三、路径 B：浏览器工具（直接子进程）

当用户要求浏览器自动化时，`browser_tool` 被调用。它不通过 shell，而是**直接 `subprocess.Popen` 启动 `agent-browser` CLI**（一个 Node.js 工具）。

### 执行链路

```
用户 query → LLM 生成 browser tool_call
  → tools/browser_tool.py: _run_tmp()
    → _find_agent_browser()  ← 解析 agent-browser 可执行文件
    → subprocess.Popen([agent_browser_cmd, "--engine", "chrome", ...], env=browser_env)
```

### 运行时准备：agent-browser 的多级解析

`tools/browser_tool.py` 的 `_find_agent_browser()` 按以下顺序查找：

```python
def _find_agent_browser():
    # 1. shutil.which("agent-browser") — PATH 上的全局安装
    # 2. 扩展 PATH 查找（含 macOS Homebrew、Hermes 自管 Node）
    # 3. node_modules/.bin/agent-browser — 项目级安装
    # 4. npx agent-browser — 回退到 npx
    # 5. 都没找到 → ensure_dependency("browser") 触发懒安装
    if ensure_dependency("browser"):
        # 重新尝试上述查找
```

**每个候选都要通过 `agent_browser_runnable()` 验证**（实际运行 `--version`），防止悬空符号链接导致静默失败。

### 运行时准备：browser 子进程环境

```python
browser_env = {**os.environ, "AGENT_BROWSER_SOCKET_DIR": task_socket_dir}
browser_env["PATH"] = _merge_browser_path(browser_env.get("PATH", ""))
```

`_merge_browser_path()` 会合并 macOS Homebrew 路径（`/opt/homebrew/bin`）和 Hermes 自管 Node 路径，确保 agent-browser 的子进程（如 Chromium）能找到所需依赖。

### 懒安装触发

当 agent-browser 完全找不到时，触发安装：

```python
from hermes_cli.dep_ensure import ensure_dependency
if ensure_dependency("browser"):   # → bash install.sh --ensure browser
```

这会调用 `install.sh` 的 `ensure_mode()`，先 `check_node` 确保 Node 可用，再 `ensure_browser` 全局安装 agent-browser。

---

## 四、路径 C：WhatsApp 桥接（直接子进程）

WhatsApp 适配器需要启动一个 Node.js 进程运行桥接脚本。

### 执行链路

`plugins/platforms/whatsapp/adapter.py`：

```python
# 1. 构建环境：with_hermes_node_path() 前置 Hermes 自管 Node 目录
bridge_env = with_hermes_node_path()

# 2. 解析 node 可执行文件
node_bin = find_node_executable("node") or "node"

# 3. 启动 Node.js 子进程
self._bridge_process = subprocess.Popen(
    [node_bin, str(bridge_path), "--port", ..., "--session", ...],
    stdout=bridge_log_fh, stderr=bridge_log_fh,
    start_new_session=True,
    env=bridge_env,
)
```

### 运行时准备

这里使用了 Python 侧的两个关键函数：

**`find_node_executable("node")`**（`hermes_constants.py`）：

```python
def find_node_executable(command):
    managed = find_hermes_node_executable(command)  # Hermes 自管 → --version 探针
    if managed:
        return managed
    if hermes_managed_node_tree_present():
        return None            # 自管树存在但损坏，不回退系统 Node
    return find_node_executable_on_path(command)   # 系统 PATH
```

**`with_hermes_node_path()`**（`hermes_constants.py`）：

```python
def with_hermes_node_path(env=None):
    merged = dict(os.environ if env is None else env)
    managed = [str(p) for p in iter_hermes_node_dirs() if p.is_dir()]
    for entry in reversed(managed):
        if entry not in parts:
            parts.insert(0, entry)     # 前置 ~/.hermes/node/bin
    merged["PATH"] = os.pathsep.join(parts)
    return merged
```

如果桥接脚本的 `node_modules` 缺失，还会自动安装：

```python
_npm_bin = find_node_executable("npm") or "npm"
install_result = subprocess.run(
    [_npm_bin, "install"], cwd=bridge_dir,
    env=with_hermes_node_path(),
)
```

---

## 五、路径 D：CLI 构建 / TUI 启动（直接子进程）

`hermes_cli/main.py` 中多处使用 `find_node_executable()` + `with_hermes_node_path()` 直接启动 Node.js 子进程。

### TUI 启动

```python
def _node_bin(bin):
    path = shutil.which(bin)
    if not path and bin == "node":
        from hermes_cli.dep_ensure import ensure_dependency
        if ensure_dependency("node"):        # ← 懒安装 Node
            path = shutil.which("node")
    if not path:
        sys.exit(1)
    return path
```

### Web UI 构建 / Electron 下载

```python
from hermes_constants import find_node_executable, with_hermes_node_path
node = find_node_executable("node")
dl_env = with_hermes_node_path(env)
subprocess.run([node, str(installer)], cwd=electron_dir, env=dl_env)
```

---

## 六、Desktop 模式下的 PATH 注入（前置准备）

在 Desktop 模式下，Electron 主进程在启动 Python 后端子进程时，就已经把 Node bin 目录注入了 PATH。这是所有路径的基础。

`apps/desktop/electron/backend-env.cjs` 的 `buildDesktopBackendPath()`：

```javascript
function buildDesktopBackendPath({ hermesHome, venvRoot, currentPath, platform }) {
  const hermesNodeBin = path.join(hermesHome, 'node', 'bin')     // ~/.hermes/node/bin
  const venvBin = path.join(venvRoot, 'bin')                     // venv/bin
  // 顺序: hermesNodeBin → venvBin → 继承的 PATH → /opt/homebrew/bin → ...
  return appendUniquePathEntries([hermesNodeBin, venvBin, currentPath, saneEntries])
}
```

这意味着 **Python 后端进程的 PATH 中，`~/.hermes/node/bin` 排在最前面**，所有通过 `subprocess` 或终端工具运行的 `node` 命令都会优先使用 Hermes 自管 Node。

macOS 从 Finder/Dock 启动的 app 只继承 `/usr/bin:/bin:/usr/sbin:/sbin`，会漏掉 Homebrew 和用户 CLI 工具，所以 `POSIX_SANE_PATH_ENTRIES` 还补了 `/opt/homebrew/bin`、`/usr/local/bin` 等。

---

## 七、Node.js 运行时准备的五层模型

综合以上分析，Node.js 运行时准备可以归纳为五层：

```
┌─────────────────────────────────────────────────────────────────────┐
│ Layer 5: 自愈 (Self-healing) — 运行时按需                              │
│   node_tool_runnable() → --version 探针                               │
│   heal_hermes_managed_node() → 重新下载损坏的 Node 树                    │
│   agent_browser_runnable() → 验证 agent-browser 可运行                  │
├─────────────────────────────────────────────────────────────────────┤
│ Layer 4: 懒安装 (Lazy install) — 检测到缺失时触发                       │
│   dep_ensure.ensure_dependency("node")    → install.sh --ensure node  │
│   dep_ensure.ensure_dependency("browser")  → install.sh --ensure browser│
├─────────────────────────────────────────────────────────────────────┤
│ Layer 3: 运行时解析 (Runtime resolution) — 每次调用时                   │
│   find_node_executable()     → Hermes自管(健康检查) → 系统PATH           │
│   with_hermes_node_path()    → 前置 Hermes Node 目录到 PATH             │
│   _find_agent_browser()      → PATH → 扩展PATH → local → npx → 懒安装   │
├─────────────────────────────────────────────────────────────────────┤
│ Layer 2: PATH 注入 (PATH injection) — 进程启动时                        │
│   Desktop: backend-env.cjs → ~/.hermes/node/bin 前置到后端 PATH         │
│   Terminal: _make_run_env() → _SANE_PATH 补全 + hermes bin 前置         │
│   Terminal: init_session() → 登录shell快照捕获 nvm/fnm PATH             │
├─────────────────────────────────────────────────────────────────────┤
│ Layer 1: 安装 (Installation) — 一次性                                  │
│   install.sh: check_node() → install_node() → ~/.hermes/node/          │
│   node-bootstrap.sh: ensure_node() → 5级发现阶梯                       │
│   configure_managed_node_npm_prefix() → npm 全局前缀重定向              │
│   install_node_deps() → npm install + playwright install chromium      │
│   ensure_browser() → npm install -g agent-browser                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 各层详细说明

#### Layer 1: 安装（一次性）

**文件：** `scripts/install.sh`、`scripts/lib/node-bootstrap.sh`

- `check_node()` / `install_node()`：从 nodejs.org 下载 Node.js 22 LTS 便携包到 `~/.hermes/node/`
- `ensure_node()`：5 级发现阶梯（PATH → Hermes 自管 → fnm/proto/nvm → Homebrew → nodejs.org tarball）
- `configure_managed_node_npm_prefix()`：重定向 npm 全局前缀到 `~/.local`，让全局包 bin 落到 PATH 上
- `install_node_deps()`：`npm install`（项目级依赖）+ `npx playwright install chromium`（浏览器引擎）
- `ensure_browser()`：`npm install -g --prefix ~/.hermes/node agent-browser@^0.26.0`

#### Layer 2: PATH 注入（进程启动时）

**文件：** `apps/desktop/electron/backend-env.cjs`、`tools/environments/local.py`

- Desktop 模式：`buildDesktopBackendPath()` 将 `~/.hermes/node/bin` 前置到 Python 后端 PATH
- 终端工具：`_make_run_env()` 补全 `_SANE_PATH`（含 `/opt/homebrew/bin`）+ 前置 hermes bin 目录
- 终端工具：`init_session()` 通过登录 shell 快照捕获 nvm/fnm/asdf 的 PATH 添加

#### Layer 3: 运行时解析（每次调用时）

**文件：** `hermes_constants.py`、`tools/browser_tool.py`

- `find_node_executable()`：Hermes 自管（带 `--version` 健康检查）→ 系统 PATH
- `with_hermes_node_path()`：前置 Hermes Node 目录到子进程 PATH
- `_find_agent_browser()`：PATH → 扩展 PATH（Homebrew + Hermes Node）→ `node_modules/.bin` → npx → 懒安装

#### Layer 4: 懒安装（检测到缺失时触发）

**文件：** `hermes_cli/dep_ensure.py`

- `_DEP_CHECKS["node"]`：`shutil.which("node") is not None`
- `_DEP_CHECKS["browser"]`：`agent_browser_runnable(...) or _has_system_browser() or _has_hermes_agent_browser()`
- `ensure_dependency(dep)`：交互式提示后调用 `bash install.sh --ensure <dep>`

#### Layer 5: 自愈（运行时按需）

**文件：** `hermes_constants.py`、`scripts/lib/node-bootstrap.sh`

- `node_tool_runnable()`：运行 `node --version` 验证可执行
- `heal_hermes_managed_node()`：当自管 Node 树存在但损坏时，重新下载修复（每进程最多一次）
- `agent_browser_runnable()`：运行 `agent-browser --version` 验证可执行，防止悬空符号链接

---

## 八、关键设计要点

1. **终端工具依赖 PATH，不显式解析 Node**：路径 A 通过 shell 执行 `node`，Node 的可发现性完全由 PATH 决定。PATH 来源有三层：Electron 注入 → 登录 shell 快照（nvm/fnm）→ `_SANE_PATH`（Homebrew）。

2. **专用工具显式解析 Node**：路径 B/C/D 不信任 PATH，而是通过 `find_node_executable()` 显式解析，优先使用 Hermes 自管 Node（带健康检查），避免被坏的系统 Node 污染。

3. **健康检查 + 自愈**：所有 Node 相关的解析都通过 `--version` 探针验证，损坏时自动重新下载（`heal_hermes_managed_node()`），而非静默失败。

4. **懒安装**：Node 和 agent-browser 都支持运行时按需安装（`ensure_dependency()`），不要求用户预先配置。

5. **环境隔离**：`with_hermes_node_path()` 只前置 Hermes 自管 Node 目录，不修改系统 Node；`_make_run_env()` 剥离 Hermes provider 凭据，防止密钥泄露到子进程。

---

## 九、关键文件索引

| 文件 | 职责 |
|---|---|
| `scripts/install.sh` | 主安装脚本：`check_node()`、`install_node()`、`install_node_deps()`、`ensure_browser()`、`ensure_mode()` |
| `scripts/lib/node-bootstrap.sh` | 可 source 的 Node 发现/安装/修复库：`ensure_node()`、`heal_managed_node()` |
| `hermes_constants.py` | Python 侧 Node 解析：`find_node_executable()`、`with_hermes_node_path()`、`heal_hermes_managed_node()`、`node_tool_runnable()` |
| `hermes_cli/dep_ensure.py` | 懒安装触发器：`ensure_dependency("node")`、`ensure_dependency("browser")` |
| `apps/desktop/electron/backend-env.cjs` | Desktop 模式 PATH 注入：`buildDesktopBackendPath()` |
| `tools/environments/local.py` | 终端工具环境构建：`_make_run_env()`、`_run_bash()`、`_SANE_PATH` |
| `tools/environments/base.py` | 终端工具会话快照：`init_session()`、`_wrap_command()` |
| `tools/browser_tool.py` | 浏览器工具：`_find_agent_browser()`、`_run_tmp()` |
| `plugins/platforms/whatsapp/adapter.py` | WhatsApp 桥接：`check_whatsapp_requirements()`、`subprocess.Popen([node, ...])` |
| `hermes_cli/main.py` | CLI/TUI/构建：`_node_bin()`、Web UI 构建、Electron 下载 |
| `agent/tool_executor.py` | 工具调度：`execute_tool_calls_sequential()`、`execute_tool_calls_concurrent()` |
