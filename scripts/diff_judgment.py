#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diff_judgment.py — 判定结果逐行对比（I3 行为等价；P2/P3/P4/P5 门禁）。

用法：
    python3 scripts/diff_judgment.py \\
        --baseline ~/.boss-pipeline/tests/baseline/base_20261028.csv \\
        --candidate /tmp/after_p2.csv

默认比较 7 列：优先级判断,匹配度,优先级判断理由,能力条目得分,叠加信号,平均JD深度,平均候选人深度
（与 run_match.py / judge_monitor.py 的 APPEND_COLS 一致）。

退出码：0 = 逐行逐列完全一致；1 = 有差异或结构不一致；2 = 文件读取失败。
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter

DEFAULT_COLUMNS = ["优先级判断", "匹配度", "优先级判断理由", "能力条目得分",
                   "叠加信号", "平均JD深度", "平均候选人深度"]

MAX_SHOW = 30


def read_csv(path: str):
    """返回 (header, rows)。按 job_id 建索引；无 job_id 时退回 岗位名称|公司名称。"""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            header = list(reader.fieldnames or [])
            rows = [dict(r) for r in reader]
    except OSError as e:
        print(f"E-READ 无法读取 {path}: {e}", file=sys.stderr)
        sys.exit(2)

    index, dups = {}, []
    for i, r in enumerate(rows):
        jid = (r.get("job_id") or "").strip()
        if not jid:
            jid = f"{r.get('岗位名称', '')}|{r.get('公司名称', '')}#{i}"
        if jid in index:
            dups.append(jid)
            jid = f"{jid}#dup{i}"
        index[jid] = r
    return header, index, dups


def norm(v) -> str:
    """数值/字符串归一化：'95' vs '95.0' 视为不同（判定必须逐字一致）。"""
    return "" if v is None else str(v).strip()


def main():
    ap = argparse.ArgumentParser(description="判定结果逐行对比")
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--columns", default=",".join(DEFAULT_COLUMNS))
    ap.add_argument("--ignore-columns", default="")
    ap.add_argument("--show", type=int, default=MAX_SHOW)
    args = ap.parse_args()

    cols = [c.strip() for c in args.columns.split(",") if c.strip()]
    ignore = {c.strip() for c in args.ignore_columns.split(",") if c.strip()}
    cols = [c for c in cols if c not in ignore]

    hb, base, dup_b = read_csv(args.baseline)
    hc, cand, dup_c = read_csv(args.candidate)

    missing_cols = [c for c in cols if c not in hc]
    if missing_cols:
        print(f"E-SCHEMA candidate 缺少列：{missing_cols}", file=sys.stderr)
        sys.exit(1)

    only_base = sorted(set(base) - set(cand))
    only_cand = sorted(set(cand) - set(base))

    diffs = []          # (job_id, column, old, new)
    for jid in sorted(set(base) & set(cand)):
        b, c = base[jid], cand[jid]
        for col in cols:
            bv, cv = norm(b.get(col)), norm(c.get(col))
            if bv != cv:
                diffs.append((jid, col, bv, cv))

    # 汇总
    def dist(idx):
        return Counter(norm(r.get("优先级判断")) for r in idx.values())

    db, dc = dist(base), dist(cand)
    print(f"baseline : {args.baseline}  rows={len(base)}  "
          + "  ".join(f"{k or '<空>'}={db[k]}" for k in ("high", "medium", "low") if db.get(k))
          + (f"  <空>={db['']}" if db.get("") else ""))
    print(f"candidate: {args.candidate}  rows={len(cand)}  "
          + "  ".join(f"{k or '<空>'}={dc[k]}" for k in ("high", "medium", "low") if dc.get(k))
          + (f"  <空>={dc['']}" if dc.get("") else ""))
    if dup_b:
        print(f"[warn] baseline 有 {len(dup_b)} 个重复 job_id")
    if dup_c:
        print(f"[warn] candidate 有 {len(dup_c)} 个重复 job_id")

    total_problems = len(diffs) + len(only_base) + len(only_cand)

    if only_base:
        print(f"\n[缺失] candidate 少了 {len(only_base)} 条：")
        for jid in only_base[:args.show]:
            print(f"  - {jid}")
    if only_cand:
        print(f"\n[新增] candidate 多了 {len(only_cand)} 条：")
        for jid in only_cand[:args.show]:
            print(f"  + {jid}")

    if diffs:
        by_col = Counter(c for _, c, _, _ in diffs)
        print(f"\n[差异] {len(diffs)} 处 / 涉及 {len({j for j, *_ in diffs})} 条岗位")
        print("  按列：" + "，".join(f"{c}={n}" for c, n in by_col.most_common()))
        for jid, col, bv, cv in diffs[:args.show]:
            print(f"  · {jid} [{col}]\n      旧: {bv[:160]}\n      新: {cv[:160]}")
        if len(diffs) > args.show:
            print(f"  … 其余 {len(diffs) - args.show} 处省略（--show N 可调整）")

    if total_problems == 0:
        print("\n结果：0 差异（逐行逐列一致）")
        return 0
    print(f"\n结果：{total_problems} 处不一致")
    return 1


if __name__ == "__main__":
    sys.exit(main())
