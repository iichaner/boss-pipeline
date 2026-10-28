#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""env_check.py —— R1.1 六项环境自检（P4）。

设计依据：DESIGN.md D.3「env_check.py 的六项与复用来源」。

输出结构（CLI 与 Dashboard 共用）::

    {
      "checks": [{"id", "label", "ok", "detail", "fix_cmd", "blocking"}],
      "summary": {"ok", "total", "passed", "failed", "blocking_failed",
                  "home", "work_root", "python"}
    }

原则
----
* 只读：不启动浏览器、不创建目录、不改配置。
* 失败只提示，不抛 traceback；CLI 退出码恒为 0（避免 launcher 被自检打断）。
* CDP / 登录检查与 serve_dashboard 同源（127.0.0.1:9222 + /json + Runtime.evaluate），
  但**不在导入期** import serve_dashboard，避免 onboarding_api 循环导入。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config_paths as cp  # noqa: E402


# ── 小工具 ────────────────────────────────────────────────
def _check(cid, label, ok, detail, fix_cmd='', blocking=False):
    return {
        'id': cid,
        'label': label,
        'ok': bool(ok),
        'detail': str(detail or ''),
        'fix_cmd': str(fix_cmd or ''),
        'blocking': bool(blocking),
    }


def _cdp_fetch(path: str = '/json/version'):
    """与 serve_dashboard.DashboardHandler._cdp_fetch 同源：直连 127.0.0.1。"""
    try:
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', 9222, timeout=5)
        conn.request('GET', path)
        resp = conn.getresponse()
        body = resp.read().decode('utf-8')
        conn.close()
        return True, body
    except Exception as e:
        return False, str(e)


# ── 六项检查 ──────────────────────────────────────────────
def check_python():
    py = sys.version_info
    ver = f'{py.major}.{py.minor}.{py.micro}'
    missing = []
    for mod, pkg in (('yaml', 'pyyaml'), ('requests', 'requests'), ('websocket', 'websocket-client')):
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    if py < (3, 10):
        return _check('python', 'Python 版本与依赖', False,
                      f'Python {ver} 低于 3.10', 'brew install python@3.12  # 或安装 3.10+', True)
    if missing:
        return _check('python', 'Python 版本与依赖', False,
                      f'Python {ver}；缺少：{", ".join(missing)}',
                      f'{sys.executable} -m pip install ' + ' '.join(missing), True)
    return _check('python', 'Python 版本与依赖', True, f'Python {ver}；pyyaml / requests / websocket-client 已安装')


def check_browser():
    try:
        binary = cp.browser_binary()
    except Exception as e:
        return _check('browser', 'CloakBrowser 可执行文件', False, str(e),
                      'export BOSS_BROWSER_BIN=/path/to/Chromium')
    if binary.is_file():
        return _check('browser', 'CloakBrowser 可执行文件', True, str(binary))
    return _check('browser', 'CloakBrowser 可执行文件', False,
                  f'未找到：{binary}',
                  '安装 CloakBrowser，或 export BOSS_BROWSER_BIN=<Chromium 路径>')


def check_cdp():
    ok, body = _cdp_fetch('/json/version')
    if not ok:
        return _check('cdp', 'CDP 调试端口 9222', False,
                      f'连不上 127.0.0.1:9222（{body[:120]}）',
                      '在 Dashboard 点「🚀 启动浏览器」，或检查是否已有浏览器占用 9222')
    if not body.strip():
        return _check('cdp', 'CDP 调试端口 9222', False, 'CDP 返回空响应',
                      '重启浏览器：关闭后点「🚀 启动浏览器」')
    try:
        info = json.loads(body)
        ver = info.get('Browser') or info.get('webSocketDebuggerUrl') or 'ok'
    except Exception:
        ver = 'ok'
    return _check('cdp', 'CDP 调试端口 9222', True, f'已连通（{ver}）')


def check_login():
    ok, body = _cdp_fetch('/json')
    if not ok:
        return _check('login', 'Boss 直聘登录状态', False,
                      '浏览器未启动，无法检查登录',
                      '先点「🚀 启动浏览器」，再扫码登录 zhipin.com')
    try:
        tabs = json.loads(body)
    except Exception:
        return _check('login', 'Boss 直聘登录状态', False, 'CDP 返回非 JSON',
                      '重启浏览器后重试')
    ws_url = None
    for tab in tabs or []:
        if 'zhipin.com' in (tab.get('url') or ''):
            ws_url = tab.get('webSocketDebuggerUrl') or ''
            break
    if not ws_url:
        return _check('login', 'Boss 直聘登录状态', False,
                      '未打开 Boss 直聘页面',
                      '在浏览器里打开 https://www.zhipin.com 并扫码登录')
    try:
        import websocket  # websocket-client
    except Exception:
        return _check('login', 'Boss 直聘登录状态', False,
                      'websocket-client 未安装，无法读取页面标题',
                      f'{sys.executable} -m pip install websocket-client')
    try:
        ws = websocket.create_connection(ws_url.replace('localhost', '127.0.0.1'),
                                         timeout=8)
        ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                            'params': {'expression': 'document.title'}}))
        resp = json.loads(ws.recv())
        ws.close()
        title = ((resp.get('result') or {}).get('result') or {}).get('value') or ''
    except Exception as e:
        return _check('login', 'Boss 直聘登录状态', False,
                      f'读取页面标题失败：{e}',
                      '确认浏览器窗口正常后重试；或重新登录 zhipin.com')
    logged_in = bool(title) and '登录' not in title and 'login' not in title.lower()
    if logged_in:
        return _check('login', 'Boss 直聘登录状态', True, f'已登录（页面标题：{title[:30]}）')
    return _check('login', 'Boss 直聘登录状态', False,
                  f'未登录（页面标题：{title[:30] or "空"}）',
                  '在浏览器窗口里扫码登录 Boss 直聘')


def home_fingerprint() -> str:
    """与 serve_dashboard._home_fingerprint / 启动器同算法：sha256(realpath(home))[:16]。

    只用路径算，不含画像/姓名/薪资等任何个人数据。
    """
    try:
        p = os.environ.get('BOSS_PIPELINE_HOME') or str(cp.DEFAULT_HOME)
        real = os.path.realpath(os.path.expanduser(p))
        return hashlib.sha256(real.encode('utf-8')).hexdigest()[:16]
    except Exception:
        return ''


def _health_fingerprint(port: int) -> str:
    """读运行中服务的 /api/health 指纹；不是本服务/读不到返回空串。"""
    try:
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        conn.request('GET', '/api/health')
        resp = conn.getresponse()
        body = resp.read().decode('utf-8')
        conn.close()
        data = json.loads(body)
        if isinstance(data, dict) and data.get('service') == 'boss-pipeline-dashboard':
            return str(data.get('home_fingerprint') or '')
    except Exception:
        pass
    return ''


def check_port(port: int = 8765):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.bind(('127.0.0.1', port))
        s.close()
        return _check('port', f'端口 {port}', True, f'空闲（Dashboard 尚未启动时可 bind）')
    except OSError:
        s.close()

    # P1-1：端口被占不等于「是本 Dashboard」——必须比对 home 指纹。
    local_fp = home_fingerprint()
    remote_fp = _health_fingerprint(port)
    if remote_fp and local_fp and remote_fp == local_fp:
        return _check('port', f'端口 {port}', True,
                      f'已被本 Dashboard 占用（home 指纹一致 {local_fp}），'
                      f'直接打开 http://localhost:{port} 即可')
    if remote_fp:
        return _check('port', f'端口 {port}', False,
                      f'被另一套数据占用（本地指纹 {local_fp or "未知"} / '
                      f'远端 {remote_fp}），不是本 Dashboard',
                      f'双击启动器会自动换端口（8765–8775）；'
                      f'想固定用本套数据：先关掉占用 {port} 的实例')

    detail = '已被占用（不是 boss-pipeline Dashboard）'
    try:
        out = subprocess.run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN'],
                             capture_output=True, text=True, timeout=5).stdout
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 2:
                detail = f'已被 {parts[0]} (pid {parts[1]}) 占用（不是本 Dashboard）'
    except Exception:
        pass
    return _check('port', f'端口 {port}', False, detail,
                  f'lsof -nP -iTCP:{port} -sTCP:LISTEN  # 找到后关闭占用进程')


def check_config():
    try:
        home = cp.home()
    except Exception as e:
        home = None
        detail = str(e)
    else:
        detail = '未初始化用户数据目录' if home is None else ''
    if home is None:
        # N6：同一项只给一条建议（指向向导），不再同时给 unset/mkdir 两条互相矛盾的路。
        first = (detail or '未找到 ~/.boss-pipeline').splitlines()[0]
        return _check('config', '配置就绪（画像 + 偏好）', False, first,
                      'python3 scripts/onboard.py  # 走向导（会创建用户数据目录）')
    try:
        profile = cp.load_profile()
        prefs = cp.load_prefs()
        n_dims = len((profile.get('capabilities') or {}).get('dimensions') or {})
        n_tax = len(((prefs.get('rules') or {}).get('taxonomy')) or [])
        return _check('config', '配置就绪（画像 + 偏好）', True,
                      f'profile={profile.get("id")}；维度自评 {n_dims} 个；taxonomy {n_tax} 项')
    except cp.ProfileNotPublished as e:
        return _check('config', '配置就绪（画像 + 偏好）', False, str(e),
                      'python3 scripts/onboard.py  # 或 migrate_profile.py --profile <id>')
    except cp.NeedOnboarding as e:
        return _check('config', '配置就绪（画像 + 偏好）', False,
                      str(e).splitlines()[0],
                      'python3 scripts/onboard.py  # 走向导创建画像')
    except Exception as e:
        return _check('config', '配置就绪（画像 + 偏好）', False,
                      str(e).splitlines()[0],
                      'python3 scripts/onboard.py  # 重新生成画像')


def run_checks() -> dict:
    checks = [check_python(), check_browser(), check_cdp(), check_login(),
              check_port(), check_config()]
    passed = sum(1 for c in checks if c['ok'])
    blocking_failed = [c for c in checks if c['blocking'] and not c['ok']]
    try:
        home = str(cp.home() or '')
    except Exception:
        home = ''
    try:
        work_root = str(cp.work_root())
    except Exception:
        work_root = ''
    return {
        'checks': checks,
        'summary': {
            'ok': not blocking_failed,
            'total': len(checks),
            'passed': passed,
            'failed': len(checks) - passed,
            'blocking_failed': len(blocking_failed),
            'home': home,
            'work_root': work_root,
            'python': sys.executable,
        },
    }


def format_text(report: dict) -> str:
    lines = []
    for c in report['checks']:
        mark = '✅' if c['ok'] else ('❌' if c['blocking'] else '⚠️')
        lines.append(f'{mark} {c["label"]}：{c["detail"]}')
        if not c['ok'] and c['fix_cmd']:
            lines.append(f'   ↳ 修复：{c["fix_cmd"]}')
    s = report['summary']
    lines.append(f'汇总：{s["passed"]}/{s["total"]} 项通过'
                 + ('（有阻塞项）' if s['blocking_failed'] else ''))
    return '\n'.join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='boss-pipeline 环境自检（R1.1 六项）')
    ap.add_argument('--json', action='store_true', help='JSON 输出')
    args = ap.parse_args(argv)
    report = run_checks()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_text(report))
    return 0


if __name__ == '__main__':
    sys.exit(main())
