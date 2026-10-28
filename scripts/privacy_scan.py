#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""privacy_scan.py —— 出厂隐私扫描（转发入口，P3）

实现只有一份：`job-match-priority/scripts/privacy_scan.py`。
本文件仅做定位 + 转发，方便在 boss-pipeline 目录下直接执行：

    python3 scripts/privacy_scan.py --roots ~/.agents/skills/{boss-pipeline,job-match-priority,boss-resume-crawler,boss-auto-greet}

退出码与实现一致：0 = 无 block；1 = 有 block；2 = 目录不可读。
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config_paths as cp  # noqa: E402


def main() -> int:
    target = cp.skill_script(cp.SKILL_MATCHER, 'scripts/privacy_scan.py')
    return subprocess.call([sys.executable, str(target), *sys.argv[1:]])


if __name__ == '__main__':
    sys.exit(main())
