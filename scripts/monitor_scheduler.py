#!/usr/bin/env python3
"""长期监控任务调度器 + 环境守卫。

职责：
- 后台线程每 30s 扫一次 running 任务，到点就爬一轮（全局串行，绝不和手动爬取抢浏览器）
- 每轮爬取前必过环境守卫：CDP 在线 → 列表页在 → 登录态有效 → 详情页抽样校验
  守卫不通过一律不爬，避免登录失效时抓到半空页面变成脏数据
- 每轮结束做跨轮 diff，写 rounds/<时间戳>/summary.json
- 登录态超过 N 小时未验证 → 黄条提醒；连续守卫失败 → 自动暂停并告警
- 进程/机器重启后 load 状态即可补跑（next_run_at 过期立即执行，受夜间窗口约束）

依赖注入：调度器不自己起爬虫，通过 crawl_runner 调用外部（serve_dashboard 的 crawl_manager），
保持「同一时刻只有一个爬取子进程」这一既有约束。
"""

import os
import sys
import json
import csv
import time
import signal
import http.client
import threading
import traceback
import urllib.parse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from monitor_state import (  # noqa: E402
    MonitorStore, read_json, atomic_write_json, now_iso, parse_iso,
    in_quiet, next_allowed, compute_next_run, read_round_jobs, apply_round,
    preseed_rows, read_round_cards,
    DEFAULT_RETRY_MINUTES, DEFAULT_MAX_FAILURES, DEFAULT_ROUND_TIMEOUT_MINUTES,
    SCROLL_KEYS,
    VOLUME_FLOOR_RATIO, VOLUME_FLOOR_ABS, DETAIL_SAMPLE_PER_ROUND,
    PRESEED_CSV, PRESEED_JSON,
)

try:
    import websocket  # websocket-client，serve_dashboard 已依赖
except ImportError:
    websocket = None

CDP_HOST = '127.0.0.1'
CDP_PORT = 9222
# 登录/风控页特征（命中即判定登录态失效，本轮不爬）
LOGIN_MARKERS = ('登录', 'login', '验证', 'verify', 'security')
LIST_CHECK_JS = """
(function(){
  var t = document.title || '';
  var u = location.href || '';
  var cards = document.querySelectorAll('.job-card-body, .job-card-wrapper, li.job-card-box').length;
  var body = (document.body && document.body.innerText || '').slice(0, 800);
  var needLogin = /请先登录|立即登录|扫码登录/.test(body);
  return JSON.stringify({title: t, url: u, cards: cards, needLogin: needLogin});
})()
"""
DETAIL_CHECK_JS = """
(function(){
  var t = document.title || '';
  var u = location.href || '';
  var body = (document.body && document.body.innerText || '').slice(0, 800);
  var needLogin = /请先登录|立即登录|扫码登录|登录查看/.test(body);
  var desc = document.querySelectorAll('.job-sec-text, .job-detail-section, .brief-desc').length;
  return JSON.stringify({title: t, url: u, needLogin: needLogin, desc: desc});
})()
"""


def _normalize_ws(url: str) -> str:
    if not url:
        return url
    return url.replace('[::1]', '127.0.0.1').replace('localhost', '127.0.0.1')


class EnvGuard:
    """爬取前置检查。任何一步失败都拒绝开爬。"""

    def __init__(self, log=print):
        self.log = log

    # --- CDP 基础
    def _tabs(self):
        try:
            conn = http.client.HTTPConnection(CDP_HOST, CDP_PORT, timeout=5)
            conn.request('GET', '/json')
            resp = conn.getresponse()
            body = resp.read().decode('utf-8', 'ignore')
            conn.close()
            tabs = json.loads(body)
            return [t for t in tabs if t.get('type') == 'page'], None
        except Exception as e:
            return None, f'CDP 不可达: {e}'

    def _cdp_alive(self) -> bool:
        try:
            conn = http.client.HTTPConnection(CDP_HOST, CDP_PORT, timeout=5)
            conn.request('GET', '/json/version')
            resp = conn.getresponse()
            resp.read()
            conn.close()
            return True
        except Exception:
            return False

    def _eval_on_tab(self, ws_url: str, expression: str, timeout: float = 10):
        """在指定 tab 执行 JS，返回解析后的 dict。"""
        if websocket is None:
            raise RuntimeError('websocket-client 未安装')
        ws = websocket.create_connection(_normalize_ws(ws_url), timeout=timeout)
        try:
            ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {
                'expression': expression, 'returnByValue': True}}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = json.loads(ws.recv())
                if msg.get('id') == 1:
                    val = msg.get('result', {}).get('result', {}).get('value')
                    if isinstance(val, str):
                        try:
                            return json.loads(val)
                        except Exception:
                            return {}
                    return val or {}
            return {}
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _find_list_tab(self, tabs, list_url: str):
        """找列表页 tab：先精确匹配 query 参数，再退化为任意 zhipin jobs 页。"""
        import urllib.parse as up
        want_q = ''
        try:
            want_q = up.parse_qs(up.urlparse(list_url).query).get('query', [''])[0]
        except Exception:
            pass
        fallback = None
        for t in tabs:
            u = t.get('url', '')
            if 'zhipin.com' not in u:
                continue
            if '/web/geek/jobs' in u:
                if want_q and want_q in up.unquote(u):
                    return t
                fallback = fallback or t
        return fallback

    def check(self, list_url: str, strict_detail: bool = False) -> dict:
        """返回 {ok, code, msg, cards}。code ∈ browser_down / no_list_tab /
        not_logged_in / empty_list / detail_check_failed / ws_error"""
        if not self._cdp_alive():
            return {'ok': False, 'code': 'browser_down',
                    'msg': 'CloakBrowser(CDP 9222) 未启动，无法监控'}
        if websocket is None:
            return {'ok': False, 'code': 'ws_error',
                    'msg': 'websocket-client 未安装，无法校验登录态'}

        tabs, err = self._tabs()
        if err:
            return {'ok': False, 'code': 'browser_down', 'msg': err}
        tab = self._find_list_tab(tabs, list_url)
        if not tab:
            return {'ok': False, 'code': 'no_list_tab',
                    'msg': '浏览器里没有 Boss 直聘列表页，请先打开目标列表页'}
        if not tab.get('webSocketDebuggerUrl'):
            return {'ok': False, 'code': 'ws_error', 'msg': '列表页 tab 不可调试'}

        try:
            info = self._eval_on_tab(tab['webSocketDebuggerUrl'], LIST_CHECK_JS)
        except Exception as e:
            return {'ok': False, 'code': 'ws_error', 'msg': f'读取列表页失败: {e}'}

        title = (info.get('title') or '')
        url_now = (info.get('url') or '')
        low_title = title.lower()
        if any(m in title or m in low_title for m in LOGIN_MARKERS[:2]):
            return {'ok': False, 'code': 'not_logged_in',
                    'msg': f'列表页标题异常（{title}），疑似未登录'}
        if info.get('needLogin'):
            return {'ok': False, 'code': 'not_logged_in', 'msg': '页面要求登录'}
        if 'zhipin.com' not in url_now:
            return {'ok': False, 'code': 'not_logged_in',
                    'msg': f'当前页已离开 Boss（{url_now}）'}
        cards = int(info.get('cards') or 0)
        if cards == 0:
            return {'ok': False, 'code': 'empty_list',
                    'msg': '列表页 0 张岗位卡片（可能失效/风控/需登录），本轮跳过'}

        if strict_detail:
            ok, msg = self._detail_spot_check(tabs)
            if not ok:
                return {'ok': False, 'code': 'detail_check_failed', 'msg': msg}

        return {'ok': True, 'code': 'ok', 'msg': f'列表页就绪（{cards} 张卡片可见）',
                'cards': cards}

    def _detail_spot_check(self, tabs):
        """抽样一个岗位详情页，确认没被踢回登录/验证页。"""
        try:
            import urllib.request as rq
            with rq.urlopen(f'http://{CDP_HOST}:{CDP_PORT}/json', timeout=5) as r:
                tabs = json.loads(r.read().decode())
        except Exception as e:
            return False, f'抽样失败: {e}'
        ws_urls = [t.get('webSocketDebuggerUrl') for t in tabs
                   if t.get('type') == 'page' and t.get('webSocketDebuggerUrl')]
        # 用列表页所在 tab 打开第一个可见详情不可靠，这里只校验已有 tab 是否被重定向到登录页
        checked = 0
        for ws in ws_urls[:3]:
            try:
                info = self._eval_on_tab(ws, DETAIL_CHECK_JS, timeout=6)
            except Exception:
                continue
            checked += 1
            u = info.get('url') or ''
            if any(m in u for m in ('/user/passport', 'verify', 'security')):
                return False, f'页面被重定向到登录/风控页（{u[:80]}）'
            if info.get('needLogin'):
                return False, '详情页要求登录，判定登录态失效'
        if checked == 0:
            return False, '无法读取任何页面做登录校验'
        return True, '详情页抽样通过'


class MonitorScheduler:
    """长期监控调度线程。"""

    def __init__(self, store: MonitorStore, crawl_runner, guard: EnvGuard = None,
                 tick_seconds: int = 30, log=None):
        """
        crawl_runner: 提供 start_crawl(params)->dict / status()->dict / stop()->dict
                      的既有管理器（serve_dashboard.CrawlManager）
        """
        self.store = store
        self.runner = crawl_runner
        self.guard = guard or EnvGuard()
        self.tick_seconds = tick_seconds
        self.log = log or (lambda m: self._log(None, m))
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.RLock()   # M-2：API mutators 与 _tick 共用，允许嵌套（relogin→set_status）
        # 当前正在跑的轮次 {monitor_id, round_name, started_at, deadline, raw_dir}
        self.active = None

    # --- 日志：写各自任务目录 + 控制台
    def _log(self, mid, msg):
        line = f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
        try:
            print(line, flush=True)
        except Exception:
            pass
        if mid:
            try:
                with open(self.store.log_path(mid), 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except Exception:
                pass

    # --- 生命周期
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._recover_orphans()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='monitor-scheduler',
                                        daemon=True)
        self._thread.start()
        self.log('长期监控调度器已启动')

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick(datetime.now())
            except Exception:
                self.log(f'调度异常: {traceback.format_exc(limit=3)}')
            self._stop.wait(self.tick_seconds)

    # --- 核心一拍（可被单测直接调用）
    def _tick(self, now: datetime):
        with self._lock:
            # 1. 有轮次在跑 → 只负责等它结束
            if self.active:
                done = self._poll_active(now)
                if not done:
                    return
                self.active = None

            # 2. 全局串行：既有手动爬取在跑，本轮让路
            st = {}
            try:
                st = self.runner.status() or {}
            except Exception:
                st = {}
            if st.get('running'):
                return

            # 3. 找到期任务
            for task in self.store.running_tasks():
                mid = task['monitor_id']

                # 时长到期 → 结束
                end_at = parse_iso(task.get('end_at'))
                if end_at and now >= end_at:
                    task['status'] = 'expired'
                    task['next_run_at'] = None
                    self.store.append_alert(task, 'info', 'expired',
                                            '持续时长已到，监控任务自动结束')
                    self.store.save(task)
                    self._log(mid, '到达持续时长，自动结束')
                    continue

                nxt = parse_iso(task.get('next_run_at'))
                if not nxt or now < nxt:
                    continue
                if in_quiet(now, task.get('quiet_hours') or []):
                    continue

                self._begin_round(task, now)
                return  # 一次只起一轮

    # --- 轮次执行
    def _begin_round(self, task: dict, now: datetime):
        mid = task['monitor_id']
        # 环境守卫：不通过一律不爬
        g = self.guard.check(task['list_url'], strict_detail=True)
        if not g['ok']:
            self._on_guard_fail(task, now, g)
            return
        task['guard_fail_streak'] = 0
        task['login']['last_login_verified_at'] = now.isoformat(timespec='seconds')
        task['login']['stale'] = False

        round_no = int(task.get('rounds') or 0) + 1
        rname = self.store.new_round_name(now)
        raw_dir = self.store.round_raw_dir(mid, rname)

        # 分层抓取（P1）：把「本轮不重抓详情」的已知岗位预置成 CSV。
        # 爬虫 load_existing_job_ids() 会把这些 job_id 视为已抓而跳过详情页
        # （每条约 20s），单轮从 1~2 小时压到 10 分钟内；该 CSV 同时为详情查看兜底。
        skipped_details = 0
        try:
            seen = read_json(self.store.seen_path(mid), {}) or {}
            ids = sorted(k for k, r in seen.items() if not r.get('superseded_by'))
            sample = set()
            if len(ids) > DETAIL_SAMPLE_PER_ROUND:
                start = (round_no * DETAIL_SAMPLE_PER_ROUND) % len(ids)
                sample = {ids[(start + i) % len(ids)] for i in range(DETAIL_SAMPLE_PER_ROUND)}
            rows = preseed_rows(seen, exclude_ids=sample)
            if rows:
                os.makedirs(raw_dir, exist_ok=True)
                with open(os.path.join(raw_dir, PRESEED_CSV), 'w',
                          newline='', encoding='utf-8-sig') as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)
                atomic_write_json(os.path.join(raw_dir, PRESEED_JSON), rows)
                skipped_details = len(rows)
        except Exception as e:
            self._log(mid, f'预置跳过明细失败（回退为全量重抓）：{e}')

        params = {'url': task['list_url'], 'output_dir': raw_dir,
                  'session_name': f'{mid}/rounds/{rname}', 'navigate': True}
        for k in SCROLL_KEYS:
            if task.get('scroll', {}).get(k) is not None:
                params[k] = task['scroll'][k]

        res = self.runner.start_crawl(params)
        if res.get('error'):
            # 起不来（通常是被手动任务抢占）：5 分钟后再试，不算失败轮
            task['next_run_at'] = (now + timedelta(minutes=5)).isoformat(timespec='seconds')
            self.store.save(task)
            self._log(mid, f'本轮让路（{res["error"]}），稍后重试')
            return

        timeout_min = float(task.get('round_timeout_minutes') or DEFAULT_ROUND_TIMEOUT_MINUTES)
        self.active = {'monitor_id': mid, 'round_no': round_no, 'round_name': rname,
                       'raw_dir': raw_dir, 'pid': res.get('pid'),
                       'started_at': now, 'deadline': now + timedelta(minutes=timeout_min)}
        task['active_round'] = {'round': round_no, 'name': rname, 'pid': res.get('pid'),
                                'started_at': now.isoformat(timespec='seconds')}
        self.store.save(task)
        self._log(mid, f'第 {round_no} 轮开始（pid={res.get("pid")}，跳过详情 {skipped_details} 条）→ {raw_dir}')

    def _build_view_cache(self, raw_dir: str) -> None:
        """只做「视图合并」：预置(旧详情) + 实抓(新详情) 写回 jobs_cache.json。

        供中列列表/详情/判定/打招呼使用；绝不参与在场判定与差异计算
        （存在性唯一依据是 list_jobs.json，见 read_round_cards / apply_round）。
        """
        try:
            preseed = read_json(os.path.join(raw_dir, PRESEED_JSON), []) or []
            fetched = read_json(os.path.join(raw_dir, 'jobs_cache.json'), []) or []
            by_id = {}
            for row in (preseed if isinstance(preseed, list) else []):
                jid = (row.get('job_id') or '').strip()
                if jid:
                    by_id[jid] = row
            for row in (fetched if isinstance(fetched, list) else []):
                jid = (row.get('job_id') or '').strip()
                if jid:
                    by_id[jid] = row
            atomic_write_json(os.path.join(raw_dir, 'jobs_cache.json'), list(by_id.values()))
        except Exception:
            pass

    @staticmethod
    def _pid_alive(pid):
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError):
            return False

    def _recover_orphans(self):
        """服务重启后内存 active 丢失：收养仍在跑的爬虫孤儿轮，或清理陈旧标记（M-5）。"""
        try:
            for t in self.store.list():
                ar = t.get('active_round') or {}
                if not ar.get('pid'):
                    continue
                if self._pid_alive(ar.get('pid')):
                    started = parse_iso(ar.get('started_at')) or datetime.now()
                    timeout_min = float(t.get('round_timeout_minutes')
                                        or DEFAULT_ROUND_TIMEOUT_MINUTES)
                    self.active = {'monitor_id': t['monitor_id'],
                                   'round_no': ar.get('round'),
                                   'round_name': ar.get('name'),
                                   'raw_dir': self.store.round_raw_dir(t['monitor_id'], ar.get('name')),
                                   'pid': ar.get('pid'),
                                   'started_at': started,
                                   'deadline': started + timedelta(minutes=timeout_min)}
                    self._log(t['monitor_id'],
                              f'服务重启：接回进行中的第 {ar.get("round")} 轮（pid={ar.get("pid")}）')
                    return   # 全局串行，只接第一个
                t['active_round'] = None
                self.store.save(t)
        except Exception as e:
            self.log(f'孤儿轮次恢复失败: {e}')

    def _poll_active(self, now: datetime) -> bool:
        """返回 True 表示轮次已结束（成功/失败/超时），可以调度下一轮。"""
        a = self.active
        try:
            st = self.runner.status() or {}
        except Exception:
            st = {}
        running = (bool(st.get('running')) and st.get('pid') == a['pid']) \
            or self._pid_alive(a.get('pid'))   # M-5：收养的孤儿轮也能正常探活

        if now >= a['deadline']:
            try:
                st2 = self.runner.status() or {}
                if st2.get('running') and st2.get('pid') == a['pid']:
                    self.runner.stop()
                elif self._pid_alive(a.get('pid')):   # 收养的孤儿：杀整个进程组
                    os.killpg(int(a['pid']), signal.SIGTERM)
            except Exception:
                pass
            self._finish_round(a, now, ok=False, error='本轮超时无进展，已终止')
            return True
        if running:
            return False
        # 子进程退出即视为本轮结束，由 progress.json 终态判定成败。
        # 回归（长期-2026-10-25-总助 第 1 轮）：旧逻辑只拦 error，进程被杀后
        # progress 停在 running 也被假记成 ok=True/0 条，熔断被架空。
        prog = read_json(os.path.join(a['raw_dir'], 'progress.json'), {}) or {}
        pstat = prog.get('status')
        scraped = int(prog.get('scraped') or 0)
        if pstat == 'done':
            # S-2：闸门以「列表页真实规模」为准——progress.list_total（页面卡片数）
            # 与上轮基线对比；实抓数 got 另须接近列表总数（显示 N 条却只抓到少数 = 异常）。
            cards = read_round_cards(a['raw_dir'])
            got = len(cards)
            task = self.store.load(a['monitor_id']) or {}
            s = task.get('stats') or {}
            lt = int(prog.get('list_total') or 0)
            base = int(s.get('last_list_total') or s.get('last_scraped') or 0)
            ref = lt if lt > 0 else got
            floor = (max(VOLUME_FLOOR_ABS, int(round(base * VOLUME_FLOOR_RATIO)))
                     if base else 1)   # 首轮抓到>0 即可（M-8：小结果搜索不被封死）
            capture_ok = got >= 1 and (lt == 0 or got >= lt)
            if (ref < floor) or (not capture_ok):
                # 列表页没加载完整/被风控时，绝不把差异写进主表
                self._finish_round(
                    a, now, ok=False, reason='volume',
                    error=(f'本轮抓取量异常：实抓 {got} 条 / 列表总数 {lt} 条'
                           f'（下限 {floor}，上轮 {base or "无基线"} 条），'
                           f'疑似列表页未加载完整或被风控拦截；本轮不计入差异'))
                return True
            self._finish_round(a, now, ok=True)
            return True
        if pstat == 'error':
            err = prog.get('error') or '爬取进程报错退出'
        else:
            err = f'爬取进程中途死亡（progress 停在“{pstat or "未建立"}”，已抓 {scraped} 条）'
            tail = self._tail_of(os.path.join(os.path.dirname(a['raw_dir']), 'pipeline.log'))
            if tail:
                err += f'；日志尾：{tail}'
        self._finish_round(a, now, ok=False, error=err)
        return True

    def _tail_of(self, path: str, limit: int = 160) -> str:
        """取日志尾部非空文本，让告警直接能看到死因。"""
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                data = f.read()
        except Exception:
            return ''
        lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
        return ' / '.join(lines[-2:])[-limit:] if lines else ''

    def _finish_round(self, a: dict, now: datetime, ok: bool, error: str = None,
                     reason: str = None):
        mid, rname, round_no = a['monitor_id'], a['round_name'], a['round_no']
        task = self.store.load(mid)
        if not task:
            self.active = None
            return
        raw_dir = a['raw_dir']
        diff = None
        summary_path = os.path.join(self.store.rounds_dir(mid), rname, 'summary.json')

        if ok:
            cards = read_round_cards(raw_dir)              # 在场（list_jobs.json）
            details = read_round_jobs(raw_dir)             # 实抓详情（写视图前读）
            diff = apply_round(self.store.seen_path(mid), round_no, cards, details)
            self._build_view_cache(raw_dir)                # 视图合并，供中列/详情/判定
            s = task.setdefault('stats', {})
            s['total_jobs'] = diff['total_seen']
            s['last_scraped'] = diff['scraped']
            lt = int((read_json(os.path.join(raw_dir, 'progress.json'), {}) or {}).get('list_total') or 0)
            s['last_list_total'] = lt if lt > 0 else diff['scraped']
            s['new_total'] = s.get('new_total', 0) + len(diff['new'])
            s['missing_total'] = s.get('missing_total', 0) + len(diff['missing'])
            s['changed_total'] = s.get('changed_total', 0) + len(diff['changed'])
            s['reposted_total'] = s.get('reposted_total', 0) + len(diff.get('reposted') or [])
            atomic_write_json(summary_path, {'round': round_no, 'at': now.isoformat(timespec='seconds'),
                                             'ok': True, 'diff': diff})
            task['last_report'] = {
                'round': round_no, 'at': now.isoformat(timespec='seconds'),
                'new': len(diff['new']), 'missing': len(diff['missing']),
                'scraped': diff['scraped'], 'total': diff['total_seen'],
                'changed': len(diff['changed']), 'reposted': len(diff.get('reposted') or []),
                'error': None,
            }
            task['volume_fail_streak'] = 0
            self._log(mid, f'第 {round_no} 轮完成：抓到 {diff["scraped"]} / 新增 {len(diff["new"])}'
                           f' / 未出现 {len(diff["missing"])} / 重发 {len(diff.get("reposted") or [])}')
        else:
            stats = task.setdefault('stats', {})
            stats['failed_rounds'] = stats.get('failed_rounds', 0) + 1
            if reason == 'volume':
                task['volume_fail_streak'] = task.get('volume_fail_streak', 0) + 1
                self.store.append_alert(
                    task, 'error' if task['volume_fail_streak'] >= 2 else 'warn',
                    'volume_collapse', error or '抓取量异常')
            else:
                task['volume_fail_streak'] = 0
                self.store.append_alert(task, 'warn', 'round_failed',
                                        error or '本轮失败')
            atomic_write_json(summary_path, {'round': round_no,
                                             'at': now.isoformat(timespec='seconds'),
                                             'ok': False, 'error': error, 'reason': reason})
            task['last_report'] = {'round': round_no, 'at': now.isoformat(timespec='seconds'),
                                   'new': 0, 'missing': 0, 'scraped': 0,
                                   'total': task.get('stats', {}).get('total_jobs', 0),
                                   'changed': 0, 'reposted': 0, 'error': error}
            self._log(mid, f'第 {round_no} 轮失败：{error}')

        task['rounds'] = round_no
        task['last_round'] = {'round': round_no, 'name': rname, 'ok': ok,
                              'at': now.isoformat(timespec='seconds'), 'error': error}
        task['active_round'] = None
        task['consecutive_failures'] = 0 if ok else task.get('consecutive_failures', 0) + 1

        if (not ok) and reason == 'volume' and int(task.get('volume_fail_streak') or 0) >= 2:
            task['status'] = 'paused'
            task['next_run_at'] = None
            self.store.append_alert(task, 'error', 'auto_paused',
                                    '连续 2 轮抓取量异常，已自动暂停：请确认列表页能正常加载后再恢复')
            self._log(mid, '连续 2 轮抓取量异常，自动暂停')
        elif task['consecutive_failures'] >= int(task.get('max_failures') or DEFAULT_MAX_FAILURES):
            task['status'] = 'paused'
            task['next_run_at'] = None
            self.store.append_alert(task, 'error', 'auto_paused',
                                    f'连续 {task["consecutive_failures"]} 轮失败，已自动暂停')
            self._log(mid, '连续失败达上限，自动暂停')
        elif (not ok) and reason == 'volume':
            quiet = task.get('quiet_hours') or []
            retry = int(task.get('guard_retry_minutes') or DEFAULT_RETRY_MINUTES)
            task['next_run_at'] = next_allowed(
                now + timedelta(minutes=retry), quiet, 0).isoformat(timespec='seconds')
        else:
            quiet = task.get('quiet_hours') or []
            task['next_run_at'] = compute_next_run(
                now, task.get('interval_hours') or 3, quiet).isoformat(timespec='seconds')
        if int(task.get('rounds') or 0) % 20 == 0:
            self.store.prune_rounds(mid)
        self.store.save(task)
        self.active = None

    def _on_guard_fail(self, task: dict, now: datetime, g: dict):
        mid = task['monitor_id']
        task['guard_fail_streak'] = task.get('guard_fail_streak', 0) + 1
        streak = task['guard_fail_streak']
        retry = int(task.get('guard_retry_minutes') or DEFAULT_RETRY_MINUTES)
        quiet = task.get('quiet_hours') or []
        if streak >= 2:
            task['status'] = 'paused'
            task['next_run_at'] = None
            self.store.append_alert(
                task, 'error', 'login_invalid',
                f'环境检查连续失败（{g["code"]}）：{g["msg"]}。已暂停，请在浏览器里重新登录 Boss 直聘后恢复任务')
            self._log(mid, f'守卫连续失败 {streak} 次 → 暂停（{g["code"]}）')
        else:
            nxt = next_allowed(now + timedelta(minutes=retry), quiet, 0)
            task['next_run_at'] = nxt.isoformat(timespec='seconds')
            self.store.append_alert(
                task, 'warn', g['code'], f'环境检查未通过：{g["msg"]}。{retry} 分钟后重试')
            self._log(mid, f'守卫未过（{g["code"]}）→ {retry} 分钟后重试')
        self.store.save(task)

    # --- 外部动作（API 调用）
    def run_now(self, mid: str) -> dict:
        task = self.store.load(mid)
        if not task:
            return {'error': '任务不存在'}
        # M-6：已终止/到期的任务不允许被「立即一轮」静默复活
        if task.get('status') in ('stopped', 'expired'):
            return {'error': '任务已终止/到期，请新建任务'}
        with self._lock:
            if self.active:
                return {'error': f'第 {self.active["round_no"]} 轮正在跑，请稍候'}
            st = self.runner.status() or {}
            if st.get('running'):
                return {'error': '已有爬取任务在运行'}
            task['status'] = 'running'
            task['next_run_at'] = now_iso()
            task['guard_fail_streak'] = 0
            task['volume_fail_streak'] = 0
            self.store.save(task)
        # M-3：不在此线程同步跑守卫/起轮，交给调度循环（≤tick_seconds 内拾取）
        return {'ok': True, 'msg': '已触发一轮'}

    def set_status(self, mid: str, status: str) -> dict:
        task = self.store.load(mid)
        if not task:
            return {'error': '任务不存在'}
        if status not in ('running', 'paused', 'stopped'):
            return {'error': '非法状态'}
        with self._lock:
            task['status'] = status
            if status == 'running':
                task['next_run_at'] = now_iso()
                task['guard_fail_streak'] = 0
                task['volume_fail_streak'] = 0
                task['consecutive_failures'] = 0
            else:
                task['next_run_at'] = None
            self.store.save(task)
        self._log(mid, f'状态 → {status}')
        return {'ok': True, 'status': status}

    def update_params(self, mid: str, fields: dict) -> dict:
        task = self.store.load(mid)
        if not task:
            return {'error': '任务不存在'}
        with self._lock:

            def _num(key):
                """只接受能转成数字的值。注意 0 是合法值（= 不限时长），不能当成没传。"""
                if key not in fields or fields.get(key) is None or fields.get(key) == '':
                    return None
                try:
                    return max(0.0, float(fields[key]))
                except (TypeError, ValueError):
                    return None

            iv = _num('interval_hours')
            du = _num('duration_hours')
            if iv is not None:
                task['interval_hours'] = max(1.0, iv)
            if du is not None:
                task['duration_hours'] = du
                if du > 0:
                    # 从“现在”起算：旧版从 started_at 起算，改时长时可能一保存就算到期
                    task['end_at'] = (datetime.now() + timedelta(hours=du)).isoformat(timespec='seconds')
                else:
                    task['end_at'] = None
            qh = fields.get('quiet_hours')
            if isinstance(qh, list) and len(qh) == 2:
                try:
                    task['quiet_hours'] = [f'{int(str(qh[0]).split(":")[0]):02d}:'
                                           f'{int(str(qh[0]).split(":")[1] or 0):02d}',
                                           f'{int(str(qh[1]).split(":")[0]):02d}:'
                                           f'{int(str(qh[1]).split(":")[1] or 0):02d}']
                except (ValueError, IndexError):
                    return {'error': f'夜间窗口格式非法：{qh}（应用 HH:MM）'}
            if fields.get('scroll'):
                for k in SCROLL_KEYS:
                    if fields['scroll'].get(k) not in (None, ''):
                        task.setdefault('scroll', {})[k] = fields['scroll'][k]
            rl = _num('relogin_after_hours')
            if rl is not None:
                task.setdefault('login', {})['relogin_after_hours'] = max(1.0, rl)
            if task.get('status') == 'running':
                task['next_run_at'] = compute_next_run(
                    datetime.now(), task.get('interval_hours') or 3,
                    task.get('quiet_hours') or []).isoformat(timespec='seconds')
            self.store.save(task)
            return {'ok': True}

    def mark_login_refreshed(self, mid: str) -> dict:
        """用户点「我已重新登录」→ 立刻做一次守卫校验，通过才刷新时效。"""
        task = self.store.load(mid)
        if not task:
            return {'error': '任务不存在'}
        g = self.guard.check(task['list_url'], strict_detail=True)
        if not g['ok']:
            return {'ok': False, 'msg': g['msg'], 'code': g['code']}
        with self._lock:
            task['login']['last_login_verified_at'] = now_iso()
            task['login']['stale'] = False
            task['guard_fail_streak'] = 0
            self.store.save(task)
        if task.get('status') == 'paused':
            self.set_status(mid, 'running')
        return {'ok': True, 'msg': g['msg']}


if __name__ == '__main__':
    import tempfile
    if '--self-test' in sys.argv:
        # 假 runner：记录调用，立即"完成"并写 progress.json + 一个 jobs_cache.json
        class FakeRunner:
            def __init__(self):
                self.calls = []
                self.dirs = []

            def start_crawl(self, params):
                self.calls.append(params)
                raw = params['output_dir']
                os.makedirs(raw, exist_ok=True)
                self.dirs.append(raw)
                n = len(self.calls)
                jobs = [{'job_id': f'j{i}', '职位名称': f'岗位{i}', '公司名称': 'X',
                         '薪资': '15K', '区域': '浦东新区', 'job_url': 'https://zhipin.com/x'}
                        for i in range(n + 8)]   # 每轮多 1 个 → 稳定产生 new；≥ 绝对下限 5
                atomic_write_json(os.path.join(raw, 'jobs_cache.json'), jobs)
                # 模拟爬虫新行为：可见卡片全量落盘（存在性唯一依据）
                cards = [{'job_id': j['job_id'], 'title': j['职位名称'], 'company': j['公司名称'],
                          'salary': j['薪资'], 'area': j['区域'], 'url': j['job_url']} for j in jobs]
                atomic_write_json(os.path.join(raw, 'list_jobs.json'), cards)
                atomic_write_json(os.path.join(raw, 'progress.json'),
                                  {'status': 'done', 'scraped': len(jobs), 'list_total': len(jobs)})
                return {'ok': True, 'pid': 2000000 + n}

            def status(self):
                return {'running': False, 'kind': 'crawl', 'pid': None}

            def stop(self):
                return {'ok': True}

        class FakeGuard:
            def __init__(self):
                self.ok = True
                self.code = 'ok'

            def check(self, list_url, strict_detail=False):
                if self.ok:
                    return {'ok': True, 'code': 'ok', 'msg': 'fake ok', 'cards': 12}
                return {'ok': False, 'code': self.code, 'msg': 'fake fail'}

        with tempfile.TemporaryDirectory() as td:
            store = MonitorStore(td)
            runner, guard = FakeRunner(), FakeGuard()
            sch = MonitorScheduler(store, runner, guard=guard, tick_seconds=1, log=lambda m: None)
            mid, t = store.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=总助',
                                   'interval_hours': 3, 'duration_hours': 48})
            t['next_run_at'] = '2026-10-25T10:00:00'
            t['end_at'] = '2026-10-27T10:00:00'
            store.save(t)
            # 第 1 轮
            sch._tick(datetime(2026, 10, 25, 10, 0))
            assert sch.active and sch.active['round_no'] == 1, sch.active
            assert runner.calls and runner.calls[0]['max_scroll'] == 30   # 长滚动：30 次（ii 定 2026-10-27）
            sch._tick(datetime(2026, 10, 25, 10, 1))
            assert sch.active is None
            t = store.load(mid)
            assert t['rounds'] == 1 and t['stats']['new_total'] == 9, t['stats']
            assert t['login']['last_login_verified_at']
            # 下一轮应落在 13:01~13:16（间隔 3h + 防规律抖动）
            nxt = parse_iso(store.load(mid)['next_run_at'])
            assert datetime(2026, 10, 25, 13, 1) <= nxt <= datetime(2026, 10, 25, 13, 16), nxt
            # 第 2 轮：到点起 / 下一拍收尾 → +1 新增，无缺失
            sch._tick(nxt)
            assert sch.active and sch.active['round_no'] == 2, sch.active
            sch._tick(nxt + timedelta(minutes=1))
            t = store.load(mid)
            assert t['rounds'] == 2 and t['stats']['new_total'] == 10, t['stats']
            assert t['last_report']['new'] == 1 and t['last_report']['missing'] == 0
            # 夜间窗口：改间隔 6h，19:00 跑第 3 轮 → 19+6 落在深夜，应顺延到 08:00 之后
            t = store.load(mid)
            t['interval_hours'] = 6
            t['next_run_at'] = '2026-10-25T19:00:00'
            store.save(t)
            sch._tick(datetime(2026, 10, 25, 19, 0))
            sch._tick(datetime(2026, 10, 25, 19, 1))
            t = store.load(mid)
            assert t['rounds'] == 3, t['rounds']
            assert t['next_run_at'] >= '2026-10-26T08:00', t['next_run_at']
            assert 'T00:' not in t['next_run_at'] and in_quiet(parse_iso(t['next_run_at']),
                                                                t['quiet_hours']) is False
            print('scheduler rounds OK')

        # 下面三个场景各自独立 store：_tick 一拍只处理最先到期的一個任务，
        # 共用 store 会把失败记到另一个任务头上（测不准）
        g_ok = FakeGuard()

        # 守卫失败：第一次重试、第二次暂停熔断
        with tempfile.TemporaryDirectory() as td2:
            st2 = MonitorStore(td2)
            guard = FakeGuard()
            mid2, _ = st2.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=HR'})
            tt = st2.load(mid2)
            tt['next_run_at'] = '2026-10-26T10:00:00'
            st2.save(tt)
            sch_b = MonitorScheduler(st2, FakeRunner(), guard=guard, tick_seconds=1,
                                     log=lambda m: None)
            guard.ok = False
            guard.code = 'not_logged_in'
            sch_b._tick(datetime(2026, 10, 26, 10, 0))
            t2 = st2.load(mid2)
            assert t2['status'] == 'running' and t2['guard_fail_streak'] == 1, \
                (t2['status'], t2['guard_fail_streak'])
            assert t2['next_run_at'] > '2026-10-26T10:00'
            assert not sch_b.active, '守卫失败不应起爬取轮次'
            sch_b._tick(datetime(2026, 10, 26, 10, 21))
            t2 = st2.load(mid2)
            assert t2['status'] == 'paused' and t2['next_run_at'] is None
            codes = [a['code'] for a in t2['alerts']]
            assert 'login_invalid' in codes, codes
            print('guard-fail fuse OK')

        # 时长到期自动结束
        with tempfile.TemporaryDirectory() as td3:
            st3 = MonitorStore(td3)
            mid3, t3 = st3.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=销售',
                                   'duration_hours': 1})
            t3['started_at'] = '2026-10-26T12:00:00'
            t3['end_at'] = '2026-10-26T13:00:00'
            t3['next_run_at'] = '2026-10-26T12:00:00'
            st3.save(t3)
            sch_c = MonitorScheduler(st3, FakeRunner(), guard=FakeGuard(), tick_seconds=1,
                                     log=lambda m: None)
            sch_c._tick(datetime(2026, 10, 26, 13, 5))
            t3 = st3.load(mid3)
            assert t3['status'] == 'expired' and t3['next_run_at'] is None, t3['status']
            print('expiry OK')

        # 手动任务抢占时让路
        with tempfile.TemporaryDirectory() as td4:
            st4 = MonitorStore(td4)
            mid4, _ = st4.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=运营'})
            tt = st4.load(mid4)
            tt['next_run_at'] = '2026-10-26T14:00:00'
            st4.save(tt)
            runner2 = FakeRunner()

            class BusyRunner(FakeRunner):
                def status(self):
                    return {'running': True, 'kind': 'crawl', 'pid': 99}
            sch_d = MonitorScheduler(st4, BusyRunner(), guard=FakeGuard(),
                                     tick_seconds=1, log=lambda m: None)
            before = len(runner2.calls)
            sch_d._tick(datetime(2026, 10, 26, 14, 0))
            assert sch_d.active is None
            assert len(runner2.calls) == before
            # 让路不能记为失败轮：rounds 仍为 0
            assert st4.load(mid4)['rounds'] == 0
            print('preemption OK')

        # 登录态刷新：守卫通过才重置时效，不通过不得刷新
        with tempfile.TemporaryDirectory() as td5:
            st5 = MonitorStore(td5)
            guard5 = FakeGuard()
            fr = FakeRunner()
            sch_e = MonitorScheduler(st5, fr, guard=guard5, tick_seconds=1, log=lambda m: None)
            mid5, t5 = st5.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=产品'})
            t5['status'] = 'paused'
            t5['guard_fail_streak'] = 3
            st5.save(t5)
            guard5.ok = False
            r = sch_e.mark_login_refreshed(mid5)
            assert r.get('ok') is False and st5.load(mid5)['status'] == 'paused', r
            guard5.ok = True
            r = sch_e.mark_login_refreshed(mid5)
            assert r.get('ok') is True, r
            t5 = st5.load(mid5)
            assert t5['status'] == 'running' and t5['guard_fail_streak'] == 0
            assert t5['login']['last_login_verified_at']
            print('relogin OK')
        # 失败轮判定：进程中途死/完成但 0 条，都必须计入失败轮并触发熔断
        # （回归 2026-10-25：旧版把这两种情况假记成功，熔断形同虚设）
        class DeadRunner(FakeRunner):
            def __init__(self, mode='hang'):
                super().__init__()
                self.mode = mode

            def start_crawl(self, params):
                self.calls.append(params)
                raw = params['output_dir']
                os.makedirs(raw, exist_ok=True)
                if self.mode == 'hang':
                    atomic_write_json(os.path.join(raw, 'progress.json'),
                                      {'status': 'running', 'scraped': 0})
                else:
                    atomic_write_json(os.path.join(raw, 'progress.json'),
                                      {'status': 'done', 'scraped': 0})
                    atomic_write_json(os.path.join(raw, 'jobs_cache.json'), [])
                return {'ok': True, 'pid': 2000300}

            def status(self):
                return {'running': False, 'kind': 'crawl', 'pid': None}

        with tempfile.TemporaryDirectory() as td6:
            import glob as _glob
            st6 = MonitorStore(td6)
            sch_f = MonitorScheduler(st6, DeadRunner('hang'), guard=FakeGuard(),
                                     tick_seconds=1, log=lambda m: None)
            mid6, _ = st6.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=死'})
            for i in range(3):
                t6 = st6.load(mid6)
                t6['next_run_at'] = f'2026-10-26T{15 + i}:00:00'
                st6.save(t6)
                sch_f._tick(datetime(2026, 10, 26, 15 + i, 0))
                sch_f._tick(datetime(2026, 10, 26, 15 + i, 1))
                t6 = st6.load(mid6)
                assert t6['stats']['failed_rounds'] == i + 1, (i, t6['stats'])
                assert t6['consecutive_failures'] == i + 1, t6
                assert t6['status'] == ('paused' if i == 2 else 'running'), (i, t6['status'])
            assert t6['status'] == 'paused', t6['status']
            codes = [a['code'] for a in t6['alerts']]
            assert 'round_failed' in codes and 'auto_paused' in codes, codes
            sps = _glob.glob(os.path.join(st6.rounds_dir(mid6), '*', 'summary.json'))
            summaries = [json.load(open(s, encoding='utf-8')) for s in sps]
            assert len(summaries) == 3 and all(s['ok'] is False for s in summaries), summaries
            assert all('中途死亡' in (s.get('error') or '') for s in summaries), summaries
            # 失败轮不得污染跨轮去重集
            seen = json.load(open(st6.seen_path(mid6), encoding='utf-8'))
            assert len(seen) == 0, seen
            print('failed-round breaker OK')

        with tempfile.TemporaryDirectory() as td7:
            import glob as _glob7
            st7 = MonitorStore(td7)
            sch_g = MonitorScheduler(st7, DeadRunner('zero'), guard=FakeGuard(),
                                     tick_seconds=1, log=lambda m: None)
            mid7, _ = st7.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=空'})
            t7 = st7.load(mid7)
            t7['next_run_at'] = '2026-10-26T15:00:00'
            st7.save(t7)
            sch_g._tick(datetime(2026, 10, 26, 15, 0))
            sch_g._tick(datetime(2026, 10, 26, 15, 1))
            t7 = st7.load(mid7)
            assert t7['stats']['failed_rounds'] == 1, t7['stats']
            sp7 = _glob7.glob(os.path.join(st7.rounds_dir(mid7), '*', 'summary.json'))[0]
            assert '0 条' in (json.load(open(sp7, encoding='utf-8')).get('error') or ''), sp7
            print('zero-scraped fail OK')
        # ── 分层抓取回归：预置跳过明细 + 合并，防「大面积未出现」误判 ──
        with tempfile.TemporaryDirectory() as td8:
            st8 = MonitorStore(td8)

            class TierRunner(FakeRunner):
                def start_crawl(self, params):
                    self.calls.append(params)
                    raw = params['output_dir']
                    os.makedirs(raw, exist_ok=True)
                    preseed = read_json(os.path.join(raw, PRESEED_JSON), []) or []
                    skip = {r['job_id'] for r in preseed} if isinstance(preseed, list) else set()
                    jobs = [{'job_id': f'j{i}', '职位名称': f'岗位{i}', '公司名称': 'X',
                             '薪资': '15K', '区域': '浦东新区', 'job_url': 'https://zhipin.com/x'}
                            for i in range(20)]
                    fetched = [j for j in jobs if j['job_id'] not in skip]  # 模拟爬虫跳过详情
                    atomic_write_json(os.path.join(raw, 'jobs_cache.json'), fetched)
                    # 可见卡片永远全量（20），与抓了几个详情无关——S-1 核心回归
                    cards = [{'job_id': j['job_id'], 'title': j['职位名称'], 'company': j['公司名称'],
                              'salary': j['薪资'], 'area': j['区域'], 'url': j['job_url']} for j in jobs]
                    atomic_write_json(os.path.join(raw, 'list_jobs.json'), cards)
                    atomic_write_json(os.path.join(raw, 'progress.json'),
                                      {'status': 'done', 'scraped': len(fetched), 'list_total': len(jobs)})
                    self.last_fetched = len(fetched)
                    return {'ok': True, 'pid': 2000100 + len(self.calls)}

            sch_h = MonitorScheduler(st8, TierRunner(), guard=FakeGuard(), tick_seconds=1,
                                     log=lambda m: None)
            mid8, _ = st8.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=分'})
            for i in range(3):
                t8 = st8.load(mid8)
                t8['next_run_at'] = f'2026-10-26T{16 + i}:00:00'
                st8.save(t8)
                sch_h._tick(datetime(2026, 10, 26, 16 + i, 0))
                sch_h._tick(datetime(2026, 10, 26, 16 + i, 1))
            t8 = st8.load(mid8)
            assert t8['rounds'] == 3 and t8['stats']['failed_rounds'] == 0, t8['stats']
            assert t8['stats']['total_jobs'] == 20 and t8['stats']['last_scraped'] == 20, t8['stats']
            import glob as _g8
            for sp in _g8.glob(os.path.join(st8.rounds_dir(mid8), '*', 'summary.json')):
                sm = json.load(open(sp, encoding='utf-8'))
                assert sm['ok'] and sm['diff']['scraped'] == 20, sm   # 每轮都应是完整 20 条
            rd8 = st8.rounds_dir(mid8)
            r2 = sorted(n for n in os.listdir(rd8))[1]
            ps8 = read_json(os.path.join(rd8, r2, '01_raw', PRESEED_JSON), [])
            assert len(ps8) == 8, len(ps8)   # 20 条 - 抽样 12 条 = 预置 8 条
            print('tiered-preseed OK')

        # ── P0 抓取量闸门：列表页缩水 → 无效轮不写缺席；两次即暂停 ──
        with tempfile.TemporaryDirectory() as td9:
            st9 = MonitorStore(td9)

            class ShrinkRunner(FakeRunner):
                def start_crawl(self, params):
                    self.calls.append(params)
                    raw = params['output_dir']
                    os.makedirs(raw, exist_ok=True)
                    n = len(self.calls)
                    jobs = [{'job_id': f'j{i}', '职位名称': f'岗位{i}', '公司名称': 'X',
                             '薪资': '15K', '区域': '浦东新区', 'job_url': 'https://zhipin.com/x'}
                            for i in range(20 if n == 1 else 3)]
                    atomic_write_json(os.path.join(raw, 'jobs_cache.json'), jobs)
                    cards = [{'job_id': j['job_id'], 'title': j['职位名称'], 'company': j['公司名称'],
                              'salary': j['薪资'], 'area': j['区域'], 'url': j['job_url']} for j in jobs]
                    atomic_write_json(os.path.join(raw, 'list_jobs.json'), cards)
                    atomic_write_json(os.path.join(raw, 'progress.json'),
                                      {'status': 'done', 'scraped': len(jobs), 'list_total': len(jobs)})
                    return {'ok': True, 'pid': 2000200 + n}

            sch_i = MonitorScheduler(st9, ShrinkRunner(), guard=FakeGuard(), tick_seconds=1,
                                     log=lambda m: None)
            mid9, _ = st9.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=缩'})
            t9 = st9.load(mid9); t9['next_run_at'] = '2026-10-26T15:00:00'; st9.save(t9)
            sch_i._tick(datetime(2026, 10, 26, 15, 0)); sch_i._tick(datetime(2026, 10, 26, 15, 1))
            t9 = st9.load(mid9)
            assert t9['stats']['total_jobs'] == 20 and t9['stats']['last_scraped'] == 20
            t9 = st9.load(mid9); t9['next_run_at'] = '2026-10-26T16:00:00'; st9.save(t9)
            sch_i._tick(datetime(2026, 10, 26, 16, 0)); sch_i._tick(datetime(2026, 10, 26, 16, 1))
            t9 = st9.load(mid9)
            assert t9['stats']['failed_rounds'] == 1 and t9['volume_fail_streak'] == 1, t9
            assert t9['status'] == 'running' and '16:2' in (t9['next_run_at'] or ''), t9
            seen9 = read_json(st9.seen_path(mid9))
            assert all(r['absent_streak'] == 0 for r in seen9.values()), seen9   # 无效轮不写缺席
            t9 = st9.load(mid9); t9['next_run_at'] = '2026-10-26T16:30:00'; st9.save(t9)
            sch_i._tick(datetime(2026, 10, 26, 16, 30)); sch_i._tick(datetime(2026, 10, 26, 16, 31))
            t9 = st9.load(mid9)
            assert t9['status'] == 'paused' and t9['next_run_at'] is None, t9
            codes9 = [a['code'] for a in t9['alerts']]
            assert 'volume_collapse' in codes9 and 'auto_paused' in codes9, codes9
            print('volume-gate OK')

        # ── 重发识别：同「公司+标题+区域」、job_id 变 → 记重发，不双计 ──
        with tempfile.TemporaryDirectory() as td10:
            sp10 = os.path.join(td10, 'seen.json')
            atomic_write_json(sp10, {})
            d1 = apply_round(sp10, 1,
                             [{'job_id': 'a1', 'title': '总助', 'company': '甲公司',
                               'area': '静安', 'salary': '20K', 'url': 'u'}],
                             [{'job_id': 'a1', 'desc': 'x'}])
            d2 = apply_round(sp10, 2, [], [])
            assert len(d2['missing']) == 1 and d2['missing'][0]['absent_rounds'] == 1, d2
            d3 = apply_round(sp10, 3,
                             [{'job_id': 'b9', 'title': '总助', 'company': '甲公司',
                               'area': '静安', 'salary': '20K', 'url': 'u'}],
                             [{'job_id': 'b9', 'desc': 'x'}])
            assert len(d3['new']) == 0 and len(d3['reposted']) == 1, (d3['new'], d3['reposted'])
            assert d3['reposted'][0]['repost_of'] == 'a1', d3['reposted']
            assert len(d3['missing']) == 0, d3['missing']       # 旧 id 被替代，不判未出现
            seen10 = read_json(sp10)
            assert seen10['a1'].get('superseded_by') == 'b9', seen10['a1']
            assert seen10['b9'].get('repost_of') == 'a1' and seen10['b9']['first_round'] == 1
            assert len(seen10) == 2, seen10                     # 只增不删
            print('repost OK')
        # ── S-1/S-2 回归（N≥50）：列表崩塌到 10 → 闸门拦住、缺席零污染 ──
        with tempfile.TemporaryDirectory() as td11:
            st11 = MonitorStore(td11)

            class CollapseRunner(FakeRunner):
                def start_crawl(self, params):
                    self.calls.append(params)
                    raw = params['output_dir']
                    os.makedirs(raw, exist_ok=True)
                    n = len(self.calls)
                    cnt = 50 if n == 1 else 10
                    jobs = [{'job_id': f'j{i}', '职位名称': f'岗位{i}', '公司名称': 'X',
                             '薪资': '15K', '区域': '浦东新区', 'job_url': 'https://zhipin.com/x'}
                            for i in range(cnt)]
                    atomic_write_json(os.path.join(raw, 'jobs_cache.json'), jobs)
                    cards = [{'job_id': j['job_id'], 'title': j['职位名称'], 'company': j['公司名称'],
                              'salary': j['薪资'], 'area': j['区域'], 'url': j['job_url']} for j in jobs]
                    atomic_write_json(os.path.join(raw, 'list_jobs.json'), cards)
                    atomic_write_json(os.path.join(raw, 'progress.json'),
                                      {'status': 'done', 'scraped': len(jobs), 'list_total': len(jobs)})
                    return {'ok': True, 'pid': 2000400 + n}

            sch_j = MonitorScheduler(st11, CollapseRunner(), guard=FakeGuard(), tick_seconds=1,
                                     log=lambda m: None)
            mid11, _ = st11.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=崩'})
            t11 = st11.load(mid11); t11['next_run_at'] = '2026-10-26T15:00:00'; st11.save(t11)
            sch_j._tick(datetime(2026, 10, 26, 15, 0)); sch_j._tick(datetime(2026, 10, 26, 15, 1))
            t11 = st11.load(mid11)
            assert t11['stats']['total_jobs'] == 50 and t11['stats']['last_list_total'] == 50, t11['stats']
            t11 = st11.load(mid11); t11['next_run_at'] = '2026-10-26T16:00:00'; st11.save(t11)
            sch_j._tick(datetime(2026, 10, 26, 16, 0)); sch_j._tick(datetime(2026, 10, 26, 16, 1))
            t11 = st11.load(mid11)
            assert t11['stats']['failed_rounds'] == 1 and t11['volume_fail_streak'] == 1, t11['stats']
            seen11 = read_json(st11.seen_path(mid11))
            assert all(r['absent_streak'] == 0 for r in seen11.values()), '崩塌轮不得写缺席'
            assert len(seen11) == 50
            print('collapse-N50 OK')

        # ── S-3 回归：超 4000 字 JD 不产生周期性假「变更」──
        with tempfile.TemporaryDirectory() as td12:
            sp12 = os.path.join(td12, 'seen.json')
            atomic_write_json(sp12, {})
            long_desc = 'x' * 5000
            c1 = [{'job_id': 'j1', 'title': 'T', 'company': 'C', 'area': 'A',
                   'salary': '20K', 'url': 'u'}]
            d1 = [{'job_id': 'j1', 'desc': long_desc}]
            r1 = apply_round(sp12, 1, c1, d1)
            assert r1['new'][0]['job_id'] == 'j1' and r1['changed'] == []
            r2 = apply_round(sp12, 2, c1, d1)   # 抽样重抓同样内容
            assert r2['changed'] == [], r2['changed']
            r3 = apply_round(sp12, 3, c1, [])   # 未实抓（预置回显视角）
            assert r3['changed'] == [], r3['changed']
            print('jd-truncate-fp OK')

        # ── M-1 回归：卡片在场但详情抓取失败 → 不判「未出现」──
        with tempfile.TemporaryDirectory() as td13:
            st13 = MonitorStore(td13)

            class FailDetailRunner(FakeRunner):
                def start_crawl(self, params):
                    self.calls.append(params)
                    raw = params['output_dir']
                    os.makedirs(raw, exist_ok=True)
                    n = len(self.calls)
                    jobs = [{'job_id': f'j{i}', '职位名称': f'岗位{i}', '公司名称': 'X',
                             '薪资': '15K', '区域': '浦东新区', 'job_url': 'https://zhipin.com/x'}
                            for i in range(5)]
                    atomic_write_json(os.path.join(raw, 'jobs_cache.json'),
                                      jobs if n == 1 else jobs[:3])   # 第2轮 j3/j4 详情失败
                    cards = [{'job_id': j['job_id'], 'title': j['职位名称'], 'company': j['公司名称'],
                              'salary': j['薪资'], 'area': j['区域'], 'url': j['job_url']} for j in jobs]
                    atomic_write_json(os.path.join(raw, 'list_jobs.json'), cards)
                    atomic_write_json(os.path.join(raw, 'progress.json'),
                                      {'status': 'done', 'scraped': len(jobs), 'list_total': len(jobs)})
                    return {'ok': True, 'pid': 2000500 + n}

            sch_k = MonitorScheduler(st13, FailDetailRunner(), guard=FakeGuard(),
                                     tick_seconds=1, log=lambda m: None)
            mid13, _ = st13.create({'url': 'https://www.zhipin.com/web/geek/jobs?query=失'})
            t13 = st13.load(mid13); t13['next_run_at'] = '2026-10-26T15:00:00'; st13.save(t13)
            sch_k._tick(datetime(2026, 10, 26, 15, 0)); sch_k._tick(datetime(2026, 10, 26, 15, 1))
            t13 = st13.load(mid13); t13['next_run_at'] = '2026-10-26T16:00:00'; st13.save(t13)
            sch_k._tick(datetime(2026, 10, 26, 16, 0)); sch_k._tick(datetime(2026, 10, 26, 16, 1))
            t13 = st13.load(mid13)
            assert t13['stats']['missing_total'] == 0, t13['stats']   # 详情失败 ≠ 消失
            assert t13['stats']['total_jobs'] == 5
            print('detail-fail-present OK')
        print('monitor_scheduler self-test PASS')
        sys.exit(0)
    print('用法: python3 monitor_scheduler.py --self-test')
