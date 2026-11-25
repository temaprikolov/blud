#!/bin/bash
cd "$(dirname "$0")"
while true; do
  python main.py
  code=$?
  [ $code -eq 3 ] && exit 3
  echo "[run.sh] бот завершился с кодом $code, перезапуск через 3 с (Ctrl+C — остановить)"
  sleep 3
done
