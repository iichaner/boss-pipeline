#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""config_paths.py — boss-pipeline 统一配置路径解析（P2）。

目标：把「个人数据（画像/偏好）」与「代码/skill 目录」解耦。
设计依据：DESIGN.md B.2（解析优先级）、B.5（迁移期兼容矩阵）、A.7（effective_config 合并顺序）。

解析优先级
----------
home：
  1. `--home <path>`（仅 CLI 脚本接受，通过 cli_home 参数传入）
  2. 环境变量 `BOSS_PIPELINE_HOME`
  3. `~/.boss-pipeline`（存在即可用；本模块**不自动创建**）
  4. 兼容回退：`<job-match-priority>/config/`（只读，打一次 legacy 警告）
  5. 全失败 → ConfigError（消息含已尝试路径 + 一条修复命令，不含 traceback）

profile id：
  `--profile-id` > `config.json.active_profile` > `state/last_used_at` 最新 >
  NeedOnboarding（Dashboard 借此跳向导，CLI 打印迁移命令）

skill 目录：
  `BOSS_PIPELINE_SKILLS_ROOT` > `__file__` 向上逐级查找同级目录 > `~/.agents/skills`（遗留兜底）

本文件是全项目**唯一**允许出现 `~/.agents/skills` 字面量的位置（见 skill_dir）。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

# ── 环境变量名 ────────────────────────────────────────────
ENV_HOME = 'BOSS_PIPELINE_HOME'
ENV_SKILLS_ROOT = 'BOSS_PIPELINE_SKILLS_ROOT'
ENV_WORK_ROOT = 'BOSS_PIPELINE_WORK_ROOT'
ENV_BROWSER_BIN = 'BOSS_BROWSER_BIN'
ENV_BROWSER_PROFILE = 'BOSS_BROWSER_PROFILE_DIR'

DEFAULT_HOME = Path('~/.boss-pipeline')
CONFIG_NAME = 'config.json'
PROFILE_SCHEMA_VERSION = 2

#: 统一的「已发布」标记。迁移（migrate）与向导（onboarding）都写这一个文件；
#: 读路径同时兼容 P2 时期的 migration.json（只读回退，不产生副作用）。
PUBLISHED_NAME = 'published.json'

SKILL_PIPELINE = 'boss-pipeline'
SKILL_MATCHER = 'job-match-priority'
SKILL_CRAWLER = 'boss-resume-crawler'
SKILL_GREET = 'boss-auto-greet'

_PROFILE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
_LEGACY_WARNED = False


# ── 异常 ──────────────────────────────────────────────────
class ConfigError(RuntimeError):
    """配置/路径解析失败（可读消息，不含 traceback）。"""


class NeedOnboarding(RuntimeError):
    """没有任何可用画像，需要跑向导或迁移。"""


class ProfileNotPublished(RuntimeError):
    """profile.json 存在但没有 migration.json 提交标记（publish 中断）。"""


# ── 基础工具 ──────────────────────────────────────────────
def _yaml():
    try:
        import yaml
    except ImportError:  # pragma: no cover - 依赖缺失时给可执行提示
        raise ConfigError('缺少依赖 PyYAML：pip3 install pyyaml')
    return yaml


def _warn_legacy(msg: str) -> None:
    global _LEGACY_WARNED
    if not _LEGACY_WARNED:
        _LEGACY_WARNED = True
        print(f'[legacy] {msg}（运行 migrate_profile.py 可消除本提示）', file=sys.stderr)


def _expand(p) -> Path:
    return Path(os.path.expanduser(str(p)))


def read_json(path) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def read_yaml(path) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return _yaml().safe_load(f) or {}


def atomic_write(path, text: str, mode: int = 0o600) -> None:
    """tmp + os.replace + chmod。用于 profile.json / prefs.yaml / config.json。"""
    path = _expand(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f'.{path.name}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


@contextlib.contextmanager
def locked(path, timeout: float = 5.0):
    """跨进程写者锁（fcntl.flock）。超时给可读错误，不抛 traceback。"""
    import fcntl

    path = _expand(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, 'a+')
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise ConfigError(
                        f'配置被其他进程占用（{timeout:.0f}s 超时）：{path}\n'
                        '请关闭另一个 Dashboard / 向导窗口后重试'
                    )
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


# ── home ──────────────────────────────────────────────────
def home(create: bool = False, cli_home=None):
    """返回 boss-pipeline 用户数据根目录；未初始化时返回 None（home 模式下）。"""
    tried = []

    if cli_home:
        p = _expand(cli_home)
        if p.is_dir():
            return p
        if create:
            p.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(p, 0o700)
            return p
        raise ConfigError(
            f'--home 指定的目录不存在：{p}\n'
            f'已尝试：{p}\n修复：mkdir -p "{p}"'
        )

    env = os.environ.get(ENV_HOME)
    if env:
        p = _expand(env)
        if p.is_dir():
            return p
        if create:
            # 首启向导可显式创建；显式指定仍不静默回退（防止「以为在改 A，实际改 B」）
            p.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(p, 0o700)
            return p
        raise ConfigError(
            f'{ENV_HOME} 指向的目录不存在：{p}\n'
            f'已尝试：{p}\n'
            f'修复：unset {ENV_HOME}  或  mkdir -p "{p}"'
        )

    p = _expand(DEFAULT_HOME)
    tried.append(str(p))
    if p.is_dir():
        return p

    if create:
        p.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(p, 0o700)
        return p

    return None


def require_home(create: bool = False) -> Path:
    p = home(create=create)
    if p is not None:
        return p
    raise ConfigError(
        '未找到 boss-pipeline 用户数据目录。\n'
        f'已尝试：{_expand(DEFAULT_HOME)}\n'
        '修复：python3 scripts/migrate_profile.py --profile <id> --verify   '
        '（或 python3 scripts/onboard.py 走向导）'
    )


def is_initialized() -> bool:
    return home() is not None


# ── config.json ───────────────────────────────────────────
def config_path() -> Path:
    p = home()
    if p is None:
        return _expand(DEFAULT_HOME) / CONFIG_NAME
    return p / CONFIG_NAME


def load_config() -> dict:
    p = config_path()
    if not p.is_file():
        return {}
    try:
        return read_json(p)
    except Exception:
        return {}


def save_config(cfg: dict, merge: bool = True) -> None:
    """原子写 config.json；merge=True 时保留调用方未提及的既有键。"""
    root = require_home(create=True)
    cur = load_config() if merge else {}
    merged = _deep_merge(cur, cfg or {})
    text = json.dumps(merged, ensure_ascii=False, indent=2) + '\n'
    with locked(root / 'state' / 'locks' / 'profile.lock'):
        atomic_write(root / CONFIG_NAME, text, mode=0o600)


def _deep_merge(base: dict, override: dict) -> dict:
    """深合并：dict 递归；标量/数组后者整体替换（数组按设计视为整体覆盖）。"""
    out = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ── 目录 ──────────────────────────────────────────────────
def profiles_dir(create: bool = False) -> Path:
    root = require_home(create=create)
    d = root / 'profiles'
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def profile_dir(profile_id=None, create: bool = False) -> Path:
    pid = _norm_id(profile_id) if profile_id else resolve_profile_id()
    d = profiles_dir(create=create) / pid
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def state_dir(profile_id=None, create: bool = False) -> Path:
    d = profile_dir(profile_id, create=create) / 'state'
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def onboarding_state_path(profile_id=None) -> Path:
    return state_dir(profile_id) / 'onboarding_state.json'


def backup_root(profile_id=None) -> Path:
    return profile_dir(profile_id) / 'backup'


# ── 画像文件 ──────────────────────────────────────────────
def _norm_id(pid: str) -> str:
    pid = str(pid or '').strip()
    if not _PROFILE_ID_RE.match(pid):
        raise ConfigError(
            f'非法画像 id：{pid!r}（要求 ^[a-z0-9][a-z0-9_-]{{0,31}}$）'
        )
    return pid


def resolve_profile_id(explicit=None) -> str:
    """--profile-id > config.json.active_profile > last_used_at 最新 > NeedOnboarding。"""
    if explicit:
        pid = _norm_id(explicit)
        if not (profiles_dir() / pid).is_dir():
            raise ConfigError(
                f'画像目录不存在：{profiles_dir() / pid}\n'
                f'修复：python3 scripts/migrate_profile.py --profile {pid}'
            )
        return pid

    root = home()
    if root is None:
        raise NeedOnboarding('未初始化 boss-pipeline，请先运行 migrate_profile.py 或 onboard.py')
    profiles = root / 'profiles'
    if not profiles.is_dir():
        raise NeedOnboarding('没有配置任何画像，请运行 python3 scripts/onboard.py')

    cfg = load_config()
    active = cfg.get('active_profile')
    if isinstance(active, str) and (profiles / active).is_dir():
        return active

    candidates = [d for d in profiles.iterdir() if d.is_dir() and not d.name.startswith('.')]
    if not candidates:
        raise NeedOnboarding('没有配置任何画像，请运行 python3 scripts/onboard.py')

    def _last_used(d: Path) -> float:
        f = d / 'state' / 'last_used_at'
        try:
            return f.stat().st_mtime
        except OSError:
            try:
                return (d / 'profile.json').stat().st_mtime
            except OSError:
                return 0.0

    candidates.sort(key=_last_used, reverse=True)
    pid = candidates[0].name
    # B.5：有 profiles/<id> 但缺 config.json → 自动补（active=pid），无感
    if not (root / CONFIG_NAME).is_file():
        with contextlib.suppress(Exception):
            atomic_write(
                root / CONFIG_NAME,
                json.dumps({'schema_version': 1, 'active_profile': pid,
                            'work_root': str(root / 'work'),
                            'dashboard': {'port': 8765},
                            'onboarding': {'completed': True}},
                           ensure_ascii=False, indent=2) + '\n',
                mode=0o600,
            )
    return pid


def require_profile(profile_id=None) -> str:
    try:
        return resolve_profile_id(profile_id)
    except NeedOnboarding:
        raise


def touch_last_used(profile_id=None) -> None:
    try:
        pid = resolve_profile_id(profile_id)
        d = state_dir(pid, create=True)
        (d / 'last_used_at').write_text(_now_iso(), encoding='utf-8')
    except Exception:
        pass


def published_path(profile_id=None) -> Path:
    """统一发布标记 published.json 的路径（home 模式）。"""
    root = home()
    if root is None:
        raise NeedOnboarding('未初始化 boss-pipeline，请先运行 onboard.py 或 migrate_profile.py')
    return root / 'profiles' / resolve_profile_id(profile_id) / PUBLISHED_NAME


def is_published(profile_id=None) -> bool:
    """画像是否已发布：published.json 为准；兼容 P2 的 migration.json 回退。"""
    try:
        d = published_path(profile_id).parent
    except Exception:
        return False
    return (d / PUBLISHED_NAME).is_file() or (d / 'migration.json').is_file()


def write_published(profile_id=None, source: str = 'onboarding', published_at: str = None) -> Path:
    """写统一发布标记。source = migrate | onboarding。"""
    if source not in ('migrate', 'onboarding'):
        raise ConfigError(f'非法发布来源：{source!r}')
    pid = resolve_profile_id(profile_id)
    p = profiles_dir() / pid / PUBLISHED_NAME
    text = json.dumps({
        'schema_version': 1,
        'profile_id': pid,
        'published_at': published_at or _now_iso(),
        'source': source,
    }, ensure_ascii=False, indent=2) + '\n'
    atomic_write(p, text, mode=0o600)
    return p


def profile_path(profile_id=None, require_published: bool = True) -> Path:
    """home 模式 → profiles/<id>/profile.json；legacy 模式 → skill 内旧画像。"""
    root = home()
    if root is None:
        return _legacy_profile_path()
    pid = resolve_profile_id(profile_id)
    d = root / 'profiles' / pid
    p = d / 'profile.json'
    if not p.is_file():
        raise NeedOnboarding(
            f'画像 {pid} 尚未生成 profile.json，请运行 '
            f'python3 scripts/migrate_profile.py --profile {pid}'
        )
    if require_published:
        # 统一标记：published.json（迁移与向导都写）；migration.json 仅作 P2 兼容回退
        if not (d / PUBLISHED_NAME).is_file() and not (d / 'migration.json').is_file():
            raise ProfileNotPublished(
                f'画像 {pid} 未完成发布（缺少 published.json），请重跑 '
                f'python3 scripts/migrate_profile.py --profile {pid} 或重新走向导'
            )
    return p


def prefs_path(profile_id=None) -> Path:
    root = home()
    if root is None:
        return legacy_config_dir() / 'match_config.yaml'
    pid = resolve_profile_id(profile_id)
    p = root / 'profiles' / pid / 'prefs.yaml'
    if not p.is_file():
        raise NeedOnboarding(
            f'画像 {pid} 缺少 prefs.yaml，请运行 '
            f'python3 scripts/migrate_profile.py --profile {pid}'
        )
    return p


def taxonomy_path(profile_id=None) -> Path:
    """K16：语义扩展用的 taxonomy.yaml（仅 --light-embed / --api-embed 读取）。"""
    root = home()
    if root is None:
        return legacy_config_dir() / 'taxonomy.yaml'
    return root / 'profiles' / _or_default_id(profile_id) / 'taxonomy.yaml'


def _or_default_id(profile_id=None) -> str:
    if profile_id:
        return _norm_id(profile_id)
    try:
        return resolve_profile_id()
    except Exception:
        return 'default'


# ── legacy 回退 ───────────────────────────────────────────
def legacy_config_dir() -> Path:
    """skill 内的旧配置目录（只读）。"""
    return skill_dir(SKILL_MATCHER) / 'config'


def legacy_available() -> bool:
    try:
        d = legacy_config_dir()
    except ConfigError:
        return False
    return (d / 'candidate_profile.json').is_file()


def _legacy_profile_path() -> Path:
    p = legacy_config_dir() / 'candidate_profile.json'
    if not p.is_file():
        raise NeedOnboarding(
            '未找到任何画像配置。\n'
            f'已尝试：{_expand(DEFAULT_HOME)}、{legacy_config_dir()}\n'
            '修复：python3 scripts/migrate_profile.py --profile <id> --verify'
        )
    _warn_legacy(f'使用 skill 内配置 {p}')
    return p


# ── skill 目录定位（B.2 第三条链）────────────────────────
def _skills_root_candidates() -> list:
    out = []
    env = os.environ.get(ENV_SKILLS_ROOT)
    if env:
        out.append(_expand(env))
    for parent in Path(__file__).resolve().parents:
        out.append(parent)
    # ↓ 遗留兜底：全项目唯一一处 `~/.agents/skills` 字面量
    out.append(Path(os.path.expanduser('~/.agents/skills')))
    seen, uniq = set(), []
    for p in out:
        k = str(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def skill_dir(name: str, required: bool = True) -> Path:
    """定位四个 skill 中任意一个的目录（支持任意安装位置）。"""
    if not re.match(r'^[A-Za-z0-9._-]+$', str(name or '')):
        raise ConfigError(f'非法 skill 名：{name!r}')
    tried = []
    for root in _skills_root_candidates():
        cand = root / name
        tried.append(str(cand))
        if cand.is_dir():
            return cand
    if not required:
        return None
    raise ConfigError(
        f'未找到 skill 目录：{name}\n'
        '已尝试：\n  ' + '\n  '.join(tried) + '\n'
        f'修复：export {ENV_SKILLS_ROOT}=<四个 skill 的父目录>'
    )


def skill_script(skill: str, rel: str) -> Path:
    rel = str(rel or '')
    if rel.startswith('/') or '..' in Path(rel).parts:
        raise ConfigError(f'非法相对路径：{rel!r}')
    return skill_dir(skill) / rel


def matcher_scripts_dir() -> Path:
    return skill_dir(SKILL_MATCHER) / 'scripts'


def crawler_script() -> Path:
    return skill_script(SKILL_CRAWLER, 'scripts/boss_extract_cdp.py')


def greet_script() -> Path:
    return skill_script(SKILL_GREET, 'scripts/boss_auto_greet.py')


def greetings_dir() -> Path:
    return skill_dir(SKILL_GREET) / 'greetings'


# ── 浏览器 ────────────────────────────────────────────────
def browser_binary() -> Path:
    env = os.environ.get(ENV_BROWSER_BIN)
    if env:
        return _expand(env)
    return _expand('~/.cache/cloakbrowser/Chromium.app/Contents/MacOS/Chromium')


def browser_profile() -> Path:
    env = os.environ.get(ENV_BROWSER_PROFILE)
    if env:
        return _expand(env)
    return _expand('~/.cache/cloakbrowser/profile-boss')


# ── work / tests ──────────────────────────────────────────
def work_root() -> Path:
    env = os.environ.get(ENV_WORK_ROOT)
    if env:
        return _expand(env)
    cfg = load_config()
    wr = cfg.get('work_root')
    if isinstance(wr, str) and wr.strip():
        return _expand(wr)
    root = home() or _expand(DEFAULT_HOME)
    return root / 'work'


def tests_dir() -> Path:
    return (home() or _expand(DEFAULT_HOME)) / 'tests'


def fixtures_dir() -> Path:
    return tests_dir() / 'fixtures'


def baseline_dir() -> Path:
    return tests_dir() / 'baseline'


def logs_dir(create: bool = False) -> Path:
    d = (home() or _expand(DEFAULT_HOME)) / 'logs'
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


# ── 读取画像 / 偏好 / 生效配置 ───────────────────────────
def load_profile(profile_id=None) -> dict:
    """返回画像 dict。v2 原样返回；legacy 返回旧形状（调用方用 profile_adapter）。"""
    p = profile_path(profile_id)
    data = read_json(p)
    if isinstance(data, dict) and data.get('schema_version') == PROFILE_SCHEMA_VERSION:
        _validate_v2_light(data, p)
    elif isinstance(data, dict) and 'meta' in data:
        _warn_legacy(f'画像仍为 v1 结构：{p}')
    else:
        raise ConfigError(f'无法识别的画像格式：{p}（缺少 schema_version/meta）')
    return data


def _validate_v2_light(data: dict, path) -> None:
    """轻量结构校验（完整 schema 校验由 P3 的 validate_profile.py 负责）。"""
    missing = [k for k in ('id', 'identity', 'targets', 'capabilities') if k not in data]
    if missing:
        raise ConfigError(f'画像缺少字段 {missing}：{path}')
    if not (data.get('targets') or {}).get('roles'):
        raise ConfigError(f'画像 targets.roles 为空：{path}')
    if not (data.get('capabilities') or {}).get('dimensions'):
        raise ConfigError(f'画像 capabilities.dimensions 为空：{path}')


def load_prefs(profile_id=None) -> dict:
    p = prefs_path(profile_id)
    data = read_yaml(p)
    if not isinstance(data, dict):
        raise ConfigError(f'prefs 格式错误：{p}')
    return data


def effective_config(profile_id=None) -> dict:
    """A.7 合并顺序：engine_defaults → generic → prefs.engine → prefs.rules。

    产出仍是**旧键名**（scoring / depth_coefficient / taxonomy / hard_filters / ...），
    因此 Matcher(profile, config) 的入参形状不变（D-1）。
    legacy 模式下直接返回旧 match_config.yaml。
    """
    root = home()
    if root is None:
        p = legacy_config_dir() / 'match_config.yaml'
        if not p.is_file():
            raise ConfigError(f'旧配置缺失：{p}')
        _warn_legacy(f'使用 skill 内配置 {p}')
        return read_yaml(p)

    prefs = load_prefs(profile_id)
    engine = prefs.get('engine') or {}
    rules = prefs.get('rules') or {}
    if not isinstance(rules, dict) or not rules:
        raise ConfigError('prefs.yaml 缺少 rules 子树')

    merged = {}
    for layer in _default_layers(prefs):
        merged = _deep_merge(merged, layer)
    if isinstance(engine, dict) and engine:
        merged = _deep_merge(merged, engine)
    merged = _deep_merge(merged, rules)
    return merged


def _default_layers(prefs: dict) -> list:
    """出厂默认层（P3 才落地；缺失时跳过，ii 的 prefs 是完整快照）。"""
    layers = []
    try:
        tpl = skill_dir(SKILL_MATCHER) / 'templates'
    except ConfigError:
        return layers
    p = tpl / 'engine_defaults.yaml'
    if p.is_file():
        layers.append(_flatten_layer(read_yaml(p)))
    fam = prefs.get('extends') or 'generic'
    if re.match(r'^[a-z0-9][a-z0-9_-]{0,31}$', str(fam)):
        p = tpl / 'families' / f'{fam}.yaml'
        if p.is_file():
            layers.append(_flatten_layer(read_yaml(p)))
    return layers


def _flatten_layer(d: dict) -> dict:
    """把 {engine:…, rules:…} 摊平成旧键名；已是扁平结构则原样返回。"""
    if not isinstance(d, dict):
        return {}
    out = {}
    for k in ('engine', 'rules'):
        v = d.get(k)
        if isinstance(v, dict):
            out = _deep_merge(out, v)
    for k, v in d.items():
        if k in ('engine', 'rules', 'meta'):
            continue
        if k not in out:
            out[k] = v
    return out


# ── 兼容旧调用 ────────────────────────────────────────────
def to_legacy_profile(profile: dict) -> dict:
    """转发到 profile_adapter（延迟导入，避免循环依赖）。"""
    import profile_adapter
    return profile_adapter.to_legacy_profile(profile)


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec='seconds')


if __name__ == '__main__':  # 手工排查用：python3 config_paths.py
    def _show():
        print('home        :', home())
        print('initialized :', is_initialized())
        print('work_root   :', work_root())
        for name in (SKILL_PIPELINE, SKILL_MATCHER, SKILL_CRAWLER, SKILL_GREET):
            try:
                print(f'skill[{name}]:', skill_dir(name))
            except ConfigError as e:
                print(f'skill[{name}]: <缺失> {e}')
        try:
            print('profile_id  :', resolve_profile_id())
        except Exception as e:
            print('profile_id  : <无>', e)
    _show()
