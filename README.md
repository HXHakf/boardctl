# boardctl

[![ci](https://github.com/HXHakf/boardctl/actions/workflows/ci.yml/badge.svg)](https://github.com/HXHakf/boardctl/actions/workflows/ci.yml)

> 给嵌入式 AI 代理装一条**证据链**：AI 说"跑通了"，要能指到一行可追溯的记录。

单文件、零依赖（Python 标准库），适配 ESP-IDF，Windows / macOS / Linux 都能跑。
AI 端可以是 Claude Code、Codex、Cline、Copilot——任何能跑 shell 的代理都行。

---

## 它解决三个具体问题

**1. "编译成功"和"真的烧进去了"是两件事**

现在：AI 敲 `idf.py flash`，屏幕滚过一屏，最后看着像成功。
它看到的是文字，不是状态。板子插在哪个口、烧的是哪个 `.bin`、字节有没有写对，它并不知道。

boardctl：烧完检查 esptool 那句 `Hash of data verified.`——那是**写完回读、逐字节比对通过**才会打的一行。看到它才写证据。

**2. 三天前的"跑通了"还在被当成结论**

现在：改完代码，AI 翻会话记忆说"这个功能上次验证过了"。你没法知道它引用的是改代码**之前**的证据。

boardctl：把源码、`sdkconfig`、`CMakeLists.txt` 的总哈希存进 `.boardctl/manifest.json`。改一个字，之前所有证据自动变 `stale`。

**3. 没人拦着它空口收工**

现在：AI 干完说"完成"。你不会每次都去查它到底验没验。

boardctl：Claude Code 的 `Stop` hook 会在它想结束回复时跑一次 `verify`，不过关就打回去。

---

## 快速开始

前置：Python ≥ 3.10、已装好的 ESP-IDF（EIM 安装器或自己 clone 的都行）、一块板子。

```bash
# 1) 放到任意位置，不需要 pip install
git clone https://github.com/<you>/boardctl.git
cd boardctl

# 2) 看看它认得哪些 IDF、哪些串口
python boardctl.py idf --probe
python boardctl.py ports

# 3) 在 ESP-IDF 工程目录里烧一次（这一步会生成 .boardctl/，项目从此"上锁"）
cd /path/to/your-esp-project
python /path/to/boardctl/boardctl.py flash

# 4) 检查证据是否还有效
python /path/to/boardctl/boardctl.py verify
```

`flash` 成功后项目里会多两样东西：

```
evidence.jsonl           # 一行一条，只追加，source 只能是 host
.boardctl/manifest.json  # 源码总指纹
```

`.boardctl/` 出现 = 这个项目被 boardctl 接管了。

---

## 接进 VS Code / Claude Code

```bash
# Stop hook：装进 ~/.claude/settings.json（先备份 .bak，不动其它键）
python boardctl.py install-hook

# VS Code 用户级任务：任何新工程都自动有 build/flash/monitor/verify 按钮
python boardctl.py install-user-tasks

# 可选：给单个工程加一份 CLAUDE.md 规则（只新建，不覆盖）
python boardctl.py init-vscode --project /path/to/your-esp-project
```

装完：

- `Ctrl+Shift+B` 编译；命令面板搜 `boardctl: flash`
- AI 想收工时被 hook 拦，除非 `verify` 通过

> hook 只在**上过锁**（有 `.boardctl/`）的工程里拦人。普通项目、非 ESP 项目一律静默放行——
> 这是故意设计的，避免把守卫变成到处误伤的噪音。

想手动看 hook 会输出什么：

```bash
echo '{}' | python boardctl.py stop-hook --format claude
```

| `--format` | 给谁 |
|---|---|
| `claude`（默认） | Claude Code：`{"decision":"block","reason":...}` 顶层字段 |
| `vscode-local` | VS Code 自家 Local harness：`hookSpecificOutput` 嵌套格式 |

---

## 它怎么找到你的 ESP-IDF

按优先级依次尝试，**不需要你预先设 `IDF_TOOLS_PATH`**：

1. `BOARDCTL_EIM` 指向的 `eim_idf.json`
2. 各平台的 EIM 注册表（`C:\Espressif\tools`、`D:\Espressif\tools`、`~/.espressif`）
3. `BOARDCTL_IDF_ROOTS` 里扫 `v*/esp-idf`
4. 默认目录扫一遍（`D:\esp`、`C:\esp`、`~/esp`、`/opt/esp` …）

找到之后按版本准备环境：优先用 EIM 生成的激活脚本；没有就自己拼
`IDF_PATH` / `IDF_TOOLS_PATH` / `IDF_PYTHON_ENV_PATH` / `ESP_IDF_VERSION`，再补一个 `idf.py` 入口。

### 为什么要自己拼环境

`export.ps1` / `export.sh` 有三个前置条件，缺一个就报错或**静默崩**：

| 变量 | 缺了会怎样 |
|---|---|
| `IDF_TOOLS_PATH` | 去 `~/.espressif/python_env/...` 找 venv，而工具链在别处 → `venv not found` |
| `IDF_PYTHON_ENV_PATH` | 同上 |
| `ESP_IDF_VERSION` | **崩**：`idf_component_manager` 里 `Version.coerce(os.getenv('ESP_IDF_VERSION'))` 拿到 `None` 抛 TypeError。而且 `idf.py --version` 会先把版本号打出来，看着像能用，后面每步都炸 |

boardctl 把这三样都算好了。

---

## 证据长什么样

```json
{"id":"ev-0007","ts":"2026-11-09T13:20:11Z","kind":"flash",
 "claim":"LED 1Hz 闪烁烧录成功","result":"pass","covers":["c1","c3"],
 "port":"COM5","idf":"v5.5","chip":"ESP32-S3",
 "artifacts":[{"path":"build/app.bin","sha256":"9f2c…","bytes":214736}],
 "inputs_sha256":"41ab…","source":"host"}
```

规则：

- `source` 只能是 `host`——由 boardctl 在真实执行后写入，agent 无法伪造
- 只追加，不重写、不删除
- `covers` 可选，对应工程里 `claims.jsonl` 的验收条件 id

`verify` 判定三件事：**过期**（源码变了）、**无证据**、**验收条件没被覆盖**。

---

## 已知限制

- **目前只支持 ESP-IDF**。Arduino / STM32 / 51 等还没写适配器——`flash` 走的是 `idf.py`。
- **没板子就烧不了**，但 `verify` / `selftest` 不需要硬件。
- 守卫只对"上过锁"的工程生效；第一次烧录是上锁动作。
- hook 只对**读 `~/.claude/settings.json` 的代理**生效。其它代理要靠 `CLAUDE.md` / `AGENTS.md` 的约定——那是自觉，不是强制。
- `Hash of data verified` 是 esptool 的措辞；若上游改了，`flash` 会判失败而不是误判成功——**宁可漏记，不记假账**。

---

## 自测（不需要板子）

```bash
python boardctl.py selftest     # 造一个假工程：记录证据 → 改源码 → 断言变 stale
python -m unittest discover -s tests -v
```

---

## English

**boardctl** gives embedded AI agents an evidence trail for ESP-IDF projects.

Three commands, one file, stdlib only:

- `flash` — wraps `idf.py`, and only records evidence when esptool reports
  `Hash of data verified.` (read-back verified), capturing port, chip and the
  sha256 of every `build/*.bin`.
- `verify` — recomputes a fingerprint of your sources; any edit invalidates
  previous evidence. Reports stale / missing / uncovered claims.
- `stop-hook` — a Claude Code `Stop` hook that refuses to let the agent finish
  while `verify` fails.

Install for Claude Code with `python boardctl.py install-hook`; add VS Code
tasks for every project with `python boardctl.py install-user-tasks`.
Only projects that have been successfully flashed once (`.boardctl/` exists)
are guarded — everything else is left alone.

ESP-IDF discovery needs no manual `IDF_TOOLS_PATH`: EIM registries, a scan of
common roots, or your own `BOARDCTL_IDF_ROOTS` all work.

MIT licensed.
