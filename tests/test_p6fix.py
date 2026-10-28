#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P6-FIX 回归：A1–A5（向导）、B1–B3（安全）、D1（health 指纹）。

全部在临时 HOME / 临时数据目录内运行，不读写真实 ~/.boss-pipeline。

用法：
    cd boss-pipeline && python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / 'scripts'))

_TMP = tempfile.mkdtemp(prefix='p6fix-')
os.environ['HOME'] = os.path.join(_TMP, 'fakehome')
os.environ['BOSS_PIPELINE_HOME'] = os.path.join(_TMP, 'home', '.boss-pipeline')
os.environ.setdefault('BOSS_PIPELINE_SKILLS_ROOT', str(REPO.parent))
Path(os.environ['HOME']).mkdir(parents=True, exist_ok=True)

import config_paths as cp  # noqa: E402
import onboarding_core as oc  # noqa: E402


def valid_answers(pid: str = 'me') -> dict:
    return {
        'profile_id': pid,
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


def reset_home() -> Path:
    root = Path(os.environ['BOSS_PIPELINE_HOME'])
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    os.chmod(root, 0o700)
    oc.save_state(oc.default_state())
    return root


class OnboardingFixes(unittest.TestCase):
    def setUp(self):
        self.root = reset_home()

    def _submit_through_5(self, answers=None):
        answers = answers or valid_answers()
        self.assertTrue(oc.submit_step(1, {}).get('ok'))
        self.assertTrue(oc.submit_step(2, answers).get('ok'))
        self.assertTrue(oc.submit_step(3, {}).get('ok'))
        self.assertTrue(oc.submit_step(4, {'skipped': True}).get('ok'))
        self.assertTrue(oc.submit_step(5, {'dry_run': True}).get('ok'))

    # ── A1 ────────────────────────────────────────────────
    def test_a1_keyword_error_is_chinese_and_blocks_advance(self):
        oc.submit_step(1, {})
        bad = valid_answers()
        bad['dimension_keywords']['专业能力'] = ['3']
        res = oc.submit_step(2, bad)
        self.assertFalse(res['ok'])
        joined = ' '.join(res['errors'])
        self.assertIn('关键词至少 2 个字', joined)
        self.assertIn('专业能力', joined)
        self.assertEqual(oc.load_state()['step'], 2)

    def test_a1_resume_rewinds_to_failing_step(self):
        oc.submit_step(1, {})
        oc.submit_step(2, valid_answers())
        st = oc.load_state()
        st['answers']['dimension_keywords']['数据能力'] = ['x']
        st['completed_steps'] = [1, 2, 3, 4, 5]
        st['step'] = 5
        st['ready'] = True
        oc.save_state(st)

        rep = oc.repair_state()
        self.assertIsNotNone(rep)
        self.assertEqual(rep['step'], 2)
        self.assertTrue(any('至少 2 个字' in e for e in rep['errors']))
        self.assertEqual(oc.load_state()['step'], 2)

    def test_a1_later_step_rejects_invalid_saved_profile(self):
        oc.submit_step(1, {})
        oc.submit_step(2, valid_answers())
        st = oc.load_state()
        st['answers']['identity']['display_name'] = ''
        oc.save_state(st)
        res = oc.submit_step(3, {})
        self.assertFalse(res['ok'])
        self.assertEqual(res.get('fix_step'), 2)
        self.assertEqual(oc.load_state()['step'], 2)

    def test_a1_numbers_are_normalized_before_publish(self):
        self._submit_through_5()
        saved = oc.load_state()['answers']['identity']['total_years']
        self.assertIsInstance(saved, int)
        res = oc.commit()
        self.assertTrue(res['ok'], res.get('errors'))
        profile = json.loads((self.root / 'profiles' / 'me' / 'profile.json').read_text())
        self.assertIsInstance(profile['identity']['total_years'], int)
        self.assertIsInstance(profile['targets']['salary']['min_k'], int)

    # ── A2 ────────────────────────────────────────────────
    def test_a2_retry_reuses_same_draft_id(self):
        self._submit_through_5()
        orig = oc._run_validator
        calls = {'n': 0}

        def flaky(*a, **kw):
            calls['n'] += 1
            if calls['n'] == 1:
                return 1, {'errors': [{'code': 'E-TEST', 'path': '', 'message': '模拟校验失败'}]}
            return orig(*a, **kw)

        oc._run_validator = flaky
        try:
            first = oc.commit()
            self.assertFalse(first['ok'])
            self.assertFalse((self.root / 'profiles' / 'me').exists(), '失败不得留下空画像目录')
            second = oc.commit()
        finally:
            oc._run_validator = orig
        self.assertTrue(second['ok'], second.get('errors'))
        self.assertEqual(second['profile_id'], 'me')
        self.assertEqual(sorted(p.name for p in (self.root / 'profiles').iterdir()), ['me'])
        self.assertFalse((self.root / 'profiles' / 'me-2').exists())

    def test_a2_published_profile_is_never_overwritten(self):
        pdir = self.root / 'profiles' / 'me'
        pdir.mkdir(parents=True)
        (pdir / 'profile.json').write_text('{"keep": true}')
        (pdir / 'published.json').write_text('{"profile_id": "me"}')

        self._submit_through_5()
        res = oc.commit()
        self.assertFalse(res['ok'])
        self.assertIn('已发布', ' '.join(res['errors']))
        self.assertEqual((pdir / 'profile.json').read_text(), '{"keep": true}')

    # ── A5 ────────────────────────────────────────────────
    def test_a5_finalize_requires_step5_done(self):
        oc.submit_step(1, {})
        oc.submit_step(2, valid_answers())
        res = oc.commit()  # finalize=True，但第 5 步未完成
        self.assertFalse(res['ok'])
        self.assertEqual(res.get('fix_step'), 5)
        self.assertFalse((self.root / 'profiles' / 'me' / 'published.json').exists())

    def test_a5_finalize_false_only_writes_draft(self):
        self._submit_through_5()
        res = oc.commit(finalize=False)
        self.assertTrue(res['ok'])
        self.assertTrue(res.get('draft'))
        draft = Path(res['path'])
        self.assertTrue((draft / 'profile.json').is_file())
        self.assertTrue((draft / 'prefs.yaml').is_file())
        self.assertFalse((self.root / 'profiles' / 'me' / 'published.json').exists())
        self.assertFalse((self.root / 'config.json').exists())
        self.assertFalse((self.root / 'profiles' / 'me').exists())

    def test_a5_commit_writes_published_and_config(self):
        self._submit_through_5()
        res = oc.commit()
        self.assertTrue(res['ok'])
        self.assertTrue((self.root / 'profiles' / 'me' / 'published.json').is_file())
        cfg = json.loads((self.root / 'config.json').read_text())
        self.assertEqual(cfg['active_profile'], 'me')
        self.assertTrue(cfg['onboarding']['completed'])

    # ── B1 ────────────────────────────────────────────────
    def test_b1_state_has_no_answers_draft_does(self):
        self._submit_through_5()
        st = oc.status()
        self.assertNotIn('answers', st)
        self.assertTrue(st.get('draft_available'))
        self.assertIn('steps', st)
        self.assertIn('answers', oc.draft_payload())

    # ── B3 ────────────────────────────────────────────────
    def test_b3_active_profile_traversal_rejected(self):
        (self.root / 'config.json').write_text(json.dumps({'active_profile': '../evil'}))
        (self.root / 'profiles' / 'evil').mkdir(parents=True, exist_ok=True)
        with self.assertRaises(cp.ConfigError) as ctx:
            cp.resolve_profile_id()
        self.assertIn('active_profile 非法', str(ctx.exception))
        with self.assertRaises(cp.ConfigError):
            cp.profile_dir('../evil')


class HttpSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = reset_home()
        cls.out = cls.root / 'work' / 'out'
        cls.hist = cls.root / 'work'
        cls.out.mkdir(parents=True, exist_ok=True)
        import serve_dashboard as sd
        cls.sd = sd
        sd.DashboardHandler.output_dir = str(cls.out)
        sd.DashboardHandler.history_root = str(cls.hist)
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), sd.DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, path, host='127.0.0.1', method='GET'):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        c.putrequest(method, path, skip_host=True)
        c.putheader('Host', host)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        headers = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, headers, body

    def test_b1_no_cors_header(self):
        st, headers, _ = self.req('/api/onboarding/state')
        self.assertEqual(st, 200)
        self.assertNotIn('access-control-allow-origin', headers)
        st2, headers2, _ = self.req('/api/health')
        self.assertEqual(st2, 200)
        self.assertNotIn('access-control-allow-origin', headers2)

    def test_b1_health_fingerprint(self):
        st, _, body = self.req('/api/health')
        self.assertEqual(st, 200)
        data = json.loads(body)
        self.assertEqual(data['service'], 'boss-pipeline-dashboard')
        self.assertRegex(data['home_fingerprint'], r'^[0-9a-f]{16}$')
        self.assertNotIn('profile', body.decode())

    def test_b2_traversal_all_404(self):
        # 协议相对路径用拼接构造，避免源码里出现真实家目录字面量
        proto = '//' + 'Users' + '/example/.boss-pipeline/profiles/x/profile.json'
        for p in ('/../../etc/passwd',
                  proto,
                  '/%2e%2e/%2e%2e/etc/passwd',
                  '/%2fetc%2fpasswd',
                  '/..%2f..%2fetc%2fpasswd'):
            st, _, body = self.req(p)
            self.assertEqual(st, 404, f'{p} -> {st}')
            self.assertNotIn(b'root:', body)

    def test_b2_legit_static_still_200(self):
        st, _, body = self.req('/wizard.html')
        self.assertEqual(st, 200)
        self.assertIn(b'<html', body.lower())

    def test_b1_bad_host_rejected(self):
        st, _, _ = self.req('/api/health', host='evil.example.com')
        self.assertEqual(st, 403)

    def test_b1_draft_endpoint_returns_answers(self):
        st = oc.default_state()
        st['answers'] = valid_answers()
        st['step'] = 2
        oc.save_state(st)
        st_code, headers, body = self.req('/api/onboarding/draft')
        self.assertEqual(st_code, 200)
        self.assertNotIn('access-control-allow-origin', headers)
        self.assertIn('answers', json.loads(body))

    def test_d1_fingerprint_differs_by_home(self):
        old = os.environ.get('BOSS_PIPELINE_HOME')
        try:
            os.environ['BOSS_PIPELINE_HOME'] = str(self.root)
            a = self.sd._home_fingerprint()
            os.environ['BOSS_PIPELINE_HOME'] = str(self.root / 'other-home')
            b = self.sd._home_fingerprint()
        finally:
            os.environ['BOSS_PIPELINE_HOME'] = old
        self.assertRegex(a, r'^[0-9a-f]{16}$')
        self.assertNotEqual(a, b)

    def test_six_gates(self):
        """三栏六条门禁：三栏 DOM + 5 个核心接口。"""
        st, _, body = self.req('/')
        self.assertEqual(st, 200)
        html = body.decode('utf-8', 'ignore')
        for marker in ('class="dashboard"', 'id="jobList"', 'id="jobDetail"'):
            self.assertIn(marker, html, f'三栏 DOM 缺失：{marker}')
        checks = [
            ('/api/onboarding/state', 'needs_onboarding'),
            ('/api/health', 'home_fingerprint'),
            ('/sessions', 'sessions'),
            ('/api/stats/global', None),
            ('/api/monitors', None),
        ]
        for path, key in checks:
            st, _, body = self.req(path)
            self.assertEqual(st, 200, f'{path} -> {st}')
            data = json.loads(body)
            self.assertIsInstance(data, (dict, list), path)
            if key:
                self.assertIn(key, data, f'{path} 缺 {key}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
