#!/usr/bin/env bash
# 本地 Web 工作台启动脚本。
#
# 用法：
#   ./start.sh                      # 用 config/config.yaml 启动，默认工程取配置里的 project_root
#   ./start.sh /path/to/project     # 指定默认工程
#   NDW_PORT=9000 ./start.sh        # 换端口
#
# 可用环境变量：NDW_PROJECT / NDW_CONFIG / NDW_HOST / NDW_PORT / PYTHON
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PY="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
    echo "找不到虚拟环境 Python：$PY" >&2
    echo "请先创建并安装依赖：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

if ! "$PY" -c "import uvicorn" >/dev/null 2>&1; then
    echo "缺少 uvicorn，请先安装：$PY -m pip install fastapi uvicorn" >&2
    exit 1
fi

CONFIG="${NDW_CONFIG:-$ROOT/config/config.yaml}"
HOST="${NDW_HOST:-127.0.0.1}"
PORT="${NDW_PORT:-8765}"
PROJECT="${1:-${NDW_PROJECT:-}}"

if [ ! -f "$CONFIG" ]; then
    echo "提示：未找到配置文件 $CONFIG，将使用内置默认值" >&2
fi

ARGS=(--config "$CONFIG" serve --host "$HOST" --port "$PORT")
if [ -n "$PROJECT" ]; then
    ARGS=(-p "$PROJECT" "${ARGS[@]}")
fi

echo "工作台地址：http://$HOST:$PORT"
if [ -n "$PROJECT" ]; then
    echo "默认工程：$PROJECT"
fi
echo "按 Ctrl+C 停止"

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m novel_drama.cli "${ARGS[@]}"
