#!/usr/bin/env python3
"""Read-only config hygiene audit; reports names and counts, never values."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path


KEY_PATTERN = re.compile(
    r"(?im)\b(?P<name>(?:[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|TICKET|CREDENTIAL)|authorization|prompt|message))\b\s*(?:=|:|$)"
)
SKIP_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".next", "data", "artifacts"}


def iter_files(root: Path):
    for path in root.rglob("*"):
        relative_parts = path.relative_to(root).parts
        if not path.is_file() or SKIP_PARTS.intersection(relative_parts):
            continue
        if path.suffix.lower() in {".pyc", ".sqlite", ".db", ".log"}:
            continue
        yield path


def audit(root: Path) -> dict[str, object]:
    categories: Counter[str] = Counter()
    findings: dict[tuple[str, str], int] = {}
    scanned_paths = 0
    for path in iter_files(root):
        scanned_paths += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in KEY_PATTERN.finditer(text):
            name = match.group("name")
            category = "credential_name" if name.isupper() else "sensitive_field_name"
            key = (str(path.relative_to(root)), name)
            findings[key] = findings.get(key, 0) + 1
            categories[category] += 1
    return {
        "scanned_paths": scanned_paths,
        "findings": [
            {"path": path, "name": name, "category": "credential_name" if name.isupper() else "sensitive_field_name", "count": count}
            for (path, name), count in sorted(findings.items())
        ],
        "categories": dict(sorted(categories.items())),
    }


def main(argv: list[str] | None = None) -> int:
    root = Path((argv or sys.argv[1:])[0] if (argv or sys.argv[1:]) else ".").resolve()
    print(json.dumps(audit(root), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
