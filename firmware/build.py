"""Build with ESP-IDF 6.1 and a real Python 3.15.2 environment.

2026-11-14: migrate tool discovery and isolate configuration from IDF 5 builds.
"""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def checked(arguments, **kwargs):
    return subprocess.run([str(value) for value in arguments], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="build", choices=(
        "build", "flash", "monitor", "menuconfig", "size", "reconfigure"
    ))
    parser.add_argument("--port", default="")
    parser.add_argument("--idf-path", type=Path, default=Path("C:/esp/v6.1/esp-idf"))
    parser.add_argument("--tools-path", type=Path, default=Path("C:/Espressif"))
    parser.add_argument("--python", type=Path, default=Path("C:/Python315/python.exe"))
    parser.add_argument("--python-environment", type=Path)
    parser.add_argument("--build-directory", type=Path, default=Path("build-idf61"))
    parser.add_argument("--install-python-environment", action="store_true")
    args = parser.parse_args()
    idf = args.idf_path.resolve()
    tools = args.tools_path.resolve()
    python_environment = (args.python_environment or tools / "tools/python_env/idf6.1_py3.15_env").resolve()
    environment_python = python_environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    version_file = (idf / "tools/cmake/version.cmake").read_text(encoding="utf-8")
    if not all(re.search(rf"set\(IDF_VERSION_{key}\s+{value}\)", version_file) for key, value in (("MAJOR", 6), ("MINOR", 1))):
        raise RuntimeError("This project requires ESP-IDF 6.1")

    def check_python(executable):
        result = checked([executable, "-c", "import platform; print(platform.python_version())"], capture_output=True, text=True)
        if result.stdout.strip() != "3.15.2":
            raise RuntimeError(f"Expected Python 3.15.2, got {result.stdout.strip()} from {executable}")

    check_python(args.python)
    env = os.environ.copy()
    env.update(IDF_PATH=str(idf), IDF_TOOLS_PATH=str(tools),
               IDF_PYTHON_ENV_PATH=str(python_environment), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if not shutil.which("git", path=env.get("PATH")) and os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\GitForWindows") as key:
                    git_path = Path(winreg.QueryValueEx(key, "InstallPath")[0]) / "cmd"
                if (git_path / "git.exe").is_file():
                    env["PATH"] = str(git_path) + os.pathsep + env.get("PATH", "")
                    break
            except OSError:
                continue
    if not shutil.which("git", path=env.get("PATH")):
        raise RuntimeError("Install Git and add it to PATH")
    if args.install_python_environment:
        python_environment.mkdir(parents=True, exist_ok=True)
        checked([args.python, idf / "tools/idf_tools.py", "install-python-env"], env=env)
    if not environment_python.is_file():
        raise RuntimeError("Run with --install-python-environment first")
    check_python(environment_python)
    exports = checked([environment_python, idf / "tools/idf_tools.py", "export", "--format", "key-value"], env=env, capture_output=True, text=True)
    if exports.stderr:
        print(exports.stderr, file=sys.stderr, end="")
    for line in exports.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            if key == "PATH":
                value = value.replace("%PATH%", env.get("PATH", "")).replace("$PATH", env.get("PATH", ""))
            env[key] = value
    env["PATH"] = str(environment_python.parent) + os.pathsep + env.get("PATH", "")
    build = (ROOT / args.build_directory).resolve()
    command = [environment_python, idf / "tools/idf.py", "-B", build, "-D", f"SDKCONFIG={build.as_posix()}/sdkconfig"]
    if args.port:
        command.extend(("-p", args.port))
    print(f"ESP-IDF 6.1 / Python 3.15.2 -> {build}", flush=True)
    checked([*command, args.action], env=env, cwd=ROOT)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        raise SystemExit(1)
