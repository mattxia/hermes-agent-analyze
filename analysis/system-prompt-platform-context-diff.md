# HermesAgent 在 macOS 与 Windows 上传给 LLM 的上下文差异分析

> **Query**: 阅读源码，分析 HermesAgent 在 MacOS 上和 Windows 上传给 LLM 的上下文有什么不同，特别关注系统相关的信息，如何引导 LLM 生成适合本平台的脚本。

## 概述

HermesAgent 的系统提示词由三层组成（`stable` / `context` / `volatile`），平台差异主要体现在 **`stable` 层**。核心逻辑分布在 `agent/system_prompt.py`、`agent/prompt_builder.py`、`agent/skill_utils.py`、`hermes_constants.py`、`tools/env_probe.py` 等文件中。

## 1. 执行环境提示（`build_environment_hints()`）

这是最核心的差异来源，位于 [agent/prompt_builder.py#L993-L1115](file:///d:/prj/hermes-agent-analyze/agent/prompt_builder.py#L993-L1115)。

### macOS 生成的内容：
```
Host: macOS (14.5)          ← 使用 platform.mac_ver() 获取版本号
User home directory: /Users/<user>
Current working directory: /Users/<user>/project
```

### Windows 生成的内容：
```
Host: Windows (10)          ← 使用 platform.release() 
User home directory: C:\Users\<user>
Current working directory: D:\prj\project
Note: on Windows, the machine hostname (e.g. from `hostname` or uname) 
is NOT the username. Use the 'User home directory' above to construct 
paths under C:\Users\<user>\, never the hostname.
```
加上一段 **Windows 专属的 Shell 提示**（[L901-L911](file:///d:/prj/hermes-agent-analyze/agent/prompt_builder.py#L901-L911)）：
```
Shell: on this Windows host your `terminal` tool runs commands through 
bash (git-bash / MSYS), NOT PowerShell or cmd.exe. Use POSIX shell syntax 
(`ls`, `$HOME`, `&&`, `|`, single-quoted strings) inside terminal calls. 
MSYS-style paths like `/c/Users/<user>/...` work alongside native 
`C:\Users\<user>\...` paths. PowerShell builtins (`Get-ChildItem`, 
`$env:FOO`, `Select-String`) will NOT work — use their POSIX equivalents 
(`ls`, `$FOO`, `grep`).
```

**关键引导逻辑**：Windows 上特别告诉 LLM 终端用的是 **bash 而非 PowerShell**，这直接决定了 LLM 生成的脚本语法风格。macOS 则无需此提示（默认 POSIX shell）。

## 2. Computer Use 指引（`computer_use_guidance()`）

位于 [agent/prompt_builder.py#L467-L574](file:///d:/prj/hermes-agent-analyze/agent/prompt_builder.py#L467-L574)，针对桌面控制工具做了平台感知渲染：

| 维度 | macOS | Windows |
|------|-------|---------|
| OS 名称 | `macOS` | `Windows` |
| 共享描述 | "share the same **Mac**" + "**Space**" | "share the same **desktop**" + "**active window**" |
| 保存快捷键 | `cmd+s` | `ctrl+s` |
| 示例应用 | `Safari` | `Chrome` |
| 离屏元素 | "different **Space** or behind another window… no need to switch Spaces" | "behind another window… Some apps may still force **foreground behavior** internally; if an action does not land, re-capture and adapt" |

macOS 提到的是多 Space（虚拟桌面）概念，Windows 则警告某些应用（Chromium/GTK）可能强制前台行为，需要重新截图适配。

## 3. Hermes Home 路径（平台原生路径）

位于 [hermes_constants.py#L46-L51](file:///d:/prj/hermes-agent-analyze/hermes_constants.py#L46-L51)：

- **macOS/Linux**: `~/.hermes/`
- **Windows**: `%LOCALAPPDATA%\hermes\`

这会影响系统提示词中 profile 提示的路径，例如：
- macOS: `~/.hermes/skills/`, `~/.hermes/profiles/<name>/`
- Windows: 对应的 LOCALAPPDATA 路径

## 4. 技能（Skills）的平台过滤

位于 [agent/skill_utils.py#L163-L211](file:///d:/prj/hermes-agent-analyze/agent/skill_utils.py#L163-L211)，通过 `skill_matches_platform()` 函数过滤：

```python
PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}
```

技能通过 frontmatter 中的 `platforms` 字段声明适用平台（如 `skills/apple/` 标记为 macOS 专属）。构建技能索引时（[prompt_builder.py#L1363](file:///d:/prj/hermes-agent-analyze/agent/prompt_builder.py#L1363)）：
- **macOS** 上：`[macos]` 标记的技能（如 Apple/macos 桌面交互技能）会出现在索引中
- **Windows** 上：`[macos]` 技能被隐藏，`[windows]` 技能可见

这意味着 LLM 在 macOS 上"看得到"osascript/Finder 等本地技能描述，从而倾向于生成 Mac 原生脚本；Windows 上则看不到这些。

## 5. WSL 特殊处理

如果检测到 WSL（[hermes_constants.py#L822-L839](file:///d:/prj/hermes-agent-analyze/hermes_constants.py#L822-L839)，通过 `/proc/version` 中的 `microsoft` 标记），会追加额外的路径翻译提示（[prompt_builder.py#L856-L866](file:///d:/prj/hermes-agent-analyze/agent/prompt_builder.py#L856-L866)）：
```
You are running inside WSL... The Windows host filesystem is mounted 
under /mnt/ — /mnt/c/ is the C: drive... translate to the /mnt/c/ equivalent.
```

## 6. Python 工具链探测（跨平台但检测平台特定问题）

[tools/env_probe.py](file:///d:/prj/hermes-agent-analyze/tools/env_probe.py) 探测本地 Python/pip/uv 状态。在 Windows 上常见的 `python` vs `python3` 差异、在 Linux 上的 PEP-668 问题都会被检测并注入提示。环境干净时不输出任何内容（零 token 成本）。

## 总结：引导 LLM 生成平台适配脚本的机制

HermesAgent 通过**四个维度**引导 LLM 生成适合本平台的脚本：

1. **声明 OS 与 Shell 类型** — `Host: Windows (10)` + "terminal runs bash, NOT PowerShell" 直接决定 LLM 用 POSIX 语法而非 PowerShell cmdlet
2. **路径风格提示** — 给出 `C:\Users\<user>` 实际路径 + hostname 警告，避免 LLM 构造错误路径
3. **技能可见性过滤** — macOS 技能（osascript、Finder 操作）只在 macOS 上出现于索引，LLM 自然会调用可见的平台原生工具
4. **Computer Use 指引定制** — 快捷键（cmd+s vs ctrl+s）、示例应用（Safari vs Chrome）、窗口行为说明都按平台渲染

这些差异全部在**会话开始时构建一次**并缓存（保持 prompt cache 命中），通过 `sys.platform` 检测运行平台，确保 LLM 拿到的上下文与实际执行环境一致。

## 差异产生的代码路径

所有差异在 `build_system_prompt_parts()` ([agent/system_prompt.py#L119](file:///d:/prj/hermes-agent-analyze/agent/system_prompt.py#L119)) 中组装，核心分支逻辑：

```
build_system_prompt_parts()
 ├─ computer_use_guidance()          ← 按 sys.platform 渲染 (Computer Use 差异)
 │   └─ is_macos ? "macOS"/"Safari"/"cmd+s"/"Space"
 │       is_windows ? "Windows"/"Chrome"/"ctrl+s"/"active window"
 ├─ build_environment_hints()        ← 按 sys.platform 渲染 (环境提示差异)
 │   ├─ sys.platform=="darwin"  → "Host: macOS (mac_ver)"
 │   ├─ sys.platform=="win32"   → "Host: Windows (release)" + hostname警告 + _WINDOWS_BASH_SHELL_HINT
 │   └─ sys.platform=="win32"   → 追加 Shell 提示
 ├─ build_skills_system_prompt()     ← 按 sys.platform 过滤 (技能索引差异)
 │   └─ skill_matches_platform() → PLATFORM_MAP: {"macos":"darwin","windows":"win32"}
 └─ active profile hint              ← ~/.hermes/ vs %LOCALAPPDATA%\hermes\ (路径差异)
```
