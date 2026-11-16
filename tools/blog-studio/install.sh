#!/usr/bin/env bash
# Добавляет Blog Studio в меню приложений KDE
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/blog-studio.desktop <<DESKTOP
[Desktop Entry]
Type=Application
Name=Blog Studio
GenericName=Редактор блога
Comment=Новые посты, картинки и публикация блога
Exec=python3 "$DIR/app.py"
Icon=story-editor
Terminal=false
Categories=Office;Utility;
Keywords=blog;hugo;блог;пост;
StartupWMClass=blog-studio
DESKTOP
update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "Готово: Blog Studio появится в меню приложений."
