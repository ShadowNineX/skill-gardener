#!/usr/bin/env python3
"""Read-only structural audit of a skill directory or a bounded skill collection."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_FILE_BYTES = 1024 * 1024
MAX_YAML_DEPTH = 32
MAX_ENTRIES = 10000
DEFAULT_DEPTH = 6
MAX_DEPTH = 64
IGNORED_DIRS = frozenset({".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__"})


if yaml is not None:
    class FrontmatterLoader(yaml.SafeLoader):
        """Safe YAML with unique string keys and bounded, alias-free nesting."""

        def __init__(self, stream):
            super().__init__(stream)
            self.nesting = 0

        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                raise yaml.YAMLError("YAML aliases are not supported")
            self.nesting += 1
            try:
                if self.nesting > MAX_YAML_DEPTH:
                    raise yaml.YAMLError("YAML nesting limit exceeded")
                return super().compose_node(parent, index)
            finally:
                self.nesting -= 1

        def construct_mapping(self, node, deep=False):
            keys = set()
            for key_node, _ in node.value:
                if key_node.tag != "tag:yaml.org,2002:str":
                    raise yaml.YAMLError("mapping keys must be strings; YAML merge keys are unsupported")
                key = self.construct_object(key_node, deep=deep)
                if key in keys:
                    raise yaml.YAMLError("duplicate mapping key")
                keys.add(key)
            return super().construct_mapping(node, deep=deep)


def read_skill(path: Path) -> str:
    """Read a bounded regular file, refusing a symlink at the file itself.

    Discovery also refuses symlink directories. This is a static audit, not a
    sandbox for trees being concurrently changed by an adversarial process.
    """
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("SKILL.md must be a regular file, not a symlink or special file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("SKILL.md must be a regular file")
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("SKILL.md changed during inspection; retry on a stable tree")
        if opened.st_size > MAX_FILE_BYTES:
            raise ValueError("SKILL.md exceeds the 1 MiB audit limit")
        raw = source.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("SKILL.md exceeds the 1 MiB audit limit")
    return raw.decode("utf-8").replace("\r\n", "\n")


def parse_frontmatter(path: Path) -> tuple[dict, str | None]:
    if yaml is None:
        return {}, "PyYAML is required; install the skill's requirements.txt in a virtual environment"
    try:
        text = read_skill(path)
    except (OSError, UnicodeError, ValueError) as exc:
        # Do not echo file contents or exception text containing arbitrary paths.
        if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError):
            return {}, str(exc)
        return {}, "cannot read SKILL.md as a regular UTF-8 file"

    lines = text.split("\n")
    if lines[0] != "---":
        return {}, "frontmatter must start at byte zero with '---'"
    end = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
    if end is None:
        return {}, "missing closing frontmatter delimiter"
    if not "\n".join(lines[end + 1:]).strip():
        return {}, "skill body is empty"
    try:
        fields = yaml.load("\n".join(lines[1:end]) + "\n", Loader=FrontmatterLoader)
    except (yaml.YAMLError, ValueError, OverflowError, RecursionError) as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" near line {mark.line + 2}, column {mark.column + 1}" if mark else ""
        return {}, "invalid or unsupported YAML frontmatter" + location
    if not isinstance(fields, dict):
        return {}, "frontmatter must be a mapping"
    return fields, None


def invalid_root(root: Path) -> bool:
    try:
        return not root.is_dir() or any(part.is_symlink() for part in (root, *root.parents))
    except OSError:
        return True


def audit(root: Path, *, single: bool = False, max_depth: int = DEFAULT_DEPTH) -> dict:
    root = root.absolute()
    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    files: list[Path] = []
    names: dict[str, Path] = {}
    result = {
        "root": str(root),
        "scope": "skill" if single else "collection",
        "skills": 0,
        "passed": False,
        "issues": issues,
        "warnings": warnings,
    }

    def issue(path: Path, message: str) -> None:
        issues.append({"file": str(path), "issue": message})

    if invalid_root(root):
        issue(root, "root must be an existing directory without symlink path components")
        return result
    if not 1 <= max_depth <= MAX_DEPTH:
        issue(root, "max_depth must be between 1 and 64")
        return result
    root = root.resolve()
    result["root"] = str(root)

    visited = 0

    def discover(directory: Path, depth: int) -> None:
        nonlocal visited
        skill_file = directory / "SKILL.md"
        # lexists also detects broken symlinks. Stop below a skill boundary.
        if os.path.lexists(skill_file):
            files.append(skill_file)
            return
        try:
            with os.scandir(directory) as entries:
                children = []
                for entry in entries:
                    visited += 1
                    if visited > MAX_ENTRIES:
                        raise ValueError
                    children.append(entry)
            for child in sorted(children, key=lambda entry: entry.name):
                if child.name in IGNORED_DIRS:
                    continue
                child_path = directory / child.name
                if child.is_symlink():
                    issue(child_path, "symlink skipped; collection is not fully audited")
                elif child.is_dir(follow_symlinks=False):
                    if depth >= max_depth:
                        issue(child_path, "depth limit reached; collection is not fully audited")
                    else:
                        discover(child_path, depth + 1)
        except OSError:
            issue(directory, "cannot enumerate directory; collection is not fully audited")

    if single:
        files.append(root / "SKILL.md")
    else:
        try:
            discover(root, 0)
        except ValueError:
            issue(root, "entry limit reached; collection is not fully audited")
    if not files:
        issue(root, "no SKILL.md files found in the requested scope")

    for path in files:
        fields, error = parse_frontmatter(path)
        if error:
            issue(path, error)
            continue
        name = fields.get("name")
        description = fields.get("description")
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or not NAME_RE.fullmatch(name):
            issue(path, "name must be 1–64 lowercase letters/digits with single internal hyphens")
        else:
            if name in names:
                issue(path, f"duplicate skill name also used by {names[name]}")
            names[name] = path
            if path.parent.name != name:
                warnings.append({"file": str(path), "warning": "directory name differs from skill name; check target runtime compatibility"})
        if not isinstance(description, str) or not description.strip():
            issue(path, "description must be a non-empty string")
        elif len(description) > 1024:
            issue(path, "description exceeds 1024 characters")
        for field in ("compatibility", "license", "allowed-tools"):
            if field in fields and (not isinstance(fields[field], str) or not fields[field].strip()):
                issue(path, f"{field} must be a non-empty string when provided")
        if isinstance(fields.get("compatibility"), str) and len(fields["compatibility"]) > 500:
            issue(path, "compatibility exceeds 500 characters")
        # OpenClaw accepts nested metadata; it is not limited to flat strings.
        if "metadata" in fields and fields["metadata"] is not None and not isinstance(fields["metadata"], dict):
            issue(path, "metadata must be a mapping when provided")

    result["skills"] = len(files)
    result["passed"] = not issues
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, help="collection root (default: ./skills)")
    parser.add_argument("--skill", type=Path, help="validate one skill directory without scanning its siblings")
    parser.add_argument("--max-depth", type=int, default=DEFAULT_DEPTH, help="collection directory depth (default: 6)")
    args = parser.parse_args()
    if args.skill is not None and args.root is not None:
        parser.error("use a collection root or --skill, not both")
    if not 1 <= args.max_depth <= MAX_DEPTH:
        parser.error("--max-depth must be between 1 and 64")
    if yaml is None:
        print(json.dumps({"passed": False, "issues": [{"issue": "PyYAML is required; install requirements.txt in a virtual environment"}]}))
        return 2
    root = args.skill if args.skill is not None else (args.root or Path("skills"))
    result = audit(root, single=args.skill is not None, max_depth=args.max_depth)
    print(json.dumps(result, indent=2))
    if invalid_root(root.absolute()):
        return 2
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
