#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P6-FIX3 回归：P1-7 爬取退出码、N4 僵尸岗默认关闭、P1-5 阈值可调、
N1 发布确认、N3 列名兼容、N5/N6 样例与自检一致性。

全部在临时目录运行，不读写真实 ~/.boss-pipeline。

用法：
    cd boss-pipeline && python3 -m unittest tests.test_p6fix3 -v
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SKILLS = REPO.parent
MATCHER = SKILLS / 'job-match-priority'
CRAWLER = SKILLS / 'boss-resume-crawler' / 'scripts' / 'boss_extract_cdp.py'
GREETER = SKILLS / 'boss-auto-greet' / 'scripts' / 'boss_auto_greet.py'

sys.path.insert(0, str(REPO / 'scripts'))


def _answers(**over):
    a = {
        'profile_id': 't3',
        'identity': {
            'display_name': '测试用户', 'total_years': 5, 'education': '本科',
            'languages': {'英语': 3}, 'certifications': [],
        },
        'targets': {
            'roles': ['运营', '项目管理'], 'cities': ['杭州'],
            'salary': {'min_k': 25, 'max_k': 40}, 'accept_remote': True,
            'exclude_title_keywords': [], 'exclude_role_keywords': [],
        },
        'filters': {'exclude_industries': [], 'domain_blacklist': []},
        'experience': {'industries': []},
        'capabilities': {'dimensions': {'专业能力': 3, '项目与流程管理': 3, '数据能力': 2}},
        'dimension_keywords': {
            '专业能力': ['流程', '复盘'],
            '项目与流程管理': ['项目', '交付'],
            '数据能力': ['看板', '指标'],
        },
        'dimension_definitions': {},
        'engine': {},
    }
    a.update(over)
    return a


class Fix3Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='p6fix3-'))
        self._old_env = {k: os.environ.get(k) for k in
                         ('HOME', 'BOSS_PIPELINE_HOME', 'BOSS_PIPELINE_SKILLS_ROOT')}
        os.environ['HOME'] = str(self.tmp / 'home')
        os.environ['BOSS_PIPELINE_HOME'] = str(self.tmp / 'data' / '.boss-pipeline')
        os.environ['BOSS_PIPELINE_SKILLS_ROOT'] = str(SKILLS)
        Path(os.environ['HOME']).mkdir(parents=True, exist_ok=True)
        Path(os.environ['BOSS_PIPELINE_HOME']).mkdir(parents=True)

        import onboarding_core as oc
        self.oc = oc
        oc.save_state(oc.default_state())

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _through(self, answers=None):
        answers = answers or _answers()
        self.assertTrue(self.oc.submit_step(1, {}).get('ok'))
        res = self.oc.submit_step(2, answers)
        self.assertTrue(res.get('ok'), res)
        self.assertTrue(self.oc.submit_step(3, {}).get('ok'))
        self.assertTrue(self.oc.submit_step(4, {'skipped': True}).get('ok'))
        self.assertTrue(self.oc.submit_step(5, {'dry_run': True}).get('ok'))


class Thresholds(Fix3Base):
    """P1-5：阈值在向导里真的能改，且有校验。"""

    def test_default_thresholds_written(self):
        self.oc.submit_step(1, {})
        self.oc.submit_step(2, _answers())
        prefs = self.oc.build_prefs(self.oc.load_state())
        thr = prefs['engine']['scoring']['thresholds']
        self.assertEqual(thr, {'high': 95, 'medium': 90})

    def test_custom_thresholds_roundtrip(self):
        a = _answers(engine={'scoring': {'thresholds': {'high': 85, 'medium': 70}}})
        self._through(a)
        prefs = self.oc.build_prefs(self.oc.load_state())
        self.assertEqual(prefs['engine']['scoring']['thresholds'],
                         {'high': 85, 'medium': 70})
        eff = self.oc.effective_for(prefs)
        self.assertEqual(eff['scoring']['thresholds'], {'high': 85, 'medium': 70})

    def test_invalid_thresholds_rejected_in_chinese(self):
        for bad in ({'high': 95, 'medium': 95}, {'high': 80, 'medium': 90},
                    {'high': 101, 'medium': 90}):
            self.oc.save_state(self.oc.default_state())
            self.oc.submit_step(1, {})
            res = self.oc.submit_step(
                2, _answers(engine={'scoring': {'thresholds': bad}}))
            self.assertFalse(res['ok'], bad)
            joined = ' '.join(res['errors'])
            self.assertIn('阈值', joined)
            self.assertIn('medium', joined)


class PublishConfirm(Fix3Base):
    """N1：只有显式 y/yes 才算确认。"""

    def test_only_explicit_yes_confirms(self):
        import onboard
        for raw in ('y', 'Y', 'yes', ' YES '):
            self.assertTrue(onboard._is_publish_confirmed(raw), raw)
        for raw in ('', 'x', '回车', 'n', 'no', '1'):
            self.assertFalse(onboard._is_publish_confirmed(raw), raw)

    def test_wizard_requires_typed_confirm(self):
        html = (REPO / 'dashboard' / 'wizard.html').read_text(encoding='utf-8')
        self.assertIn('id="f_confirm"', html)
        self.assertIn('syncFinishBtn', html)
        self.assertIn('finishBtn" onclick="finish()" disabled', html)


class ErrorPaths(Fix3Base):
    """P1-4：预期错误 2 / 未知异常 1 且堆栈只进日志。"""

    def test_expected_vs_unexpected(self):
        sys.path.insert(0, str(MATCHER / 'scripts'))
        import run_match

        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run_match._fail_expected(FileNotFoundError(2, 'no', '/nope.csv'))
        self.assertEqual(code, 2)
        self.assertIn('/nope.csv', err.getvalue())
        self.assertIn('修复：', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run_match._fail_unexpected(RuntimeError('boom'))
        self.assertEqual(code, 1)
        self.assertIn('未预期的错误', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())  # 终端不刷堆栈
        log = Path(os.environ['BOSS_PIPELINE_HOME']) / 'logs' / 'run_match_error.log'
        self.assertTrue(log.is_file())
        self.assertIn('RuntimeError: boom', log.read_text(encoding='utf-8'))


class WizardThresholds(unittest.TestCase):
    """P1-5 的另一半：网页端第 2 步真的有阈值输入并提交。"""

    def test_step2_has_threshold_inputs(self):
        html = (REPO / 'dashboard' / 'wizard.html').read_text(encoding='utf-8')
        for needle in ('id="f_thr_high"', 'id="f_thr_medium"',
                       '0 < medium < high',
                       'thresholds: {'):
            self.assertIn(needle, html)


class ZombieDefaultOff(unittest.TestCase):
    """N4：出厂模板默认关闭僵尸岗规则，机制仍在。"""

    def test_generic_zombie_rules_off(self):
        cfg = yaml.safe_load(
            (MATCHER / 'templates' / 'families' / 'generic.yaml').read_text(encoding='utf-8'))
        hf = cfg['rules']['hard_filters']
        self.assertEqual(hf['zombie_empty_highpay'], {})
        self.assertEqual(hf['zombie_title_keywords'], [])
        self.assertEqual(hf['zombie_shell']['company_keywords'], [])
        self.assertEqual(hf['zombie_shell']['title_keywords'], [])

    def test_short_highpay_jd_not_filtered_by_default(self):
        """JD < 200 字 + 25–40K，在默认模板下不应再被判「僵尸岗位」。"""
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix='p6fix3-z-'))
        try:
            prof = {
                'meta': {
                    'name': '测试', 'target_titles': ['后端'], 'target_roles': ['后端'],
                    'expected_salary_min_k': 25, 'expected_salary_max_k': 40,
                    'preferred_cities': ['杭州'], 'accept_remote': True,
                    'exclude_titles_keywords': [], 'exclude_assistant_types': [],
                    'welfare_floor': [], 'education_max': '本科', 'total_years': 5,
                    'language_levels': {'英语': 3}, 'industry_experience': [],
                    'hard_filter_industries': [], 'no_certifications': [],
                    'certifications': [], 'role_accumulation': {},
                },
                'dimensions': {'专业能力': 3, '数据能力': 2, '项目与流程管理': 3},
                'domain_coverage': [], 'domain_blacklist': [],
                'ai_boost': {'enabled': False, 'scope': 'title'},
            }
            (tmp / 'p.json').write_text(json.dumps(prof, ensure_ascii=False), encoding='utf-8')
            cfg = yaml.safe_load(
                (MATCHER / 'templates' / 'families' / 'generic.yaml').read_text(encoding='utf-8'))
            eff = {}
            for layer in (cfg.get('engine') or {}, cfg.get('rules') or {}):
                from copy import deepcopy
                eff = {**eff, **deepcopy(layer)}
            (tmp / 'c.yaml').write_text(
                yaml.safe_dump(eff, allow_unicode=True, sort_keys=False), encoding='utf-8')
            rows = [{
                '职位名称': '后端开发工程师', '薪资': '25-40K', '城市': '杭州',
                '公司名称': '示例公司', 'job_id': 'z1',
                '职位描述': '负责 Java 后端开发，熟悉 Spring Boot 与 MySQL，参与接口设计与性能优化。',
            }]
            with open(tmp / 'in.csv', 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            out = subprocess.run(
                [sys.executable, str(MATCHER / 'scripts' / 'run_match.py'),
                 '--profile', str(tmp / 'p.json'), '--config', str(tmp / 'c.yaml'),
                 '--input', str(tmp / 'in.csv'), '--out', str(tmp / 'out.csv')],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            with open(tmp / 'out.csv', encoding='utf-8-sig', newline='') as f:
                rec = next(csv.DictReader(f))
            self.assertNotIn('僵尸', rec['优先级判断理由'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CrawlerExitCodes(unittest.TestCase):
    """P1-7：失败不再 exit 0。"""

    def test_dry_run_check_cdp_down_is_nonzero(self):
        env = dict(os.environ)
        env['BOSS_CDP_PORT'] = '9339'
        out = subprocess.run(
            [sys.executable, str(CRAWLER), '--url',
             'https://www.zhipin.com/web/geek/jobs?query=x', '--dry-run-check'],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(out.returncode, 4, out.stdout + out.stderr)
        self.assertIn('9339', out.stdout)
        self.assertIn('修复', out.stdout)
        self.assertNotIn('Traceback', out.stderr)

    def test_crawl_failure_does_not_print_success(self):
        env = dict(os.environ)
        env['BOSS_CDP_PORT'] = '9339'
        tmp = Path(tempfile.mkdtemp(prefix='p6fix3-c-'))
        try:
            out = subprocess.run(
                [sys.executable, str(CRAWLER), '--url',
                 'https://www.zhipin.com/web/geek/jobs?query=x',
                 '--output', str(tmp), '--max-jobs', '1'],
                capture_output=True, text=True, timeout=90, env=env,
            )
            self.assertNotEqual(out.returncode, 0)
            self.assertNotIn('爬取完成', out.stdout)
            self.assertFalse(list(tmp.glob('jobs_data*.csv')))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class GreeterColumns(unittest.TestCase):
    """N3：职位列名兼容，dry-run 不空白。"""

    def test_alias_columns_filled(self):
        tmp = Path(tempfile.mkdtemp(prefix='p6fix3-g-'))
        try:
            p = tmp / 'r.csv'
            with open(p, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.DictWriter(f, fieldnames=['job_id', '岗位名称', '公司名称',
                                                  '优先级判断', '状态'])
                w.writeheader()
                w.writerow({'job_id': 'J1', '岗位名称': '数据运营',
                            '公司名称': '示例公司', '优先级判断': 'high', '状态': ''})
            out = subprocess.run(
                [sys.executable, str(GREETER), '--csv', str(p), '--dry-run'],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn('数据运营', out.stdout)
            self.assertNotIn('[1]  |', out.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SamplesAndEnv(unittest.TestCase):
    """N5 / P1-6 / N6。"""

    def test_onboarding_samples_cover_three_directions(self):
        data = json.loads((MATCHER / 'templates' / 'samples' /
                           'jd_onboarding.json').read_text(encoding='utf-8'))
        jds = data['jds']
        self.assertEqual(len(jds), 6)
        titles = ' '.join(j['title'] for j in jds)
        self.assertTrue(any(k in titles for k in ('运营',)))
        self.assertTrue(any(k in titles for k in ('开发', '工程师')))
        self.assertTrue(any(k in titles for k in ('人力', '财务')))
        self.assertIn('预演', data['_note'])
        for j in jds:
            self.assertLess(len(j['description']), 600)
            self.assertGreater(len(j['description']), 150)

    def test_sample_csv_uses_crawler_columns(self):
        with open(MATCHER / 'sample' / 'jds_sample.csv', encoding='utf-8-sig',
                  newline='') as f:
            header = next(csv.reader(f))
        for col in ('职位名称', '薪资', '城市', '职位描述', 'job_id'):
            self.assertIn(col, header)

    def test_env_check_port_suggestion_is_single(self):
        import env_check
        c = env_check._check('config', 'x', False,
                             'BOSS_PIPELINE_HOME 指向的目录不存在\n修复：unset X 或 mkdir -p Y',
                             'python3 scripts/onboard.py')
        self.assertTrue(c['fix_cmd'].startswith('python3 scripts/onboard.py'))

    def test_fingerprint_matches_dashboard(self):
        import env_check
        import serve_dashboard as sd
        self.assertEqual(env_check.home_fingerprint(), sd._home_fingerprint())


class ExplainLow(unittest.TestCase):
    def test_buckets(self):
        tmp = Path(tempfile.mkdtemp(prefix='p6fix3-e-'))
        try:
            p = tmp / 'r.csv'
            rows = [
                {'优先级判断': 'low', '优先级判断理由': '硬过滤：薪资完全不覆盖'},
                {'优先级判断': 'low', '优先级判断理由': '硬过滤：僵尸岗位(空泛高薪)'},
                {'优先级判断': 'low', '优先级判断理由': '硬过滤：城市不符'},
                {'优先级判断': 'high', '优先级判断理由': '匹配度100%，专业能力匹配'},
            ]
            with open(p, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.DictWriter(f, fieldnames=['优先级判断', '优先级判断理由'])
                w.writeheader()
                w.writerows(rows)
            out = subprocess.run(
                [sys.executable, str(REPO / 'scripts' / 'explain_low.py'),
                 '--result', str(p)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn('[salary]', out.stdout)
            self.assertIn('[zombie]', out.stdout)
            self.assertIn('[city]', out.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
