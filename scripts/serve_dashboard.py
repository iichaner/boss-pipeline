#!/usr/bin/env python3
"""
T6 Dashboard 本地服务器。

功能：
1. 提供 Dashboard 静态页面（index.html）
2. 代理 progress.json / jobs_cache.json / priorities.json
3. 提供 /detail?job_id=xxx 接口（从 CSV 读取 JD 全文）
4. 提供 /priority POST 接口（人工判定优先级）
5. 提供 /navigate POST 接口（CDP 跳转岗位页面）
6. 提供 /open-folder GET 接口（Finder 打开文件夹）
7. 提供 /sessions GET 接口（历史爬取 session 列表）
8. 支持 ?session= 参数切换历史 session

用法：
    python3 serve_dashboard.py --output-dir /path/to/01_raw/ [--port 8765] [--history-root /path/to/root/]
    
    或由 pipeline 自动启动：
    from serve_dashboard import start_dashboard_server
    url = start_dashboard_server("/path/to/01_raw/")
    # 返回 "http://localhost:8765"
"""

import http.server
import json
import os
import csv
import sys
import threading
import urllib.parse
import subprocess
import time
import signal
from datetime import datetime
import glob as glob_mod
import urllib.request

# === CDP 连接加固（修复 [Errno 61] Connection refused）===
# 根因：macOS 上 localhost 同时解析到 ::1(IPv6) 与 127.0.0.1(IPv4)，
# 而 Chromium 仅监听 IPv4。http.client 连 localhost 会自动回落到 127.0.0.1
# 成功，但 websocket-client 连 ws://localhost:9222 时若先命中 ::1 且不回落，
# 就会直接 Connection refused，造成「CDP HTTP 通、WebSocket 拒」的错位。
# 对策：① 所有 CDP 连接强制走 127.0.0.1；② ws URL 中的 localhost 统一改写成
# 127.0.0.1；③ 把本机地址加入 NO_PROXY 白名单，避免 ClashX 等全局代理把
# localhost 的 websocket 也转发到代理而被拒。
_PROXY_OLD = os.environ.get('NO_PROXY', os.environ.get('no_proxy', ''))
os.environ['NO_PROXY'] = '127.0.0.1,localhost' + (',' + _PROXY_OLD if _PROXY_OLD else '')
os.environ['no_proxy'] = os.environ['NO_PROXY']

DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'dashboard')

# ── 优先级判定监控模块 ──────────────────────────────────────
try:
    from judge_monitor import judge_monitor
    JUDGE_MONITOR_AVAILABLE = judge_monitor.available
except Exception as e:
    judge_monitor = None
    JUDGE_MONITOR_AVAILABLE = False
    print(f"[serve_dashboard] judge_monitor 加载失败: {e}")


class CrawlManager:
    """管理爬取/打招呼子进程。"""

    def _cdp_navigate(self, target_url: str) -> bool:
        """CDP 新建 tab 并导航到目标 URL。返回 True 表示成功。"""
        try:
            import http.client, json
            conn = http.client.HTTPConnection('127.0.0.1', 9222, timeout=5)
            conn.request('PUT', '/json/new?' + target_url)
            resp = conn.getresponse()
            body = json.loads(resp.read().decode())
            conn.close()
            tab_id = body.get('id')
            if tab_id:
                import time as _t
                _t.sleep(3)  # 等页面开始加载
                return True
            return False
        except Exception:
            return False

    @staticmethod
    def _normalize_ws(url: str) -> str:
        if not url:
            return url
        return url.replace('[::1]', '127.0.0.1').replace('localhost', '127.0.0.1')

    def _navigate_reuse(self, target_url: str) -> bool:
        """优先复用已打开的 zhipin tab 导航到目标 URL；无 zhipin tab 时才新建。

        修复「列表页 tab 越积越多导致爬错页」：旧逻辑每次 PUT /json/new 新建 tab，
        从不关闭，浏览器里堆积多个 zhipin jobs tab，爬虫启发式选页会挑错。
        """
        try:
            import http.client
            conn = http.client.HTTPConnection('127.0.0.1', 9222, timeout=5)
            conn.request('GET', '/json')
            resp = conn.getresponse()
            tabs = json.loads(resp.read().decode())
            conn.close()
            target_ws = None
            for tab in tabs:
                if tab.get('type') == 'page' and 'zhipin.com' in tab.get('url', ''):
                    target_ws = self._normalize_ws(tab.get('webSocketDebuggerUrl'))
                    break
            if not target_ws:
                return self._cdp_navigate(target_url)
            import websocket
            ws = websocket.create_connection(target_ws, timeout=10)
            ws.send(json.dumps({'id': 1, 'method': 'Page.navigate', 'params': {'url': target_url}}))
            try:
                ws.recv()
            except Exception:
                pass
            ws.close()
            time.sleep(3)
            return True
        except Exception:
            return False

    def __init__(self):
        self.process = None       # subprocess.Popen 对象（当前活跃任务）
        self.kind = None          # 'crawl' | 'greet'
        self.started_at = None    # 启动时间
        self.pid = None           # 进程 PID
        self.log_path = None      # 日志文件路径
        # 任务管理器：记录所有启动过的任务（含已结束），支持暂停/恢复/停止/重启
        self.tasks = {}           # task_id -> task dict
        self._task_seq = 0

    def start_crawl(self, params: dict) -> dict:
        """启动爬取子进程。params: {url, max_scroll, max_jobs, base_wait, output_dir, session_name}"""
        if self.process and self.process.poll() is None:
            return {'error': '已有爬取任务在运行'}

        # 如果提供了 URL，先导航 CDP 浏览器到目标页面。
        # navigate=False（跳过滚动场景）：保持浏览器当前页面（已滚动到底），
        # 不再 Page.navigate 重载，避免丢失滚动位置导致只能爬到顶部岗位。
        target_url = params.get('url', '')
        if target_url and params.get('navigate', True):
            try:
                self._navigate_reuse(target_url)
            except Exception:
                pass  # 导航失败不阻塞，爬虫自身有重试

        output_dir = params.get('output_dir', '')
        if not output_dir:
            return {'error': '缺少 output_dir'}
        os.makedirs(output_dir, exist_ok=True)

        # === A3: 断点续爬参数恢复 ===
        # 续爬时从 crawl_params.json 恢复上次参数（url / max_scroll / max_jobs 等），
        # 显式传入的键优先，不覆盖（max_scroll=0 表示「跳过滚动」，必须保留）。
        params_file = os.path.join(output_dir, 'crawl_params.json')
        if params.get('resume') and os.path.isfile(params_file):
            try:
                with open(params_file, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                for k in ('url', 'max_scroll', 'max_jobs', 'base_wait', 'scroll_wait',
                          'scroll_delta', 'job_gap', 'detail_jitter'):
                    if (k not in params or params[k] is None) and saved.get(k) is not None:
                        params[k] = saved.get(k)
                target_url = params.get('url', '')
            except Exception:
                pass
        # 持久化本次参数（供下次续爬）
        try:
            saved_params = {k: params.get(k) for k in ('url', 'max_scroll', 'max_jobs', 'base_wait',
                                                       'scroll_wait', 'scroll_delta', 'job_gap', 'detail_jitter')}
            tmp = params_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(saved_params, f, ensure_ascii=False, indent=2)
            os.replace(tmp, params_file)
        except Exception:
            pass

        cmd = [
            sys.executable,
            os.path.expanduser('~/.agents/skills/boss-resume-crawler/scripts/boss_extract_cdp.py'),
            '--output', output_dir,
        ]
        if target_url:
            cmd += ['--url', target_url]
        cmd += [
            '--max-scroll', str(params.get('max_scroll', 50)),
            '--base-wait', str(params.get('base_wait', 20)),
            '--scroll-wait', str(params.get('scroll_wait', 5)),
            '--scroll-delta', str(params.get('scroll_delta', '1.5-2.5')),
            '--job-gap', str(params.get('job_gap', '1.0-3.0')),
            '--detail-jitter', str(params.get('detail_jitter', 3)),
        ]
        max_jobs_val = params.get('max_jobs')
        if max_jobs_val and int(max_jobs_val) > 0:
            cmd += ['--max-jobs', str(max_jobs_val)]
        # 续爬不再强制 max-scroll=1：保留上次滚动参数，由爬虫按 job_id 跨文件去重跳过已爬

        session_name = params.get('session_name', '')
        history_root = params.get('history_root', '')
        if history_root and session_name:
            log_path = os.path.join(history_root, session_name, 'pipeline.log')
        else:
            log_path = os.path.join(os.path.dirname(output_dir), 'pipeline.log')
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log_file = open(log_path, 'a')

        self.process = subprocess.Popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT,
            preexec_fn=os.setsid
        )
        self.kind = 'crawl'
        self.started_at = time.time()
        self.pid = self.process.pid
        self.log_path = log_path

        self._register_task('crawl', session_name or '爬取任务', cmd, log_path)
        result = {'ok': True, 'pid': self.pid, 'log': log_path}
        if session_name:
            result['session_name'] = session_name
        if params.get('session_name_renamed'):
            result['renamed'] = True
        return result

    def start_greet(self, params: dict) -> dict:
        """启动打招呼子进程。params: {csv_path, message, max_greet}"""
        if self.process and self.process.poll() is None:
            return {'error': '已有任务在运行'}

        csv_path = params.get('csv_path', '')
        if not csv_path or not os.path.isfile(csv_path):
            return {'error': 'CSV 文件不存在'}

        cmd = [
            sys.executable,
            os.path.expanduser('~/.agents/skills/boss-auto-greet/scripts/boss_auto_greet.py'),
            '--csv', csv_path,
        ]
        if params.get('message'):
            cmd += ['--message', params['message']]
        if params.get('max_greet') and int(params['max_greet']) > 0:
            cmd += ['--max-greet', str(params['max_greet'])]
        if params.get('first_one'):
            cmd += ['--first-one']

        log_path = os.path.join(os.path.dirname(csv_path), 'greet_pipeline.log')
        log_file = open(log_path, 'a')

        self.process = subprocess.Popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT,
            preexec_fn=os.setsid
        )
        self.kind = 'greet'
        self.started_at = time.time()
        self.pid = self.process.pid
        self.log_path = log_path

        self._register_task('greet', '打招呼任务', cmd, log_path)
        return {'ok': True, 'pid': self.pid, 'log': log_path}

    def skip_scroll(self, params: dict) -> dict:
        """跳过滚动：可靠终止当前爬取进程，等待其完全退出后以 max-scroll=0 重启。

        修复「点击跳过滚动弹『已有爬取任务』后停住」：
        旧实现 kill 后不等待进程回收就调用 start_crawl，其首行守卫
        poll() is None 会误判旧进程仍存活，返回「已有爬取任务在运行」。
        """
        process_dead = not self.process or self.process.poll() is not None
        if process_dead:
            # 进程已死：直接以「跳过滚动」语义重启（不再残留 max_scroll=1）
            params['max_scroll'] = 0
            params['resume'] = True
            params['navigate'] = False
            return self.start_crawl(params)
        # 1) SIGINT 优雅终止
        try:
            import signal
            os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
            self.process.wait(timeout=10)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        # 2) 轮询等待 poll() 归位（最长 10 秒），确保 start_crawl 守卫不会误触发
        for _ in range(100):
            if self.process is None or self.process.poll() is not None:
                break
            time.sleep(0.1)
        if self.process and self.process.poll() is None:
            return {'error': '旧爬取任务未能停止，请稍后重试或先点「停止」'}
        params['max_scroll'] = 0
        params['resume'] = True
        # 关键：不重新导航浏览器，保留已滚动到底的列表页（否则页面重载、滚动失效）
        params['navigate'] = False
        return self.start_crawl(params)

    def stop(self) -> dict:
        """停止子进程。"""
        if not self.process or self.process.poll() is not None:
            return {'error': '无运行中的任务'}
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        return {'ok': True, 'pid': self.pid}

    # === 任务管理器 ===
    def _register_task(self, kind: str, label: str, cmd: list, log_path: str):
        """把刚启动的子进程登记到任务列表。"""
        self._task_seq += 1
        task_id = f"task-{self._task_seq}"
        self.tasks[task_id] = {
            'id': task_id,
            'kind': kind,
            'label': label,
            'cmd': list(cmd),
            'process': self.process,
            'pid': self.pid,
            'log_path': log_path,
            'paused': False,
            'started_at': time.time(),
            'finished_at': None,
        }
        return task_id

    def list_tasks(self) -> list:
        """返回任务列表（含状态/已用时间）。"""
        result = []
        for t in self.tasks.values():
            proc = t['process']
            poll = proc.poll()
            if t.get('paused'):
                status = 'paused'
            elif poll is None:
                status = 'running'
            else:
                status = 'finished'
            if status == 'finished':
                elapsed = round((t.get('finished_at') or time.time()) - t['started_at'])
            else:
                elapsed = round(time.time() - t['started_at'])
            result.append({
                'id': t['id'],
                'kind': t['kind'],
                'label': t['label'],
                'status': status,
                'pid': t['pid'],
                'elapsed': elapsed,
                'returncode': poll if status == 'finished' else None,
            })
        result.sort(key=lambda x: x['id'], reverse=True)
        return result

    def _get_task(self, task_id: str):
        return self.tasks.get(task_id)

    def pause_task(self, task_id: str) -> dict:
        """SIGSTOP 暂停任务。"""
        t = self.tasks.get(task_id)
        if not t:
            return {'error': '任务不存在'}
        if t['process'].poll() is not None:
            return {'error': '任务已结束'}
        if t.get('paused'):
            return {'error': '任务已是暂停状态'}
        try:
            os.killpg(os.getpgid(t['process'].pid), signal.SIGSTOP)
            t['paused'] = True
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}

    def resume_task(self, task_id: str) -> dict:
        """SIGCONT 恢复暂停的任务。"""
        t = self.tasks.get(task_id)
        if not t:
            return {'error': '任务不存在'}
        if not t.get('paused'):
            return {'error': '任务未处于暂停状态'}
        try:
            os.killpg(os.getpgid(t['process'].pid), signal.SIGCONT)
            t['paused'] = False
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}

    def stop_task(self, task_id: str) -> dict:
        """停止指定任务（SIGTERM → SIGKILL）。"""
        t = self.tasks.get(task_id)
        if not t:
            return {'error': '任务不存在'}
        proc = t['process']
        if t.get('paused'):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGCONT)
            except Exception:
                pass
            t['paused'] = False
        if proc.poll() is not None:
            t['finished_at'] = t.get('finished_at') or time.time()
            return {'ok': True, 'already': True}
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        t['finished_at'] = time.time()
        return {'ok': True}

    def restart_task(self, task_id: str) -> dict:
        """用保存的命令重新启动已结束/停止的任务。"""
        t = self.tasks.get(task_id)
        if not t:
            return {'error': '任务不存在'}
        # 若仍在运行/暂停，先停止
        if t['process'].poll() is None:
            self.stop_task(task_id)
        # 避免与当前运行任务冲突（CDP 浏览器单一，同一时刻一个活跃任务）
        for other in self.tasks.values():
            if other['id'] != task_id and other['process'].poll() is None and not other.get('paused'):
                return {'error': '已有其他任务在运行，请先停止'}
        log_path = t['log_path']
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            log_file = open(log_path, 'a')
        except Exception as e:
            return {'error': f'日志文件打开失败: {e}'}
        new_proc = subprocess.Popen(
            t['cmd'], stdout=log_file, stderr=subprocess.STDOUT,
            preexec_fn=os.setsid
        )
        t['process'] = new_proc
        t['pid'] = new_proc.pid
        t['paused'] = False
        t['started_at'] = time.time()
        t['finished_at'] = None
        # 更新「当前活跃任务」引用
        self.process = new_proc
        self.kind = t['kind']
        self.pid = new_proc.pid
        self.started_at = t['started_at']
        self.log_path = log_path
        return {'ok': True, 'pid': new_proc.pid, 'task_id': task_id}

    def status(self) -> dict:
        """获取子进程状态。"""
        if not self.process:
            return {'running': False, 'kind': None}
        running = self.process.poll() is None
        return {
            'running': running,
            'kind': self.kind,
            'pid': self.pid,
            'elapsed': round(time.time() - self.started_at) if self.started_at else 0,
            'returncode': self.process.returncode if not running else None,
        }


# 全局实例
crawl_manager = CrawlManager()


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """自定义 HTTP Handler：静态文件 + JSON 代理 + CSV 查询。"""

    output_dir = None        # 当前 session 的 01_raw/ 目录
    history_root = None      # 历史 session 根目录（如 boss直聘一体化/）

    @staticmethod
    def _resolve_session_dir(session_name: str) -> str:
        """根据 session 名称解析到对应的 01_raw/ 目录。"""
        if not session_name:
            return DashboardHandler.output_dir
        # session 格式为 "YYYY-MM-DD-XXX"
        candidate = os.path.join(DashboardHandler.history_root, session_name, '01_raw')
        if os.path.isdir(candidate):
            return candidate
        return DashboardHandler.output_dir

    @staticmethod
    def _scan_sessions() -> list:
        """扫描历史爬取 session 目录。"""
        root = DashboardHandler.history_root
        if not root or not os.path.isdir(root):
            return []
        sessions = []
        try:
            for name in os.listdir(root):
                raw_dir = os.path.join(root, name, '01_raw')
                if not os.path.isdir(raw_dir):
                    continue
                # 统计 CSV 数量
                csv_count = len([f for f in os.listdir(raw_dir) if f.endswith('.csv')])
                # 读取 progress.json 获取状态
                progress_path = os.path.join(raw_dir, 'progress.json')
                status = 'unknown'
                job_count = 0
                updated_at = ''
                search_url = ''
                if os.path.exists(progress_path):
                    try:
                        with open(progress_path, 'r', encoding='utf-8') as f:
                            p = json.load(f)
                        status = p.get('status', 'unknown')
                        job_count = p.get('scraped', 0)
                        updated_at = p.get('updated_at', '')
                        search_url = p.get('search_url', '')
                    except Exception:
                        pass
                # 重新爬取锁定 URL 的兜底来源：crawl_params.json（A3 持久化的 url）
                crawl_url = ''
                cp_path = os.path.join(raw_dir, 'crawl_params.json')
                if os.path.isfile(cp_path):
                    try:
                        with open(cp_path, 'r', encoding='utf-8') as f:
                            cp = json.load(f)
                        crawl_url = cp.get('url') or ''
                    except Exception:
                        pass
                sessions.append({
                    'id': name,
                    'label': name,
                    'path': raw_dir,
                    'csvCount': csv_count,
                    'jobCount': job_count,
                    'status': status,
                    'updatedAt': updated_at,
                    'searchUrl': search_url,
                    'crawlUrl': crawl_url,
                })
        except Exception:
            pass
        # 按更新时间倒序
        sessions.sort(key=lambda s: s.get('updatedAt', ''), reverse=False)
        return sessions

    def _stats_global(self):
        """全局统计：综合全部历史 session 的爬取记录，跨 session 按 job_id 去重汇总。

        规则：
        - 总爬取：跨 session job_id 去重数量（另附原始总数=各 session 相加）
        - 判定：每个 job 取最新判定（session 按时间倒序处理，先到先得，旧判定不覆盖）
        - 打招呼：每个 job 按 greet_log 聚合，取时间最新记录
        - 每日趋势：按 session 目录名首 10 位（YYYY-MM-DD）聚合去重 job 数
        """
        root = DashboardHandler.history_root
        if not root or not os.path.isdir(root):
            return self._json_response({'error': '无历史数据目录'})

        sessions = []
        all_job_sessions = {}   # job_id -> set(session 名)
        judged_latest = {}      # job_id -> 优先级（最新判定）
        greet_latest = {}       # job_id -> {result, time}
        daily_jobs = {}         # date -> set(job_id)
        # 全局聚合（薪资/地区/耗时，跨 session 汇总）
        salary_buckets_total = {}   # bucket -> 数量
        location_total = {}         # 区 -> 数量
        time_weight_sum = 0.0       # Σ(avg_seconds_per_job × scraped)
        time_count = 0
        salary_weight_sum = 0.0     # Σ(平均薪资 × count)
        salary_count = 0

        # 最新 session 在前（名字按时间倒序，兼容 YYYY-MM-DD-前缀）
        names = sorted(os.listdir(root), reverse=True)
        for name in names:
            raw_dir = os.path.join(root, name, '01_raw')
            if not os.path.isdir(raw_dir):
                continue
            date = name[:10] if len(name) >= 10 and name[4] == '-' else '未知'

            # 本 session 爬取（跨 CSV 去重）
            job_ids = set()
            for fp in glob_mod.glob(os.path.join(raw_dir, 'jobs_data*.csv')):
                try:
                    with open(fp, 'r', encoding='utf-8-sig') as f:
                        for row in csv.DictReader(f):
                            jid = (row.get('job_id') or '').strip()
                            if jid:
                                job_ids.add(jid)
                except Exception:
                    pass
            if not job_ids:
                continue

            # 本 session 判定（最新判定 CSV）
            mapping, _src = self._load_judgment_map(raw_dir)
            judged = {jid: m['judgment_level'] for jid, m in mapping.items() if m.get('judgment_level')}
            # 本 session 打招呼
            greet = {}
            greet_dir = os.path.join(root, name, '03_greet')
            glp = os.path.join(greet_dir, 'greet_log.csv')
            if os.path.isfile(glp):
                try:
                    with open(glp, 'r', encoding='utf-8-sig') as f:
                        for row in csv.DictReader(f):
                            jid = (row.get('job_id') or '').strip()
                            if not jid:
                                continue
                            raw = (row.get('结果') or row.get('status') or '').strip()
                            if raw in ('成功', 'done'):
                                r = '成功'
                            elif '跳过' in raw or '无沟通' in raw or 'SKIP' in raw:
                                r = '跳过'
                            else:
                                r = '失败'
                            ts = (row.get('时间') or '').strip()
                            if jid not in greet or (ts and ts >= greet[jid].get('time', '')):
                                greet[jid] = {'result': r, 'time': ts}
                except Exception:
                    pass

            for jid in job_ids:
                all_job_sessions.setdefault(jid, set()).add(name)
                daily_jobs.setdefault(date, set()).add(jid)
            # 最新判定：session 倒序处理，先到（最新）优先，旧的不覆盖
            for jid, lv in judged.items():
                if jid not in judged_latest:
                    judged_latest[jid] = lv
            for jid, g in greet.items():
                if jid not in greet_latest:
                    greet_latest[jid] = g

            # 读 progress.json 聚合薪资/地区/耗时（该 session 爬取时统计）
            pp = os.path.join(raw_dir, 'progress.json')
            if os.path.isfile(pp):
                try:
                    with open(pp, 'r', encoding='utf-8') as f:
                        pd = json.load(f)
                    avg_sec = pd.get('avg_seconds_per_job') or 0
                    scraped_n = pd.get('scraped') or 0
                    if avg_sec and scraped_n:
                        time_weight_sum += avg_sec * scraped_n
                        time_count += scraped_n
                    ss = pd.get('salary_stats') or {}
                    if ss.get('avg') and ss.get('count'):
                        salary_weight_sum += ss['avg'] * ss['count']
                        salary_count += ss['count']
                    for b, n in (pd.get('salary_buckets') or {}).items():
                        salary_buckets_total[b] = salary_buckets_total.get(b, 0) + int(n)
                    for loc, n in (pd.get('location_stats') or {}).items():
                        location_total[loc] = location_total.get(loc, 0) + int(n)
                except Exception:
                    pass

            sessions.append({
                'id': name,
                'date': date,
                'crawled': len(job_ids),
                'judged': sum(1 for jid in job_ids if jid in judged),
                'high': sum(1 for jid in job_ids if judged.get(jid) == '高'),
                'medium': sum(1 for jid in job_ids if judged.get(jid) == '中'),
                'low': sum(1 for jid in job_ids if judged.get(jid) == '低'),
                'greet_success': sum(1 for jid in job_ids if greet.get(jid, {}).get('result') == '成功'),
            })

        # 跨 session 重复岗位
        dup_jobs = []
        for jid, sess in all_job_sessions.items():
            if len(sess) > 1:
                dup_jobs.append({'job_id': jid, 'count': len(sess), 'sessions': sorted(sess)})

        # 判定分布（每 job 最新判定）
        dist = {'高': 0, '中': 0, '低': 0}
        for lv in judged_latest.values():
            if lv in dist:
                dist[lv] += 1

        greet_sum = {'成功': 0, '失败': 0, '跳过': 0}
        for g in greet_latest.values():
            if g['result'] in greet_sum:
                greet_sum[g['result']] += 1

        return self._json_response({
            'total_crawled_unique': len(all_job_sessions),
            'total_crawled_raw': sum(s['crawled'] for s in sessions),
            'total_judged': len(judged_latest),
            'judgment_dist': dist,
            'greet': {'success': greet_sum['成功'], 'failed': greet_sum['失败'], 'skipped': greet_sum['跳过']},
            'daily_trend': {d: len(jobs) for d, jobs in sorted(daily_jobs.items())},
            'dup_count': len(dup_jobs),
            'dup_jobs': dup_jobs,
            'sessions': sessions,
            'session_count': len(sessions),
            # 新增：平均耗时 / 平均薪资 / 薪资分布 / 地区分布 / 中高占比
            'avg_time': round(time_weight_sum / time_count, 1) if time_count else 0,
            'avg_salary': round(salary_weight_sum / salary_count, 1) if salary_count else None,
            'salary_buckets': salary_buckets_total,
            'location_stats': location_total,
            'high_mid_ratio': round((dist['高'] + dist['中']) / len(judged_latest) * 100, 1) if judged_latest else 0,
        })

    def _get_current_session_id(self) -> str:
        """从 output_dir 推导出当前 session ID（如 2026-08-05-人力数字化）。"""
        # output_dir = .../YYYY-MM-DD-XXX/01_raw/
        return os.path.basename(os.path.dirname(self.output_dir or ''))

    @staticmethod
    def _dedupe_session_name(name: str) -> str:
        """重名 session 自动避让：history_root 下已存在同名目录时追加 -2/-3 后缀。

        修复「同名任务合并目录 → 多份 CSV 混在一起 → 数据读取混乱/读取不出来」。
        """
        root = DashboardHandler.history_root
        if not root or not name:
            return name
        base = name
        if not os.path.isdir(os.path.join(root, name)):
            return name
        i = 2
        while os.path.isdir(os.path.join(root, f'{base}-{i}')):
            i += 1
        return f'{base}-{i}'

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.lstrip('/')
        qs = urllib.parse.parse_qs(parsed.query)
        session = qs.get('session', [None])[0]
        dir_ = self._resolve_session_dir(session)

        # /api/stats/global → 全局统计（跨 session 去重汇总）
        if path == 'api/stats/global':
            return self._stats_global()

        # /sessions → 历史 session 列表
        if path == 'sessions':
            sessions = self._scan_sessions()
            current = self._get_current_session_id()
            return self._json_response({'sessions': sessions, 'current': current})

        # /progress.json → 自动修正死进程的 stale "running" 状态
        if path == 'progress.json':
            # 进程已死但 progress.json 仍显示 running → 修正为 error
            cs = crawl_manager.status()
            if not cs.get('running') and cs.get('kind') == 'crawl':
                pp = os.path.join(dir_, 'progress.json')
                if os.path.isfile(pp):
                    try:
                        with open(pp, 'r', encoding='utf-8') as f:
                            pd = json.load(f)
                        if pd.get('status') == 'running':
                            pd['status'] = 'error'
                            pd['updated_at'] = datetime.now().isoformat()
                            tmp = pp + '.tmp'
                            with open(tmp, 'w', encoding='utf-8') as f:
                                json.dump(pd, f, ensure_ascii=False, indent=2)
                            os.replace(tmp, pp)
                    except Exception:
                        pass
            return self._serve_json(dir_, 'progress.json')

        # /jobs_cache.json → 合并判定结果后返回
        if path == 'jobs_cache.json':
            return self._serve_jobs_cache(dir_)

        # /priorities.json → 原始文件读取
        if path == 'priorities.json':
            self._auto_init_priorities(dir_)
            return self._serve_json(dir_, 'priorities.json')

        # /detail?job_id=xxx
        if path == 'detail':
            job_id = qs.get('job_id', [None])[0]
            return self._serve_detail(dir_, job_id)

        # /files → 列出 CSV 文件
        if path == 'files':
            files = []
            try:
                for f in sorted(os.listdir(dir_)):
                    if f.endswith('.csv'):
                        fp = os.path.join(dir_, f)
                        files.append({'name': f, 'size': os.path.getsize(fp)})
            except Exception:
                pass
            return self._json_response({'dir': dir_, 'files': files})

        # /open-folder → Finder 打开
        if path == 'open-folder':
            return self._open_folder(dir_)

        # /api/tasks → 任务管理器：列出所有任务 + 运行中数量
        if path == 'api/tasks':
            tasks = crawl_manager.list_tasks()
            running_count = sum(1 for t in tasks if t['status'] in ('running', 'paused'))
            return self._json_response({'tasks': tasks, 'running_count': running_count})

        # /api/crawl/status → 爬取状态
        if path == 'api/crawl/status':
            return self._json_response(crawl_manager.status())

        # /api/check-login → CDP 检查登录状态
        if path == 'api/check-login':
            return self._check_login()

        # /api/browser/status → CloakBrowser 运行状态
        if path == 'api/browser/status':
            return self._browser_status()

        # /api/browser/start → 自动启动 CloakBrowser
        if path == 'api/browser/start':
            return self._browser_start()

        # /api/browser/active-list → 当前已打开的 Boss 列表页 URL
        if path == 'api/browser/active-list':
            return self._handle_active_list()

        # /api/agent-priorities → 读取 02_result CSV 优先级（合并手动覆盖）
        if path == 'api/agent-priorities':
            return self._agent_priorities(dir_)

        # /priority (GET) → 读取合并后优先级（前端 showGreetModal 调用）
        if path == 'priority':
            return self._agent_priorities(dir_)

        # /api/judgment-prompt → 生成完整判定提示词（读取模板，填充当前 session 路径）
        if path == 'api/judgment-prompt':
            return self._judgment_prompt(dir_)

        # /api/create-empty-judgment → 创建空判定 CSV（供手动操作判定）
        if path == 'api/create-empty-judgment':
            return self._create_empty_judgment(dir_)

        # /api/judgment-files → 列出可导入的判定 CSV
        if path == 'api/judgment-files':
            return self._judgment_files(dir_)

        # /api/greet/status → 打招呼状态
        if path == 'api/greet/status':
            return self._greet_status(dir_)

        # /api/greeting-files → 列出可用打招呼语 + 当前来源
        if path == 'api/greeting-files':
            return self._greeting_files()

        # /api/greeting-content → 读取打招呼语内容
        if path == 'api/greeting-content':
            return self._greeting_content(qs)

        # /api/greeting-open → Finder 打开打招呼语文件
        if path == 'api/greeting-open':
            name = qs.get('file', ['default.md'])[0]
            path = os.path.join(self.GREETINGS_DIR, name)
            subprocess.run(['open', '-R', path], timeout=5)
            return self._json_response({'ok': True, 'path': path})

        # /api/judge/status → 优先级判定监控状态
        if path == 'api/judge/status':
            if judge_monitor:
                return self._json_response(judge_monitor.get_status())
            return self._json_response({'available': False, 'error': 'judge_monitor 未加载'})

        # / → index.html（no-cache 防浏览器缓存旧版）
        if path in ('', 'index.html'):
            return self._serve_file(os.path.join(DASHBOARD_DIR, 'index.html'), 'text/html', cache=False)

        # 其他静态文件
        file_path = os.path.join(DASHBOARD_DIR, path)
        if os.path.isfile(file_path):
            return self._serve_file(file_path)

        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.lstrip('/')
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else b'{}'

        try:
            data = json.loads(body)
        except Exception:
            return self._json_response({'error': 'invalid json'}, 400)

        # session 优先从 body 读取，其次从 query string
        session = data.get('session') or None
        if not session:
            qs = urllib.parse.parse_qs(parsed.query)
            session = qs.get('session', [None])[0]
        dir_ = self._resolve_session_dir(session)

        # /api/browser/start → 自动启动 CloakBrowser
        if path == 'api/browser/start':
            return self._browser_start()

        # /api/browser/navigate → CDP 导航浏览器到指定 URL
        if path == 'api/browser/navigate':
            return self._browser_navigate(data)

        # /api/browser/active-list → 当前已打开的 Boss 列表页 URL
        if path == 'api/browser/active-list':
            return self._handle_active_list()

        # /api/task/pause|resume|stop|restart → 任务管理器操作
        if path in ('api/task/pause', 'api/task/resume', 'api/task/stop', 'api/task/restart'):
            task_id = data.get('task_id', '')
            if not task_id:
                return self._json_response({'error': '缺少 task_id'}, 400)
            action = path.rsplit('/', 1)[-1]
            fn = {'pause': crawl_manager.pause_task,
                  'resume': crawl_manager.resume_task,
                  'stop': crawl_manager.stop_task,
                  'restart': crawl_manager.restart_task}.get(action)
            if not fn:
                return self._json_response({'error': '未知操作'}, 400)
            return self._json_response(fn(task_id))

        # /api/crawl/start → 启动爬取
        if path == 'api/crawl/start':
            data['history_root'] = DashboardHandler.history_root or ''
            # A4: 重名 session 自动避让（防止同名任务合并目录导致数据混乱）
            # keep_session（重新爬取）与 resume（续爬）场景写回原 session，不避让
            if data.get('session_name') and not data.get('resume') and not data.get('keep_session'):
                renamed = self._dedupe_session_name(data['session_name'])
                if renamed != data['session_name']:
                    data['session_name'] = renamed
                    data['session_name_renamed'] = True
            if not data.get('output_dir'):
                session_name = data.get('session_name', '')
                if DashboardHandler.history_root and session_name:
                    data['output_dir'] = os.path.join(DashboardHandler.history_root, session_name, '01_raw')
                else:
                    data['output_dir'] = DashboardHandler.output_dir or ''
            result = crawl_manager.start_crawl(data)
            # 启动判定监控（如果 matcher 可用）
            if result.get('ok') and judge_monitor and judge_monitor.available:
                output_dir = data.get('output_dir', '')
                if output_dir:
                    judge_monitor.start_monitor(output_dir)
            return self._json_response(result)

        # /api/crawl/stop → 停止爬取
        if path == 'api/crawl/stop':
            # 停止判定监控
            if judge_monitor:
                judge_monitor.stop_monitor()
            return self._json_response(crawl_manager.stop())

        # /api/crawl/skip-scroll → 跳过滚动，直接爬取
        if path == 'api/crawl/skip-scroll':
            data['history_root'] = DashboardHandler.history_root or ''
            if not data.get('output_dir'):
                data['output_dir'] = DashboardHandler.output_dir or ''
            return self._json_response(crawl_manager.skip_scroll(data))

        # /api/greet/start → 启动打招呼
        if path == 'api/greet/start':
            return self._start_greet(dir_, data)

        # /priority → 写入优先级
        if path == 'priority':
            return self._handle_priority(dir_, data)

        # /api/import-judgment → 手动导入指定判定 CSV（持久化）
        if path == 'api/import-judgment':
            return self._import_judgment(dir_, data)

        # /api/create-empty-judgment → 创建空判定 CSV（POST 入口）
        if path == 'api/create-empty-judgment':
            return self._create_empty_judgment(dir_)

        # /api/judge/incremental → 增量判定（每 20 条）
        if path == 'api/judge/incremental':
            return self._handle_judge(dir_, 'incremental')

        # /api/judge/full → 全量判定（所有未判定的 JD）
        if path == 'api/judge/full':
            return self._handle_judge(dir_, 'full')

        # /navigate → CDP 跳转
        if path == 'navigate':
            return self._handle_navigate(data)

        self.send_error(404)

    def _serve_json(self, dir_: str, filename: str):
        filepath = os.path.join(dir_, filename)
        if not os.path.exists(filepath):
            if filename == 'priorities.json':
                return self._json_response({})
            return self._json_response({})
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data.encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _handle_priority(self, dir_: str, data: dict):
        """写入/读取优先级，并同步写回 02_result 当前生效判定 CSV。"""
        pri_path = os.path.join(dir_, 'priorities.json')
        priorities = {}
        if os.path.exists(pri_path):
            try:
                with open(pri_path, 'r', encoding='utf-8') as f:
                    priorities = json.load(f)
            except Exception:
                priorities = {}

        job_id = data.get('job_id')
        level = data.get('level')
        note = data.get('note')

        # 更新优先级（支持 note 字段）
        if job_id and level in ('高', '中', '低'):
            existing = priorities.get(job_id)
            old_note = ''
            if isinstance(existing, dict):
                old_note = existing.get('note', '')
            priorities[job_id] = {'level': level, 'note': note if note is not None else old_note}
            tmp = pri_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(priorities, f, ensure_ascii=False, indent=2)
            os.replace(tmp, pri_path)
            self._sync_priority_to_csv(dir_, job_id, {'高': 'high', '中': 'medium', '低': 'low'}[level])
        elif job_id and note is not None and not level:
            # 仅更新备注（不改优先级）
            existing = priorities.get(job_id)
            if isinstance(existing, dict):
                existing['note'] = note
            elif isinstance(existing, str):
                priorities[job_id] = {'level': existing, 'note': note}
            else:
                priorities[job_id] = {'level': '', 'note': note}
            tmp = pri_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(priorities, f, ensure_ascii=False, indent=2)
            os.replace(tmp, pri_path)
            self._sync_note_to_csv(dir_, job_id, note)

        # 统一返回格式：兼容前端（前端同时处理 string 和 dict）
        return self._json_response({'priorities': priorities})

    def _sync_priority_to_csv(self, dir_: str, job_id: str, level_en: str):
        """把手动优先级同步写回 02_result 当前生效判定 CSV 的 优先级判断 字段。
        若无判定 CSV 则自动创建空判定 CSV 再写入。"""
        try:
            _mapping, chosen = self._load_judgment_map(dir_)
        except Exception:
            chosen = None
        if not chosen:
            # 自动创建空判定 CSV
            result = self._do_create_empty_judgment(dir_)
            if not result or not result.get('ok'):
                return
            try:
                _mapping, chosen = self._load_judgment_map(dir_)
            except Exception:
                return
            if not chosen:
                return
        result_dir = os.path.join(os.path.dirname(dir_), '02_result')
        csv_path = os.path.join(result_dir, chosen)
        if not os.path.isfile(csv_path):
            return
        try:
            rows = []
            with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for row in reader:
                    if row.get('job_id') == job_id:
                        row['优先级判断'] = level_en
                    rows.append(row)
            if fieldnames and '优先级判断' not in fieldnames:
                fieldnames = fieldnames + ['优先级判断']
            with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(rows)
        except Exception:
            pass

    def _sync_note_to_csv(self, dir_: str, job_id: str, note: str):
        """把手动备注同步写回判定 CSV 的 人工备注 字段。"""
        try:
            _mapping, chosen = self._load_judgment_map(dir_)
        except Exception:
            chosen = None
        if not chosen:
            result = self._do_create_empty_judgment(dir_)
            if not result or not result.get('ok'):
                return
            try:
                _mapping, chosen = self._load_judgment_map(dir_)
            except Exception:
                return
            if not chosen:
                return
        result_dir = os.path.join(os.path.dirname(dir_), '02_result')
        csv_path = os.path.join(result_dir, chosen)
        if not os.path.isfile(csv_path):
            return
        try:
            rows = []
            with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
                for row in reader:
                    if row.get('job_id') == job_id:
                        row['人工备注'] = note
                    rows.append(row)
            if '人工备注' not in fieldnames:
                fieldnames.append('人工备注')
            with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(rows)
        except Exception:
            pass

    def _do_create_empty_judgment(self, dir_: str) -> dict:
        """创建空判定 CSV 核心逻辑（不发送 HTTP 响应）。返回 {ok, csv} 或 {error}。"""
        result_dir = os.path.join(os.path.dirname(dir_), '02_result')
        os.makedirs(result_dir, exist_ok=True)

        # 找最新的 raw CSV
        raws = glob_mod.glob(os.path.join(dir_, 'jobs_data*.csv'))
        if not raws:
            return {'error': '当前 session 无爬取数据（缺少 jobs_data*.csv）'}
        raw_csv = max(raws, key=os.path.getmtime)

        # 生成输出文件名：原文件名 + _判定结果-手动.csv
        base_name = os.path.splitext(os.path.basename(raw_csv))[0]
        out_name = base_name + '_判定结果-手动.csv'
        out_path = os.path.join(result_dir, out_name)

        # 读取 raw CSV，写入带空判定列的 CSV
        try:
            with open(raw_csv, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
                for col in ('优先级判断', '匹配度', '优先级判断理由'):
                    if col not in fieldnames:
                        fieldnames.append(col)
                with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames)
                    w.writeheader()
                    for row in reader:
                        for col in ('优先级判断', '匹配度', '优先级判断理由'):
                            if col not in row:
                                row[col] = ''
                        w.writerow(row)
        except Exception as e:
            return {'error': f'创建失败: {e}'}

        # 持久化为当前生效判定文件
        src_path = os.path.join(dir_, 'judgment_source.json')
        tmp = src_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'csv': out_name}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, src_path)

        self._auto_init_priorities(dir_)
        return {'ok': True, 'csv': out_name}

    def _create_empty_judgment(self, dir_: str):
        """API 包装：创建空判定 CSV 并返回 HTTP 响应。"""
        return self._json_response(self._do_create_empty_judgment(dir_))

    def _handle_navigate(self, data: dict):
        """通过 CDP 在浏览器中打开目标 URL。"""
        target_url = data.get('url', '')
        if not target_url:
            return self._json_response({'error': 'missing url'}, 400)

        try:
            import websocket
        except ImportError:
            return self._json_response({'error': 'websocket-client not installed'}, 500)

        # 获取列表页 ws URL
        try:
            ok, body = self._cdp_fetch('/json')
            if not ok:
                return self._json_response({'error': 'CDP 9222 不可用', 'diagnostic': body}, 500)
            tabs = json.loads(body)
        except json.JSONDecodeError as e:
            return self._json_response({'error': f'CDP 响应解析失败: {e}'}, 500)
        except Exception as e:
            return self._json_response({'error': f'CDP 连接失败: {e}', 'diagnostic': str(e)}, 500)

        # 找到列表页（Boss 直聘 jobs 页面）
        list_ws = None
        for tab in tabs:
            url = tab.get('url', '')
            if 'zhipin.com/web/geek/jobs' in url:
                list_ws = self._normalize_ws(tab.get('webSocketDebuggerUrl'))
                break
        if not list_ws:
            # 退而求其次，用第一个可用的 tab
            for tab in tabs:
                if tab.get('webSocketDebuggerUrl'):
                    list_ws = self._normalize_ws(tab.get('webSocketDebuggerUrl'))
                    break

        if not list_ws:
            return self._json_response({'error': '未找到可用浏览器 tab'}, 500)

        try:
            # 在当前列表页直接 navigate 到目标 URL
            ws = websocket.create_connection(list_ws, timeout=10)
            msg_id = 1
            ws.send(json.dumps({
                'id': msg_id,
                'method': 'Page.navigate',
                'params': {'url': target_url}
            }))
            # 等待响应
            resp = json.loads(ws.recv())
            ws.close()
            return self._json_response({
                'ok': True,
                'message': '已跳转',
                'tab': '列表页'
            })
        except Exception as e:
            return self._json_response({'error': f'CDP 跳转失败: {e}'}, 500)

    def _handle_active_list(self):
        """返回当前浏览器中已打开的 Boss 列表页 URL（无需 websocket）。"""
        ok, body = self._cdp_fetch('/json')
        if not ok:
            return self._json_response({'found': False, 'error': 'CDP 9222 不可用', 'diagnostic': body})
        try:
            tabs = json.loads(body)
        except Exception as e:
            return self._json_response({'found': False, 'error': f'CDP 解析失败: {e}'})
        for tab in tabs:
            url = tab.get('url', '')
            if 'zhipin.com/web/geek/jobs' in url:
                return self._json_response({'found': True, 'url': url})
        return self._json_response({'found': False})

    def _open_folder(self, dir_: str):
        """用 Finder 打开文件夹。"""
        try:
            abs_dir = os.path.abspath(dir_)
            subprocess.run(['open', '-R', abs_dir], timeout=5)
            return self._json_response({'ok': True, 'path': abs_dir})
        except Exception as e:
            return self._json_response({'error': str(e)}, 500)

    def _serve_detail(self, dir_: str, job_id):
        if not job_id:
            return self._json_response({"error": "missing job_id"}, 400)
        import glob
        mapping, _src = self._load_judgment_map(dir_)
        csv_files = glob.glob(os.path.join(dir_, 'jobs_data*.csv'))
        for csv_path in csv_files:
            try:
                with open(csv_path, 'r', encoding='utf-8-sig') as f:
                    for row in csv.DictReader(f):
                        if row.get('job_id') == job_id:
                            if job_id in mapping:
                                row.update(mapping[job_id])
                            manual = self._read_json_raw(dir_, 'priorities.json') or {}
                            if isinstance(manual, dict) and manual.get(job_id) in ('高', '中', '低'):
                                row['judgment_level'] = manual[job_id]
                                row['judgment_manual'] = True
                            return self._json_response(row)
            except Exception:
                continue
        return self._json_response({"error": f"job_id {job_id} not found"}, 404)

    @staticmethod
    def _normalize_ws(url: str) -> str:
        """把 ws URL 中的 localhost / [::1] 统一改写成 127.0.0.1，
        强制 IPv4，避开 macOS localhost 双栈解析导致 websocket 连 ::1 被拒。"""
        if not url:
            return url
        return url.replace('[::1]', '127.0.0.1').replace('localhost', '127.0.0.1')

    @staticmethod
    def _cdp_fetch(path: str = '/json/version') -> list:
        """从 CDP 9222 获取数据（http.client 直连 127.0.0.1，绕过代理）。返回 [success: bool, body: str]."""
        try:
            import http.client
            conn = http.client.HTTPConnection('127.0.0.1', 9222, timeout=5)
            conn.request('GET', path)
            resp = conn.getresponse()
            body = resp.read().decode('utf-8')
            conn.close()
            return [True, body]
        except Exception as e:
            return [False, str(e)]

    def _browser_status(self):
        """检查 CloakBrowser 是否已启动（CDP 9222 是否可用）。"""
        ok, body = self._cdp_fetch('/json/version')
        if not ok:
            return self._json_response({'running': False, 'diagnostic': body})
        if not body.strip():
            return self._json_response({'running': False, 'diagnostic': 'CDP 返回空响应'})
        return self._json_response({'running': True})

    def _browser_start(self):
        """自动启动 CloakBrowser（CDP 9222，有头模式）。"""
        # 已启动（CDP 可用）则复用，无需重复拉起
        ok, body = self._cdp_fetch('/json/version')
        if ok and body.strip():
            return self._json_response({'ok': True, 'already': True})

        binary = os.path.expanduser('~/.cache/cloakbrowser/Chromium.app/Contents/MacOS/Chromium')
        profile = os.path.expanduser('~/.cache/cloakbrowser/profile-boss')
        if not os.path.isfile(binary):
            return self._json_response({'error': f'CloakBrowser 二进制不存在: {binary}'}, 500)

        # 直接启动 Chromium 二进制（可靠传递 CDP 标志）。
        # 关键修复（对应主人提出的两个失败标准）：
        #  1) 不用 `open --args` —— macOS 下 open 不保证把 --remote-debugging-port
        #     传给 Chromium，导致 CDP 端口不生效、Dashboard 连不上。
        #  2) 用 subprocess.Popen 而非 subprocess.run(timeout=...) —— run 会等待进程
        #     退出，而 Chromium 常驻不退出，10s 后超时会被杀掉，造成「浏览器启动即被杀」。
        # 有头模式：不传 --headless，浏览器窗口会真实显示，主人可全程监控。
        # 专用 --user-data-dir 与主人日常浏览器隔离，互不干扰。
        cmd = [
            binary,
            '--remote-debugging-port=9222',
            '--remote-allow-origins=*',
            f'--user-data-dir={profile}',
            '--no-sandbox',
            '--no-first-run',
            '--no-default-browser-check',
            'about:blank',
        ]
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            return self._json_response({'error': f'启动失败: {e}'}, 500)

        # 等待 CDP 就绪（最多 20 秒轮询）
        for _ in range(20):
            time.sleep(1)
            ok, body = self._cdp_fetch('/json/version')
            if ok and body.strip():
                return self._json_response({'ok': True, 'already': False})

        return self._json_response({
            'error': 'CloakBrowser 已触发，但 CDP 9222 未在 20 秒内就绪',
            'diagnostic': 'launch-timeout'
        }, 500)

    def _browser_navigate(self, data: dict):
        """通过 CDP WebSocket 导航浏览器到指定 URL，等待页面加载完成。"""
        target_url = data.get('url', '')
        if not target_url:
            return self._json_response({'error': 'missing url'}, 400)

        try:
            import websocket
        except ImportError:
            return self._json_response({'error': 'websocket-client not installed'}, 500)

        # 获取所有浏览器 tab
        try:
            ok, body = self._cdp_fetch('/json')
            if not ok:
                return self._json_response({'error': 'CDP 9222 不可用', 'diagnostic': body}, 500)
            tabs = json.loads(body)
        except json.JSONDecodeError as e:
            return self._json_response({'error': f'CDP 响应解析失败: {e}'}, 500)
        except Exception as e:
            return self._json_response({'error': f'CDP 连接失败: {e}', 'diagnostic': str(e)}, 500)

        # 找到目标 tab：优先 zhipin.com，其次第一个有 webSocketDebuggerUrl 的
        target_ws = None
        for tab in tabs:
            url = tab.get('url', '')
            if 'zhipin.com' in url:
                target_ws = self._normalize_ws(tab.get('webSocketDebuggerUrl'))
                break
        if not target_ws:
            for tab in tabs:
                if tab.get('webSocketDebuggerUrl'):
                    target_ws = self._normalize_ws(tab.get('webSocketDebuggerUrl'))
                    break

        if not target_ws:
            return self._json_response({'error': '未找到可用浏览器 tab'}, 500)

        try:
            ws = websocket.create_connection(target_ws, timeout=10)
            msg_id = 1
            ws.send(json.dumps({
                'id': msg_id,
                'method': 'Page.navigate',
                'params': {'url': target_url}
            }))
            # 读取导航响应
            ws.recv()

            # 等待 Page.loadEventFired 事件（最多 10 秒）
            ws.settimeout(10)
            try:
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get('method') == 'Page.loadEventFired':
                        break
            except Exception:
                # 超时或连接关闭，仍视为成功
                pass

            ws.close()
            return self._json_response({'ok': True, 'message': '已导航到列表页'})
        except Exception as e:
            return self._json_response({'error': f'CDP 导航失败: {e}'}, 500)

    def _check_login(self):
        """通过 CDP 检查 Boss 直聘登录状态。"""
        try:
            ok, body = self._cdp_fetch('/json')
            if not ok:
                return self._json_response({'logged_in': False, 'reason': 'CloakBrowser 未启动', 'diagnostic': body})

            tabs = json.loads(body)
            boss_ws = None
            for tab in tabs:
                if 'zhipin.com' in tab.get('url', ''):
                    boss_ws = self._normalize_ws(tab.get('webSocketDebuggerUrl'))
                    break

            if not boss_ws:
                return self._json_response({'logged_in': False, 'reason': '未找到 Boss 直聘页面'})

            try:
                import websocket
            except ImportError:
                return self._json_response({'logged_in': False, 'reason': 'websocket-client 未安装'})

            ws = websocket.create_connection(boss_ws, timeout=10)
            ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {
                'expression': 'document.title'
            }}))
            resp = json.loads(ws.recv())
            ws.close()

            title = resp.get('result', {}).get('result', {}).get('value', '')
            logged_in = '登录' not in title and 'login' not in title.lower()

            return self._json_response({
                'logged_in': logged_in,
                'title': title,
                'reason': None if logged_in else '未登录（页面标题含"登录"）'
            })
        except Exception as e:
            return self._json_response({'logged_in': False, 'reason': str(e), 'diagnostic': str(e)})

    def _agent_priorities(self, dir_: str):
        """合并 AI 判定(CSV) + 手动覆盖(priorities.json) 的优先级。"""
        mapping, _src = self._load_judgment_map(dir_)
        priorities = {jid: m['judgment_level'] for jid, m in mapping.items() if m.get('judgment_level')}
        notes = {jid: m.get('judgment_note', '') for jid, m in mapping.items() if m.get('judgment_note')}
        # 手动覆盖优先（兼容旧格式 string 和新格式 dict）
        manual = self._read_json_raw(dir_, 'priorities.json') or {}
        for jid, val in manual.items():
            if isinstance(val, dict):
                lv = val.get('level', '')
                if lv in ('高', '中', '低'):
                    priorities[jid] = lv
                if val.get('note'):
                    notes[jid] = val['note']
            elif isinstance(val, str) and val in ('高', '中', '低'):
                priorities[jid] = val
        return self._json_response({'priorities': priorities, 'notes': notes})

    def _greet_status(self, dir_: str):
        """读取 03_greet/greet_log.csv 返回打招呼进度。

        兼容新旧两种记录格式：
        - 新格式（boss_auto_greet 现行）：结果列 = 成功/失败，错误信息列存原因
        - 旧格式：status 列 = done/failed
        按 job_id 聚合（同一岗位多条记录取时间最新），返回 byJob 供前端列表标记。
        """
        greet_dir = os.path.join(os.path.dirname(dir_), '03_greet')
        log_path = os.path.join(greet_dir, 'greet_log.csv')
        if not os.path.isfile(log_path):
            return self._json_response({'done': 0, 'failed': 0, 'skipped': 0, 'total': 0, 'running': False, 'byJob': {}})

        by_job = {}  # job_id -> {result: 成功/失败/跳过, error, time}
        try:
            with open(log_path, 'r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    jid = (row.get('job_id') or '').strip()
                    if not jid:
                        continue
                    raw = (row.get('结果') or row.get('status') or '').strip()
                    if raw in ('成功', 'done'):
                        result = '成功'
                    elif '跳过' in raw or '无沟通' in raw or 'SKIP' in raw or raw in ('skip', 'SKIP_NO_BUTTON'):
                        result = '跳过'
                    else:
                        result = '失败'
                    error = (row.get('错误信息') or '').strip()
                    ts = (row.get('时间') or '').strip()
                    # 同一岗位多条记录：时间最新者覆盖（无时间则后写覆盖先写）
                    if jid not in by_job or (ts and ts >= by_job[jid].get('time', '')):
                        by_job[jid] = {'result': result, 'error': error, 'time': ts}
        except Exception:
            pass

        done = sum(1 for v in by_job.values() if v['result'] == '成功')
        failed = sum(1 for v in by_job.values() if v['result'] == '失败')
        skipped = sum(1 for v in by_job.values() if v['result'] == '跳过')
        total = len(by_job)

        running = crawl_manager.status().get('running', False) and crawl_manager.kind == 'greet'
        return self._json_response({
            'done': done, 'failed': failed, 'skipped': skipped, 'total': total,
            'running': running, 'byJob': by_job
        })

    GREETINGS_DIR = os.path.expanduser('~/.agents/skills/boss-auto-greet/greetings')

    def _greeting_files(self):
        """列出可用打招呼语 MD 文件 + 当前来源。"""
        files = []
        if os.path.isdir(self.GREETINGS_DIR):
            for fp in sorted(os.listdir(self.GREETINGS_DIR)):
                if fp.endswith('.md'):
                    files.append(fp)
        # 当前选择（session 级别持久化）
        current = 'default.md'
        return self._json_response({'files': files, 'current': current})

    def _greeting_content(self, qs):
        """读取指定打招呼语 MD 内容。?file=default.md"""
        name = qs.get('file', ['default.md'])[0]
        path = os.path.join(self.GREETINGS_DIR, name)
        if not os.path.isfile(path):
            return self._json_response({'error': '文件不存在'}, 404)
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        return self._json_response({'file': name, 'content': content, 'path': path})

    def _auto_init_priorities(self, dir_: str):
        """如果 priorities.json 不存在或为空，从判定结果（手动导入优先，否则自动匹配）初始化。"""
        pri_path = os.path.join(dir_, 'priorities.json')
        need_init = False
        if not os.path.exists(pri_path):
            need_init = True
        else:
            try:
                with open(pri_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not data:
                    need_init = True
            except Exception:
                need_init = True

        if not need_init:
            return

        mapping, _src = self._load_judgment_map(dir_)
        if not mapping:
            return

        priorities = {}
        for jid, m in mapping.items():
            lv = m.get('judgment_level', '')
            note = m.get('judgment_note', '')
            if lv or note:
                priorities[jid] = {'level': lv, 'note': note}
        if priorities:
            tmp = pri_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(priorities, f, ensure_ascii=False, indent=2)
            os.replace(tmp, pri_path)

    # === 判定结果合并（自动 + 手动导入） ===
    def _load_judgment_map(self, dir_: str):
        """加载判定结果，返回 ({job_id: 判定字段字典}, chosen_csv_name_or_None)。
        来源优先级：1) 用户手动导入（judgment_source.json）；2) 自动匹配 同名*判定结果.csv；3) 任意 *判定结果.csv。"""
        result_dir = os.path.join(os.path.dirname(dir_), '02_result')
        if not os.path.isdir(result_dir):
            return {}, None

        chosen = None
        # 1) 手动导入优先（持久化选择）
        src_path = os.path.join(dir_, 'judgment_source.json')
        if os.path.isfile(src_path):
            try:
                with open(src_path, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                cand = d.get('csv')
                if cand and os.path.isfile(os.path.join(result_dir, cand)):
                    chosen = cand
            except Exception:
                pass

        # 2) 自动匹配：收集「raw 同名判定文件 + 任意判定文件」全部候选，按 mtime 取最新。
        #    B2: 多次判定时（无论同名重写还是不同命名）始终展示最新一次判定。
        if not chosen:
            candidates = []
            raws = glob_mod.glob(os.path.join(dir_, 'jobs_data*.csv'))
            for rc in raws:
                name = os.path.splitext(os.path.basename(rc))[0]
                fp = os.path.join(result_dir, name + '_判定结果.csv')
                if os.path.isfile(fp) and fp not in candidates:
                    candidates.append(fp)
            for fp in glob_mod.glob(os.path.join(result_dir, '*判定结果.csv')):
                if fp not in candidates:
                    candidates.append(fp)
            if candidates:
                candidates.sort(key=os.path.getmtime, reverse=True)
                chosen = os.path.basename(candidates[0])

        if not chosen:
            return {}, None

        level_map = {'low': '低', 'medium': '中', 'high': '高',
                     'Low': '低', 'Medium': '中', 'High': '高'}
        mapping = {}
        try:
            with open(os.path.join(result_dir, chosen), 'r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    jid = (row.get('job_id') or '').strip()
                    if not jid:
                        continue
                    lvl = row.get('优先级判断') or row.get('优先级') or ''
                    mapping[jid] = {
                        'judgment_level': level_map.get(lvl, lvl),
                        'judgment_match': (row.get('匹配度') or '').strip(),
                        'judgment_reason': (row.get('优先级判断理由') or '').strip(),
                        'judgment_scores': (row.get('能力条目得分') or '').strip(),
                        'judgment_signals': (row.get('叠加信号') or '').strip(),
                        'judgment_jd_depth': (row.get('平均JD深度') or '').strip(),
                        'judgment_candidate_depth': (row.get('平均候选人深度') or '').strip(),
                        'judgment_note': (row.get('人工备注') or '').strip(),
                    }
        except Exception:
            return {}, chosen
        return mapping, chosen

    def _read_json_raw(self, dir_: str, filename: str):
        """读取 JSON 文件返回解析后的对象（不发送响应），用于服务端合并。"""
        filepath = os.path.join(dir_, filename)
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    def _serve_jobs_cache(self, dir_: str):
        """返回 jobs_cache.json，合并 AI 判定 + 手动覆盖(priorities.json)。"""
        data = self._read_json_raw(dir_, 'jobs_cache.json')
        if data is None:
            return self._json_response([])
        mapping, _src = self._load_judgment_map(dir_)
        manual = self._read_json_raw(dir_, 'priorities.json') or {}
        if mapping and isinstance(data, list):
            for job in data:
                jid = job.get('job_id')
                if jid in mapping:
                    job.update(mapping[jid])
                val = manual.get(jid)
                if isinstance(val, dict):
                    if val.get('level') in ('高', '中', '低'):
                        job['judgment_level'] = val['level']
                        job['judgment_manual'] = True
                    if val.get('note'):
                        job['judgment_note'] = val['note']
                elif isinstance(val, str) and val in ('高', '中', '低'):
                    job['judgment_level'] = val
                    job['judgment_manual'] = True
        return self._json_response(data)

    def _judgment_prompt(self, dir_: str):
        """生成完整判定提示词：读取 references/judgment-prompt-template.md 并填充占位符。

        数据文件取当前 session 中最新（mtime）的 jobs_data*.csv，
        输出目录固定为同 session 的 02_result/。
        """
        # 最新 raw CSV
        raws = glob_mod.glob(os.path.join(dir_, 'jobs_data*.csv'))
        if not raws:
            return self._json_response({'error': '当前 session 无爬取数据（缺少 jobs_data*.csv）'})
        raw_csv = max(raws, key=os.path.getmtime)
        result_dir = os.path.join(os.path.dirname(dir_), '02_result')
        os.makedirs(result_dir, exist_ok=True)

        template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'references', 'judgment-prompt-template.md')
        if os.path.isfile(template_path):
            with open(template_path, 'r', encoding='utf-8') as f:
                template = f.read()
        else:
            template = (
                "请执行 job-match-priority 优先级判定任务：\n\n"
                "数据文件：{raw_csv}\n输出目录：{result_dir}\n候选人画像：last\n\n"
                "请按照 job-match-priority Skill 的流程执行判定，将结果写入输出目录。")

        prompt = template.replace('{raw_csv}', raw_csv).replace('{result_dir}', result_dir)
        return self._json_response({
            'ok': True,
            'prompt': prompt,
            'raw_csv': raw_csv,
            'result_dir': result_dir,
            'template': os.path.basename(template_path) if os.path.isfile(template_path) else 'builtin',
        })

    def _judgment_files(self, dir_: str):
        """列出 02_result/ 下所有判定 CSV，并标记当前生效来源。"""
        result_dir = os.path.join(os.path.dirname(dir_), '02_result')
        files = []
        if os.path.isdir(result_dir):
            for fp in sorted(os.listdir(result_dir)):
                if fp.endswith('.csv'):
                    files.append(fp)
        # 手动导入优先显示
        current = None
        src_path = os.path.join(dir_, 'judgment_source.json')
        if os.path.isfile(src_path):
            try:
                with open(src_path, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                if d.get('csv'):
                    current = d.get('csv')
            except Exception:
                pass
        if not current:
            _m, current = self._load_judgment_map(dir_)
        return self._json_response({'files': files, 'current': current})

    def _import_judgment(self, dir_: str, data: dict):
        """手动导入指定的判定 CSV（持久化到 judgment_source.json，每次打开自动引用）。"""
        name = (data.get('csv') or '').strip()
        if not name:
            return self._json_response({'error': '缺少 csv 名称'}, 400)
        result_dir = os.path.join(os.path.dirname(dir_), '02_result')
        fp = os.path.join(result_dir, name)
        if not os.path.isfile(fp):
            return self._json_response({'error': '文件不存在: ' + name}, 404)
        # 校验含 job_id 字段，确保可关联
        try:
            with open(fp, 'r', encoding='utf-8-sig') as f:
                header = csv.DictReader(f).fieldnames or []
            if 'job_id' not in header:
                return self._json_response({'error': '该 CSV 无 job_id 字段，无法关联'}, 400)
            # B3: 校验判定必需列，避免合并后无优先级
            pri_col = [c for c in header if c in ('优先级判断', '优先级')]
            if not pri_col:
                return self._json_response({
                    'error': '该 CSV 缺少「优先级判断」列，无法用于打招呼筛选。请检查判定结果文件（必填列：job_id、优先级判断）'
                }, 400)
        except Exception as e:
            return self._json_response({'error': '读取失败: ' + str(e)}, 400)
        # 持久化选择
        src_path = os.path.join(dir_, 'judgment_source.json')
        tmp = src_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'csv': name}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, src_path)
        # 同步刷新 priorities.json（供打招呼筛选使用）
        self._auto_init_priorities(dir_)
        return self._json_response({'ok': True, 'csv': name})

    def _handle_judge(self, dir_: str, mode: str):
        """处理优先级判定请求（增量/全量）。"""
        if not judge_monitor or not judge_monitor.available:
            return self._json_response({
                'ok': False,
                'error': 'matcher 引擎不可用，请检查 job-match-priority 配置'
            }, 500)

        result = judge_monitor.judge_batch(dir_, mode=mode)
        if result.get('ok'):
            # 刷新 priorities.json 和判定来源
            self._auto_init_priorities(dir_)
        return self._json_response(result)

    def _start_greet(self, dir_: str, data: dict):
        """启动打招呼：合并 AI 判定(CSV) + 手动覆盖(priorities.json)，筛选高/中级。"""
        # 合并 AI 判定与手动覆盖（与 _serve_jobs_cache 一致）
        mapping, _src = self._load_judgment_map(dir_)
        if not mapping:
            return self._json_response({'error': '无判定数据，请先执行一键判定'})
        # 读取手动覆盖
        pri_path = os.path.join(dir_, 'priorities.json')
        manual = {}
        if os.path.isfile(pri_path):
            try:
                with open(pri_path, 'r', encoding='utf-8') as f:
                    manual = json.load(f)
            except Exception:
                pass
        # 合并：AI 为基础，手动覆盖优先
        all_pri = {jid: m['judgment_level'] for jid, m in mapping.items() if m.get('judgment_level')}
        for jid, val in manual.items():
            if isinstance(val, dict):
                lv = val.get('level', '')
                if lv in ('高', '中', '低'):
                    all_pri[jid] = lv
            elif isinstance(val, str) and val in ('高', '中', '低'):
                all_pri[jid] = val

        high_mid_ids = [jid for jid, level in all_pri.items() if level in ('高', '中')]
        if not high_mid_ids:
            return self._json_response({'error': '无高/中级岗位（AI判定+手动覆盖均无可打招呼的岗位）'})

        cache_path = os.path.join(dir_, 'jobs_cache.json')
        if not os.path.isfile(cache_path):
            return self._json_response({'error': '无岗位数据'})

        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                all_jobs = json.load(f)
        except Exception:
            return self._json_response({'error': '读取岗位数据失败'})

        job_map = {j['job_id']: j for j in all_jobs if 'job_id' in j}

        greet_dir = os.path.join(os.path.dirname(dir_), '03_greet')
        os.makedirs(greet_dir, exist_ok=True)
        greet_csv = os.path.join(greet_dir, 'greet_targets.csv')

        with open(greet_csv, 'w', encoding='utf-8-sig', newline='') as f:
            # 字段名必须匹配 boss_auto_greet.py 的期望：
            # 优先级判断=high/medium, 岗位链接=完整URL
            writer = csv.DictWriter(f, fieldnames=['job_id', '岗位链接', '职位名称', '公司名称', '优先级判断'])
            writer.writeheader()
            for jid in high_mid_ids:
                job = job_map.get(jid, {})
                level_en = {'高': 'high', '中': 'medium', '低': 'low'}.get(all_pri.get(jid, ''), '')
                writer.writerow({
                    'job_id': jid,
                    '岗位链接': job.get('job_url', ''),
                    '职位名称': job.get('职位名称', ''),
                    '公司名称': job.get('公司名称', ''),
                    '优先级判断': level_en,
                })

        # 读取打招呼语：前端传的优先，否则从默认 MD 文件
        message = data.get('message', '')
        if not message:
            default_md = os.path.expanduser('~/.agents/skills/boss-auto-greet/greetings/default.md')
            if os.path.isfile(default_md):
                with open(default_md, 'r', encoding='utf-8') as f:
                    message = f.read().strip()

        result = crawl_manager.start_greet({
            'csv_path': greet_csv,
            'message': message,
            'max_greet': data.get('max_greet', 0),
            'first_one': bool(data.get('first_one')),
        })
        result['total'] = len(high_mid_ids)
        return self._json_response(result)

    def _json_response(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filepath, content_type=None, cache=True):
        if not os.path.isfile(filepath):
            self.send_error(404)
            return
        ext = os.path.splitext(filepath)[1].lower()
        types = {'.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript',
                 '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml'}
        ct = content_type or types.get(ext, 'application/octet-stream')
        with open(filepath, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ct)
        if not cache:
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('Pragma', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


def _derive_history_root(output_dir: str) -> str:
    """从 01_raw/ 目录向上推两级得到 history_root（如 boss直聘一体化/）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(output_dir)))
    if os.path.isdir(root):
        return root
    return None


def start_dashboard_server(output_dir: str, port: int = 8765, background: bool = True,
                           history_root: str = None) -> str:
    """
    启动 Dashboard 服务器。

    参数：
        output_dir: 爬取输出目录（含 progress.json / jobs_cache.json / jobs_data*.csv）
        port: 监听端口（默认 8765）
        background: 是否后台运行（默认 True）
        history_root: 历史 session 根目录。不传则从 output_dir 自动推导。

    返回：
        服务器 URL，如 "http://localhost:8765"
    """
    DashboardHandler.output_dir = os.path.abspath(output_dir)
    DashboardHandler.history_root = history_root or _derive_history_root(output_dir)

    server = http.server.HTTPServer(('127.0.0.1', port), DashboardHandler)
    server.allow_reuse_address = True

    if background:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    else:
        server.serve_forever()

    url = f"http://localhost:{port}"
    print(f"🌐 Dashboard 已启动: {url}")
    print(f"   数据目录: {DashboardHandler.output_dir}")
    if DashboardHandler.history_root:
        print(f"   历史目录: {DashboardHandler.history_root}")
    return url


# === CLI ===
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Boss 直聘爬取 Dashboard 服务器")
    parser.add_argument("--output-dir", required=True, help="爬取输出目录")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--history-root", default=None, help="历史 session 根目录（默认自动推导）")
    args = parser.parse_args()
    print(f"启动 Dashboard 服务器...")
    print(f"数据目录: {args.output_dir}")
    start_dashboard_server(args.output_dir, args.port, background=False, history_root=args.history_root)
