import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def test_documented_cli_runs_and_replays_in_a_fresh_database(self):
        with tempfile.TemporaryDirectory() as folder:
            command = [sys.executable, "-m", "scheduler_agent", "run", "--input", "examples/inputs/normal.json",
                       "--mode", "mock", "--db", str(Path(folder) / "cli.db"), "--output-dir", str(Path(folder) / "output")]
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
            runs = [subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=True) for _ in range(2)]
            outputs = [json.loads(r.stdout) for r in runs]
            self.assertEqual(outputs[0]["save_status"], "saved")
            self.assertEqual(outputs[1]["save_status"], "already_exists")
            self.assertEqual(outputs[0]["draft_id"], outputs[1]["draft_id"])
            self.assertTrue(all(Path(r["trace_path"]).is_file() for r in outputs))

    def test_cli_failure_returns_nonzero_and_explicit_failed_status(self):
        with tempfile.TemporaryDirectory() as folder:
            command = [sys.executable, "-m", "scheduler_agent", "run", "--input", "examples/inputs/normal.json",
                       "--mode", "mock", "--simulate-save-failure", "--db", str(Path(folder) / "cli.db"),
                       "--output-dir", str(Path(folder) / "output")]
            run = subprocess.run(command, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 1)
            self.assertEqual(json.loads(run.stdout)["save_status"], "failed")
