#!/bin/bash
# Запуск с автоперезапуском: если процесс умер (нехватка памяти, редкий сбой) — поднимется сам через 3 секунды.
# Использование (внутри screen):  cd /bots/blud && source venv/bin/activate && bash run.sh
cd "$(dirname "$0")"
while true; do
  python main.py
  code=$?
  [ $code -eq 3 ] && exit 3  # бот уже запущен в другом месте — не плодим копии
  echo "[run.sh] бот завершился с кодом $code, перезапуск через 3 с (Ctrl+C — остановить)"
  sleep 3
done
