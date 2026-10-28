#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""onboarding_api.py —— Dashboard 向导 6 条 API 的 Mixin（P4）。

设计依据：DESIGN.md D.2 / D.3。由 serve_dashboard.DashboardHandler 多继承；
方法返回一律走宿主类的 `self._json_response(...)`，不自行写 socket。

路由（注册在 serve_dashboard.do_GET / do_POST）::

    GET  api/onboarding/state
    GET  api/onboarding/env
    POST api/onboarding/answer
    POST api/onboarding/preview
    POST api/onboarding/commit
    POST api/onboarding/reset

失败时不抛 traceback：任何异常都收敛为 `{ok:false, error: <可读原因>}`。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config_paths as cp  # noqa: E402
import env_check  # noqa: E402
import onboarding_core as oc  # noqa: E402


class OnboardingMixin:
    """给 DashboardHandler 增加向导 API；不触碰任何既有 handler 分支。"""

    # ── GET ───────────────────────────────────────────────
    def _onb_state(self):
        try:
            # B1：只回进度（step/done），不回任何答案内容
            return self._json_response(oc.status())
        except Exception as e:
            # 静默降级：前端 catch 放行，不把用户锁在向导外
            return self._json_response({
                'needs_onboarding': False,
                'profile_id': None,
                'step': 1,
                'completed': False,
                'reason': 'state_error',
                'error': str(e),
            })

    def _onb_draft(self):
        """表单回填专用：仅同源页面可读（与 state 分离，降低跨域泄露面）。"""
        try:
            return self._json_response(oc.draft_payload())
        except Exception as e:
            return self._json_response({'ok': False, 'answers': {}, 'error': str(e)}, 500)

    def _onb_env(self):
        try:
            return self._json_response(env_check.run_checks())
        except Exception as e:
            return self._json_response({
                'checks': [],
                'summary': {'ok': False, 'total': 0, 'passed': 0, 'failed': 0,
                            'blocking_failed': 0, 'home': '', 'work_root': '', 'python': ''},
                'error': str(e),
            })

    # ── POST ──────────────────────────────────────────────
    def _onb_answer(self, data: dict):
        try:
            step = (data or {}).get('step')
            answers = (data or {}).get('answers') or {}
            if step is None:
                return self._json_response({'ok': False, 'errors': ['缺少 step']}, 400)
            result = oc.submit_step(int(step), answers)
            return self._json_response(result, 200 if result.get('ok') else 400)
        except (ValueError, TypeError):
            return self._json_response({'ok': False, 'errors': ['step 必须是 1–5']}, 400)
        except Exception as e:
            return self._json_response({'ok': False, 'errors': [str(e)]}, 500)

    def _onb_preview(self, data: dict):
        try:
            data = data or {}
            profile = data.get('profile')
            prefs = data.get('prefs')
            if profile is not None or prefs is not None:
                if not isinstance(profile, dict) or not isinstance(prefs, dict):
                    return self._json_response(
                        {'items': [], 'spread_ok': False,
                         'errors': ['profile / prefs 必须同时为对象']}, 400)
                result = oc.preview(profile, prefs)
            else:
                # {draft_id} 或空：使用当前向导草稿
                result = oc.preview()
            return self._json_response(result, 200 if not result.get('errors') else 400)
        except Exception as e:
            return self._json_response({'items': [], 'spread_ok': False,
                                        'errors': [str(e)]}, 500)

    def _onb_commit(self, data: dict):
        try:
            data = data or {}
            profile = data.get('profile')
            prefs = data.get('prefs')
            if (profile is None) != (prefs is None):
                return self._json_response(
                    {'ok': False, 'errors': ['profile / prefs 必须同时提供或同时省略']}, 400)
            # A5：finalize 只决定「发布」还是「只存草稿」，不再是一个可绕过闸门的开关
            finalize = data.get('finalize', True)
            if not isinstance(finalize, bool):
                return self._json_response(
                    {'ok': False, 'errors': ['finalize 必须是布尔值']}, 400)
            result = oc.commit(profile, prefs, finalize)
            return self._json_response(result, 200 if result.get('ok') else 400)
        except cp.ConfigError as e:
            return self._json_response({'ok': False, 'errors': [str(e)]}, 400)
        except Exception as e:
            return self._json_response({'ok': False, 'errors': [str(e)]}, 500)

    def _onb_reset(self, data: dict):
        try:
            data = data or {}
            result = oc.reset(data.get('profile_id'))
            return self._json_response(result)
        except Exception as e:
            return self._json_response({'ok': False, 'error': str(e)}, 500)
