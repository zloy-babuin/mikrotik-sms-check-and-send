#!/bin/bash
cd "$(dirname "$0")"
# Активируем виртуальную среду
source venv/bin/activate

# Запускаем основной скрипт
python3 main.py

# Деактивируем виртуальную среду (не обязательно, но хорошая практика)
deactivate