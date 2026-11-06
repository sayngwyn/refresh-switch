"""Build Refresh Switch: dist/RefreshSwitch.dll and one zip per game, each with the DLL and its ini.

    python tools/build.py [--xmake PATH] [--vsdevcmd PATH] [--toolset 14.44]

Needs only the Visual Studio C++ build tools, xmake and Python's standard library; nothing is downloaded. xmake comes from --xmake, the XMAKE_EXE environment
variable or PATH. Visual Studio is found through vswhere unless --vsdevcmd names its VsDevCmd.bat.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
DIST = ROOT / "dist"
DLL = "RefreshSwitch.dll"
INI = "RefreshSwitch.ini"
EXPORTS = {"SKSEPlugin_Version", "SKSEPlugin_Query", "SKSEPlugin_Load", "F4SEPlugin_Version", "F4SEPlugin_Query", "F4SEPlugin_Load", "RefreshSwitchSpawn", "RefreshSwitchWatch"}
ALLOWED_IMPORTS = {"kernel32.dll", "user32.dll", "shell32.dll", "ole32.dll"}
STEP_TIMEOUT = 600


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def find_vsdevcmd(given: str | None) -> Path | None:
    if given:
        return Path(given)
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        return None
    found = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-find", r"Common7\Tools\VsDevCmd.bat"],
                           capture_output=True, text=True, timeout=60)
    lines = found.stdout.strip().splitlines()
    return Path(lines[0]) if lines else None


def developer_environment(vsdevcmd: Path, toolset: str | None) -> dict[str, str] | None:
    version = f" -vcvars_ver={toolset}" if toolset else ""
    done = subprocess.run(f'cmd /d /s /c ""{vsdevcmd}" -arch=x64 -host_arch=x64 -no_logo{version} && set"', capture_output=True, text=True, timeout=STEP_TIMEOUT)
    env = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line and not line.startswith("="))
    if done.returncode != 0 or not any(key.upper() == "PATH" for key in env):
        return None
    return env


def run(step: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(step, cwd=ROOT, env=env, capture_output=True, text=True, timeout=STEP_TIMEOUT)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--xmake", default=os.environ.get("XMAKE_EXE") or shutil.which("xmake"))
    ap.add_argument("--vsdevcmd")
    ap.add_argument("--toolset", help="MSVC toolset version, such as 14.44 (default: the newest installed)")
    args = ap.parse_args()
    if not args.xmake or not Path(args.xmake).is_file():
        return fail("xmake not found: pass --xmake, set XMAKE_EXE or put it on PATH")
    vsdevcmd = find_vsdevcmd(args.vsdevcmd)
    if not vsdevcmd or not vsdevcmd.is_file():
        return fail("Visual Studio's C++ build tools not found: install them or pass --vsdevcmd")
    version = re.search(r'^#define REFRESHSWITCH_VERSION "([^"]+)"', (ROOT / "src" / "version.h").read_text(encoding="utf-8"), re.M)
    if not version:
        return fail("REFRESHSWITCH_VERSION not found in src/version.h")
    env = developer_environment(vsdevcmd, args.toolset)
    if env is None:
        return fail("the Visual Studio developer setup failed")
    for key in ("MSYSTEM", "MINGW_PREFIX"):   # Git Bash's own settings make xmake pick the wrong compiler
        env.pop(key, None)
    env.update(XMAKE_GLOBALDIR=str(BUILD / "xmake-global"), XMAKE_CONFIGDIR=str(BUILD / "xmake-config"), XMAKE_TMPDIR=str(BUILD / "tmp"), XMAKE_STATS="n")
    env["CL"] = f'/d1trimfile:"{ROOT}\\\\"'   # the build machine's folders are not compiled into the DLL
    out = BUILD / "out"
    if BUILD.exists():
        shutil.rmtree(BUILD)
    for step in ([args.xmake, "f", "-c", "-y", "-p", "windows", "-a", "x64", "-m", "release", "-o", str(out)], [args.xmake, "-r", "-y"]):
        ran = run(step, env)
        if ran.returncode != 0:
            return fail(f"xmake failed (exit code {ran.returncode}):\n{ran.stdout[-4000:]}\n{ran.stderr[-2000:]}")
    built = sorted(out.rglob(DLL))
    if len(built) != 1:
        return fail(f"expected one {DLL} under {out}, found {len(built)}")
    dumpbin = shutil.which("dumpbin", path=next(value for key, value in env.items() if key.upper() == "PATH"))   # Windows looks in this process's own PATH, not env's
    if not dumpbin:
        return fail("dumpbin not found in the Visual Studio developer setup")
    exports = run([dumpbin, "/nologo", "/exports", str(built[0])], env).stdout
    dependents = run([dumpbin, "/nologo", "/dependents", str(built[0])], env).stdout
    missing = sorted(name for name in EXPORTS if not re.search(rf"\s{name}\s*$", exports, re.M))
    imports = {name.lower() for name in re.findall(r"^\s+(\S+\.dll)\s*$", dependents, re.M)} - {DLL.lower()}
    if missing or imports - ALLOWED_IMPORTS:
        return fail(f"the built DLL is not right: missing exports {missing}, imports {sorted(imports)} (allowed: {sorted(ALLOWED_IMPORTS)})")
    data = built[0].read_bytes()
    leaks = sorted({m.decode("ascii", "replace") for m in re.findall(rb"[A-Za-z]:[\\/][\x20-\x7e]{3,}", data)})
    if leaks:
        return fail(f"the built DLL names folders on this machine: {leaks}")
    DIST.mkdir(exist_ok=True)
    shutil.copy2(built[0], DIST / DLL)
    print(f"built {version.group(1)}: {DIST / DLL}")
    print(f"sha256 {hashlib.sha256(data).hexdigest()}")
    # One download per Nexus page, each with only its own game's folder. The third holds both, for lists that keep one copy.
    for name, loaders in ((f"Refresh-Switch-Skyrim-{version.group(1)}.zip", ("SKSE",)), (f"Refresh-Switch-Fallout-4-{version.group(1)}.zip", ("F4SE",)), (f"Refresh-Switch-{version.group(1)}.zip", ("SKSE", "F4SE"))):
        with zipfile.ZipFile(DIST / name, "w", zipfile.ZIP_DEFLATED) as archive:
            for loader in loaders:
                archive.write(DIST / DLL, f"{loader}/Plugins/{DLL}")
                archive.write(ROOT / "src" / INI, f"{loader}/Plugins/{INI}")
            archive.write(ROOT / "LICENSE", "LICENSE")
        print(f"package {DIST / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
