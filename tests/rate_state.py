"""Print every active monitor: its refresh rate as it really runs, the rate saved in Windows' settings, and HDR. Read-only.

    python tests/rate_state.py
"""

from __future__ import annotations

import ctypes
import json
from ctypes import wintypes

QDC_ONLY_ACTIVE_PATHS = 2
QDC_DATABASE_CURRENT = 4
SOURCE_MODE, TARGET_MODE = 1, 2
INVALID_IDX = 0xFFFFFFFF


class Luid(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.LONG)]


class Source(ctypes.Structure):
    _fields_ = [("adapter", Luid), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32), ("statusFlags", ctypes.c_uint32)]


class Target(ctypes.Structure):
    _fields_ = [("adapter", Luid), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32),
                ("scaling", ctypes.c_uint32), ("refreshNum", ctypes.c_uint32), ("refreshDen", ctypes.c_uint32), ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", ctypes.c_int), ("statusFlags", ctypes.c_uint32)]


class Path(ctypes.Structure):
    _fields_ = [("source", Source), ("target", Target), ("flags", ctypes.c_uint32)]


class Mode(ctypes.Structure):
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapter", Luid), ("raw", ctypes.c_byte * 48)]


class Header(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapter", Luid), ("id", ctypes.c_uint32)]


class ColorInfo2(ctypes.Structure):
    _fields_ = [("header", Header), ("value", ctypes.c_uint32), ("encoding", ctypes.c_uint32), ("bits", ctypes.c_uint32), ("mode", ctypes.c_uint32)]


class ColorInfo(ctypes.Structure):
    _fields_ = [("header", Header), ("value", ctypes.c_uint32), ("encoding", ctypes.c_uint32), ("bits", ctypes.c_uint32)]


def _query(flags: int) -> tuple:
    user32 = ctypes.WinDLL("user32")
    path_count, mode_count = ctypes.c_uint32(), ctypes.c_uint32()
    if user32.GetDisplayConfigBufferSizes(flags, ctypes.byref(path_count), ctypes.byref(mode_count)):
        raise OSError("GetDisplayConfigBufferSizes failed")
    paths, modes = (Path * path_count.value)(), (Mode * mode_count.value)()
    topology = ctypes.c_uint32()
    answer = user32.QueryDisplayConfig(flags, ctypes.byref(path_count), paths, ctypes.byref(mode_count), modes, ctypes.byref(topology) if flags == QDC_DATABASE_CURRENT else None)
    if answer:
        raise OSError(f"QueryDisplayConfig failed: {answer}")
    return paths[: path_count.value], modes[: mode_count.value]


def _rate(path: Path, modes) -> tuple[int, int]:
    index = path.target.modeInfoIdx
    if index != INVALID_IDX and index < len(modes) and modes[index].infoType == TARGET_MODE:
        # DISPLAYCONFIG_VIDEO_SIGNAL_INFO: pixel rate, horizontal rate, then the vertical rate
        numerator, denominator = ctypes.c_uint32.from_buffer(modes[index].raw, 16).value, ctypes.c_uint32.from_buffer(modes[index].raw, 20).value
        if denominator:
            return numerator, denominator
    return path.target.refreshNum, path.target.refreshDen


def monitors() -> list[dict]:
    user32 = ctypes.WinDLL("user32")
    paths, modes = _query(QDC_ONLY_ACTIVE_PATHS)
    saved_paths, saved_modes = _query(QDC_DATABASE_CURRENT)
    found = []
    for path in paths:
        index = path.source.modeInfoIdx
        primary = False
        if index != INVALID_IDX and index < len(modes) and modes[index].infoType == SOURCE_MODE:
            x, y = ctypes.c_int32.from_buffer(modes[index].raw, 12).value, ctypes.c_int32.from_buffer(modes[index].raw, 16).value
            primary = x == 0 and y == 0
        numerator, denominator = _rate(path, modes)
        saved = None
        for other in saved_paths:
            if other.target.id == path.target.id and other.target.adapter.low == path.target.adapter.low and other.target.adapter.high == path.target.adapter.high:
                saved_num, saved_den = _rate(other, saved_modes)
                saved = round(saved_num / saved_den, 3) if saved_den else None
        row = {"target": path.target.id, "adapter": [path.target.adapter.low, path.target.adapter.high], "primary": primary, "rate": round(numerator / denominator, 3) if denominator else 0,
               "exact": [numerator, denominator], "saved": saved, "hdr": None}
        modern = ColorInfo2()
        modern.header = Header(15, ctypes.sizeof(ColorInfo2), path.target.adapter, path.target.id)
        if user32.DisplayConfigGetDeviceInfo(ctypes.byref(modern.header)) == 0:
            row["hdr"] = bool(modern.value & 1 << 5)
        else:
            older = ColorInfo()
            older.header = Header(9, ctypes.sizeof(ColorInfo), path.target.adapter, path.target.id)
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(older.header)) == 0:
                row["hdr"] = bool(older.value & 2)
        found.append(row)
    return found


def primary() -> dict:
    return next(m for m in monitors() if m["primary"])


if __name__ == "__main__":
    print(json.dumps(monitors(), indent=1))
