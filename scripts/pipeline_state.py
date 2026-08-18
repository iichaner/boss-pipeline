#!/usr/bin/env python3
"""
boss-pipeline 状态机读写模块。

用途：全流程进度落盘，中断后可从 pipeline_state.json 续跑，不重头来。
状态结构见 plan.md 6.3：
{
  "stage": "crawl" | "judge" | "greet" | "done",
  "status": "running" | "waiting" | "done" | "error",
  "raw_csv": "<01_raw 下的 CSV 绝对路径>",
  "result_csv": "<02_result 下的判定结果 CSV 绝对路径>",
  "counts": {"total": 0, "high": 0, "medium": 0, "low": 0},
  "greet": {"scheduled_at": null, "done": 0, "failed": 0},
  "search_query": "<搜索词，用于目录命名溯源>",
  "list_url": "<原始列表页 URL>",
  "created_at": "<ISO 时间>",
  "updated_at": "<ISO 时间>"
}

用法：
    from pipeline_state import init_state, load_state, update_state

    state = init_state(work_dir, list_url="...", search_query="人力 数字化")
    update_state(work_dir, stage="crawl", status="running")
    ...
    state = load_state(work_dir)  # 中断后重新读取，判断从哪一步继续
"""

import json
import os
from datetime import datetime

STATE_FILENAME = "pipeline_state.json"

DEFAULT_STATE = {
    "stage": "crawl",
    "status": "running",
    "raw_csv": None,
    "result_csv": None,
    "counts": {"total": 0, "high": 0, "medium": 0, "low": 0},
    "greet": {"scheduled_at": None, "done": 0, "failed": 0},
    "search_query": "",
    "list_url": "",
}


def _state_path(work_dir):
    return os.path.join(work_dir, STATE_FILENAME)


def init_state(work_dir, list_url="", search_query="", overwrite=False):
    """
    首次创建 pipeline_state.json。

    若文件已存在且 overwrite=False，直接返回现有状态（不覆盖），
    避免重复初始化时把已有进度清空。
    """
    os.makedirs(work_dir, exist_ok=True)
    path = _state_path(work_dir)

    if os.path.exists(path) and not overwrite:
        existing = load_state(work_dir)
        if existing is not None:
            return existing

    now = datetime.now().isoformat(timespec="seconds")
    state = dict(DEFAULT_STATE)
    state["counts"] = dict(DEFAULT_STATE["counts"])
    state["greet"] = dict(DEFAULT_STATE["greet"])
    state["list_url"] = list_url
    state["search_query"] = search_query
    state["created_at"] = now
    state["updated_at"] = now

    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    return state


def load_state(work_dir):
    """读取现有状态，文件不存在或解析失败返回 None。"""
    path = _state_path(work_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def update_state(work_dir, **fields):
    """
    合并更新字段（浅合并；对 counts/greet 这类 dict 字段做浅层合并而非整体替换），
    自动写入 updated_at，返回更新后的完整状态。

    若状态文件不存在，先按 DEFAULT_STATE 初始化再合并。
    """
    state = load_state(work_dir)
    if state is None:
        state = init_state(work_dir)

    for key, value in fields.items():
        if key in ("counts", "greet") and isinstance(value, dict) and isinstance(state.get(key), dict):
            state[key].update(value)
        else:
            state[key] = value

    state["updated_at"] = datetime.now().isoformat(timespec="seconds")

    with open(_state_path(work_dir), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    return state


def next_action(state):
    """
    根据当前状态返回下一步应执行的动作描述（供 main 编排判断续跑起点）。
    返回值：'crawl' | 'judge' | 'greet' | 'done' | 'error'
    """
    if state is None:
        return "crawl"
    if state.get("status") == "error":
        return "error"
    stage = state.get("stage", "crawl")
    status = state.get("status", "running")
    if stage == "done":
        return "done"
    if stage == "crawl" and status != "done":
        return "crawl"
    if stage in ("crawl", "judge") and status == "done" and stage == "crawl":
        return "judge"
    if stage == "judge" and status != "done":
        return "judge"
    if stage in ("judge", "greet") and status == "done" and stage == "judge":
        return "greet"
    if stage == "greet" and status != "done":
        return "greet"
    return "done"


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="boss-pipeline 状态机 CLI（调试用）")
    parser.add_argument("work_dir", help="工作目录（含 pipeline_state.json）")
    parser.add_argument("--show", action="store_true", help="打印当前状态")
    args = parser.parse_args()

    if args.show:
        s = load_state(args.work_dir)
        if s is None:
            print("状态文件不存在", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(s, ensure_ascii=False, indent=2))
