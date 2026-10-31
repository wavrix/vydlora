"""MEGAcmd commands must finish even when a persistent helper inherits output."""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import mega_service as mega


class MegaCommandStartupTests(unittest.TestCase):
    def run_client(self, source, timeout=3):
        run = subprocess.run
        def launch(_argv, **kwargs):
            return run([sys.executable, "-c", source], **kwargs)
        with patch.object(mega, "client_path", return_value=Path(sys.executable)), \
                patch.object(mega.subprocess, "run", side_effect=launch):
            return mega._run(["whoami"], timeout=timeout)

    def test_persistent_helper_does_not_block_completed_client(self):
        base = Path(__file__).resolve().parents[1] / "build"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="mega-command-", dir=base) as directory:
            pid_file = Path(directory) / "helper.pid"
            source = (
                "import subprocess,sys,pathlib; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)']); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
                "print('Account e-mail: person@example.test',flush=True)"
            )
            started = time.monotonic()
            try:
                result = self.run_client(source)
                self.assertTrue(result.ok, result.message)
                self.assertEqual(result.out.strip(), "Account e-mail: person@example.test")
                self.assertLess(time.monotonic() - started, 3)
            finally:
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_timeout_returns_without_waiting_for_output_eof(self):
        started = time.monotonic()
        result = self.run_client("import time; time.sleep(10)", timeout=0.25)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.message)
        self.assertLess(time.monotonic() - started, 3)

    def test_utf8_and_error_output_are_preserved(self):
        source = "import sys; sys.stdout.buffer.write('caf\u00e9'.encode()); sys.stderr.buffer.write('error'.encode()); sys.exit(7)"
        result = self.run_client(source)
        self.assertEqual(result.rc, 7)
        self.assertEqual(result.out, "caf\u00e9")
        self.assertEqual(result.err, "error")


if __name__ == "__main__":
    unittest.main()
