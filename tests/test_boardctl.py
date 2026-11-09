"""boardctl 的最小测试集：不需要开发板，也不需要 ESP-IDF。

跑法：python -m unittest discover -s tests -v
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "boardctl.py"

_spec = importlib.util.spec_from_file_location("boardctl", SCRIPT)
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)

RUN_KW = dict(capture_output=True, encoding="utf-8", errors="replace")


def make_project(tmp: Path) -> Path:
    """造一个最小的 ESP-IDF 形状工程。"""
    (tmp / "main").mkdir(parents=True, exist_ok=True)
    (tmp / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
    (tmp / "sdkconfig").write_text('CONFIG_IDF_TARGET="esp32"\n', encoding="utf-8")
    (tmp / "main" / "main.c").write_text("void app_main(void){}\n", encoding="utf-8")
    return tmp


def record_flash(root: Path, ev_id: str = "ev-0001") -> str:
    """模拟一次成功烧录：写 manifest + 一条 host 证据。"""
    sha, entries = bc.compute_inputs(root)
    bc.write_manifest(root, sha, entries)
    bc.append_jsonl(root / "evidence.jsonl", {
        "id": ev_id, "ts": bc.now(), "kind": "flash", "claim": "test",
        "result": "pass", "covers": [], "source": "host",
        "inputs_sha256": sha, "artifacts": [],
    })
    return sha


def run_hook(root: Path, fmt: str = "claude", stdin: str = "{}") -> str:
    p = subprocess.run(
        [sys.executable, str(SCRIPT), "stop-hook", "--format", fmt],
        cwd=str(root), input=stdin, timeout=120, **RUN_KW,
    )
    return (p.stdout or "").strip()


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bc-test-"))
        make_project(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unarmed_project_is_not_guarded(self):
        """没烧过的工程不能被拦。"""
        self.assertFalse(bc.is_armed(self.tmp))
        self.assertEqual(run_hook(self.tmp), "")

    def test_verified_after_record(self):
        record_flash(self.tmp)
        rep = bc.do_verify(self.tmp)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["evidence_pass_count"], 1)

    def test_source_change_makes_it_stale(self):
        record_flash(self.tmp)
        (self.tmp / "main" / "main.c").write_text(
            "void app_main(void){ /*x*/ }\n", encoding="utf-8")
        rep = bc.do_verify(self.tmp)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("过期" in p for p in rep["problems"]), rep)

    def test_missing_claim_coverage_reported(self):
        record_flash(self.tmp)
        (self.tmp / "claims.jsonl").write_text(
            json.dumps({"id": "c9", "text": "点灯"}) + "\n", encoding="utf-8")
        rep = bc.do_verify(self.tmp)
        self.assertIn("c9", rep["missing_covers"])


class TestHook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bc-hook-"))
        make_project(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_claude_format_blocks_when_stale(self):
        record_flash(self.tmp)
        (self.tmp / "main" / "main.c").write_text("// changed\n", encoding="utf-8")
        payload = json.loads(run_hook(self.tmp, "claude"))
        self.assertEqual(payload["decision"], "block")
        self.assertIn("reason", payload)

    def test_vscode_local_format_shape(self):
        record_flash(self.tmp)
        (self.tmp / "main" / "main.c").write_text("// changed\n", encoding="utf-8")
        payload = json.loads(run_hook(self.tmp, "vscode-local"))
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "Stop")

    def test_stop_hook_active_does_not_loop(self):
        """已被拦过一次就该放行，避免无限循环。"""
        record_flash(self.tmp)
        (self.tmp / "main" / "main.c").write_text("// drift\n", encoding="utf-8")
        out = run_hook(self.tmp, "claude", '{"stop_hook_active": true}')
        self.assertEqual(out, "")


class TestEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bc-ev-"))
        make_project(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_is_append_only(self):
        rec = {"id": "ev-1", "ts": bc.now(), "kind": "flash", "claim": "a",
               "result": "pass", "covers": [], "source": "host",
               "inputs_sha256": "x", "artifacts": []}
        bc.append_jsonl(self.tmp / "evidence.jsonl", rec)
        bc.append_jsonl(self.tmp / "evidence.jsonl", dict(rec, id="ev-2"))
        lines = (self.tmp / "evidence.jsonl").read_text(
            encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_inputs_hash_ignores_build_dir(self):
        sha1, _ = bc.compute_inputs(self.tmp)
        (self.tmp / "build").mkdir(exist_ok=True)
        (self.tmp / "build" / "app.bin").write_bytes(b"binary")
        sha2, _ = bc.compute_inputs(self.tmp)
        self.assertEqual(sha1, sha2, "build/ 不该进指纹")


class TestCLI(unittest.TestCase):
    def test_selftest_command_passes(self):
        p = subprocess.run([sys.executable, str(SCRIPT), "selftest"],
                           timeout=180, **RUN_KW)
        payload = json.loads(p.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertFalse(payload["after_change"]["ok"])

    def test_help_works(self):
        p = subprocess.run([sys.executable, str(SCRIPT), "--help"], timeout=120, **RUN_KW)
        self.assertEqual(p.returncode, 0)
        self.assertIn("flash", p.stdout)


if __name__ == "__main__":
    unittest.main()
