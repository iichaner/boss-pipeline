#!/usr/bin/env python3
"""
T5 辅助脚本：构建 cron 打招呼任务的参数。

用途：pipeline 调用此脚本生成 cron job JSON，再由 main 通过 cron 工具创建任务。
不直接调用 cron API（那是 main 的职责）。

用法：
    from schedule_greet import build_greet_cron_job

    job = build_greet_cron_job(
        result_csv="/path/to/02_result/result.csv",
        greeting="您好，...",
        max_greet=10,
        run_at="2026-08-06T10:00:00+08:00",
        search_query="人力 数字化"
    )
    # job 是 dict，可直接传给 cron(action="add", job=job)
"""

import json
import os
from datetime import datetime, timedelta


DEFAULT_GREETING = "您好，看到贵司在招的岗位很感兴趣，我的背景和经验比较匹配，希望能有机会进一步沟通~"

GREET_SCRIPT = os.path.expanduser(
    "~/.agents/skills/boss-auto-greet/scripts/boss_auto_greet.py"
)


def parse_time_input(time_str: str, reference: datetime = None) -> str:
    """
    解析用户输入的时间，返回 ISO-8601 时间戳（含时区）。

    支持格式：
    - "10:00" → 当天或次日 10:00
    - "14:30" → 当天或次日 14:30
    - "明天 10:00" → 次日 10:00
    - "2026-08-06 10:00" → 指定日期

    返回：ISO 格式字符串，如 "2026-08-06T10:00:00+08:00"
    """
    if reference is None:
        reference = datetime.now()

    time_str = time_str.strip()

    # "明天 HH:MM"
    if time_str.startswith("明天"):
        parts = time_str.replace("明天", "").strip().split(":")
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        target = reference + timedelta(days=1)
        target = target.replace(hour=h, minute=m, second=0, microsecond=0)
        return target.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    # "YYYY-MM-DD HH:MM" or "YYYY-MM-DDTHH:MM:SS"
    if "-" in time_str and ("T" in time_str or " " in time_str):
        try:
            if "T" in time_str:
                target = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            else:
                target = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
            return target.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        except ValueError:
            pass

    # "HH:MM"
    if ":" in time_str:
        parts = time_str.split(":")
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        target = reference.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= reference:
            target += timedelta(days=1)
        return target.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    # 兜底：次日 10:00
    target = reference + timedelta(days=1)
    target = target.replace(hour=10, minute=0, second=0, microsecond=0)
    return target.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def build_greet_cron_job(
    result_csv: str,
    greeting: str = DEFAULT_GREETING,
    max_greet: int = 0,
    run_at: str = None,
    search_query: str = "",
) -> dict:
    """
    构建 cron job 字典，供 cron(action="add", job=...) 使用。

    参数：
        result_csv: 02_result/ 下的判定结果 CSV 绝对路径
        greeting: 打招呼语（默认内置）
        max_greet: 单批上限（0=全部）
        run_at: ISO 时间戳（None 则默认次日 10:00）
        search_query: 搜索词（用于 job name 溯源）

    返回：
        cron job dict
    """
    if not os.path.isabs(result_csv):
        raise ValueError(f"result_csv 必须是绝对路径: {result_csv}")

    # 计算执行时间
    if run_at is None:
        run_at = parse_time_input("10:00")

    # 从路径提取日期标记
    basename = os.path.basename(os.path.dirname(result_csv))  # e.g. "2026-08-05-人力数字化"
    date_tag = datetime.now().strftime("%Y%m%d")
    query_tag = search_query.replace(" ", "-")[:20] if search_query else basename

    # 构建命令
    cmd_parts = [
        f"python3 {GREET_SCRIPT}",
        f'--csv "{result_csv}"',
    ]
    if greeting != DEFAULT_GREETING:
        cmd_parts.append(f'--message "{greeting}"')
    if max_greet > 0:
        cmd_parts.append(f"--max-greet {max_greet}")

    command = " \\\n  ".join(cmd_parts)

    # 构建 payload message
    message = f"""【自动打招呼任务】

请执行 boss-auto-greet 脚本：

{command}

执行完成后汇报结果（成功/失败条数）。"""

    job = {
        "name": f"boss-greet-{query_tag}-{date_tag}",
        "schedule": {
            "kind": "at",
            "at": run_at,
        },
        "payload": {
            "kind": "agentTurn",
            "message": message,
        },
        "sessionTarget": "isolated",
        "delivery": {
            "mode": "announce",
        },
    }

    return job


def build_greet_summary(
    result_csv: str,
    greeting: str = DEFAULT_GREETING,
    max_greet: int = 0,
    run_at: str = None,
    search_query: str = "",
) -> str:
    """
    构建询问 ii 的确认文本。
    """
    if run_at is None:
        run_at = parse_time_input("10:00")

    display_time = run_at.replace("T", " ").split("+")[0]

    lines = [
        "【打招呼确认】",
        "",
        f"执行时间: {display_time}",
        f"打招呼语: {greeting[:50]}{'...' if len(greeting) > 50 else ''}",
        f"单批上限: {'全部' if max_greet == 0 else max_greet}",
        f"结果文件: {result_csv}",
        "",
        "确认无误请回复「确认」，需要修改请告知。",
    ]
    return "\n".join(lines)


# === CLI ===
if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="构建 cron 打招呼任务参数")
    parser.add_argument("--result-csv", required=True, help="判定结果 CSV 绝对路径")
    parser.add_argument("--greeting", default=DEFAULT_GREETING, help="打招呼语")
    parser.add_argument("--max-greet", type=int, default=0, help="单批上限（0=全部）")
    parser.add_argument("--run-at", default=None, help="执行时间（ISO 或 HH:MM）")
    parser.add_argument("--search-query", default="", help="搜索词")
    parser.add_argument("--summary", action="store_true", help="仅输出确认文本（不生成 job）")

    args = parser.parse_args()

    if args.summary:
        text = build_greet_summary(
            result_csv=args.result_csv,
            greeting=args.greeting,
            max_greet=args.max_greet,
            run_at=args.run_at,
            search_query=args.search_query,
        )
        print(text)
    else:
        job = build_greet_cron_job(
            result_csv=args.result_csv,
            greeting=args.greeting,
            max_greet=args.max_greet,
            run_at=args.run_at,
            search_query=args.search_query,
        )
        print(json.dumps(job, ensure_ascii=False, indent=2))
