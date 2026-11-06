"""Does the rate change, stay out of Windows' settings, and go back exactly, however the game closes, alone and beside HDR Switch? Stand-in games load the real DLLs through
a stand-in of the loader, including the all-plugins-loaded message.

    python tests/rate_check.py --quiet            Nothing on the monitor changes and no window shows. Safe at any time.
    python tests/rate_check.py --full             THIS VISIBLY FLIPS THE PRIMARY MONITOR about 40 times over a few minutes and shows small windows. Only when nobody is playing or filming.
    (--hdr PATH: HdrSwitch.dll for the runs beside it; default: a sibling HDR Switch folder's dist. --rate N: the rate to switch to; default: another one the monitor offers.)

The game's logs are put back afterwards.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rate_state import monitors, primary  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "tests" / "test-env" / "rate_check"
PLUGIN = WORK / "plugin"
GAMES = {"skyrimse.exe", "fallout4.exe", "skyrimvr.exe", "fallout4vr.exe"}
OLD_LISTED = 0   # set in main, before anything changes
CHANGE_LIMIT_S = 10.0
BACK_LIMIT_S = 16.0   # HDR Switch's turn (up to 6 s), then the change

STAND_IN = r"""
import ctypes, os, sys
from ctypes import c_bool, c_char_p, c_uint32, c_void_p
dlls, title, how, make_window = sys.argv[1].split(";"), sys.argv[2], sys.argv[3], sys.argv[4]
class Message(ctypes.Structure):
    _fields_ = [("sender", c_char_p), ("type", c_uint32), ("dataLen", c_uint32), ("data", c_void_p)]
listeners = []
REGISTER = ctypes.CFUNCTYPE(c_bool, c_uint32, c_char_p, c_void_p)
QUERY = ctypes.CFUNCTYPE(c_void_p, c_uint32)
HANDLE = ctypes.CFUNCTYPE(c_uint32)
LISTENER = ctypes.CFUNCTYPE(None, ctypes.POINTER(Message))
def register(handle, sender, callback):
    listeners.append(LISTENER(callback))
    return True
register_fn = REGISTER(register)
class Messaging(ctypes.Structure):
    _fields_ = [("version", c_uint32), ("register", REGISTER)]
messaging = Messaging(1, register_fn)
query_fn = QUERY(lambda which: ctypes.addressof(messaging) if which == 5 else None)
handle_fn = HANDLE(lambda: 7)
class Interface(ctypes.Structure):
    _fields_ = [("xse", c_uint32), ("runtime", c_uint32), ("editor", c_uint32), ("isEditor", c_uint32), ("query", QUERY), ("handle", HANDLE), ("release", c_void_p), ("info", c_void_p)]
interface = Interface(0, (1 << 24) | (6 << 16) | (1170 << 4), 0, 0, query_fn, handle_fn, None, None)
user32 = ctypes.WinDLL("user32")
user32.CreateWindowExW.restype = c_void_p
user32.CreateWindowExW.argtypes = [c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, c_uint32, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, c_void_p, c_void_p, c_void_p, c_void_p]
kept = []
for path in dlls:
    dll = ctypes.WinDLL(path)
    kept.append(dll)
    dll.SKSEPlugin_Load.restype = c_bool
    dll.SKSEPlugin_Load(ctypes.byref(interface))
if make_window == "1":
    # A never-shown window: proves the plugin's window count sees one when there is one.
    user32.CreateWindowExW(0, "STATIC", "stand-in", 0, 0, 0, 10, 10, None, None, None, None)
for kind in (0, 1):
    for listener in listeners:
        listener(ctypes.byref(Message(b"SKSE", kind, 0, None)))
print("loaded", flush=True)
if how == "wait":
    import time
    time.sleep(600)
import tkinter
root = tkinter.Tk()
root.title(title)
root.geometry("320x60")
tkinter.Label(root, text="Refresh Switch test: closes by itself").pack()
if how == "quits":
    root.after(1500, lambda: os._exit(0))
elif how == "crashes":
    ctypes.windll.kernel32.SetErrorMode(2)
    root.after(1500, lambda: ctypes.windll.kernel32.RaiseFailFastException(None, None, 0))
root.mainloop()
"""


def documents() -> Path:
    guid = ctypes.c_char_p(bytes.fromhex("D09AD3FD8F23AF46ADB46C85480369C7"))   # FOLDERID_Documents
    out = ctypes.c_wchar_p()
    if ctypes.WinDLL("shell32").SHGetKnownFolderPath(guid, 0, None, ctypes.byref(out)):
        raise OSError("no Documents folder")
    return Path(out.value)


LOG_DIR = documents() / "My Games" / "Skyrim Special Edition" / "SKSE"
LOG = LOG_DIR / "RefreshSwitch.log"
HDR_LOG = LOG_DIR / "HdrSwitch.log"
RUNDLL = Path(os.environ["SystemRoot"]) / "System32" / "rundll32.exe"


def games_on_this_desktop() -> list[str]:
    """Game windows on the visible desktop. Hidden test games have theirs on another desktop, so they don't count."""
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


class DevMode(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar * 32), ("specVersion", ctypes.c_ushort), ("driverVersion", ctypes.c_ushort), ("size", ctypes.c_ushort), ("extra", ctypes.c_ushort),
                ("fields", ctypes.c_uint32), ("display", ctypes.c_byte * 16), ("color", ctypes.c_short), ("duplex", ctypes.c_short), ("yRes", ctypes.c_short), ("tt", ctypes.c_short),
                ("collate", ctypes.c_short), ("form", ctypes.c_wchar * 32), ("logPixels", ctypes.c_ushort), ("bits", ctypes.c_uint32), ("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("freq", ctypes.c_uint32), ("rest", ctypes.c_byte * 32)]


def listed_now() -> int:
    """The current rate as Windows' list writes it: 240 for 239.765, 120 for 119.998, 59 for 59.94."""
    user32 = ctypes.WinDLL("user32")
    user32.EnumDisplaySettingsW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p]
    current = DevMode(size=ctypes.sizeof(DevMode))
    user32.EnumDisplaySettingsW(None, 0xFFFFFFFF, ctypes.byref(current))
    return current.freq


def offered_rates() -> list[int]:
    user32 = ctypes.WinDLL("user32")
    user32.EnumDisplaySettingsW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p]
    current = DevMode(size=ctypes.sizeof(DevMode))
    user32.EnumDisplaySettingsW(None, 0xFFFFFFFF, ctypes.byref(current))
    rates, index = set(), 0
    while True:
        mode = DevMode(size=ctypes.sizeof(DevMode))
        if not user32.EnumDisplaySettingsW(None, index, ctypes.byref(mode)):
            return sorted(rates)
        if (mode.width, mode.height) == (current.width, current.height) and mode.freq > 1:
            rates.add(mode.freq)
        index += 1


def write_ini(text: str) -> None:
    (PLUGIN / "RefreshSwitch.ini").write_text(f"[General]\nfRefreshRate={text}\n", encoding="ascii")


def clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("HDR_SWITCH_OFF", "REFRESH_SWITCH_OFF")}
    env.update(extra)
    return env


def start_game(dlls: list[Path], how: str, title: str = "x", env: dict | None = None, window: str = "0") -> subprocess.Popen:
    game = subprocess.Popen([sys.executable, "-c", STAND_IN, ";".join(map(str, dlls)), title, how, window], env=env or clean_env(), stdout=subprocess.PIPE, text=True)
    return game


def wait_loaded(game: subprocess.Popen) -> None:
    assert game.stdout is not None
    game.stdout.readline()


def wait_until(check, limit: float) -> float | None:
    started = time.perf_counter()
    while time.perf_counter() - started < limit:
        if check():
            return time.perf_counter() - started
        time.sleep(0.02)
    return None


def log_text() -> str:
    return LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""


def window_of(title: str, limit: float = 15.0) -> int:
    started = time.perf_counter()
    while time.perf_counter() - started < limit:
        hwnd = ctypes.WinDLL("user32").FindWindowW(None, title)
        if hwnd:
            return hwnd
        time.sleep(0.05)
    return 0


def close(how: str, game: subprocess.Popen, hwnd: int) -> None:
    user32 = ctypes.WinDLL("user32")
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    user32.EndTask.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    if how == "close button":
        user32.PostMessageW(hwnd, 0x0010, None, None)
    elif how == "force kill":
        game.kill()
    elif how == "End Task call":
        user32.EndTask(hwnd, 0, 1)
    elif how == "kill whole process tree":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(game.pid)], capture_output=True, timeout=30)


class Check:
    def __init__(self) -> None:
        self.failed: list[str] = []

    def row(self, good: bool, name: str, detail: str = "") -> bool:
        print(f"{'PASSED' if good else 'FAILED'}  {name}{': ' + detail if detail else ''}", flush=True)
        if not good:
            self.failed.append(name)
        return good


def quiet_part(check: Check, dll_dir: Path, rate_list: list[int]) -> None:
    dll = dll_dir / "RefreshSwitch.dll"
    before = monitors()
    not_offered = next(rate for rate in range(77, 1000) if all(abs(rate - offered) > 1 for offered in rate_list))
    current = int(primary()["rate"] + 0.5)

    def one(name: str, ini: str | None, expect: str, env: dict | None = None, window: str = "0", some_window: bool = False) -> None:
        LOG.unlink(missing_ok=True)
        (PLUGIN / "RefreshSwitch.ini").unlink(missing_ok=True)
        if ini is not None:
            write_ini(ini)
        game = start_game([dll], "wait", env=env, window=window)
        wait_loaded(game)
        text = log_text()
        game.kill()
        game.wait(timeout=20)
        lines = text.strip().splitlines() or [""]
        good = expect in text and monitors() == before and not (some_window and "windows open: 0," in text)
        check.row(good, name, repr(next((line for line in lines if expect in line), lines[-1])))

    one("no ini file does nothing", None, "skipped: no rate set")
    one("fRefreshRate=0 does nothing", "0", "skipped: no rate set")
    one("nonsense in the ini does nothing", "fast", "skipped: no rate set")
    one("a rate the monitor does not offer is refused", str(not_offered), "does not offer")
    one("the rate it is already at changes nothing", str(current), "already at")
    one("REFRESH_SWITCH_OFF keeps it quiet", "120", "REFRESH_SWITCH_OFF is set", env=clean_env(REFRESH_SWITCH_OFF="1"))
    one("HDR_SWITCH_OFF keeps it quiet", "120", "HDR_SWITCH_OFF is set", env=clean_env(HDR_SWITCH_OFF="1"))
    one("no window, and the log says so", "0", "game windows open: 0, 0 visible")
    one("a window, and the log says so", "0", "game windows open:", window="1", some_window=True)   # Windows adds its own input windows beside it, so more than 1

    # On a hidden desktop, with no test setting and a rate it could change to.
    other = next(rate for rate in reversed(rate_list) if rate != current)
    write_ini(str(other))
    LOG.unlink(missing_ok=True)
    user32, kernel32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR), ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR), ("x", wintypes.DWORD), ("y", wintypes.DWORD),
                    ("xs", wintypes.DWORD), ("ys", wintypes.DWORD), ("xc", wintypes.DWORD), ("yc", wintypes.DWORD), ("attr", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("show", wintypes.WORD), ("r2", wintypes.WORD), ("r3", ctypes.c_void_p), ("i", wintypes.HANDLE), ("o", wintypes.HANDLE), ("e", wintypes.HANDLE)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE), ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]

    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    desktop = user32.CreateDesktopW("RefreshSwitchTest", None, None, 0, 0x10000000, None)
    script = WORK / "standin.py"
    script.write_text(STAND_IN, encoding="utf-8")
    start, info = StartupInfo(cb=ctypes.sizeof(StartupInfo), desktop="RefreshSwitchTest"), ProcessInfo()
    command = ctypes.create_unicode_buffer(f'"{sys.executable}" "{script}" "{dll}" x wait 0')
    block = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in clean_env().items()) + "\0")
    if not kernel32.CreateProcessW(None, command, None, None, False, 0x08000400, block, None, ctypes.byref(start), ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        wait_until(lambda: "skipped" in log_text(), 20)
        text = log_text()
        check.row("hidden desktop" in text and monitors() == before, "a hidden desktop keeps it quiet", repr(text.strip().splitlines()[-1] if text.strip() else ""))
    finally:
        kernel32.TerminateProcess(info.process, 1)
        kernel32.WaitForSingleObject(info.process, 5000)
        kernel32.CloseHandle(info.process)
        kernel32.CloseHandle(info.thread)
        user32.CloseDesktop(desktop)


def put_back(dll_dir: Path, hdr_dll: Path | None, start: dict) -> None:
    """Anything a failed step left: the plugins' own watchers, told to watch a process already gone."""
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait(timeout=20)
    now = primary()
    if hdr_dll and now["hdr"] and not start["hdr"]:
        subprocess.run(f'"{RUNDLL}" "{hdr_dll}",HdrSwitchWatch {gone.pid} {start["adapter"][0]} {start["adapter"][1]} {start["target"]} SKSE', timeout=20)
    now = primary()
    if now["exact"] != start["exact"]:
        subprocess.run(f'"{RUNDLL}" "{dll_dir / "RefreshSwitch.dll"}",RefreshSwitchWatch {gone.pid} {start["adapter"][0]} {start["adapter"][1]} {start["target"]} SKSE '
                       f'{start["exact"][0]} {start["exact"][1]} {OLD_LISTED} {listed_now()}', timeout=20)
    print("put back; monitors now:", json.dumps(monitors()), flush=True)


def watchers() -> list[int]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Process -Filter \"name='rundll32.exe'\" | Where-Object CommandLine -match 'RefreshSwitchWatch').ProcessId"],
                         capture_output=True, text=True, timeout=60).stdout
    return [int(line) for line in out.split()]


def full_part(check: Check, dll_dir: Path, hdr_dir: Path | None, wanted: int, skip_closes: bool = False) -> None:
    dll = dll_dir / "RefreshSwitch.dll"
    hdr = (hdr_dir / "HdrSwitch.dll") if hdr_dir else None
    start_state = monitors()
    start = primary()
    write_ini(str(wanted))
    at_wanted = lambda: abs(primary()["rate"] - wanted) < 1.01
    restored = lambda: monitors() == start_state

    def closes(label: str, dlls: list[Path]) -> None:
        for how in ("quits", "crashes", "close button", "force kill", "End Task call", "kill whole process tree"):
            LOG.unlink(missing_ok=True)
            title = f"Refresh Switch test {os.getpid()}"
            game = start_game(dlls, how, title)
            hwnd = window_of(title)
            changed = wait_until(at_wanted, CHANGE_LIMIT_S)
            during = primary()
            time.sleep(0.5)
            closed = time.perf_counter()
            close(how, game, hwnd)
            try:
                game.wait(timeout=20)
            except subprocess.TimeoutExpired:
                game.kill()
                game.wait(timeout=20)
            back = wait_until(restored, BACK_LIMIT_S)
            taken = "never" if back is None else f"{time.perf_counter() - closed:.2f} s"
            good = changed is not None and back is not None and during["saved"] == start["saved"]
            if hdr in dlls:
                good = good and during["hdr"] is True
            lines = log_text().strip().splitlines()
            check.row(good, f"{label}, {how}", f"rate {during['rate']} (saved {during['saved']}, HDR {during['hdr']}) then back after {taken}; {lines[-1] if lines else ''}")
            if back is None:
                raise RuntimeError("it did not go back")
            time.sleep(1)

    if not skip_closes:
        closes("alone", [dll])
        if hdr:
            closes("HDR Switch first in the load order", [hdr, dll])
            closes("HDR Switch second in the load order", [dll, hdr])

    if hdr:
        # Both orders of closing: two stand-ins, so each plugin's game can end on its own.
        for first in ("HDR", "rate"):
            hdr_game = start_game([hdr], "wait")
            wait_loaded(hdr_game)
            wait_until(lambda: primary()["hdr"] is True, CHANGE_LIMIT_S)
            rate_game = start_game([dll], "wait")
            wait_loaded(rate_game)
            on_both = wait_until(at_wanted, CHANGE_LIMIT_S) is not None and primary()["hdr"] is True
            time.sleep(1)
            if first == "HDR":
                # Windows drops an unsaved rate when HDR goes off, so the old rate comes back here by itself. Only a second game can do this: in one game HDR Switch goes off after the game.
                hdr_game.kill()
                wait_until(lambda: primary()["hdr"] is False, CHANGE_LIMIT_S)
                time.sleep(1)
                middle = primary()
                middle_good = middle["hdr"] is False
                rate_game.kill()
            else:
                rate_game.kill()
                wait_until(lambda: primary()["exact"] == start["exact"], CHANGE_LIMIT_S)
                time.sleep(1)
                middle = primary()
                middle_good = middle["exact"] == start["exact"] and middle["hdr"] is True
                hdr_game.kill()
            hdr_game.wait(timeout=20)
            rate_game.wait(timeout=20)
            back = wait_until(restored, BACK_LIMIT_S)
            check.row(on_both and middle_good and back is not None, f"{first} ends first", f"in between: rate {middle['rate']}, HDR {middle['hdr']}; all back: {back is not None}")
            if back is None:
                raise RuntimeError("it did not go back")
            time.sleep(1)

    # Exact rates, both directions: a first stand-in sets the "desktop" rate, a second switches from it and must return to it exactly.
    rates = offered_rates()
    bases = [rate for rate in (59, 120) if rate in rates and rate != wanted]
    for base in bases:
        write_ini(f"{base}.94" if base == 59 else str(base))
        desktop = start_game([dll], "wait")
        wait_loaded(desktop)
        wait_until(lambda: abs(primary()["rate"] - base) < 1.01, CHANGE_LIMIT_S)
        base_state = primary()
        top = int(start["rate"] + 0.5)
        write_ini(str(top))
        later_game = start_game([dll], "wait")
        wait_loaded(later_game)
        moved = wait_until(lambda: abs(primary()["rate"] - top) < 1.01, CHANGE_LIMIT_S)
        time.sleep(1)
        later_game.kill()
        later_game.wait(timeout=20)
        back = wait_until(lambda: primary()["exact"] == base_state["exact"], BACK_LIMIT_S)
        check.row(moved is not None and back is not None, f"goes from {base_state['rate']} to {top} and back exactly", f"{base_state['exact']} now {primary()['exact']}")
        desktop.kill()
        desktop.wait(timeout=20)
        if wait_until(restored, BACK_LIMIT_S) is None:
            raise RuntimeError("it did not go back")
        time.sleep(1)

    # Someone else changes the rate during the game: it is theirs, so it stays.
    third = next((rate for rate in reversed(rates) if abs(rate - wanted) > 1.01 and abs(rate - start["rate"]) > 1.01), None)
    if third:
        write_ini(str(wanted))
        LOG.unlink(missing_ok=True)
        game = start_game([dll], "wait")
        wait_loaded(game)
        wait_until(at_wanted, CHANGE_LIMIT_S)
        time.sleep(1)
        user32 = ctypes.WinDLL("user32")
        user32.ChangeDisplaySettingsExW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        other = DevMode(size=ctypes.sizeof(DevMode), fields=0x400000, freq=third)
        answer = user32.ChangeDisplaySettingsExW(None, ctypes.byref(other), None, 0, None)
        if answer or wait_until(lambda: listed_now() == third, CHANGE_LIMIT_S) is None:
            print(f"the test's own change to {third} failed: answer {answer}", flush=True)
        time.sleep(1)
        game.kill()
        game.wait(timeout=20)
        time.sleep(5)
        kept = abs(primary()["rate"] - third) < 1.01
        check.row(kept and "leaving it" in log_text() and "another program changed it" in log_text(), "a rate someone else set during the game stays, and the log says so", log_text().strip().splitlines()[-1])
        put_back(dll_dir, hdr, start)
        if not restored():
            raise RuntimeError("it did not go back")
        time.sleep(1)

    # A game closed and another started at once: the second waits for the first's watcher and reads the right old rate.
    write_ini(str(wanted))
    first_game = start_game([dll], "wait")
    wait_loaded(first_game)
    wait_until(at_wanted, CHANGE_LIMIT_S)
    time.sleep(0.5)
    first_game.kill()
    first_game.wait(timeout=20)
    second = start_game([dll], "wait")
    wait_loaded(second)
    wait_until(at_wanted, CHANGE_LIMIT_S)
    time.sleep(1)
    second.kill()
    second.wait(timeout=20)
    back = wait_until(restored, BACK_LIMIT_S)
    check.row(back is not None, "closing a game and starting another at once", f"back to the real old rate: {back is not None}; {log_text().strip().splitlines()[:6]}")
    if back is None:
        raise RuntimeError("it did not go back")
    time.sleep(1)

    # What Windows does on its own when nothing restores: the watcher and the game both die. Reported, not judged: it decides how much the watcher must do.
    game = start_game([dll], "wait")
    wait_loaded(game)
    wait_until(at_wanted, CHANGE_LIMIT_S)
    time.sleep(1)
    for pid in watchers():
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=30)
    game.kill()
    game.wait(timeout=20)
    time.sleep(8)
    now = primary()
    print(f"INFO    watcher and game both killed: 8 s later the rate is {now['rate']} (was {start['rate']}, saved {now['saved']})", flush=True)
    if now["exact"] != start["exact"]:
        put_back(dll_dir, hdr, start)
        check.row(monitors() == start_state, "put back by hand after that")
    time.sleep(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--quiet", action="store_true")
    mode.add_argument("--full", action="store_true")
    ap.add_argument("--dll", default=str(ROOT / "dist" / "RefreshSwitch.dll"))
    ap.add_argument("--hdr", default=str(ROOT.parent / "HDR Switch" / "dist" / "HdrSwitch.dll"))
    ap.add_argument("--rate", type=int)
    ap.add_argument("--skip-closes", action="store_true", help="with --full: leave out the rounds of every way of closing")
    args = ap.parse_args()
    global OLD_LISTED
    OLD_LISTED = listed_now()
    dll_source = Path(args.dll)
    if not dll_source.is_file():   # rundll32 shows an error box on screen for a missing DLL
        print(f"REFUSED: {dll_source} is not there; build first")
        return 2
    start = primary()
    rates = offered_rates()
    print("monitors:", json.dumps(monitors()), "offers:", rates, flush=True)
    wanted = args.rate or next((rate for rate in (120, 100, 75, 60) if rate in rates and abs(rate - start["rate"]) > 1.01), None)
    if args.full:
        running = games_on_this_desktop()
        if running:
            print(f"REFUSED: {running} on this desktop; this check flips the monitor")
            return 2
        if start["hdr"]:
            print("REFUSED: HDR is on already")
            return 2
        if wanted is None:
            print("REFUSED: no other rate to switch to")
            return 2
    WORK.mkdir(parents=True, exist_ok=True)
    dll_dir = PLUGIN
    hdr_dir = WORK / "hdr"
    dll_dir.mkdir(exist_ok=True)
    shutil.copy2(dll_source, dll_dir / "RefreshSwitch.dll")
    hdr_source = Path(args.hdr)
    have_hdr = hdr_source.is_file()
    if have_hdr:
        hdr_dir.mkdir(exist_ok=True)
        shutil.copy2(hdr_source, hdr_dir / "HdrSwitch.dll")
    saved = {path: path.read_bytes() if path.exists() else None for path in (LOG, HDR_LOG)}
    state_before = monitors()
    check = Check()
    try:
        quiet_part(check, dll_dir, rates)
        if args.full:
            try:
                full_part(check, dll_dir, hdr_dir if have_hdr else None, wanted, args.skip_closes)
            except RuntimeError as problem:
                check.row(False, str(problem))
            if not have_hdr:
                print("NOT RUN: beside HDR Switch; no HdrSwitch.dll found")
    finally:
        if monitors() != state_before:
            time.sleep(6)
            if monitors() != state_before:
                put_back(dll_dir, hdr_dir / "HdrSwitch.dll" if have_hdr else None, next(m for m in state_before if m["primary"]))
        time.sleep(1)
        for path, data in saved.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
    print("PASSED" if not check.failed else f"FAILED: {check.failed}")
    print("monitors now:", json.dumps(monitors()))
    return 0 if not check.failed else 1


if __name__ == "__main__":
    sys.exit(main())
