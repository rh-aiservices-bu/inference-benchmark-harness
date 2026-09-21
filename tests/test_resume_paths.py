"""A mistyped resume path must not create or alter a campaign."""
from pathlib import Path
import fcntl
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ResumePathTests(unittest.TestCase):
    def test_missing_checkpoint_is_rejected_without_writing(self):
        for command, config in (("run", "benchmark.json"), ("matrix-run", "matrix.json")):
            for layout in ("missing", "existing", "checkpoint_directory", "abandoned_lock", "file"):
                with self.subTest(command=command, layout=layout), tempfile.TemporaryDirectory() as tmp:
                    parent = Path(tmp)
                    run = parent / "nested" / "run"
                    if layout != "missing":
                        run.parent.mkdir()
                        if layout == "file":
                            run.write_bytes(b"unrelated file")
                        else:
                            run.mkdir()
                            (run / "keep.txt").write_bytes(b"existing evidence")
                            if layout == "abandoned_lock":
                                (run / ".lock").write_bytes(b"retained lock")
                            if layout == "checkpoint_directory":
                                (run / "state.json").mkdir()

                    def snapshot():
                        return {str(p.relative_to(parent)): p.read_bytes() if p.is_file() else None
                                for p in parent.rglob("*")}

                    before = snapshot()
                    result = subprocess.run(
                        [sys.executable, "-m", "bench", command, "--config", str(ROOT / "examples" / config),
                         "--run", str(run), "--resume", "--execute", "--aiperf", "must-not-run"],
                        cwd=ROOT, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(snapshot(), before, "Rejected resume changed the filesystem")
                    self.assertEqual(result.stdout, "")
                    self.assertIn("checkpoint", result.stderr)

    def test_active_owner_blocks_resume_before_first_checkpoint(self):
        for command, config in (("run", "benchmark.json"), ("matrix-run", "matrix.json")):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                run = Path(tmp)
                with (run / ".lock").open("w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    result = subprocess.run(
                        [sys.executable, "-m", "bench", command, "--config", str(ROOT / "examples" / config),
                         "--run", str(run), "--resume", "--execute", "--aiperf", "must-not-run"],
                        cwd=ROOT, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("still owns", result.stderr)
                self.assertEqual(set(p.name for p in run.iterdir()), {".lock"})
