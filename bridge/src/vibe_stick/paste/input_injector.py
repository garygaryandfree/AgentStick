from __future__ import annotations

import platform
import subprocess
import time
import ctypes
import ctypes.wintypes
from dataclasses import dataclass


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.wintypes.DWORD),
        ("wParamL", ctypes.wintypes.WORD),
        ("wParamH", ctypes.wintypes.WORD),
    ]


class INPUTUNION(ctypes.Union):
    # INPUT is a tagged union. Even though streaming text only uses `ki`, the
    # mouse member must be present so ctypes gives INPUT the Win32 ABI size
    # (40 bytes on 64-bit Windows). A keyboard-only union is 32 bytes and
    # SendInput rejects every event with ERROR_INVALID_PARAMETER.
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", ctypes.wintypes.DWORD), ("data", INPUTUNION)]


@dataclass
class PasteResult:
    success: bool
    message: str


class PasteInjector:
    def begin_session(self, session_id: str) -> PasteResult:
        del session_id
        return PasteResult(True, "Local input session started")

    def end_session(self, session_id: str = "") -> PasteResult:
        del session_id
        return PasteResult(True, "Local input session ended")

    def capture_target(self) -> int:
        if platform.system() != "Windows":
            return 0
        try:
            ctypes.windll.user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
            return int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except (AttributeError, OSError):
            return 0

    def edit(self, delete_count: int, text: str, *, target_window: int = 0) -> PasteResult:
        if platform.system() != "Windows":
            return PasteResult(False, "Streaming input is only available on Windows")
        try:
            user32 = ctypes.windll.user32
            if target_window:
                if not user32.IsWindow(target_window):
                    return PasteResult(False, "The original input window is no longer available")
                if int(user32.GetForegroundWindow() or 0) != target_window:
                    user32.SetForegroundWindow(target_window)

            inputs: list[INPUT] = []
            for _ in range(max(0, delete_count)):
                inputs.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0x08, 0, 0, 0, 0)))
                inputs.append(
                    INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0x08, 0, KEYEVENTF_KEYUP, 0, 0))
                )
            utf16 = text.encode("utf-16-le")
            for offset in range(0, len(utf16), 2):
                code_unit = int.from_bytes(utf16[offset : offset + 2], "little")
                if code_unit in (0x000A, 0x000D):
                    inputs.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0x0D, 0, 0, 0, 0)))
                    inputs.append(
                        INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0x0D, 0, KEYEVENTF_KEYUP, 0, 0))
                    )
                    continue
                inputs.append(
                    INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, code_unit, KEYEVENTF_UNICODE, 0, 0))
                )
                inputs.append(
                    INPUT(
                        type=INPUT_KEYBOARD,
                        ki=KEYBDINPUT(0, code_unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0),
                    )
                )
            if not inputs:
                return PasteResult(True, "No streaming edit was needed")
            user32.SendInput.argtypes = [
                ctypes.wintypes.UINT,
                ctypes.POINTER(INPUT),
                ctypes.c_int,
            ]
            user32.SendInput.restype = ctypes.wintypes.UINT
            input_array = (INPUT * len(inputs))(*inputs)
            sent = int(user32.SendInput(len(inputs), input_array, ctypes.sizeof(INPUT)))
            if sent != len(inputs):
                error = int(ctypes.windll.kernel32.GetLastError())
                return PasteResult(
                    False,
                    f"Streaming input sent {sent}/{len(inputs)} events (Win32 error {error})",
                )
            return PasteResult(True, "Streaming text updated in the focused app")
        except (AttributeError, OSError) as exc:
            return PasteResult(False, f"Windows streaming input failed: {exc}")

    def press_enter(self) -> PasteResult:
        system = platform.system()
        if system == "Windows":
            try:
                user32 = ctypes.windll.user32
                key_up = 0x0002
                vk_return = 0x0D
                user32.keybd_event(vk_return, 0, 0, 0)
                user32.keybd_event(vk_return, 0, key_up, 0)
            except (AttributeError, OSError) as exc:
                return PasteResult(False, f"Windows Enter key failed: {exc}")
            return PasteResult(True, "Pressed Enter in the focused app")
        if system == "Darwin":
            try:
                result = subprocess.run(
                    ["osascript", "-e", 'tell application "System Events" to key code 36'],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return PasteResult(False, f"macOS Enter key failed: {exc}")
            if result.returncode != 0:
                message = (result.stderr or result.stdout or "macOS Enter key failed").strip()
                return PasteResult(False, message)
            return PasteResult(True, "Pressed Enter in the focused app")
        return PasteResult(False, "Automatic Enter is only available on macOS and Windows")

    def paste(self, text: str, press_enter: bool = False) -> PasteResult:
        text = text.strip()
        if not text:
            return PasteResult(False, "No text to paste")
        system = platform.system()
        if system == "Windows":
            return self._paste_windows(text, press_enter)
        if system != "Darwin":
            return PasteResult(False, "Automatic paste is only available on macOS and Windows")

        previous_text = self._read_clipboard()
        set_result = self._set_clipboard(text)
        if not set_result.success:
            return set_result

        script = [
            'tell application "System Events" to keystroke "v" using command down',
        ]
        if press_enter:
            script.extend([
                "delay 0.12",
                'tell application "System Events" to key code 36',
            ])

        args = ["osascript"]
        for line in script:
            args.extend(["-e", line])
        result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=5)
        time.sleep(0.2)
        if previous_text is not None:
            self._set_clipboard(previous_text)

        if result.returncode != 0:
            message = (result.stderr or result.stdout or "macOS paste failed").strip()
            return PasteResult(False, message)
        return PasteResult(True, "Pasted into the focused app")

    def _paste_windows(self, text: str, press_enter: bool) -> PasteResult:
        previous_text = self._read_windows_clipboard()
        set_result = self._set_windows_clipboard(text)
        if not set_result.success:
            return set_result
        try:
            user32 = ctypes.windll.user32
            key_up = 0x0002
            vk_control = 0x11
            vk_v = 0x56
            vk_return = 0x0D
            user32.keybd_event(vk_control, 0, 0, 0)
            user32.keybd_event(vk_v, 0, 0, 0)
            user32.keybd_event(vk_v, 0, key_up, 0)
            user32.keybd_event(vk_control, 0, key_up, 0)
            if press_enter:
                time.sleep(0.12)
                user32.keybd_event(vk_return, 0, 0, 0)
                user32.keybd_event(vk_return, 0, key_up, 0)
            time.sleep(0.2)
        except (AttributeError, OSError) as exc:
            return PasteResult(False, f"Windows paste failed: {exc}")
        finally:
            if previous_text is not None:
                self._set_windows_clipboard(previous_text)
        return PasteResult(True, "Pasted into the focused app")

    def _read_windows_clipboard(self) -> str | None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetClipboardData.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        cf_unicode_text = 13
        if not self._open_windows_clipboard(user32):
            return None
        try:
            handle = user32.GetClipboardData(cf_unicode_text)
            if not handle:
                return None
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def _set_windows_clipboard(self, text: str) -> PasteResult:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        cf_unicode_text = 13
        gmem_moveable = 0x0002
        data = (text + "\0").encode("utf-16-le")
        if not self._open_windows_clipboard(user32):
            return PasteResult(False, "Could not open the Windows clipboard")
        handle = None
        try:
            if not user32.EmptyClipboard():
                return PasteResult(False, "Could not clear the Windows clipboard")
            handle = kernel32.GlobalAlloc(gmem_moveable, len(data))
            if not handle:
                return PasteResult(False, "Could not allocate Windows clipboard memory")
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return PasteResult(False, "Could not lock Windows clipboard memory")
            try:
                ctypes.memmove(pointer, data, len(data))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(cf_unicode_text, handle):
                return PasteResult(False, "Could not update the Windows clipboard")
            handle = None  # Clipboard owns the allocation after SetClipboardData succeeds.
            return PasteResult(True, "Clipboard updated")
        finally:
            if handle:
                kernel32.GlobalFree(handle)
            user32.CloseClipboard()

    @staticmethod
    def _open_windows_clipboard(user32: object) -> bool:
        for _ in range(10):
            if user32.OpenClipboard(None):
                return True
            time.sleep(0.02)
        return False

    def _read_clipboard(self) -> str | None:
        try:
            result = subprocess.run(
                ["pbpaste"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def _set_clipboard(self, text: str) -> PasteResult:
        try:
            result = subprocess.run(
                ["pbcopy"],
                input=text,
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return PasteResult(False, f"Clipboard write failed: {exc}")
        if result.returncode != 0:
            message = (result.stderr or "Clipboard write failed").strip()
            return PasteResult(False, message)
        return PasteResult(True, "Clipboard updated")


# Backwards-compatible name for callers and third-party imports.
MacPasteInjector = PasteInjector
