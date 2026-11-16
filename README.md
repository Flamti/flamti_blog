# Шаблон блога (Hugo + PaperMod)

## Быстрый старт на Arch Linux

1. Установите зависимости (если ещё не установлены):
   ```bash
   sudo pacman -S hugo go git
   ```

2. Перейдите в распакованную папку и скачайте тему в `_vendor/`:
   ```bash
   hugo mod get github.com/adityatelange/hugo-PaperMod
   hugo mod vendor
   ```

3. Проверьте сайт локально в браузере (`http://localhost:1313`):
   ```bash
   hugo server -D
   ```

4. Отправьте проект в свой репозиторий `pages` на Codeberg:
   ```bash
   git init -b main
   git add .
   git commit -m "Initial blog setup"
   git remote add origin git@codeberg.org:Flamti/pages.git
   git push -u origin main
   ```

## Blog Studio — ежедневная работа с постами

Запуск: «Blog Studio» в меню приложений или `./studio.sh`
(ярлык ставит `tools/blog-studio/install.sh`). Нужны `python-pyside6` и `python-pillow`.

- **Ctrl+N** — новая заметка, **Ctrl+Shift+N** — новая статья. Папка: `ГГГГ-ММ-ДД-транслит`,
  создаются `index.ru/en/es.md`, русская версия сразу открывается в Obsidian.
- **Заголовки и теги** всех трёх языков правятся в приложении, текст — в Obsidian (кнопки RU / EN / ES).
- **Картинки**: перетащить в окно или Ctrl+V. Удаляется EXIF (в том числе GPS), картинка уменьшается
  и сжимается в WebP с выбранными качеством и размером, ссылка добавляется во все языки.
  Оригиналы хранятся в `~/.local/share/blog-studio/originals/`, поэтому картинку можно пережать заново (ПКМ).
  Картинки, вставленные прямо в Obsidian, обрабатываются кнопкой «Обработать картинки из Obsidian».
- **Предпросмотр** запускает `hugo server -D` и открывает пост в браузере.
- **Опубликовать**: проверка (три языка, картинки, сборка Hugo) → `draft: false` и дата → коммит → push.
  Затем приложение ждёт, пока пост появится на сайте, и показывает уведомление.
- Шаблоны новых постов: `archetypes/notes/` (заметка) и `archetypes/posts/` (статья).
