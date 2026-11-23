"""Deployment configuration: resolve runtime files outside a fresh Git checkout."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DataDirectoryTests(unittest.TestCase):
    def test_external_data_dir_reused_for_quarantine_and_bot_state(self):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["BOT_DATA_DIR"] = directory
            code = (
                "from pathlib import Path; import config; "
                "assert Path(config.DATA_DIR).resolve() == Path(" + repr(directory) + ").resolve(); "
                "assert config.MEMORY_FILE.startswith(config.DATA_DIR); "
                "assert config.WARNINGS_FILE.startswith(config.DATA_DIR); "
                "assert config.AUTOMOD_FILE.startswith(config.DATA_DIR)"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=Path(__file__).resolve().parent.parent,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_empty_path_falls_back_to_local_data_directory(self):
        env = os.environ.copy()
        env["BOT_DATA_DIR"] = ""
        result = subprocess.run(
            [sys.executable, "-c", "import config; assert config.DATA_DIR == 'data'"],
            cwd=Path(__file__).resolve().parent.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
