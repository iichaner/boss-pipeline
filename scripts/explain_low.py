#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""explain_low.py —— 「判定全 low」归因（P1-5 / N4）。

只读分析，不改任何文件；把判定结果里的「优先级判断理由」聚合成可执行的排查线索。

用法
----
    python3 scripts/explain_low.py                      # 用最新一次判定的结果
    python3 scripts/explain_low.py --result /path/判定结果.csv
    python3 scripts/explain_low.py --cause zombie       # 只看某一类原因
    python3 scripts/explain_low.py --cause keywords --profile-id me

--cause 取值：role / city / salary / industry / zombie / keywords / other / all（默认 all）

退出码：0 = 分析完成（是否有 low 不影响退出码）；2 = 找不到可读的结果文件。
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config_paths as cp  # noqa: E402

#: 原因类别 →（理由里出现的关键词, 人话解释, 一条修复动作）
CAUSES = {
    'role': (
        ('岗位方向不匹配',),
        '目标岗位关键词 / 标题闸门把岗位判成「方向不对」',
        '回向导第 2 步核对「目标岗位关键词」（标题必须命中其一）；'
        '确实想做这个方向就把词补上',
    ),
    'city': (
        ('城市不符',),
        '岗位城市不在期望城市里，且没有开「接受远程」',
        '第 2 步改「期望城市」；异地可远程就勾「接受远程」',
    ),
    'salary': (
        ('薪资完全不覆盖', '薪资严重不足'),
        '薪资区间与期望完全不重叠（或严重偏低）',
        '第 2 步核对期望薪资上下限；只看高薪岗位时这条会大量命中',
    ),
    'industry': (
        ('行业', '专业资质'),
        '行业 / 资质硬过滤把岗位排除了',
        '第 2 步「排除的行业」是否写得太宽；画像 filters.exclude_industries',
    ),
    'zombie': (
        ('僵尸岗位',),
        '启用了僵尸岗规则（JD 过短 + 高薪 / 标题特征 / 壳公司）',
        'generic 出厂已关闭；若画像 prefs 里显式开了，改成空对象/空词表即可'
        '（rules.hard_filters.zombie_*）',
    ),
    'keywords': (
        (),  # 兜底：没有硬过滤但档位仍低
        '没有被硬过滤，但能力关键词命中太少 / 自评偏低 / 阈值偏高',
        '第 2 步补维度关键词与自评，必要时下调 high/medium 阈值（默认 95/90）',
    ),
    'other': (
        (),  # 其它硬过滤（英语/小语种/学历/实习等）
        '其它硬过滤（语言、学历、实习/日薪等）',
        '看下面「出现最多的理由」逐条核对；语言等级在画像 identity.languages，'
        '学历在 identity.education',
    ),
}

ORDER = ['role', 'city', 'salary', 'industry', 'zombie', 'keywords']


def _latest_result() -> str:
    """在 work root 下找最新的 02_result/*判定结果*.csv。"""
    try:
        root = str(cp.work_root())
    except Exception:
        root = str(Path.home() / '.boss-pipeline' / 'work')
    pats = [
        os.path.join(root, '**', '02_result', '*判定结果*.csv'),
        os.path.join(root, '**', '*判定结果*.csv'),
    ]
    files = []
    for pat in pats:
        files.extend(glob.glob(pat, recursive=True))
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return ''
    return max(files, key=lambda f: os.path.getmtime(f))


def _read(path: str):
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def _classify(reason: str) -> str:
    for key in ORDER:
        kws = CAUSES[key][0]
        if kws and any(k in reason for k in kws):
            return key
    if '硬过滤' in reason:
        return 'other'
    return 'keywords'


def _thresholds(profile_id: str):
    try:
        prefs = cp.load_prefs(profile_id)
    except Exception:
        return None
    eng = prefs.get('engine') or {}
    sc = eng.get('scoring') or {}
    return sc.get('thresholds') or None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='判定全 low 的归因分析（只读）')
    ap.add_argument('--result', default='', help='判定结果 CSV（默认取最新一次）')
    ap.add_argument('--cause', default='all', choices=ORDER + ['other', 'all'])
    ap.add_argument('--profile-id', default=None, help='顺带显示阈值与 taxonomy 规模')
    args = ap.parse_args(argv)

    path = args.result or _latest_result()
    if not path or not os.path.isfile(path):
        print('❌ 没找到判定结果 CSV')
        print('   修复：先跑一次判定（第 5 步），或显式指定 '
              '--result <判定结果.csv>')
        return 2

    rows = _read(path)
    if not rows:
        print(f'❌ 结果文件是空的：{path}')
        print('   修复：检查输入 CSV 是否有数据行（表头之外至少 1 条）')
        return 2

    levels = Counter((r.get('优先级判断') or '').strip().lower() for r in rows)
    total = len(rows)
    print(f'结果文件：{path}')
    print(f'总计 {total} 条：' + '，'.join(
        f'{lv or "（空）"}={levels.get(lv, 0)}'
        for lv in ('high', 'medium', 'low')))
    thr = _thresholds(args.profile_id) if args.profile_id else None
    if thr:
        print(f'当前阈值：high={thr.get("high")}, medium={thr.get("medium")}')

    reasons = Counter()
    buckets = Counter()
    lows = 0
    for r in rows:
        lv = (r.get('优先级判断') or '').strip().lower()
        reason = (r.get('优先级判断理由') or '').strip()
        if lv == 'low':
            lows += 1
            buckets[_classify(reason)] += 1
        if reason:
            reasons[reason] += 1

    if not lows:
        print('\n本次没有 low，无需归因。')
        return 0

    print(f'\nlow 共 {lows} 条，按原因归类：')
    shown = ORDER + ['other'] if args.cause == 'all' else [args.cause]
    for key in shown:
        n = buckets.get(key, 0)
        if n == 0:
            continue
        _, why, fix = CAUSES[key]
        print(f'  · [{key}] {n} 条 —— {why}')
        print(f'    ↳ 处置：{fix}')

    print('\n出现最多的理由（前 8）：')
    for reason, n in reasons.most_common(8):
        print(f'  {n:>4}  {reason[:60]}')

    if buckets.get('keywords'):
        print('\n提示：keywords 这一类没有硬过滤，纯靠「关键词命中 + 自评深度 + 阈值」。'
              '先补关键词，再考虑降阈值（向导第 2 步「高级」）。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
