#!/usr/bin/env python3
"""
judge_monitor.py — 优先级判定监控模块。

功能：
1. 直接调用 job-match-priority 的 matcher 引擎（纯 Python，零 LLM 调用）
2. 增量判定：后台线程监控爬取进度，每新增 20 条未判定 JD 触发一次判定
3. 全量判定：对所有未判定的 JD 执行一次判定

依赖：job-match-priority 的 matcher.py + config/（已编译，无需额外依赖）
"""

import csv
import glob
import json
import os
import sys
import threading
import time
from datetime import datetime

# ── 加载 job-match-priority 引擎 ──────────────────────────
MATCHER_SKILL_DIR = os.path.expanduser('~/.agents/skills/job-match-priority')
MATCHER_SCRIPTS_DIR = os.path.join(MATCHER_SKILL_DIR, 'scripts')
MATCHER_CONFIG_DIR = os.path.join(MATCHER_SKILL_DIR, 'config')

# 将 matcher 脚本目录加入 sys.path
if MATCHER_SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, MATCHER_SCRIPTS_DIR)

try:
    from matcher import load_profile, load_config, Matcher, enrich_jd
    MATCHER_AVAILABLE = True
except ImportError as e:
    MATCHER_AVAILABLE = False
    _MATCHER_IMPORT_ERROR = str(e)

# ── 常量 ──────────────────────────────────────────────────
BATCH_SIZE = 20  # 每 20 条触发一次增量判定
POLL_INTERVAL = 10  # 后台监控轮询间隔（秒）

# 判定结果追加列（与 run_match.py 一致）
APPEND_COLS = ["优先级判断", "匹配度", "优先级判断理由", "能力条目得分", "叠加信号", "平均JD深度", "平均候选人深度"]

# 优先级中英文映射
LEVEL_EN_TO_CN = {'high': '高', 'medium': '中', 'low': '低'}
LEVEL_CN_TO_EN = {'高': 'high', '中': 'medium', '低': 'low'}


def _find_latest_raw_csv(output_dir: str) -> str:
    """找到最新的 raw CSV 文件。"""
    csvs = glob.glob(os.path.join(output_dir, 'jobs_data*.csv'))
    if not csvs:
        return None
    return max(csvs, key=os.path.getmtime)


def _find_or_create_judgment_csv(raw_csv: str, result_dir: str) -> str:
    """找到或创建对应的判定结果 CSV。返回判定 CSV 路径。"""
    os.makedirs(result_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(raw_csv))[0]
    judgment_csv = os.path.join(result_dir, base_name + '_判定结果.csv')

    if os.path.isfile(judgment_csv):
        return judgment_csv

    # 创建空判定 CSV（带表头）
    try:
        with open(raw_csv, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            for col in APPEND_COLS:
                if col not in fieldnames:
                    fieldnames.append(col)
            rows = list(reader)

        with open(judgment_csv, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                for col in APPEND_COLS:
                    if col not in row:
                        row[col] = ''
                writer.writerow(row)
    except Exception as e:
        print(f"[judge_monitor] 创建判定 CSV 失败: {e}")
        return None

    return judgment_csv


def _load_existing_judgments(judgment_csv: str, priorities_path: str) -> dict:
    """加载已有判定结果。返回 {job_id: {level, note, source}}。

    来源优先级：
    1. priorities.json 中有 note 字段的（人工判定带备注）→ 最高优先级
    2. priorities.json 中有 level 字段的（人工判定）
    3. 判定 CSV 中的 AI 判定
    """
    judgments = {}

    # 1. 加载 priorities.json（人工判定）
    if os.path.isfile(priorities_path):
        try:
            with open(priorities_path, 'r', encoding='utf-8') as f:
                priorities = json.load(f)
            for jid, val in priorities.items():
                if isinstance(val, dict):
                    level = val.get('level', '')
                    note = val.get('note', '')
                    if level or note:
                        judgments[jid] = {
                            'level': level,
                            'note': note,
                            'source': 'manual' if note else 'manual_no_note'
                        }
                elif isinstance(val, str) and val in ('高', '中', '低'):
                    judgments[jid] = {'level': val, 'note': '', 'source': 'manual'}
        except Exception:
            pass

    # 2. 加载判定 CSV（AI 判定）
    if judgment_csv and os.path.isfile(judgment_csv):
        try:
            with open(judgment_csv, 'r', encoding='utf-8-sig', newline='') as f:
                for row in csv.DictReader(f):
                    jid = (row.get('job_id') or '').strip()
                    if not jid:
                        continue
                    level_raw = (row.get('优先级判断') or '').strip()
                    level = LEVEL_EN_TO_CN.get(level_raw, level_raw)
                    if level not in ('高', '中', '低'):
                        continue
                    # 仅当 priorities.json 中没有该 job_id 的记录时，才使用 CSV 判定
                    if jid not in judgments:
                        judgments[jid] = {'level': level, 'note': '', 'source': 'ai'}
        except Exception:
            pass

    return judgments


def _read_raw_rows(raw_csv: str) -> list:
    """读取 raw CSV 所有行。"""
    rows = []
    try:
        with open(raw_csv, 'r', encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                rows.append(row)
    except Exception:
        pass
    return rows


def _append_judgment_rows(judgment_csv: str, new_rows: list, append_cols: list):
    """将新判定结果追加到判定 CSV。使用原子写入。"""
    if not new_rows:
        return

    # 读取现有行
    existing_rows = []
    fieldnames = []
    if os.path.isfile(judgment_csv):
        try:
            with open(judgment_csv, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
                existing_rows = list(reader)
        except Exception:
            pass

    if not fieldnames:
        # 从新行推导 fieldnames
        fieldnames = list(new_rows[0].keys())
        for col in append_cols:
            if col not in fieldnames:
                fieldnames.append(col)

    # 合并：更新已有行的判定字段，或追加新行
    existing_jids = {row.get('job_id'): i for i, row in enumerate(existing_rows) if row.get('job_id')}
    for new_row in new_rows:
        jid = new_row.get('job_id')
        if jid and jid in existing_jids:
            # 更新已有行的判定字段
            idx = existing_jids[jid]
            for col in append_cols:
                if col in new_row and new_row[col]:
                    existing_rows[idx][col] = new_row[col]
        else:
            existing_rows.append(new_row)

    # 确保 fieldnames 包含所有追加列
    for col in append_cols:
        if col not in fieldnames:
            fieldnames.append(col)

    # 原子写入
    tmp = judgment_csv + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in existing_rows:
                writer.writerow(row)
        os.replace(tmp, judgment_csv)
    except Exception as e:
        print(f"[judge_monitor] 写入判定 CSV 失败: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)


class JudgeMonitor:
    """优先级判定监控器。

    功能：
    1. 加载 matcher 引擎（单例）
    2. 提供 batch_judge() 批量判定方法
    3. 提供 start_monitor() / stop_monitor() 后台监控
    """

    def __init__(self):
        self._matcher = None
        self._lock = threading.Lock()
        self._monitor_thread = None
        self._monitor_running = False
        self._monitor_output_dir = None
        self._last_judged_count = 0  # 上次判定时的已判定数量
        self._status = {
            'running': False,
            'last_run': None,
            'last_count': 0,
            'total_judged': 0,
            'error': None,
        }

        # 尝试加载 matcher 引擎
        if MATCHER_AVAILABLE:
            try:
                profile_path = os.path.join(MATCHER_CONFIG_DIR, 'candidate_profile.json')
                config_path = os.path.join(MATCHER_CONFIG_DIR, 'match_config.yaml')
                if os.path.isfile(profile_path) and os.path.isfile(config_path):
                    profile = load_profile(profile_path)
                    config = load_config(config_path)
                    self._matcher = Matcher(profile, config)
                    print("[judge_monitor] matcher 引擎加载成功")
                else:
                    print("[judge_monitor] 配置文件不存在，matcher 不可用")
            except Exception as e:
                print(f"[judge_monitor] matcher 引擎加载失败: {e}")
        else:
            print(f"[judge_monitor] matcher 模块导入失败: {_MATCHER_IMPORT_ERROR}")

    @property
    def available(self) -> bool:
        """matcher 引擎是否可用。"""
        return self._matcher is not None

    def get_status(self) -> dict:
        """返回监控状态。"""
        return {
            **self._status,
            'available': self.available,
            'monitor_running': self._monitor_running,
        }

    def judge_batch(self, output_dir: str, mode: str = 'incremental') -> dict:
        """批量判定。

        Args:
            output_dir: 01_raw/ 目录路径
            mode: 'incremental'（增量，每 20 条）或 'full'（全量，所有未判定）

        Returns:
            {ok, judged_count, skipped_count, total, error}
        """
        if not self.available:
            return {'ok': False, 'error': 'matcher 引擎不可用'}

        # H2 修复：全量模式分批处理，每批 BATCH_SIZE 条，避免长时间持锁
        if mode == 'full':
            total_judged = 0
            total_skipped = 0
            total_rows = 0
            last_result = None
            while True:
                with self._lock:
                    result = self._do_judge(output_dir, 'incremental')
                if not result.get('ok'):
                    return result
                total_judged += result.get('judged_count', 0)
                total_skipped += result.get('skipped_count', 0)
                total_rows = result.get('total', total_rows)
                last_result = result
                # 如果本轮判定数量为 0，说明所有未判定的 JD 都已处理完
                if result.get('judged_count', 0) == 0:
                    break
            return {
                'ok': True,
                'judged_count': total_judged,
                'skipped_count': total_skipped,
                'total': total_rows,
                'judgment_csv': last_result.get('judgment_csv') if last_result else None,
            }

        with self._lock:
            return self._do_judge(output_dir, mode)

    def _do_judge(self, output_dir: str, mode: str) -> dict:
        """执行判定（需持有锁）。"""
        # 1. 找到 raw CSV
        raw_csv = _find_latest_raw_csv(output_dir)
        if not raw_csv:
            return {'ok': False, 'error': '未找到爬取数据（缺少 jobs_data*.csv）'}

        # 2. 找到或创建判定 CSV
        result_dir = os.path.join(os.path.dirname(output_dir), '02_result')
        judgment_csv = _find_or_create_judgment_csv(raw_csv, result_dir)
        if not judgment_csv:
            return {'ok': False, 'error': '无法创建判定 CSV'}

        # 3. 加载已有判定
        priorities_path = os.path.join(output_dir, 'priorities.json')
        existing = _load_existing_judgments(judgment_csv, priorities_path)

        # 4. 读取 raw CSV
        raw_rows = _read_raw_rows(raw_csv)
        if not raw_rows:
            return {'ok': False, 'error': 'raw CSV 为空'}

        # 5. 筛选未判定的 JD
        to_judge = []
        skipped = 0
        for row in raw_rows:
            jid = (row.get('job_id') or '').strip()
            if not jid:
                continue
            if jid in existing:
                skipped += 1
                continue
            to_judge.append(row)

        if not to_judge:
            return {'ok': True, 'judged_count': 0, 'skipped_count': skipped,
                    'total': len(raw_rows), 'message': '无未判定的 JD'}

        # 增量模式：如果未判定数量不足 BATCH_SIZE，跳过
        if mode == 'incremental' and len(to_judge) < BATCH_SIZE:
            return {'ok': True, 'judged_count': 0, 'skipped_count': skipped,
                    'total': len(raw_rows),
                    'message': f'未判定 {len(to_judge)} 条，不足 {BATCH_SIZE} 条，等待积累'}

        # 6. 执行判定
        judged_rows = []
        judged_count = 0
        for row in to_judge:
            try:
                jd = enrich_jd(row)
                result = self._matcher.evaluate(jd)
                out_row = dict(row)
                for col in APPEND_COLS:
                    out_row[col] = result.get(col, '')
                judged_rows.append(out_row)
                judged_count += 1
            except Exception as e:
                print(f"[judge_monitor] 判定失败 job_id={row.get('job_id')}: {e}")
                continue

        # 7. 写入判定 CSV
        if judged_rows:
            _append_judgment_rows(judgment_csv, judged_rows, APPEND_COLS)

        # 8. 更新状态
        now = datetime.now().isoformat()
        self._status.update({
            'last_run': now,
            'last_count': judged_count,
            'total_judged': len(existing) + judged_count,
            'error': None,
        })

        # 9. 同步更新 priorities.json（供 Dashboard 显示）
        self._sync_priorities(output_dir, judgment_csv)

        return {
            'ok': True,
            'judged_count': judged_count,
            'skipped_count': skipped,
            'total': len(raw_rows),
            'judgment_csv': judgment_csv,
        }

    def _sync_priorities(self, output_dir: str, judgment_csv: str):
        """将判定结果同步到 priorities.json。"""
        priorities_path = os.path.join(output_dir, 'priorities.json')
        priorities = {}

        # 读取现有 priorities（保留人工判定）
        if os.path.isfile(priorities_path):
            try:
                with open(priorities_path, 'r', encoding='utf-8') as f:
                    priorities = json.load(f)
            except Exception:
                pass

        # 从判定 CSV 读取 AI 判定
        try:
            with open(judgment_csv, 'r', encoding='utf-8-sig', newline='') as f:
                for row in csv.DictReader(f):
                    jid = (row.get('job_id') or '').strip()
                    if not jid:
                        continue
                    level_raw = (row.get('优先级判断') or '').strip()
                    level = LEVEL_EN_TO_CN.get(level_raw, level_raw)
                    if level not in ('高', '中', '低'):
                        continue
                    # 不覆盖已有人工判定
                    existing = priorities.get(jid)
                    if isinstance(existing, dict) and existing.get('note'):
                        continue  # 有人工备注，跳过
                    if isinstance(existing, dict) and existing.get('level'):
                        continue  # 有人工判定，跳过
                    if isinstance(existing, str) and existing in ('高', '中', '低'):
                        continue  # 有人工判定，跳过
                    priorities[jid] = {'level': level, 'note': ''}
        except Exception:
            pass

        # 原子写入
        tmp = priorities_path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(priorities, f, ensure_ascii=False, indent=2)
            os.replace(tmp, priorities_path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)

    def start_monitor(self, output_dir: str):
        """启动后台监控线程。"""
        if self._monitor_running:
            return

        self._monitor_output_dir = output_dir
        self._monitor_running = True
        self._status['running'] = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        print(f"[judge_monitor] 后台监控已启动，轮询间隔 {POLL_INTERVAL}s，批次大小 {BATCH_SIZE}")

    def stop_monitor(self):
        """停止后台监控。"""
        self._monitor_running = False
        self._status['running'] = False
        if self._monitor_thread:
            self._monitor_thread = None
        print("[judge_monitor] 后台监控已停止")

    def _monitor_loop(self):
        """后台监控主循环。

        自动停止条件（满足任一即停止）：
        1. 连续 IDLE_LIMIT 轮无新判定（默认 10 轮 × 10s = 100 秒）
        2. 爬取进程已退出且无新判定
        """
        IDLE_LIMIT = 10  # 连续无判定轮数上限
        idle_count = 0

        while self._monitor_running:
            # 每次循环清除旧错误（避免过时错误残留）
            self._status['error'] = None
            judged_this_round = False

            try:
                output_dir = self._monitor_output_dir
                if output_dir and os.path.isdir(output_dir):
                    result = self.judge_batch(output_dir, mode='incremental')
                    if result.get('judged_count', 0) > 0:
                        print(f"[judge_monitor] 增量判定完成: {result['judged_count']} 条")
                        judged_this_round = True
            except Exception as e:
                self._status['error'] = str(e)
                print(f"[judge_monitor] 监控异常: {e}")

            # 自动停止逻辑
            if judged_this_round:
                idle_count = 0
            else:
                idle_count += 1
                if idle_count >= IDLE_LIMIT:
                    print(f"[judge_monitor] 连续 {IDLE_LIMIT} 轮无新判定，自动停止监控")
                    break

            # 等待下一次轮询
            for _ in range(POLL_INTERVAL):
                if not self._monitor_running:
                    break
                time.sleep(1)

        # 循环结束，清理状态
        self._monitor_running = False
        self._status['running'] = False


# 全局单例
judge_monitor = JudgeMonitor()
