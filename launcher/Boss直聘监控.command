#!/bin/bash
# Boss直聘监控.command
# 双击启动 Dashboard + 自动打开浏览器
# 首次启动会先跑环境自检；没有画像时直接把浏览器指到向导页。

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DASHBOARD_SCRIPT="$SKILL_DIR/scripts/serve_dashboard.py"

# 用户数据根与产物根（P2 解耦）：BOSS_PIPELINE_HOME → 默认 ~/.boss-pipeline；
# 产物根优先级：BOSS_PIPELINE_WORK_ROOT > config.json.work_root > <home>/work
PIPELINE_HOME="${BOSS_PIPELINE_HOME:-$HOME/.boss-pipeline}"
WORKSPACE="${BOSS_PIPELINE_WORK_ROOT:-}"
if [ -z "$WORKSPACE" ] && [ -f "$PIPELINE_HOME/config.json" ]; then
  WORKSPACE=$(plutil -extract work_root raw -o - "$PIPELINE_HOME/config.json" 2>/dev/null || true)
fi
[ -n "$WORKSPACE" ] || WORKSPACE="$PIPELINE_HOME/work"
mkdir -p "$WORKSPACE" 2>/dev/null || true

# 选择带有 websocket-client 的 Python
PYTHON_BIN=""
for cand in "/usr/local/bin/python3" "$(command -v python3)"; do
  if [ -x "$cand" ] && "$cand" -c "import websocket" >/dev/null 2>&1; then
    PYTHON_BIN="$cand"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || echo python3)"
  echo "⚠️ 未找到已安装 websocket-client 的 Python，正在尝试自动安装..."
  "$PYTHON_BIN" -m pip install --quiet --user websocket-client 2>/dev/null || "$PYTHON_BIN" -m pip install --quiet websocket-client 2>/dev/null
fi
echo "▶ 使用 Python: $PYTHON_BIN"

# 首启自检：env_check 恒 exit 0，不阻断启动；失败只打印修复命令
echo "▶ 环境自检"
"$PYTHON_BIN" "$SKILL_DIR/scripts/env_check.py" 2>&1 || true

# 依赖缺失时不再启动服务（否则会在 import 阶段抛 traceback）
if ! "$PYTHON_BIN" -c "import yaml, requests, websocket" >/dev/null 2>&1; then
  echo "❌ Python 依赖未就绪，先执行这一条："
  echo "   $PYTHON_BIN -m pip install pyyaml requests websocket-client"
  echo "   安装完成后重新双击本启动器。"
  exit 1
fi

# 画像是否就绪（只读，不启动浏览器，不受 CDP 拖慢）
CONFIG_READY="$("$PYTHON_BIN" - "$SKILL_DIR" <<'PY' 2>/dev/null
import sys
sys.path.insert(0, sys.argv[1] + '/scripts')
try:
    import config_paths as cp
    cp.load_profile()
    cp.load_prefs()
    print('1')
except Exception:
    print('0')
PY
)"
if [ "$CONFIG_READY" != "1" ]; then
  echo "⚠️ 尚未配置画像：首次使用请完成向导（网页会自动打开）"
  echo "   命令行方式：$PYTHON_BIN $SKILL_DIR/scripts/onboard.py"
fi

# D1：8765 复用前必须校验运行中的服务用的是同一个 BOSS_PIPELINE_HOME。
# 指纹 = sha256(realpath(home))[:16]，与 /api/health 返回的 home_fingerprint 同算法；
# 指纹相同才说明是「同一套数据」，否则一律换端口，绝不把用户送进别人的 Dashboard。
HOME_FP="$("$PYTHON_BIN" - "$PIPELINE_HOME" <<'PY'
import hashlib, os, sys
p = os.path.realpath(os.path.expanduser(sys.argv[1]))
print(hashlib.sha256(p.encode('utf-8')).hexdigest()[:16])
PY
)"
if lsof -nP -i :8765 -sTCP:LISTEN >/dev/null 2>&1; then
  REMOTE_FP="$(curl -s --max-time 2 http://127.0.0.1:8765/api/health 2>/dev/null \
    | "$PYTHON_BIN" -c 'import sys,json;print((json.load(sys.stdin) or {}).get("home_fingerprint",""))' 2>/dev/null || true)"
  if [ -n "$HOME_FP" ] && [ "$REMOTE_FP" = "$HOME_FP" ]; then
    echo "▶ 检测到常驻服务已在 8765 运行，且 home 指纹一致（同一套数据），直接打开浏览器"
    if [ "$CONFIG_READY" != "1" ]; then
      open "http://localhost:8765/wizard.html"
    else
      open "http://localhost:8765"
    fi
    exit 0
  fi
  echo "⚠️ 8765 被另一套数据占用（home 指纹：本地 ${HOME_FP:-未知} / 远端 ${REMOTE_FP:-未知}）"
  echo "   → 不复用该服务，自动换到下一个空闲端口"
fi

# 找可用端口（8765-8775）
PORT=8765
for p in $(seq 8765 8775); do
  if ! lsof -i :$p >/dev/null 2>&1; then
    PORT=$p
    break
  fi
done

# 默认 output-dir：最新一次性 session（20*/ 日期前缀，排除 长期- 监控目录：
# 它顶层没有 01_raw，会被误当最新 session 导致默认视图/历史根全部错位）
LATEST=$(ls -td "$WORKSPACE"/20*/ 2>/dev/null | head -1)
OUTPUT_DIR="${LATEST}01_raw"

if [ ! -d "$OUTPUT_DIR" ]; then
  OUTPUT_DIR="$WORKSPACE"
fi

# 后台启动服务器
"$PYTHON_BIN" "$DASHBOARD_SCRIPT" --output-dir "$OUTPUT_DIR" --history-root "$WORKSPACE" --port $PORT &
SERVER_PID=$!

# 等服务器就绪
sleep 1.5

# 打开浏览器：无画像 → 向导页；已配置 → 主界面
if [ "$CONFIG_READY" != "1" ]; then
  open "http://localhost:$PORT/wizard.html"
else
  open "http://localhost:$PORT"
fi

# 等待服务器进程（阻塞，关闭终端时自动 kill）
wait $SERVER_PID
