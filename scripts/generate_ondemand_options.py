#!/usr/bin/env python3
"""
Convert the client fork's ondemand/OPTIONS.md into src/resources/ondemand_options.json.

    scripts/generate_ondemand_options.py ~/code-priv/onedrive-wt/engine/ondemand/OPTIONS.md

The JSON records the source file and the git commit it was read at, so the table can be
regenerated and compared instead of being edited by hand.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from ondemand_options import OPTIONS_JSON, parse_options_markdown  # noqa: E402


def source_commit(path):
    directory = os.path.dirname(os.path.abspath(path))
    try:
        commit = subprocess.run(["git", "-C", directory, "log", "-1", "--format=%H", "--", os.path.basename(path)], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", directory, "status", "--porcelain", "--", os.path.basename(path)], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return (commit or "uncommitted") + ("+modified" if dirty else "")


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(f"usage: {sys.argv[0]} OPTIONS.md [output.json]")
    source = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) == 3 else OPTIONS_JSON
    with open(source) as f:
        options = parse_options_markdown(f.read())
    if not options:
        sys.exit(f"{source}: no table with Option and Class columns found")
    data = {"source": "ondemand/OPTIONS.md", "source_commit": source_commit(source), "options": dict(sorted(options.items()))}
    with open(output, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"{output}: {len(options)} options from {source} at {data['source_commit']}")


if __name__ == "__main__":
    main()
