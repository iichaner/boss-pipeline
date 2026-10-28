#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""onboard.py —— 五步向导 CLI（P4）。

用法
----
    python3 scripts/onboard.py --status            # 只查看进度（不写文件）
    python3 scripts/onboard.py                     # 交互式；中断后自动续做
    python3 scripts/onboard.py --resume            # 同上（显式）
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
    print('下一步：python3 scripts/onboard.py --resume')
    return 0


def _ask(prompt: str, default: str = '') -> str:
    suffix = f' [{default}]' if default != '' else ''
    try:
        v = input(f'{prompt}{suffix}：').strip()
    except EOFError:
        return default
    return v or default


def _ask_list(prompt: str, default: str = '') -> list:
    raw = _ask(prompt + '（逗号分隔）', default)
    return [x.strip() for x in raw.replace('，', ',').split(',') if x.strip()]


def collect_step2() -> dict:
    print('\n[2/5] 偏好配置（直接回车采用方括号内默认值）')
    pid = _ask('画像 id（小写字母/数字/短横线）', 'me')
    name = _ask('怎么称呼你', '我')
    years = _ask('工作年限（年）', '3')
    edu = _ask('学历（大专/本科/硕士/博士/其他）', '本科')
    eng = _ask('英语等级 0–5（0=不会，3=可工作，5=母语级）', '3')
    roles = _ask_list('目标岗位关键词（标题需命中其一）', '运营')
    cities = _ask_list('期望城市', '上海')
    smin = _ask('期望薪资下限（K）', '15')
    smax = _ask('期望薪资上限（K）', '30')
    remote = _ask('接受远程？(y/n)', 'n').lower().startswith('y')
    ex_titles = _ask_list('排除的标题关键词（可空）', '')
    ex_roles = _ask_list('排除的岗位类型（可空）', '')
    ex_ind = _ask_list('排除的行业（可空）', '')

    print('\n能力自评：0=没有 1=了解 2=能执行 3=能独立负责 4=能搭体系 5=能定战略')
    dims, keywords, defs = {}, {}, {}
    for name_dim in oc.NEUTRAL_DIMENSIONS:
        lvl = _ask(f'  {name_dim} 自评', '0')
        if lvl in ('0', ''):
            continue
        words = _ask_list(f'  {name_dim} 的关键词（至少 1 个，例如流程/复盘）', '')
        if not words:
            print('    ↳ 没有关键词会跳过该维度')
            continue
        dims[name_dim] = int(lvl)
        keywords[name_dim] = words
        d = _ask(f'  {name_dim} 一句话定义（可空）', '')
        if d:
            defs[name_dim] = d

    return {
        'profile_id': pid,
        'identity': {
            'display_name': name,
            'total_years': float(years) if '.' in years else int(years or 0),
            'education': edu,
            'languages': {'英语': int(eng)},
            'certifications': [],
        },
        'targets': {
            'roles': roles,
            'cities': cities,
            'salary': {'min_k': float(smin), 'max_k': float(smax)},
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


def run_interactive() -> int:
    st = oc.load_state()
    print(f'从第 {st["step"]} 步继续（共 {oc.STEPS} 步）')
    while not st.get('ready') and st['step'] <= oc.STEPS:
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
            answers = collect_step2()
        elif step == 3:
            res = oc.preview(oc.build_profile(st), oc.build_prefs(st))
            _print_preview(res)
            if not res.get('spread_ok'):
                print('提示：3 条样例没有出现 2 种以上档位，可回第 2 步补充关键词/自评。')
            _ask('按回车记录本步并继续', '')
            answers = {}
        elif step == 4:
            print('可选：起浏览器 → 登录 → 粘贴列表页 URL，只会爬 5 条。')
            url = _ask('列表页 URL（直接回车跳过）', '')
            answers = {'url': url} if url else {'skipped': True}
        else:
            plan = oc.dry_run_plan(st)
            print(f'dry-run：将向 {plan["would_send"]} 条高/中优先级岗位打招呼（不会真发）')
            _ask('按回车确认 dry-run 并完成', '')
            answers = {'dry_run': True}

        res = oc.submit_step(step, answers)
        if not res.get('ok'):
            print('校验未通过：')
            for e in res.get('errors') or []:
                print('  -', e)
            if step == 2:
                continue
            return 2
        st = oc.load_state()

    result = oc.commit()
    if not result.get('ok'):
        print('提交失败：')
        for e in result.get('errors') or []:
            print('  -', e)
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
            return 2
        print(f'[ok] 第 {step} 步 {oc.STEP_LABELS[step]}')
        st = oc.load_state()

    result = oc.commit()
    if not result.get('ok'):
        print('提交失败：', file=sys.stderr)
        for e in result.get('errors') or []:
            print('  -', e, file=sys.stderr)
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
    return run_interactive()


if __name__ == '__main__':
    sys.exit(main())
