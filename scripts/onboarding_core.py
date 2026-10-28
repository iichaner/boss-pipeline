#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""onboarding_core.py —— Onboarding 步进状态机 / 答案转换 / 样例试判定（P4）。

CLI（onboard.py）与 Dashboard（onboarding_api.py）共用本模块，保证两条通道
的进度、校验结果、落盘产物完全一致。

设计依据
--------
* DESIGN.md F-P4、D.2、D.3
* R1.2 五步：环境自检 → 偏好配置 → 试判定 → 试爬取（可选）→ 试打招呼（强制 dry-run）
* R1.3 断点续做：`<home>/state/onboarding_state.json`
* R1.4 完成态：写 profile.json / prefs.yaml / published.json + config.onboarding.completed

隐私
----
本模块只读写用户本机 `~/.boss-pipeline`；不引入任何网络依赖；日志/返回值只含
结构化统计与用户自己填写的字段，不回显 ii 的画像。
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config_paths as cp  # noqa: E402
import profile_adapter as pa  # noqa: E402

STATE_NAME = 'onboarding_state.json'
SCHEMA_VERSION = 1
STEPS = 5
STEP_LABELS = {
    1: '环境自检',
    2: '偏好配置',
    3: '试判定',
    4: '试爬取（可选）',
    5: '试打招呼（dry-run）',
}
EDU_ENUM = ('大专', '本科', '硕士', '博士', '其他')
PROFILE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')

#: 向导暴露的 6 个中性维度（来自 templates/families/generic.yaml 的 dimensions_vocab）。
NEUTRAL_DIMENSIONS = [
    '专业能力', '项目与流程管理', '数据能力', '沟通协作', '团队管理', '行业知识',
]

DEFAULT_GREETING = (
    '您好，看到贵司在招{job}，我的经历与岗位要求比较匹配，'
    '方便的话想和您进一步沟通，谢谢！'
)


class OnboardingError(cp.ConfigError):
    """向导可读错误（API 映射为 400，CLI 打印一行）。"""


# ── 基础 ──────────────────────────────────────────────────
def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _home(create: bool = False):
    try:
        return cp.home(create=create)
    except Exception:
        return None


def state_path(create: bool = False) -> Path:
    """向导草稿状态：`<home>/state/onboarding_state.json`。

    画像尚未创建时也要能续做，因此不放在 profiles/<id>/ 下；commit 成功后会
    额外镜像一份到 `profiles/<id>/state/onboarding_state.json`（DESIGN B.1）。
    显式 `BOSS_PIPELINE_HOME` 优先，即使目录尚未创建也不能读到别的 home。
    """
    env = os.environ.get(cp.ENV_HOME)
    if env:
        root = cp._expand(env)
    else:
        root = _home(create=create)
        if root is None:
            root = cp._expand(cp.DEFAULT_HOME)
    d = root / 'state'
    if create:
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return d / STATE_NAME


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


# ── 状态机 ────────────────────────────────────────────────
def default_state() -> dict:
    return {
        'schema_version': SCHEMA_VERSION,
        'profile_id': None,
        'step': 1,
        'completed_steps': [],
        'answers': {},
        'ready': False,
        'completed': False,
        'published_profile_id': None,
        'created_at': _now(),
        'updated_at': _now(),
        'completed_at': None,
    }


def load_state() -> dict:
    data = _read_json(state_path(), None)
    if not isinstance(data, dict):
        return default_state()
    st = default_state()
    st.update(data)
    try:
        st['step'] = max(1, min(STEPS, int(st.get('step') or 1)))
    except Exception:
        st['step'] = 1
    if not isinstance(st.get('completed_steps'), list):
        st['completed_steps'] = []
    if not isinstance(st.get('answers'), dict):
        st['answers'] = {}
    return st


def save_state(st: dict) -> None:
    st = dict(st)
    st['updated_at'] = _now()
    root = _home(create=True)
    if root is None:
        raise OnboardingError('无法创建 ~/.boss-pipeline（检查目录权限）')
    text = json.dumps(st, ensure_ascii=False, indent=2) + '\n'
    with cp.locked(root / 'state' / 'locks' / 'profile.lock'):
        cp.atomic_write(state_path(create=True), text, mode=0o600)


def status() -> dict:
    """Dashboard / CLI 共用的入口判断（DESIGN G3）。"""
    st = load_state()
    try:
        cfg = cp.load_config()
    except Exception:
        cfg = {}
    reason = None
    needs = False
    pid = st.get('profile_id')
    published = False
    valid = False

    root = _home()
    if root is None:
        needs, reason = True, 'no_home'
    else:
        try:
            explicit = pid if pid and (cp.profiles_dir() / pid).is_dir() else None
            pid = cp.resolve_profile_id(explicit)
            cp.load_profile(pid)
            cp.load_prefs(pid)
            valid = True
            published = cp.is_published(pid)
            if not published:
                needs, reason = True, 'profile_unpublished'
        except cp.NeedOnboarding:
            needs, reason = True, 'no_profile'
        except cp.ProfileNotPublished:
            needs, reason = True, 'profile_unpublished'
        except Exception:
            needs, reason = True, 'profile_invalid'

    completed_flag = bool(((cfg.get('onboarding') or {}).get('completed')))
    completed = bool(valid and published and completed_flag)
    if valid and published and not completed_flag:
        # 已发布但未标记完成：老用户（迁移前手工建的画像）按 G3 仍要求补一次完成态
        needs, reason = True, 'not_completed'
    if completed:
        needs, reason = False, None
    if st.get('completed') and valid and published:
        needs, reason, completed = False, None, True

    return {
        'needs_onboarding': bool(needs),
        'profile_id': pid,
        'step': int(st.get('step') or 1),
        'completed': completed,
        'reason': reason,
        'steps': [{'step': i, 'label': STEP_LABELS[i],
                   'done': i in (st.get('completed_steps') or [])} for i in range(1, STEPS + 1)],
        'answers': st.get('answers') or {},
        'updated_at': st.get('updated_at'),
    }


# ── 答案校验 ──────────────────────────────────────────────
def _num(v, name, lo, hi, errors, allow_none=False):
    if v is None or v == '':
        if allow_none:
            return None
        errors.append(f'{name} 必填')
        return None
    try:
        n = float(v)
    except Exception:
        errors.append(f'{name} 必须是数字')
        return None
    if n < lo or n > hi:
        errors.append(f'{name} 必须在 {lo}–{hi} 之间')
        return None
    return int(n) if float(n).is_integer() else n


def _list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r'[,，、;；\s]+', str(v)) if x.strip()]


def validate_step(step: int, answers: dict, st: dict) -> list:
    answers = answers or {}
    if step == 1:
        return []
    if step == 2:
        return _validate_profile_answers(answers)
    if step == 3:
        return []
    if step == 4:
        url = str(answers.get('url') or '').strip()
        if url and not url.startswith(('http://', 'https://')):
            return ['页面 URL 必须以 http:// 或 https:// 开头']
        return []
    if step == 5:
        if answers.get('dry_run') is not True:
            return ['第 5 步只允许 dry-run（后端校验 dry_run=True），不提供真发开关']
        return []
    return [f'未知步骤：{step}']


def _validate_profile_answers(a: dict) -> list:
    errors = []
    pid = str(a.get('profile_id') or 'me').strip().lower()
    if not PROFILE_ID_RE.match(pid):
        errors.append('画像 id 只允许小写字母/数字/下划线/短横线（≤32 位）')

    ident = a.get('identity') or {}
    if not str(ident.get('display_name') or '').strip():
        errors.append('称呼（display_name）必填')
    elif len(str(ident.get('display_name'))) > 40:
        errors.append('称呼不能超过 40 字')
    _num(ident.get('total_years'), '工作年限', 0, 60, errors)
    if ident.get('education') not in EDU_ENUM:
        errors.append('学历必须是：' + ' / '.join(EDU_ENUM))
    langs = ident.get('languages') or {}
    if not isinstance(langs, dict):
        errors.append('语言等级必须是 {语种: 0-5}')
    else:
        for k, v in langs.items():
            try:
                iv = int(v)
            except Exception:
                errors.append(f'语言等级「{k}」必须是 0–5 的整数')
                continue
            if iv < 0 or iv > 5:
                errors.append(f'语言等级「{k}」必须在 0–5 之间')

    tgt = a.get('targets') or {}
    roles = _list(tgt.get('roles'))
    if not roles:
        errors.append('目标岗位至少填 1 个')
    for r in roles:
        if len(r) < 2 or len(r) > 20:
            errors.append(f'目标岗位「{r}」长度需在 2–20 字')
    cities = _list(tgt.get('cities'))
    if not cities:
        errors.append('期望城市至少填 1 个')
    sal = tgt.get('salary') or {}
    lo = _num(sal.get('min_k'), '期望薪资下限', 1, 500, errors)
    hi = _num(sal.get('max_k'), '期望薪资上限', 1, 500, errors)
    if lo is not None and hi is not None and lo > hi:
        errors.append('期望薪资下限不能大于上限')

    caps = (a.get('capabilities') or {}).get('dimensions') or {}
    if not isinstance(caps, dict):
        errors.append('能力自评必须是 {维度: 0-5}')
        caps = {}
    if len(caps) < 3:
        errors.append('至少自评 3 个能力维度')
    for k, v in caps.items():
        try:
            iv = int(v)
        except Exception:
            errors.append(f'维度「{k}」自评必须是 0–5 的整数')
            continue
        if iv < 0 or iv > 5:
            errors.append(f'维度「{k}」自评必须在 0–5 之间')

    kw = a.get('dimension_keywords') or {}
    nonempty = [k for k, v in kw.items() if _list(v)]
    if len(nonempty) < 3:
        errors.append('至少为 3 个维度各填 1 个以上关键词（否则样例判定会全空）')
    for k in caps:
        if k not in kw or not _list(kw.get(k)):
            errors.append(f'维度「{k}」缺少关键词（每个自评维度都要有词）')
    for k in nonempty:
        if k not in caps:
            errors.append(f'关键词维度「{k}」不在能力自评里')

    inds = (a.get('experience') or {}).get('industries') or []
    if not isinstance(inds, list):
        errors.append('行业经历必须是数组')
    else:
        for i, it in enumerate(inds):
            if not isinstance(it, dict) or not str(it.get('industry') or '').strip():
                errors.append(f'行业经历第 {i + 1} 条缺少行业名')
    return errors


# ── 答案 → profile / prefs ────────────────────────────────
def _profile_id_from(st_or_answers: dict) -> str:
    a = st_or_answers.get('answers') if 'answers' in st_or_answers else st_or_answers
    return str((a or {}).get('profile_id') or 'me').strip().lower()


def build_profile(st: dict) -> dict:
    a = st.get('answers') or {}
    pid = _profile_id_from(a)
    ident = a.get('identity') or {}
    tgt = a.get('targets') or {}
    sal = tgt.get('salary') or {}
    flt = a.get('filters') or {}
    exp = a.get('experience') or {}
    caps = (a.get('capabilities') or {}).get('dimensions') or {}
    now = _now()

    blacklist = []
    for item in (flt.get('domain_blacklist') or []):
        if isinstance(item, dict):
            blacklist.append({'keyword': str(item.get('keyword') or ''),
                              'reason': str(item.get('reason') or '用户排除')[:60]})
        elif str(item).strip():
            blacklist.append({'keyword': str(item).strip(), 'reason': '用户排除'})

    industries = []
    for it in (exp.get('industries') or []):
        if not isinstance(it, dict) or not str(it.get('industry') or '').strip():
            continue
        industries.append({
            'industry': str(it.get('industry')).strip(),
            'years': it.get('years') or 0,
            'roles': _list(it.get('roles')),
        })

    return {
        'schema_version': 2,
        'id': pid,
        'created_at': st.get('created_at') or now,
        'updated_at': now,
        'identity': {
            'display_name': str(ident.get('display_name') or '').strip(),
            'total_years': ident.get('total_years') or 0,
            'education': ident.get('education') or '其他',
            'languages': {str(k): int(v) for k, v in (ident.get('languages') or {}).items()},
            'certifications': _list(ident.get('certifications')),
        },
        'targets': {
            'roles': _list(tgt.get('roles')),
            'titles': _list(tgt.get('roles')),
            'exclude_title_keywords': _list(tgt.get('exclude_title_keywords')),
            'exclude_role_keywords': _list(tgt.get('exclude_role_keywords')),
            'cities': _list(tgt.get('cities')),
            'accept_remote': bool(tgt.get('accept_remote', False)),
            'salary': {'min_k': sal.get('min_k'), 'max_k': sal.get('max_k')},
            'welfare_floor': [],
        },
        'experience': {
            'industries': industries,
            'role_years': {},
        },
        'capabilities': {
            'dimensions': {str(k): int(v) for k, v in caps.items()},
            'confidence': 'self',
        },
        'filters': {
            'exclude_industries': _list(flt.get('exclude_industries')),
            'exclude_certifications': _list(flt.get('exclude_certifications')),
            'domain_coverage': [],
            'domain_blacklist': blacklist,
        },
    }


def build_prefs(st: dict) -> dict:
    """稀疏 prefs：只写用户答过的项，其余继承 generic（DESIGN D-2）。"""
    a = st.get('answers') or {}
    kw = a.get('dimension_keywords') or {}
    defs = a.get('dimension_definitions') or {}
    dims = (a.get('capabilities') or {}).get('dimensions') or {}

    taxonomy = []
    for name in dims:
        words = _list(kw.get(name))
        if words:
            taxonomy.append({'dimension': name, 'keywords': words})
    # 保持与 generic 一致的顺序（先工程维度，后用户自定义）
    order = {n: i for i, n in enumerate(NEUTRAL_DIMENSIONS)}
    taxonomy.sort(key=lambda e: order.get(e['dimension'], 99))

    rules = {
        'taxonomy': taxonomy,
        'dimension_definitions': {k: str(v) for k, v in defs.items()
                                  if str(v or '').strip()},
        # 空对象 = 全部继承 generic 的通用硬过滤开关
        'hard_filters': {},
        # 新用户不设岗位族标题闸门（空词表 = 关闭，避免误杀）
        'title_bias': {'strong_keywords': [], 'senior_title_keywords': [],
                       'core_dimensions': []},
        'dimension_aliases': {},
    }
    engine = copy.deepcopy(a.get('engine') or {})
    return {
        'schema_version': 2,
        'extends': 'generic',
        'engine': engine,
        'rules': rules,
    }


def effective_for(prefs: dict) -> dict:
    """与 cp.effective_config 相同的合并顺序，但吃内存里的 prefs（草稿态）。"""
    merged = {}
    for layer in cp._default_layers(prefs):
        merged = cp._deep_merge(merged, layer)
    merged = cp._deep_merge(merged, prefs.get('engine') or {})
    merged = cp._deep_merge(merged, prefs.get('rules') or {})
    return merged


# ── 样例试判定 ────────────────────────────────────────────
def _matcher_module():
    d = str(cp.matcher_scripts_dir())
    if d not in sys.path:
        sys.path.insert(0, d)
    import matcher  # noqa: F401
    return matcher


def _samples() -> list:
    p = cp.skill_dir(cp.SKILL_MATCHER) / 'templates' / 'samples' / 'jd_onboarding.json'
    data = _read_json(p, {}) or {}
    return data.get('jds') or []


def preview(profile: dict = None, prefs: dict = None) -> dict:
    """用 3 条内置中性 JD 跑判定；返回档位 + 命中维度 + 硬过滤原因。"""
    st = load_state()
    if profile is None:
        profile = build_profile(st)
    if prefs is None:
        prefs = build_prefs(st)
    errors = _validate_profile_answers(_answers_from(profile, prefs, st))
    if errors:
        return {'items': [], 'spread_ok': False, 'errors': errors}

    matcher = _matcher_module()
    eff = effective_for(prefs)
    legacy = pa.to_legacy_profile(profile)
    m = matcher.Matcher(legacy, eff)

    items = []
    for raw in _samples():
        jd = matcher.enrich_jd(raw)
        result = m.evaluate(jd)
        filtered_by = result.get('hard_filter')
        hits = []
        if not filtered_by:
            try:
                extracted, _conf, _n = m.extract_items(jd)
                sc = m.score(extracted)
                hits = [d['dimension'] for d in sc.get('details') or []]
            except Exception:
                hits = []
        items.append({
            'job_id': raw.get('job_id'),
            'jd_title': raw.get('title'),
            'level': result.get('优先级判断'),
            'match_pct': result.get('匹配度'),
            'reason': result.get('优先级判断理由'),
            'hits': hits,
            'filtered_by': filtered_by,
        })
    levels = [i['level'] for i in items if not i['filtered_by']]
    return {
        'items': items,
        'spread_ok': len(set(levels)) >= 2,
        'levels': levels,
        'errors': [],
    }


def _answers_from(profile: dict, prefs: dict, st: dict) -> dict:
    """把内存 profile/prefs 反推成答案形状，复用同一套校验。"""
    if profile is None or prefs is None:
        return (st.get('answers') or {})
    rules = prefs.get('rules') or {}
    taxonomy = rules.get('taxonomy') or []
    return {
        'profile_id': profile.get('id'),
        'identity': {
            'display_name': (profile.get('identity') or {}).get('display_name'),
            'total_years': (profile.get('identity') or {}).get('total_years'),
            'education': (profile.get('identity') or {}).get('education'),
            'languages': (profile.get('identity') or {}).get('languages'),
            'certifications': (profile.get('identity') or {}).get('certifications'),
        },
        'targets': {
            'roles': (profile.get('targets') or {}).get('roles'),
            'cities': (profile.get('targets') or {}).get('cities'),
            'salary': (profile.get('targets') or {}).get('salary'),
            'accept_remote': (profile.get('targets') or {}).get('accept_remote'),
            'exclude_title_keywords': (profile.get('targets') or {}).get('exclude_title_keywords'),
            'exclude_role_keywords': (profile.get('targets') or {}).get('exclude_role_keywords'),
        },
        'filters': profile.get('filters') or {},
        'experience': profile.get('experience') or {},
        'capabilities': profile.get('capabilities') or {},
        'dimension_keywords': {t.get('dimension'): t.get('keywords') for t in taxonomy},
        'dimension_definitions': rules.get('dimension_definitions') or {},
        'engine': prefs.get('engine') or {},
    }


# ── 提交答案 ──────────────────────────────────────────────
def submit_step(step: int, answers: dict) -> dict:
    st = load_state()
    try:
        step = int(step)
    except Exception:
        return {'ok': False, 'step': st['step'], 'next_step': st['step'],
                'errors': ['step 必须是 1–5']}
    if step < 1 or step > STEPS:
        return {'ok': False, 'step': st['step'], 'next_step': st['step'],
                'errors': ['step 必须是 1–5']}
    if step > st['step']:
        return {'ok': False, 'step': st['step'], 'next_step': st['step'],
                'errors': [f'请先完成第 {st["step"]} 步（不能跳步）']}

    errors = validate_step(step, answers, st)
    if errors:
        return {'ok': False, 'step': st['step'], 'next_step': st['step'], 'errors': errors}

    merged = copy.deepcopy(st.get('answers') or {})
    merged.update(copy.deepcopy(answers or {}))
    if step == 2:
        merged['profile_id'] = _profile_id_from(answers or {})
        st['profile_id'] = merged['profile_id']
    st['answers'] = merged
    if step not in st['completed_steps']:
        st['completed_steps'].append(step)
        st['completed_steps'].sort()
    if step < STEPS:
        st['step'] = step + 1
    else:
        st['step'] = STEPS
        st['ready'] = True
    save_state(st)

    resp = {'ok': True, 'step': step, 'next_step': st['step'], 'errors': []}
    if step == 3:
        resp['preview'] = preview(build_profile(st), build_prefs(st))
    if step == 5:
        resp['dry_run'] = dry_run_plan(st)
    return resp


def dry_run_plan(st: dict = None) -> dict:
    """第 5 步 dry-run：只生成计划，不调用爬虫/不发送任何消息。"""
    st = st or load_state()
    pv = preview(build_profile(st), build_prefs(st))
    targets = [i for i in pv.get('items') or [] if i['level'] in ('high', 'medium')]
    message = DEFAULT_GREETING
    return {
        'dry_run': True,
        'sent': 0,
        'would_send': len(targets),
        'targets': [{'job_id': t['job_id'], 'jd_title': t['jd_title'], 'level': t['level']}
                    for t in targets],
        'message_preview': message,
        'note': 'dry-run 只展示计划，本步骤不会真实发送任何招呼',
    }


# ── 校验 + 发布 ───────────────────────────────────────────
def _run_validator(profile_path: Path, prefs_path: Path):
    target = cp.skill_script(cp.SKILL_MATCHER, 'scripts/validate_profile.py')
    try:
        out = subprocess.run(
            [sys.executable, str(target), str(profile_path), '--prefs', str(prefs_path), '--json'],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        return 2, {'errors': [{'code': 'E-EXEC', 'path': '', 'message': str(e)}],
                   'warnings': [], 'exit': 2}
    try:
        return out.returncode, json.loads(out.stdout or '{}')
    except Exception:
        return out.returncode, {'errors': [{'code': 'E-OUTPUT', 'path': '',
                                            'message': (out.stderr or out.stdout or '')[:300]}],
                                 'warnings': [], 'exit': out.returncode}


def _next_profile_id(base: str, root: Path) -> str:
    pid, n = base, 2
    while (root / 'profiles' / pid).exists():
        pid = f'{base}-{n}'
        n += 1
        if n > 50:
            raise OnboardingError('同名画像过多，请换一个画像 id')
    return pid


def commit(profile: dict = None, prefs: dict = None, finalize: bool = True) -> dict:
    """原子发布 profile.json / prefs.yaml / published.json + config.json。"""
    st = load_state()
    if profile is None:
        profile = build_profile(st)
    if prefs is None:
        prefs = build_prefs(st)
    errors = _validate_profile_answers(_answers_from(profile, prefs, st))
    if errors:
        return {'ok': False, 'profile_id': None, 'warnings': [], 'errors': errors}
    if finalize and not st.get('ready'):
        return {'ok': False, 'profile_id': None, 'warnings': [],
                'errors': ['请先完成第 5 步 dry-run，再提交']}

    root = _home(create=True)
    if root is None:
        return {'ok': False, 'profile_id': None, 'warnings': [],
                'errors': ['无法创建 ~/.boss-pipeline（检查目录权限）']}
    base = str(profile.get('id') or 'me').lower()
    if not PROFILE_ID_RE.match(base):
        return {'ok': False, 'profile_id': None, 'warnings': [],
                'errors': ['画像 id 非法']}

    with cp.locked(root / 'state' / 'locks' / 'profile.lock'):
        pid = st.get('published_profile_id') or _next_profile_id(base, root)
        pdir = root / 'profiles' / pid
        pdir.mkdir(parents=True, exist_ok=True)
        _chmod(pdir, 0o700)
        _chmod(pdir.parent, 0o700)

        profile['id'] = pid
        p_text = json.dumps(profile, ensure_ascii=False, indent=2) + '\n'
        y_text = cp._yaml().safe_dump(prefs, allow_unicode=True, sort_keys=False,
                                      default_flow_style=False, width=1000)

        tmp_profile = pdir / '.profile.json.tmp'
        tmp_prefs = pdir / '.prefs.yaml.tmp'
        tmp_profile.write_text(p_text, encoding='utf-8')
        tmp_prefs.write_text(y_text, encoding='utf-8')

        code, report = _run_validator(tmp_profile, tmp_prefs)
        errs = report.get('errors') or []
        if code != 0 or errs:
            _unlink(tmp_profile)
            _unlink(tmp_prefs)
            return {'ok': False, 'profile_id': pid, 'warnings':
                    [w.get('message') for w in report.get('warnings') or []],
                    'errors': [f'{e.get("code")} {e.get("path")} {e.get("message")}'
                               for e in errs] or ['validate_profile 校验失败']}

        os.replace(tmp_profile, pdir / 'profile.json')
        os.replace(tmp_prefs, pdir / 'prefs.yaml')
        _chmod(pdir / 'profile.json', 0o600)
        _chmod(pdir / 'prefs.yaml', 0o600)

        # 统一发布标记（与迁移共用）
        pub = {
            'schema_version': 1,
            'profile_id': pid,
            'published_at': _now(),
            'source': 'onboarding',
        }
        cp.atomic_write(pdir / cp.PUBLISHED_NAME,
                        json.dumps(pub, ensure_ascii=False, indent=2) + '\n', mode=0o600)
        (pdir / 'state').mkdir(parents=True, exist_ok=True)
        (pdir / 'state' / 'last_used_at').write_text(_now(), encoding='utf-8')

        cfg = cp.load_config()
        cfg.setdefault('schema_version', 1)
        cfg['active_profile'] = pid
        if not cfg.get('work_root'):
            cfg['work_root'] = str(root / 'work')
        cfg.setdefault('dashboard', {'port': 8765})
        cfg['onboarding'] = {
            'completed': True,
            'completed_at': _now(),
            'source': 'onboarding',
            'profile_id': pid,
        }
        cp.atomic_write(root / 'config.json',
                        json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', mode=0o600)

        st['published_profile_id'] = pid
        st['profile_id'] = pid
        st['completed'] = True
        st['completed_at'] = _now()
        st['ready'] = True
        save_state_locked(root, st, pdir)

    return {
        'ok': True,
        'profile_id': pid,
        'warnings': [w.get('message') for w in report.get('warnings') or []],
        'errors': [],
        'next': '回到主界面，粘贴 Boss 列表页 URL 开始爬取',
    }


def save_state_locked(root: Path, st: dict, pdir: Path) -> None:
    st = dict(st)
    st['updated_at'] = _now()
    text = json.dumps(st, ensure_ascii=False, indent=2) + '\n'
    cp.atomic_write(root / 'state' / STATE_NAME, text, mode=0o600)
    try:
        (pdir / 'state').mkdir(parents=True, exist_ok=True)
        cp.atomic_write(pdir / 'state' / STATE_NAME, text, mode=0o600)
    except OSError:
        pass


def _chmod(p: Path, mode: int) -> None:
    try:
        os.chmod(p, mode)
    except OSError:
        pass


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def reset(profile_id: str = None) -> dict:
    """只清向导草稿；已发布画像永不删除。"""
    removed = False
    p = state_path()
    if p.is_file():
        try:
            p.unlink()
            removed = True
        except OSError:
            pass
    note = '已清空向导进度' if removed else '没有可清理的向导进度'
    if profile_id:
        note += f'；已发布画像 {profile_id} 未删除'
    return {'ok': True, 'removed': removed, 'note': note}
