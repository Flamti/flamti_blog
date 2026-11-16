"""Логика Blog Studio без GUI: посты, frontmatter, картинки, проверки, git."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LANGS = ("ru", "en", "es")
LANG_NAMES = {"ru": "Русский", "en": "English", "es": "Español"}
KINDS = {"note": "Заметка", "article": "Статья"}
KIND_ARCHETYPE = {"note": "notes", "article": "posts"}

TEMPLATE_TITLES = {"", "Заголовок статьи", "Article Title", "Título del artículo"}
TEMPLATE_BODIES = {"", "Текст статьи...", "Article text...", "Texto del artículo..."}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
              ".heic", ".heif", ".avif", ".svg"}
PASSTHROUGH_EXTS = {".svg", ".gif"}  # не перекодируем (вектор / анимация)

ORIGINALS_ROOT = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "blog-studio" / "originals"

MONTHS = {
    "ru": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"],
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
           "septiembre", "octubre", "noviembre", "diciembre"],
}


def human_date(d: datetime, lang: str) -> str:
    m = MONTHS[lang][d.month - 1]
    if lang == "en":
        return f"{m} {d.day}, {d.year}"
    if lang == "es":
        return f"{d.day} de {m} de {d.year}"
    return f"{d.day} {m} {d.year}"


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- транслит

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "і": "i", "ї": "yi", "є": "ye", "ґ": "g",
}


def slugify(text: str, max_len: int = 60) -> str:
    text = "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0] or text[:max_len]
    return text


# ---------------------------------------------------------------- frontmatter
# Минимальный парсер/редактор YAML-frontmatter: верхнеуровневые скаляры,
# списки (flow и блочные) и один уровень вложенности (cover:). Редактирование
# точечное, построчное — остальное форматирование файла не трогаем.

def _strip_comment(raw: str) -> str:
    quote = None
    for i, ch in enumerate(raw):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or raw[i - 1] in " \t"):
            return raw[:i].rstrip()
    return raw.strip()


def _parse_value(raw: str):
    raw = raw.strip()
    if raw == "":
        return ""
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw.strip('"')
    if raw.startswith("'"):
        return raw[1:-1].replace("''", "'")
    if raw.startswith("["):
        try:
            return json.loads(raw)
        except ValueError:
            inner = raw.strip("[]").strip()
            return [_parse_value(x) for x in inner.split(",")] if inner else []
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return raw


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    m = re.match(r"^---[ \t]*\r?\n(.*?)(?:\r?\n)?^---[ \t]*(?:\r?\n|$)", text, re.S | re.M)
    if not m:
        return "", text
    return m.group(1), text[m.end():]


def join_frontmatter(fm: str, body: str) -> str:
    return f"---\n{fm.rstrip()}\n---\n{body}"


def parse_frontmatter(fm: str) -> dict:
    result: dict = {}
    parent = None
    for line in fm.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent > 0 and stripped.startswith("- ") and parent is not None:
            if not isinstance(result.get(parent), list):
                result[parent] = []
            result[parent].append(_parse_value(_strip_comment(stripped[2:])))
            continue
        m = re.match(r"^\s*([A-Za-z0-9_]+):(?:\s+(.*))?$", line)
        if not m:
            continue
        key, raw = m.group(1), _strip_comment(m.group(2) or "")
        if indent == 0:
            if raw == "":
                result[key] = {}
                parent = key
            else:
                result[key] = _parse_value(raw)
                parent = None
        elif parent is not None and isinstance(result.get(parent), dict):
            result[parent][key] = _parse_value(raw)
    return result


def yaml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return json.dumps(list(v), ensure_ascii=False)
    if isinstance(v, datetime):
        return v.isoformat()
    return json.dumps(str(v), ensure_ascii=False)


def _block_end(lines: list[str], i: int) -> int:
    j = i + 1
    while j < len(lines) and (lines[j].startswith((" ", "\t")) or lines[j].strip() == ""):
        j += 1
    # не захватываем пустые строки в конце блока
    while j > i + 1 and lines[j - 1].strip() == "":
        j -= 1
    return j


def fm_set(fm: str, key: str, value, raw: bool = False) -> str:
    val = value if raw else yaml_value(value)
    lines = fm.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}:(\s|$)", line):
            lines[i:_block_end(lines, i)] = [f"{key}: {val}"]
            return "\n".join(lines)
    lines.append(f"{key}: {val}")
    return "\n".join(lines)


def fm_set_nested(fm: str, parent: str, key: str, value) -> str:
    val = yaml_value(value)
    lines = fm.splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^{re.escape(parent)}:\s*(.*)$", line)
        if not m:
            continue
        lines[i] = f"{parent}:"  # на случай `cover: {}` или комментария
        end = _block_end(lines, i)
        indent = "  "
        for j in range(i + 1, end):
            cm = re.match(rf"^(\s+){re.escape(key)}:(\s|$)", lines[j])
            if cm:
                lines[j] = f"{cm.group(1)}{key}: {val}"
                return "\n".join(lines)
            if lines[j].strip():
                indent = re.match(r"^(\s+)", lines[j]).group(1)
        lines.insert(end, f"{indent}{key}: {val}")
        return "\n".join(lines)
    lines += [f"{parent}:", f"  {key}: {val}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- картинки в markdown

MD_IMG = re.compile(r'!\[([^\]]*)\]\((<[^>]+>|[^)\s]+)(\s+"[^"]*")?\)')
WIKI_IMG = re.compile(r"!\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]")

# Ссылки, которые Markdown не распознаёт: [текст]{адрес}, [текст] (адрес), [текст](адрес с пробелом)
_URL = r"(?:https?://|mailto:|www\.)[^\s(){}<>]+"
BROKEN_LINK = re.compile(r"(?<!!)\[([^\[\]\n]+)\](?:\{\s*(" + _URL + r")\s*\}|[ \t]+\(\s*(" + _URL + r")\s*\)"
                         r"|\(\s+(" + _URL + r")\s*\)|\((" + _URL + r")\s+\))")


def fix_broken_links(text: str) -> str:
    """[текст]{адрес} и похожие опечатки → [текст](адрес)."""
    def repl(m):
        url = next(g for g in m.groups()[1:] if g)
        if url.startswith("www."):
            url = "https://" + url
        return f"[{m.group(1)}]({url})"
    return BROKEN_LINK.sub(repl, text)


def _norm_target(t: str) -> str | None:
    t = t.strip()
    if t.startswith("<") and t.endswith(">"):
        t = t[1:-1]
    if "://" in t or t.startswith(("/", "#", "data:")):
        return None
    t = urllib.parse.unquote(t)
    while t.startswith("./"):
        t = t[2:]
    return t


def image_refs(body: str) -> tuple[list[str], list[str]]:
    """(обычные markdown-ссылки, wiki-вставки Obsidian)"""
    md = [n for n in (_norm_target(m.group(2)) for m in MD_IMG.finditer(body)) if n]
    wiki = [m.group(1).strip() for m in WIKI_IMG.finditer(body)]
    return md, wiki


def md_link(name: str, alt: str = "") -> str:
    target = f"<{name}>" if " " in name else name
    return f"![{alt}]({target})"


def replace_image_ref(body: str, old: str, new: str | None) -> str:
    """Меняет ссылки на картинку old на new (или удаляет при new=None)."""
    def md_sub(m):
        if _norm_target(m.group(2)) != old:
            return m.group(0)
        return "" if new is None else md_link(new, m.group(1))

    def wiki_sub(m):
        if m.group(1).strip() != old:
            return m.group(0)
        return "" if new is None else md_link(new)

    out = WIKI_IMG.sub(wiki_sub, MD_IMG.sub(md_sub, body))
    if new is None:
        out = re.sub(r"\n{3,}", "\n\n", out)
    return out


# ---------------------------------------------------------------- модель поста

@dataclass
class LangFile:
    lang: str
    path: Path
    exists: bool = False
    fm_text: str = ""
    body: str = ""
    fm: dict = field(default_factory=dict)

    def load(self):
        self.exists = self.path.exists()
        if self.exists:
            text = self.path.read_text(encoding="utf-8")
            self.fm_text, self.body = split_frontmatter(text)
            self.fm = parse_frontmatter(self.fm_text)
        else:
            self.fm_text, self.body, self.fm = "", "", {}
        return self

    def save(self):
        self.path.write_text(join_frontmatter(self.fm_text, self.body), encoding="utf-8")
        self.fm = parse_frontmatter(self.fm_text)

    @property
    def title(self) -> str:
        return str(self.fm.get("title", "") or "")

    @property
    def tags(self) -> list[str]:
        t = self.fm.get("tags") or []
        return [str(x) for x in t] if isinstance(t, list) else [str(t)]

    @property
    def complete(self) -> bool:
        return (self.exists and self.title.strip() not in TEMPLATE_TITLES
                and self.body.strip() not in TEMPLATE_BODIES)


class Post:
    def __init__(self, path: Path):
        self.dir = path
        self.slug = path.name
        self.langs = {l: LangFile(l, path / f"index.{l}.md") for l in LANGS}
        self.reload()

    def reload(self):
        for lf in self.langs.values():
            lf.load()
        return self

    @property
    def main(self) -> LangFile:
        for l in LANGS:
            if self.langs[l].exists:
                return self.langs[l]
        return self.langs["ru"]

    @property
    def draft(self) -> bool:
        return bool(self.main.fm.get("draft", False))

    @property
    def kind(self) -> str:
        return "note" if self.main.fm.get("kind") == "note" else "article"

    @property
    def date(self) -> str:
        return str(self.main.fm.get("date", ""))

    @property
    def cover(self) -> str:
        c = self.main.fm.get("cover")
        return str(c.get("image", "") or "") if isinstance(c, dict) else ""

    def title(self, lang="ru") -> str:
        return self.langs[lang].title or self.main.title

    def images(self) -> list[Path]:
        return sorted(p for p in self.dir.iterdir()
                      if p.is_file() and p.suffix.lower() in IMAGE_EXTS)

    def referenced_images(self) -> dict[str, set[str]]:
        refs: dict[str, set[str]] = {}
        for l, lf in self.langs.items():
            md, wiki = image_refs(lf.body)
            for n in md + wiki:
                refs.setdefault(n, set()).add(l)
        return refs

    def update_all(self, fn):
        """fn(LangFile) меняет fm_text/body; изменения сохраняются для существующих файлов."""
        for lf in self.langs.values():
            lf.load()
            if lf.exists:
                fn(lf)
                lf.save()

    def set_cover(self, name: str):
        def f(lf):
            lf.fm_text = fm_set_nested(lf.fm_text, "cover", "image", name)
            lf.fm_text = fm_set_nested(lf.fm_text, "cover", "relative", True)
        self.update_all(f)

    def rename_image_refs(self, old: str, new: str | None):
        def f(lf):
            lf.body = replace_image_ref(lf.body, old, new)
            c = lf.fm.get("cover")
            if isinstance(c, dict) and c.get("image") == old:
                lf.fm_text = fm_set_nested(lf.fm_text, "cover", "image", new or "")
        self.update_all(f)

    def append_image_link(self, name: str, langs=LANGS):
        def f(lf):
            if lf.lang not in langs:
                return
            body = "" if lf.body.strip() in TEMPLATE_BODIES else lf.body
            lf.body = body.rstrip("\n") + ("\n\n" if body.strip() else "\n") + md_link(name) + "\n"
        self.update_all(f)


def list_posts(repo: Path) -> list[Post]:
    root = repo / "content" / "posts"
    if not root.exists():
        return []
    posts = [Post(p) for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    posts.sort(key=lambda p: (p.date, p.slug), reverse=True)
    return posts


def unique_slug(repo: Path, slug: str) -> str:
    root = repo / "content" / "posts"
    cand, n = slug, 2
    while (root / cand).exists():
        cand, n = f"{slug}-{n}", n + 1
    return cand


def suggest_slug(title_ru: str, when: datetime | None = None) -> str:
    when = when or datetime.now()
    s = slugify(title_ru)
    return f"{when:%Y-%m-%d}-{s}" if s else f"{when:%Y-%m-%d}"


def create_post(repo: Path, slug: str, kind: str, titles: dict[str, str]) -> Post:
    d = repo / "content" / "posts" / slug
    d.mkdir(parents=True, exist_ok=False)
    arche = repo / "archetypes" / KIND_ARCHETYPE[kind]
    date = now_iso()
    for l in LANGS:
        src = arche / f"index.{l}.md"
        text = src.read_text(encoding="utf-8") if src.exists() else "---\ntitle: \"\"\ndate: {{ .Date }}\ndraft: true\n---\n\n"
        text = text.replace("{{ .Date }}", date)
        fm, body = split_frontmatter(text)
        if titles.get(l):
            fm = fm_set(fm, "title", titles[l])
        (d / f"index.{l}.md").write_text(join_frontmatter(fm, body), encoding="utf-8")
    return Post(d)


# ---------------------------------------------------------------- обработка картинок

def _open_image(src: Path):
    from PIL import Image
    try:
        im = Image.open(src)
        im.load()
        return im
    except Exception:
        # HEIC/AVIF и прочее, что Pillow не умеет, — через ImageMagick
        tmp = Path(tempfile.mkstemp(suffix=".png")[1])
        try:
            subprocess.run(["magick", str(src), "-auto-orient", str(tmp)],
                           check=True, capture_output=True)
            im = Image.open(tmp)
            im.load()
            return im
        finally:
            tmp.unlink(missing_ok=True)


def encode_webp(src: Path, dest: Path, quality: int, max_side: int | None):
    """Поворот по EXIF, удаление всех метаданных (кроме цветового профиля), ресайз, WebP."""
    from PIL import Image, ImageOps
    im = _open_image(src)
    icc = im.info.get("icc_profile")
    im = ImageOps.exif_transpose(im)
    has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if has_alpha else "RGB")
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    clean = Image.new(im.mode, im.size)  # чистый холст: никаких exif/xmp в info
    clean.paste(im)
    kw = {"quality": int(quality), "method": 6}
    if icc:
        kw["icc_profile"] = icc
    clean.save(dest, "WEBP", **kw)


def originals_dir(slug: str) -> Path:
    return ORIGINALS_ROOT / slug


def find_original(slug: str, stem: str) -> Path | None:
    d = originals_dir(slug)
    if not d.exists():
        return None
    for p in d.iterdir():
        if p.stem == stem:
            return p
    return None


def unique_name(folder: Path, stem: str, ext: str) -> str:
    name, n = f"{stem}{ext}", 2
    while (folder / name).exists():
        name, n = f"{stem}-{n}{ext}", n + 1
    return name


def nice_stem(original_name: str) -> str:
    s = slugify(Path(original_name).stem, 40)
    if not s or re.match(r"^(pasted-image|screenshot|snimok-ekrana|img|image|photo|dsc|pxl|vid)[-_0-9]*", s):
        return "img"
    return s


def download(url: str, folder: Path) -> Path:
    """Скачивает картинку по http(s) (перетаскивание из браузера)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 blog-studio"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(50_000_000)
        ctype = r.headers.get_content_type()
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if ext not in IMAGE_EXTS:
        ext = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
               "image/avif": ".avif"}.get(ctype, ".jpg")
    stem = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).stem or "img"
    out = folder / f"{stem[:60]}{ext}"
    out.write_bytes(data)
    return out


def add_image(post: Post, src: Path, quality: int, max_side: int | None,
              display_name: str | None = None) -> str:
    """Добавляет внешний файл в пост. Возвращает имя файла в папке поста."""
    ext = src.suffix.lower()
    stem = nice_stem(display_name or src.name)
    if ext in PASSTHROUGH_EXTS and _is_passthrough(src):
        name = unique_name(post.dir, stem, ext)
        shutil.copy2(src, post.dir / name)
        return name
    name = unique_name(post.dir, stem, ".webp")
    out = post.dir / name
    encode_webp(src, out, quality, max_side)
    od = originals_dir(post.slug)
    od.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, od / f"{Path(name).stem}{ext or '.png'}")
    return name


def _is_passthrough(src: Path) -> bool:
    if src.suffix.lower() == ".svg":
        return True
    try:
        from PIL import Image
        with Image.open(src) as im:
            return getattr(im, "n_frames", 1) > 1
    except Exception:
        return False


def needs_processing(path: Path) -> bool:
    ext = path.suffix.lower()
    if ext == ".svg":
        return False
    if ext == ".gif":
        return not _is_passthrough(path)
    if ext != ".webp":
        return True
    try:
        from PIL import Image
        with Image.open(path) as im:
            return bool(im.info.get("exif") or im.info.get("xmp") or len(im.getexif()))
    except Exception:
        return False


def process_in_place(post: Post, name: str, quality: int, max_side: int | None) -> str:
    """Обрабатывает картинку, уже лежащую в папке (например, вставленную в Obsidian):
    оригинал уходит в хранилище оригиналов, ссылки во всех языках переписываются."""
    src = post.dir / name
    stem = nice_stem(name) if not name.lower().endswith(".webp") else Path(name).stem
    new = unique_name(post.dir, stem, ".webp") if src.suffix.lower() != ".webp" else name
    od = originals_dir(post.slug)
    od.mkdir(parents=True, exist_ok=True)
    orig = od / f"{Path(new).stem}{src.suffix.lower()}"
    shutil.copy2(src, orig)
    tmp = post.dir / f".{new}.tmp"
    encode_webp(orig, tmp, quality, max_side)
    if new != name:
        src.unlink()
    tmp.replace(post.dir / new)
    post.rename_image_refs(name, new)
    return new


def recompress(post: Post, name: str, quality: int, max_side: int | None) -> bool:
    orig = find_original(post.slug, Path(name).stem)
    if not orig:
        return False
    tmp = post.dir / f".{name}.tmp"
    encode_webp(orig, tmp, quality, max_side)
    tmp.replace(post.dir / name)
    return True


def trash(path: Path):
    r = subprocess.run(["gio", "trash", str(path)], capture_output=True)
    if r.returncode != 0:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


# ---------------------------------------------------------------- проверки

@dataclass
class Issue:
    level: str  # "error" | "warning"
    text: str
    fix: str | None = None  # "process" — можно исправить обработкой картинок


def check_post(post: Post) -> list[Issue]:
    post.reload()
    issues: list[Issue] = []
    for l, lf in post.langs.items():
        L = l.upper()
        if not lf.exists:
            issues.append(Issue("error", f"{L}: нет файла index.{l}.md"))
            continue
        if lf.title.strip() in TEMPLATE_TITLES:
            issues.append(Issue("error", f"{L}: не заполнен заголовок"))
        if lf.body.strip() in TEMPLATE_BODIES:
            issues.append(Issue("error", f"{L}: нет текста"))
    files = {p.name for p in post.images()}
    refs = post.referenced_images()
    for name, langs in sorted(refs.items()):
        if name not in files and not (post.dir / name).exists():
            issues.append(Issue("error", f"Картинка «{name}» не найдена ({', '.join(sorted(langs)).upper()})"))
        missing = [l for l in LANGS if post.langs[l].exists and l not in langs]
        if missing and name in files:
            issues.append(Issue("warning", f"«{name}» нет в: {', '.join(missing).upper()}"))
    for l, lf in post.langs.items():
        n = len(BROKEN_LINK.findall(lf.body))
        if n:
            issues.append(Issue("error", f"{l.upper()}: ссылок с ошибкой — {n} (например [текст]{{адрес}} вместо "
                                         f"[текст](адрес)), на сайте они не будут ссылками", "links"))
        if WIKI_IMG.search(lf.body):
            issues.append(Issue("error", f"{l.upper()}: вставка ![[…]] в формате Obsidian — Hugo её не покажет", "process"))
    if post.cover and post.cover not in files:
        issues.append(Issue("error", f"Обложка «{post.cover}» не найдена"))
    for p in post.images():
        if needs_processing(p):
            issues.append(Issue("warning", f"«{p.name}» не обработана (EXIF/размер/формат)", "process"))
        elif p.stat().st_size > 1_000_000:
            issues.append(Issue("warning", f"«{p.name}» весит {p.stat().st_size // 1024} КБ"))
        if p.name not in refs and p.name != post.cover:
            issues.append(Issue("warning", f"«{p.name}» нигде не используется"))
    return issues


def hugo_check(repo: Path) -> tuple[bool, str]:
    r = subprocess.run(["hugo", "build", "--buildDrafts", "--renderToMemory", "--quiet"],
                       cwd=repo, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0 and "ERROR" not in out, out


# ---------------------------------------------------------------- git

def git_env() -> dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("LC_ALL", "C.UTF-8")
    for askpass in ("/usr/bin/ksshaskpass", "/usr/lib/ssh/ssh-askpass", "/usr/bin/ssh-askpass"):
        if Path(askpass).exists():
            env["SSH_ASKPASS"] = askpass
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env.setdefault("GIT_ASKPASS", askpass)
            break
    return env


def git(repo: Path, *args, check=True) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                       env=git_env(), stdin=subprocess.DEVNULL)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}:\n{(r.stdout + r.stderr).strip()}")
    return r.stdout


def git_changes(repo: Path) -> list[str]:
    out = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", check=False)
    paths, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        if len(entry) > 3:
            paths.append(entry[3:])
            if entry[0] in "RC":
                i += 1
        i += 1
    return paths


def dirty_slugs(changes: list[str]) -> set[str]:
    out = set()
    for p in changes:
        parts = p.split("/")
        if len(parts) >= 3 and parts[0] == "content" and parts[1] == "posts":
            out.add(parts[2])
    return out


def unpushed_count(repo: Path) -> int:
    out = git(repo, "rev-list", "--count", "@{u}..HEAD", check=False).strip()
    return int(out) if out.isdigit() else 0


def site_url(repo: Path) -> str:
    try:
        m = re.search(r'^baseURL:\s*"?([^"\n]+)"?', (repo / "hugo.yaml").read_text(), re.M)
        if m:
            return m.group(1).rstrip("/") + "/"
    except OSError:
        pass
    return ""


def post_url(base: str, slug: str, lang: str = "ru") -> str:
    return f"{base}{lang}/posts/{slug}/"


def commit_and_push(repo: Path, paths: list[str], message: str, log) -> None:
    if paths:
        git(repo, "add", "-A", "--", *paths)
    if git(repo, "diff", "--cached", "--name-only").strip():
        log("Коммит…")
        git(repo, "commit", "-m", message)
    else:
        log("Новых изменений для коммита нет.")
    log("Подтягиваю изменения с сервера…")
    try:
        git(repo, "pull", "--rebase", "--autostash")
    except RuntimeError:
        git(repo, "rebase", "--abort", check=False)
        raise
    log("Отправка (git push)…")
    out = subprocess.run(["git", "push"], cwd=repo, capture_output=True, text=True,
                         env=git_env(), stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError("git push:\n" + (out.stdout + out.stderr).strip())
    log((out.stdout + out.stderr).strip())


def set_published(post: Post, published: bool):
    def f(lf):
        if published and lf.fm.get("draft", False):
            lf.fm_text = fm_set(lf.fm_text, "date", now_iso(), raw=True)
        lf.fm_text = fm_set(lf.fm_text, "draft", not published)
    post.update_all(f)


def url_is_live(url: str) -> bool:
    try:
        req = urllib.request.Request(url + "?t=" + str(int(datetime.now().timestamp())),
                                     method="HEAD", headers={"User-Agent": "blog-studio"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False
