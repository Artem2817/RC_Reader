from __future__ import annotations

from collections import deque

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class ChannelBar(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.minimum = 988
        self.maximum = 2012
        self.center = 1500
        self.value = 1500
        self.setMinimumHeight(18)

    def set_range(self, minimum: int, center: int, maximum: int) -> None:
        self.minimum, self.center, self.maximum = minimum, center, maximum
        self.update()

    def set_value(self, value: int) -> None:
        self.value = value
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(1, 4, -1, -4)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#303846"))
        painter.drawRoundedRect(rect, 5, 5)
        span = max(1, self.maximum - self.minimum)
        ratio = max(0.0, min(1.0, (self.value - self.minimum) / span))
        fill = rect.adjusted(0, 0, -round(rect.width() * (1 - ratio)), 0)
        painter.setBrush(QColor("#58A6FF"))
        painter.drawRoundedRect(fill, 5, 5)
        center_x = rect.left() + round(rect.width() * (self.center - self.minimum) / span)
        painter.setPen(QPen(QColor("#E6EDF3"), 1))
        painter.drawLine(center_x, rect.top() - 2, center_x, rect.bottom() + 2)


class HistoryPlot(QWidget):
    """Lightweight antialiased live plot with bounded memory."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.values: deque[int] = deque(maxlen=300)
        self.setMinimumHeight(115)

    def clear(self) -> None:
        self.values.clear()
        self.update()

    def push(self, value: int) -> None:
        self.values.append(value)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        area = self.rect().adjusted(10, 8, -10, -10)
        painter.setPen(QPen(QColor("#202936"), 1))
        for fraction in (0.25, 0.5, 0.75):
            y = area.top() + round(area.height() * fraction)
            painter.drawLine(area.left(), y, area.right(), y)
        if len(self.values) < 2:
            painter.setPen(QColor("#8B949E"))
            painter.drawText(area, Qt.AlignCenter, "Waiting for channel data")
            return
        values = list(self.values)
        span = max(1, len(values) - 1)
        path = QPainterPath()
        for index, value in enumerate(values):
            x = area.left() + area.width() * index / span
            y = area.bottom() - area.height() * max(0, min(1000, value - 1000)) / 1000
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(QColor("#58A6FF"), 2))
        painter.drawPath(path)


class MonitorChannel(QFrame):
    def __init__(self, index: int) -> None:
        super().__init__()
        self.setObjectName("metric")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        top = QHBoxLayout()
        self.name = QLabel(f"CH {index + 1:02d}")
        self.value = QLabel("—")
        self.value.setProperty("class", "metricValue")
        top.addWidget(self.name)
        top.addStretch()
        top.addWidget(self.value)
        self.bar = ChannelBar()
        layout.addLayout(top)
        layout.addWidget(self.bar)

    def update_value(self, value_us: int, minimum: int = 988, maximum: int = 2012) -> None:
        self.value.setText(f"{value_us} µs")
        self.bar.set_range(minimum, (minimum + maximum) // 2, maximum)
        self.bar.set_value(value_us)


class GeneratorChannel(QFrame):
    value_changed = Signal(int, int)

    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index
        self.setObjectName("metric")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        self.enabled = QCheckBox(f"CH {index + 1:02d}")
        self.enabled.setChecked(True)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(750, 2250)
        self.slider.setValue(1500)
        self.value = QSpinBox()
        self.value.setRange(750, 2250)
        self.value.setSuffix(" µs")
        self.value.setValue(1500)
        center = QPushButton("Center")
        center.setToolTip("Return this channel to 1500 µs")
        layout.addWidget(self.enabled)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.value)
        layout.addWidget(center)
        self.slider.valueChanged.connect(self.value.setValue)
        self.value.valueChanged.connect(self.slider.setValue)
        self.value.valueChanged.connect(lambda value: self.value_changed.emit(self.index, value))
        center.clicked.connect(lambda: self.value.setValue(1500))


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "—") -> None:
        super().__init__()
        self.setObjectName("metric")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        label = QLabel(title)
        label.setProperty("class", "muted")
        self.value = QLabel(value)
        self.value.setProperty("class", "metricValue")
        layout.addWidget(label)
        layout.addWidget(self.value)
