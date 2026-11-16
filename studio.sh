#!/usr/bin/env bash
# Запуск Blog Studio — графического интерфейса для постов
exec python3 "$(dirname "$(readlink -f "$0")")/tools/blog-studio/app.py" "$@"
