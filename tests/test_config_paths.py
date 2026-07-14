import os
import unittest
from pathlib import Path
from unittest import mock

from vibe_stick.config import paths


class AppSupportPathTests(unittest.TestCase):
    def test_explicit_override_wins(self) -> None:
        with mock.patch.dict(os.environ, {"VIBE_STICK_APP_SUPPORT_DIR": "custom-data"}, clear=False):
            self.assertEqual(paths._app_support_dir(), Path("custom-data"))

    def test_windows_uses_local_app_data(self) -> None:
        env = {"LOCALAPPDATA": r"C:\Users\tester\AppData\Local"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(paths.platform, "system", return_value="Windows"):
                self.assertEqual(
                    paths._app_support_dir(),
                    Path(r"C:\Users\tester\AppData\Local") / "VibeStick",
                )


if __name__ == "__main__":
    unittest.main()
