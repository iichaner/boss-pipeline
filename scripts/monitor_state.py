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
import re
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
KEEP_ROUNDS_DETAIL = 50          # 保留最近多少轮的原始明细，更早的只留 summary（P2：20→50）
VOLUME_FLOOR_RATIO = 0.6         # 本轮列表规模 < 上轮 × 该比例 → 判「抓取量异常」无效轮（P0）
VOLUME_FLOOR_ABS = 5             # 绝对下限：连 5 条都不到一律无效（首轮/无基线也适用）
DETAIL_SAMPLE_PER_ROUND = 12      # 每轮抽样重抓详情的条数，其余已知岗位跳过详情（分层抓取）
PRESEED_CSV = 'jobs_data_preseed.csv'   # 预置 CSV：让爬虫按续爬逻辑跳过这些 job_id 的详情页
PRESEED_JSON = '_preseed.json'

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
        # M-7：清洗路径穿越与引号注入字符，label 会拼进文件路径并出现在内联 onclick 里
        label = re.sub(r"[/\\\\'\"\.\x00-\x1f]+", '_', label).strip('_') or '监控'
        mid = self._unique_id(label if label.startswith(PREFIX) else f'{PREFIX}{label}')

        # 数值参数：必须区分「没传」与「传了 0」。
        # 旧写法 params.get('duration_hours') or DEFAULT 会把 0（=不限时长）当成缺省→ 48h。
        def _num(key, default, minv=None):
            raw = params.get(key)
            if raw is None or raw == '':
                return float(default)
            try:
                v = float(raw)
            except (TypeError, ValueError):
                return float(default)
            if minv is not None:
                v = max(float(minv), v)
            return v

        interval = _num('interval_hours', DEFAULT_INTERVAL_HOURS, 1)
        duration = _num('duration_hours', DEFAULT_DURATION_HOURS, 0)
        relogin_after = _num('relogin_after_hours', DEFAULT_RELOGIN_AFTER_HOURS, 1)
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
                'relogin_after_hours': relogin_after,
                'last_login_verified_at': None,
                'stale': False,
            },
            'next_run_at': None if start_paused else now.isoformat(timespec='seconds'),  # 创建即跑第一轮
            'rounds': 0,
            'last_round': None,
            'consecutive_failures': 0,
            'guard_fail_streak': 0,
            'volume_fail_streak': 0,
            'stats': {'total_jobs': 0, 'last_scraped': 0, 'new_total': 0, 'missing_total': 0,
                      'changed_total': 0, 'reposted_total': 0, 'failed_rounds': 0},
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
                    'city': j.get('城市') or '',
                    'exp': j.get('经验要求') or '',
                    'edu': j.get('学历要求') or '',
                    'security_id': j.get('security_id') or '',
                    'created': j.get('创建日期') or '',
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
                        'city': row.get('城市') or '',
                        'exp': row.get('经验要求') or '',
                        'edu': row.get('学历要求') or '',
                        'security_id': row.get('security_id') or '',
                        'created': row.get('创建日期') or '',
                    })
        except Exception:
            continue
    return jobs


def _jd_fingerprint(job: dict) -> str:
    """JD 指纹：描述+薪资+标题的组合哈希，用于识别「内容变更」。

    描述先截断 [:4000] 再哈希，与 seen 表入库截断对齐（S-3）：
    否则超长 JD 会在「实抓轮(全文) vs 预置轮(截断文)」之间稳定产生假变更。
    """
    desc = (job.get('desc') or '')[:4000]
    src = '|'.join([desc, job.get('salary', ''), job.get('title', '')])
    return hashlib.md5(src.encode('utf-8', 'ignore')).hexdigest()[:12]


def _job_key(rec: dict) -> str:
    """重发识别指纹：公司+标题+区域（去空白/小写）。

    岗位重新发布时 Boss 会换 job_id，但公司/标题/区域通常不变；
    靠这个指纹把「转世重发」识别出来，避免每次都算成 1 新增 + 1 未出现。
    """
    parts = []
    for k in ('company', 'title', 'area'):
        parts.append(''.join((rec.get(k) or '').split()).lower())
    return '|'.join(parts)


def apply_round(seen_path: str, round_no: int, cards: list, details: list) -> dict:
    """把本轮结果并入跨轮主表，算出差异。

    存在性（this_ids）只来自 cards（列表页实际可见 = list_jobs.json），
    预置回显绝不参与在场判定——这是分层抓取能成立的前提（S-1 根治）。
    details 只用于刷新内容（desc/薪资等）；未实抓详情的岗位沿用主表旧内容，
    因此不会产生假「未出现」、假「变更」。

    返回：{scraped, new[], missing[], reappeared[], changed[], reposted[], suspected_delisted, total_seen}
    - new        : 本次列表页出现、历史从未见过的 job_id
    - missing    : 历史有、本次列表页没有（不等于下架；连续 ≥2 轮缺席才标疑似下架）
    - reappeared : 之前 missing、本轮又出现
    - changed    : 同一 job_id 本轮实抓详情后 JD 指纹变化（薪资/描述/标题被改写）
    - reposted   : job_id 新，但「公司+标题+区域」命中当前缺席记录 → 同一岗位重发
    """
    seen = read_json(seen_path, {}) or {}
    this_ids = {c['job_id'] for c in cards if c.get('job_id')}
    prev_ids = set(seen.keys())

    detail_map = {}
    for d in (details or []):
        jid = d.get('job_id')
        if jid:
            detail_map[jid] = d

    # 重发索引：指纹 → 未替代的 job_id 列表
    fp_index = {}
    for jid, rec in seen.items():
        if rec.get('superseded_by'):
            continue
        key = _job_key(rec)
        if rec.get('company') and rec.get('title'):
            fp_index.setdefault(key, []).append(jid)

    new, reappeared, changed, reposted = [], [], [], []
    for c in cards:
        jid = c.get('job_id') or ''
        if not jid:
            continue
        title = c.get('title') or ''
        company = c.get('company') or ''
        salary = c.get('salary') or ''
        detail = detail_map.get(jid)
        # 指纹：实抓轮用抓到内容；未实抓轮沿用主表旧内容 → 不产生假变更
        eff_desc = (detail or {}).get('desc') or (seen.get(jid) or {}).get('desc') or ''
        fp = _jd_fingerprint({'desc': eff_desc, 'salary': salary, 'title': title})
        rec = seen.get(jid)
        if rec is None:
            key = _job_key({'company': company, 'title': title, 'area': c.get('area') or ''})
            cand = [x for x in fp_index.get(key, [])
                    if x not in this_ids and not seen[x].get('superseded_by')]
            if cand and company and title:
                old_id = max(cand, key=lambda x: seen[x].get('absent_streak', 0))
                seen[old_id]['superseded_by'] = jid
                seen[jid] = {
                    'title': title, 'company': company, 'salary': salary,
                    'area': c.get('area') or '', 'city': (detail or {}).get('city') or '',
                    'url': c.get('url') or '', 'desc': ((detail or {}).get('desc') or '')[:4000],
                    'exp': (detail or {}).get('exp') or '', 'edu': (detail or {}).get('edu') or '',
                    'security_id': (detail or {}).get('security_id') or '',
                    'created': (detail or {}).get('created') or '',
                    'jd_fp': fp,
                    'first_round': seen[old_id].get('first_round', round_no),
                    'last_round': round_no, 'absent_streak': 0, 'repost_of': old_id,
                }
                reposted.append({'job_id': jid, 'title': title, 'company': company,
                                 'salary': salary, 'area': c.get('area') or '',
                                 'url': c.get('url') or '', 'repost_of': old_id})
                continue
            seen[jid] = {
                'title': title, 'company': company, 'salary': salary,
                'area': c.get('area') or '', 'city': (detail or {}).get('city') or '',
                'url': c.get('url') or '', 'desc': ((detail or {}).get('desc') or '')[:4000],
                'exp': (detail or {}).get('exp') or '', 'edu': (detail or {}).get('edu') or '',
                'security_id': (detail or {}).get('security_id') or '',
                'created': (detail or {}).get('created') or '',
                'jd_fp': fp,
                'first_round': round_no, 'last_round': round_no, 'absent_streak': 0,
            }
            new.append({'job_id': jid, 'title': title, 'company': company,
                        'salary': salary, 'area': c.get('area') or '',
                        'url': c.get('url') or ''})
            continue

        if rec.get('absent_streak', 0) > 0:
            reappeared.append({'job_id': jid, 'title': title or rec.get('title'),
                               'absent_rounds': rec.get('absent_streak')})
        if detail is not None and rec.get('jd_fp') != fp:
            changed.append({'job_id': jid, 'title': title,
                            'old_salary': rec.get('salary'), 'new_salary': salary})
        rec.update({'title': title or rec.get('title', ''),
                    'company': company or rec.get('company', ''),
                    'salary': salary or rec.get('salary', ''),
                    'area': c.get('area') or rec.get('area', ''),
                    'city': (detail or {}).get('city') or rec.get('city', ''),
                    'url': c.get('url') or rec.get('url', ''),
                    'desc': ((detail or {}).get('desc') or rec.get('desc') or '')[:4000],
                    'exp': (detail or {}).get('exp') or rec.get('exp', ''),
                    'edu': (detail or {}).get('edu') or rec.get('edu', ''),
                    'security_id': (detail or {}).get('security_id') or rec.get('security_id', ''),
                    'created': (detail or {}).get('created') or rec.get('created', ''),
                    'jd_fp': fp, 'last_round': round_no, 'absent_streak': 0})

    missing = []
    for jid in sorted(prev_ids - this_ids):
        rec = seen[jid]
        if rec.get('superseded_by'):
            continue   # 已被重发替代：既不算「未出现」，也不删历史
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
        'scraped': len(cards),
        'new': new,
        'missing': missing,
        'reappeared': reappeared,
        'changed': changed,
        'reposted': reposted,
        'suspected_delisted': sum(1 for m in missing if m['suspected_delisted']),
        'total_seen': len(seen),
    }


def read_round_cards(raw_dir: str) -> list:
    """读本轮「列表页实际可见」岗位卡片（list_jobs.json）。

    存在性判定的唯一依据（新增/未出现/重发/抓取量闸门都基于它）。
    旧版爬虫没有该文件时回退到 jobs_cache.json 的 id，保证不崩。
    """
    cards = read_json(os.path.join(raw_dir, 'list_jobs.json'))
    if isinstance(cards, list):
        out = []
        for c in cards:
            jid = (c.get('job_id') or '').strip()
            if not jid:
                continue
            out.append({'job_id': jid,
                        'title': c.get('title') or c.get('职位名称') or '',
                        'company': c.get('company') or c.get('公司名称') or '',
                        'salary': c.get('salary') or c.get('薪资') or '',
                        'area': c.get('area') or c.get('区域') or '',
                        'url': c.get('url') or c.get('job_url') or c.get('岗位链接') or ''})
        return out
    jobs = read_round_jobs(raw_dir)
    return [{'job_id': j['job_id'], 'title': j.get('title', ''),
             'company': j.get('company', ''), 'salary': j.get('salary', ''),
             'area': j.get('area', ''), 'url': j.get('url', '')} for j in jobs]


def preseed_rows(seen: dict, exclude_ids=None) -> list:
    """把「本轮不打算重抓详情」的已知岗位转成爬虫 CSV 行（分层抓取）。

    写成 jobs_data_preseed.csv 后，爬虫 load_existing_job_ids() 会把这些 job_id
    视为已抓而跳过详情页（每个详情约 20s），单轮从 1~2 小时压到 10 分钟内；
    该 CSV 同时为「本轮详情查看」兜底（含上次抓到的 JD 文本）。
    """
    exclude_ids = set(exclude_ids or ())
    rows = []
    for jid, rec in (seen or {}).items():
        if jid in exclude_ids or rec.get('superseded_by'):
            continue
        rows.append({
            '职位名称': rec.get('title') or '', '薪资': rec.get('salary') or '',
            '经验要求': rec.get('exp') or '', '学历要求': rec.get('edu') or '',
            '公司名称': rec.get('company') or '', '城市': rec.get('city') or '',
            '区域': rec.get('area') or '', 'job_id': jid,
            'job_url': rec.get('url') or '', 'security_id': rec.get('security_id') or '',
            '职位描述': rec.get('desc') or '', '创建日期': rec.get('created') or '',
        })
    rows.sort(key=lambda r: r['job_id'])
    return rows


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

            # 不限时长（duration_hours=0）→ end_at 必须为 None
            mid0, t0 = st.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=不限',
                                  'duration_hours': 0})
            assert t0['end_at'] is None, t0['end_at']
            assert t0['duration_hours'] == 0, t0['duration_hours']
            # 间隔非法值 clamp 到 >=1
            midbad, tb = st.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=零',
                                    'interval_hours': 0})
            assert tb['interval_hours'] == 1, tb['interval_hours']
            print('defaults OK')

        print('monitor_state self-test PASS')
        sys.exit(0)
    print('用法: python3 monitor_state.py --self-test')
