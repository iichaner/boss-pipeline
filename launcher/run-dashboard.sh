#!/bin/bash
# 常驻启动 Boss 直聘 Dashboard（给 launchd 用：前台运行，进程退出由 launchd 拉起）
# 与双击版 Boss直聘监控.command 的区别：不开浏览器、不依赖终端，开机自启 + 崩溃自恢复，
# 长期监控任务的调度线程就跑在这个进程里，所以它必须一直活着。

SKILL_DIR="$HOME/.agents/skills/boss-pipeline"
WORKSPACE="$HOME/.openclaw/workspace/boss直聘一体化"
PORT="${BOSS_DASHBOARD_PORT:-8765}"
LOG_DIR="$HOME/Library/Logs/boss-pipeline"
mkdir -p "$LOG_DIR"

# output_dir 用最新 session（仅为看板默认视图；监控任务本身存于 history_root，不受影响）
LATEST=$(ls -td "$WORKSPACE"/*/ 2>/dev/null | head -1)
OUTPUT_DIR="${LATEST}01_raw"
[ -d "$OUTPUT_DIR" ] || OUTPUT_DIR="$WORKSPACE"

# 端口被外部进程占用时不自愈重试（避免 launchd 无限循环抢端口）
if lsof -i :"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  EXIST=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)
  echo "[$(date '+%F %T')] 端口 $PORT 已被 PID $EXIST 占用，本次不启动" >> "$LOG_DIR/launch.log"
  exit 0
fi

# 选带 websocket-client 的 Python
PYTHON_BIN=""
for cand in "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3" \
            "/usr/local/bin/python3" "$(command -v python3)"; do
  if [ -x "$cand" ] && "$cand" -c "import websocket" >/dev/null 2>&1; then
    PYTHON_BIN="$cand"; break
  fi
done
[ -n "$PYTHON_BIN" ] || PYTHON_BIN="$(command -v python3)"

echo "[$(date '+%F %T')] 启动 python=$PYTHON_BIN port=$PORT output=$OUTPUT_DIR" >> "$LOG_DIR/launch.log"

# exec 让 python 成为本进程，launchd 的 kill/重启直接作用在服务上
exec "$PYTHON_BIN" "$SKILL_DIR/scripts/serve_dashboard.py" \
  --output-dir "$OUTPUT_DIR" --port "$PORT" >> "$LOG_DIR/server.log" 2>&1
