#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

python_is_usable() {
    "$1" -c 'import sys, lxml, pypdf; assert sys.version_info >= (3, 10)' \
        >/dev/null 2>&1
}

if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]] \
    && python_is_usable "$PROJECT_ROOT/.venv/bin/python"; then
    PYTHON_EXE="$PROJECT_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1 \
    && python_is_usable "$(command -v python3)"; then
    PYTHON_EXE="$(command -v python3)"
elif command -v python >/dev/null 2>&1 \
    && python_is_usable "$(command -v python)"; then
    PYTHON_EXE="$(command -v python)"
elif command -v uv >/dev/null 2>&1; then
    cd "$PROJECT_ROOT"
    exec uv run python -m market_maker_tool "$@"
else
    echo "未找到包含项目依赖的 Python 3.10+；请先安装项目依赖。" >&2
    exit 1
fi

cd "$PROJECT_ROOT"
exec "$PYTHON_EXE" -m market_maker_tool "$@"
