from __future__ import annotations

from queue import Empty, Queue
from typing import Any

from PySide6.QtCore import QSize, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QStackedWidget, QStyle, QVBoxLayout, QWidget,
)

from .pages import DiagnosticsPage, GeneratorPage, MonitorPage
from .theme import STYLESHEET


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("RC Reader")
        self.resize(1440, 900)
        self.setMinimumSize(1050, 700)
        self.events: Queue[tuple[str, Any]] = Queue(maxsize=4000)

        root = QWidget()
        root.setObjectName("root")
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(14, 20, 14, 18)
        brand = QLabel("RC READER")
        brand.setStyleSheet("font-size: 15pt; font-weight: 750; letter-spacing: 1px; color: #E6EDF3;")
        caption = QLabel("Protocol workstation")
        caption.setProperty("class", "muted")
        nav.addWidget(brand)
        nav.addWidget(caption)
        nav.addSpacing(24)

        self.stack = QStackedWidget()
        self.monitor = MonitorPage(self.events)
        self.generator = GeneratorPage(self.events)
        self.diagnostics = DiagnosticsPage()
        self.pages = (self.monitor, self.generator, self.diagnostics)
        for page in self.pages:
            self.stack.addWidget(page)

        group = QButtonGroup(self)
        group.setExclusive(True)
        icons = (QStyle.SP_ComputerIcon, QStyle.SP_MediaPlay, QStyle.SP_FileDialogDetailedView)
        names = ("RC Monitor", "RC Generator", "Diagnostics")
        for index, (name, icon) in enumerate(zip(names, icons)):
            button = QPushButton(name)
            button.setProperty("class", "nav")
            button.setCheckable(True)
            button.setIcon(self.style().standardIcon(icon))
            button.setIconSize(QSize(18, 18))
            button.clicked.connect(lambda checked=False, page=index: self.stack.setCurrentIndex(page))
            group.addButton(button)
            nav.addWidget(button)
            if index == 0:
                button.setChecked(True)
        nav.addStretch()
        version = QLabel("v2.0  ·  RX / TX / UART")
        version.setProperty("class", "muted")
        nav.addWidget(version)

        shell.addWidget(sidebar)
        shell.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.setStyleSheet(STYLESHEET)

        self.event_timer = QTimer(self)
        self.event_timer.setInterval(30)
        self.event_timer.timeout.connect(self._drain_events)
        self.event_timer.start()

    def _drain_events(self) -> None:
        processed = 0
        while processed < 500:
            try:
                kind, payload = self.events.get_nowait()
            except Empty:
                break
            for page in self.pages:
                page.handle_event(kind, payload)
            processed += 1

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.monitor.scanner:
            self.monitor.scanner.stop()
        if self.monitor.worker:
            self.monitor.worker.stop()
        self.generator.generator.stop()
        event.accept()


def run_app() -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("RC Reader")
    app.setOrganizationName("RC Reader")
    window = MainWindow()
    window.show()
    return app.exec()
