# Hermes Desktop 安装包分析（Mac / Windows）

> 对应 query：阅读项目源码，分析应用安装包(分Mac和Windows)，分别包含哪些部分

## 一、核心架构：安装包是"瘦壳"，后端首次启动时拉取

最重要的结论来自 `apps/desktop/scripts/before-build.cjs`：

> "The Hermes Agent Python payload is no longer bundled; the Electron app fetches it at first launch via install.ps1's stage protocol."

也就是说，**安装包本身不包含 Python Agent 后端**，只装 Electron 外壳。首次启动时，由 `apps/desktop/electron/bootstrap-runner.cjs` 按 `apps/desktop/scripts/write-build-stamp.cjs` 写入的 git commit 锁定版本，下载并执行 `install.ps1`(Windows) / `install.sh`(macOS) 把 `hermes-agent` 后端装进 `HERMES_HOME`（macOS: `~/.hermes`，Windows: `%LOCALAPPDATA%\hermes`）。

因此"安装包"要分两部分看：**A. 安装器自身内容** + **B. 首启时拉取的后端**。

---

## 二、A. 安装器自身内容（Mac/Windows 共有）

由 `apps/desktop/package.json` 的 `build` 段定义，electron-builder 打包：

| 部分 | 来源 | 说明 |
|------|------|------|
| **Electron 运行时** | electron 40.10.2 | Chromium + Node.js 外壳 |
| **渲染层构建产物** `dist/` | Vite 构建 React 前端 | 聊天 UI、文件浏览器、语音、设置、onboarding |
| **主进程** `electron/main.cjs` | `bundle-electron-main.mjs` 用 esbuild 打成单文件 | 窗口管理、后端引导、自更新；无需 node_modules |
| **preload 脚本** `electron/preload.cjs` | 单独文件 | IPC 桥 |
| **原生依赖** `native-deps/` | `stage-native-deps.cjs` 按目标 arch 暂存 | 仅 `node-pty`（终端 PTY）|
| **静态资源** `assets/`、`public/` | 图标、hermes 帧动画、启动图等 | |
| **构建戳** `install-stamp.json` | extraResources | git commit/branch，用于锁定首启后端版本 |
| **package.json** | 应用元数据 | |

打包配置关键点（`apps/desktop/package.json` 的 `build` 段）：
- `asar: true`，但 `asarUnpack`: `**/*.node`、`**/prebuilds/**`、`dist/**`（原生模块和构建产物解包）
- `beforeBuild` 返回 false，跳过 node_modules 收集，保证打包确定性

---

## 三、Mac 安装包特有部分

`build.mac`（`apps/desktop/package.json` 的 mac 配置段）：

- **产物格式**：`dmg`（磁盘映像）+ `zip`
- **Bundle**：标准 `Hermes.app`
- **Info.plist 扩展**：`CFBundleDisplayName=Hermes`、`NSMicrophoneUsageDescription`、`NSAudioCaptureUsageDescription`（语音）
- **权限** `apps/desktop/electron/entitlements.mac.plist`：
  - `allow-jit` / `allow-unsigned-executable-memory`（Electron 需要）
  - `disable-library-validation`（加载未签名 node-pty 原生模块）
  - `device.audio-input`（麦克风）
- **Hardened Runtime**：开启（公证前提）
- **公证**：`afterSign: scripts/notarize.cjs`（Apple notarization）
- **图标**：`assets/icon.icns`
- **DMG 布局**：自定义背景 `#f5f5f7`，560×360 窗口，拖到 Applications
- **node-pty 预编译**：`prebuilds/darwin-<arch>/*.node` + `spawn-helper`（`stage-native-deps.cjs` 对其 `chmod 755`）

---

## 四、Windows 安装包特有部分

`build.win`（`apps/desktop/package.json` 的 win 配置段）：

- **产物格式**：`nsis`（.exe 安装器）+ `msi`
- **NSIS 配置**：非一键安装、允许自定义安装目录、per-user（`perMachine: false`）、快捷方式名 "Hermes"
- **Exe 身份戳印**（Windows 专属）：`apps/desktop/scripts/after-pack.cjs` → `apps/desktop/scripts/set-exe-identity.cjs`，用 rcedit 把 `assets/icon.ico` + 版本元信息（ProductName/FileDescription/CompanyName/Copyright）盖到 `Hermes.exe`
- **不签名**：`signAndEditExecutable: false`——避免 electron-builder 拉取 winCodeSign-2.6.0.7z（其 macOS 符号链接在非管理员 Windows 上让 7-Zip 崩溃，无解）。代价是 exe 默认无签名，改用 rcedit 直接改 PE 资源来恢复图标/名称
- **图标**：`assets/icon.ico`（同时作为 extraResource 装入）
- **PowerShell 引导**：首启通过绝对路径解析 PowerShell（`SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe`，回退 PATH、pwsh 7）执行 `install.ps1`
- **node-pty 预编译**：`prebuilds/win32-<arch>/*.node` + `*.dll` + `*.exe`（OpenConsole.exe、winpty-agent.exe）+ `conpty/*`，**排除 .pdb 调试符号**（约 25MB）以精简安装器

---

## 五、B. 首启时拉取的 Python 后端（安装的"另一半"）

`apps/desktop/electron/bootstrap-runner.cjs` 解析安装脚本优先级：本地源码树 → GitHub 按 commit 下载 → 已装 agent 内的脚本回退。然后按 manifest 分阶段执行。

**Windows（`scripts/install.ps1` 阶段）**：
1. `uv` 装 uv 包管理器
2. `python` 验证 Python 3.11
3. `git` 装 PortableGit
4. `node` 检测 Node.js（可选，缺则跳过）
5. `system-packages` 装 ripgrep + ffmpeg
6. `repository` 克隆 hermes-agent 仓库（锁 commit）
7. `venv` 建 Python 虚拟环境
8. `dependencies` 装依赖（`uv sync`）
9. `node-deps` 装 Node 依赖（浏览器工具）
10. `path` 加入 PATH
11. `config-templates` 写配置模板
12. `platform-sdks` 装消息平台 SDK
13. `bootstrap-marker` 标记完成
14. `configure`（交互，非交互模式跳过）配 API key/模型
15. `gateway`（交互，非交互模式跳过）启动消息网关

**macOS（`scripts/install.sh` 阶段）**：`prerequisites` → `repository` → `venv` → `python-deps` → `node-deps` → `path` → `config` → `setup`(交互) → `gateway`(交互) → `complete`，逻辑对应、命名略简。

后端最终落在 `~/.hermes/hermes-agent/`（macOS）或 `%LOCALAPPDATA%\hermes\hermes-agent\`（Windows），**与 CLI 安装完全同一布局**，二者可互换。

---

## 小结

| 维度 | Mac | Windows |
|------|-----|---------|
| 安装器格式 | DMG + ZIP | NSIS (.exe) + MSI |
| 应用结构 | Hermes.app 包 | Hermes.exe + 资源目录 |
| 图标 | icon.icns | icon.ico（rcedit 盖印） |
| 签名/公证 | Hardened Runtime + Apple 公证 | 不签名（规避 winCodeSign 坑） |
| 权限 | 麦克风/音频/JIT/库校验关闭 entitlements | 无（exe 不签名） |
| 原生模块 | node-pty darwin 预编译 + spawn-helper | node-pty win32 预编译 + conpty/OpenConsole |
| 首启引导脚本 | install.sh（bash） | install.ps1（PowerShell） |
| 后端落地 | ~/.hermes/hermes-agent/ | %LOCALAPPDATA%\hermes\hermes-agent\ |
| 共有 | Electron 40.10.2 外壳 + React 渲染层 + build stamp + node-pty，后端均首启拉取 ||

两平台的安装器本质相同——都是 Electron 瘦壳；差异在打包格式、签名策略、原生二进制和首启引导脚本，而真正的 Agent 能力由首次启动时按 git commit 锁定拉取的 Python 后端提供。
