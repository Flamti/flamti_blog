#!/usr/bin/env python3
"""Blog Studio — графическая оболочка для ведения Hugo-блога с редактором Obsidian."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import traceback
import urllib.parse
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (QFileSystemWatcher, QObject, QProcess, QSettings, QSize, Qt,
                            QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import (QAction, QDesktopServices, QGuiApplication, QIcon, QImage,
                           QImageReader, QKeySequence, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
                               QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
                               QScrollArea, QSizePolicy, QSlider, QSplitter, QStyle, QTabWidget, QToolBar,
                               QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
from editor import FormatBar, MarkdownEdit  # noqa: E402

REPO = Path(os.environ.get("BLOG_REPO") or Path(__file__).resolve().parents[2])
HUGO_PORT = 1313
MAX_SIDES = [(1280, "1280 px"), (1600, "1600 px"), (1920, "1920 px"), (2560, "2560 px"), (0, "Не уменьшать")]


def icon(name: str, fallback=QStyle.StandardPixmap.SP_FileIcon) -> QIcon:
    ic = QIcon.fromTheme(name)
    return ic if not ic.isNull() else QApplication.style().standardIcon(fallback)


def open_url(url: str):
    """Открывает ссылку/файл через xdg-open (QDesktopServices под Wayland иногда молча ничего не делает)."""
    ok, _pid = QProcess.startDetached("xdg-open", [url])
    if not ok:
        QDesktopServices.openUrl(QUrl(url))


def open_in_obsidian(path: Path):
    open_url("obsidian://open?path=" + urllib.parse.quote(str(path), safe=""))


def mime_payload(md, for_editor=False):
    """Извлекает из перетаскивания/буфера картинки: ('files', [Path]) | ('image', QImage) | ('remote', [url])."""
    if md is None:
        return None
    if md.hasUrls():
        local = [Path(u.toLocalFile()) for u in md.urls() if u.isLocalFile()]
        imgs = [p for p in local if p.suffix.lower() in core.IMAGE_EXTS]
        if imgs:
            return ("files", imgs)
    if md.hasImage() and not (for_editor and md.hasText() and not md.hasUrls()):
        img = QImage(md.imageData())
        if not img.isNull():
            return ("image", img)
    if md.hasUrls():
        remote = [u.toString() for u in md.urls() if u.scheme() in ("http", "https")
                  and Path(u.path()).suffix.lower() in core.IMAGE_EXTS]
        if remote:
            return ("remote", remote)
    return None


class BodyEdit(MarkdownEdit):
    """Поле текста поста: картинки, вставленные/перетащенные сюда, попадают в позицию курсора."""
    media = Signal(object)

    def canInsertFromMimeData(self, md):
        return mime_payload(md, True) is not None or super().canInsertFromMimeData(md)

    def insertFromMimeData(self, md):
        payload = mime_payload(md, True)
        if payload:
            QTimer.singleShot(0, lambda: self.media.emit(payload))
            return
        super().insertFromMimeData(md)


# ======================================================================= фон

class Worker(QThread):
    log = Signal(str)
    done = Signal(bool, object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(True, self.fn(self.log.emit))
        except Exception as e:  # noqa: BLE001
            self.done.emit(False, str(e) or traceback.format_exc())


# ======================================================================= новый пост

class NewPostDialog(QDialog):
    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Новый пост")
        self.setMinimumWidth(520)
        self.slug_touched = False
        self.today = datetime.now()

        lay = QVBoxLayout(self)
        kinds = QHBoxLayout()
        self.rb_note = QRadioButton("Заметка — коротко, без оглавления")
        self.rb_article = QRadioButton("Статья — с оглавлением и обложкой")
        (self.rb_note if kind == "note" else self.rb_article).setChecked(True)
        kinds.addWidget(self.rb_note)
        kinds.addWidget(self.rb_article)
        lay.addLayout(kinds)

        form = QFormLayout()
        self.titles = {}
        for l in core.LANGS:
            e = QLineEdit()
            e.textChanged.connect(self.update_hint)
            self.titles[l] = e
            form.addRow(f"Заголовок ({l.upper()}):", e)
        self.slug = QLineEdit()
        self.slug.textEdited.connect(lambda: setattr(self, "slug_touched", True))
        form.addRow("Папка / адрес:", self.slug)
        self.url_hint = QLabel()
        self.url_hint.setStyleSheet("color: gray")
        form.addRow("", self.url_hint)
        lay.addLayout(form)

        self.note_hint = QLabel()
        self.note_hint.setWordWrap(True)
        self.note_hint.setStyleSheet("color: gray")
        lay.addWidget(self.note_hint)

        self.open_obs = QCheckBox("Сразу открыть в Obsidian (иначе пишите прямо здесь)")
        self.open_obs.setChecked(QSettings("blog-studio", "blog-studio").value("open_obsidian", "false") in (True, "true"))
        lay.addWidget(self.open_obs)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Создать")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.rb_note.toggled.connect(self.update_hint)
        self.slug.textChanged.connect(self.update_url)
        self.update_hint()
        self.titles["ru"].setFocus()

    def kind(self):
        return "note" if self.rb_note.isChecked() else "article"

    def update_hint(self):
        if not self.slug_touched:
            self.slug.setText(core.suggest_slug(self.titles["ru"].text(), self.today))
        if self.kind() == "note":
            self.note_hint.setText("Пустые заголовки заметки заполнятся датой: «"
                                   + core.human_date(self.today, "ru") + "», «"
                                   + core.human_date(self.today, "en") + "», …")
        else:
            self.note_hint.setText("Заголовки можно оставить пустыми и заполнить позже.")

    def update_url(self):
        self.url_hint.setText(core.post_url(core.site_url(REPO) or "/", self.slug.text() or "…"))

    def result_data(self):
        titles = {l: e.text().strip() for l, e in self.titles.items()}
        if self.kind() == "note":
            for l in core.LANGS:
                titles[l] = titles[l] or core.human_date(self.today, l)
        return self.kind(), core.slugify(self.slug.text(), 100) or core.suggest_slug("", self.today), titles


# ======================================================================= публикация

class PublishDialog(QDialog):
    def __init__(self, win: "MainWindow", post: core.Post, unpublish=False):
        super().__init__(win)
        self.win, self.post, self.unpublish = win, post, unpublish
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(("Снять с публикации: " if unpublish else "Публикация: ") + post.title())
        self.resize(640, 520)
        lay = QVBoxLayout(self)

        self.status = QLabel()
        self.status.setWordWrap(True)
        f = self.status.font()
        f.setPointSizeF(f.pointSizeF() * 1.15)
        self.status.setFont(f)
        lay.addWidget(self.status)

        self.issues = QListWidget()
        lay.addWidget(self.issues, 1)

        self.other_box = QCheckBox()
        self.other_box.setVisible(False)
        lay.addWidget(self.other_box)

        form = QFormLayout()
        was_tracked = bool(core.git(REPO, "log", "-1", "--format=%h", "--",
                                    f"content/posts/{post.slug}", check=False).strip())
        if unpublish:
            msg = f"Снято с публикации: {post.title()}"
        elif post.draft or not was_tracked:
            msg = f"{core.KINDS[post.kind]}: {post.title()}"
        else:
            msg = f"Правка: {post.title()}"
        self.msg = QLineEdit(msg)
        form.addRow("Сообщение коммита:", self.msg)
        lay.addLayout(form)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setVisible(False)
        lay.addWidget(self.log, 1)

        btns = QHBoxLayout()
        self.open_btn = QPushButton(icon("internet-web-browser"), "Открыть на сайте")
        self.open_btn.setVisible(False)
        self.open_btn.clicked.connect(lambda: open_url(self.live_url))
        btns.addWidget(self.open_btn)
        btns.addStretch()
        self.go = QPushButton(icon("document-send"), "Снять с публикации" if unpublish else "Опубликовать")
        self.go.setDefault(True)
        self.go.setEnabled(False)
        self.go.clicked.connect(self.start_publish)
        self.close_btn = QPushButton("Отмена")
        self.close_btn.clicked.connect(self.reject)
        btns.addWidget(self.go)
        btns.addWidget(self.close_btn)
        lay.addLayout(btns)

        self.live_url = core.post_url(core.site_url(REPO), post.slug)
        self.status.setText("⏳ Проверяю пост и собираю сайт…")
        self.worker = Worker(self.run_checks, self)
        self.worker.done.connect(self.checks_done)
        self.worker.start()

    def run_checks(self, log):
        issues = [] if self.unpublish else core.check_post(self.post)
        ok, out = core.hugo_check(REPO)
        if not ok:
            issues.append(core.Issue("error", "Hugo не собирает сайт:\n" + out[-1500:]))
        changes = core.git_changes(REPO)
        prefix = f"content/posts/{self.post.slug}/"
        others = [c for c in changes if not c.startswith(prefix)]
        return issues, others

    def checks_done(self, ok, result):
        if not ok:
            self.status.setText("❌ Ошибка проверки:\n" + str(result))
            return
        issues, self.others = result
        errors = [i for i in issues if i.level == "error"]
        for i in issues:
            it = QListWidgetItem(("⛔ " if i.level == "error" else "⚠️ ") + i.text)
            self.issues.addItem(it)
        if not issues:
            self.issues.addItem("✅ Всё в порядке: три языка заполнены, картинки на месте, Hugo собирает сайт.")
        if self.others:
            shown = ", ".join(self.others[:4]) + (" …" if len(self.others) > 4 else "")
            self.other_box.setText(f"Также отправить другие изменения в репозитории ({len(self.others)}): {shown}")
            self.other_box.setToolTip("\n".join(self.others))
            self.other_box.setVisible(True)
        if errors:
            self.status.setText(f"⛔ Найдено ошибок: {len(errors)}. Исправьте их и откройте публикацию снова.")
            self.go.setEnabled(False)
        elif issues:
            self.status.setText("⚠️ Есть предупреждения — можно публиковать, но лучше проверить.")
            self.go.setText(self.go.text() + " всё равно")
            self.go.setEnabled(True)
        else:
            self.status.setText("✅ Готово к отправке.")
            self.go.setEnabled(True)

    def start_publish(self):
        self.go.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.issues.setVisible(False)
        self.other_box.setEnabled(False)
        self.msg.setEnabled(False)
        self.log.setVisible(True)
        self.status.setText("🚀 Отправляю…")
        include_other = self.other_box.isVisible() and self.other_box.isChecked()
        post, msg, unpublish = self.post, self.msg.text().strip() or "Update blog", self.unpublish

        def job(log):
            core.set_published(post, not unpublish)
            log("Статус в файлах: " + ("черновик" if unpublish else "опубликован"))
            paths = ["."] if include_other else [f"content/posts/{post.slug}"]
            core.commit_and_push(REPO, paths, msg, log)
            return True

        self.worker = Worker(job, self)
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.done.connect(self.publish_done)
        self.worker.start()

    def publish_done(self, ok, result):
        self.close_btn.setEnabled(True)
        self.close_btn.setText("Закрыть")
        self.win.refresh()
        if not ok:
            self.status.setText("❌ Не удалось отправить. Изменения сохранены локально — "
                                "можно повторить позже кнопкой «Отправить» в главном окне.")
            self.log.appendPlainText("\n" + str(result))
            return
        if self.unpublish:
            self.status.setText("✅ Пост снят с публикации. Сайт пересоберётся через минуту-две.")
            return
        self.status.setText("✅ Отправлено! Жду, пока Codeberg соберёт сайт…")
        self.open_btn.setVisible(True)
        self.win.watch_live(self.live_url, self.post.title())

    def reject(self):
        if self.worker.isRunning():
            return  # не закрываем, пока идёт проверка/отправка
        super().reject()

    def live_ok(self):
        self.status.setText("🎉 Пост на сайте!")


# ======================================================================= картинки

class DropSplitter(QSplitter):
    """Центральная область окна: картинку можно бросить куда угодно."""
    dropped = Signal(object)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if mime_payload(e.mimeData()):
            e.setDropAction(Qt.DropAction.CopyAction)
            e.accept()

    def dragMoveEvent(self, e):
        e.setDropAction(Qt.DropAction.CopyAction)
        e.accept()

    def dropEvent(self, e):
        payload = mime_payload(e.mimeData())
        e.setDropAction(Qt.DropAction.CopyAction)
        e.accept()
        QTimer.singleShot(0, lambda: self.dropped.emit(payload))


class ImageList(QListWidget):
    files_dropped = Signal(object)

    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setIconSize(QSize(150, 110))
        self.setGridSize(QSize(180, 185))
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setWordWrap(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setMinimumHeight(190)
        self.placeholder = "Перетащите сюда картинки или нажмите Ctrl+V"

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() == 0:
            from PySide6.QtGui import QPainter
            p = QPainter(self.viewport())
            p.setPen(self.palette().placeholderText().color())
            p.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.setDropAction(Qt.DropAction.CopyAction)
            e.accept()

    def dragMoveEvent(self, e):
        e.setDropAction(Qt.DropAction.CopyAction)
        e.accept()

    def dropEvent(self, e):
        payload = mime_payload(e.mimeData())  # данные нужно забрать до конца события
        e.setDropAction(Qt.DropAction.CopyAction)
        e.accept()
        QTimer.singleShot(0, lambda: self.files_dropped.emit(payload))


_thumb_cache: dict[tuple, QIcon] = {}


def thumb(path: Path) -> QIcon:
    key = (str(path), path.stat().st_mtime)
    if key not in _thumb_cache:
        r = QImageReader(str(path))
        r.setAutoTransform(True)
        sz = r.size()
        if sz.isValid():
            sz.scale(300, 220, Qt.AspectRatioMode.KeepAspectRatio)
            r.setScaledSize(sz)
        img = r.read()
        _thumb_cache[key] = QIcon(QPixmap.fromImage(img)) if not img.isNull() else icon("image-x-generic")
    return _thumb_cache[key]


# ======================================================================= главное окно

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("blog-studio", "blog-studio")
        self.setWindowTitle("Blog Studio")
        self.setWindowIcon(icon("story-editor", QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.posts: list[core.Post] = []
        self.current: core.Post | None = None
        self.hugo: QProcess | None = None
        self.pending_preview: str | None = None
        self.live_checks: list[tuple[str, str, float]] = []
        self.dirty: set[str] = set()

        self.build_toolbar()
        self.build_ui()

        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(lambda _: self.refresh_timer.start())
        self.watcher.fileChanged.connect(lambda _: self.refresh_timer.start())
        self.refresh_timer = QTimer(self, singleShot=True, interval=400)
        self.refresh_timer.timeout.connect(self.refresh)

        self.live_timer = QTimer(self, interval=15000)
        self.live_timer.timeout.connect(self.poll_live)

        QShortcut(QKeySequence.StandardKey.Paste, self, activated=self.paste_clipboard)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=lambda: self.new_post("note"))
        QShortcut(QKeySequence("Ctrl+Shift+N"), self, activated=lambda: self.new_post("article"))
        QShortcut(QKeySequence("F5"), self, activated=self.refresh)

        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        else:
            self.resize(1200, 800)
        st = self.settings.value("splitter")
        if st:
            self.splitter.restoreState(st)
        self.refresh()
        last = self.settings.value("last_post")
        self.select_slug(last) if last else None

    # ------------------------------------------------------------ UI

    def build_toolbar(self):
        tb = QToolBar("Основное")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        a = QAction(icon("document-new"), "Новая заметка", self)
        a.setToolTip("Ctrl+N")
        a.triggered.connect(lambda: self.new_post("note"))
        tb.addAction(a)
        a = QAction(icon("document-new-from-template", QStyle.StandardPixmap.SP_FileIcon), "Новая статья", self)
        a.setToolTip("Ctrl+Shift+N")
        a.triggered.connect(lambda: self.new_post("article"))
        tb.addAction(a)
        tb.addSeparator()
        a = QAction(icon("view-refresh", QStyle.StandardPixmap.SP_BrowserReload), "Обновить", self)
        a.setToolTip("F5")
        a.triggered.connect(self.refresh)
        tb.addAction(a)
        a = QAction(icon("internet-web-browser"), "Сайт", self)
        a.triggered.connect(lambda: open_url(core.site_url(REPO)))
        tb.addAction(a)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.push_btn = QPushButton(icon("go-up"), "")
        self.push_btn.setVisible(False)
        self.push_btn.clicked.connect(self.push_pending)
        tb.addWidget(self.push_btn)
        self.hugo_label = QLabel()
        tb.addWidget(self.hugo_label)
        self.hugo_stop = QToolButton()
        self.hugo_stop.setIcon(icon("process-stop", QStyle.StandardPixmap.SP_BrowserStop))
        self.hugo_stop.setToolTip("Остановить сервер предпросмотра")
        self.hugo_stop.setVisible(False)
        self.hugo_stop.clicked.connect(self.stop_hugo)
        tb.addWidget(self.hugo_stop)

    def build_ui(self):
        self.splitter = DropSplitter()
        self.splitter.dropped.connect(lambda payload: self.handle_media(payload))
        self.setCentralWidget(self.splitter)

        # ---- слева: список постов
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(6, 6, 0, 6)
        fl = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItems(["Все", "Черновики", "Опубликованные"])
        self.filter.setCurrentIndex(int(self.settings.value("filter", 0)))
        self.filter.currentIndexChanged.connect(self.fill_list)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.fill_list)
        fl.addWidget(self.filter)
        fl.addWidget(self.search, 1)
        ll.addLayout(fl)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Дата", "Заголовок", "Тип", "Статус", "Языки"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        h = self.tree.header()
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in (0, 2, 3, 4):
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.currentItemChanged.connect(self.on_select)
        self.tree.itemDoubleClicked.connect(lambda *_: self.current and open_in_obsidian(self.current.langs["ru"].path))
        ll.addWidget(self.tree)
        self.splitter.addWidget(left)

        # ---- справа: детали
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.detail = QWidget()
        self.detail_scroll.setWidget(self.detail)
        dl = QVBoxLayout(self.detail)

        self.h_title = QLabel()
        f = self.h_title.font()
        f.setPointSizeF(f.pointSizeF() * 1.6)
        f.setBold(True)
        self.h_title.setFont(f)
        self.h_title.setWordWrap(True)
        dl.addWidget(self.h_title)
        self.h_sub = QLabel()
        self.h_sub.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.h_sub.setStyleSheet("color: gray")
        dl.addWidget(self.h_sub)

        actions = QHBoxLayout()
        self.obs_btns = {}
        for l in core.LANGS:
            b = QPushButton(icon("obsidian", QStyle.StandardPixmap.SP_FileIcon), l.upper())
            b.setToolTip(f"Открыть index.{l}.md в Obsidian")
            b.clicked.connect(lambda _=False, l=l: self.current and open_in_obsidian(self.current.langs[l].path))
            actions.addWidget(b)
            self.obs_btns[l] = b
        b = QPushButton(icon("folder-open", QStyle.StandardPixmap.SP_DirOpenIcon), "Папка")
        b.clicked.connect(lambda: self.current and open_url(str(self.current.dir)))
        actions.addWidget(b)
        actions.addStretch()
        self.preview_btn = QPushButton(icon("document-preview", QStyle.StandardPixmap.SP_FileDialogContentsView), "Предпросмотр")
        self.preview_btn.clicked.connect(self.preview)
        actions.addWidget(self.preview_btn)
        self.publish_btn = QPushButton(icon("document-send", QStyle.StandardPixmap.SP_ArrowUp), "Опубликовать")
        self.publish_btn.setStyleSheet("QPushButton { font-weight: bold; padding: 4px 14px; }")
        self.publish_btn.clicked.connect(lambda: self.publish(False))
        actions.addWidget(self.publish_btn)
        more = QToolButton()
        more.setText("⋯")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        m = QMenu(more)
        m.addAction("Копировать ссылку на пост", self.copy_post_url)
        m.addAction("Открыть на сайте", lambda: self.current and open_url(core.post_url(core.site_url(REPO), self.current.slug)))
        m.addSeparator()
        self.unpublish_act = m.addAction("Снять с публикации…", lambda: self.publish(True))
        self.delete_act = m.addAction(icon("edit-delete"), "Удалить черновик…", self.delete_post)
        more.setMenu(m)
        actions.addWidget(more)
        dl.addLayout(actions)

        # текст по языкам
        self.lang_tabs = QTabWidget()
        self.title_edits, self.tag_edits, self.body_edits, self.word_lbls = {}, {}, {}, {}
        self.fmt_bars = {}
        self.saved_body: dict[str, str] = {}
        self.body_timers = {}
        for l in core.LANGS:
            page = QWidget()
            pl = QVBoxLayout(page)
            form = QFormLayout()
            te = QLineEdit()
            te.setPlaceholderText("Заголовок")
            te.editingFinished.connect(lambda l=l: self.save_meta(l))
            ge = QLineEdit()
            ge.setPlaceholderText("через запятую")
            ge.editingFinished.connect(lambda l=l: self.save_meta(l))
            form.addRow("Заголовок:", te)
            form.addRow("Теги:", ge)
            pl.addLayout(form)
            be = BodyEdit()
            be.setPlaceholderText({"ru": "Текст поста (Markdown)… Картинки можно вставлять сюда через Ctrl+V или перетаскиванием.",
                                   "en": "Post text (Markdown)…", "es": "Texto (Markdown)…"}[l])
            be.setMinimumHeight(300)
            bf = be.font()
            bf.setPointSizeF(bf.pointSizeF() * 1.1)
            be.setFont(bf)
            be.textChanged.connect(lambda l=l: self.on_body_changed(l))
            be.media.connect(lambda payload, l=l: self.handle_media(payload, l))
            be.image_requested.connect(lambda l=l: self.add_files_dialog(l))
            fb = FormatBar(be)
            self.fmt_bars[l] = fb
            pl.addWidget(fb)
            pl.addWidget(be, 1)
            wl = QLabel()
            wl.setStyleSheet("color: gray")
            pl.addWidget(wl)
            t = QTimer(self, singleShot=True, interval=700)
            t.timeout.connect(lambda l=l: self.save_body(l))
            self.title_edits[l], self.tag_edits[l], self.body_edits[l], self.word_lbls[l] = te, ge, be, wl
            self.body_timers[l] = t
            self.lang_tabs.addTab(page, l.upper())
        dl.addWidget(self.lang_tabs, 3)

        # картинки
        imgs = QGroupBox("Картинки")
        il = QVBoxLayout(imgs)
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Качество WebP:"))
        self.quality = QSlider(Qt.Orientation.Horizontal)
        self.quality.setRange(40, 100)
        self.quality.setValue(int(self.settings.value("quality", 82)))
        self.quality.setMaximumWidth(160)
        self.quality_lbl = QLabel()
        self.quality.valueChanged.connect(self.on_quality)
        opts.addWidget(self.quality)
        opts.addWidget(self.quality_lbl)
        opts.addSpacing(16)
        opts.addWidget(QLabel("Макс. сторона:"))
        self.max_side = QComboBox()
        for v, t in MAX_SIDES:
            self.max_side.addItem(t, v)
        self.max_side.setCurrentIndex(max(0, self.max_side.findData(int(self.settings.value("max_side", 1600)))))
        self.max_side.currentIndexChanged.connect(lambda: self.settings.setValue("max_side", self.max_side.currentData()))
        opts.addWidget(self.max_side)
        opts.addSpacing(16)
        self.add_links = QCheckBox("Добавлять ссылку во все языки")
        self.add_links.setChecked(self.settings.value("add_links", "true") in (True, "true"))
        self.add_links.setToolTip("При вставке в текст: в текущий язык — у курсора, в остальные — в конец.\n"
                                  "Без галочки ссылка попадает только туда, куда вставили (или в буфер).")
        self.add_links.toggled.connect(lambda v: self.settings.setValue("add_links", v))
        opts.addWidget(self.add_links)
        opts.addStretch()
        il.addLayout(opts)
        self.on_quality(self.quality.value())

        self.images = ImageList()
        self.images.files_dropped.connect(lambda payload: self.handle_media(payload))
        self.images.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.images.customContextMenuRequested.connect(self.image_menu)
        self.images.itemDoubleClicked.connect(lambda it: open_url(str(self.current.dir / it.data(Qt.ItemDataRole.UserRole))))
        il.addWidget(self.images)
        ib = QHBoxLayout()
        b = QPushButton(icon("list-add"), "Добавить файлы…")
        b.clicked.connect(self.add_files_dialog)
        ib.addWidget(b)
        b = QPushButton(icon("edit-paste"), "Вставить из буфера")
        b.setToolTip("Ctrl+V")
        b.clicked.connect(self.paste_clipboard)
        ib.addWidget(b)
        self.process_btn = QPushButton(icon("tools-wizard", QStyle.StandardPixmap.SP_DialogApplyButton), "Обработать картинки из Obsidian")
        self.process_btn.setToolTip("Сжать, удалить EXIF и переписать ссылки для картинок, вставленных прямо в Obsidian")
        self.process_btn.clicked.connect(self.process_all)
        ib.addWidget(self.process_btn)
        ib.addStretch()
        hint = QLabel("ПКМ по картинке — обложка, ссылка, пережать, удалить")
        hint.setStyleSheet("color: gray")
        ib.addWidget(hint)
        il.addLayout(ib)
        dl.addWidget(imgs, 1)

        # проверка
        chk = QGroupBox("Проверка перед публикацией")
        cl = QVBoxLayout(chk)
        self.issues = QLabel()
        self.issues.setWordWrap(True)
        self.issues.setTextFormat(Qt.TextFormat.RichText)
        cl.addWidget(self.issues)
        self.fix_links_btn = QPushButton(icon("tools-wizard", QStyle.StandardPixmap.SP_DialogApplyButton),
                                         "Исправить ссылки во всех языках")
        self.fix_links_btn.setToolTip("[текст]{адрес} → [текст](адрес)")
        self.fix_links_btn.clicked.connect(self.fix_links)
        self.fix_links_btn.setVisible(False)
        cl.addWidget(self.fix_links_btn, 0, Qt.AlignmentFlag.AlignLeft)
        dl.addWidget(chk)

        self.empty = QLabel("Выберите пост слева или создайте новый (Ctrl+N — заметка, Ctrl+Shift+N — статья)")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("color: gray; font-size: 14pt")
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.empty)
        rl.addWidget(self.detail_scroll)
        self.splitter.addWidget(right)
        self.splitter.setSizes([430, 770])
        self.detail_scroll.setVisible(False)

    # ------------------------------------------------------------ список

    def refresh(self):
        self.posts = core.list_posts(REPO)
        changes = core.git_changes(REPO)
        self.dirty = core.dirty_slugs(changes)
        n = core.unpushed_count(REPO)
        self.push_btn.setText(f"Не отправлено коммитов: {n} — отправить")
        self.push_btn.setVisible(n > 0)
        self.fill_list()
        # следим за папкой постов и текущим постом
        paths = [str(REPO / "content" / "posts")]
        if self.current and self.current.dir.exists():
            paths += [str(self.current.dir)] + [str(lf.path) for lf in self.current.langs.values() if lf.path.exists()]
        old = self.watcher.files() + self.watcher.directories()
        if old:
            self.watcher.removePaths(old)
        self.watcher.addPaths(paths)

    def status_of(self, p: core.Post) -> str:
        if p.draft:
            return "Черновик"
        return "Опубликован ✎" if p.slug in self.dirty else "Опубликован"

    def fill_list(self):
        self.settings.setValue("filter", self.filter.currentIndex())
        cur = self.current.slug if self.current else None
        q = self.search.text().lower().strip()
        flt = self.filter.currentIndex()
        self.tree.blockSignals(True)
        self.tree.clear()
        select = None
        for p in self.posts:
            if flt == 1 and not p.draft or flt == 2 and p.draft:
                continue
            if q and q not in (p.slug + " " + " ".join(lf.title for lf in p.langs.values())).lower():
                continue
            langs = " ".join(l if p.langs[l].complete else "··" for l in core.LANGS)
            it = QTreeWidgetItem([p.date[:10], p.title() or p.slug, core.KINDS[p.kind], self.status_of(p), langs])
            it.setData(0, Qt.ItemDataRole.UserRole, p.slug)
            it.setToolTip(1, p.slug)
            if p.draft:
                for c in range(5):
                    it.setForeground(c, self.palette().placeholderText())
            self.tree.addTopLevelItem(it)
            if p.slug == cur:
                select = it
        self.tree.blockSignals(False)
        if select:
            self.tree.setCurrentItem(select)
            self.load_post(next(p for p in self.posts if p.slug == cur), keep_edits=True)
        elif cur and not any(p.slug == cur for p in self.posts):
            self.current = None
            self.show_detail(False)

    def select_slug(self, slug):
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.data(0, Qt.ItemDataRole.UserRole) == slug:
                self.tree.setCurrentItem(it)
                return

    def on_select(self, it, _prev=None):
        if not it:
            return
        slug = it.data(0, Qt.ItemDataRole.UserRole)
        post = next((p for p in self.posts if p.slug == slug), None)
        if post:
            if self.current and self.current.slug != slug:
                self.flush_bodies()
            self.settings.setValue("last_post", slug)
            self.current = post
            self.load_post(post)
            self.refresh_timer.start()  # переподписать watcher

    def show_detail(self, on: bool):
        self.detail_scroll.setVisible(on)
        self.empty.setVisible(not on)

    # ------------------------------------------------------------ детали

    def load_post(self, p: core.Post, keep_edits=False):
        self.current = p
        self.show_detail(True)
        self.h_title.setText(p.title() or p.slug)
        url = core.post_url(core.site_url(REPO), p.slug)
        self.h_sub.setText(f"{core.KINDS[p.kind]} · {self.status_of(p)} · {p.date[:16].replace('T', ' ')} · {url}")
        switched = getattr(self, "_loaded_slug", None) != p.slug
        self._loaded_slug = p.slug
        for i, l in enumerate(core.LANGS):
            lf = p.langs[l]
            self.lang_tabs.setTabText(i, ("✅ " if lf.complete else "✏️ ") + l.upper())
            self.lang_tabs.setTabToolTip(i, "Заголовок и текст заполнены" if lf.complete else "Не заполнен заголовок или текст")
            self.load_body(l, lf, switched)
            for edit, val in ((self.title_edits[l], lf.title), (self.tag_edits[l], ", ".join(lf.tags))):
                if keep_edits and edit.hasFocus():
                    continue
                if edit.text() != val:
                    edit.setText(val)
            self.title_edits[l].setPlaceholderText("Заголовок" if lf.exists else "файла нет")
            for w in (self.title_edits[l], self.tag_edits[l], self.body_edits[l], self.fmt_bars[l]):
                w.setEnabled(lf.exists)
        self.unpublish_act.setEnabled(not p.draft)
        self.delete_act.setEnabled(p.draft)
        self.publish_btn.setText("Опубликовать" if p.draft else "Отправить правки")
        self.fill_images(p)
        self.fill_issues(p)

    @staticmethod
    def norm_body(body: str) -> str:
        return "" if body.strip() in core.TEMPLATE_BODIES else body.strip("\n")

    def load_body(self, l, lf, switched):
        be = self.body_edits[l]
        text = self.norm_body(lf.body)
        dirty = not switched and be.toPlainText() != self.saved_body.get(l, "")
        if not dirty and be.toPlainText() != text:
            pos = be.textCursor().position()
            be.blockSignals(True)
            be.setPlainText(text)
            be.blockSignals(False)
            if not switched:
                c = be.textCursor()
                c.setPosition(min(pos, len(text)))
                be.setTextCursor(c)
        if not dirty:
            self.saved_body[l] = text
        self.update_words(l)

    def update_words(self, l):
        t = self.body_edits[l].toPlainText()
        self.word_lbls[l].setText(f"{len(t.split())} слов · {len(t)} знаков · сохраняется автоматически")

    def on_body_changed(self, l):
        self.update_words(l)
        self.body_timers[l].start()

    def save_body(self, l):
        self.body_timers[l].stop()
        p = self.current
        if not p or getattr(self, "_loaded_slug", None) != p.slug:
            return
        text = self.body_edits[l].toPlainText()
        if text == self.saved_body.get(l):
            return
        lf = p.langs[l].load()
        if not lf.exists:
            return
        lf.body = "\n" + text.strip("\n") + "\n" if text.strip() else "\n"
        lf.save()
        self.saved_body[l] = text
        self.statusBar().showMessage(f"Сохранено: index.{l}.md", 1500)

    def flush_bodies(self):
        for l in core.LANGS:
            self.save_body(l)

    def handle_media(self, payload, lang=None):
        """Картинки из буфера/перетаскивания. lang — поле текста, куда вставить ссылку у курсора."""
        if not payload:
            self.statusBar().showMessage("Не нашёл картинку в перетаскиваемых данных / буфере", 4000)
            return
        kind, data = payload
        tmpdir = Path(tempfile.mkdtemp(prefix="blog-studio-"))
        try:
            if kind == "files":
                self.add_paths(data, lang=lang)
            elif kind == "image":
                tmp = tmpdir / "img.png"
                data.save(str(tmp), "PNG")
                self.add_paths([tmp], ["img.png"], lang=lang)
            elif kind == "remote":
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                try:
                    paths = [core.download(u, tmpdir) for u in data]
                except Exception as e:  # noqa: BLE001
                    QMessageBox.warning(self, "Не удалось скачать картинку", str(e))
                    return
                finally:
                    QApplication.restoreOverrideCursor()
                self.add_paths(paths, lang=lang)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def save_meta(self, l):
        self.flush_bodies()
        p = self.current
        if not p or not p.langs[l].path.exists():
            return
        lf = p.langs[l].load()
        title = self.title_edits[l].text().strip()
        tags = [t.strip() for t in self.tag_edits[l].text().split(",") if t.strip()]
        if title == lf.title and tags == lf.tags:
            return
        lf.fm_text = core.fm_set(lf.fm_text, "title", title)
        lf.fm_text = core.fm_set(lf.fm_text, "tags", tags)
        lf.save()
        self.statusBar().showMessage(f"Сохранено: index.{l}.md", 2500)
        self.refresh()

    def fill_images(self, p: core.Post):
        refs = p.referenced_images()
        sel = {i.data(Qt.ItemDataRole.UserRole) for i in self.images.selectedItems()}
        self.images.clear()
        any_raw = False
        for path in p.images():
            raw = core.needs_processing(path)
            any_raw |= raw
            size = path.stat().st_size
            marks = []
            if path.name == p.cover:
                marks.append("★ обложка")
            if raw:
                marks.append("⚠ не обработана")
            elif path.name not in refs and path.name != p.cover:
                marks.append("не используется")
            label = f"{path.name}\n{size // 1024} КБ" + ("\n" + ", ".join(marks) if marks else "")
            it = QListWidgetItem(thumb(path), label)
            it.setData(Qt.ItemDataRole.UserRole, path.name)
            it.setToolTip(f"{path.name} — {size // 1024} КБ\nИспользуется в: "
                          + (", ".join(sorted(refs.get(path.name, []))).upper() or "нигде"))
            self.images.addItem(it)
            if path.name in sel:
                it.setSelected(True)
        self.process_btn.setVisible(any_raw or any(core.WIKI_IMG.search(lf.body) for lf in p.langs.values()))

    def fill_issues(self, p: core.Post):
        issues = core.check_post(p)
        self.fix_links_btn.setVisible(any(i.fix == "links" for i in issues))
        if not issues:
            self.issues.setText("✅ Всё готово к публикации")
            return
        rows = [("⛔ " if i.level == "error" else "⚠️ ") + i.text.replace("<", "&lt;") for i in issues]
        self.issues.setText("<br>".join(rows))

    def fix_links(self):
        p = self.current
        if not p:
            return
        self.flush_bodies()
        for l in core.LANGS:
            lf = p.langs[l].load()
            if lf.exists:
                fixed = core.fix_broken_links(lf.body)
                if fixed != lf.body:
                    lf.body = fixed
                    lf.save()
        self.statusBar().showMessage("Ссылки исправлены", 4000)
        self.refresh()

    # ------------------------------------------------------------ картинки

    def on_quality(self, v):
        self.quality_lbl.setText(f"{v}")
        self.quality_lbl.setToolTip("82 — хороший баланс; 90+ — почти без потерь, но тяжелее")
        self.settings.setValue("quality", v)

    def img_opts(self):
        return self.quality.value(), (self.max_side.currentData() or None)

    def add_paths(self, paths: list[Path], display_names: list[str] | None = None, lang=None):
        p = self.current
        self.flush_bodies()
        if not p:
            QMessageBox.information(self, "Blog Studio", "Сначала выберите или создайте пост.")
            return
        q, ms = self.img_opts()
        added, errors = [], []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for i, src in enumerate(paths):
                if src.suffix.lower() not in core.IMAGE_EXTS and not (display_names and display_names[i]):
                    errors.append(f"{src.name}: не картинка")
                    continue
                try:
                    added.append(core.add_image(p, src, q, ms, display_names[i] if display_names else None))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{src.name}: {e}")
        finally:
            QApplication.restoreOverrideCursor()
        if added:
            links = "\n\n".join(core.md_link(n) for n in added)
            if lang:
                be = self.body_edits[lang]
                c = be.textCursor()
                full = be.toPlainText()
                before, after = full[:c.position()], full[c.position():]
                prefix = "" if not before or before.endswith("\n\n") else ("\n" if before.endswith("\n") else "\n\n")
                suffix = "" if after.startswith("\n\n") else ("\n" if after.startswith("\n") or not after else "\n\n")
                c.insertText(prefix + links + suffix)
                be.setTextCursor(c)
                self.save_body(lang)
                others = [l for l in core.LANGS if l != lang] if self.add_links.isChecked() else []
                for n in added:
                    p.append_image_link(n, others)
                msg = f"вставлены в текст {lang.upper()}" + (" и в конец остальных языков" if others else "")
            elif self.add_links.isChecked():
                for n in added:
                    p.append_image_link(n)
                msg = "добавлены в конец текста на всех языках"
            else:
                QGuiApplication.clipboard().setText(links)
                msg = "ссылки скопированы в буфер"
            if p.kind == "article" and not p.cover:
                p.set_cover(added[0])
                msg += "; первая стала обложкой"
            self.statusBar().showMessage(f"Картинки ({len(added)}) {msg}", 8000)
        if errors:
            QMessageBox.warning(self, "Не всё получилось", "\n".join(errors))
        self.refresh()

    def paste_clipboard(self):
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QPlainTextEdit)):
            fw.paste()
            return
        self.handle_media(mime_payload(QGuiApplication.clipboard().mimeData()))

    def add_files_dialog(self, lang=None):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Добавить картинки", str(self.settings.value("last_img_dir", Path.home() / "Pictures")),
            "Картинки (" + " ".join("*" + e for e in sorted(core.IMAGE_EXTS)) + ")")
        if files:
            self.settings.setValue("last_img_dir", str(Path(files[0]).parent))
            self.add_paths([Path(f) for f in files], lang=lang)

    def selected_images(self) -> list[str]:
        return [i.data(Qt.ItemDataRole.UserRole) for i in self.images.selectedItems()]

    def image_menu(self, pos):
        it = self.images.itemAt(pos)
        p = self.current
        if not it or not p:
            return
        if not it.isSelected():
            self.images.clearSelection()
            it.setSelected(True)
        names = self.selected_images()
        name = it.data(Qt.ItemDataRole.UserRole)
        m = QMenu(self)
        if name == p.cover:
            m.addAction("Убрать обложку", lambda: self.img_action(lambda: p.set_cover("")))
        else:
            m.addAction("★ Сделать обложкой", lambda: self.img_action(lambda: p.set_cover(name)))
        m.addAction("Копировать Markdown-ссылку", lambda: (
            QGuiApplication.clipboard().setText("\n\n".join(core.md_link(n) for n in names)),
            self.statusBar().showMessage("Скопировано — вставьте в Obsidian", 3000)))
        m.addAction("Добавить ссылку в конец всех языков",
                    lambda: self.img_action(lambda: [p.append_image_link(n) for n in names]))
        m.addSeparator()
        q, ms = self.img_opts()
        raw = [n for n in names if core.needs_processing(p.dir / n)]
        if raw:
            m.addAction("Обработать (WebP, без EXIF)",
                        lambda: self.img_action(lambda: [core.process_in_place(p, n, q, ms) for n in raw]))
        has_orig = [n for n in names if core.find_original(p.slug, Path(n).stem)]
        a = m.addAction(f"Пережать с текущими настройками ({q}, {self.max_side.currentText()})",
                        lambda: self.img_action(lambda: [core.recompress(p, n, q, ms) for n in has_orig]))
        a.setEnabled(bool(has_orig))
        if not has_orig:
            a.setToolTip("Оригинал не сохранён")
        m.addSeparator()
        m.addAction(icon("edit-delete"), "Удалить (и убрать ссылки)…", lambda: self.delete_images(names))
        m.exec(self.images.mapToGlobal(pos))

    def img_action(self, fn):
        self.flush_bodies()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Ошибка", str(e))
        finally:
            QApplication.restoreOverrideCursor()
        self.refresh()

    def delete_images(self, names):
        p = self.current
        if QMessageBox.question(self, "Удалить картинки",
                                "Удалить в корзину и убрать ссылки на них из всех языков?\n\n" + "\n".join(names)) \
                != QMessageBox.StandardButton.Yes:
            return

        def run():
            for n in names:
                p.rename_image_refs(n, None)
                core.trash(p.dir / n)
        self.img_action(run)

    def process_all(self):
        p = self.current
        q, ms = self.img_opts()

        def run():
            # вставки ![[x]] → ![](x)
            for lf in p.langs.values():
                for n in core.image_refs(lf.body)[1]:
                    p.rename_image_refs(n, n)
            for path in p.images():
                if core.needs_processing(path):
                    core.process_in_place(p, path.name, q, ms)
        self.img_action(run)
        self.statusBar().showMessage("Картинки обработаны, ссылки обновлены", 4000)

    # ------------------------------------------------------------ посты

    def new_post(self, kind):
        d = NewPostDialog(kind, self)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        kind, slug, titles = d.result_data()
        slug = core.unique_slug(REPO, slug)
        try:
            post = core.create_post(REPO, slug, kind, titles)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Не удалось создать пост", str(e))
            return
        self.current = post
        self.filter.setCurrentIndex(0)
        self.refresh()
        self.select_slug(slug)
        self.settings.setValue("open_obsidian", d.open_obs.isChecked())
        if d.open_obs.isChecked():
            open_in_obsidian(post.langs["ru"].path)
        else:
            self.lang_tabs.setCurrentIndex(0)
            self.body_edits["ru"].setFocus()

    def delete_post(self):
        p = self.current
        if not p or not p.draft:
            return
        if QMessageBox.question(self, "Удалить черновик",
                                f"Переместить папку «{p.slug}» в корзину?") != QMessageBox.StandardButton.Yes:
            return
        core.trash(p.dir)
        self.current = None
        self.show_detail(False)
        self.refresh()

    def copy_post_url(self):
        if self.current:
            QGuiApplication.clipboard().setText(core.post_url(core.site_url(REPO), self.current.slug))
            self.statusBar().showMessage("Ссылка скопирована", 2500)

    def publish(self, unpublish):
        if not self.current:
            return
        self.flush_bodies()
        for l in core.LANGS:  # сохранить недописанные поля
            self.save_meta(l)
        PublishDialog(self, self.current.reload(), unpublish).exec()

    def push_pending(self):
        self.push_btn.setEnabled(False)
        self.statusBar().showMessage("Отправка…")

        def job(log):
            core.commit_and_push(REPO, [], "", log)
        w = Worker(job, self)

        def done(ok, res):
            self.push_btn.setEnabled(True)
            self.statusBar().showMessage("Отправлено" if ok else "Ошибка отправки", 5000)
            if not ok:
                QMessageBox.warning(self, "Ошибка отправки", str(res))
            self.refresh()
        w.done.connect(done)
        w.start()
        self._push_worker = w

    # ------------------------------------------------------------ живой сайт

    def watch_live(self, url, title):
        self.live_checks.append((url, title, time.time()))
        self.live_timer.start()
        self.statusBar().showMessage(f"Жду появления «{title}» на сайте…")

    def poll_live(self):
        def job(log):
            return [(u, t, s, core.url_is_live(u)) for u, t, s in self.live_checks]
        w = Worker(job, self)

        def done(ok, res):
            if not ok:
                return
            keep = []
            for u, t, s, live in res:
                if live:
                    self.notify(f"«{t}» опубликован", u)
                    for d in self.findChildren(PublishDialog):
                        if d.live_url == u:
                            d.live_ok()
                elif time.time() - s > 900:
                    self.notify(f"«{t}» не появился за 15 минут", "Проверьте сборку на Codeberg (Actions)")
                else:
                    keep.append((u, t, s))
            self.live_checks = keep
            if not keep:
                self.live_timer.stop()
        w.done.connect(done)
        w.start()
        self._live_worker = w

    def notify(self, title, text):
        self.statusBar().showMessage(f"{title} — {text}", 15000)
        QProcess.startDetached("notify-send", ["-a", "Blog Studio", "-i", "document-send", title, text])

    # ------------------------------------------------------------ предпросмотр

    def preview(self):
        if not self.current:
            return
        url = f"http://localhost:{HUGO_PORT}/ru/posts/{self.current.slug}/"
        if self.hugo and self.hugo.state() == QProcess.ProcessState.Running:
            open_url(url)
            return
        self.pending_preview = url
        self.hugo_buf = ""
        self.hugo = QProcess(self)
        self.hugo.setWorkingDirectory(str(REPO))
        self.hugo.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.hugo.readyReadStandardOutput.connect(self.hugo_output)
        self.hugo.finished.connect(self.hugo_finished)
        self.hugo.start("hugo", ["server", "--buildDrafts", "--buildFuture", "--bind", "127.0.0.1",
                                 "--port", str(HUGO_PORT), "--navigateToChanged", "--disableFastRender"])
        self.hugo_label.setText("⏳ запускаю hugo server…")
        self.hugo_stop.setVisible(True)

    def hugo_output(self):
        out = bytes(self.hugo.readAllStandardOutput()).decode(errors="replace")
        self.hugo_buf = (getattr(self, "hugo_buf", "") + out)[-4000:]
        if self.pending_preview and "Web Server is available" in self.hugo_buf:
            self.hugo_label.setText(f"🟢 предпросмотр: localhost:{HUGO_PORT}")
            open_url(self.pending_preview)
            self.pending_preview = None
        elif "address already in use" in self.hugo_buf and self.pending_preview:
            # сервер уже запущен где-то ещё — просто открываем страницу
            open_url(self.pending_preview)
            self.pending_preview = None

    def hugo_finished(self, code, _status):
        self.hugo_label.setText("")
        self.hugo_stop.setVisible(False)
        if code not in (0, 9, 15) and "address already in use" not in getattr(self, "hugo_buf", ""):
            QMessageBox.warning(self, "hugo server завершился", getattr(self, "hugo_buf", "")[-2000:])
        self.hugo_buf = ""

    def stop_hugo(self):
        if self.hugo:
            self.hugo.terminate()
            self.hugo.waitForFinished(3000)

    # ------------------------------------------------------------ окно


    def event(self, e):
        from PySide6.QtCore import QEvent
        if e.type() == QEvent.Type.WindowActivate:
            self.refresh_timer.start()
        return super().event(e)

    def closeEvent(self, e):
        self.flush_bodies()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.saveState())
        self.stop_hugo()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Blog Studio")
    app.setDesktopFileName("blog-studio")
    if not (REPO / "hugo.yaml").exists():
        QMessageBox.critical(None, "Blog Studio", f"Не найден hugo.yaml в {REPO}")
        return 1
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
