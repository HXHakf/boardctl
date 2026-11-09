#!/usr/bin/env python3
"""boardctl —— ESP-IDF 的最小「证据链」工具。

三件事：
  flash      烧录，只在 esptool 回读校验通过后才记一行证据
  verify     源码一改，旧证据自动作废
  stop-hook  给 VS Code 的 Stop hook 用：没证据就不许 AI 收工

只用标准库，依赖你机器上已有的 ESP-IDF（通过 EIM 的 eim_idf.json 发现）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
IS_WIN = os.name == "nt"
PY = sys.executable or ("python" if IS_WIN else "python3")


def _split_paths(raw: str | None) -> list[Path]:
    if not raw:
        return []
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def eim_candidates() -> list[Path]:
    """EIM 注册表位置：先看环境变量，再看各平台常见位置。"""
    out: list[Path] = []
    env = os.environ.get("BOARDCTL_EIM")
    if env:
        out.append(Path(env).expanduser())
    if IS_WIN:
        for drive in ("C:", "D:", "E:"):
            out.append(Path(f"{drive}\\Espressif\\tools\\eim_idf.json"))
    out.append(Path.home() / ".espressif" / "eim_idf.json")
    return out


def scan_roots() -> list[Path]:
    """没有 EIM 注册表时，直接在这些目录里找 <root>/v*/esp-idf。"""
    roots = _split_paths(os.environ.get("BOARDCTL_IDF_ROOTS"))
    if IS_WIN:
        roots += [Path("D:/esp"), Path("C:/esp"), Path("E:/esp"), Path.home() / "esp"]
    else:
        roots += [Path.home() / "esp", Path.home() / ".espressif",
                  Path("/opt/esp"), Path("/opt/espressif")]
    return roots


# USB 转串口 / 原生 USB 的 VID；只认这些，避免选中蓝牙虚拟串口
SERIAL_VIDS = {"10C4", "1A86", "0403", "303A", "2341", "067B"}
SOURCE_EXT = {".c", ".h", ".cpp", ".hpp", ".cc", ".s", ".ld", ".csv",
              ".txt", ".json", ".cmake", ".yml", ".yaml", ".toml"}
SKIP_DIRS = {"build", ".git", ".boardctl", "managed_components", "out", "__pycache__"}


# ---------------------------------------------------------------- 基础工具

def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def die(msg: str, code: int = 1) -> "NoReturn":
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    raise SystemExit(code)


# ---------------------------------------------------------------- 项目 / 配置

def project_root(start: Path, explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).resolve()
        if not p.is_dir():
            die(f"项目目录不存在: {p}")
        return p
    cur = start.resolve()
    for cand in [cur, *cur.parents]:
        if (cand / "CMakeLists.txt").exists():
            return cand
    return cur


def meta_dir(root: Path) -> Path:
    d = root / ".boardctl"
    d.mkdir(parents=True, exist_ok=True)
    return d


def input_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix.lower() in SOURCE_EXT:
            out.append(p)
    return sorted(out, key=lambda p: p.relative_to(root).as_posix())


def compute_inputs(root: Path) -> tuple[str, list[dict]]:
    entries, h = [], hashlib.sha256()
    for p in input_files(root):
        digest = sha256_file(p)
        rel = p.relative_to(root).as_posix()
        entries.append({"path": rel, "sha256": digest})
        h.update(rel.encode())
        h.update(b"\0")
        h.update(digest.encode())
        h.update(b"\n")
    return h.hexdigest(), entries


# ---------------------------------------------------------------- IDF 发现

def all_eims() -> list[dict]:
    out = []
    for p in eim_candidates():
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            d["_registry"] = str(p)
            out.append(d)
    return out


def _venv_python(base: Path, name: str) -> Path | None:
    """在某个 toolsPath 下找该版本的 venv python（Windows / POSIX 两种布局）。"""
    for layout in (Path("python") / name / "venv", Path("python_env") / f"idf{name.lstrip('v')}_py3.14_env"):
        for sub in ("Scripts/python.exe", "bin/python3"):
            cand = base / layout / sub
            if cand.exists():
                return cand
    return None


def _infer_venv(name: str) -> tuple[str | None, str | None]:
    """从名字（如 v6.1）推断 EIM 的 toolsPath 与 venv。"""
    bases = [Path.home() / ".espressif", Path.home() / ".espressif" / "tools"]
    if IS_WIN:
        bases = [Path(r"C:\Espressif\tools"), Path(r"D:\Espressif\tools")] + bases
    for base in bases:
        py = _venv_python(base, name)
        if py:
            return str(base), str(py.parent.parent)
    return None, None


def idf_installs() -> list[dict]:
    """注册表里的 + 磁盘上有但没登记的，合并去重。"""
    seen: dict[str, dict] = {}
    for eim in all_eims():
        for it in eim.get("idfInstalled", []):
            path = it.get("path")
            if not path or not Path(path).is_dir():
                continue
            seen[str(Path(path)).lower()] = {
                "name": it.get("name"), "path": path,
                "idfToolsPath": it.get("idfToolsPath"),
                "python": it.get("python"),
                "activationScript": it.get("activationScript"),
                "source": eim.get("_registry"),
            }
    for base in scan_roots():
        if not base.is_dir():
            continue
        for d in sorted(base.glob("v*")):
            idf = d / "esp-idf"
            if not (idf / "tools" / "idf.py").exists():
                continue
            key = str(idf.resolve()).lower()
            if key in seen:
                continue
            tools, venv = _infer_venv(d.name)
            py = _venv_python(Path(tools), d.name) if tools else None
            seen[key] = {
                "name": d.name, "path": str(idf),
                "idfToolsPath": tools,
                "python": str(py) if py else None,
                "activationScript": None, "source": "scan",
            }
    return list(seen.values())



def pick_idf(name: str | None) -> dict:
    installs = idf_installs()
    if not installs:
        die("没找到任何 ESP-IDF 安装（查过 eim_idf.json 和 D:/esp 等目录）", 2)
    if name:
        for it in installs:
            if it.get("name") == name:
                return it
        die(f"没有名为 {name} 的 IDF，可用：{', '.join(i.get('name','?') for i in installs)}", 2)
    for eim in all_eims():
        sel = eim.get("idfSelectedId")
        if not sel:
            continue
        sel_path = next((i.get("path") for i in eim.get("idfInstalled", [])
                         if i.get("id") == sel), None)
        if sel_path:
            for it in installs:
                if Path(it["path"]).resolve() == Path(sel_path).resolve():
                    return it
    return installs[-1]


def idf_inner(idf: dict, tail: str = "") -> str:
    """一段 shell 前置：把 IDF 环境弄好。优先用 EIM 激活脚本。"""
    act = idf.get("activationScript")
    if act and Path(act).exists():
        if IS_WIN:
            return f". '{act}' *> $null; " + tail
        return f". '{act}' >/dev/null 2>&1; " + tail

    mm = re.match(r"v?(\d+)\.(\d+)", idf.get("name") or "")
    version = f"{mm.group(1)}.{mm.group(2)}" if mm else ""
    py = idf.get("python")

    if not IS_WIN:
        # POSIX：直接设环境变量即可，idf.py 在 <idf>/tools 下
        parts = [f"export IDF_PATH='{idf['path']}'"]
        if idf.get("idfToolsPath"):
            parts.append(f"export IDF_TOOLS_PATH='{idf['idfToolsPath']}'")
        if version:
            parts.append(f"export ESP_IDF_VERSION='{version}'")
        if py and Path(py).exists():
            parts.append(f"export IDF_PYTHON_ENV_PATH='{Path(py).parent.parent}'")
            parts.append(f"export PATH='{Path(py).parent}:$PATH'")
        return "; ".join(parts) + "; " + tail

    setup = [f"$env:IDF_PATH='{idf['path']}'"]
    if idf.get("idfToolsPath"):
        setup.append(f"$env:IDF_TOOLS_PATH='{idf['idfToolsPath']}'")
    # ESP_IDF_VERSION 必须设成 major.minor，否则 idf_component_manager 会 coerce(None) 崩
    if version:
        setup.append(f"$env:ESP_IDF_VERSION='{version}'")
    if py and Path(py).exists():
        setup.append(f"$env:IDF_PYTHON_ENV_PATH='{Path(py).parent.parent}'")
        setup.append(f"$env:Path='{Path(py).parent};' + $env:Path")
        # 没有 EIM 激活脚本时 idf.py 不在 PATH 上，用函数补平
        idf_py = Path(idf["path"]) / "tools" / "idf.py"
        setup.append("function global:idf.py { & '" + str(py) + "' '" + str(idf_py) + "' @args }")
    return "; ".join(setup) + "; " + tail


def idf_run_str(idf: dict, argv: list[str]) -> str:
    """idf.py 的调用串：有 PATH 就用 idf.py，没有就用 venv 的 python 直接跑。"""
    tail = " ".join(argv)
    if idf.get("activationScript") and Path(idf["activationScript"]).exists():
        return "idf.py " + tail
    py = idf.get("python")
    if py and Path(py).exists():
        if IS_WIN:
            return f"& '{py}' '{Path(idf['path']) / 'tools' / 'idf.py'}' " + tail
        return f"'{py}' '{Path(idf['path']) / 'tools' / 'idf.py'}' " + tail
    return "idf.py " + tail


def idf_cmd(idf: dict, argv: list[str]) -> list[str]:
    inner = idf_inner(idf, idf_run_str(idf, argv))
    if IS_WIN:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", inner]
    return ["bash", "-lc", inner]



def probe_idf(idf: dict) -> dict:
    """真跑一次 --version，判断这个安装到底能不能用。"""
    try:
        p = subprocess.run(idf_cmd(idf, ["--version"]), capture_output=True, timeout=180)
    except Exception as e:  # noqa: BLE001
        return {"name": idf.get("name"), "ok": False, "version": None,
                "problems": [f"调用失败: {e}"]}
    out = decode(p.stdout) + "\n" + decode(p.stderr)
    m = re.search(r"ESP-IDF (v[\w.\-]+)", out)
    problems = [l.strip() for l in out.splitlines() if l.strip().startswith("ERROR")]
    if not m:
        problems.append(f"没有输出版本号（退出码 {p.returncode}）")
    return {"name": idf.get("name"), "ok": bool(m) and not problems,
            "version": m.group(1) if m else None,
            "problems": problems[:3]}



def detect_ports() -> list[dict]:
    """列出可能是开发板的串口。只认已知 USB 芯片的 VID，避免选中蓝牙虚拟串口。"""
    if not IS_WIN:
        return _detect_ports_posix()

    ps = ("Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPDeviceID } | "
          "ForEach-Object { $_.Name + '|' + $_.PNPDeviceID }")
    try:
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, timeout=40).stdout
    except Exception:
        return []
    ports = []
    for line in decode(raw).splitlines():
        m = re.search(r"\((COM\d+)\)", line)
        if not m:
            continue
        vid = re.search(r"VID_([0-9A-Fa-f]{4})", line)
        if vid and vid.group(1).upper() in SERIAL_VIDS:
            ports.append({"port": m.group(1), "device": line.split("|")[0].strip()})
    return ports


def _udev_has_board_vid(dev: Path) -> bool:
    """看 sysfs 里的 idVendor（需要 udev 权限，读不到就当作非开发板）。"""
    try:
        node = (dev.resolve() / ".." / ".." / "idVendor").resolve()
        return node.read_text(encoding="utf-8").strip().upper() in SERIAL_VIDS
    except Exception:
        return False


def _detect_ports_posix() -> list[dict]:
    ports = []
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/cu.usbserial*", "/dev/cu.usbmodem*"):
        for dev in sorted(Path("/").glob(pattern.lstrip("/"))):
            ports.append({"port": str(dev),
                          "device": dev.name,
                          "verified_vid": _udev_has_board_vid(dev)})
    return ports


def resolve_port(explicit: str | None, allow_empty: bool = False) -> str | None:
    if explicit:
        return explicit
    envp = os.environ.get("BOARDCTL_PORT")
    if envp:
        return envp
    found = detect_ports()
    if len(found) == 1:
        return found[0]["port"]
    if not found:
        if allow_empty:
            return None
        die("没找到开发板（没有 CP210x/CH340/FTDI/USB-JTAG 串口）。"
            "插上板子，或用 --port COMx 指定", 2)
    die("检测到多块可能的板子：" + ", ".join(f"{p['port']}({p['device']})" for p in found)
        + "，请用 --port 指定", 2)


# ---------------------------------------------------------------- 证据

def evidence_path(root: Path) -> Path:
    return root / "evidence.jsonl"


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def append_jsonl(p: Path, rec: dict) -> None:
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def next_evidence_id(root: Path) -> str:
    n = len(read_jsonl(evidence_path(root))) + 1
    return f"ev-{n:04d}"


def write_manifest(root: Path, inputs_sha: str, entries: list[dict]) -> None:
    (meta_dir(root) / "manifest.json").write_text(json.dumps({
        "schema": SCHEMA,
        "inputs_sha256": inputs_sha,
        "computed_at": now(),
        "file_count": len(entries),
        "files": entries,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def read_manifest(root: Path) -> dict | None:
    p = meta_dir(root) / "manifest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------- verify

def do_verify(root: Path) -> dict:
    manifest = read_manifest(root)
    evs = read_jsonl(evidence_path(root))
    passing = [e for e in evs if e.get("result") == "pass" and e.get("source") == "host"]

    current_sha, _ = compute_inputs(root)
    problems: list[str] = []

    if manifest is None:
        problems.append("没有 .boardctl/manifest.json（还没成功烧录过）")
    elif manifest.get("inputs_sha256") != current_sha:
        problems.append("源码已改动，之前的证据全部过期（inputs_sha256 不一致）")

    if not passing:
        problems.append("没有任何 host 实测通过的证据（evidence.jsonl 为空）")

    # claims 覆盖（可选）
    claims = read_jsonl(root / "claims.jsonl")
    missing = []
    if claims:
        covered = {c for e in passing for c in e.get("covers", [])}
        missing = [c["id"] for c in claims if c.get("id") not in covered]
        if missing:
            problems.append("这些验收条件没有证据覆盖：" + ", ".join(missing))

    return {
        "ok": not problems,
        "problems": problems,
        "inputs_sha256": current_sha,
        "manifest_inputs_sha256": (manifest or {}).get("inputs_sha256"),
        "evidence_pass_count": len(passing),
        "last_evidence": passing[-1]["id"] if passing else None,
        "missing_covers": missing,
    }


# ---------------------------------------------------------------- flash

def find_bins(root: Path) -> list[Path]:
    build = root / "build"
    if not build.is_dir():
        return []
    return sorted(p for p in build.glob("*.bin") if p.is_file())


def do_flash(args) -> int:
    root = project_root(Path.cwd(), args.project)
    idf = pick_idf(args.idf)
    port = resolve_port(args.port)
    if not (root / "CMakeLists.txt").exists():
        die(f"{root} 看起来不是 ESP-IDF 项目（没有 CMakeLists.txt）")

    argv = ["-p", port, "flash"]
    cmd = idf_cmd(idf, argv)
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "cwd": str(root),
                          "idf": idf.get("name"), "port": port,
                          "command": cmd[-1]}, ensure_ascii=False, indent=2))
        return 0

    inputs_sha, entries = compute_inputs(root)
    print(f"[boardctl] 烧录中：idf.py -p {port} flash（IDF {idf.get('name')}）", file=sys.stderr)
    proc = subprocess.run(cmd, cwd=root, capture_output=True)
    out = decode(proc.stdout) + "\n" + decode(proc.stderr)

    verified = "hash of data verified" in out.lower()
    chip = (re.search(r"Chip is ([^\r\n(]+)", out) or [None, ""])[1]
    chip = chip.strip() if isinstance(chip, str) else ""

    bins = find_bins(root)
    artifacts = [{"path": b.relative_to(root).as_posix(),
                  "sha256": sha256_file(b), "bytes": b.stat().st_size} for b in bins]

    ok = proc.returncode == 0 and verified
    reason = ("ok" if ok else
              "esptool 没有报告 Hash of data verified（写进去的字节没被回读确认）"
              if proc.returncode == 0 else f"idf.py 退出码 {proc.returncode}")

    if not ok:
        tail = [l for l in out.splitlines() if l.strip()][-12:]
        print(json.dumps({"ok": False, "error": reason, "chip": chip, "port": port,
                          "output_tail": tail}, ensure_ascii=False, indent=2))
        return 1

    write_manifest(root, inputs_sha, entries)
    rec = {
        "id": next_evidence_id(root),
        "ts": now(),
        "kind": "flash",
        "claim": args.claim or "固件烧录并通过回读校验",
        "result": "pass",
        "covers": [c.strip() for c in (args.covers or "").split(",") if c.strip()],
        "port": port,
        "idf": idf.get("name"),
        "chip": chip,
        "artifacts": artifacts,
        "inputs_sha256": inputs_sha,
        "source": "host",
    }
    append_jsonl(evidence_path(root), rec)
    print(json.dumps({"ok": True, "evidence": rec["id"],
                      "chip": chip, "port": port,
                      "artifacts": [a["path"] for a in artifacts],
                      "note": "已写入 evidence.jsonl 与 .boardctl/manifest.json"},
                     ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- hooks

def is_armed(root: Path) -> bool:
    """项目是否「上锁」：第一次成功烧录后 .boardctl/ 出现，从此开始守。

    非 ESP 项目（没有 .boardctl/）直接放行，免得这套守卫误伤别的工程。
    """
    return (root / ".boardctl").is_dir()


def do_stop_hook(args) -> int:
    """VS Code Stop hook：不通过就 block。"""
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except Exception:
        payload = {}
    if payload.get("stop_hook_active"):
        return 0  # 已经被拦过一次，避免无限循环

    root = project_root(Path.cwd(), args.project)
    if not is_armed(root):
        return 0  # 非受管项目，静默放行
    rep = do_verify(root)
    if rep["ok"]:
        return 0
    reason = "；".join(rep["problems"]) + "。请重新烧录并通过回读校验，或如实说明未验证。"
    if getattr(args, "format", "claude") == "vscode-local":
        out = {"hookSpecificOutput": {"hookEventName": "Stop",
                                      "decision": "block", "reason": reason}}
    else:
        # Claude Code：decision / reason 在顶层
        out = {"decision": "block", "reason": reason}
    print(json.dumps(out, ensure_ascii=False))
    return 0


def do_shell(args) -> int:
    """开一个已经激活好 IDF 的 shell（Windows: PowerShell；其它: bash）。"""
    idf = pick_idf(args.idf)
    if args.exec:
        inner = idf_inner(idf, args.exec)
        cmd = (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", inner]
               if IS_WIN else ["bash", "-lc", inner])
        p = subprocess.run(cmd, capture_output=True)
        sys.stdout.write(decode(p.stdout))
        sys.stderr.write(decode(p.stderr))
        return p.returncode
    name = idf.get("name")
    print(f"[boardctl] 已激活 {name}，在这个窗口里直接敲 idf.py；exit 退出。",
          file=sys.stderr)
    if IS_WIN:
        inner = idf_inner(idf, f"Write-Host 'ESP-IDF {name} 就绪。当前目录: ' (Get-Location)")
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-NoExit", "-Command", inner]).returncode
    inner = idf_inner(idf, f"echo 'ESP-IDF {name} 就绪。当前目录: $PWD'; exec bash -i")
    return subprocess.run(["bash", "-lc", inner]).returncode


def make_tasks(script: str) -> dict:
    """VS Code 任务：全部走 process，避免 shell 引号问题。"""
    def t(label, extra, is_build=False):
        task = {"label": label, "type": "process", "command": PY,
                "args": [script, *extra], "problemMatcher": [],
                "presentation": {"reveal": "always", "panel": "dedicated"}}
        if is_build:
            task["group"] = {"kind": "build", "isDefault": True}
        return task

    return {"version": "2.0.0", "tasks": [
        t("boardctl: build", ["shell", "--exec", "idf.py build"], True),
        t("boardctl: flash", ["flash"]),
        t("boardctl: monitor", ["shell", "--exec", "idf.py monitor"]),
        t("boardctl: verify", ["verify"]),
    ]}


CLAUDE_RULES = """<!-- boardctl -->
## 这块板子有证据链（boardctl）

- 收工前必须跑 `"{py}" "{script}" verify`；`ok: true` 才能说"完成"。
- 说"编译通过 / 烧录成功 / 跑通了"时，必须能指向 `evidence.jsonl` 里某条 `source: "host"` 的 id；指不出来就明说"未验证"。
- **不要**直接编辑 `evidence.jsonl` 或 `.boardctl/manifest.json`——那是宿主写的，改了等于造假。
- 烧录用 `"{py}" "{script}" flash`，别自己拼 `idf.py flash`：只有前者会做 esptool 回读校验并记账。
- 改了源码，之前所有证据自动过期；想再次声称"通过"，必须重新烧一次。
- VS Code 里也有同名任务（`Ctrl+Shift+B` 编译；`boardctl: flash` 烧录）。
"""


def do_init_vscode(args) -> int:
    root = project_root(Path.cwd(), args.project)
    if not (root / "CMakeLists.txt").exists():
        die(f"{root} 不是 ESP-IDF 工程（没有 CMakeLists.txt）", 2)
    script = str(Path(__file__).resolve())
    vscode = root / ".vscode"
    vscode.mkdir(exist_ok=True)

    created, skipped = [], []
    tasks_path = vscode / "tasks.json"
    if tasks_path.exists():
        skipped.append(str(tasks_path))
    else:
        tasks_path.write_text(json.dumps(make_tasks(script), ensure_ascii=False, indent=2),
                              encoding="utf-8")
        created.append(str(tasks_path))

    rules = CLAUDE_RULES.format(script=script, py=PY)
    md = root / "CLAUDE.md"
    if md.exists():
        text = md.read_text(encoding="utf-8")
        if "<!-- boardctl -->" in text:
            skipped.append(str(md))
        else:
            md.write_text(text.rstrip() + "\n\n" + rules, encoding="utf-8")
            created.append(str(md))
    else:
        md.write_text(rules, encoding="utf-8")
        created.append(str(md))

    print(json.dumps({"ok": True, "project": str(root),
                      "created": created, "skipped": skipped},
                     ensure_ascii=False, indent=2))
    return 0


def do_install_hook(args) -> int:
    """把 Stop hook 合并进 Claude Code 的 settings.json（先备份，不动其它键）。"""
    sp = Path(args.settings) if args.settings else (Path.home() / ".claude" / "settings.json")
    script = str(Path(__file__).resolve())
    cmd = f'"{PY}" "{script}" stop-hook --format claude'

    data: dict = {}
    if sp.exists():
        data = json.loads(sp.read_text(encoding="utf-8"))
        bak = sp.with_name(sp.name + ".bak")
        bak.write_text(sp.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        sp.parent.mkdir(parents=True, exist_ok=True)

    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault("Stop", [])
    # 去掉旧的 boardctl 条目（升级脚本路径/解释器时能原地替换），再追加当前这条
    before = len(groups)
    kept = []
    for g in groups:
        hs = g.get("hooks", []) if isinstance(g, dict) else []
        if any("boardctl" in str(h.get("command", "")) for h in hs if isinstance(h, dict)):
            continue
        kept.append(g)
    kept.append({"hooks": [{"type": "command", "command": cmd, "timeout": 30}]})
    hooks["Stop"] = kept
    changed = True

    sp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "settings": str(sp), "changed": changed,
                      "replaced": before - len(kept) + 1, "command": cmd},
                     ensure_ascii=False, indent=2))
    return 0


def vscode_user_dir() -> Path:
    """VS Code 用户配置目录，按平台。"""
    if IS_WIN:
        return Path(os.environ.get("APPDATA", Path.home())) / "Code" / "User"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Code" / "User"


def do_install_user_tasks(args) -> int:
    """写 VS Code 用户级 tasks.json：任何新工程都有按钮。"""
    target = Path(args.path) if args.path else (vscode_user_dir() / "tasks.json")
    script = str(Path(__file__).resolve())
    data = make_tasks(script)
    for t in data["tasks"]:
        t["options"] = {"cwd": "${workspaceFolder}"}

    if target.exists():
        target.with_name(target.name + ".bak").write_text(
            target.read_text(encoding="utf-8"), encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(target),
                      "tasks": [t["label"] for t in data["tasks"]]},
                     ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- selftest

def do_selftest(args) -> int:
    """不开板子也能证明 verify 是有效的。"""
    tmp = Path(tempfile.mkdtemp(prefix="boardctl-selftest-"))
    (tmp / "CMakeLists.txt").write_text("# fake project\n", encoding="utf-8")
    (tmp / "main").mkdir()
    src = tmp / "main" / "main.c"
    src.write_text('void app_main(void){}\n', encoding="utf-8")

    sha, entries = compute_inputs(tmp)
    write_manifest(tmp, sha, entries)
    append_jsonl(evidence_path(tmp), {
        "id": "ev-0001", "ts": now(), "kind": "flash", "claim": "selftest",
        "result": "pass", "covers": ["c1"], "source": "host",
        "inputs_sha256": sha, "artifacts": [],
    })
    (tmp / "claims.jsonl").write_text(
        json.dumps({"id": "c1", "text": "点灯"}) + "\n", encoding="utf-8")

    r1 = do_verify(tmp)
    src.write_text('void app_main(void){ /* changed */ }\n', encoding="utf-8")
    r2 = do_verify(tmp)

    ok = r1["ok"] is True and r2["ok"] is False and any("过期" in p for p in r2["problems"])
    print(json.dumps({
        "ok": ok,
        "before_change": r1,
        "after_change": r2,
        "workdir": str(tmp),
    }, ensure_ascii=False, indent=2))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(prog="boardctl")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("flash", help="烧录并记账")
    p.add_argument("--project")
    p.add_argument("--port")
    p.add_argument("--idf")
    p.add_argument("--claim")
    p.add_argument("--covers", help="逗号分隔的 claims id，例如 c1,c3")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("verify", help="证据是否仍然成立")
    p.add_argument("--project")

    p = sub.add_parser("stop-hook", help="VS Code Stop hook 入口")
    p.add_argument("--project")
    p.add_argument("--format", choices=["claude", "vscode-local"], default="claude")

    p = sub.add_parser("init-vscode", help="给 ESP-IDF 工程装 VS Code 任务与 CLAUDE.md")
    p.add_argument("--project")

    p = sub.add_parser("install-hook", help="把 Stop hook 合并进 Claude Code 设置")
    p.add_argument("--settings", help="默认 ~/.claude/settings.json")

    p = sub.add_parser("install-user-tasks", help="写 VS Code 用户级 tasks.json（全局按钮）")
    p.add_argument("--path")

    p = sub.add_parser("ports", help="列出可烧录的串口")
    p = sub.add_parser("shell", help="开一个已激活 IDF 的 PowerShell")
    p.add_argument("--idf")
    p.add_argument("--exec", help="不交互，改跑给定命令")
    p = sub.add_parser("idf", help="列出 ESP-IDF 安装（--probe 会真跑一次 --version）")
    p.add_argument("--probe", action="store_true")
    p = sub.add_parser("selftest", help="不开板子验证 verify 逻辑")

    args = ap.parse_args()

    if args.cmd == "flash":
        return do_flash(args)
    if args.cmd == "verify":
        root = project_root(Path.cwd(), args.project)
        if not (root / ".boardctl").is_dir():
            print(json.dumps({"ok": True, "armed": False,
                              "note": "这个工程还没成功烧录过（没有 .boardctl/），无需校验"},
                             ensure_ascii=False, indent=2))
            return 0
        rep = do_verify(root)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ok"] else 1
    if args.cmd == "stop-hook":
        return do_stop_hook(args)
    if args.cmd == "ports":
        print(json.dumps({"ok": True, "ports": detect_ports()}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "shell":
        return do_shell(args)
    if args.cmd == "init-vscode":
        return do_init_vscode(args)
    if args.cmd == "install-hook":
        return do_install_hook(args)
    if args.cmd == "install-user-tasks":
        return do_install_user_tasks(args)
    if args.cmd == "idf":
        installs = idf_installs()
        rows = []
        for i in installs:
            row = {
                "name": i.get("name"),
                "path": i.get("path"),
                "venv": bool(i.get("python") and Path(i["python"]).exists()),
                "activation": bool(i.get("activationScript")
                                   and Path(i["activationScript"]).exists()),
                "source": i.get("source"),
            }
            if args.probe:
                row.update(probe_idf(i))
            rows.append(row)
        print(json.dumps({"ok": bool(installs), "installs": rows},
                         ensure_ascii=False, indent=2))
        return 0 if installs else 2
    if args.cmd == "selftest":
        return do_selftest(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
