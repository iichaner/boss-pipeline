#!/bin/bash
# Boss直聘监控.command
# 双击启动 Dashboard + 自动打开浏览器

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$HOME/.agents/skills/boss-pipeline"
DASHBOARD_SCRIPT="$SKILL_DIR/scripts/serve_dashboard.py"

# 找可用端口（8765-8775）
PORT=8765
for p in $(seq 8765 8775); do
  if ! lsof -i :$p >/dev/null 2>&1; then
    PORT=$p
    break
  fi
done

# 默认 output-dir：最新的 session 目录
WORKSPACE="$HOME/.openclaw/workspace/boss直聘一体化"
LATEST=$(ls -td "$WORKSPACE"/*/ 2>/dev/null | head -1)
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
"$PYTHON_BIN" "$DASHBOARD_SCRIPT" --output-dir "$OUTPUT_DIR" --port $PORT &
SERVER_PID=$!

# 等服务器就绪
sleep 1.5

# 打开浏览器
open "http://localhost:$PORT"

# 等待服务器进程（阻塞，关闭终端时自动 kill）
wait $SERVER_PID
