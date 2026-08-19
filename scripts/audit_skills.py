#!/usr/bin/env python3
"""Audit local OpenClaw AgentSkills without third-party packages."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FIELD_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_frontmatter(path: Path) -> tuple[dict[str, str], str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {}, f"cannot read UTF-8 text: {exc}"

    if not text.startswith("---\n"):
        return {}, "frontmatter must start at byte zero with '---'"

    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, "missing closing frontmatter delimiter"

    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            continue
        match = FIELD_RE.match(line)
        if not match:
            return {}, f"malformed top-level frontmatter line: {line!r}"
        fields[match.group(1)] = unquote(match.group(2))

    if not text[end + 5 :].strip():
        return fields, "skill body is empty"
    return fields, None


def audit(root: Path) -> dict[str, object]:
    files = sorted(root.glob("*/SKILL.md"))
    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    names: dict[str, Path] = {}

    if not files:
        issues.append(
            {
                "file": str(root),
                "issue": "no skills found; expected at least one */SKILL.md",
            }
        )

    for path in files:
        fields, error = parse_frontmatter(path)
        rel = str(path)
        if error:
            issues.append({"file": rel, "issue": error})
            continue

        name = fields.get("name", "").strip()
        description = fields.get("description", "").strip()

        if not name:
            issues.append({"file": rel, "issue": "missing non-empty name"})
        elif not NAME_RE.fullmatch(name):
            issues.append({"file": rel, "issue": "name must be lowercase hyphen-case"})
        else:
            if name in names:
                issues.append(
                    {
                        "file": rel,
                        "issue": f"duplicate skill name also used by {names[name]}",
                    }
                )
            names[name] = path
            if path.parent.name != name:
                warnings.append(
                    {
                        "file": rel,
                        "warning": f"legacy directory '{path.parent.name}' differs from name '{name}'",
                    }
                )

        if not description:
            issues.append({"file": rel, "issue": "missing non-empty description"})
        elif len(description) > 1024:
            issues.append({"file": rel, "issue": "description exceeds 1024 characters"})

    return {
        "root": str(root),
        "skills": len(files),
        "passed": not issues,
        "issues": issues,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default="skills", type=Path)
    args = parser.parse_args()

    if not args.root.is_dir():
        print(json.dumps({"passed": False, "issues": [{"issue": "root is not a directory"}]}))
        return 2

    result = audit(args.root)
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
