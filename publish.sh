#!/usr/bin/env bash
set -e
MSG="${1:-Update blog}"
git add .
git commit -m "$MSG"
git push
