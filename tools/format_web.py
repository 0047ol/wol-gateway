"""Format website HTML/Jinja, JavaScript, CSS and project Python consistently."""

import argparse
import subprocess
import sys
from pathlib import Path

import cssbeautifier
import jsbeautifier

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
options = jsbeautifier.default_options()
options.indent_size = 2
options.end_with_newline = True
css_options = cssbeautifier.default_options()
css_options.indent_size = 2
css_options.end_with_newline = True
changed = []
for path in sorted((root / "app/static").iterdir()):
    if path.suffix not in {".js", ".css"}:
        continue
    before = path.read_text(encoding="utf-8")
    after = (
        jsbeautifier.beautify(before, options)
        if path.suffix == ".js"
        else cssbeautifier.beautify(before, css_options)
    )
    if before != after:
        changed.append(str(path.relative_to(root)))
        if not args.check:
            path.write_text(after, encoding="utf-8")
if args.check and changed:
    print("Formatting needed: " + ", ".join(changed))
    sys.exit(1)
command = [
    sys.executable,
    "-m",
    "djlint",
    "app/templates",
    "--profile",
    "jinja",
    "--indent",
    "2",
    "--format-js",
    "--format-css",
    "--indent-js",
    "2",
    "--indent-css",
    "2",
    "--preserve-blank-lines",
    "--max-line-length",
    "100",
    "--check" if args.check else "--reformat",
    "--quiet",
]
result = subprocess.run(command, cwd=root)
if result.returncode and (args.check or result.returncode != 1):
    sys.exit(result.returncode)
if not args.check:
    subprocess.run(
        [part if part != "--reformat" else "--check" for part in command],
        cwd=root,
        check=True,
    )
subprocess.run(
    [
        sys.executable,
        "-m",
        "black",
        *(["--check"] if args.check else []),
        "app",
        "tests",
        "tools",
        "deploy",
        "run.py",
    ],
    cwd=root,
    check=True,
)
