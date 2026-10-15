from __future__ import annotations

import os
import html
import shutil
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate, QObject, QSettings, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .exporter import export_batch, export_migration_bundle
from .importer import import_archives, load_imported_sessions
from .reader import APP_VERSION, Catalog, SessionInfo, _local_display, load_session_events, scan_codex_home


def default_codex_home() -> str:
    return os.path.abspath(os.path.expandvars(os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")))


def default_import_root() -> str:
    local_app_data = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local_app_data, "CodexHistoryViewer", "imports")


class ScanWorker(QThread):
    progress = Signal(str, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, root: str):
        super().__init__()
        self.root = root
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            catalog = scan_codex_home(self.root, self.cancel_event, lambda text, value: self.progress.emit(text, value))
            self.completed.emit(catalog)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ExportWorker(QThread):
    progress = Signal(str, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, sessions: list[SessionInfo], catalog: Catalog, options: dict[str, Any]):
        super().__init__()
        self.sessions = sessions
        self.catalog = catalog
        self.options = options
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            results = export_batch(
                self.sessions,
                self.catalog,
                self.options["output_root"],
                self.options["formats"],
                self.options["mode"],
                self.options["redact"],
                self.options["include_raw"],
                self.cancel_event,
                lambda text, value: self.progress.emit(text, value),
            )
            self.completed.emit(results)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class MigrationExportWorker(QThread):
    progress = Signal(str, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, sessions: list[SessionInfo], catalog: Catalog, output_path: str):
        super().__init__()
        self.sessions = sessions
        self.catalog = catalog
        self.output_path = output_path
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            result = export_migration_bundle(
                self.sessions,
                self.catalog,
                self.output_path,
                self.cancel_event,
                lambda text, value: self.progress.emit(text, value),
            )
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ImportWorker(QThread):
    progress = Signal(str, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, archive_paths: list[str], import_root: str, existing_ids: set[str]):
        super().__init__()
        self.archive_paths = archive_paths
        self.import_root = import_root
        self.existing_ids = existing_ids
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            result = import_archives(
                self.archive_paths,
                self.import_root,
                self.existing_ids,
                self.cancel_event,
                lambda text, value: self.progress.emit(text, value),
            )
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class PreviewWorker(QThread):
    completed = Signal(str, object)
    failed = Signal(str)

    def __init__(self, session: SessionInfo, catalog: Catalog):
        super().__init__()
        self.session = session
        self.catalog = catalog
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            events, issues, _ = load_session_events(self.session, self.catalog, self.cancel_event)
            chunks = []
            for event in events:
                kind = event.get("kind")
                timestamp = _local_display(str(event.get("raw_timestamp", "")))
                attachments = "".join(self._attachment_html(item) for item in event.get("attachments", []))
                if kind == "message":
                    who = "用户" if event.get("role") == "user" else "助手"
                    phase = f"（{event.get('phase')}）" if event.get("phase") else ""
                    title = f"[{timestamp}] {who}{phase}"
                    text = html.escape(str(event.get("text", ""))).replace("\n", "<br>")
                    chunks.append(f'<article class="message"><h3>{html.escape(title)}</h3><div>{text}</div>{attachments}</article>')
                elif kind == "tool_call":
                    title = f"[{timestamp}] 工具调用：{event.get('name')}  call_id={event.get('call_id') or '未找到'}"
                    chunks.append(f'<details><summary>{html.escape(title)}</summary><pre>{html.escape(str(event.get("text", "")))}</pre>{attachments}</details>')
                elif kind == "tool_result":
                    title = f"[{timestamp}] 工具结果：{event.get('name')}  call_id={event.get('call_id') or '未找到'}"
                    chunks.append(f'<details><summary>{html.escape(title)}</summary><pre>{html.escape(str(event.get("text", "")))}</pre>{attachments}</details>')
                elif kind in {"status", "unknown"}:
                    title = f"[{timestamp}] {event.get('name', '事件')}"
                    chunks.append(f'<details><summary>{html.escape(title)}</summary><pre>{html.escape(str(event.get("text", "")))}</pre>{attachments}</details>')
            if issues:
                chunks.append("<section><h3>读取报告</h3><ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in issues) + "</ul></section>")
            if not chunks:
                chunks.append("<p>此会话尚无可预览的聊天内容。</p>")
            page = """<!doctype html><html><head><meta charset="utf-8"><style>
body { font: 14px/1.65 'Microsoft YaHei UI', sans-serif; color: #202a29; }
article, details, section { margin: 8px 0 14px; padding: 10px 12px; border: 1px solid #d7dfdb; border-radius: 7px; background: #fff; }
article { border-left: 3px solid #318661; } h3 { margin: 0 0 6px; font-size: 13px; color: #687570; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f5f7f6; padding: 8px; border-radius: 4px; }
summary { cursor: pointer; font-weight: 600; } a { color: #176e60; text-decoration: none; } a:hover { text-decoration: underline; }
figure { margin: 8px 0 4px; }
</style></head><body>""" + "\n".join(chunks) + "</body></html>"
            self.completed.emit(self.session.session_id, page)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _attachment_html(self, attachment: dict[str, Any]) -> str:
        reference = str(attachment.get("ref") or "")
        status = str(attachment.get("status") or "状态未知")
        path_value = str(attachment.get("path") or reference)
        if path_value.casefold().startswith("file:"):
            path_value = QUrl(path_value).toLocalFile()
        try:
            path = Path(path_value).resolve(strict=True)
            if self.session.source == "导入 ZIP" and self.session.paths:
                allowed_root = (Path(self.session.paths[0]).resolve().parent / "attachments").resolve()
            else:
                allowed_root = (Path(self.catalog.root).resolve() / "attachments").resolve()
            if not path.is_file() or os.path.commonpath((os.path.normcase(str(allowed_root)), os.path.normcase(str(path)))) != os.path.normcase(str(allowed_root)):
                path = None
        except (OSError, RuntimeError, ValueError):
            path = None

        label = html.escape(str(attachment.get("kind") or "附件"))
        if path is None:
            return f'<p class="attachment">📎 {label} · {html.escape(status)} · {html.escape(reference or "未找到")}</p>'

        url = html.escape(QUrl.fromLocalFile(str(path)).toString(), quote=True)
        filename = html.escape(path.name)
        if path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
            return f'<figure class="attachment"><a href="{url}">📎 {label} · {filename}</a><br><img src="{url}" alt="{filename}" width="440"></figure>'
        return f'<p class="attachment"><a href="{url}">📎 {label} · {filename}</a></p>'


class ExportOptions(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("导出设置")
        self.setMinimumWidth(600)
        outer = QVBoxLayout(self)

        location = QGroupBox("保存位置")
        loc_layout = QHBoxLayout(location)
        self.output_edit = QLineEdit(os.path.join(os.path.expanduser("~"), "Documents", "CodexHistoryExports"))
        browse = QPushButton("选择…")
        browse.clicked.connect(self._choose_folder)
        loc_layout.addWidget(self.output_edit, 1)
        loc_layout.addWidget(browse)
        outer.addWidget(location)

        fmt = QGroupBox("导出格式")
        grid = QGridLayout(fmt)
        self.format_checks = {
            "txt": QCheckBox("TXT"),
            "md": QCheckBox("Markdown"),
            "html": QCheckBox("HTML"),
            "json": QCheckBox("JSON"),
            "raw": QCheckBox("原始 JSONL / ZST"),
            "zip": QCheckBox("ZIP 可移植包（可导入）"),
        }
        for index, (key, check) in enumerate(self.format_checks.items()):
            grid.addWidget(check, index // 3, index % 3)
        self.format_checks["md"].setChecked(True)
        self.format_checks["json"].setChecked(True)
        self.format_checks["zip"].setChecked(True)
        outer.addWidget(fmt)

        reading = QGroupBox("阅读版与隐私")
        form = QFormLayout(reading)
        self.mode = QComboBox()
        self.mode.addItem("完整版：聊天、工具和状态事件", "full")
        self.mode.addItem("纯聊天版：用户与助手消息", "chat")
        self.redact = QCheckBox("对聊天和工具文字做常见令牌/密码脱敏")
        self.redact.setChecked(False)
        self.include_raw_confirm = QCheckBox("我确认原始文件可能包含密码、令牌等敏感内容")
        self.include_raw_confirm.setEnabled(False)
        self.format_checks["raw"].toggled.connect(self.include_raw_confirm.setEnabled)
        self.format_checks["raw"].toggled.connect(lambda checked: self.include_raw_confirm.setChecked(False) if not checked else None)
        form.addRow("阅读内容", self.mode)
        form.addRow("", self.redact)
        form.addRow("", self.include_raw_confirm)
        outer.addWidget(reading)
        warning = QLabel("聊天文本默认按记录原样导出；脱敏只覆盖常见键值格式，不会改写原始文件。原始文件仅在勾选并确认后复制，且不应用脱敏。")
        warning.setWordWrap(True)
        outer.addWidget(warning)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.export_button = QPushButton("开始导出")
        self.cancel_button.clicked.connect(self.close)
        self.export_button.clicked.connect(self._accept)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.export_button)
        outer.addLayout(actions)
        self.options: dict[str, Any] | None = None

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择导出文件夹", self.output_edit.text())
        if folder:
            self.output_edit.setText(folder)

    def _accept(self) -> None:
        if not self.output_edit.text().strip():
            QMessageBox.warning(self, "缺少保存位置", "请选择导出文件夹。")
            return
        formats = [key for key in ("txt", "md", "html", "json", "zip") if self.format_checks[key].isChecked()]
        include_raw = self.format_checks["raw"].isChecked()
        if not formats and not include_raw:
            QMessageBox.warning(self, "未选择格式", "至少选择一种导出格式。")
            return
        if include_raw and not self.include_raw_confirm.isChecked():
            QMessageBox.warning(self, "需要确认", "请确认原始文件可能包含敏感内容后再导出。")
            return
        if include_raw:
            formats.append("raw")
        self.options = {
            "output_root": os.path.abspath(os.path.expandvars(self.output_edit.text().strip())),
            "formats": formats,
            "mode": self.mode.currentData(),
            "redact": self.redact.isChecked(),
            "include_raw": include_raw,
        }
        self.accept()


class MainWindow(QMainWindow):
    COLUMNS = ["选择", "会话", "最后更新", "用户 / 助手", "读取状态"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Codex 历史记录浏览与导出")
        self.resize(1440, 860)
        self.setMinimumSize(1120, 680)
        self.settings = QSettings("Local Codex Tools", "CodexHistoryViewer")
        self.catalog: Catalog | None = None
        self.sessions_by_id: dict[str, SessionInfo] = {}
        self.import_root = default_import_root()
        self.imported_sessions = load_imported_sessions(self.import_root)
        self.sessions_by_id.update({session.session_id: session for session in self.imported_sessions})
        self.visible_sessions: list[SessionInfo] = []
        self.selected_ids: set[str] = set()
        self.scan_worker: ScanWorker | None = None
        self.export_worker: ExportWorker | None = None
        self.migration_worker: MigrationExportWorker | None = None
        self.import_worker: ImportWorker | None = None
        self.preview_worker: PreviewWorker | None = None
        self._active_preview_id = ""
        self._pending_preview: SessionInfo | None = None
        self._updating_table = False
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(180)
        self._filter_timer.timeout.connect(self.apply_filters)

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 10, 12, 8)
        root_layout.setSpacing(8)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Codex 数据目录"))
        self.path_edit = QLineEdit(default_codex_home())
        self.path_edit.setClearButtonEnabled(False)
        self.path_edit.setToolTip("默认使用 CODEX_HOME；未设置时为 %USERPROFILE%\\.codex")
        self.choose_path_button = QPushButton("浏览…")
        self.scan_button = QPushButton("扫描 / 刷新")
        self.choose_path_button.clicked.connect(self._choose_codex_home)
        self.scan_button.clicked.connect(self.start_scan)
        source_row.addWidget(self.path_edit, 1)
        source_row.addWidget(self.choose_path_button)
        source_row.addWidget(self.scan_button)
        root_layout.addLayout(source_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("搜索"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("标题、会话 ID、聊天内容或工作目录")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda *_: self._filter_timer.start())
        filter_row.addWidget(self.search_edit, 1)
        self.source_filter = QComboBox()
        self.source_filter.addItem("全部来源", "all")
        self.source_filter.addItem("本机扫描", "local")
        self.source_filter.addItem("已导入", "imported")
        self.source_filter.setToolTip("只看当前电脑扫描到的会话，或从 ZIP 导入的会话")
        self.source_filter.currentIndexChanged.connect(self.apply_filters)
        filter_row.addWidget(self.source_filter)
        self.archive_filter = QComboBox()
        self.archive_filter.addItem("全部会话", "all")
        self.archive_filter.addItem("未归档", "active")
        self.archive_filter.addItem("已归档", "archived")
        self.archive_filter.currentIndexChanged.connect(self.apply_filters)
        filter_row.addWidget(self.archive_filter)
        self.date_enabled = QCheckBox("日期范围")
        self.date_enabled.toggled.connect(self.apply_filters)
        filter_row.addWidget(self.date_enabled)
        self.date_from = QDateEdit(QDate.currentDate().addMonths(-1))
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        self.date_to = QDateEdit(QDate.currentDate())
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        self.date_from.dateChanged.connect(self.apply_filters)
        self.date_to.dateChanged.connect(self.apply_filters)
        self.date_from.setEnabled(False)
        self.date_to.setEnabled(False)
        self.date_enabled.toggled.connect(self.date_from.setEnabled)
        self.date_enabled.toggled.connect(self.date_to.setEnabled)
        filter_row.addWidget(self.date_from)
        filter_row.addWidget(QLabel("至"))
        filter_row.addWidget(self.date_to)
        root_layout.addLayout(filter_row)

        action_row = QHBoxLayout()
        self.select_visible_button = QPushButton("全选当前结果")
        self.clear_selection_button = QPushButton("清除选择")
        self.preview_button = QPushButton("预览")
        self.import_button = QPushButton("导入 ZIP")
        self.migration_export_button = QPushButton("导出全部迁移包")
        self.export_button = QPushButton("导出所选")
        self.export_button.setObjectName("primaryAction")
        self.report_button = QPushButton("读取报告")
        self.select_visible_button.clicked.connect(self.select_visible)
        self.clear_selection_button.clicked.connect(self.clear_selection)
        self.preview_button.clicked.connect(self.preview_selected)
        self.import_button.clicked.connect(self.start_import)
        self.migration_export_button.clicked.connect(self.start_migration_export)
        self.export_button.clicked.connect(self.start_export)
        self.report_button.clicked.connect(self.show_scan_report)
        self.result_label = QLabel("等待扫描")
        self.result_label.setObjectName("subtleText")
        self.selection_label = QLabel("已选择 0 个会话")
        self.selection_label.setObjectName("subtleText")
        action_row.addWidget(self.select_visible_button)
        action_row.addWidget(self.clear_selection_button)
        action_row.addWidget(self.preview_button)
        action_row.addWidget(self.import_button)
        action_row.addWidget(self.migration_export_button)
        action_row.addWidget(self.export_button)
        action_row.addWidget(self.report_button)
        action_row.addSpacing(8)
        action_row.addWidget(self.result_label)
        action_row.addWidget(self.selection_label)
        action_row.addStretch(1)
        root_layout.addLayout(action_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(5)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setMinimumWidth(500)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setSortingEnabled(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellDoubleClicked.connect(lambda *_: self.preview_selected())
        self.table.currentCellChanged.connect(self._current_row_changed)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.resizeSection(0, 48)
        header.setSectionResizeMode(1, header.ResizeMode.Stretch)
        header.resizeSection(2, 132)
        header.resizeSection(3, 112)
        header.resizeSection(4, 132)
        splitter.addWidget(self.table)

        preview_panel = QWidget()
        preview_panel.setMinimumWidth(360)
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(12, 0, 0, 0)
        preview_layout.setSpacing(4)
        self.preview_title = QLabel("会话预览")
        self.preview_title.setObjectName("previewTitle")
        self.preview_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview_meta = QLabel()
        self.preview_meta.setObjectName("subtleText")
        self.preview_meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview_meta.setWordWrap(False)
        self.preview = QTextBrowser()
        self.preview.setOpenLinks(False)
        self.preview.setOpenExternalLinks(False)
        self.preview.anchorClicked.connect(lambda url: QDesktopServices.openUrl(url))
        self.preview.setFont(QFont("Microsoft YaHei UI", 9))
        preview_layout.addWidget(self.preview_title)
        preview_layout.addWidget(self.preview_meta)
        preview_layout.addWidget(self.preview, 1)
        splitter.addWidget(preview_panel)
        splitter.setSizes([820, 620])
        root_layout.addWidget(splitter, 1)
        self.splitter = splitter

        saved_geometry = self.settings.value("window/geometry")
        if saved_geometry:
            self.restoreGeometry(saved_geometry)
        saved_splitter = self.settings.value("window/splitter")
        if saved_splitter:
            self.splitter.restoreState(saved_splitter)

        footer = QHBoxLayout()
        self.status_label = QLabel("就绪")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximumWidth(260)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_current)
        self.help_button = QPushButton("使用说明")
        self.help_button.clicked.connect(self.show_help)
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.progress_bar)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.help_button)
        root_layout.addLayout(footer)
        self.statusBar().showMessage(f"版本 {APP_VERSION} · 数据仅在本机处理")

        self.apply_filters()
        self.start_scan()

    def _choose_codex_home(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择 Codex 数据目录", self.path_edit.text())
        if folder:
            self.path_edit.setText(folder)
            self.start_scan()

    def start_scan(self) -> None:
        if (
            self.scan_worker and self.scan_worker.isRunning()
            or self.export_worker and self.export_worker.isRunning()
            or self.migration_worker and self.migration_worker.isRunning()
            or self.import_worker and self.import_worker.isRunning()
        ):
            return
        root = os.path.abspath(os.path.expandvars(self.path_edit.text().strip()))
        if not os.path.isdir(root):
            if self.imported_sessions:
                self.apply_filters()
                self.status_label.setText("未找到 Codex 数据目录；当前显示已导入会话")
            else:
                QMessageBox.warning(self, "目录不存在", "请选择有效的 Codex 数据目录。")
            return
        self.scan_button.setEnabled(False)
        self.choose_path_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.migration_export_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status_label.setText("正在扫描…")
        self.progress_bar.setRange(0, 0)
        self.scan_worker = ScanWorker(root)
        self.scan_worker.progress.connect(self._progress)
        self.scan_worker.completed.connect(self._scan_completed)
        self.scan_worker.failed.connect(self._scan_failed)
        self.scan_worker.start()

    @Slot(str, int)
    def _progress(self, message: str, value: int) -> None:
        self.status_label.setText(message)
        if value < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(value)

    @Slot(object)
    def _scan_completed(self, catalog: Catalog) -> None:
        old = self.catalog
        self.catalog = catalog
        self.imported_sessions = load_imported_sessions(self.import_root)
        self.sessions_by_id = {session.session_id: session for session in self._all_sessions()}
        self.scan_button.setEnabled(True)
        self.choose_path_button.setEnabled(True)
        self.import_button.setEnabled(True)
        self.migration_export_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.apply_filters()
        size_mb = catalog.bytes_scanned / (1024 * 1024)
        self.status_label.setText(f"扫描完成：{len(catalog.sessions):,} 个会话，{catalog.files_scanned} 个记录文件，{size_mb:.1f} MiB")
        if catalog.issues:
            self.statusBar().showMessage(f"扫描报告有 {len(catalog.issues)} 项；会话行的读取状态与预览中可查看详情", 10000)
        if old and old.temp_dir and old.temp_dir != catalog.temp_dir:
            shutil.rmtree(old.temp_dir, ignore_errors=True)

    @Slot(str)
    def _scan_failed(self, message: str) -> None:
        self.scan_button.setEnabled(True)
        self.choose_path_button.setEnabled(True)
        self.import_button.setEnabled(True)
        self.migration_export_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label.setText("扫描失败")
        QMessageBox.critical(self, "扫描失败", message)

    def _date_matches(self, session: SessionInfo) -> bool:
        if not self.date_enabled.isChecked() or not session.created_at:
            return True
        try:
            day = QDate.fromString(session.created_at[:10], "yyyy-MM-dd")
            return self.date_from.date() <= day <= self.date_to.date()
        except Exception:
            return False

    def apply_filters(self, *_args) -> None:
        current_row = self.table.currentRow()
        current_session_id = (
            self.visible_sessions[current_row].session_id
            if 0 <= current_row < len(self.visible_sessions)
            else self._active_preview_id
        )
        if not self.catalog and not self.imported_sessions:
            self.result_label.setText("等待扫描")
            self._clear_preview()
            return
        needle = self.search_edit.text().strip().casefold()
        source_mode = self.source_filter.currentData()
        archive_mode = self.archive_filter.currentData()
        result = []
        all_sessions = self._all_sessions()
        for session in all_sessions:
            is_imported = session.source == "导入 ZIP"
            if source_mode == "imported" and not is_imported or source_mode == "local" and is_imported:
                continue
            if archive_mode == "active" and session.archived or archive_mode == "archived" and not session.archived:
                continue
            if not self._date_matches(session):
                continue
            if needle:
                haystack = "\n".join((session.title, session.session_id, session.cwd, session.search_text)).casefold()
                if needle not in haystack:
                    continue
            result.append(session)
        self.visible_sessions = result
        self._render_rows(current_session_id)
        self.result_label.setText(f"显示 {len(result):,} / {len(all_sessions):,} 条")

    def _render_rows(self, preserve_session_id: str = "") -> None:
        self._updating_table = True
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(self.visible_sessions))
        for row, session in enumerate(self.visible_sessions):
            select = QTableWidgetItem()
            select.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            select.setCheckState(Qt.CheckState.Checked if session.session_id in self.selected_ids else Qt.CheckState.Unchecked)
            select.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, select)
            metadata = "\n".join(
                (
                    f"会话 ID：{session.session_id}",
                    f"创建时间：{session.created_at or '未找到'}",
                    f"原始创建时间：{session.raw_created_at or '未找到'}",
                    f"工作目录：{session.cwd or '未找到'}",
                    f"数据来源：{session.source or '未知'}",
                    f"读取状态：{session.status}",
                    f"归档：{'是' if session.archived else '否'}",
                    f"工具调用：{session.tool_count}",
                    f"事件数：{session.event_count}",
                )
            )
            values = [
                session.title,
                session.updated_at,
                f"{session.user_count} / {session.assistant_count}",
                f"{session.status}{' · 已归档' if session.archived else ''}",
            ]
            for offset, value in enumerate(values, 1):
                item = QTableWidgetItem(str(value or ""))
                item.setToolTip(metadata)
                if offset == 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if offset == 4:
                    if any(marker in session.status for marker in ("部分", "损坏", "未找到", "错误", "缺失")):
                        item.setForeground(QColor("#9a5c25"))
                    elif session.status in {"可读取", "已导入"}:
                        item.setForeground(QColor("#267254"))
                self.table.setItem(row, offset, item)
        preserved_row = next(
            (row for row, session in enumerate(self.visible_sessions) if session.session_id == preserve_session_id),
            -1,
        )
        if preserved_row >= 0:
            self.table.setCurrentCell(preserved_row, 1)
        self._updating_table = False
        self._update_selection_label()
        visible_ids = {session.session_id for session in self.visible_sessions}
        if preserve_session_id and preserved_row < 0:
            self._clear_preview()
        elif self._active_preview_id and self._active_preview_id not in visible_ids:
            self._clear_preview()

    def _clear_preview(self) -> None:
        self._active_preview_id = ""
        self._pending_preview = None
        self.preview_title.setText("会话预览")
        self.preview_title.setToolTip("")
        self.preview_meta.clear()
        self.preview_meta.setToolTip("")
        self.preview.setPlainText("选择会话以查看聊天内容。")

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table or item.column() != 0:
            return
        session = self.visible_sessions[item.row()]
        if item.checkState() == Qt.CheckState.Checked:
            self.selected_ids.add(session.session_id)
        else:
            self.selected_ids.discard(session.session_id)
        self._update_selection_label()

    def _update_selection_label(self) -> None:
        self.selection_label.setText(f"已选择 {len(self.selected_ids):,} 个会话")

    def select_visible(self) -> None:
        current_row = self.table.currentRow()
        current_session_id = self.visible_sessions[current_row].session_id if 0 <= current_row < len(self.visible_sessions) else self._active_preview_id
        self.selected_ids.update(session.session_id for session in self.visible_sessions)
        self._render_rows(current_session_id)

    def clear_selection(self) -> None:
        current_row = self.table.currentRow()
        current_session_id = self.visible_sessions[current_row].session_id if 0 <= current_row < len(self.visible_sessions) else self._active_preview_id
        self.selected_ids.clear()
        self._render_rows(current_session_id)

    def _selected_sessions(self) -> list[SessionInfo]:
        return [session for session in self._all_sessions() if session.session_id in self.selected_ids]

    def _all_sessions(self) -> list[SessionInfo]:
        sessions = {session.session_id: session for session in self.imported_sessions}
        if self.catalog:
            sessions.update({session.session_id: session for session in self.catalog.sessions})
        return sorted(sessions.values(), key=lambda session: (session.updated_at, session.session_id), reverse=True)

    def _current_row_changed(self, current_row: int, *_args) -> None:
        if self._updating_table or current_row < 0 or current_row >= len(self.visible_sessions):
            return
        self._load_preview(self.visible_sessions[current_row])

    def preview_selected(self) -> None:
        session = None
        row = self.table.currentRow()
        if 0 <= row < len(self.visible_sessions):
            session = self.visible_sessions[row]
        elif self._selected_sessions():
            session = self._selected_sessions()[0]
        if not session:
            QMessageBox.information(self, "预览", "请先选择一个会话。")
            return
        self._load_preview(session)

    def _load_preview(self, session: SessionInfo) -> None:
        if self.preview_worker and self.preview_worker.isRunning():
            self._pending_preview = None if session.session_id == self._active_preview_id else session
            if self._pending_preview:
                self.preview_title.setText(f"等待读取：{session.title}")
            return
        if not self.catalog:
            self.catalog = Catalog(root=self.import_root, sessions=self.imported_sessions)
        self._active_preview_id = session.session_id
        self.preview_title.setText(session.title or "未命名会话")
        self.preview_title.setToolTip(session.title or "未命名会话")
        self.preview_meta.setText(
            f"{session.created_at or '创建时间未知'}  ·  {session.source or '来源未知'}  ·  "
            f"{session.status}{'  ·  已归档' if session.archived else ''}"
        )
        self.preview_meta.setToolTip(
            f"会话 ID：{session.session_id}\n"
            f"原始创建时间：{session.raw_created_at or '未找到'}\n"
            f"工作目录：{session.cwd or '未找到'}"
        )
        self.preview.setPlainText("正在读取会话记录…")
        self.preview_worker = PreviewWorker(session, self.catalog)
        self.preview_worker.completed.connect(self._preview_completed)
        self.preview_worker.failed.connect(lambda error, sid=session.session_id: self._preview_failed(sid, error))
        self.preview_worker.finished.connect(self._preview_worker_finished)
        self.preview_worker.start()

    @Slot(str, object)
    def _preview_completed(self, session_id: str, text: str) -> None:
        if session_id != self._active_preview_id or self._pending_preview:
            return
        self.preview.setHtml(text)
        cursor = self.preview.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self.preview.setTextCursor(cursor)

    def _preview_failed(self, session_id: str, error: str) -> None:
        if session_id == self._active_preview_id and not self._pending_preview:
            self.preview.setPlainText("读取失败：" + error)

    def _preview_worker_finished(self) -> None:
        self.preview_worker = None
        pending = self._pending_preview
        self._pending_preview = None
        if pending:
            self._load_preview(pending)

    def start_export(self) -> None:
        selected = self._selected_sessions()
        if not selected:
            QMessageBox.information(self, "导出", "请先勾选一个或多个会话。")
            return
        if not self.catalog:
            self.catalog = Catalog(root=self.import_root, sessions=[])
        dialog = ExportOptions(self)
        if dialog.exec() != ExportOptions.DialogCode.Accepted or not dialog.options:
            return
        options = dialog.options
        if options["include_raw"]:
            answer = QMessageBox.warning(
                self,
                "原始内容可能含敏感信息",
                f"将复制 {len(selected)} 个会话的原始记录文件。原始文件不应用脱敏。确认继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.export_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.migration_export_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.choose_path_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setRange(0, 0)
        self.status_label.setText("正在导出…")
        self.export_worker = ExportWorker(selected, self.catalog, options)
        self.export_worker.progress.connect(self._progress)
        self.export_worker.completed.connect(self._export_completed)
        self.export_worker.failed.connect(self._export_failed)
        self.export_worker.start()

    def start_migration_export(self) -> None:
        if (
            self.scan_worker and self.scan_worker.isRunning()
            or self.export_worker and self.export_worker.isRunning()
            or self.migration_worker and self.migration_worker.isRunning()
            or self.import_worker and self.import_worker.isRunning()
        ):
            return
        sessions = self._all_sessions()
        if not sessions:
            QMessageBox.information(self, "导出全部数据", "请先扫描 Codex 数据目录。")
            return
        if not self.catalog:
            self.catalog = Catalog(root=self.import_root, sessions=self.imported_sessions)
        default_path = os.path.join(
            os.path.expanduser("~/Documents"),
            f"CodexHistoryViewer-migration-{date.today().isoformat()}.zip",
        )
        output_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出本机全部会话到迁移包",
            default_path,
            "迁移 ZIP (*.zip)",
        )
        if not output_path:
            return
        if not output_path.casefold().endswith(".zip"):
            output_path += ".zip"

        self.import_button.setEnabled(False)
        self.migration_export_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.choose_path_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label.setText(f"正在打包全部 {len(sessions):,} 个会话…")
        self.migration_worker = MigrationExportWorker(sessions, self.catalog, output_path)
        self.migration_worker.progress.connect(self._progress)
        self.migration_worker.completed.connect(self._migration_export_completed)
        self.migration_worker.failed.connect(self._migration_export_failed)
        self.migration_worker.start()

    def start_import(self) -> None:
        if (
            self.scan_worker and self.scan_worker.isRunning()
            or self.export_worker and self.export_worker.isRunning()
            or self.migration_worker and self.migration_worker.isRunning()
            or self.import_worker and self.import_worker.isRunning()
        ):
            return
        archive_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "导入本工具导出的 ZIP 会话包或整库迁移包",
            os.path.expanduser("~"),
            "ZIP 会话包或迁移包 (*.zip);;所有文件 (*)",
        )
        if not archive_paths:
            return
        self.import_button.setEnabled(False)
        self.migration_export_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.choose_path_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setRange(0, 0)
        self.status_label.setText(f"正在导入 {len(archive_paths)} 个 ZIP 包…")
        self.import_worker = ImportWorker(archive_paths, self.import_root, set(self.sessions_by_id))
        self.import_worker.progress.connect(self._progress)
        self.import_worker.completed.connect(self._import_completed)
        self.import_worker.failed.connect(self._import_failed)
        self.import_worker.start()

    @Slot(object)
    def _import_completed(self, result: dict[str, Any]) -> None:
        self.imported_sessions = result.get("sessions", [])
        self.sessions_by_id = {session.session_id: session for session in self._all_sessions()}
        self.apply_filters()
        self.import_button.setEnabled(True)
        self.migration_export_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self.choose_path_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        imported = int(result.get("imported", 0))
        skipped = int(result.get("skipped", 0))
        failed = int(result.get("failed", 0))
        self.status_label.setText(f"导入结束：成功 {imported}，跳过 {skipped}，失败 {failed}")
        lines = [f"导入结束：成功 {imported}，跳过 {skipped}，失败 {failed}", ""]
        for item in result.get("results", []):
            lines.append(f"[{item.get('status')}] {item.get('title') or item.get('path')}")
            if item.get("message"):
                lines.append(f"  {item['message']}")
        self.preview_title.setText("最近一次导入结果")
        self.preview_meta.setText(f"成功 {imported}  ·  跳过 {skipped}  ·  失败 {failed}")
        self.preview_meta.setToolTip("")
        self.preview.setPlainText("\n".join(lines))

    @Slot(str)
    def _import_failed(self, message: str) -> None:
        self.import_button.setEnabled(True)
        self.migration_export_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self.choose_path_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label.setText("导入失败")
        QMessageBox.critical(self, "导入失败", message)

    @Slot(object)
    def _export_completed(self, results: list[dict[str, Any]]) -> None:
        self.import_button.setEnabled(True)
        self.migration_export_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self.choose_path_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        completed = sum(1 for result in results if result.get("status", "").startswith("完成"))
        failed = len(results) - completed
        self.status_label.setText(f"导出结束：成功 {completed}，失败或取消 {failed}")
        lines = [f"导出结束：成功 {completed}，失败或取消 {failed}", ""]
        for result in results:
            lines.append(f"[{result.get('status')}] {result.get('title') or result.get('session_id')}")
            lines.append(f"  目录：{result.get('folder', '')}")
            if result.get("zip"):
                lines.append(f"  ZIP：{result['zip']}")
            for issue in result.get("issues", [])[:5]:
                lines.append(f"  报告：{issue}")
        self.preview_title.setText("最近一次导出结果")
        self.preview_meta.setText(f"成功 {completed}  ·  失败或取消 {failed}")
        self.preview_meta.setToolTip("")
        self.preview.setPlainText("\n".join(lines))
        if results:
            folder = results[0].get("folder")
            if folder and os.path.isdir(folder):
                open_button = QMessageBox(self)
                open_button.setWindowTitle("导出完成")
                open_button.setText(f"导出已完成。结果保存在：\n{folder}")
                open_button.setStandardButtons(QMessageBox.StandardButton.Ok)
                open_button.setInformativeText("详细错误和文件哈希位于每个会话目录的 manifest.json。")
                open_button.exec()

    @Slot(str)
    def _export_failed(self, message: str) -> None:
        self.import_button.setEnabled(True)
        self.migration_export_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self.choose_path_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label.setText("导出失败")
        QMessageBox.critical(self, "导出失败", message)

    @Slot(object)
    def _migration_export_completed(self, result: dict[str, Any]) -> None:
        self.import_button.setEnabled(True)
        self.migration_export_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self.choose_path_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        if result.get("cancelled"):
            self.progress_bar.setValue(0)
            self.status_label.setText("迁移包导出已取消")
            self.preview_title.setText("迁移包导出已取消")
            self.preview_meta.clear()
            self.preview.setPlainText("已取消导出，未生成可导入的迁移包。")
            return

        exported = int(result.get("exported", 0))
        failed = int(result.get("failed", 0))
        output_path = str(result.get("path") or "")
        self.progress_bar.setValue(100)
        self.status_label.setText(f"迁移包已生成：{exported:,} 个会话，失败 {failed:,} 个")
        lines = [
            f"已打包会话：{exported:,}",
            f"未能打包：{failed:,}",
            f"迁移包：{output_path}",
            "",
            "将此 ZIP 文件复制到另一台电脑，在那里点击“导入 ZIP”即可载入会话。",
            "包内含聊天记录和关联附件，不含 Codex 登录配置、原始数据库或原始 JSONL/ZST。",
        ]
        for item in result.get("results", []):
            if not str(item.get("status", "")).startswith("完成"):
                lines.append(f"[{item.get('status')}] {item.get('title') or item.get('session_id')}")
                for issue in item.get("issues", [])[:3]:
                    lines.append(f"  {issue}")
        self.preview_title.setText("最近一次迁移包")
        self.preview_meta.setText(f"已打包 {exported:,}  ·  失败 {failed:,}")
        self.preview_meta.setToolTip("")
        self.preview.setPlainText("\n".join(lines))
        QMessageBox.information(
            self,
            "迁移包已生成",
            f"已打包 {exported:,} 个会话。\n\n{output_path}\n\n"
            "把这个 ZIP 文件复制到另一台电脑，再用“导入 ZIP”载入。",
        )

    @Slot(str)
    def _migration_export_failed(self, message: str) -> None:
        self.import_button.setEnabled(True)
        self.migration_export_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self.choose_path_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label.setText("迁移包导出失败")
        QMessageBox.critical(self, "迁移包导出失败", message)

    def cancel_current(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.cancel()
            self.status_label.setText("正在取消扫描…")
        elif self.export_worker and self.export_worker.isRunning():
            self.export_worker.cancel()
            self.status_label.setText("正在取消导出…已写出的部分目录会标记为未完成")
        elif self.migration_worker and self.migration_worker.isRunning():
            self.migration_worker.cancel()
            self.status_label.setText("正在取消迁移包导出…")
        elif self.import_worker and self.import_worker.isRunning():
            self.import_worker.cancel()
            self.status_label.setText("正在取消导入…")

    def show_help(self) -> None:
        QMessageBox.information(
            self,
            "本地数据说明",
            "工具只读扫描所选 Codex 目录。聊天正文以 rollout JSONL 为主，数据库用于会话元数据与缺少 rollout 时的条目回退；session_index.jsonl 和 history.jsonl 不当作完整聊天记录。\n\n"
            "归档 ZST 需要 zstandard 依赖。SQLite 先以只读方式建立临时一致性快照，从而读取 WAL 中已提交内容。附件仅复制 attachments\\<会话 ID> 或记录明确引用的文件。\n\n"
            "数据库投影可能晚于正在写入的 rollout，或者仅保留部分条目。读取报告、损坏行、缺失附件和未知事件类型会写入预览及 manifest.json。\n\n"
            "“导出全部迁移包”会将当前发现的会话和关联附件打包成一个 ZIP；将它复制到另一台电脑后，可用“导入 ZIP”载入。迁移包不含 Codex 登录配置、原始数据库或原始 JSONL/ZST。导入内容保存在本机应用数据目录；相同会话 ID 会跳过。原始导出可能含敏感内容，默认关闭；常见格式脱敏是启发式处理，不能保证识别所有秘密。HTML 为静态离线文件，记录内容经过转义。",
        )

    def show_scan_report(self) -> None:
        if not self.catalog:
            QMessageBox.information(self, "读取报告", "请先扫描 Codex 数据目录。")
            return
        lines = [
            f"扫描目录：{self.catalog.root}",
            f"rollout 文件：{self.catalog.files_scanned}",
            f"读取字节：{self.catalog.bytes_scanned:,}",
            f"发现会话：{len(self.catalog.sessions):,}",
            "",
            "辅助数据源：",
            *[f"- {note}" for note in self.catalog.notes],
            "",
            "SQLite 结构检测：",
        ]
        for database in self.catalog.databases:
            recognized = [table.name for table in database.tables if table.metadata_id or table.item_json]
            lines.append(f"- {database.source_path}")
            lines.append(f"  已识别表：{', '.join(recognized) if recognized else '无会话条目表'}")
            if database.error:
                lines.append(f"  状态：{database.error}")
        if self.catalog.issues:
            lines.extend(["", f"扫描问题（{len(self.catalog.issues)} 项）：", *[f"- {item}" for item in self.catalog.issues]])
        session_issue_count = sum(len(session.issues) for session in self.catalog.sessions)
        if session_issue_count:
            lines.extend(["", f"会话问题（{session_issue_count} 项）："])
            for session in self.catalog.sessions:
                for issue in session.issues:
                    lines.append(f"- {session.session_id}：{issue}")
        dialog = QDialog(self)
        dialog.setWindowTitle("读取报告")
        dialog.resize(860, 620)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText("\n".join(lines))
        layout.addWidget(view, 1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    def closeEvent(self, event) -> None:
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/splitter", self.splitter.saveState())
        for worker in (self.scan_worker, self.export_worker, self.migration_worker, self.import_worker, self.preview_worker):
            if worker and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(5000)
        if self.catalog and self.catalog.temp_dir:
            shutil.rmtree(self.catalog.temp_dir, ignore_errors=True)
        super().closeEvent(event)


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Codex 历史记录浏览与导出")
    app.setOrganizationName("Local Codex Tools")
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    app.setStyleSheet(
        """
        QWidget { color: #202a29; }
        QMainWindow, QDialog { background: #f5f7f6; }
        QLineEdit, QComboBox, QDateEdit, QPlainTextEdit, QTextBrowser, QTableWidget {
            background: #ffffff;
            border: 1px solid #d0dad6;
            border-radius: 6px;
            padding: 7px 9px;
            selection-background-color: #e0eee9;
            selection-color: #1e332c;
        }
        QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QPlainTextEdit:focus, QTextBrowser:focus {
            border: 1px solid #3c806a;
        }
        QPushButton {
            background: #ffffff;
            border: 1px solid #cbd6d1;
            border-radius: 6px;
            padding: 8px 13px;
            min-height: 18px;
        }
        QPushButton:hover { background: #edf3f0; border-color: #a8bdb4; }
        QPushButton:pressed { background: #e2ece7; }
        QPushButton:disabled { color: #8a9692; background: #edf0ef; }
        QPushButton#primaryAction {
            color: #ffffff;
            background: #287357;
            border-color: #287357;
            font-weight: 600;
        }
        QPushButton#primaryAction:hover { background: #205f47; }
        QHeaderView::section {
            background: #eef2f0;
            border: 0;
            border-right: 1px solid #e0e6e3;
            border-bottom: 1px solid #d4ddd8;
            padding: 9px 7px;
            font-weight: 600;
        }
        QTableWidget {
            gridline-color: transparent;
            alternate-background-color: #f7f9f8;
        }
        QTableWidget::item { padding: 5px 7px; border-bottom: 1px solid #edf0ee; }
        QTableWidget::item:selected { background: #e5f0eb; color: #1e332c; }
        QLabel#subtleText { color: #687570; }
        QLabel#previewTitle { font-size: 11pt; font-weight: 600; padding: 2px 0; }
        QSplitter::handle { background: #e1e7e3; }
        QSplitter::handle:hover { background: #b9c9c1; }
        QProgressBar {
            background: #e6ebe8;
            border: 0;
            border-radius: 6px;
            text-align: center;
            min-height: 14px;
        }
        QProgressBar::chunk { background: #318661; border-radius: 3px; }
        QCheckBox { spacing: 6px; }
        QGroupBox {
            border: 1px solid #d7dfdb;
            border-radius: 6px;
            margin-top: 10px;
            padding: 8px;
            font-weight: 600;
        }
        QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
        """
    )
    window = MainWindow()
    window.show()
    return app.exec()

