#!/usr/bin/env bash
set -e

if [ -z "$1" ]; then
  echo "Использование: ./new-post.sh имя-папки-поста"
  echo "Пример:        ./new-post.sh devlog-01"
  exit 1
fi

hugo new content --kind posts "posts/$1"
echo "Готово! Файлы созданы в content/posts/$1/"
