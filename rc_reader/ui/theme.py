from __future__ import annotations


PALETTE = {
    "background": "#0D1117",
    "surface": "#161B22",
    "panel": "#1C2430",
    "border": "#303846",
    "text": "#E6EDF3",
    "muted": "#8B949E",
    "accent": "#58A6FF",
    "success": "#3FB950",
    "warning": "#D29922",
    "error": "#F85149",
}


STYLESHEET = """
* { font-family: "Segoe UI Variable", "Segoe UI"; font-size: 10pt; color: #E6EDF3; }
QMainWindow, QWidget#root { background: #0D1117; }
QWidget { background: transparent; }
QFrame#sidebar, QFrame.card, QFrame.toolbar { background: #161B22; border: 1px solid #303846; border-radius: 10px; }
QFrame.metric { background: #1C2430; border: 1px solid #303846; border-radius: 8px; }
QLabel.title { font-size: 21pt; font-weight: 650; }
QLabel.subtitle, QLabel.muted { color: #8B949E; }
QLabel.metricValue { font-size: 15pt; font-weight: 650; }
QLabel.statusOk { color: #3FB950; font-weight: 600; }
QLabel.statusWarn { color: #D29922; font-weight: 600; }
QLabel.statusError { color: #F85149; font-weight: 600; }
QPushButton { background: #212936; border: 1px solid #303846; border-radius: 8px; padding: 7px 13px; }
QPushButton:hover { background: #2A3442; border-color: #4A586B; }
QPushButton:pressed { background: #18202B; }
QPushButton:disabled { color: #66707D; background: #171D25; }
QPushButton.primary { background: #1F6FEB; border-color: #388BFD; font-weight: 600; }
QPushButton.primary:hover { background: #388BFD; }
QPushButton.danger { background: #A83232; border-color: #F85149; font-weight: 650; }
QPushButton.danger:hover { background: #C63C3C; }
QPushButton.nav { text-align: left; border: 0; padding: 10px 12px; background: transparent; }
QPushButton.nav:hover { background: #1C2430; }
QPushButton.nav:checked { background: #1F3046; color: #79C0FF; border-left: 3px solid #58A6FF; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit { background: #0D1117; border: 1px solid #303846; border-radius: 7px; padding: 6px 9px; min-height: 20px; }
QComboBox:hover, QSpinBox:hover, QLineEdit:hover { border-color: #4A586B; }
QComboBox:focus, QSpinBox:focus, QLineEdit:focus { border-color: #58A6FF; }
QComboBox QAbstractItemView { background: #1C2430; border: 1px solid #303846; selection-background-color: #1F6FEB; }
QScrollArea { border: 0; }
QScrollBar:vertical { background: #0D1117; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #303846; border-radius: 5px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QSlider::groove:horizontal { height: 6px; background: #303846; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #58A6FF; border-radius: 3px; }
QSlider::handle:horizontal { width: 16px; margin: -5px 0; background: #E6EDF3; border: 2px solid #58A6FF; border-radius: 8px; }
QProgressBar { background: #0D1117; border: 1px solid #303846; border-radius: 6px; text-align: center; }
QProgressBar::chunk { background: #1F6FEB; border-radius: 5px; }
QTableWidget, QTreeWidget { background: #0D1117; alternate-background-color: #111820; border: 1px solid #303846; border-radius: 8px; gridline-color: #202936; }
QHeaderView::section { background: #1C2430; color: #8B949E; border: 0; border-bottom: 1px solid #303846; padding: 7px; font-weight: 600; }
QTableWidget::item:selected, QTreeWidget::item:selected { background: #1F3046; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #4A586B; border-radius: 4px; background: #0D1117; }
QCheckBox::indicator:checked { background: #1F6FEB; border-color: #58A6FF; }
QToolTip { background: #1C2430; color: #E6EDF3; border: 1px solid #4A586B; padding: 5px; }
"""
