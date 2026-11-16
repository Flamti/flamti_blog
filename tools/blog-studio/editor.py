"""Поле Markdown с панелью форматирования и подсветкой разметки."""
from __future__ import annotations

import re

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QKeySequence,
                           QSyntaxHighlighter, QTextCharFormat, QTextCursor)
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QToolBar, QToolButton, QVBoxLayout)

import core

URL_RE = re.compile(r"^(?:https?://|mailto:|www\.)\S+$|^[\w.-]+\.[a-z]{2,}(?:/\S*)?$", re.I)
MD_LINK_RE = re.compile(r"^\[([^\]]*)\]\(\s*<?([^)>]*)>?\s*\)$")
PS = " "  # разделитель абзацев в QTextCursor.selectedText()


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url or url.startswith(("#", "/", "./", "../")) or re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
        return url
    if "." in url.split("/")[0] and not url.lower().endswith(tuple(core.IMAGE_EXTS)):
        return "https://" + url  # flamti.org → https://flamti.org
    return url


def md_url(url: str) -> str:
    url = url.replace(")", "%29").replace("(", "%28")
    return f"<{url}>" if " " in url else url


# ======================================================================= ссылка

class LinkDialog(QDialog):
    def __init__(self, text="", url="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ссылка")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.text = QLineEdit(text)
        self.text.setPlaceholderText("что увидит читатель")
        self.url = QLineEdit(url)
        self.url.setPlaceholderText("https://…")
        form.addRow("Текст:", self.text)
        form.addRow("Адрес:", self.url)
        lay.addLayout(form)
        self.preview = QLabel()
        self.preview.setStyleSheet("color: gray")
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.preview)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Вставить")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        self.text.textChanged.connect(self.update_preview)
        self.url.textChanged.connect(self.update_preview)
        self.update_preview()
        (self.url if text and not url else self.text).setFocus()

    def markdown(self) -> str:
        url = normalize_url(self.url.text())
        text = self.text.text().strip().replace("[", "\\[").replace("]", "\\]") or url
        return f"[{text}]({md_url(url)})"

    def update_preview(self):
        self.ok.setEnabled(bool(self.url.text().strip()))
        self.preview.setText("Будет вставлено: " + self.markdown() if self.url.text().strip() else "")


# ======================================================================= подсветка

class MarkdownHighlighter(QSyntaxHighlighter):
    def __init__(self, doc, palette):
        super().__init__(doc)
        accent = palette.link().color()
        gray = palette.placeholderText().color()

        def fmt(color=None, bold=False, italic=False, strike=False, under=False, mono=False, wave=False, scale=None):
            f = QTextCharFormat()
            if color:
                f.setForeground(color)
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            f.setFontItalic(italic)
            f.setFontStrikeOut(strike)
            if under:
                f.setFontUnderline(True)
            if wave:
                f.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
                f.setUnderlineColor(QColor("#e0443e"))
                f.setBackground(QColor(224, 68, 62, 40))
            if mono:
                f.setFontFamilies([QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()])
                f.setBackground(QColor(127, 127, 127, 40))
            return f

        self.f_head = fmt(accent, bold=True)
        self.f_mark = fmt(gray)
        self.f_code_block = fmt(mono=True)
        self.rules = [
            (re.compile(r"^>.*"), fmt(gray, italic=True)),
            (re.compile(r"^\s*(?:[-*+]|\d+[.)])\s"), fmt(accent, bold=True)),
            (re.compile(r"^(?:-{3,}|\*{3,}|_{3,})\s*$"), fmt(gray)),
            (re.compile(r"(?<![*\w])\*(?!\s)[^*\n]+?(?<!\s)\*(?![*\w])|(?<!\w)_(?!\s)[^_\n]+?(?<!\s)_(?!\w)"),
             fmt(italic=True)),
            (re.compile(r"\*\*(?!\s).+?(?<!\s)\*\*|__(?!\s).+?(?<!\s)__"), fmt(bold=True)),
            (re.compile(r"~~(?!\s).+?(?<!\s)~~"), fmt(strike=True)),
            (re.compile(r"`[^`\n]+`"), fmt(mono=True)),
            (re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)|<https?://[^>\s]+>"), fmt(accent, under=True)),
            (core.BROKEN_LINK, fmt(wave=True)),
        ]

    def highlightBlock(self, text):
        u16 = (lambda i: i) if all(ord(ch) < 0x10000 for ch in text) else \
            (lambda i: i + sum(1 for ch in text[:i] if ord(ch) > 0xFFFF))

        def setf(s, e, f):
            self.setFormat(u16(s), u16(e) - u16(s), f)

        prev = self.previousBlockState()
        fence = text.lstrip().startswith("```")
        if prev == 1 or fence:  # внутри блока кода
            setf(0, len(text), self.f_code_block)
            self.setCurrentBlockState(0 if (fence and prev == 1) else 1)
            return
        self.setCurrentBlockState(0)
        m = re.match(r"^(#{1,6})\s.*", text)
        if m:
            setf(0, len(text), self.f_head)
            return
        for rx, f in self.rules:
            for m in rx.finditer(text):
                setf(m.start(), m.end(), f)


# ======================================================================= поле

class MarkdownEdit(QPlainTextEdit):
    """QPlainTextEdit с командами форматирования Markdown (жирный, ссылки, списки…)."""
    image_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.highlighter = MarkdownHighlighter(self.document(), self.palette())

    def event(self, e):
        # QPlainTextEdit сам забирает некоторые сочетания (Ctrl+K = удалить до конца строки) — наши важнее
        if e.type() == QEvent.Type.ShortcutOverride:
            ks = QKeySequence(e.keyCombination())
            if any(a.shortcut() == ks for a in self.actions()):
                e.ignore()
                return True
        return super().event(e)

    # ---------------------------------------------------- вспомогательное

    def _text(self, s, e) -> str:
        c = QTextCursor(self.document())
        c.setPosition(max(0, s))
        c.setPosition(min(e, self.document().characterCount() - 1), QTextCursor.MoveMode.KeepAnchor)
        return c.selectedText()

    def _segments(self, c: QTextCursor) -> list[tuple[int, int]]:
        """Выделение, разбитое по строкам, без пробелов по краям (разметка не переходит через абзацы)."""
        s, e = c.selectionStart(), c.selectionEnd()
        segs, pos = [], s
        for part in self._text(s, e).split(PS):
            a, b = pos, pos + len(part.encode("utf-16-le")) // 2
            lead = len(part) - len(part.lstrip())
            trail = len(part) - len(part.rstrip())
            if part.strip():
                segs.append((a + len(part[:lead].encode("utf-16-le")) // 2,
                             b - len(part[len(part) - trail:].encode("utf-16-le")) // 2))
            pos = b + 1
        return segs

    def _word_under_cursor(self) -> QTextCursor:
        c = self.textCursor()
        if not c.hasSelection():
            w = QTextCursor(c)
            w.select(QTextCursor.SelectionType.WordUnderCursor)
            if w.selectedText().strip() and re.match(r"^\w+$", w.selectedText()):
                return w
        return c

    # ---------------------------------------------------- команды

    def wrap(self, mark: str, placeholder: str):
        """Обернуть выделение (или слово под курсором) в mark…mark; повторное нажатие снимает разметку."""
        c = self._word_under_cursor()
        n = len(mark)
        c.beginEditBlock()
        if not c.hasSelection():
            pos = c.position()
            c.insertText(mark + placeholder + mark)
            c.setPosition(pos + n)
            c.setPosition(pos + n + len(placeholder), QTextCursor.MoveMode.KeepAnchor)
        else:
            def is_mark(s, e):  # ровно mark, а не часть более длинного (* внутри **)
                if self._text(s, e) != mark:
                    return False
                return n > 1 or (self._text(s - 1, s) != mark and self._text(e, e + 1) != mark)

            def marks(a, b):
                """Где стоят открывающая/закрывающая метки сегмента: внутри выделения или сразу за ним."""
                left = (a, a + n) if b - a >= 2 * n and is_mark(a, a + n) else \
                    (a - n, a) if is_mark(a - n, a) else None
                right = (b - n, b) if b - a >= 2 * n and is_mark(b - n, b) else \
                    (b, b + n) if is_mark(b, b + n) else None
                return (left, right) if left and right else None

            segs = self._segments(c)
            unwrap = all(marks(a, b) for a, b in segs)
            spans = []  # (начало, конец, изменение длины) — с конца документа к началу
            for a, b in reversed(segs):
                e = QTextCursor(self.document())
                if unwrap:
                    (l0, l1), (r0, r1) = marks(a, b)
                    e.setPosition(r0); e.setPosition(r1, QTextCursor.MoveMode.KeepAnchor); e.removeSelectedText()
                    e.setPosition(l0); e.setPosition(l1, QTextCursor.MoveMode.KeepAnchor); e.removeSelectedText()
                    spans.append((min(a, l0), max(b, r1) - 2 * n, -2 * n))
                else:
                    e.setPosition(b); e.insertText(mark)
                    e.setPosition(a); e.insertText(mark)
                    spans.append((a + n, b + n, 2 * n))
            if spans:
                # последний сегмент сдвинут правками всех, что выше него
                first = spans[-1][0]
                end = spans[0][1] + sum(d for _, _, d in spans[1:])
                c.setPosition(first)
                c.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        c.endEditBlock()
        self.setTextCursor(c)
        self.setFocus()

    LINE_KINDS = {
        "h2": (r"#{1,6}\s+", lambda i: "## "),
        "h3": (r"#{1,6}\s+", lambda i: "### "),
        "quote": (r">\s?", lambda i: "> "),
        "ul": (r"(?:[-*+]|\d+[.)])\s+", lambda i: "- "),
        "ol": (r"(?:[-*+]|\d+[.)])\s+", lambda i: f"{i + 1}. "),
    }

    def line_prefix(self, kind: str):
        """Заголовок / цитата / список для текущей строки или всех выделенных строк (повтор — снимает)."""
        strip_rx, make = self.LINE_KINDS[kind]
        own = {"h2": r"##\s+", "h3": r"###\s+", "quote": r">\s?", "ul": r"[-*+]\s+", "ol": r"\d+[.)]\s+"}[kind]
        c = self.textCursor()
        doc = self.document()
        b0 = doc.findBlock(c.selectionStart())
        b1 = doc.findBlock(c.selectionEnd())
        if c.hasSelection() and b1 != b0 and c.selectionEnd() == b1.position():
            b1 = b1.previous()
        blocks, b = [], b0
        while b.isValid():
            blocks.append(b)
            if b == b1:
                break
            b = b.next()
        filled = [b for b in blocks if b.text().strip()] or blocks
        remove = all(re.match("^" + own, b.text()) for b in filled)
        c.beginEditBlock()
        i = 0
        for b in blocks:
            t = b.text()
            if not t.strip() and len(blocks) > 1:
                continue
            body = re.sub("^" + strip_rx, "", t)
            new = body if remove else make(i) + body
            i += 1
            if new != t:
                e = QTextCursor(b)
                e.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                e.insertText(new)
        c.endEditBlock()
        end = QTextCursor(blocks[-1])
        end.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        self.setTextCursor(end)
        self.setFocus()

    def _ensure_own_paragraph(self, c: QTextCursor, text: str) -> str:
        before = self._text(c.selectionStart() - 2, c.selectionStart()).replace(PS, "\n")
        after = self._text(c.selectionEnd(), c.selectionEnd() + 2).replace(PS, "\n")
        at_start = c.selectionStart() == 0
        pre = "" if at_start or before.endswith("\n\n") else ("\n" if before.endswith("\n") else "\n\n")
        post = "" if after.startswith("\n\n") else ("\n" if after.startswith("\n") or not after else "\n\n")
        return pre + text + post

    def code_block(self):
        c = self.textCursor()
        sel = c.selectedText().replace(PS, "\n")
        c.beginEditBlock()
        inner = sel or "код"
        block = "```\n" + inner + "\n```"
        start_text = self._ensure_own_paragraph(c, block)
        pos = c.selectionStart()
        c.insertText(start_text)
        c.endEditBlock()
        if not sel:
            off = start_text.index("```\n") + 4
            c.setPosition(pos + off)
            c.setPosition(pos + off + len(inner), QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(c)
        self.setFocus()

    def horizontal_rule(self):
        c = self.textCursor()
        c.clearSelection()
        c.insertText(self._ensure_own_paragraph(c, "---"))
        self.setTextCursor(c)
        self.setFocus()

    def insert_link(self):
        c = self.textCursor()
        if not c.hasSelection():
            # курсор внутри существующей ссылки [..](..) — редактируем её
            blk = c.block()
            col = c.position() - blk.position()
            for m in re.finditer(r"(?<!!)\[[^\]\n]*\]\([^)\n]*\)", blk.text()):
                if m.start() <= col <= m.end():
                    c.setPosition(blk.position() + m.start())
                    c.setPosition(blk.position() + m.end(), QTextCursor.MoveMode.KeepAnchor)
                    break
        sel = c.selectedText().replace(PS, " ").strip()
        text, url = sel, ""
        m = MD_LINK_RE.match(sel)
        if m:
            text, url = m.group(1), m.group(2)
        elif URL_RE.match(sel):
            text, url = "", sel
        else:
            clip = QGuiApplication.clipboard().text().strip()
            if URL_RE.match(clip) and "\n" not in clip:
                url = clip
        d = LinkDialog(text, url, self)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        md = d.markdown()
        c.insertText(md)
        self.setTextCursor(c)
        self.setFocus()

    def insertFromMimeData(self, md):
        # вставка адреса поверх выделенного текста превращает его в ссылку
        c = self.textCursor()
        if md.hasText() and c.hasSelection() and not md.hasImage():
            url = md.text().strip()
            sel = c.selectedText()
            if URL_RE.match(url) and "\n" not in url and PS not in sel and not URL_RE.match(sel.strip()):
                c.insertText(f"[{sel}]({md_url(normalize_url(url))})")
                self.setTextCursor(c)
                return
        super().insertFromMimeData(md)


# ======================================================================= панель

def _icon(name: str) -> QIcon:
    return QIcon.fromTheme(name)


class FormatBar(QToolBar):
    """Панель форматирования над полем текста. Сочетания клавиш работают, когда фокус в поле."""

    def __init__(self, edit: MarkdownEdit, parent=None):
        super().__init__(parent)
        self.edit = edit
        self.setIconSize(self.iconSize() * 0.85)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setStyleSheet("QToolBar { border: none; spacing: 1px; padding: 0; }")

        def act(text, theme, tip, fn, keys=None, font=None):
            a = QAction(_icon(theme), text, self)
            a.setToolTip(tip + (f"  ({QKeySequence(keys).toString(QKeySequence.SequenceFormat.NativeText)})"
                                if keys else ""))
            a.triggered.connect(fn)
            if keys:
                a.setShortcut(QKeySequence(keys))
                a.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
                edit.addAction(a)
            self.addAction(a)
            if a.icon().isNull():  # нет значка в теме — показываем текст
                btn = self.widgetForAction(a)
                if isinstance(btn, QToolButton):
                    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
                    if font:
                        f = btn.font()
                        font(f)
                        btn.setFont(f)
            return a

        act("Ж", "format-text-bold", "Жирный: **текст**", lambda: edit.wrap("**", "жирный"), "Ctrl+B",
            lambda f: f.setBold(True))
        act("К", "format-text-italic", "Курсив: *текст*", lambda: edit.wrap("*", "курсив"), "Ctrl+I",
            lambda f: f.setItalic(True))
        act("З", "format-text-strikethrough", "Зачёркнутый: ~~текст~~", lambda: edit.wrap("~~", "текст"),
            "Ctrl+Shift+X", lambda f: f.setStrikeOut(True))
        act("</>", "format-text-code", "Код в строке: `текст`", lambda: edit.wrap("`", "код"), "Ctrl+E")
        self.addSeparator()
        act("🔗 Ссылка", "insert-link", "Ссылка: [текст](адрес).\n"
            "Подсказка: выделите слово и вставьте адрес через Ctrl+V — получится ссылка.\n"
            "Курсор внутри ссылки — откроет её для правки.", edit.insert_link, "Ctrl+K")
        act("🖼", "insert-image", "Картинка у курсора (сожмётся в WebP, без EXIF)", edit.image_requested.emit)
        self.addSeparator()
        act("H2", "format-text-heading-2", "Заголовок раздела: ## текст", lambda: edit.line_prefix("h2"), "Ctrl+2",
            lambda f: f.setBold(True))
        act("H3", "format-text-heading-3", "Подзаголовок: ### текст", lambda: edit.line_prefix("h3"), "Ctrl+3",
            lambda f: f.setBold(True))
        act("•", "format-list-unordered", "Маркированный список: - пункт", lambda: edit.line_prefix("ul"),
            "Ctrl+Shift+8")
        act("1.", "format-list-ordered", "Нумерованный список: 1. пункт", lambda: edit.line_prefix("ol"),
            "Ctrl+Shift+7")
        act("❝", "format-text-blockquote", "Цитата: > текст", lambda: edit.line_prefix("quote"), "Ctrl+Shift+.")
        self.addSeparator()
        act("{ }", "code-context", "Блок кода: ```", edit.code_block)
        act("—", "newline", "Горизонтальная линия: ---", edit.horizontal_rule)
