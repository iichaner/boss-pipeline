#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_profile.py — 把旧画像/旧判定配置迁移到 `~/.boss-pipeline/profiles/<id>/`（P2）。

设计依据：DESIGN.md C.1（幂等状态机）、C.2（回滚）、A.7（I1/I2 断言）。

状态机
------
detect → backup → transform → write(tmp) → validate(I1) → publish(atomic) → verify(I2) → mark

用法
----
    python3 scripts/migrate_profile.py --profile ii --verify
    python3 scripts/migrate_profile.py --profile ii --rollback
    python3 scripts/migrate_profile.py --profile ii --work-root "/path/to/workspace"
    python3 scripts/migrate_profile.py --profile ii --as ii-ops --switch

退出码
------
    0 成功 / 幂等跳过
    2 源缺失（E-SOURCE）
    3 校验失败（E-VALIDATE，已自动回滚）
    4 需要人工介入（哨兵冲突 / 无备份可回滚）

隐私
----
本脚本的所有输出只包含**统计与键名**，绝不打印画像真实数值（薪资/城市/年限/行业）。
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config_paths as cp
import profile_adapter as pa

SCHEMA_VERSION = 2
EXIT_OK, EXIT_SOURCE, EXIT_VALIDATE, EXIT_MANUAL = 0, 2, 3, 4

#: matcher.py:534-542 的质量闸门基线值（v1.1-stable-20261028）。
#: 同时用 `_quality_gate_source_ok()` 校验源码仍是这几个分支，防止悄悄漂移。
QUALITY_GATES = {
    'min_items_for_high': 4,
    'min_items_for_medium': 3,
    'min_distinct_dims_for_high': 2,
}


# ── 小工具 ────────────────────────────────────────────────
def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _ts() -> str:
    return datetime.now().strftime('%Y%m%d-%H%M%S')


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def _sha256_many(paths) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(_sha256_file(p).encode())
    return h.hexdigest()


def _dump_yaml(obj) -> str:
    y = cp._yaml()
    return y.safe_dump(obj, allow_unicode=True, sort_keys=False,
                       default_flow_style=False, width=1000)


def _dump_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + '\n'


def _die(code: int, msg: str):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ── 源码字面量外提（K11–K15）─────────────────────────────
def _matcher_py() -> Path:
    return cp.skill_script(cp.SKILL_MATCHER, 'scripts/matcher.py')


def extract_matcher_literals(src: str = None) -> dict:
    """从 matcher.py 源码 AST 提取 K11/K12/K13/K15 的字面量。

    P3 起这些字面量已外提到 prefs（matcher.py 不再内联），此时返回 None；
    调用方改用「prefs 自证 + diff 门禁」，见 check_i2。
    """
    if src is None:
        p = _matcher_py()
        if not p.is_file():
            raise RuntimeError(f'E-TRANSFORM 找不到引擎源码：{p}')
        src = p.read_text(encoding='utf-8')
    wanted = {'strong_hr_keywords', 'gm_keywords', 'core_dims', 'ID_TO_NAME'}
    found = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in wanted:
                    try:
                        found[t.id] = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        pass
    missing = wanted - set(found)
    if missing:
        # K11–K15 已在 P3 外提（源码里没有内联字面量）→ 由 prefs 承担真源
        return None
    return {
        'strong_keywords': list(found['strong_hr_keywords']),
        'senior_title_keywords': list(found['gm_keywords']),
        'core_dimensions': sorted(found['core_dims']),
        'dimension_aliases': dict(found['ID_TO_NAME']),
    }


def literals_from_prefs(prefs: dict) -> dict:
    """外提后的 K11–K15 取值来源：已落盘的 prefs.yaml（用于 diff 门禁与迁移记录）。"""
    rules = (prefs or {}).get('rules') or {}
    tb = rules.get('title_bias') or {}
    return {
        'strong_keywords': list(tb.get('strong_keywords') or []),
        'senior_title_keywords': list(tb.get('senior_title_keywords') or []),
        'core_dimensions': sorted(tb.get('core_dimensions') or []),
        'dimension_aliases': dict(rules.get('dimension_aliases') or {}),
    }


def _quality_gate_source_ok(src: str = None) -> bool:
    if src is None:
        p = _matcher_py()
        if not p.is_file():
            return False
        src = p.read_text(encoding='utf-8')
    # P3（K14）后闸门改为读 prefs.rules.quality_gates；两种形态都认，防源码悄悄漂移
    legacy = (r'len\(items\)\s*<\s*4', r'len\(items\)\s*<\s*3', r'len\(unique_dims\)\s*<=\s*1')
    modern = (r'min_items_for_high', r'min_items_for_medium', r'min_distinct_dims_for_high')
    groups = (legacy, modern)
    return any(all(re.search(c, src) for c in g) for g in groups)


# ── 配置拆分（K01–K10）───────────────────────────────────
def split_config(match_cfg: dict, literals: dict) -> dict:
    """旧 match_config.yaml → prefs.yaml（引擎层 + 规则层 + 外提块）。"""
    cfg = copy.deepcopy(match_cfg or {})
    scoring = cfg.get('scoring') or {}
    depth = cfg.get('depth_coefficient') or {}
    rules = {k: cfg[k] for k in
             ('taxonomy', 'dimension_definitions', 'hard_filters', 'override_signals',
              'ai_keywords', 'review_rules', 'reason') if k in cfg}

    # T-1：ai_boost 历史上恒为关闭（旧 config 无 enabled 键，matcher 读到的就是假）。
    # 迁移**强制写 false**，不得"顺手修正"——否则含 AI 关键词的岗位会整体升一级。
    ov = rules.get('override_signals')
    if isinstance(ov, dict):
        ab = ov.get('ai_boost')
        if isinstance(ab, dict):
            ab['enabled'] = False
        else:
            ov['ai_boost'] = {'enabled': False, 'scope': 'title'}
    else:
        # C2：没有 override_signals 的旧配置也要显式写 ai_boost.enabled=false，
        # 否则 I2 的 T-1 断言（读 prefs）会误报
        rules['override_signals'] = {'ai_boost': {'enabled': False, 'scope': 'title'}}

    rules['title_bias'] = {
        'strong_keywords': (literals or {}).get('strong_keywords') or [],
        'senior_title_keywords': (literals or {}).get('senior_title_keywords') or [],
        'core_dimensions': (literals or {}).get('core_dimensions') or [],
    }
    rules['quality_gates'] = dict(QUALITY_GATES)
    rules['dimension_aliases'] = (literals or {}).get('dimension_aliases') or {}

    return {
        'schema_version': SCHEMA_VERSION,
        'extends': 'generic',
        'engine': {'scoring': scoring, 'depth_coefficient': depth},
        'rules': rules,
    }


# ── 断言 ──────────────────────────────────────────────────
def check_i1(profile_v2: dict, legacy_source: dict) -> tuple:
    """I1：to_legacy(profile) == 旧 candidate_profile.json（排除 M23 移层的 ai_boost）。"""
    projection = pa.to_legacy_profile(profile_v2)
    diffs, ignored = pa.diff_legacy(legacy_source, projection, ignore=pa.MOVED_KEYS)
    return (not diffs), diffs, ignored


def check_i2(prefs: dict, effective: dict, frozen: dict, literals: dict) -> tuple:
    """I2：effective_config(ii) == 冻结的 match_config 快照 + K11–K15 外提值一致。

    literals=None 表示 P3 后 matcher.py 已无内联字面量（外提到 prefs）；
    此时改为「prefs 自身完整性」自证（词表非空、别名表可读），
    真正的行为等价保证由 diff_judgment.py（逐行七列）负责。
    """
    problems = []

    def _cmp(key, a, b):
        if a != b:
            problems.append(f'{key}: 与冻结快照不一致')

    for key in ('scoring', 'depth_coefficient', 'taxonomy', 'dimension_definitions',
                'hard_filters', 'ai_keywords', 'review_rules', 'reason'):
        if key not in frozen:
            continue
        _cmp(key, effective.get(key), frozen[key])

    # override_signals：迁移新增 ai_boost.enabled=false（旧文件无此键，行为等价）
    if 'override_signals' in frozen:
        f_ov = copy.deepcopy(frozen.get('override_signals') or {})
        e_ov = copy.deepcopy(effective.get('override_signals') or {})
        if isinstance(f_ov.get('ai_boost'), dict):
            f_ov['ai_boost'].pop('enabled', None)
        e_ab = (e_ov.get('ai_boost') or {})
        if isinstance(e_ov.get('ai_boost'), dict):
            e_ov['ai_boost'] = {k: v for k, v in e_ov['ai_boost'].items() if k != 'enabled'}
        _cmp('override_signals', e_ov, f_ov)

    if (prefs.get('rules', {}).get('override_signals', {}).get('ai_boost', {})
            .get('enabled') is not False):
        problems.append('rules.override_signals.ai_boost.enabled 必须为 false（T-1）')

    # K11–K15 外提值：literals 来自 matcher.py 字面量（旧路径）；None 则自证
    tb = (prefs.get('rules') or {}).get('title_bias') or {}
    aliases = (prefs.get('rules') or {}).get('dimension_aliases')
    if literals is None:
        if not isinstance(tb.get('strong_keywords'), list) or not tb.get('strong_keywords'):
            problems.append('rules.title_bias.strong_keywords 缺失或为空（外提后必须显式保留）')
        if not isinstance(tb.get('senior_title_keywords'), list):
            problems.append('rules.title_bias.senior_title_keywords 缺失')
        if not isinstance(tb.get('core_dimensions'), list):
            problems.append('rules.title_bias.core_dimensions 缺失')
        if not isinstance(aliases, dict):
            problems.append('rules.dimension_aliases 缺失')
    else:
        if tb.get('strong_keywords') != literals['strong_keywords']:
            problems.append('rules.title_bias.strong_keywords 与 matcher.py 字面量不一致')
        if tb.get('senior_title_keywords') != literals['senior_title_keywords']:
            problems.append('rules.title_bias.senior_title_keywords 与 matcher.py 字面量不一致')
        if set(tb.get('core_dimensions') or []) != set(literals['core_dimensions']):
            problems.append('rules.title_bias.core_dimensions 与 matcher.py 字面量不一致')
        if aliases != literals['dimension_aliases']:
            problems.append('rules.dimension_aliases 与 matcher.py ID_TO_NAME 不一致')
    if (prefs.get('rules') or {}).get('quality_gates') != QUALITY_GATES:
        problems.append('rules.quality_gates 与基线值不一致')
    if not _quality_gate_source_ok():
        problems.append('matcher.py 质量闸门分支已变化（源码漂移），需人工确认')

    return (not problems), problems


# ── 迁移主流程 ────────────────────────────────────────────
class Migrator:
    def __init__(self, profile_id: str, home: Path, force=False, work_root=None, switch=False):
        self.pid = profile_id
        self.home = home
        self.force = force
        self.work_root = work_root
        self.switch = switch
        self.profile_dir = home / 'profiles' / profile_id
        self.migration_json = self.profile_dir / 'migration.json'
        self.sentinel = self.profile_dir / '.publishing'

    # -- 源 --------------------------------------------------
    def detect_sources(self) -> dict:
        """返回 {profile, match_config, taxonomy} 三个源文件路径。"""
        cfg_dir = cp.legacy_config_dir()
        skill_profile = cfg_dir / 'candidate_profile.json'
        skill_cfg = cfg_dir / 'match_config.yaml'
        skill_tax = cfg_dir / 'taxonomy.yaml'
        inplace = self.profile_dir / 'profile.json'

        src_profile = skill_profile if skill_profile.is_file() else None
        if src_profile is None and inplace.is_file():
            data = cp.read_json(inplace)
            if not pa.is_v2(data):
                src_profile = inplace
        if src_profile is None:
            _die(EXIT_SOURCE,
                 'E-SOURCE 找不到可迁移的旧画像。\n'
                 f'已尝试：{skill_profile}\n         {inplace}\n'
                 '如已迁移过：python3 scripts/migrate_profile.py --profile '
                 f'{self.pid} --verify')

        if not skill_cfg.is_file():
            _die(EXIT_SOURCE, f'E-SOURCE 找不到旧判定配置：{skill_cfg}')
        if not skill_tax.is_file():
            _die(EXIT_SOURCE, f'E-SOURCE 找不到 taxonomy：{skill_tax}')

        return {'profile': src_profile, 'match_config': skill_cfg, 'taxonomy': skill_tax}

    # -- 备份 ------------------------------------------------
    def backup(self, sources: dict, source_hash: str) -> Path:
        bdir = self.profile_dir / 'backup' / _ts()
        n = 1
        while bdir.exists():
            bdir = self.profile_dir / 'backup' / f'{_ts()}-{n}'
            n += 1
        bdir.mkdir(parents=True, mode=0o700)

        files = []
        plan = (
            ('profile', sources['profile'], 'candidate_profile.json', 'profile.json'),
            ('prefs', sources['match_config'], 'match_config.yaml', 'prefs.yaml'),
            ('taxonomy', sources['taxonomy'], 'taxonomy.yaml', 'taxonomy.yaml'),
        )
        for role, src, archive_name, target_name in plan:
            shutil.copy2(src, bdir / f'source-{archive_name}')
            target = self.profile_dir / target_name
            existed = target.is_file()
            if existed:
                # 被覆盖前的现场原样留档（回滚用）
                shutil.copy2(target, bdir / f'current-{target_name}')
            files.append({
                'role': role,
                'archive': f'source-{archive_name}',
                'archive_current': f'current-{target_name}' if existed else None,
                'target': target_name,
                'existed': existed,
                'source': str(src),
                'sha256': _sha256_file(src),
            })

        manifest = {
            'created_at': _now(),
            'profile_id': self.pid,
            'source_sha256': source_hash,
            'files': files,
        }
        (bdir / 'manifest.json').write_text(_dump_json(manifest), encoding='utf-8')
        with contextlib.suppress(OSError):
            os.chmod(bdir / 'manifest.json', 0o600)
        return bdir

    @staticmethod
    def latest_backup(profile_dir: Path):
        root = profile_dir / 'backup'
        if not root.is_dir():
            return None
        cands = [d for d in root.iterdir() if d.is_dir() and (d / 'manifest.json').is_file()]
        return max(cands, key=lambda d: d.name) if cands else None

    # -- 回滚 ------------------------------------------------
    def rollback(self) -> int:
        bdir = self.latest_backup(self.profile_dir)
        if bdir is None:
            print('E-MANUAL 没有可用备份，无法回滚', file=sys.stderr)
            return EXIT_MANUAL
        manifest = json.loads((bdir / 'manifest.json').read_text(encoding='utf-8'))
        restored, removed = [], []
        for f in manifest.get('files', []):
            target = self.profile_dir / f['target']
            if f.get('existed') and f.get('archive_current'):
                shutil.copy2(bdir / f['archive_current'], target)
                with contextlib.suppress(OSError):
                    os.chmod(target, 0o600)
                restored.append(f['target'])
            elif target.is_file():
                target.unlink()
                removed.append(f['target'])
        with contextlib.suppress(FileNotFoundError):
            self.migration_json.unlink()
        with contextlib.suppress(FileNotFoundError):
            (self.profile_dir / cp.PUBLISHED_NAME).unlink()
        with contextlib.suppress(FileNotFoundError):
            self.sentinel.unlink()
        print(f'[rollback] 备份：{bdir.name}')
        print(f'[rollback] 恢复：{", ".join(restored) or "无"}')
        print(f'[rollback] 删除：{", ".join(removed) or "无"}（迁移新增文件）')
        print('[rollback] 已删除 migration.json；旧文件未被改动，系统回到迁移前状态')
        return EXIT_OK

    # -- 字面量 ------------------------------------------------
    def _resolve_literals(self, match_cfg: dict):
        """K11–K15 取值链（C2）：绝不静默写空词表。

        1) matcher.py 内联字面量（P3 之前的老仓库）
        2) 源 match_config.yaml 自带的 title_bias
        3) 已存在的 profiles/<id>/prefs.yaml
        4) templates/legacy_literals.yaml（v1.1 冻结快照，随仓库分发）
        5) 全部缺失 → E-LITERALS 明确报错（不发布、不回滚）
        """
        lit = extract_matcher_literals()
        if lit:
            print('[literals] 来源：matcher.py 内联字面量')
            return lit

        tb = (match_cfg or {}).get('title_bias')
        if isinstance(tb, dict) and tb.get('strong_keywords'):
            print('[literals] 来源：源 match_config.yaml 的 title_bias')
            return {
                'strong_keywords': list(tb.get('strong_keywords') or []),
                'senior_title_keywords': list(tb.get('senior_title_keywords') or []),
                'core_dimensions': sorted(tb.get('core_dimensions') or []),
                'dimension_aliases': dict((match_cfg or {}).get('dimension_aliases') or {}),
            }

        existing = self.profile_dir / 'prefs.yaml'
        if existing.is_file():
            try:
                lit = literals_from_prefs(cp.read_yaml(existing))
                if lit.get('strong_keywords'):
                    print('[literals] 来源：既有 prefs.yaml（保留已外提的词表）')
                    return lit
            except Exception:
                pass

        try:
            lit = cp.legacy_literals()
            print('[literals] 来源：templates/legacy_literals.yaml（v1.1 冻结）')
            return {
                'strong_keywords': lit['title_bias']['strong_keywords'],
                'senior_title_keywords': lit['title_bias']['senior_title_keywords'],
                'core_dimensions': lit['title_bias']['core_dimensions'],
                'dimension_aliases': lit['dimension_aliases'],
            }
        except cp.ConfigError as e:
            print('E-LITERALS 无法确定标题闸门词表：matcher.py 已外提到 prefs，'
                  '源 match_config.yaml 也没有 title_bias。\n'
                  '此时写空词表会关闭闸门、改变旧判定，并导致 I2 失败回滚；'
                  '本次未写入任何文件。\n'
                  f'{e}', file=sys.stderr)
            sys.exit(EXIT_VALIDATE)

    # -- 主流程 ----------------------------------------------
    def run(self, verify_only: bool = False) -> int:
        # 0) 崩溃恢复：上次 publish 中断 → 先回滚再重来
        if self.sentinel.is_file():
            print('[recover] 检测到 .publishing 哨兵（上次发布中断），先回滚到备份')
            if self.rollback_latest_silent() != EXIT_OK:
                print('E-MANUAL 哨兵冲突且无法自动回滚，请人工检查：'
                      f'{self.profile_dir}', file=sys.stderr)
                return EXIT_MANUAL

        sources = self.detect_sources()
        source_hash = _sha256_many([sources['profile'], sources['match_config'],
                                    sources['taxonomy']])

        legacy_profile = cp.read_json(sources['profile'])
        if pa.is_v2(legacy_profile):
            _die(EXIT_SOURCE, f'E-SOURCE 源画像已是 v2：{sources["profile"]}')

        # 先读源配置：字面量链与冻结快照都需要它
        match_cfg = cp.read_yaml(sources['match_config'])

        literals = self._resolve_literals(match_cfg)

        # C1：冻结快照缺失（新机器）时，从源 match_config.yaml 现场生成，不再 E-SOURCE。
        frozen_path = cp.baseline_dir() / 'match_config_20261028.yaml'
        if frozen_path.is_file():
            frozen = cp.read_yaml(frozen_path)
        else:
            frozen = copy.deepcopy(match_cfg)
            frozen_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(frozen_path.parent, 0o700)
            cp.atomic_write(frozen_path, _dump_yaml(frozen), mode=0o600)
            print(f'[baseline] 未找到冻结快照，已从源 match_config.yaml 生成：{frozen_path}')

        # 幂等判定
        if self.migration_json.is_file() and not self.force:
            rec = json.loads(self.migration_json.read_text(encoding='utf-8'))
            if rec.get('source_sha256') == source_hash and \
                    (self.profile_dir / 'profile.json').is_file():
                # 统一发布标记（P4）：老迁移产物补写 published.json，幂等无副作用
                if not (self.profile_dir / cp.PUBLISHED_NAME).is_file():
                    try:
                        cp.write_published(self.pid, source='migrate',
                                           published_at=rec.get('migrated_at') or _now())
                        print(f'[mark] published.json 已补写（source=migrate）')
                    except Exception as e:
                        print(f'[warn] published.json 写入失败：{e}（画像仍可读，migration.json 回退生效）')
                print('[skip] 已迁移且源未变更（幂等跳过）')
                return self._verify(self._load_payload(), legacy_profile, frozen, literals)
            print('[detect] 源已变更，将新建备份并重新迁移')
        elif (self.profile_dir / 'profile.json').is_file() and not self.force:
            cur = cp.read_json(self.profile_dir / 'profile.json')
            if pa.is_v2(cur):
                _die(EXIT_VALIDATE,
                     'E-VALIDATE 目标已是 v2 但缺少 migration.json（疑似手工改动）。\n'
                     '确认要覆盖请加 --force（会先备份）')

        if verify_only:
            return self._verify(self._load_payload(), legacy_profile, frozen, literals)

        # 1) 备份
        bdir = self.backup(sources, source_hash)
        print(f'[backup] {bdir}（{len(sources)} 个源文件）')

        # 2) transform
        created_at = None
        if self.migration_json.is_file():
            with contextlib.suppress(Exception):
                created_at = json.loads(
                    self.migration_json.read_text(encoding='utf-8')).get('created_at')
        profile_v2 = pa.to_v2(legacy_profile, self.pid,
                              created_at=created_at or _now(), updated_at=_now())
        # K11–K15 已外提：源码无字面量时，_resolve_literals 已按 C2 取值链给出非空词表
        prefs = split_config(match_cfg, literals)
        taxonomy_text = sources['taxonomy'].read_text(encoding='utf-8')

        # 3) validate（I1，发布前）
        ok_i1, diffs, ignored = check_i1(profile_v2, legacy_profile)
        if not ok_i1:
            print('[I1] to_legacy(profile) == candidate_profile.json      FAIL')
            for d in diffs[:10]:
                print(f'  - {d}')
            print('[rollback] 校验失败，未发布任何文件')
            return EXIT_VALIDATE

        # 4) write tmp
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.profile_dir, 0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.profile_dir.parent, 0o700)
        payload = {
            'profile.json': _dump_json(profile_v2),
            'prefs.yaml': _dump_yaml(prefs),
            'taxonomy.yaml': taxonomy_text,
        }
        tmps = {}
        for name, text in payload.items():
            t = self.profile_dir / f'.{name}.tmp'
            t.write_text(text, encoding='utf-8')
            tmps[name] = t

        # 5) publish（哨兵 + 原子替换）
        self.sentinel.write_text(_dump_json({'pid': os.getpid(), 'at': _now(),
                                             'backup': bdir.name}), encoding='utf-8')
        for name in ('profile.json', 'prefs.yaml', 'taxonomy.yaml'):
            os.replace(tmps[name], self.profile_dir / name)
            with contextlib.suppress(OSError):
                os.chmod(self.profile_dir / name, 0o600)
        self.sentinel.unlink()
        print('[publish] profile.json / prefs.yaml / taxonomy.yaml（权限 600）')

        # 6) verify（I2，发布后，读真实文件）
        code = self._verify(self._load_payload(), legacy_profile, frozen, literals)
        if code != EXIT_OK:
            print('[rollback] I2 失败，自动回滚')
            self.rollback()
            return EXIT_VALIDATE

        # 7) mark
        self.migration_json.write_text(_dump_json({
            'schema_version': 1,
            'profile_id': self.pid,
            'migrated_at': _now(),
            'source_sha256': source_hash,
            'source_files': {k: str(v) for k, v in sources.items()},
            'backup_dir': str(bdir),
            'matcher_version': _matcher_version(literals or literals_from_prefs(prefs)),
        }), encoding='utf-8')
        with contextlib.suppress(OSError):
            os.chmod(self.migration_json, 0o600)

        # 统一发布标记（P4）：向导与迁移共用同一个「已发布」判据
        try:
            cp.write_published(self.pid, source='migrate')
        except Exception as e:
            print(f'[warn] published.json 写入失败：{e}（migration.json 回退仍可读）')

        # config.json（active_profile / work_root / onboarding.completed）
        self._write_config()
        (self.profile_dir / 'state').mkdir(parents=True, exist_ok=True)
        (self.profile_dir / 'state' / 'last_used_at').write_text(_now(), encoding='utf-8')
        print(f'[mark] migration.json 已写入（source_sha256={source_hash[:12]}…）')
        print('[done] 迁移完成；旧 config/candidate_profile.json 未删除（保留到 P6 验收）')
        return EXIT_OK

    def rollback_latest_silent(self) -> int:
        bdir = self.latest_backup(self.profile_dir)
        if bdir is None:
            with contextlib.suppress(FileNotFoundError):
                self.sentinel.unlink()
            return EXIT_MANUAL
        manifest = json.loads((bdir / 'manifest.json').read_text(encoding='utf-8'))
        for f in manifest.get('files', []):
            target = self.profile_dir / f['target']
            if f.get('existed') and f.get('archive_current'):
                shutil.copy2(bdir / f['archive_current'], target)
            elif target.is_file():
                target.unlink()
        with contextlib.suppress(FileNotFoundError):
            self.migration_json.unlink()
        with contextlib.suppress(FileNotFoundError):
            (self.profile_dir / cp.PUBLISHED_NAME).unlink()
        with contextlib.suppress(FileNotFoundError):
            self.sentinel.unlink()
        return EXIT_OK

    def _load_payload(self) -> dict:
        p = self.profile_dir / 'profile.json'
        prefs = self.profile_dir / 'prefs.yaml'
        if not p.is_file() or not prefs.is_file():
            _die(EXIT_VALIDATE, f'E-VALIDATE 迁移产物缺失：{p} / {prefs}')
        return {'profile': cp.read_json(p), 'prefs': cp.read_yaml(prefs)}

    def _verify(self, payload: dict, legacy_profile: dict, frozen: dict, literals: dict) -> int:
        profile_v2, prefs = payload['profile'], payload['prefs']
        rc = EXIT_OK

        ok_i1, diffs, ignored = check_i1(profile_v2, legacy_profile)
        tail = f'（已排除 {", ".join(ignored)}：M23 移层到 prefs）' if ignored else ''
        print(f'[I1] to_legacy(profile) == candidate_profile.json      {"OK" if ok_i1 else "FAIL"}{tail}')
        if not ok_i1:
            for d in diffs[:10]:
                print(f'  - {d}')
            rc = EXIT_VALIDATE

        # I2 用真实合并链路（与运行时同一函数），而不是内存对象
        if literals is None:
            print('[I2] K11–K15 已外提到 prefs（matcher.py 无内联字面量），改由 prefs 自证')
        effective = cp.effective_config(self.pid)
        ok_i2, problems = check_i2(prefs, effective, frozen, literals)
        print(f'[I2] effective_config({self.pid}) == match_config_20261028     '
              f'{"OK" if ok_i2 else "FAIL"}')
        if not ok_i2:
            for p in problems:
                print(f'  - {p}')
            rc = EXIT_VALIDATE

        if rc == EXIT_OK:
            n_rules = len(effective.get('taxonomy') or [])
            print(f'[I2] 规则层键：{", ".join(sorted(effective.keys()))}')
            print(f'[I2] taxonomy 维度数：{n_rules}；ai_boost.enabled=false（T-1 保持关闭）')
        return rc

    def _write_config(self) -> None:
        cur = cp.load_config()
        cfg = dict(cur)
        cfg.setdefault('schema_version', 1)
        if self.switch or not cur.get('active_profile'):
            cfg['active_profile'] = self.pid
        if self.work_root:
            cfg['work_root'] = str(Path(os.path.expanduser(self.work_root)))
        elif not cur.get('work_root'):
            cfg['work_root'] = str(self.home / 'work')
        cfg.setdefault('dashboard', {'port': 8765})
        cfg.setdefault('onboarding', {'completed': True})
        cp.atomic_write(self.home / 'config.json', _dump_json(cfg), mode=0o600)


def _matcher_version(literals: dict) -> dict:
    literals = literals or {}
    return {
        'strong_keywords': len(literals.get('strong_keywords') or []),
        'senior_title_keywords': len(literals.get('senior_title_keywords') or []),
        'core_dimensions': len(literals.get('core_dimensions') or []),
        'dimension_aliases': len(literals.get('dimension_aliases') or {}),
    }


def main():
    ap = argparse.ArgumentParser(description='迁移画像/判定配置到 ~/.boss-pipeline')
    ap.add_argument('--profile', required=True, help='画像 id（目录名）')
    ap.add_argument('--as', dest='as_id', default=None, help='迁移成另一个 id（复制而非移动）')
    ap.add_argument('--switch', action='store_true', help='迁移后把 active_profile 切到该 id')
    ap.add_argument('--verify', action='store_true', help='只做 I1/I2 断言，不写文件')
    ap.add_argument('--rollback', action='store_true', help='从最近备份回滚')
    ap.add_argument('--force', action='store_true', help='目标已是 v2 时也覆盖（会先备份）')
    ap.add_argument('--home', default=None, help='覆盖 boss-pipeline 数据根目录')
    ap.add_argument('--work-root', default=None, help='写入 config.json 的爬取产物根目录')
    args = ap.parse_args()

    home = cp.home(create=not args.verify, cli_home=args.home)
    if home is None:
        home = cp.home(create=True, cli_home=args.home)
    # 让后续 config_paths 调用（effective_config 等）解析到同一个 home
    if args.home:
        os.environ[cp.ENV_HOME] = str(home)

    pid = cp._norm_id(args.as_id or args.profile)
    m = Migrator(pid, home, force=args.force, work_root=args.work_root, switch=args.switch)

    if args.rollback:
        sys.exit(m.rollback())
    sys.exit(m.run(verify_only=args.verify))


if __name__ == '__main__':
    main()
