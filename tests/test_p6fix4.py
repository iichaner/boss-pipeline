#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P6-FIX4 回归：样例文件名、quality 归因、预览占位符、--step 校验、端口建议。

全部在临时目录运行，不读写真实 ~/.boss-pipeline。

用法：
    cd boss-pipeline && python3 -m unittest tests.test_p6fix4 -v
"""

from __future__ import annotations

import csv
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SKILLS = REPO.parent

sys.path.insert(0, str(REPO / 'scripts'))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='p6fix4-'))
        self._old = {k: os.environ.get(k) for k in
                     ('HOME', 'BOSS_PIPELINE_HOME', 'BOSS_PIPELINE_SKILLS_ROOT')}
        os.environ['HOME'] = str(self.tmp / 'home')
        os.environ['BOSS_PIPELINE_HOME'] = str(self.tmp / 'data')
        os.environ['BOSS_PIPELINE_SKILLS_ROOT'] = str(SKILLS)
        Path(os.environ['HOME']).mkdir(parents=True, exist_ok=True)
        Path(os.environ['BOSS_PIPELINE_HOME']).mkdir(parents=True, exist_ok=True)
        import onboarding_core as oc
        self.oc = oc
        oc.save_state(oc.default_state())

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class SampleFilenames(unittest.TestCase):
    def test_quickstart_uses_real_sample_name(self):
        text = (REPO / 'QUICKSTART.md').read_text(encoding='utf-8')
        self.assertNotIn('jds_' + 'onboarding', text)
        self.assertIn('samples/jd_onboarding.json', text)
        self.assertTrue((SKILLS / 'job-match-priority' / 'templates' /
                         'samples' / 'jd_onboarding.json').is_file())

    def test_no_bad_sample_name_in_four_repos(self):
        bad = []
        for repo in ('boss-pipeline', 'boss-resume-crawler',
                     'job-match-priority', 'boss-auto-greet'):
            root = SKILLS / repo
            for p in root.rglob('*'):
                if not p.is_file() or p.suffix.lower() not in ('.md', '.py', '.html'):
                    continue
                if '__pycache__' in p.parts or '.git' in p.parts:
                    continue
                bad_name = 'jds_' + 'onboarding'
                if bad_name in p.read_text(encoding='utf-8', errors='ignore'):
                    bad.append(str(p.relative_to(SKILLS)))
        self.assertEqual(bad, [])


class ExplainLowQuality(unittest.TestCase):
    def test_quality_buckets_and_command(self):
        tmp = Path(tempfile.mkdtemp(prefix='p6fix4-e-'))
        try:
            p = tmp / 'r.csv'
            rows = [
                {'优先级判断': 'low',
                 '优先级判断理由': '匹配度100.0%，专业能力匹配，严重超配降级',
                 '能力条目得分': '专业能力12/12'},
                {'优先级判断': 'low',
                 '优先级判断理由': '匹配度92.0%，专业能力匹配',
                 '能力条目得分': '专业能力10/10 数据能力6/6'},
                {'优先级判断': 'low',
                 '优先级判断理由': '匹配度96.0%，专业能力匹配；[复核降级]JD内容空泛',
                 '能力条目得分': '专业能力12/12 数据能力12/12'},
                {'优先级判断': 'low',
                 '优先级判断理由': '匹配度60.0%，专业能力匹配',
                 '能力条目得分': '专业能力4/10 数据能力4/10 沟通协作4/10 团队管理4/10'},
                {'优先级判断': 'low',
                 '优先级判断理由': '硬过滤：城市不符'},
            ]
            with open(p, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.DictWriter(f, fieldnames=['优先级判断', '优先级判断理由',
                                                  '能力条目得分'])
                w.writeheader()
                w.writerows(rows)
            out = subprocess.run(
                [sys.executable, str(REPO / 'scripts' / 'explain_low.py'),
                 '--result', str(p), '--cause', 'quality'],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn('[quality] 3 条', out.stdout)
            self.assertIn('超配 1 条', out.stdout)
            self.assertIn('复核降级 1 条', out.stdout)
            self.assertIn('命中条目不足 1 条', out.stdout)
            self.assertNotIn('[keywords]', out.stdout)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class DryRunPreview(Base):
    def _plan(self, items):
        oc = self.oc
        orig = (oc.preview, oc.build_profile, oc.build_prefs)
        oc.preview = lambda *a, **k: {'items': items}
        oc.build_profile = lambda st: {}
        oc.build_prefs = lambda st: {}
        try:
            return oc.dry_run_plan(oc.default_state())
        finally:
            oc.preview, oc.build_profile, oc.build_prefs = orig

    def test_preview_uses_first_target_title(self):
        plan = self._plan([
            {'job_id': 'x', 'jd_title': '后端开发工程师', 'level': 'medium'},
        ])
        self.assertIn('后端开发工程师', plan['message_preview'])
        self.assertNotIn('{job}', plan['message_preview'])
        self.assertNotIn('{', plan['message_preview'])
        self.assertNotIn('}', plan['message_preview'])

    def test_preview_falls_back_when_no_target(self):
        plan = self._plan([])
        self.assertIn('该岗位', plan['message_preview'])
        self.assertNotIn('{', plan['message_preview'])
        self.assertNotIn('}', plan['message_preview'])


class StepValidation(Base):
    def _run(self, step):
        env = os.environ.copy()
        return subprocess.run(
            [sys.executable, str(REPO / 'scripts' / 'onboard.py'),
             '--step', step],
            capture_output=True, text=True, timeout=60, env=env,
        )

    def test_out_of_range_steps_fail_fast(self):
        for step in ('9', '0', 'abc'):
            out = self._run(step)
            self.assertNotEqual(out.returncode, 0, step)
            combined = out.stdout + out.stderr
            self.assertNotIn('从第 5 步', combined, step)
            self.assertNotIn('[5/5]', combined, step)
            if step == 'abc':
                self.assertIn('invalid int value', combined)
            else:
                self.assertIn('--step 必须是 1–5', combined)


class PortSuggestion(unittest.TestCase):
    def test_port_advice_is_launcher_not_kill(self):
        import env_check
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            c = env_check.check_port(port)
        finally:
            s.close()
        self.assertFalse(c['ok'])
        self.assertIn('启动器', c['fix_cmd'])
        self.assertNotIn('lsof', c['fix_cmd'])
        self.assertNotIn('lsof', c['fix_cmd'])
        self.assertNotIn('kill', c['fix_cmd'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
