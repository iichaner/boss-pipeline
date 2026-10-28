#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_profile.py —— 画像/偏好校验（转发入口，P3）

实现只有一份：`job-match-priority/scripts/validate_profile.py`。
用法：

    python3 scripts/validate_profile.py ~/.boss-pipeline/profiles/<id>/profile.json

退出码：0 = 0 error；1 = 有 error（E-*）；2 = 文件不可读。
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config_paths as cp  # noqa: E402


def main() -> int:
    target = cp.skill_script(cp.SKILL_MATCHER, 'scripts/validate_profile.py')
    return subprocess.call([sys.executable, str(target), *sys.argv[1:]])


if __name__ == '__main__':
    sys.exit(main())
