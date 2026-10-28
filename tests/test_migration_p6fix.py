#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P6-FIX 迁移回归：C1（无冻结快照自行生成）、C2（缺词表明确报错）、C3（legacy 不静默关闸）。

全部在 /tmp 沙箱内运行，源配置为合成数据，不读取真实画像。
用法：cd boss-pipeline && python3 -m unittest tests.test_migration_p6fix -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
MATCHER = REPO.parent / 'job-match-priority'
MIGRATE = REPO / 'scripts' / 'migrate_profile.py'

V1_PROFILE = {
    'meta': {
        'name': '测试用户', 'target_titles': ['运营'],
        'expected_salary_min_k': 20, 'expected_salary_max_k': 35,
        'preferred_cities': ['杭州'], 'accept_remote': False,
        'exclude_titles_keywords': [], 'target_roles': ['运营'],
        'exclude_assistant_types': [], 'welfare_floor': [],
        'education_max': '本科', 'total_years': 5,
        'language_levels': {'英语': 3}, 'industry_experience': [],
        'role_accumulation': {}, 'hard_filter_industries': [],
        'no_certifications': [], 'certifications': [],
    },
    'dimensions': {'专业能力': 3, '项目与流程管理': 3, '数据能力': 2},
    'domain_coverage': [], 'domain_blacklist': [],
    'ai_boost': {'enabled': True, 'scope': 'title'},
}

MATCH_CONFIG = """\
scoring:
  weight_first: 15
  weight_second: 15
  weight_rest: 10
  min_items: 5
  max_items: 7
  thresholds: {high: 80, medium: 60}
depth_coefficient: {ge: 1.0, minus1: 0.5, minus2plus: 0.0}
"""

TAXONOMY = """\
schema_version: 1
dimensions:
  - id: compensation
    name: 薪酬福利
"""


class MigrationFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='p6fix-mig-'))
        self.skills = self.tmp / 'skills'
        jm = self.skills / 'job-match-priority'
        (jm / 'config').mkdir(parents=True)
        (jm / 'scripts').mkdir(parents=True)
        shutil.copy2(MATCHER / 'scripts' / 'matcher.py', jm / 'scripts' / 'matcher.py')
        self._install_templates(jm)
        (self.skills / 'boss-pipeline').symlink_to(REPO, target_is_directory=True)
        (jm / 'config' / 'candidate_profile.json').write_text(
            json.dumps(V1_PROFILE, ensure_ascii=False), encoding='utf-8')
        (jm / 'config' / 'match_config.yaml').write_text(MATCH_CONFIG, encoding='utf-8')
        (jm / 'config' / 'taxonomy.yaml').write_text(TAXONOMY, encoding='utf-8')
        self.fakehome = self.tmp / 'fakehome'
        self.fakehome.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install_templates(self, jm: Path):
        shutil.copytree(MATCHER / 'templates', jm / 'templates')

    def _env(self):
        env = dict(os.environ)
        env['HOME'] = str(self.fakehome)
        env['BOSS_PIPELINE_SKILLS_ROOT'] = str(self.skills)
        env.pop('BOSS_PIPELINE_HOME', None)
        return env

    def _run_migrate(self, home: Path):
        return subprocess.run(
            [sys.executable, str(MIGRATE), '--profile', 't1', '--home', str(home)],
            capture_output=True, text=True, env=self._env(), timeout=120,
        )

    # ── C1 ────────────────────────────────────────────────
    def test_c1_generates_baseline_and_succeeds(self):
        home = self.tmp / 'home1' / '.boss-pipeline'
        out = self._run_migrate(home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('[baseline] 未找到冻结快照', out.stdout)
        baseline = home / 'tests' / 'baseline' / 'match_config_20261028.yaml'
        self.assertTrue(baseline.is_file())
        self.assertEqual(baseline.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('E-SOURCE', out.stdout + out.stderr)
        prefs = (home / 'profiles' / 't1' / 'prefs.yaml').read_text(encoding='utf-8')
        self.assertIn('strong_keywords:', prefs)
        import yaml
        data = yaml.safe_load(prefs)
        self.assertEqual(len(data['rules']['title_bias']['strong_keywords']), 28)
        self.assertEqual(len(data['rules']['dimension_aliases']), 9)

    # ── C2 ────────────────────────────────────────────────
    def test_c2_missing_literals_is_explicit_error(self):
        jm = self.skills / 'job-match-priority'
        (jm / 'templates' / 'legacy_literals.yaml').unlink()
        home = self.tmp / 'home2' / '.boss-pipeline'
        out = self._run_migrate(home)
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertIn('E-LITERALS', out.stderr)
        self.assertIn('本次未写入任何文件', out.stderr)
        self.assertFalse((home / 'profiles').exists())

    # ── C3 ────────────────────────────────────────────────
    def test_c3_legacy_mode_keeps_title_gate(self):
        code = (
            'import sys, json; sys.path.insert(0, %r); '
            'import config_paths as cp; '
            'e = cp.effective_config(); '
            'print(json.dumps({"tb": len((e.get("title_bias") or {}).get("strong_keywords") or []),'
            '"aliases": len(e.get("dimension_aliases") or {}),'
            '"qg": e.get("quality_gates") or {}}, ensure_ascii=False))'
        ) % str(REPO / 'scripts')
        env = self._env()
        env.pop('BOSS_PIPELINE_HOME', None)
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        data = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(data['tb'], 28)
        self.assertEqual(data['aliases'], 9)
        self.assertEqual(data['qg']['min_items_for_high'], 4)
        self.assertIn('冻结词表', out.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
