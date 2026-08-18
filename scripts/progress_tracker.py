#!/usr/bin/env python3
"""
T6 进度追踪器：爬取过程中写 progress.json + jobs_cache.json，供 Dashboard 轮询。

用法：
    from progress_tracker import ProgressTracker

    tracker = ProgressTracker(output_dir="/path/to/01_raw/")
    tracker.start(list_total=300)
    tracker.on_job_scraped(job_dict, success=True)
    tracker.on_job_scraped(job_dict, success=False, error="security_id 无效")
    tracker.finish()
    
    # 也支持在爬取结束后一次性填充 jobs（从 CSV 读取）
    tracker.refresh_from_csv(csv_path)
"""

import json
import os
import re
import time
import threading
import csv
from datetime import datetime


class ProgressTracker:
    """线程安全的进度追踪器，写两个 JSON 文件供 Dashboard 前端轮询。"""

    # 薪资桶边界（K）
    SALARY_BUCKETS = [0, 5, 10, 15, 20, 25, 30, 35, 40, 50, 100]

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self._lock = threading.Lock()
        self._progress = {
            "status": "idle",
            "started_at": None,
            "updated_at": None,
            "list_total": 0,
            "scraped": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "eta_seconds": None,
            "avg_seconds_per_job": 0,
            "elapsed_seconds": 0,
            "salary_stats": {"avg": 0, "min": None, "max": None, "count": 0},
            "salary_buckets": {},  # {"5-10K": 3, "10-15K": 5, ...}
            "location_stats": {},  # 仅区级，如 {"浦东新区": 5, "徐汇区": 3}
        }
        self._jobs = []
        self._start_time = None

    def start(self, list_total: int = 0, search_url: str = '', resume: bool = False):
        """标记爬取开始。

        resume=True（断点续爬）：加载已有 jobs_cache 到内存并恢复累计统计，
        不把 Dashboard 列表清空（修复「续爬清空已有岗位」）。
        """
        with self._lock:
            now = datetime.now().isoformat(timespec="seconds")
            if resume:
                # 续爬：从已有 jobs_cache 续写
                self._jobs = []
                j_path = os.path.join(self.output_dir, "jobs_cache.json")
                if os.path.isfile(j_path):
                    try:
                        with open(j_path, "r", encoding="utf-8") as f:
                            self._jobs = json.load(f) or []
                    except Exception:
                        self._jobs = []
                self._progress.update({
                    "status": "running",
                    "started_at": now,
                    "updated_at": now,
                    "list_total": list_total,
                    "search_url": search_url,
                    "scraped": len(self._jobs),
                    "success": sum(1 for j in self._jobs if j.get("success") is not False),
                    "failed": sum(1 for j in self._jobs if j.get("success") is False),
                    "skipped": 0,
                    "eta_seconds": None,
                    "avg_seconds_per_job": 0,
                    "elapsed_seconds": 0,
                })
                # 恢复历史薪资/地区统计（避免统计清零）
                p_path = os.path.join(self.output_dir, "progress.json")
                if os.path.isfile(p_path):
                    try:
                        with open(p_path, "r", encoding="utf-8") as f:
                            old = json.load(f)
                        for k in ("salary_stats", "salary_buckets", "location_stats"):
                            if k in old:
                                self._progress[k] = old[k]
                    except Exception:
                        pass
            else:
                # 全新爬取：从零开始
                self._jobs = []
                self._progress.update({
                    "status": "running",
                    "started_at": now,
                    "updated_at": now,
                    "list_total": list_total,
                    "search_url": search_url,
                    "scraped": 0,
                    "success": 0,
                    "failed": 0,
                    "skipped": 0,
                    "eta_seconds": None,
                    "avg_seconds_per_job": 0,
                    "elapsed_seconds": 0,
                })
            self._start_time = time.time()
            self._write()

    def update_list_total(self, n: int):
        """滚动后更新列表总数并立即写盘（Dashboard 实时可见）。"""
        with self._lock:
            self._progress["list_total"] = int(n) if n else 0
            self._progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._write()

    def on_job_scraped(self, job: dict, success: bool = True, error: str = ""):
        """每爬完一条调用。job 为 CSV row dict。"""
        with self._lock:
            self._progress["scraped"] += 1
            if success:
                self._progress["success"] += 1
            else:
                self._progress["failed"] += 1

            # 时间统计
            elapsed = time.time() - self._start_time if self._start_time else 0
            self._progress["elapsed_seconds"] = round(elapsed, 1)
            scraped = self._progress["scraped"]
            if scraped > 0:
                self._progress["avg_seconds_per_job"] = round(elapsed / scraped, 1)
                remaining = self._progress["list_total"] - scraped
                if remaining > 0 and self._progress["avg_seconds_per_job"] > 0:
                    self._progress["eta_seconds"] = round(
                        self._progress["avg_seconds_per_job"] * remaining, 0
                    )
                else:
                    self._progress["eta_seconds"] = 0

            # 薪资统计 + 分布桶
            salary_str = job.get("薪资", "")
            if salary_str and salary_str != "面议":
                mid = self._parse_salary_mid(salary_str)
                if mid is not None:
                    stats = self._progress["salary_stats"]
                    if stats["min"] is None or mid < stats["min"]:
                        stats["min"] = mid
                    if stats["max"] is None or mid > stats["max"]:
                        stats["max"] = mid
                    total = stats["avg"] * stats["count"] + mid
                    stats["count"] += 1
                    stats["avg"] = round(total / stats["count"], 1)

                    # 薪资桶
                    bucket = self._salary_bucket(mid)
                    self._progress["salary_buckets"][bucket] = \
                        self._progress["salary_buckets"].get(bucket, 0) + 1

            # 地区统计（仅区级，截取 "·" 之前的部分）
            district = job.get("区域", "")
            if district:
                district_only = district.split("·")[0].strip()
                if district_only:
                    self._progress["location_stats"][district_only] = \
                        self._progress["location_stats"].get(district_only, 0) + 1

            job_id = job.get("job_id", "")
            job_url = f"https://www.zhipin.com/job_detail/{job_id}.html" if job_id else ""

            # 岗位摘要
            self._jobs.append({
                "职位名称": job.get("职位名称", ""),
                "薪资": salary_str,
                "公司名称": job.get("公司名称", ""),
                "城市": job.get("城市", ""),
                "区域": district,
                "job_id": job_id,
                "job_url": job_url,
                "创建日期": job.get("创建日期", ""),
                "success": success,
                "error": error,
            })

            self._progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._write()

    def finish(self, status: str = "done"):
        """标记爬取结束。"""
        with self._lock:
            self._progress["status"] = status
            self._progress["eta_seconds"] = 0
            self._progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._write()

    def refresh_from_csv(self, csv_path: str):
        """从 CSV 文件重新加载岗位列表（供 Dashboard 加载描述）。"""
        jobs = []
        if not os.path.exists(csv_path):
            return jobs
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    jobs.append(row)
        except Exception:
            pass
        return jobs

    # === 内部方法 ===

    def _write(self):
        """写两个 JSON 文件（原子写入防半读）。"""
        p_path = os.path.join(self.output_dir, "progress.json")
        tmp_p = p_path + ".tmp"
        with open(tmp_p, "w", encoding="utf-8") as f:
            json.dump(self._progress, f, ensure_ascii=False, indent=2)
        os.replace(tmp_p, p_path)

        j_path = os.path.join(self.output_dir, "jobs_cache.json")
        tmp_j = j_path + ".tmp"
        with open(tmp_j, "w", encoding="utf-8") as f:
            json.dump(self._jobs, f, ensure_ascii=False)
        os.replace(tmp_j, j_path)

    @staticmethod
    def _salary_bucket(mid_k: float) -> str:
        """将薪资中值归类到桶，如 17.5 → '15-20K'"""
        for i in range(len(ProgressTracker.SALARY_BUCKETS) - 1):
            lo = ProgressTracker.SALARY_BUCKETS[i]
            hi = ProgressTracker.SALARY_BUCKETS[i + 1]
            if lo <= mid_k < hi:
                return f"{lo}-{hi}K"
        return f"{ProgressTracker.SALARY_BUCKETS[-1]}+K"

    @staticmethod
    def _parse_salary_mid(salary_str: str):
        """从薪资字符串提取中值（K 为单位）。"""
        m = re.search(r'(\d+)(?:-(\d+))?K', salary_str, re.IGNORECASE)
        if m:
            low = int(m.group(1))
            high = int(m.group(2)) if m.group(2) else low
            return (low + high) / 2
        return None


# === CLI 调试 ===
if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="进度追踪器 CLI（调试用）")
    parser.add_argument("--output-dir", required=True, help="输出目录")
    parser.add_argument("--action", choices=["start", "finish", "status"], default="status")
    parser.add_argument("--list-total", type=int, default=0)

    args = parser.parse_args()
    tracker = ProgressTracker(args.output_dir)

    if args.action == "start":
        tracker.start(list_total=args.list_total)
        print(f"进度文件已创建: {args.output_dir}/progress.json")
    elif args.action == "finish":
        tracker.finish()
        print("状态已标记为 done")
    else:
        p_path = os.path.join(args.output_dir, "progress.json")
        if os.path.exists(p_path):
            with open(p_path, "r") as f:
                print(json.dumps(json.load(f), ensure_ascii=False, indent=2))
        else:
            print("progress.json 不存在", file=sys.stderr)