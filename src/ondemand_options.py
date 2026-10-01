"""
Which client config options matter in Files On-Demand mode, from the client fork's
ondemand/OPTIONS.md. The markdown is converted to resources/ondemand_options.json by
scripts/generate_ondemand_options.py (which records the source commit); this module only
reads the JSON, so the table is data, not code.
"""

import json
import os
import re

from global_config import DIR_PATH

OPTIONS_JSON = os.path.join(DIR_PATH, "resources", "ondemand_options.json")
CLASSES = ("relevant", "relevant-ondemand", "ignored", "refused", "risky", "unknown")
HIDDEN_CLASSES = ("ignored", "refused")

HEADER_NAMES = {
    "option": ("option", "key", "config option"),
    "class": ("class", "classification", "status", "category"),
    "resync": ("resync", "resync required", "needs resync"),
    "notes": ("notes", "note", "reason", "description", "comment"),
}


def _cells(line):
    line = line.strip()
    if not (line.startswith("|") and line.endswith("|")):
        return None
    return [cell.strip() for cell in line[1:-1].split("|")]


def _plain(text):
    return re.sub(r"[`*]", "", text).strip()


def parse_options_markdown(text):
    """Every markdown table with Option and Class columns -> {option: {class, resync, notes}}."""
    options = {}
    columns = None
    for line in text.splitlines():
        cells = _cells(line)
        if cells is None:
            columns = None
            continue
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells if cell):
            continue
        lowered = [_plain(cell).lower() for cell in cells]
        found = {field: next((i for i, cell in enumerate(lowered) if cell in names), None) for field, names in HEADER_NAMES.items()}
        if found["option"] is not None and found["class"] is not None:
            columns = found
            continue
        if columns is None:
            continue

        def cell(field):
            index = columns[field]
            return cells[index] if index is not None and index < len(cells) else ""

        # An option cell may list several keys ("`skip_file`, `skip_dir`").
        keys = [k for k in re.split(r"[,\s/]+", _plain(cell("option"))) if re.fullmatch(r"\w+", k)]
        option_class = _plain(cell("class")).lower()
        if option_class not in CLASSES:
            option_class = "unknown"
        resync = _plain(cell("resync")).lower() in ("yes", "y", "true", "x", "required")
        for key in keys:
            options[key] = {"class": option_class, "resync": resync, "notes": _plain(cell("notes"))}
    return options


def load_options_table(path=OPTIONS_JSON):
    """{option: {class, resync, notes}}, or {} when no table has been generated."""
    try:
        with open(path) as f:
            return json.load(f).get("options", {})
    except (OSError, ValueError):
        return {}
