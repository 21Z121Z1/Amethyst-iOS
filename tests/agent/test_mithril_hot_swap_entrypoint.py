from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class MithrilHotSwapEntryPointIntegrationTests(unittest.TestCase):
    def test_status_executes_real_module_and_canonical_daemon_without_device(self) -> None:
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["AMETHYST_AGENT_HOME"] = tmp
            env["AMETHYST_AGENT_PYTHON"] = sys.executable
            env.pop("AMETHYST_DEVICE_UDID", None)
            env.pop("AMETHYST_BUNDLE_ID", None)

            result = subprocess.run(
                [str(root / "tools/amethystctl"), "mithril", "status"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertEqual(result.returncode, 5, result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["failure"], "DEVICE_NOT_FOUND")
            self.assertIn("configure a device", payload["detail"])

            stop = subprocess.run(
                [str(root / "tools/amethystctl"), "daemon", "stop"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertEqual(stop.returncode, 0, stop.stderr)
            stopped = json.loads(stop.stdout.strip().splitlines()[-1])
            self.assertTrue(stopped["ok"])
            self.assertTrue(stopped["stopping"])


if __name__ == "__main__":
    unittest.main()
