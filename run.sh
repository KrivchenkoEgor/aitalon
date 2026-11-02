#!/bin/zsh
# python3 в PATH — это 3.8 без FastAPI, поэтому путь зафиксирован.
PY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13
cd "$(dirname "$0")"
# 0.0.0.0 — чтобы в чат попадали с телефона по QR. AITALON_HOST=127.0.0.1 ./run.sh
# вернёт сервер в режим «только на этом Маке».
exec "$PY" -m uvicorn app.main:app --host "${AITALON_HOST:-0.0.0.0}" --port 8000
