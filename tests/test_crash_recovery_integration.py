"""Real parent abrupt exit after receipt, then recovery without rerunning trial one."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from as_researcher.batch_runtime import recovery_preview

ROOT = Path(__file__).resolve().parents[1]


class CrashRecoveryIntegrationTests(unittest.TestCase):
    def test_abrupt_parent_exit_adopts_committed_receipt_and_runs_only_remaining(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "reports") as tmp:
            batch = Path(tmp) / "batch"
            # The actual worker saves run/draft/receipt. Only the parent is killed,
            # exactly before the saved index is committed. No production crash flag.
            code = """import os,sys
from scripts import benchmark_research as command
original=command.write_json
def crash(path,value):
    if path.name=="index.json" and isinstance(value,list) and any(r.get("status")=="saved" for r in value):
        os._exit(23)
    original(path,value)
command.write_json=crash
command.main(["run","--mode","fixture","--case","mcp_stdio","--strategy","segments","--repeats","2","--corpus","reports/evaluation-20261101/corpus-exposed","--execute","--output",sys.argv[1]])
"""
            crashed = subprocess.run(
                [sys.executable, "-c", code, str(batch)],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(crashed.returncode, 23, crashed.stderr[-1000:])
            preview = recovery_preview(batch, ROOT / "scripts/benchmark_research.py")
            self.assertEqual(len(preview["recoverable"]), 1)
            self.assertEqual(len(preview["pending"]), 1)
            self.assertEqual(preview["uncertain"], [])
            receipt = next(iter(preview["recoverable"].values()))
            run_path = batch / receipt["run_file"] / "run.json"
            before = hashlib.sha256(run_path.read_bytes()).hexdigest()
            recovered = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/benchmark_research.py"),
                    "resume",
                    "--batch",
                    str(batch),
                    "--execute",
                ],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr[-1000:])
            rows = json.loads((batch / "index.json").read_bytes())
            self.assertTrue(all(row["status"] == "saved" for row in rows))
            self.assertEqual(hashlib.sha256(run_path.read_bytes()).hexdigest(), before)
            self.assertEqual(len(list((batch / rows[0]["id"]).glob("*/run.json"))), 1)
            self.assertEqual(len(list((batch / "attempts").glob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
