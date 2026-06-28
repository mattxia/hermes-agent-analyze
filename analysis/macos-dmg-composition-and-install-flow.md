# macOS DMG 安装包组成与安装流程分析

> 对应 query：请分析MacOS的DMG安装包的组成，以及安装过程，给出流程图等关键流程信息

## 一、DMG 安装包的内部组成

DMG 由 electron-builder 按 `apps/desktop/package.json` 的 `build.mac` / `build.dmg` 配置生成。打开 DMG 后是一个经典的"拖到 Applications"窗口。

### 1. 顶层结构

```
Hermes-<version>-mac-<arch>.dmg
└── 挂载后 "Install Hermes" 窗口
    ├── Hermes.app            ← 拖到 /Applications
    └── /Applications 快捷方式（链接）
```

### 2. Hermes.app 包内容（`Hermes.app/Contents/`）

| 路径 | 内容 | 来源 |
|------|------|------|
| `Info.plist` | Bundle 元数据：`CFBundleDisplayName=Hermes`、`CFBundleExecutable=Hermes`、麦克风/音频使用说明 | electron-builder + `build.mac.extendInfo` |
| `MacOS/Hermes` | 主可执行文件（重命名自 Electron 的 `Electron` 二进制） | Electron 40.10.2 运行时 |
| `Frameworks/` | Chromium 内容框架（`Chromium Embedded Framework.framework` 等） | Electron 运行时 |
| `Resources/` | 应用资源 | electron-builder |
| `Resources/app.asar` | 打包后的应用代码（asar 归档） | 见下方 |
| `Resources/app.asar.unpacked/` | 解包的文件（原生模块、构建产物） | `asarUnpack` 配置 |
| `Resources/install-stamp.json` | 构建戳：git commit / branch / builtAt | extraResources，由 `apps/desktop/scripts/write-build-stamp.cjs` 生成 |
| `Resources/native-deps/` | 原生依赖暂存 | extraResources |
| `Resources/icon.icns` | 应用图标 | extraResources |
| `Resources/*.lproj/` | 本地化资源 | electron-builder |

### 3. app.asar 内部（应用代码层）

由 `apps/desktop/package.json` 的 `files` 字段决定：

```
app.asar
├── dist/                      ← Vite 构建的 React 渲染层（聊天UI/文件树/语音/设置）
│   ├── assets/
│   ├── index.html
│   └── ...
├── electron/
│   ├── main.cjs               ← 主进程（esbuild 打包成单文件，无 node_modules 依赖）
│   ├── preload.cjs            ← IPC 桥（单独文件，未打包）
│   └── *.cjs                  ← backend-env / bootstrap-runner / hardening 等辅助模块
├── assets/                    ← 图标等
├── public/                    ← 启动动画帧、横幅等静态资源
└── package.json               ← 应用元数据
```

关键点（`apps/desktop/scripts/bundle-electron-main.mjs`）：`main.cjs` 用 esbuild 打成单文件，`external: ['electron', 'node-pty']`，因此**整个 app 不需要 node_modules**，构建确定性强。

### 4. app.asar.unpacked（解包的原生负载）

由 `asarUnpack`（`apps/desktop/package.json` 的 asarUnpack 段）决定：

```
app.asar.unpacked/
├── **/*.node                 ← 原生 Node.js 模块
├── **/prebuilds/**           ← node-pty 预编译二进制
└── dist/**                   ← 渲染层构建产物（解包以便直接文件访问）
```

`native-deps/`（通过 extraResources 单独装入）：
```
native-deps/
└── node-pty/
    ├── package.json
    ├── lib/*.js
    └── prebuilds/darwin-<arch>/
        ├── pty.node
        └── spawn-helper      ← 已 chmod 755（macOS 专属，`apps/desktop/scripts/stage-native-deps.cjs`）
```

### 5. macOS 安全与权限层

| 层 | 配置 | 来源 |
|----|------|------|
| Hardened Runtime | 开启 | `build.mac.hardenedRuntime: true` |
| Entitlements | JIT、未签名可执行内存、禁用库校验、音频输入 | `apps/desktop/electron/entitlements.mac.plist` |
| 继承 Entitlements | 同上（子进程继承） | `apps/desktop/electron/entitlements.mac.inherit.plist` |
| Gatekeeper 评估 | 关闭 | `gatekeeperAssess: false` |
| Apple 公证 | `afterSign` 钩子 | `apps/desktop/scripts/notarize.cjs` |
| Stapler 装订 | 公证后 staple 到 .app | 同上 |

公证流程（`apps/desktop/scripts/notarize.cjs`）：`ditto` 压缩 .app → `xcrun notarytool submit --wait` → `xcrun stapler staple`。

### 6. DMG 视觉布局

来自 `apps/desktop/package.json` 的 `build.dmg` 段：
- 标题："Install Hermes"
- 背景：`#f5f5f7`（浅灰）
- 图标大小：96px
- 窗口：560×360
- 图标位置：Hermes.app 在 (160,170)，Applications 链接在 (400,170)

---

## 二、安装流程（构建时 + 用户安装时 + 首次启动时）

整个"安装"跨三个阶段：**DMG 构建 → 用户安装 .app → 首启拉取后端**。

### 流程图一：DMG 构建流程（CI/开发者侧）

```
┌─────────────────────────────────────────────────────────────┐
│  npm run dist:mac                                           │
└──────────────────────────────┬──────────────────────────────┘
                               │
        ┌──────────────────────┴──────────────────────┐
        │  npm run build                              │
        └──────────────────────┬──────────────────────┘
                               │
   ┌───────────────────────────┼───────────────────────────┐
   │                           │                           │
   ▼                           ▼                           ▼
┌─────────┐            ┌─────────────┐            ┌──────────────┐
│ write-  │            │ stage-      │            │ tsc -b +     │
│ build-  │            │ native-deps │            │ vite build   │
│ stamp   │            │ (node-pty   │            │ (渲染层 dist)│
│ .cjs    │            │  darwin)    │            │              │
└────┬────┘            └──────┬──────┘            └──────┬───────┘
     │                        │                          │
     │ build/install-         │ build/native-deps/       │
     │ stamp.json             │                          │
     ▼                        ▼                          ▼
   ┌────────────────────────────────────────────────────────┐
   │  bundle-electron-main.mjs (esbuild)                    │
   │  electron/main.cjs → 单文件，external electron/node-pty│
   └────────────────────────────┬───────────────────────────┘
                                │
                                ▼
   ┌────────────────────────────────────────────────────────┐
   │  prebuilder: patch-electron-builder-mac-binary.cjs     │
   │  修补 app-builder-lib 的 macOS 二进制缺失回退逻辑       │
   └────────────────────────────┬───────────────────────────┘
                                │
                                ▼
   ┌────────────────────────────────────────────────────────┐
   │  electron-builder (run-electron-builder.cjs)           │
   │  beforeBuild: 返回 false（跳过 node_modules 收集）      │
   │  beforePack: 清理 stale unpacked 目录                   │
   │  → 复制 Electron.app，重命名为 Hermes                  │
   │  → 写入 app.asar + unpacked + extraResources           │
   │  afterPack: (macOS 无操作，仅 Windows 盖图标)          │
   │  afterSign: notarize.cjs（公证 + staple）              │
   └────────────────────────────┬───────────────────────────┘
                                │
              ┌─────────────────┼─────────────────┐
              ▼                                   ▼
   ┌───────────────────┐               ┌───────────────────┐
   │ Hermes-...-mac-   │               │ Hermes-...-mac-   │
   │ <arch>.dmg        │               │ <arch>.zip        │
   │ (磁盘映像安装器)  │               │ (自动更新用)      │
   └───────────────────┘               └───────────────────┘
```

### 流程图二：用户安装 .app（DMG → /Applications）

```
┌───────────────────────────────────────────────────────┐
│  用户下载 Hermes-<version>-mac-<arch>.dmg             │
└──────────────────────────┬────────────────────────────┘
                           │
                           ▼
            ┌───────────────────────────┐
            │ 双击 DMG 挂载             │
            │ 打开 "Install Hermes" 窗口│
            └─────────────┬─────────────┘
                          │
                          ▼
            ┌───────────────────────────┐
            │ 拖拽 Hermes.app           │
            │ → /Applications           │
            └─────────────┬─────────────┘
                          │
                          ▼
            ┌───────────────────────────┐
            │ 推出 DMG（可选）          │
            └─────────────┬─────────────┘
                          │
                          ▼
            ┌───────────────────────────┐
            │ 首次启动 Hermes.app       │
            │ (Gatekeeper 校验公证)     │
            └─────────────┬─────────────┘
                          │
                  ┌───────┴────────┐
                  ▼                ▼
            ┌──────────┐    ┌──────────────────┐
            │ 正常启动 │    │ 未公证/损坏提示  │
            └──────────┘    │ (右键打开可绕过) │
                            └──────────────────┘
```

### 流程图三：首次启动 — 后端引导（核心流程）

首次启动时，`apps/desktop/electron/main.cjs` 的 `resolveHermesBackend()` 按优先级解析后端。这是最关键的部分——**安装包本身不含 Python 后端，首启时按 install-stamp.json 锁定的 git commit 拉取**。

```
┌─────────────────────────────────────────────────────────────────┐
│  Hermes.app 启动                                                │
│  读取 Resources/install-stamp.json (commit/branch)             │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────────┐
        │  resolveHermesBackend() 后端解析阶梯     │
        │  (按优先级，命中即返回)                  │
        └──────────────────────────┬───────────────┘
                                   │
   ┌───────────────────────────────┼───────────────────────────────┐
   │                               │                               │
   ▼ ①                            ▼ ②                            ▼ ③
┌──────────────┐          ┌──────────────┐          ┌──────────────────┐
│HERMES_DESKTOP│   否     │ ~/.hermes/   │   否     │ PATH 上的 hermes │
│_HERMES_ROOT  │──────►   │hermes-agent/ │──────►   │ (--version 验活) │
│环境变量覆盖  │          │.hermes-      │          │                  │
│(开发用)      │          │bootstrap-    │          │                  │
│              │          │complete 存在?│          │                  │
└──────┬───────┘          └──────┬───────┘          └────────┬─────────┘
       │ 是                      │ 是                       │ 否
       │                         │                          ▼
       │                         │                ┌──────────────────┐
       │                         │                │ ④ 系统 Python    │
       │                         │                │ canImportHermesCli│
       │                         │                │ (import 探针)    │
       │                         │                └────────┬─────────┘
       │                         │                         │ 否
       │                         │                         ▼
       │                         │          ┌──────────────────────────┐
       │                         │          │ ⑤ kind: "bootstrap-needed"│
       │                         │          │ 触发首次启动引导         │
       │                         │          └────────────┬─────────────┘
       │                         │                       │
       ▼                         ▼                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  ① ② ③ ④ 命中 → 直接启动后端（venv python -m hermes_cli.main） │
│  ⑤ 命中   → runBootstrap() 下载执行 install.sh                 │
└──────────────────────────┬──────────────────────────────────────┘
                           │ (⑤ 路径)
                           ▼
```

### 流程图四：install.sh 后端引导（macOS 首启拉取后端）

`apps/desktop/electron/bootstrap-runner.cjs` 解析 install.sh 来源后执行：

```
┌──────────────────────────────────────────────────────────────────┐
│  resolveInstallScript() 解析 install.sh 来源                     │
│  优先级:                                                          │
│    1. SOURCE_REPO_ROOT 本地源码树（开发）                         │
│    2. GitHub raw 按 install-stamp.commit 下载                     │
│       → ~/.hermes/bootstrap-cache/install-<commit>.sh            │
│    3. 已装 agent 内 install.sh 回退（本地构建未 push 时）         │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────────┐
        │  install.sh --manifest                   │
        │  获取阶段清单 JSON                        │
        └──────────────────┬───────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────────┐
        │  逐阶段执行 install.sh --stage <name>    │
        │  --non-interactive --json                │
        │  (流式输出 JSON 进度帧给渲染层)           │
        └──────────────────┬───────────────────────┘
                           │
                           ▼
   ┌───────────────────────────────────────────────────────────┐
   │  阶段序列（macOS, scripts/install.sh 的 emit_manifest）   │
   │                                                           │
   │  1. prerequisites  → install_uv, check_python(3.11),      │
   │                      check_git, check_node, install_      │
   │                      system_packages(ripgrep, ffmpeg)     │
   │  2. repository     → clone_repo (git clone, 锁 commit)    │
   │  3. venv           → setup_venv (uv venv)                 │
   │  4. python-deps    → install_deps (uv sync)               │
   │  5. node-deps      → install_node_deps (浏览器工具)       │
   │  6. path           → setup_path (安装 hermes 命令)        │
   │  7. config         → copy_config_templates                │
   │  8. setup          → run_setup_wizard (交互,非交互跳过)   │
   │  9. gateway        → maybe_start_gateway (交互,跳过)      │
   │ 10. complete       → 写 .install_method, print_success    │
   └──────────────────────────┬────────────────────────────────┘
                              │
                              ▼
        ┌──────────────────────────────────────────┐
        │  writeBootstrapMarker()                  │
        │  写入 ~/.hermes/hermes-agent/            │
        │        .hermes-bootstrap-complete        │
        └──────────────────┬───────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────────┐
        │  重新解析后端 → ensureRuntime()          │
        │  isBootstrapComplete() = true            │
        │  → createActiveBackend()                 │
        └──────────────────┬───────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────────┐
        │  启动 Python 后端                        │
        │  venv/bin/python -m hermes_cli.main      │
        │     dashboard ...                        │
        │  等待 stdout: "HERMES_DASHBOARD_READY    │
        │  port=<N>" (最长 90s, backend-ready.cjs) │
        └──────────────────┬───────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────────┐
        │  渲染层连接 ws://127.0.0.1:<port>        │
        │  显示聊天 UI                             │
        └──────────────────────────────────────────┘
```

### 流程图五：后续启动（已引导完成）

```
┌─────────────────────────────────────────────────────┐
│  Hermes.app 启动                                    │
└──────────────────────────┬──────────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────────┐
        │  resolveHermesBackend()                  │
        │  isBootstrapComplete() = true            │
        │  → createActiveBackend()                 │
        └──────────────────┬───────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────────┐
        │  venv/bin/python -m hermes_cli.main      │
        │     dashboard ...                        │
        │  (无引导，直接启动)                      │
        └──────────────────┬───────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────────┐
        │  HERMES_DASHBOARD_READY port=<N>         │
        │  渲染层连接，显示 UI                     │
        └──────────────────────────────────────────┘
```

---

## 三、关键路径与文件落地

| 项目 | 路径 |
|------|------|
| DMG 内 .app | `Hermes.app/Contents/MacOS/Hermes` |
| 构建戳（随 DMG 分发） | `Hermes.app/Contents/Resources/install-stamp.json` |
| 原生模块（随 DMG 分发） | `Hermes.app/Contents/Resources/native-deps/node-pty/` |
| Hermes 数据根 | `~/.hermes/`（`HERMES_HOME`） |
| Agent 代码 | `~/.hermes/hermes-agent/` |
| Python venv | `~/.hermes/hermes-agent/venv/` |
| 引导完成标记 | `~/.hermes/hermes-agent/.hermes-bootstrap-complete` |
| 引导缓存 | `~/.hermes/bootstrap-cache/install-<commit>.sh` |
| 日志 | `~/.hermes/logs/desktop.log`、`~/.hermes/logs/bootstrap-<ts>.log` |
| Hermes 命令 | PATH 中的 `hermes`（`/usr/local/bin` 或 Homebrew） |

---

## 四、关键设计要点

1. **安装包是瘦壳**：DMG 只装 Electron + React UI + node-pty，约百兆级；Python Agent 后端首启时按 git commit 锁定拉取，保证版本可复现。

2. **install-stamp.json 是版本锚点**（`apps/desktop/scripts/write-build-stamp.cjs`）：优先 CI 的 `GITHUB_SHA`，回退本地 `git rev-parse HEAD`。无此戳的构建拒绝打包。

3. **后端解析阶梯**（`apps/desktop/electron/main.cjs` 的 `resolveHermesBackend`）：环境变量覆盖 → 已完成引导 → PATH 上的 hermes → 系统 Python → 引导。每一级都有探针验活（`--version`、`import hermes_cli`），避免返回死后端。

4. **node-pty 是唯一原生依赖**（`apps/desktop/scripts/stage-native-deps.cjs`）：按目标 arch 只暂存 `prebuilds/darwin-<arch>/`，`spawn-helper` 在 macOS 上 `chmod 755`。

5. **macOS 专属补丁**（`apps/desktop/scripts/patch-electron-builder-mac-binary.cjs`）：修补 electron-builder 26.8.x 在 arm64 打包时偶尔丢失 `MacOS/Electron` 二进制的 bug，从已装的 Electron 运行时回补。

6. **公证是 afterSign 钩子**（`apps/desktop/scripts/notarize.cjs`）：支持 `APPLE_NOTARY_PROFILE`（keychain）或 `APPLE_API_KEY`+`APPLE_API_KEY_ID`+`APPLE_API_ISSUER`（.p8 文件/内联）两种凭证；`--wait` 阻塞等结果，`stapler staple` 装订到 .app。

7. **DMG 与 ZIP 双产物**：DMG 供用户下载安装，ZIP 供 electron-updater 自动更新（体积更小，直接替换 .app）。

8. **install.sh 的 `--include-desktop` 阶段默认跳过**：避免已运行的 Hermes.app 递归构建覆盖自己。
