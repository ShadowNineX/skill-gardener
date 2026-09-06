"""Behavioral regressions for the read-only audit. No network or live skills."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_skills.py"
SPEC = importlib.util.spec_from_file_location("audit_skills", SCRIPT)
audit_skills = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_skills)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gardener-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "skills"
        self.root.mkdir()

    def skill(self, directory="demo", frontmatter='name: demo\ndescription: "Useful workflow"', body="# Demo\n"):
        path = self.root / directory / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
        return path

    def result(self):
        return audit_skills.audit(self.root)

    def cli(self, *args, isolated=False):
        command = [sys.executable]
        if isolated:
            command.append("-S")
        return subprocess.run(command + [str(SCRIPT), *map(str, args)], capture_output=True, text=True, timeout=10)

    def test_valid_skill_and_read_only_behavior(self):
        path = self.skill()
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertTrue(self.result()["passed"])
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_valid_yaml_variations(self):
        for fields in [
            'name: demo # comment\ndescription: useful',
            '"name": demo\ndescription: "Useful: workflow"',
            "name: 'demo'\ndescription: 'It''s useful'",
            'name: demo\ndescription: |\n  First line\n  Second line',
            'name: demo\ndescription: >-\n  Folded\n  description',
            'name: demo\ndescription: "Escaped \\u0061"',
            'name: demo\ndescription: useful\nmetadata:\n  openclaw:\n    tags: [skills, learning]',
        ]:
            with self.subTest(fields=fields):
                self.skill(frontmatter=fields)
                self.assertTrue(self.result()["passed"], self.result())

    def test_decoded_description_is_preserved(self):
        path = self.skill(frontmatter='name: demo\ndescription: |\n  First line\n  Second line')
        fields, error = audit_skills.parse_frontmatter(path)
        self.assertIsNone(error)
        self.assertEqual(fields["description"], "First line\nSecond line\n")

    def test_invalid_description_types_and_empty_values(self):
        for description in ['', 'null', '~', 'false', '123', '[]', '{}', '"" # empty', '|', '>']:
            with self.subTest(description=description):
                self.skill(frontmatter=f"name: demo\ndescription: {description}")
                self.assertFalse(self.result()["passed"])

    def test_missing_required_fields(self):
        for fields in ['name: demo', 'description: useful', '']:
            with self.subTest(fields=fields):
                self.skill(frontmatter=fields)
                self.assertFalse(self.result()["passed"])

    def test_malformed_yaml(self):
        for fields in [
            'name: demo\ndescription: "unfinished',
            'name: demo\ndescription: useful\nmetadata:\n  nested: [',
            'name: demo\ndescription: useful\nmetadata:\n  first: okay\n second: wrong',
            '- demo\n- useful',
        ]:
            with self.subTest(fields=fields):
                self.skill(frontmatter=fields)
                self.assertFalse(self.result()["passed"])

    def test_duplicate_mapping_keys_at_any_depth(self):
        for fields in [
            'name: first\nname: demo\ndescription: useful',
            'name: demo\ndescription: useful\nmetadata:\n  key: first\n  key: second',
        ]:
            with self.subTest(fields=fields):
                self.skill(frontmatter=fields)
                self.assertFalse(self.result()["passed"])

    def test_aliases_merge_keys_and_unsafe_tags_rejected(self):
        for extra in [
            'metadata: &data {key: value}\nother: *data',
            'metadata: {<<: {key: value}}',
            'metadata: !!python/object/apply:os.system ["echo forbidden"]',
            'metadata: &data [*data]',
            'metadata: {1: value}',
        ]:
            with self.subTest(extra=extra):
                self.skill(frontmatter=f"name: demo\ndescription: useful\n{extra}")
                self.assertFalse(self.result()["passed"])

    def test_yaml_nesting_limit(self):
        self.skill(frontmatter='name: demo\ndescription: useful\nmetadata: ' + '[' * 40 + '0' + ']' * 40)
        self.assertFalse(self.result()["passed"])

    def test_scalar_constructor_errors_have_structured_reports(self):
        for value in ['9999-99-99', '9' * 5000]:
            with self.subTest(value_length=len(value)):
                self.skill(frontmatter=f'name: demo\ndescription: {value}')
                run = self.cli(self.root, isolated=True)
                self.assertEqual(run.returncode, 1)
                self.assertFalse(json.loads(run.stdout)["passed"])
                self.assertNotIn('Traceback', run.stderr)

    def test_name_format_and_length(self):
        for name in ['Demo', '-demo', 'demo-', 'demo--skill', 'demo_skill', '" demo "', '[]', 'null', 'a' * 65]:
            with self.subTest(name=name):
                self.skill(frontmatter=f"name: {name}\ndescription: useful")
                self.assertFalse(self.result()["passed"])
        self.skill(frontmatter='name: ' + 'a' * 64 + '\ndescription: useful')
        self.assertTrue(self.result()["passed"])

    def test_description_decoded_length_limit(self):
        for value in ['"' + 'a' * 1025 + '"', '|-\n  ' + 'a' * 1025, '"' + '\\u0061' * 1025 + '"']:
            with self.subTest(length=len(value)):
                self.skill(frontmatter=f'name: demo\ndescription: {value}')
                self.assertFalse(self.result()["passed"])
        self.skill(frontmatter='name: demo\ndescription: "' + 'a' * 1024 + '"')
        self.assertTrue(self.result()["passed"])

    def test_optional_fields(self):
        for extra in ['compatibility: []', 'compatibility: ' + 'a' * 501, 'license: false', 'allowed-tools: []', 'allowed-tools: [Read, false]', 'metadata: []']:
            with self.subTest(extra=extra):
                self.skill(frontmatter=f'name: demo\ndescription: useful\n{extra}')
                self.assertFalse(self.result()["passed"])
        self.skill(frontmatter='name: demo\ndescription: useful\nallowed-tools:\n  - Read\n  - Exec')
        self.assertTrue(self.result()["passed"])

    def test_empty_body_and_missing_delimiters(self):
        for raw in ['---\nname: demo\ndescription: useful\n---', 'name: demo\n', '\ufeff---\nname: demo\n---\n# Body', '---\nname: demo\n']:
            with self.subTest(raw=raw):
                path = self.skill()
                path.write_text(raw, encoding='utf-8')
                self.assertFalse(self.result()["passed"])

    def test_crlf_and_body_without_final_newline(self):
        path = self.skill()
        path.write_bytes(b'---\r\nname: demo\r\ndescription: useful\r\n---\r\n# Body')
        self.assertTrue(self.result()["passed"])

    def test_non_utf8_file(self):
        path = self.skill()
        path.write_bytes(b'\xff')
        self.assertFalse(self.result()["passed"])

    def test_large_file(self):
        self.skill(body='a' * (audit_skills.MAX_FILE_BYTES + 1))
        self.assertFalse(self.result()["passed"])

    def test_errors_do_not_echo_source(self):
        self.skill(frontmatter='name: demo\ndescription: useful\nDUMMY_PRIVATE_VALUE')
        output = json.dumps(self.result())
        self.assertNotIn('DUMMY_PRIVATE_VALUE', output)
        self.assertFalse(self.result()["passed"])

    def test_grouped_skills_and_duplicates(self):
        self.skill('group/demo')
        self.assertTrue(self.result()["passed"])
        self.skill('other/demo')
        self.assertFalse(self.result()["passed"])
        self.assertEqual(self.result()["skills"], 2)

    def test_malformed_grouped_skill_not_silently_skipped(self):
        self.skill()
        self.skill('group/broken', frontmatter='not YAML')
        self.assertFalse(self.result()["passed"])
        self.assertEqual(self.result()["skills"], 2)

    def test_directory_name_mismatch_warns(self):
        self.skill('legacy')
        result = self.result()
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["warnings"]), 1)

    def test_discovery_stops_below_skill_even_if_invalid(self):
        for fm in ['name: demo\ndescription: useful', 'not YAML']:
            with self.subTest(fm=fm):
                self.skill(frontmatter=fm)
                self.skill('demo/assets/example', frontmatter='not a skill')
                self.assertEqual(self.result()["skills"], 1)

    def test_single_skill_does_not_scan_siblings(self):
        path = self.skill()
        self.skill('broken', frontmatter='invalid')
        self.assertTrue(audit_skills.audit(path.parent, single=True)["passed"])
        self.assertFalse(self.result()["passed"])

    def symlink(self, link, target, is_dir=False):
        try:
            link.symlink_to(target, target_is_directory=is_dir)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')

    def test_file_symlink_not_read(self):
        outside = Path(self.temp.name) / 'outside.md'
        outside.write_text('DUMMY_PRIVATE_VALUE')
        folder = self.root / 'linked'
        folder.mkdir()
        self.symlink(folder / 'SKILL.md', outside)
        with patch.object(audit_skills.os, 'open', side_effect=AssertionError('must not open symlink')):
            result = self.result()
        self.assertFalse(result["passed"])
        self.assertNotIn('DUMMY_PRIVATE_VALUE', json.dumps(result))

    def test_directory_and_broken_symlinks_not_followed(self):
        self.skill()
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        self.symlink(self.root / 'linked', outside, True)
        self.symlink(self.root / 'broken', outside / 'absent')
        result = self.result()
        self.assertFalse(result["passed"])
        self.assertEqual(result["skills"], 1)
        self.assertEqual(len(result["issues"]), 2)

    def test_symlink_root_rejected(self):
        self.skill()
        linked = Path(self.temp.name) / 'linked'
        self.symlink(linked, self.root, True)
        self.assertFalse(audit_skills.audit(linked)["passed"])

    def test_symlink_parent_in_explicit_skill_path_rejected(self):
        self.skill()
        linked = Path(self.temp.name) / 'linked'
        self.symlink(linked, self.root, True)
        result = self.cli('--skill', linked / 'demo')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["passed"])

    def test_dot_root_uses_actual_directory_name(self):
        path = self.skill()
        result = subprocess.run([sys.executable, str(SCRIPT), '--skill', '.'], cwd=path.parent, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["warnings"], [])

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'requires FIFO support')
    def test_fifo_not_opened(self):
        path = self.skill()
        path.unlink()
        os.mkfifo(path)
        result = self.cli(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["passed"])

    def test_limits_never_report_clean_coverage(self):
        self.skill()
        self.skill('group/nested/demo')
        self.assertFalse(audit_skills.audit(self.root, max_depth=1)["passed"])
        with patch.object(audit_skills, 'MAX_ENTRIES', 1):
            self.assertFalse(self.result()["passed"])

    def test_unreadable_directory_is_reported(self):
        with patch.object(audit_skills.os, 'scandir', side_effect=PermissionError):
            self.assertFalse(self.result()["passed"])

    def test_ignored_dependency_directory(self):
        self.skill()
        self.skill('node_modules/fixture', frontmatter='invalid')
        result = self.result()
        self.assertTrue(result["passed"])
        self.assertEqual(result["skills"], 1)

    def test_cli_exit_codes_and_json(self):
        path = self.skill()
        for args, expected in [([self.root], 0), (['--skill', path.parent], 0), ([self.root / 'missing'], 2)]:
            with self.subTest(args=args):
                run = self.cli(*args)
                self.assertEqual(run.returncode, expected)
                self.assertEqual(json.loads(run.stdout)["passed"], expected == 0)
        path.write_text('invalid')
        run = self.cli(self.root)
        self.assertEqual(run.returncode, 1)
        self.assertFalse(json.loads(run.stdout)["passed"])

    def test_cli_invalid_arguments(self):
        for args in [['--max-depth', '0'], ['--max-depth', '1000'], [self.root, '--skill', self.root]]:
            with self.subTest(args=args):
                self.assertEqual(self.cli(*args).returncode, 2)

    def test_builtin_parser_works_without_yaml_dependency(self):
        for fields in [
            'name: demo\ndescription: "Useful: workflow"',
            'name: demo\ndescription: |\n  First line\n  Second line',
            'name: demo\ndescription: useful\nmetadata:\n  openclaw:\n    tags: [skills, learning]',
            'name: demo\ndescription: useful\nallowed-tools:\n  - Read\n  - Exec',
        ]:
            with self.subTest(fields=fields):
                self.skill(frontmatter=fields)
                run = self.cli(self.root, isolated=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertTrue(json.loads(run.stdout)["passed"])

    def test_builtin_parser_rejects_unsafe_yaml(self):
        for fields in [
            'name: demo\ndescription: useful\nmetadata: &data {key: value}\nother: *data',
            'name: demo\ndescription: useful\nmetadata:\n  key: first\n  key: second',
            'name: demo\ndescription: useful\nmetadata: !!python/object/apply:os.system ["echo forbidden"]',
        ]:
            with self.subTest(fields=fields):
                self.skill(frontmatter=fields)
                run = self.cli(self.root, isolated=True)
                self.assertEqual(run.returncode, 1, run.stderr)
                self.assertFalse(json.loads(run.stdout)["passed"])

    def test_empty_collection_fails(self):
        self.assertFalse(self.result()["passed"])


if __name__ == '__main__':
    unittest.main()
