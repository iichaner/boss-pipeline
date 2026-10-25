#!/usr/bin/env python3
"""长期监控任务数据层：状态持久化 + 轮次差异计算 + 调度时间计算。

纯数据模块：不起 HTTP、不开子进程、不依赖第三方库，方便单测与复用。
被 monitor_scheduler.py（调度）与 serve_dashboard.py（API）共同引用。

设计要点（对应 PLAN.md）：
- 每个长期任务 = history_root 下一个 `长期-<日期>-<搜索词>/` 目录
- monitor.json  任务配置 + 运行态（原子写，进程重启可恢复）
- seen_jobs.json 跨轮去重主表（job_id → 元数据 + 出现轮次 + JD 指纹）
- rounds/<时间戳>/01_raw/  每轮独立爬取目录
- rounds/<时间戳>/summary.json  每轮差异报告
"""

import os
import json
import csv
import glob as glob_mod
import hashlib
import random
from datetime import datetime, timedelta

PREFIX = '长期-'
MONITOR_FILE = 'monitor.json'
SEEN_FILE = 'seen_jobs.json'

# ── 默认参数（PLAN.md §4.6，全部可被任务配置覆盖）────────────────
DEFAULT_SCROLL = {
    'max_scroll': 50,
    'max_jobs': 300,
    'base_wait': 20,
    'scroll_wait': 5,
    'scroll_delta': '1.5-2.5',
    'job_gap': '1.0-3.0',
    'detail_jitter': 3,
}
DEFAULT_INTERVAL_HOURS = 3
DEFAULT_DURATION_HOURS = 48
DEFAULT_QUIET_HOURS = ['00:00', '08:00']
DEFAULT_RELOGIN_AFTER_HOURS = 48
DEFAULT_MAX_FAILURES = 3
DEFAULT_RETRY_MINUTES = 20
DEFAULT_ROUND_TIMEOUT_MINUTES = 180
KEEP_ROUNDS_DETAIL = 20          # 保留最近多少轮的原始明细，更早的只留 summary

SCROLL_KEYS = tuple(DEFAULT_SCROLL.keys())


# ── 基础 IO ────────────────────────────────────────────────
def atomic_write_json(path: str, data) -> None:
    """原子写 JSON：tmp + os.replace，避免写一半被 kill 导致状态损坏。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: str, default=None):
    if not path or not os.path.isfile(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def now_iso() -> str:
    return datetime.now().isoformat(timespec='seconds')


def parse_iso(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def parse_hm(s: str) -> int:
    """'08:00' → 480（分钟）。非法值抛 ValueError。"""
    hh, mm = str(s).strip().split(':')[:2]
    v = int(hh) * 60 + int(mm)
    if not (0 <= v < 1440):
        raise ValueError('bad time: %r' % s)
    return v


# ── 时间窗口计算 ───────────────────────────────────────────
def in_quiet(dt: datetime, quiet_hours) -> bool:
    """判断时刻是否落在禁跑窗口内。支持跨零点（如 22:00-06:00）。"""
    try:
        start, end = parse_hm(quiet_hours[0]), parse_hm(quiet_hours[1])
    except Exception:
        return False
    if start == end:
        return False
    m = dt.hour * 60 + dt.minute
    if start < end:
        return start <= m < end
    return m >= start or m < end


def next_allowed(dt: datetime, quiet_hours, jitter_max: int = 0, rng=None) -> datetime:
    """把落在禁跑窗口的时间顺延到窗口结束后（可加随机抖动防规律化）。"""
    rng = rng or random
    cur = dt
    guard = 0
    while in_quiet(cur, quiet_hours) and guard < 3:
        guard += 1
        try:
            end = parse_hm(quiet_hours[1])
        except Exception:
            break
        base = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        candidate = base + timedelta(minutes=end)
        if candidate <= cur:
            candidate = candidate + timedelta(days=1)
        cur = candidate
    if jitter_max > 0:
        cur = cur + timedelta(seconds=rng.randint(0, int(jitter_max) * 60))
    return cur.replace(second=0, microsecond=0)


def compute_next_run(base: datetime, interval_hours: float, quiet_hours,
                     jitter_max: int = 15, rng=None) -> datetime:
    """下一次执行时间：base + 间隔，再避开禁跑窗口。"""
    interval_hours = max(0.5, float(interval_hours or DEFAULT_INTERVAL_HOURS))
    target = base + timedelta(hours=interval_hours)
    return next_allowed(target, quiet_hours, jitter_max, rng)


# ── 任务存储 ───────────────────────────────────────────────
class MonitorStore:
    """history_root 下所有长期监控任务的读写。"""

    def __init__(self, root: str):
        self.root = root

    # --- 路径
    def monitor_dir(self, mid: str) -> str:
        return os.path.join(self.root, mid)

    def _mpath(self, mid: str) -> str:
        return os.path.join(self.monitor_dir(mid), MONITOR_FILE)

    def seen_path(self, mid: str) -> str:
        return os.path.join(self.monitor_dir(mid), SEEN_FILE)

    def rounds_dir(self, mid: str) -> str:
        return os.path.join(self.monitor_dir(mid), 'rounds')

    def diff_dir(self, mid: str) -> str:
        return os.path.join(self.monitor_dir(mid), 'diff')

    def log_path(self, mid: str) -> str:
        return os.path.join(self.monitor_dir(mid), 'monitor.log')

    def path_for_raw(self, mid: str, round_name: str) -> str:
        return os.path.join(self.rounds_dir(mid), round_name, '01_raw')

    # --- 增删查
    def exists(self, mid: str) -> bool:
        return os.path.isfile(self._mpath(mid))

    def _unique_id(self, label: str) -> str:
        base = label if label.startswith(PREFIX) else PREFIX + label
        mid, i = base, 2
        while self.exists(mid) or os.path.isdir(self.monitor_dir(mid)):
            mid = f'{base}-{i}'
            i += 1
        return mid

    def create(self, params: dict) -> tuple:
        """新建监控任务。params: {url, label?, interval_hours?, duration_hours?,
        quiet_hours?, scroll?, relogin_after_hours?}"""
        url = (params.get('url') or '').strip()
        if not url:
            return None, {'error': '缺少列表页 URL'}
        if 'zhipin.com' not in url:
            return None, {'error': 'URL 不是 Boss 直聘页面'}

        date_str = datetime.now().strftime('%Y-%m-%d')
        query = ''
        try:
            import urllib.parse as _up
            q = _up.parse_qs(_up.urlparse(url).query)
            query = (q.get('query') or [''])[0].strip().replace(' ', '')
        except Exception:
            pass
        label = (params.get('label') or '').strip() or f'{date_str}-{query or "监控"}'
        mid = self._unique_id(label if label.startswith(PREFIX) else f'{PREFIX}{label}')

        interval = float(params.get('interval_hours') or DEFAULT_INTERVAL_HOURS)
        duration = float(params.get('duration_hours') or DEFAULT_DURATION_HOURS)
        quiet = params.get('quiet_hours') or list(DEFAULT_QUIET_HOURS)
        scroll = dict(DEFAULT_SCROLL)
        for k in SCROLL_KEYS:
            if params.get('scroll', {}).get(k) not in (None, ''):
                scroll[k] = params['scroll'][k]

        now = datetime.now()
        created = now.isoformat(timespec='seconds')
        # duration_hours<=0 → 不限时长，直到人工停止
        end_at = (now + timedelta(hours=duration)).isoformat(timespec='seconds') if duration > 0 else None
        start_paused = bool(params.get('start_paused'))

        task = {
            'monitor_id': mid,
            'label': mid,
            'list_url': url,
            'status': 'paused' if start_paused else 'running',          # running|paused|stopped|expired|blocked
            'kind': 'monitor',
            'created_at': created,
            'updated_at': created,
            'started_at': created,
            'interval_hours': interval,
            'duration_hours': duration,
            'end_at': end_at,
            'quiet_hours': quiet,
            'scroll': scroll,
            'login': {
                'relogin_after_hours': float(params.get('relogin_after_hours')
                                             or DEFAULT_RELOGIN_AFTER_HOURS),
                'last_login_verified_at': None,
                'stale': False,
            },
            'next_run_at': None if start_paused else now.isoformat(timespec='seconds'),  # 创建即跑第一轮
            'rounds': 0,
            'last_round': None,
            'consecutive_failures': 0,
            'guard_fail_streak': 0,
            'stats': {'total_jobs': 0, 'new_total': 0, 'missing_total': 0,
                      'changed_total': 0, 'failed_rounds': 0},
            'alerts': [],
            'active_round': None,
        }
        os.makedirs(self.rounds_dir(mid), exist_ok=True)
        os.makedirs(self.diff_dir(mid), exist_ok=True)
        atomic_write_json(self.seen_path(mid), {})
        self.save(task)
        return mid, task

    def load(self, mid: str):
        return read_json(self._mpath(mid))

    def save(self, task: dict) -> dict:
        task['updated_at'] = now_iso()
        atomic_write_json(self._mpath(task['monitor_id']), task)
        return task

    def list(self) -> list:
        out = []
        if not self.root or not os.path.isdir(self.root):
            return out
        for name in os.listdir(self.root):
            if not name.startswith(PREFIX):
                continue
            t = read_json(self._mpath(name))
            if t:
                t.setdefault('monitor_id', name)
                out.append(t)
        out.sort(key=lambda t: t.get('created_at', ''), reverse=True)
        return out

    def running_tasks(self) -> list:
        return [t for t in self.list() if t.get('status') == 'running']

    # --- 轮次目录
    def new_round_name(self, when: datetime = None) -> str:
        when = when or datetime.now()
        return when.strftime('%Y-%m-%dT%H%M')

    def round_raw_dir(self, mid: str, round_name: str) -> str:
        d = self.path_for_raw(mid, round_name)
        os.makedirs(d, exist_ok=True)
        return d

    def append_alert(self, task: dict, level: str, code: str, message: str) -> dict:
        """就地追加告警到 task（**由调用方负责 save**）。

        之前这里自己 load+save，调用方随后又 save 内存对象会把告警覆盖掉，
        导致熔断/登录失效告警静默丢失。现改为纯内存变更，写入时机只有一个。
        只保留最近 50 条。
        """
        task.setdefault('alerts', []).append({
            'at': now_iso(), 'level': level, 'code': code,
            'message': message, 'acked': False,
        })
        task['alerts'] = task['alerts'][-50:]
        return task

    def prune_rounds(self, mid: str, keep: int = KEEP_ROUNDS_DETAIL):
        """只保留最近 N 轮的原始 CSV/cache 明细，更早轮次删除 01_raw 以省磁盘。
        summary.json 永久保留，数据不丢。"""
        rd = self.rounds_dir(mid)
        if not os.path.isdir(rd):
            return
        names = sorted(n for n in os.listdir(rd)
                       if os.path.isdir(os.path.join(rd, n)))
        for n in names[:-keep] if keep > 0 else []:
            raw = os.path.join(rd, n, '01_raw')
            summary = os.path.join(rd, n, 'summary.json')
            if os.path.isfile(summary) and os.path.isdir(raw):
                try:
                    import shutil
                    shutil.rmtree(raw)
                except Exception:
                    pass


# ── 轮次差异计算 ───────────────────────────────────────────
def read_round_jobs(raw_dir: str) -> list:
    """读取一轮爬取结果。优先 jobs_cache.json，回落到 CSV。"""
    cache = read_json(os.path.join(raw_dir, 'jobs_cache.json'))
    jobs = []
    if isinstance(cache, list):
        for j in cache:
            jid = (j.get('job_id') or '').strip()
            if jid:
                jobs.append({
                    'job_id': jid,
                    'title': j.get('职位名称') or '',
                    'company': j.get('公司名称') or '',
                    'salary': j.get('薪资') or '',
                    'area': j.get('区域') or '',
                    'url': j.get('job_url') or '',
                    'desc': j.get('职位描述') or '',
                })
        return jobs
    for fp in sorted(glob_mod.glob(os.path.join(raw_dir, '*.csv'))):
        try:
            with open(fp, 'r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    jid = (row.get('job_id') or '').strip()
                    if not jid:
                        continue
                    jobs.append({
                        'job_id': jid,
                        'title': row.get('职位名称') or '',
                        'company': row.get('公司名称') or '',
                        'salary': row.get('薪资') or '',
                        'area': row.get('区域') or '',
                        'url': row.get('岗位链接') or row.get('job_url') or '',
                        'desc': row.get('职位描述') or '',
                    })
        except Exception:
            continue
    return jobs


def _jd_fingerprint(job: dict) -> str:
    """JD 指纹：描述+薪资+标题的组合哈希，用于识别「内容变更」。"""
    src = '|'.join([job.get('desc', ''), job.get('salary', ''), job.get('title', '')])
    return hashlib.md5(src.encode('utf-8', 'ignore')).hexdigest()[:12]


def apply_round(seen_path: str, round_no: int, jobs: list) -> dict:
    """把本轮结果并入跨轮主表，算出差异。

    返回：{scraped, new[], missing[], reappeared[], changed[], absent_streak_over}
    - new        : 历史从未出现过的 job_id
    - missing    : 本轮未出现（不等于下架，列表页有翻页/排序波动）
    - suspected  : 连续 ≥2 轮未出现的 missing，才标注「疑似下架」
    - reappeared : 之前 missing、本轮又出现的
    - changed    : 同一 job_id 的 JD 指纹变化（薪资/描述/标题被改写）
    """
    seen = read_json(seen_path, {}) or {}
    this_ids = {j['job_id'] for j in jobs}
    prev_ids = set(seen.keys())

    new, reappeared, changed = [], [], []
    for j in jobs:
        jid = j.get('job_id') or ''
        if not jid:
            continue
        title = j.get('title') or ''
        company = j.get('company') or ''
        salary = j.get('salary') or ''
        fp = _jd_fingerprint(j)
        rec = seen.get(jid)
        if rec is None:
            seen[jid] = {
                'title': title, 'company': company, 'salary': salary,
                'area': j.get('area') or '', 'url': j.get('url') or '', 'jd_fp': fp,
                'first_round': round_no, 'last_round': round_no, 'absent_streak': 0,
            }
            new.append({'job_id': jid, 'title': title, 'company': company,
                        'salary': salary, 'area': j.get('area') or '',
                        'url': j.get('url') or ''})
            continue

        if rec.get('absent_streak', 0) > 0:
            reappeared.append({'job_id': jid, 'title': title or rec.get('title'),
                               'absent_rounds': rec.get('absent_streak')})
        if rec.get('jd_fp') != fp:
            changed.append({'job_id': jid, 'title': title,
                            'old_salary': rec.get('salary'), 'new_salary': salary})
        rec.update({'title': title or rec.get('title', ''),
                    'company': company or rec.get('company', ''),
                    'salary': salary or rec.get('salary', ''),
                    'area': j.get('area') or rec.get('area', ''),
                    'url': j.get('url') or rec.get('url', ''),
                    'jd_fp': fp, 'last_round': round_no, 'absent_streak': 0})

    missing = []
    for jid in sorted(prev_ids - this_ids):
        rec = seen[jid]
        rec['absent_streak'] = rec.get('absent_streak', 0) + 1
        missing.append({
            'job_id': jid, 'title': rec.get('title', ''), 'company': rec.get('company', ''),
            'salary': rec.get('salary', ''), 'last_seen_round': rec.get('last_round'),
            'absent_rounds': rec['absent_streak'],
            'suspected_delisted': rec['absent_streak'] >= 2,
        })

    atomic_write_json(seen_path, seen)
    return {
        'round': round_no,
        'scraped': len(jobs),
        'new': new,
        'missing': missing,
        'reappeared': reappeared,
        'changed': changed,
        'suspected_delisted': sum(1 for m in missing if m['suspected_delisted']),
        'total_seen': len(seen),
    }


# ── 前端视图 ───────────────────────────────────────────────
def task_public(task: dict) -> dict:
    """把内部状态转成前端需要的字段（含倒计时、登录态时效）。"""
    now = datetime.now()
    nxt = parse_iso(task.get('next_run_at'))
    last_verified = parse_iso((task.get('login') or {}).get('last_login_verified_at'))
    relogin_after = float((task.get('login') or {}).get('relogin_after_hours')
                          or DEFAULT_RELOGIN_AFTER_HOURS)
    out = dict(task)
    out['next_run_in'] = int((nxt - now).total_seconds()) if nxt else None
    out['login_age_hours'] = round((now - last_verified).total_seconds() / 3600, 1) if last_verified else None
    out['login_stale'] = bool(last_verified is None
                              or (now - last_verified).total_seconds() > relogin_after * 3600)
    end_at = parse_iso(task.get('end_at'))
    out['remaining_hours'] = round((end_at - now).total_seconds() / 3600, 1) if end_at else None
    return out


if __name__ == '__main__':
    import sys
    if '--self-test' in sys.argv:
        # 时间窗口
        q = ['00:00', '08:00']
        assert in_quiet(datetime(2026, 10, 25, 3, 0), q) is True
        assert in_quiet(datetime(2026, 10, 25, 9, 0), q) is False
        assert next_allowed(datetime(2026, 10, 25, 2, 0), q) == datetime(2026, 10, 25, 8, 0)
        # 跨零点窗口
        q2 = ['22:00', '06:00']
        assert in_quiet(datetime(2026, 10, 25, 23, 30), q2) is True
        assert in_quiet(datetime(2026, 10, 25, 12, 0), q2) is False
        assert in_quiet(datetime(2026, 10, 25, 5, 0), q2) is True
        assert next_allowed(datetime(2026, 10, 25, 23, 30), q2).hour == 6
        # 间隔计算避开窗口
        n = compute_next_run(datetime(2026, 10, 25, 6, 0), 3, q, jitter_max=0)
        assert n == datetime(2026, 10, 25, 9, 0), n
        print('quiet-hours OK')

        # 存储 + 差异
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            st = MonitorStore(td)
            mid, t = st.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=总助&city=101020100'})
            assert mid.startswith(PREFIX), mid
            assert t['status'] == 'running'
            r1 = [{'job_id': 'a', 'title': 'A', 'company': 'X', 'salary': '10K', 'desc': 'x'},
                  {'job_id': 'b', 'title': 'B', 'company': 'Y', 'salary': '20K', 'desc': 'y'}]
            d1 = apply_round(st.seen_path(mid), 1, r1)
            assert len(d1['new']) == 2 and d1['total_seen'] == 2, d1
            # 第2轮：b 涨薪（changed），新增 c，a 消失（missing 1 轮，还不算疑似下架）
            r2 = [{'job_id': 'b', 'title': 'B', 'company': 'Y', 'salary': '25K', 'desc': 'y'},
                  {'job_id': 'c', 'title': 'C', 'company': 'Z', 'salary': '30K', 'desc': 'z'}]
            d2 = apply_round(st.seen_path(mid), 2, r2)
            assert [x['job_id'] for x in d2['new']] == ['c'], d2
            assert [x['job_id'] for x in d2['missing']] == ['a'], d2
            assert d2['missing'][0]['suspected_delisted'] is False, d2
            assert len(d2['changed']) == 1 and d2['changed'][0]['new_salary'] == '25K', d2
            # 第3轮：a 继续缺席 → 疑似下架
            d3 = apply_round(st.seen_path(mid), 3, [{'job_id': 'b', 'title': 'B',
                                                    'company': 'Y', 'salary': '25K', 'desc': 'y'}])
            assert d3['suspected_delisted'] == 1, d3
            # 第4轮：a 回归 → reappeared
            d4 = apply_round(st.seen_path(mid), 4, r1)
            assert [x['job_id'] for x in d4['reappeared']] == ['a'], d4
            # 唯一 ID / 列表 / 视图
            mid2, _ = st.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=总助&city=101020100'})
            assert mid2 != mid
            assert len(st.list()) == 2
            pv = task_public(st.load(mid))
            assert pv['next_run_in'] is not None and 'login_stale' in pv
            print('store+diff OK')
        print('monitor_state self-test PASS')
        sys.exit(0)
    print('用法: python3 monitor_state.py --self-test')
