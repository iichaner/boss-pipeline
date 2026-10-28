#!/bin/bash
# Boss直聘监控.command
# 双击启动 Dashboard + 自动打开浏览器

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DASHBOARD_SCRIPT="$SKILL_DIR/scripts/serve_dashboard.py"

# launchd 常驻服务优先：8765 已活着就直接开浏览器，不再起第二个服务
# （两个服务共用同一历史根目录会出现两个监控调度线程抢同一批任务 = 重复爬 = 风控风险；
#   代码层已加 flock 兜底，这里是把双击体验也对齐）
if lsof -nP -i :8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "▶ 检测到常驻服务已在 8765 运行（launchd），直接打开浏览器"
  open "http://localhost:8765"
  exit 0
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
# 用户数据根与产物根（P2 解耦）：BOSS_PIPELINE_HOME → 默认 ~/.boss-pipeline；
# 产物根优先级：BOSS_PIPELINE_WORK_ROOT > config.json.work_root > <home>/work
PIPELINE_HOME="${BOSS_PIPELINE_HOME:-$HOME/.boss-pipeline}"
WORKSPACE="${BOSS_PIPELINE_WORK_ROOT:-}"
if [ -z "$WORKSPACE" ] && [ -f "$PIPELINE_HOME/config.json" ]; then
  WORKSPACE=$(plutil -extract work_root raw -o - "$PIPELINE_HOME/config.json" 2>/dev/null || true)
fi
[ -n "$WORKSPACE" ] || WORKSPACE="$PIPELINE_HOME/work"
mkdir -p "$WORKSPACE" 2>/dev/null || true
LATEST=$(ls -td "$WORKSPACE"/20*/ 2>/dev/null | head -1)
OUTPUT_DIR="${LATEST}01_raw"

if [ ! -d "$OUTPUT_DIR" ]; then
  OUTPUT_DIR="$WORKSPACE"
fi

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

# 后台启动服务器
"$PYTHON_BIN" "$DASHBOARD_SCRIPT" --output-dir "$OUTPUT_DIR" --history-root "$WORKSPACE" --port $PORT &
SERVER_PID=$!

# 等服务器就绪
sleep 1.5

# 打开浏览器
open "http://localhost:$PORT"

# 等待服务器进程（阻塞，关闭终端时自动 kill）
wait $SERVER_PID
