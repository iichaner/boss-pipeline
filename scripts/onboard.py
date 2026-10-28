#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""onboard.py —— 五步向导 CLI（P4）。

用法
----
    python3 scripts/onboard.py --status            # 只查看进度（不写文件）
    python3 scripts/onboard.py                     # 交互式；中断后自动续做
    python3 scripts/onboard.py --resume            # 同上（显式；会先自检并回到出错步骤）
    python3 scripts/onboard.py --step 2            # 回到第 2 步重填（保留已填内容）
    python3 scripts/onboard.py --answers a.json    # 非交互（脚本化 driver / CI）
    python3 scripts/onboard.py --reset             # 只清向导草稿，不删已发布画像

`--answers` 结构：{"2": {…偏好…}, "3": {}, "4": {"skipped": true}, "5": {"dry_run": true}}
第 2 步字段见 onboarding_core._validate_profile_answers。

退出码：0 成功 / 2 参数或校验错误 / 3 提交失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_check  # noqa: E402
import onboarding_core as oc  # noqa: E402


def cmd_status() -> int:
    st = oc.status()
    st_raw = oc.load_state()
    print('boss-pipeline 向导状态')
    print('─' * 42)
    if st['needs_onboarding']:
        print('结论：需要引导（needs_onboarding=true）')
        print(f'原因：{st.get("reason") or "unknown"}')
    else:
        print('结论：已就绪（needs_onboarding=false）')
    print(f'画像：{st.get("profile_id") or "（尚未创建）"}')
    print(f'当前步骤：第 {st["step"]} 步 / 共 {oc.STEPS} 步 · '
          f'{oc.STEP_LABELS[st["step"]]}')
    done = st_raw.get('completed_steps') or []
    print('已完成：' + ('、'.join(f'{i}.{oc.STEP_LABELS[i]}' for i in done) or '无'))
    print(f'状态文件：{oc.state_path()}')

    bad_step, bad_errors = oc.first_invalid_step(st_raw)
    if bad_step:
        print(f'⚠️ 草稿第 {bad_step} 步有 {len(bad_errors)} 处必须修正（否则 --resume 必败）：')
        for e in bad_errors[:8]:
            print(f'  - {e}')
        print(f'下一步：python3 scripts/onboard.py --step {bad_step}')
    elif st['needs_onboarding']:
        print('下一步：python3 scripts/onboard.py --resume')
    else:
        print('下一步：回到主界面，粘贴 Boss 列表页 URL 开始爬取')
    return 0


def _ask(prompt: str, default: str = '') -> str:
    global _EOF_SEEN
    suffix = f' [{default}]' if default != '' else ''
    try:
        v = input(f'{prompt}{suffix}：').strip()
    except EOFError:
        _EOF_SEEN = True
        return default
    return v or default


_EOF_SEEN = False


def _ask_list(prompt: str, default: str = '', min_len: int = 0, label: str = '这一项') -> list:
    raw = _ask(prompt + '（逗号分隔）', default)
    items = [x.strip() for x in raw.replace('，', ',').split(',') if x.strip()]
    while True:
        bad = [x for x in items if len(x) < min_len] if min_len else []
        if not bad:
            return items
        if _EOF_SEEN:
            print(f'  ↳ {label}「{bad[0]}」不符合要求；当前输入已结束，无法继续询问')
            return items
        print(f'  ↳ {label}「{bad[0]}」太短：每个至少 {min_len} 个字，请重填')
        raw = _ask(prompt + '（逗号分隔，重填）', default)
        items = [x.strip() for x in raw.replace('，', ',').split(',') if x.strip()]


def _ask_num(prompt: str, default, lo, hi, label: str = '') -> float:
    while True:
        raw = _ask(prompt, str(default))
        try:
            n = float(raw)
        except Exception:
            print(f'  ↳ {label or prompt} 必须是数字，请重填')
            continue
        if n < lo or n > hi:
            print(f'  ↳ {label or prompt} 必须在 {lo}–{hi} 之间，请重填')
            if _EOF_SEEN:
                return n
            continue
        return int(n) if float(n).is_integer() else n


def collect_step2(prev: dict = None) -> dict:
    prev = prev or {}
    p_ident = prev.get('identity') or {}
    p_tgt = prev.get('targets') or {}
    p_sal = p_tgt.get('salary') or {}
    p_flt = prev.get('filters') or {}
    p_dims = (prev.get('capabilities') or {}).get('dimensions') or {}
    p_kw = prev.get('dimension_keywords') or {}

    print('\n[2/5] 偏好配置（直接回车采用方括号内默认值；任何一项填错都会当场提示）')
    pid = _ask('画像 id（小写字母/数字/短横线）', prev.get('profile_id') or 'me').lower()
    while not oc.PROFILE_ID_RE.match(pid):
        print('  ↳ 画像 id 只允许小写字母/数字/下划线/短横线，且以字母或数字开头（≤32 位）')
        pid = _ask('画像 id（重填）', 'me').lower()
    name = _ask('怎么称呼你', p_ident.get('display_name') or '我').strip()
    while not name or len(name) > 40:
        print('  ↳ 称呼必填且不超过 40 字')
        name = _ask('怎么称呼你（重填）', '我').strip()
    years = _ask_num('工作年限（年）', p_ident.get('total_years') if p_ident.get('total_years') is not None else 3, 0, 60, '工作年限')
    edu = _ask('学历（大专/本科/硕士/博士/其他）', p_ident.get('education') or '本科')
    while edu not in oc.EDU_ENUM:
        print('  ↳ 学历必须是：' + ' / '.join(oc.EDU_ENUM))
        edu = _ask('学历（重填）', '本科')
    eng = _ask_num('英语等级 0–5（0=不会，3=可工作，5=母语级）',
                   (p_ident.get('languages') or {}).get('英语', 3), 0, 5, '英语等级')
    roles = _ask_list('目标岗位关键词（标题需命中其一）',
                      ', '.join(p_tgt.get('roles') or []) or '运营', 2, '目标岗位')
    while not roles:
        print('  ↳ 目标岗位至少填 1 个')
        roles = _ask_list('目标岗位关键词（重填）', '运营', 2, '目标岗位')
    cities = _ask_list('期望城市', ', '.join(p_tgt.get('cities') or []) or '上海', 2, '城市')
    smin = _ask_num('期望薪资下限（K）', p_sal.get('min_k') or 15, 1, 500, '期望薪资下限')
    smax = _ask_num('期望薪资上限（K）', p_sal.get('max_k') or 30, 1, 500, '期望薪资上限')
    while smin > smax:
        print('  ↳ 期望薪资下限不能大于上限，请重填上限')
        smax = _ask_num('期望薪资上限（K）', 30, 1, 500, '期望薪资上限')
    remote = _ask('接受远程？(y/n)', 'y' if p_tgt.get('accept_remote') else 'n').lower().startswith('y')
    ex_titles = _ask_list('排除的标题关键词（可空）',
                          ', '.join(p_tgt.get('exclude_title_keywords') or []))
    ex_roles = _ask_list('排除的岗位类型（可空，例：电话销售/外包）',
                         ', '.join(p_tgt.get('exclude_role_keywords') or []))
    ex_ind = _ask_list('排除的行业（可空）',
                       ', '.join(p_flt.get('exclude_industries') or []))

    print('\n能力自评：0=没有 1=了解 2=能执行 3=能独立负责 4=能搭体系 5=能定战略')
    dims, keywords, defs = {}, {}, {}
    for name_dim in oc.NEUTRAL_DIMENSIONS:
        lvl = _ask_num(f'  {name_dim} 自评', p_dims.get(name_dim, 0), 0, 5, f'{name_dim} 自评')
        if lvl in (0, 0.0) or str(lvl) == '0':
            continue
        words = _ask_list(f'  {name_dim} 的关键词（至少 1 个，例如流程/复盘）',
                          ', '.join(p_kw.get(name_dim) or []), 2, f'{name_dim} 的关键词')
        if not words:
            print('    ↳ 没有关键词会跳过该维度')
            continue
        dims[name_dim] = lvl
        keywords[name_dim] = words
        d = _ask_optional_definition(name_dim)
        if d:
            defs[name_dim] = d

    return {
        'profile_id': pid,
        'identity': {
            'display_name': name,
            'total_years': years,
            'education': edu,
            'languages': {'英语': eng},
            'certifications': [],
        },
        'targets': {
            'roles': roles,
            'cities': cities,
            'salary': {'min_k': smin, 'max_k': smax},
            'accept_remote': remote,
            'exclude_title_keywords': ex_titles,
            'exclude_role_keywords': ex_roles,
        },
        'filters': {'exclude_industries': ex_ind, 'domain_blacklist': []},
        'experience': {'industries': []},
        'capabilities': {'dimensions': dims},
        'dimension_keywords': keywords,
        'dimension_definitions': defs,
        'engine': {},
    }


def _ask_optional_definition(name_dim: str) -> str:
    d = _ask(f'  {name_dim} 一句话定义（可空，例：带 5 人团队做交付）', '')
    return d.strip()


def _print_errors(title: str, errors) -> None:
    print(title)
    for e in errors or []:
        print('  -', e)


def run_interactive(step_override: int = None) -> int:
    if step_override:
        oc.rewind_to(step_override)
    else:
        # A1：--resume 先自检已保存的答案；有必败字段就回到出错步骤，不原样重试。
        rep = oc.repair_state()
        if rep:
            print(f'⚠️ 上次保存的答案有 {len(rep["errors"])} 处必须修正，已回到第 {rep["step"]} 步：')
            _print_errors('', rep['errors'][:10])

    st = oc.load_state()
    if st.get('completed') and not step_override:
        print(f'✅ 已完成配置（画像 {st.get("profile_id") or "当前"}）。')
        print('想修改：python3 scripts/onboard.py --step 2')
        return 0

    print(f'从第 {st["step"]} 步继续（共 {oc.STEPS} 步）')
    while st['step'] <= oc.STEPS and not st.get('ready'):
        step = st['step']
        print(f'\n[{step}/{oc.STEPS}] {oc.STEP_LABELS[step]}')

        if step == 1:
            report = env_check.run_checks()
            print(env_check.format_text(report))
            if report['summary']['blocking_failed']:
                print('存在阻塞项（Python 依赖），请先按上面的修复命令处理后重跑。')
                return 2
            if not _ask('继续？(Y/n)', 'y').lower().startswith('y'):
                return 0
            answers = {}
        elif step == 2:
            answers = collect_step2(st.get('answers'))
            errs = oc.validate_step(2, answers, st)
            if errs:
                _print_errors('还有字段不合格，请按提示修正：', errs)
                if _EOF_SEEN:
                    print('输入已结束（非交互环境）。修复后重跑：python3 scripts/onboard.py --step 2')
                    return 2
                continue
        elif step == 3:
            res = oc.preview(oc.build_profile(st), oc.build_prefs(st))
            _print_preview(res)
            if not res.get('spread_ok'):
                print('提示：样例方向与你的目标岗位不同时，全 low 属正常；可继续。')
            if _ask('按回车记录本步并继续（b=回第 2 步修改）', '').strip().lower() == 'b':
                oc.rewind_to(2)
                st = oc.load_state()
                continue
            answers = {}
        elif step == 4:
            print('可选：起浏览器 → 登录 → 粘贴列表页 URL，只会爬 5 条。')
            raw = _ask('列表页 URL（直接回车跳过，b=回第 3 步）', '')
            if raw.strip().lower() == 'b':
                oc.rewind_to(3)
                st = oc.load_state()
                continue
            answers = {'url': raw} if raw else {'skipped': True}
        else:
            plan = oc.dry_run_plan(st)
            print(f'dry-run：将向 {plan["would_send"]} 条高/中优先级岗位打招呼（不会真发）')
            if _ask('按回车确认 dry-run 并完成（b=回第 4 步）', '').strip().lower() == 'b':
                oc.rewind_to(4)
                st = oc.load_state()
                continue
            answers = {'dry_run': True}

        res = oc.submit_step(step, answers)
        if not res.get('ok'):
            _print_errors('校验未通过：', res.get('errors'))
            fix = res.get('fix_step')
            if fix and fix != step:
                print(f'→ 已回到第 {fix} 步，请按上面的中文提示修正')
                oc.rewind_to(fix)
                st = oc.load_state()
                continue
            if step == 2:
                continue
            return 2
        st = oc.load_state()

    result = oc.commit()
    if not result.get('ok'):
        _print_errors('提交失败：', result.get('errors'))
        fix = result.get('fix_step')
        if fix:
            print(f'→ 已回到第 {fix} 步，进度已保存；修正后重跑：'
                  f'python3 scripts/onboard.py --resume')
            oc.rewind_to(fix)
        return 3
    print(f'\n✅ 已创建画像 {result["profile_id"]}')
    for w in result.get('warnings') or []:
        print('  ⚠️', w)
    print(f'下一步：{result.get("next")}')
    return 0


def run_answers(path: str) -> int:
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    except Exception as e:
        print(f'读取答案文件失败：{e}', file=sys.stderr)
        return 2
    st = oc.load_state()
    rep = oc.repair_state()
    if rep:
        print(f'⚠️ 草稿第 {rep["step"]} 步有 {len(rep["errors"])} 处需修正，先回到该步重校：', file=sys.stderr)
        for e in rep['errors'][:10]:
            print('  -', e, file=sys.stderr)
    st = oc.load_state()
    while not st.get('ready') and st['step'] <= oc.STEPS:
        step = st['step']
        answers = payload.get(str(step))
        if answers is None:
            if step == 2:
                print(f'答案文件缺少第 {step} 步（必需）', file=sys.stderr)
                return 2
            answers = {}
        if step == 1:
            report = env_check.run_checks()
            print(f'[1/5] 环境自检：{report["summary"]["passed"]}/{report["summary"]["total"]} 项通过')
            if report['summary']['blocking_failed']:
                print('存在阻塞项（Python 依赖），请先修复', file=sys.stderr)
                return 2
        res = oc.submit_step(step, answers)
        if not res.get('ok'):
            print(f'第 {step} 步校验失败：', file=sys.stderr)
            for e in res.get('errors') or []:
                print('  -', e, file=sys.stderr)
            fix = res.get('fix_step')
            if fix and fix != step and str(fix) in payload:
                print(f'→ 已回到第 {fix} 步，用答案文件里的第 {fix} 步重新提交', file=sys.stderr)
                oc.rewind_to(fix)
                st = oc.load_state()
                continue
            return 2
        print(f'[ok] 第 {step} 步 {oc.STEP_LABELS[step]}')
        st = oc.load_state()

    result = oc.commit()
    if not result.get('ok'):
        print('提交失败：', file=sys.stderr)
        for e in result.get('errors') or []:
            print('  -', e, file=sys.stderr)
        fix = result.get('fix_step')
        if fix:
            oc.rewind_to(fix)
            print(f'→ 已回到第 {fix} 步；修正答案文件后重跑', file=sys.stderr)
        return 3
    print(f'✅ 已创建画像 {result["profile_id"]}（profile.json / prefs.yaml / published.json）')
    for w in result.get('warnings') or []:
        print('  ⚠️', w)
    print(f'下一步：{result.get("next")}')
    return 0


def _print_preview(res: dict) -> None:
    if res.get('errors'):
        for e in res['errors']:
            print('  ✗', e)
        return
    for it in res.get('items') or []:
        if it['filtered_by']:
            print(f'  {it["jd_title"]}：{it["level"]}（硬过滤：{it["filtered_by"]}）')
        else:
            print(f'  {it["jd_title"]}：{it["level"]} {it["match_pct"]}% · '
                  f'命中 {"、".join(it["hits"]) or "无"}')
    print(f'档位分布：{"、".join(res.get("levels") or []) or "无"} '
          f'（{"≥2 档 ✅" if res.get("spread_ok") else "仅 1 档"}）')


def main() -> int:
    ap = argparse.ArgumentParser(description='boss-pipeline 五步向导（CLI）')
    ap.add_argument('--status', action='store_true', help='只查看进度')
    ap.add_argument('--reset', action='store_true', help='清空向导草稿（不删已发布画像）')
    ap.add_argument('--resume', action='store_true', help='从断点续做（默认行为）')
    ap.add_argument('--step', type=int, default=None, help='回到指定步骤重填（1–5）')
    ap.add_argument('--answers', default=None, help='非交互答案 JSON 文件')
    ap.add_argument('--home', default=None, help='覆盖 ~/.boss-pipeline（同 BOSS_PIPELINE_HOME）')
    args = ap.parse_args()

    if args.home:
        import config_paths as cp
        try:
            h = cp.home(create=True, cli_home=args.home)
            import os
            os.environ[cp.ENV_HOME] = str(h)
        except Exception as e:
            print(f'--home 无效：{e}', file=sys.stderr)
            return 2

    if args.status:
        return cmd_status()
    if args.reset:
        print(oc.reset()['note'])
        return 0
    if args.answers:
        return run_answers(args.answers)
    return run_interactive(args.step)


if __name__ == '__main__':
    sys.exit(main())
