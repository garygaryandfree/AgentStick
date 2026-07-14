import ctypes
import platform
import unittest

from vibe_stick.paste.input_injector import INPUT


class InputInjectorTests(unittest.TestCase):
    def test_input_structure_matches_windows_abi(self) -> None:
        if platform.system() != "Windows":
            self.skipTest("Win32 ABI assertion")
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(INPUT), expected)


if __name__ == "__main__":
    unittest.main()
