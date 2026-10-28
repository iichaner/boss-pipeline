#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""profile_adapter.py — 画像 v1（旧）↔ v2（分层后）双向转换（P2）。

设计依据：DESIGN.md A.2（v2 字段）、A.6（M01–M24 映射表）。

约定
----
* `to_v2()` 生成**完整快照**：18 个 meta 键全部有落点，空容器原样保留。
  这样 `to_legacy(to_v2(x)) == x` 才成立（I1）。
* 未参与判定的字段（industry_experience / role_accumulation / certifications /
  welfare_floor / hard_filter_industries / no_certifications）按原对象深拷贝搬运，
  不重建子键，避免丢字段。
* 唯一例外：`ai_boost`（M23/M24）。它**移层到 prefs.rules.override_signals.ai_boost**，
  v2 画像不再保存；`to_legacy()` 回填 `enabled: False`（T-1：不得把旧 true 带进引擎）。
  因此 I1 断言会显式排除这一个键，并在 `migrate_profile.py --verify` 里单独断言
  `prefs.rules.override_signals.ai_boost.enabled is False`。
"""

from __future__ import annotations

import copy

SCHEMA_VERSION = 2
EDUCATION_ENUM = ('大专', '本科', '硕士', '博士', '其他')

# 旧 meta 的 18 个键（顺序即旧文件顺序，仅用于人类阅读；dict 比较不看顺序）
META_KEYS = (
    'name', 'target_titles', 'expected_salary_min_k', 'expected_salary_max_k',
    'preferred_cities', 'accept_remote', 'exclude_titles_keywords', 'target_roles',
    'exclude_assistant_types', 'welfare_floor', 'education_max', 'total_years',
    'language_levels', 'industry_experience', 'role_accumulation',
    'hard_filter_industries', 'no_certifications', 'certifications',
)

# I1 里唯一允许不参与等值比较的键（M23 移层，见模块 docstring）
MOVED_KEYS = ('ai_boost',)


def is_v2(profile: dict) -> bool:
    return isinstance(profile, dict) and profile.get('schema_version') == SCHEMA_VERSION


def _list(v):
    return list(v) if isinstance(v, (list, tuple)) else ([] if v in (None, '') else [v])


def _dict(v):
    return copy.deepcopy(v) if isinstance(v, dict) else {}


def _education(v):
    v = str(v or '').strip()
    return v if v in EDUCATION_ENUM else '其他'


def to_v2(profile: dict, profile_id: str, created_at: str = None, updated_at: str = None) -> dict:
    """v1（或已是 v2）→ v2。已是 v2 时补 id/时间戳后原样返回副本。"""
    if is_v2(profile):
        out = copy.deepcopy(profile)
        out.setdefault('id', profile_id)
        if created_at:
            out.setdefault('created_at', created_at)
        if updated_at:
            out['updated_at'] = updated_at
        return out

    meta = profile.get('meta') or {}
    dims = profile.get('dimensions') or {}

    out = {
        'schema_version': SCHEMA_VERSION,
        'id': profile_id,
        'identity': {
            'display_name': meta.get('name', ''),
            'total_years': meta.get('total_years', 0),
            'education': _education(meta.get('education_max')),
            'languages': _dict(meta.get('language_levels')),
            'certifications': _list(meta.get('certifications')),
        },
        'targets': {
            'roles': _list(meta.get('target_roles')),
            'titles': _list(meta.get('target_titles')),
            'exclude_title_keywords': _list(meta.get('exclude_titles_keywords')),
            'exclude_role_keywords': _list(meta.get('exclude_assistant_types')),
            'cities': _list(meta.get('preferred_cities')),
            'accept_remote': bool(meta.get('accept_remote', False)),
            'salary': {
                'min_k': meta.get('expected_salary_min_k'),
                'max_k': meta.get('expected_salary_max_k'),
            },
            'welfare_floor': _list(meta.get('welfare_floor')),
        },
        'experience': {
            'industries': copy.deepcopy(meta.get('industry_experience') or []),
            'role_years': _dict(meta.get('role_accumulation')),
        },
        'capabilities': {
            'dimensions': {str(k): int(v) for k, v in dims.items()},
            'confidence': 'self',
        },
        'filters': {
            'exclude_industries': _list(meta.get('hard_filter_industries')),
            'exclude_certifications': _list(meta.get('no_certifications')),
            'domain_coverage': _list(profile.get('domain_coverage')),
            'domain_blacklist': copy.deepcopy(profile.get('domain_blacklist') or []),
        },
    }
    if created_at:
        out['created_at'] = created_at
    if updated_at:
        out['updated_at'] = updated_at
    return out


def to_legacy_profile(profile: dict) -> dict:
    """v2 → v1 形状（喂给 matcher.py 的 `profile` 参数）；v1 原样返回副本。"""
    if not is_v2(profile):
        return copy.deepcopy(profile)

    ident = profile.get('identity') or {}
    tgt = profile.get('targets') or {}
    sal = tgt.get('salary') or {}
    exp = profile.get('experience') or {}
    cap = profile.get('capabilities') or {}
    flt = profile.get('filters') or {}

    meta = {
        'name': ident.get('display_name', ''),
        'target_titles': _list(tgt.get('titles')),
        'expected_salary_min_k': sal.get('min_k'),
        'expected_salary_max_k': sal.get('max_k'),
        'preferred_cities': _list(tgt.get('cities')),
        'accept_remote': bool(tgt.get('accept_remote', False)),
        'exclude_titles_keywords': _list(tgt.get('exclude_title_keywords')),
        'target_roles': _list(tgt.get('roles')),
        'exclude_assistant_types': _list(tgt.get('exclude_role_keywords')),
        'welfare_floor': _list(tgt.get('welfare_floor')),
        'education_max': ident.get('education', '其他'),
        'total_years': ident.get('total_years', 0),
        'language_levels': _dict(ident.get('languages')),
        'industry_experience': copy.deepcopy(exp.get('industries') or []),
        'role_accumulation': _dict(exp.get('role_years')),
        'hard_filter_industries': _list(flt.get('exclude_industries')),
        'no_certifications': _list(flt.get('exclude_certifications')),
        'certifications': _list(ident.get('certifications')),
    }

    return {
        'meta': meta,
        'dimensions': {str(k): int(v) for k, v in (cap.get('dimensions') or {}).items()},
        'domain_coverage': _list(flt.get('domain_coverage')),
        'domain_blacklist': copy.deepcopy(flt.get('domain_blacklist') or []),
        # T-1：profile 侧 ai_boost 从未被引擎读取；回填 False 以保持与迁移后 prefs 一致
        'ai_boost': {'enabled': bool((profile.get('ai_boost') or {}).get('enabled', False)),
                     'scope': (profile.get('ai_boost') or {}).get('scope', 'title')},
    }


def diff_legacy(old: dict, new_projection: dict, ignore=()):
    """返回 (differences:list[str], ignored:list[str])，用于 I1 断言输出。"""
    diffs, ignored = [], []

    def walk(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                p = f'{path}.{k}' if path else k
                if k in ignore and path == '':
                    ignored.append(p)
                    continue
                if k not in a:
                    diffs.append(f'{p}: 新增（旧文件没有）')
                elif k not in b:
                    diffs.append(f'{p}: 丢失（新结构没有）')
                else:
                    walk(a[k], b[k], p)
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                diffs.append(f'{path}: 长度 {len(a)} != {len(b)}')
            else:
                for i, (x, y) in enumerate(zip(a, b)):
                    walk(x, y, f'{path}[{i}]')
        else:
            if a != b or type(a) is not type(b):
                diffs.append(f'{path}: 值/类型不同')

    walk(old, new_projection, '')
    return diffs, ignored
