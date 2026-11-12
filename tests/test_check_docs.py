"""Focused fixtures for scripts/check_docs.py heading and link validation."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / 'scripts' / 'check_docs.py'


def run_checker(files: dict[str, str]) -> tuple[int, str, str]:
    """Run the tracker against a throwaway repository containing *files*."""
    with tempfile.TemporaryDirectory(prefix='check-docs-fixture-') as temp:
        repo = Path(temp)
        (repo / 'scripts').mkdir()
        shutil.copyfile(CHECKER, repo / 'scripts' / 'check_docs.py')
        for name, text in files.items():
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
        subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
        subprocess.run(['git', 'add', '-A'], cwd=repo, check=True)
        result = subprocess.run(
            [sys.executable, str(repo / 'scripts' / 'check_docs.py')],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr


class CheckDocsFixtureTests(unittest.TestCase):
    def test_chinese_heading_and_duplicate_suffix_are_supported(self):
        readme = (
            '# 中文标题\n\n'
            '[chinese](#中文标题)\n'
            '[duplicate](#same-heading-1)\n\n'
            '## Same heading\n\n'
            '## Same heading\n'
        )
        code, stdout, stderr = run_checker({'README.md': readme})
        self.assertEqual(code, 0, stderr)
        self.assertIn('2 local documentation links/anchors; 0 errors.', stdout)

    def test_inline_code_is_removed_from_the_slug(self):
        readme = '# Use `foo` here\n\n[inline code](#use-foo-here)\n'
        code, stdout, stderr = run_checker({'README.md': readme})
        self.assertEqual(code, 0, stderr)
        self.assertIn('0 errors.', stdout)

    def test_missing_local_destination_fails(self):
        readme = '[missing](does-not-exist.md)\n'
        code, stdout, stderr = run_checker({'README.md': readme})
        self.assertEqual(code, 1)
        self.assertIn('missing local destination does-not-exist.md', stderr)

    def test_valid_and_invalid_fragments_are_distinguished(self):
        readme = '# Present\n\n[ok](#present)\n[bad](#absent)\n'
        code, stdout, stderr = run_checker({'README.md': readme})
        self.assertEqual(code, 1)
        self.assertIn('missing heading #absent', stderr)
        self.assertNotIn('#present', stderr)

    def test_fenced_code_links_are_not_checked(self):
        readme = '```\n[fenced](never-created.md)\n```\n'
        code, stdout, stderr = run_checker({'README.md': readme})
        self.assertEqual(code, 0, stderr)
        self.assertIn('0 local documentation links/anchors; 0 errors.', stdout)


if __name__ == '__main__':
    unittest.main()
