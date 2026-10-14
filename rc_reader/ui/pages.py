from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
from queue import Queue
from time import monotonic
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QColor, QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..inspector import inspect_packet
from ..protocols import ChannelFrame, ParserStats, ProtocolPacket
from ..serial_worker import ScanWorker, SerialConfig, SerialWorker
from .widgets import GeneratorChannel, HistoryPlot, MetricCard, MonitorChannel
from ..core.generator import GeneratorProfile, RCGenerator, load_profile, save_profile
from ..core.ports import PortInfo, list_serial_ports, transmission_warning
from ..core.protocol_registry import REGISTRY, protocol_names


def _card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    return frame


def _title(layout: QVBoxLayout, title: str, subtitle: str) -> None:
    label = QLabel(title)
    label.setProperty("class", "title")
    hint = QLabel(subtitle)
    hint.setProperty("class", "subtitle")
    hint.setWordWrap(True)
    layout.addWidget(label)
    layout.addWidget(hint)


class MonitorPage(QWidget):
    def __init__(self, events: Queue[tuple[str, Any]]) -> None:
        super().__init__()
        self.events = events
        self.worker: SerialWorker | None = None
        self.scanner: ScanWorker | None = None
        self.channels: list[MonitorChannel] = []
        self.frame_times: deque[float] = deque(maxlen=300)
        self.last_frame_at = 0.0
        self.frame_lost_count = 0
        self.ports: tuple[PortInfo, ...] = ()

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        _title(root, "RC Monitor", "Receive, decode and inspect RC traffic in real time.")

        toolbar = _card()
        toolbar.setObjectName("toolbar")
        tools = QGridLayout(toolbar)
        tools.setContentsMargins(14, 12, 14, 12)
        self.port = QComboBox()
        self.refresh_button = QPushButton("Refresh")
        self.protocol = QComboBox()
        self.protocol.addItems(protocol_names(rx=True))
        self.baud = QLineEdit()
        self.baud.setValidator(QIntValidator(1200, 4_000_000, self))
        self.uart = QComboBox()
        self.uart.addItems(("8N1", "8N2", "8E1", "8E2", "8O1", "8O2"))
        self.auto = QPushButton("Auto Detect")
        self.connect_button = QPushButton("Connect")
        self.connect_button.setProperty("class", "primary")
        self.status = QLabel("●  Disconnected")
        self.status.setProperty("class", "muted")
        for column, (caption, widget) in enumerate((("COM port", self.port), ("Protocol", self.protocol), ("Baud rate", self.baud), ("UART", self.uart))):
            lab = QLabel(caption)
            lab.setProperty("class", "muted")
            tools.addWidget(lab, 0, column)
            tools.addWidget(widget, 1, column)
        tools.addWidget(self.refresh_button, 1, 4)
        tools.addWidget(self.auto, 1, 5)
        tools.addWidget(self.connect_button, 1, 6)
        tools.addWidget(self.status, 1, 7)
        tools.setColumnStretch(0, 2)
        root.addWidget(toolbar)

        self.scan_panel = _card()
        scan_layout = QVBoxLayout(self.scan_panel)
        scan_top = QHBoxLayout()
        self.scan_label = QLabel("Ready to scan")
        self.scan_detail = QLabel("")
        self.scan_detail.setProperty("class", "muted")
        scan_top.addWidget(self.scan_label)
        scan_top.addStretch()
        scan_top.addWidget(self.scan_detail)
        self.scan_progress = QProgressBar()
        scan_layout.addLayout(scan_top)
        scan_layout.addWidget(self.scan_progress)
        self.scan_panel.hide()
        root.addWidget(self.scan_panel)

        metrics = QGridLayout()
        self.rate_card = MetricCard("Frame rate", "0.0 Hz")
        self.good_card = MetricCard("Valid frames", "0")
        self.crc_card = MetricCard("CRC / decode errors", "0")
        self.loss_card = MetricCard("Frame lost", "0")
        self.failsafe_card = MetricCard("Failsafe", "No")
        self.last_card = MetricCard("Last valid frame", "—")
        self.rssi_card = MetricCard("RSSI", "N/A")
        self.lq_card = MetricCard("Link quality", "N/A")
        for index, card in enumerate((self.rate_card, self.good_card, self.crc_card, self.loss_card, self.failsafe_card, self.last_card, self.rssi_card, self.lq_card)):
            metrics.addWidget(card, index // 4, index % 4)
        root.addLayout(metrics)

        plot_card = _card()
        plot_layout = QVBoxLayout(plot_card)
        plot_top = QHBoxLayout()
        plot_title = QLabel("Live channel history")
        self.plot_channel = QComboBox()
        self.plot_channel.addItems(tuple(f"CH {index:02d}" for index in range(1, 33)))
        plot_top.addWidget(plot_title)
        plot_top.addStretch()
        plot_top.addWidget(self.plot_channel)
        self.plot = HistoryPlot()
        plot_layout.addLayout(plot_top)
        plot_layout.addWidget(self.plot)
        root.addWidget(plot_card)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        self.channel_grid = QGridLayout(host)
        self.channel_grid.setContentsMargins(0, 0, 0, 0)
        self.channel_grid.setSpacing(8)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        self.refresh_button.clicked.connect(self.refresh_ports)
        self.protocol.currentTextChanged.connect(self._protocol_changed)
        self.connect_button.clicked.connect(self.toggle_connection)
        self.auto.clicked.connect(self.start_scan)
        self.plot_channel.currentIndexChanged.connect(lambda _index: self.plot.clear())
        self.refresh_ports()
        self._protocol_changed(self.protocol.currentText())

    def refresh_ports(self) -> None:
        selected = self.port.currentData()
        self.ports = list_serial_ports()
        self.port.clear()
        for item in self.ports:
            self.port.addItem(item.label, item.device)
        if selected:
            index = self.port.findData(selected)
            if index >= 0:
                self.port.setCurrentIndex(index)
        if not self.ports:
            self.port.addItem("No serial ports found", "")

    def _protocol_changed(self, name: str) -> None:
        if name not in REGISTRY:
            return
        config = REGISTRY[name].config
        self.baud.setText(str(config.baudrate))
        self.uart.setCurrentText(config.uart_format)
        self.baud.setToolTip(config.note)

    def toggle_connection(self) -> None:
        if self.worker and self.worker.running:
            self.worker.stop()
            self.connect_button.setEnabled(False)
            return
        device = self.port.currentData()
        if not device:
            QMessageBox.warning(self, "No COM port", "Connect a USB-UART adapter and refresh the port list.")
            return
        try:
            baudrate = int(self.baud.text())
        except ValueError:
            QMessageBox.warning(self, "Invalid baud rate", "Enter a baud rate between 1200 and 4,000,000.")
            return
        self.worker = SerialWorker(SerialConfig(device, baudrate, self.protocol.currentText(), self.uart.currentText()), self.events)
        self.frame_times.clear()
        self.frame_lost_count = 0
        self.worker.start()
        self.status.setText("●  Opening port…")
        self.status.setProperty("class", "statusWarn")
        self.style().polish(self.status)

    def start_scan(self) -> None:
        device = self.port.currentData()
        if not device:
            QMessageBox.warning(self, "No COM port", "Select an available COM port first.")
            return
        if self.worker and self.worker.running:
            self.worker.stop()
        if self.scanner and self.scanner.running:
            self.scanner.stop()
            return
        preferred = int(self.baud.text() or 0)
        self.scanner = ScanWorker(device, self.events, preferred, self.uart.currentText())
        self.scan_panel.show()
        self.scan_progress.setValue(0)
        self.scan_label.setText("Scanning UART configurations…")
        self.auto.setText("Cancel Scan")
        self.scanner.start()

    def _ensure_channels(self, count: int) -> None:
        while len(self.channels) < count:
            channel = MonitorChannel(len(self.channels))
            index = len(self.channels)
            self.channels.append(channel)
            self.channel_grid.addWidget(channel, index // 2, index % 2)
        for index, channel in enumerate(self.channels):
            channel.setVisible(index < count)

    def handle_event(self, kind: str, payload: Any) -> None:
        if kind == "connected":
            self.status.setText("●  Connected")
            self.status.setProperty("class", "statusOk")
            self.connect_button.setText("Disconnect")
            self.connect_button.setEnabled(True)
            self.style().polish(self.status)
        elif kind == "disconnected":
            self.status.setText("●  Disconnected")
            self.status.setProperty("class", "muted")
            self.connect_button.setText("Connect")
            self.connect_button.setEnabled(True)
            self.style().polish(self.status)
        elif kind == "error":
            self.status.setText("●  Connection error")
            self.status.setProperty("class", "statusError")
            self.style().polish(self.status)
            QMessageBox.critical(self, "Serial connection", str(payload))
        elif kind == "frame" and isinstance(payload, ChannelFrame):
            now = monotonic()
            self.frame_times.append(now)
            self.last_frame_at = now
            while self.frame_times and now - self.frame_times[0] > 2.0:
                self.frame_times.popleft()
            rate = (len(self.frame_times) - 1) / max(0.001, self.frame_times[-1] - self.frame_times[0]) if len(self.frame_times) > 1 else 0.0
            self.rate_card.value.setText(f"{rate:.1f} Hz")
            self.last_card.value.setText(datetime.now().strftime("%H:%M:%S.%f")[:-3])
            self.loss_card.value.setText("Yes" if payload.frame_lost else "No")
            if payload.frame_lost:
                self.frame_lost_count += 1
            self.loss_card.value.setText(str(self.frame_lost_count))
            self.failsafe_card.value.setText("ACTIVE" if payload.failsafe else "No")
            metadata = dict(payload.metadata)
            rssi = next((value for key, value in metadata.items() if "RSSI" in key), None)
            lq = next((value for key, value in metadata.items() if "Link Quality" in key or "LQ" in key), None)
            self.rssi_card.value.setText(rssi or "N/A")
            self.lq_card.value.setText(lq or "N/A")
            self._ensure_channels(len(payload.channels))
            for index, value in enumerate(payload.microseconds):
                self.channels[index].update_value(value)
            selected = self.plot_channel.currentIndex()
            if selected < len(payload.microseconds):
                self.plot.push(payload.microseconds[selected])
        elif kind == "stats" and isinstance(payload, ParserStats):
            self.good_card.value.setText(str(payload.valid_frames))
            self.crc_card.value.setText(str(payload.invalid_frames))
        elif kind == "scan_progress":
            baud, uart, index, total = payload
            self.scan_progress.setMaximum(total)
            self.scan_progress.setValue(index)
            self.scan_detail.setText(f"{baud} baud · {uart} · parser bank · {index}/{total}")
        elif kind == "scan_observation":
            baud, uart, protocol, valid = payload
            self.scan_label.setText(f"Testing {self.port.currentData()} · best candidate {protocol} · {valid} valid frames")
            self.scan_detail.setText(f"{baud} baud · {uart} · all RX decoders")
        elif kind == "scan_found":
            spec, confidence = payload
            self.protocol.setCurrentText(spec.name)
            self.baud.setText(str(spec.baudrate))
            self.uart.setCurrentText(spec.uart_format)
            self.scan_label.setText(f"Detected {spec.name} · confidence {confidence:.1f}%")
            self.scan_progress.setValue(self.scan_progress.maximum())
            self.auto.setText("Auto Detect")
            QTimer.singleShot(180, self.toggle_connection)
        elif kind in {"scan_done", "scan_error"}:
            self.auto.setText("Auto Detect")
            if kind == "scan_error":
                self.scan_label.setText(f"Scan failed: {payload}")
            elif payload:
                confidence, spec = payload[0]
                self.scan_label.setText(f"No stable lock. Best candidate: {spec.name} ({confidence:.1f}%)")
            else:
                self.scan_label.setText("No valid RC frames found")


class GeneratorPage(QWidget):
    def __init__(self, events: Queue[tuple[str, Any]]) -> None:
        super().__init__()
        self.events = events
        self.generator = RCGenerator(events)
        self.ports: tuple[PortInfo, ...] = ()
        self.channels: list[GeneratorChannel] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        _title(root, "RC Generator", "Transmit standards-compliant binary RC frames through a USB-UART adapter.")

        toolbar = _card()
        tools = QGridLayout(toolbar)
        tools.setContentsMargins(14, 12, 14, 12)
        self.port = QComboBox()
        self.protocol = QComboBox()
        self.protocol.addItems(protocol_names(tx=True))
        self.baud = QSpinBox()
        self.baud.setRange(1200, 4_000_000)
        self.uart = QComboBox()
        self.uart.addItems(("8N1", "8N2", "8E1", "8E2", "8O1", "8O2"))
        self.rate = QSpinBox()
        self.rate.setRange(1, 500)
        self.rate.setSuffix(" Hz")
        self.rate.setValue(100)
        self.refresh_button = QPushButton("Refresh")
        self.start_button = QPushButton("Start TX")
        self.start_button.setProperty("class", "primary")
        self.stop_button = QPushButton("EMERGENCY STOP")
        self.stop_button.setProperty("class", "danger")
        self.stop_button.setEnabled(False)
        self.tx_status = QLabel("●  TX idle · 0 packets")
        self.tx_status.setProperty("class", "muted")
        for column, (caption, widget) in enumerate((("COM port", self.port), ("Protocol", self.protocol), ("Baud rate", self.baud), ("UART", self.uart), ("Packet rate", self.rate))):
            lab = QLabel(caption)
            lab.setProperty("class", "muted")
            tools.addWidget(lab, 0, column)
            tools.addWidget(widget, 1, column)
        tools.addWidget(self.refresh_button, 1, 5)
        tools.addWidget(self.start_button, 1, 6)
        tools.addWidget(self.stop_button, 1, 7)
        tools.addWidget(self.tx_status, 2, 0, 1, 8)
        root.addWidget(toolbar)

        mode_card = _card()
        modes = QHBoxLayout(mode_card)
        modes.setContentsMargins(14, 10, 14, 10)
        self.mode = QComboBox()
        self.mode.addItems(("Manual", "Sweep", "Step", "Preset", "Sequence"))
        self.active_channel = QSpinBox()
        self.active_channel.setRange(1, 16)
        self.active_channel.setPrefix("CH ")
        self.step = QSpinBox()
        self.step.setRange(1, 500)
        self.step.setValue(50)
        self.step.setSuffix(" µs")
        self.minimum = QPushButton("All Minimum")
        self.center = QPushButton("All Neutral")
        self.maximum = QPushButton("All Maximum")
        self.save = QPushButton("Save Profile")
        self.load = QPushButton("Load Profile")
        modes.addWidget(QLabel("Mode"))
        modes.addWidget(self.mode)
        modes.addWidget(QLabel("Motion channel"))
        modes.addWidget(self.active_channel)
        modes.addWidget(QLabel("Step"))
        modes.addWidget(self.step)
        modes.addStretch()
        for button in (self.minimum, self.center, self.maximum, self.load, self.save):
            modes.addWidget(button)
        root.addWidget(mode_card)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        for index in range(16):
            channel = GeneratorChannel(index)
            channel.value_changed.connect(self._channel_changed)
            self.channels.append(channel)
            grid.addWidget(channel, index // 2, index % 2)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        self.refresh_button.clicked.connect(self.refresh_ports)
        self.protocol.currentTextChanged.connect(self._protocol_changed)
        self.start_button.clicked.connect(self.start_tx)
        self.stop_button.clicked.connect(self.stop_tx)
        self.mode.currentTextChanged.connect(self._mode_changed)
        self.active_channel.valueChanged.connect(self._mode_changed)
        self.step.valueChanged.connect(self._mode_changed)
        self.minimum.clicked.connect(lambda: self._set_all(1000))
        self.center.clicked.connect(lambda: self._set_all(1500))
        self.maximum.clicked.connect(lambda: self._set_all(2000))
        self.save.clicked.connect(self.save_profile)
        self.load.clicked.connect(self.load_profile)
        self.refresh_ports()
        self._protocol_changed(self.protocol.currentText())

    def refresh_ports(self) -> None:
        selected = self.port.currentData()
        self.ports = list_serial_ports()
        self.port.clear()
        for item in self.ports:
            self.port.addItem(item.label, item.device)
        if selected and self.port.findData(selected) >= 0:
            self.port.setCurrentIndex(self.port.findData(selected))
        if not self.ports:
            self.port.addItem("No serial ports found", "")

    def _protocol_changed(self, name: str) -> None:
        if name not in REGISTRY:
            return
        config = REGISTRY[name].config
        self.baud.setValue(config.baudrate)
        self.uart.setCurrentText(config.uart_format)
        self.baud.setEnabled(config.custom_baudrate)
        self.rate.setRange(config.tx_rate_min, config.tx_rate_max)
        self.rate.setValue(max(config.tx_rate_min, min(config.tx_rate_max, 100)))
        self.uart.setEnabled(False)
        self.start_button.setToolTip(config.note)

    def _profile(self) -> GeneratorProfile:
        sequence = [
            {"duration": 1.0, "channels": [1000, 1500, 1500, 1500]},
            {"duration": 1.0, "channels": [2000, 1500, 1500, 1500]},
            {"duration": 1.0, "channels": [1500] * 4},
        ]
        return GeneratorProfile(
            name="RC Reader profile",
            protocol=self.protocol.currentText(),
            baudrate=self.baud.value(),
            uart_format=self.uart.currentText(),
            frame_rate=self.rate.value(),
            channels=[channel.value.value() for channel in self.channels],
            enabled=[channel.enabled.isChecked() for channel in self.channels],
            mode=self.mode.currentText(),
            active_channel=self.active_channel.value() - 1,
            step_size=self.step.value(),
            sequence=sequence,
        )

    def start_tx(self) -> None:
        device = self.port.currentData()
        if not device:
            QMessageBox.warning(self, "No COM port", "Select a USB-UART adapter first.")
            return
        adapter = next((item.adapter for item in self.ports if item.device == device), "Serial port")
        warning = transmission_warning(self.protocol.currentText(), adapter)
        message = "Start continuous RC transmission?\n\nChannels begin at the values shown. Remove propellers and isolate the power stage before bench testing."
        if warning:
            message += f"\n\nHardware warning: {warning}"
        answer = QMessageBox.question(self, "Confirm RC transmission", message, QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes:
            return
        self.generator.start(device, self._profile())
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.protocol.setEnabled(False)
        self.port.setEnabled(False)

    def stop_tx(self) -> None:
        self.generator.stop()
        self.stop_button.setEnabled(False)

    def _channel_changed(self, index: int, value: int) -> None:
        self.generator.update_channel(index, value)

    def _mode_changed(self, _value: Any = None) -> None:
        mode = self.mode.currentText()
        if mode == "Preset":
            self._set_all(1500)
        self.generator.set_mode(mode, self.active_channel.value() - 1, self.step.value())

    def _set_all(self, value: int) -> None:
        for channel in self.channels:
            channel.value.setValue(value)

    def save_profile(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save generator profile", "generator-profile.json", "JSON profile (*.json)")
        if path:
            try:
                save_profile(path, self._profile())
            except Exception as exc:
                QMessageBox.critical(self, "Profile", str(exc))

    def load_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load generator profile", "", "JSON profile (*.json)")
        if not path:
            return
        try:
            profile = load_profile(path)
            self.protocol.setCurrentText(profile.protocol)
            self.baud.setValue(profile.baudrate)
            self.uart.setCurrentText(profile.uart_format)
            self.rate.setValue(round(profile.frame_rate))
            self.mode.setCurrentText(profile.mode)
            self.active_channel.setValue(profile.active_channel + 1)
            self.step.setValue(profile.step_size)
            for index, value in enumerate(profile.channels[: len(self.channels)]):
                self.channels[index].value.setValue(value)
                self.channels[index].enabled.setChecked(profile.enabled[index])
        except Exception as exc:
            QMessageBox.critical(self, "Profile", str(exc))

    def handle_event(self, kind: str, payload: Any) -> None:
        if kind == "tx_connected":
            self.tx_status.setText("●  TX active · 0 packets")
            self.tx_status.setProperty("class", "statusOk")
            self.style().polish(self.tx_status)
        elif kind == "tx_frame":
            _timestamp, _frame, count = payload
            self.tx_status.setText(f"●  TX active · {count:,} packets")
        elif kind == "tx_error":
            self.tx_status.setText("●  TX error")
            self.tx_status.setProperty("class", "statusError")
            self.style().polish(self.tx_status)
            QMessageBox.critical(self, "RC transmission", str(payload))
        elif kind == "tx_disconnected":
            self.tx_status.setText(f"●  TX stopped · {self.generator.sent_packets:,} packets")
            self.tx_status.setProperty("class", "muted")
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.protocol.setEnabled(True)
            self.port.setEnabled(True)
            self.style().polish(self.tx_status)


class DiagnosticsPage(QWidget):
    MAX_ROWS = 2000

    def __init__(self) -> None:
        super().__init__()
        self.paused = False
        self.latest_packet: ChannelFrame | ProtocolPacket | None = None
        self.error_count = 0
        self.last_decode_errors = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        _title(root, "Diagnostics", "Bounded RX/TX capture, decoded packet structure and error counters.")
        toolbar = _card()
        tools = QHBoxLayout(toolbar)
        self.pause = QPushButton("Pause")
        self.clear = QPushButton("Clear")
        self.export = QPushButton("Export Log")
        self.counts = QLabel("0 entries · 0 errors")
        self.counts.setProperty("class", "muted")
        tools.addWidget(self.pause)
        tools.addWidget(self.clear)
        tools.addWidget(self.export)
        tools.addStretch()
        tools.addWidget(self.counts)
        root.addWidget(toolbar)

        body = QHBoxLayout()
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("Time", "Direction", "State", "Bytes", "HEX"))
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.structure = QTreeWidget()
        self.structure.setHeaderLabels(("Decoded field", "Value", "Offset"))
        self.structure.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.structure.header().setSectionResizeMode(1, QHeaderView.Stretch)
        body.addWidget(self.table, 3)
        body.addWidget(self.structure, 2)
        root.addLayout(body, 1)

        self.pause.clicked.connect(self.toggle_pause)
        self.clear.clicked.connect(self.clear_log)
        self.export.clicked.connect(self.export_log)

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause.setText("Resume" if self.paused else "Pause")

    def clear_log(self) -> None:
        self.table.setRowCount(0)
        self.structure.clear()
        self.error_count = 0
        self.last_decode_errors = 0
        self._update_counts()

    def _append(self, direction: str, state: str, data: bytes, color: str) -> None:
        if self.paused:
            return
        if self.table.rowCount() >= self.MAX_ROWS:
            self.table.removeRow(0)
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = (datetime.now().strftime("%H:%M:%S.%f")[:-3], direction, state, str(len(data)), data.hex(" ").upper())
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setForeground(QColor(color))
            self.table.setItem(row, column, item)
        self.table.scrollToBottom()
        self._update_counts()

    def _show_packet(self, packet: ChannelFrame | ProtocolPacket) -> None:
        self.structure.clear()
        root = QTreeWidgetItem((packet.protocol, getattr(packet, "frame_name", "") or "RC frame", ""))
        self.structure.addTopLevelItem(root)
        for field in inspect_packet(packet):
            root.addChild(QTreeWidgetItem((field.name, str(field.value), f"{field.offset}:{field.length}")))
        for key, value in packet.metadata:
            root.addChild(QTreeWidgetItem((key, value, "metadata")))
        root.setExpanded(True)

    def _update_counts(self) -> None:
        self.counts.setText(f"{self.table.rowCount():,} entries · {self.error_count:,} errors · memory limit {self.MAX_ROWS:,}")

    def export_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export diagnostic log", "rc-diagnostics.csv", "CSV (*.csv);;Text (*.txt)")
        if not path:
            return
        lines = ["time,direction,state,bytes,hex"]
        for row in range(self.table.rowCount()):
            values = [self.table.item(row, column).text().replace('"', '""') for column in range(5)]
            lines.append(",".join(f'"{value}"' for value in values))
        Path(path).write_text("\n".join(lines), encoding="utf-8-sig")

    def handle_event(self, kind: str, payload: Any) -> None:
        if kind == "raw":
            _timestamp, data = payload
            self._append("RX", "RAW", data, "#8B949E")
        elif kind == "frame" and isinstance(payload, ChannelFrame):
            self._append("RX", "VALID", payload.raw_frame, "#3FB950")
            self._show_packet(payload)
        elif kind == "packet" and isinstance(payload, ProtocolPacket):
            self._append("RX", "VALID", payload.raw_frame, "#3FB950")
            self._show_packet(payload)
        elif kind == "tx_frame":
            _timestamp, frame, _count = payload
            self._append("TX", "SENT", frame, "#58A6FF")
        elif kind in {"error", "tx_error", "scan_error"}:
            self.error_count += 1
            self._append("SYS", "ERROR", str(payload).encode("utf-8", "replace"), "#F85149")
        elif kind == "stats" and isinstance(payload, ParserStats) and payload.invalid_frames:
            self.error_count = max(self.error_count, payload.invalid_frames)
            if payload.invalid_frames > self.last_decode_errors:
                delta = payload.invalid_frames - self.last_decode_errors
                self._append("RX", f"DECODE ERROR +{delta}", b"", "#F85149")
            self.last_decode_errors = payload.invalid_frames
            self._update_counts()
