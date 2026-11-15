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
