# AGENTS.md

给接手这个仓库的 AI 代理看的操作手册。人类看 [README.md](README.md) 就够。

## 你在这个仓库里要做什么

帮用户把 boardctl 接到**他已有的 ESP-IDF 工程**上，让"烧录成功"这件事留下可追溯记录。
不要重写这个工具，也不要改它的证据格式——格式是它唯一的价值。

## 安装顺序（照做，不要跳步）

```bash
# 0) 环境自检：必须有 Python ≥3.10 和至少一个 ESP-IDF
python3 --version
python3 boardctl.py idf --probe      # 期望：至少一个安装 ok=true

# 1) 有板子吗
python3 boardctl.py ports            # 期望：列出 1 个；空列表 = 没插板或没驱动

# 2) 真烧一次（这步会创建 .boardctl/，从此该工程受管）
cd <用户的 ESP-IDF 工程>
python3 <repo>/boardctl.py flash --port COMx      # Windows 用 COM3；POSIX 用 /dev/ttyUSB0

# 3) 装集成
python3 boardctl.py install-hook          # Claude Code 用户级 Stop hook
python3 boardctl.py install-user-tasks    # VS Code 用户级任务
```

## 判成功的标准

- `flash` 输出里 `"ok": true`，且 `evidence` 有 id
- 工程里出现 `evidence.jsonl` 和 `.boardctl/manifest.json`
- `verify` 输出 `"ok": true`

**不要**凭"命令没报错"就宣布成功。没有 `evidence.jsonl` 就是没烧。

## 判失败时怎么排查（按顺序）

1. `python3 boardctl.py idf --probe` → 看 `problems` 字段，那是真跑一次 `--version` 抓的
2. `python3 boardctl.py ports` 为空 → 没插板 / 缺 USB 驱动；Windows 上要排除蓝牙虚拟串口
3. `flash` 报 `不是 ESP-IDF 项目` → 当前目录没有 `CMakeLists.txt`，用 `--project <dir>`
4. `flash` 说 `esptool 没有报告 Hash of data verified` → 真的没写对，别绕过这个检查
5. `verify` 报过期 → 正常，说明源码改过；重新 `flash` 一次

## 红线

- **不要**手工编辑 `evidence.jsonl` 或 `.boardctl/manifest.json`。它们是宿主写的，改了等于造假。
- **不要**为了"让检查通过"而放宽 `Hash of data verified` 的判定。
- **不要**替用户跑 `git push`、发版、改他的全局配置而不说明；`install-hook` 会改
  `~/.claude/settings.json`，跑之前告诉用户会备份 `.bak`。
- 没有实测结论时，明确说"未验证"，不要用"应该可以了"代替。

## 你可以改什么

- 加工具链适配器（Arduino / STM32 / 51）：新文件放 `boardctl/adapters/`，实现
  `build(cfg, name, root) -> BuildResult` 和 `flash(cfg, name, root, port) -> FlashResult`。
- 加测试：`tests/` 用标准库 `unittest`，不要引入第三方依赖。
- 改文档：README 的中英两节要同步。

## 一次最小验证（不需要板子）

```bash
python3 boardctl.py selftest
python3 -m unittest discover -s tests -v
```

`selftest` 会造一个假工程，记录一条证据，改一行源码，然后断言 `verify` 变 stale。
它返回 `"ok": true` 就说明证据链逻辑是活的。
