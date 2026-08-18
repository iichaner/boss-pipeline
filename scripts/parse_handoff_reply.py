#!/usr/bin/env python3
"""
解析 zhizhi 的回执消息，提取 result_csv 路径和 high/medium/low 计数。

支持两种格式：
1. 结构化格式（【匹配完成】模板）
2. 自然语言回执（正则兜底）

用法：
    # 作为模块
    from parse_handoff_reply import parse_handoff
    result = parse_handoff(zhizhi_reply_text)
    # result = {"ok": True, "result_csv": "...", "total": 30, "high": 5, ...}
    # result = {"ok": False, "error": "缺少必填字段: result_csv"}

    # 作为 CLI
    python3 parse_handoff_reply.py "【匹配完成】\nresult_csv: /path/to/result.csv\ntotal: 30\nhigh: 5\nmedium: 12\nlow: 13"
"""

import re
import sys
import json


def parse_handoff(text: str) -> dict:
    """
    从 zhizhi 回执文本中解析结构化数据。

    返回：
        成功: {"ok": True, "result_csv": str, "total": int, "high": int, "medium": int, "low": int}
        失败: {"ok": False, "error": str, "partial": {...}}  # partial 含已解析到的字段
    """
    if not text or not text.strip():
        return {"ok": False, "error": "回执为空"}

    text = text.strip()
    result = {}

    # === 方式1：结构化格式解析 ===
    # 匹配 result_csv: <path>
    m_csv = re.search(r'result_csv:\s*(.+)', text)
    if m_csv:
        result["result_csv"] = m_csv.group(1).strip()

    # 匹配 total/high/medium/low: <number>
    for field in ("total", "high", "medium", "low"):
        m = re.search(rf'{field}:\s*(\d+)', text)
        if m:
            result[field] = int(m.group(1))

    # === 方式2：自然语言兜底 ===
    if "result_csv" not in result:
        # 尝试从路径模式中提取 CSV 路径
        m_path = re.search(r'(/[\w/.-]+\.csv)', text)
        if m_path:
            result["result_csv"] = m_path.group(1)

    if "total" not in result:
        # 尝试匹配 "共 N 条" / "总计 N 条" / "N 条职位" / "N条"
        m_total = re.search(r'(?:共|总计|合计)\D{0,5}?(\d+)\s*条', text)
        if m_total:
            result["total"] = int(m_total.group(1))

    # 尝试从 "high N / medium M / low L" 模式中提取
    for field, patterns in [
        ("high", [r'高优先级\s*(\d+)', r'high\s*[:：]?\s*(\d+)', r'高\s*(\d+)']),
        ("medium", [r'中优先级\s*(\d+)', r'medium\s*[:：]?\s*(\d+)', r'中等?\s*(\d+)']),
        ("low", [r'低优先级\s*(\d+)', r'low\s*[:：]?\s*(\d+)', r'低\s*(\d+)']),
    ]:
        if field not in result:
            for pat in patterns:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    result[field] = int(m.group(1))
                    break

    # === 校验 ===
    required_fields = ["result_csv", "total", "high", "medium", "low"]
    missing = [f for f in required_fields if f not in result]

    if missing:
        return {
            "ok": False,
            "error": f"缺少必填字段: {', '.join(missing)}",
            "partial": result
        }

    # 校验计数一致性
    if result["high"] + result["medium"] + result["low"] != result["total"]:
        return {
            "ok": False,
            "error": f"计数不一致: high({result['high']}) + medium({result['medium']}) + low({result['low']}) = "
                     f"{result['high'] + result['medium'] + result['low']} ≠ total({result['total']})",
            "partial": result
        }

    if result["total"] == 0:
        return {
            "ok": False,
            "error": "total=0，可能是 CSV 为空或读取失败",
            "partial": result
        }

    return {"ok": True, **result}


def parse_needs_profile(text: str) -> bool:
    """
    判断 zhizhi 回执是否表示"需要候选人画像"。
    """
    if not text:
        return False
    indicators = ["需要先建立", "候选人画像", "求职偏好", "画像文件", "Step 0", "引导流程"]
    return any(ind in text for ind in indicators)


# === CLI ===
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 parse_handoff_reply.py <回执文本>")
        print("  或: echo '回执文本' | python3 parse_handoff_reply.py -")
        sys.exit(1)

    if sys.argv[1] == "-":
        text = sys.stdin.read()
    else:
        text = " ".join(sys.argv[1:])

    # 检查是否需要画像
    if parse_needs_profile(text):
        print(json.dumps({"needs_profile": True, "raw": text}, ensure_ascii=False, indent=2))
        sys.exit(0)

    result = parse_handoff(text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)
