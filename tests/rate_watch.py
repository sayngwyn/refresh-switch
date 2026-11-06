"""Write down the main monitor's rate and which game is running, each time either changes. Read-only: it never changes anything.

    python tests/rate_watch.py <log file> <minutes>
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rate_state import primary  # noqa: E402

GAMES = {"skyrimse.exe", "fallout4.exe"}


def games_on_this_desktop() -> list[str]:
    """Game windows on the visible desktop; hidden test games have theirs on another desktop."""
    user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    found: set[str] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def look(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = kernel32.OpenProcess(0x1000, False, pid.value)
        if process:
            buffer, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                name = Path(buffer.value).name.lower()
                if name in GAMES:
                    found.add(name)
            kernel32.CloseHandle(process)
        return True

    user32.EnumWindows(look, 0)
    return sorted(found)


def main() -> int:
    log, minutes = Path(sys.argv[1]), float(sys.argv[2])
    ends = time.monotonic() + minutes * 60
    last = None
    with log.open("a", encoding="utf-8") as out:
        while time.monotonic() < ends:
            try:
                state = primary()
                now = (state["rate"], state["saved"], state["hdr"], tuple(games_on_this_desktop()))
            except OSError:   # the display is in the middle of a change
                time.sleep(0.1)
                continue
            if now != last:
                out.write(f"{datetime.now():%H:%M:%S.%f} rate {now[0]} saved {now[1]} HDR {now[2]} game {','.join(now[3]) or 'none'}\n")
                out.flush()
                last = now
            time.sleep(0.25)
    return 0


if __name__ == "__main__":
    sys.exit(main())
