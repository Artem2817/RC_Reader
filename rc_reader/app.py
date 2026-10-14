from __future__ import annotations

from collections import deque
import ctypes
from queue import Empty, Queue
import statistics
import sys
from time import monotonic
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .analysis import CalibrationWizard, DualComparator, SignalAnalyzer
from .encoder import OUTPUT_PROTOCOLS, encode_channels
from .inspector import inspect_packet
from .plugin_loader import load_plugins, plugin_directory
from .profiles import PROTOCOL_BY_NAME as BUILTIN_PROTOCOL_BY_NAME
from .profiles import PROTOCOL_SPECS
from .protocols import ChannelFrame, ParserStats, ProtocolPacket
from .reports import export_html_report, export_pdf_report
from .serial_worker import (
    UART_FORMATS,
    ScanWorker,
    SerialConfig,
    SerialOutputWorker,
    SerialWorker,
)
from .session import ReplayWorker, SessionRecorder, export_csv


PLUGIN_PROTOCOLS = load_plugins()
ALL_PROTOCOL_SPECS = PROTOCOL_SPECS + tuple(plugin.spec for plugin in PLUGIN_PROTOCOLS)
PROTOCOLS = tuple(spec.name for spec in ALL_PROTOCOL_SPECS)
PROTOCOL_BY_NAME = {
    **BUILTIN_PROTOCOL_BY_NAME,
    **{plugin.spec.name: plugin.spec for plugin in PLUGIN_PROTOCOLS},
}
BAUDRATES = (
    "420000",
    "416666",
    "125000",
    "115200",
    "100000",
    "921600",
    "460800",
    "230400",
    "57600",
)
PROTOCOL_DEFAULTS = {
    spec.name: str(spec.baudrate) for spec in ALL_PROTOCOL_SPECS
}
SERIAL_FORMATS = {
    spec.name: spec.uart_format for spec in ALL_PROTOCOL_SPECS
}
MAX_DISPLAY_CHANNELS = 32

COLORS = {
    "window": "#09111F",
    "panel": "#0F1A2B",
    "surface": "#17243A",
    "surface_hover": "#20314D",
    "border": "#263650",
    "text": "#E7EDF7",
    "muted": "#8FA1B9",
    "accent": "#3B82F6",
    "accent_hover": "#60A5FA",
    "success": "#2DD4BF",
    "track": "#1C2A40",
    "disabled": "#52627A",
}


class RCReaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RC Reader — Universal RC Protocol Monitor")
        self.geometry("1220x900")
        self.minsize(1040, 760)
        self.configure(background=COLORS["window"])

        self.events: Queue[tuple[str, object]] = Queue()
        self.worker: SerialWorker | None = None
        self.secondary_worker: SerialWorker | None = None
        self.output_worker: SerialOutputWorker | None = None
        self.scan_worker: ScanWorker | None = None
        self.replay_worker: ReplayWorker | None = None
        self.recorder = SessionRecorder()
        self.analyzer = SignalAnalyzer(MAX_DISPLAY_CHANNELS)
        self.calibration = CalibrationWizard(MAX_DISPLAY_CHANNELS)
        self.comparator = DualComparator(MAX_DISPLAY_CHANNELS)
        self.frame_times: deque[float] = deque()
        self.frame_intervals_ms: deque[float] = deque(maxlen=500)
        self.last_frame_at: float | None = None
        self.session_started_at = monotonic()
        self.session_frames: deque[tuple[float, ChannelFrame]] = deque(maxlen=200_000)
        self.channel_history: list[deque[tuple[float, int]]] = [
            deque(maxlen=1200) for _ in range(MAX_DISPLAY_CHANNELS)
        ]
        self.raw_frame_by_id: dict[str, ChannelFrame | ProtocolPacket] = {}
        self.last_protocol = "CRSF"
        self.last_generator_frame_at = 0.0
        self.last_alert_sound_at = 0.0

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value=PROTOCOL_DEFAULTS["CRSF"])
        self.protocol_var = tk.StringVar(value="CRSF")
        self.format_var = tk.StringVar(value=SERIAL_FORMATS["CRSF"])
        self.connection_var = tk.StringVar(value="Не подключено")
        self.stats_var = tk.StringVar(value="Кадры: 0   FPS: 0.0   Ошибки: 0   Байт: 0")
        self.flags_var = tk.StringVar(value="Failsafe: —   Frame lost: —   CH17: —   CH18: —")
        self.hex_var = tk.StringVar(value="—")
        self.graph_channel_var = tk.StringVar(value="CH 01")
        self.graph_window_var = tk.StringVar(value="10")
        self.graph_overlay_var = tk.BooleanVar(value=False)
        self.graph_paused_var = tk.BooleanVar(value=False)
        self.graph_paused_at: float | None = None
        self.graph_markers: deque[float] = deque(maxlen=50)
        self.graph_stats_var = tk.StringVar(value="Ожидание данных")
        self.metadata_var = tk.StringVar(value="Телеметрия и метаданные появятся после приёма")
        self.record_var = tk.StringVar(value="● REC")
        self.wiring_var = tk.StringVar(value=PROTOCOL_BY_NAME["CRSF"].wiring_note)
        self.signal_summary_var = tk.StringVar(value="Ожидание RC-кадров")
        self.calibration_role_var = tk.StringVar(value=CalibrationWizard.ROLES[0])
        self.calibration_status_var = tk.StringVar(value="Калибровка не запущена")
        self.alarm_enabled_var = tk.BooleanVar(value=True)
        self.dropout_threshold_var = tk.StringVar(value="50")
        self.jitter_threshold_var = tk.StringVar(value="5")
        self.lq_threshold_var = tk.StringVar(value="30")
        self.lab_mode_var = tk.StringVar(value="Сравнение RX")
        self.lab_port_var = tk.StringVar()
        self.lab_protocol_var = tk.StringVar(value="SBUS")
        self.lab_baud_var = tk.StringVar(value="100000")
        self.lab_format_var = tk.StringVar(value="8E2")
        self.lab_status_var = tk.StringVar(value="Второй порт не подключён")
        self.compare_summary_var = tk.StringVar(value="Ожидание данных с двух портов")
        self.generator_rate_var = tk.StringVar(value="50")
        self.generator_channel_vars = [
            tk.DoubleVar(value=1000 if index == 2 else 1500)
            for index in range(8)
        ]

        self.channel_bars: list[ttk.Progressbar] = []
        self.raw_vars: list[tk.StringVar] = []
        self.us_vars: list[tk.StringVar] = []
        self.percent_vars: list[tk.StringVar] = []

        self._configure_style()
        self._build_ui()
        self.after(10, self._enable_dark_title_bar)
        self.protocol_var.trace_add("write", self._on_protocol_changed)
        self.refresh_ports()
        self.after(30, self._poll_events)
        self.after(120, self._draw_graph)
        self.after(200, self._refresh_signal_lab)
        self.after(20, self._generator_tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        self.option_add("*Font", ("Segoe UI", 10))
        self.option_add("*TCombobox*Listbox.background", COLORS["surface"])
        self.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", COLORS["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")

        style.configure(".", background=COLORS["window"], foreground=COLORS["text"])
        style.configure("App.TFrame", background=COLORS["window"])
        style.configure("TLabel", background=COLORS["window"], foreground=COLORS["text"])
        style.configure(
            "TCheckbutton",
            background=COLORS["window"],
            foreground=COLORS["text"],
            indicatorbackground=COLORS["surface"],
            indicatorforeground=COLORS["accent"],
        )
        style.map(
            "TCheckbutton",
            background=[("active", COLORS["window"])],
            foreground=[("disabled", COLORS["disabled"])],
        )
        style.configure("TPanedwindow", background=COLORS["window"])
        style.configure(
            "Horizontal.TScale",
            background=COLORS["window"],
            troughcolor=COLORS["track"],
        )
        style.configure(
            "Title.TLabel",
            background=COLORS["window"],
            foreground=COLORS["text"],
            font=("Segoe UI Variable Display", 22, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=COLORS["window"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 10),
        )
        style.configure(
            "Header.TLabel",
            background=COLORS["window"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "Status.TLabel",
            background=COLORS["window"],
            foreground=COLORS["success"],
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "TLabelframe",
            background=COLORS["window"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "TLabelframe.Label",
            background=COLORS["window"],
            foreground=COLORS["accent_hover"],
            font=("Segoe UI", 10, "bold"),
            padding=(2, 0, 6, 0),
        )
        style.configure(
            "TButton",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["surface"],
            darkcolor=COLORS["surface"],
            padding=(12, 7),
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "TButton",
            background=[
                ("active", COLORS["surface_hover"]),
                ("pressed", COLORS["border"]),
                ("disabled", COLORS["track"]),
            ],
            foreground=[("disabled", COLORS["disabled"])],
        )
        style.configure(
            "Accent.TButton",
            background=COLORS["accent"],
            foreground="#FFFFFF",
            bordercolor=COLORS["accent"],
            lightcolor=COLORS["accent"],
            darkcolor=COLORS["accent"],
            padding=(14, 7),
        )
        style.map(
            "Accent.TButton",
            background=[
                ("active", COLORS["accent_hover"]),
                ("pressed", "#2563EB"),
                ("disabled", COLORS["track"]),
            ],
            foreground=[("disabled", COLORS["disabled"])],
        )
        style.configure(
            "TCombobox",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            fieldbackground=COLORS["surface"],
            arrowcolor=COLORS["muted"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            selectbackground=COLORS["accent"],
            selectforeground="#FFFFFF",
            padding=5,
        )
        style.map(
            "TCombobox",
            background=[
                ("readonly", COLORS["surface"]),
                ("active", COLORS["surface_hover"]),
                ("disabled", COLORS["track"]),
            ],
            fieldbackground=[
                ("readonly", COLORS["surface"]),
                ("disabled", COLORS["track"]),
            ],
            foreground=[("disabled", COLORS["disabled"])],
            arrowcolor=[("disabled", COLORS["disabled"])],
        )
        style.configure(
            "TEntry",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            fieldbackground=COLORS["surface"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            insertcolor=COLORS["text"],
            padding=6,
        )
        style.map(
            "TEntry",
            fieldbackground=[("readonly", COLORS["surface"])],
            foreground=[("readonly", COLORS["muted"])],
        )
        style.configure(
            "Channel.Horizontal.TProgressbar",
            troughcolor=COLORS["track"],
            background=COLORS["accent"],
            bordercolor=COLORS["track"],
            lightcolor=COLORS["accent"],
            darkcolor=COLORS["accent"],
            thickness=13,
        )
        style.configure(
            "TNotebook",
            background=COLORS["window"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            tabmargins=(0, 4, 0, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background=COLORS["surface"],
            foreground=COLORS["muted"],
            padding=(16, 8),
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", COLORS["accent"]),
                ("active", COLORS["surface_hover"]),
            ],
            foreground=[("selected", "#FFFFFF"), ("active", COLORS["text"])],
        )
        style.configure(
            "Treeview",
            background=COLORS["surface"],
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            rowheight=26,
        )
        style.map(
            "Treeview",
            background=[("selected", COLORS["accent"])],
            foreground=[("selected", "#FFFFFF")],
        )
        style.configure(
            "Treeview.Heading",
            background=COLORS["track"],
            foreground=COLORS["muted"],
            bordercolor=COLORS["border"],
            font=("Segoe UI", 9, "bold"),
        )

    def _enable_dark_title_bar(self) -> None:
        if sys.platform != "win32":
            return
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            enabled = ctypes.c_int(1)
            for attribute in (20, 19):
                result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    attribute,
                    ctypes.byref(enabled),
                    ctypes.sizeof(enabled),
                )
                if result == 0:
                    break
        except Exception:
            pass

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=(18, 16), style="App.TFrame")
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="RC READER", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="UNIVERSAL SERIAL RC PROTOCOL MONITOR",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(0, 16))

        controls = ttk.LabelFrame(outer, text="ПОДКЛЮЧЕНИЕ", padding=(12, 10))
        controls.pack(fill="x")
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(4, weight=1)

        ttk.Label(controls, text="COM-порт").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.port_combo = ttk.Combobox(controls, textvariable=self.port_var, width=24)
        self.port_combo.grid(row=0, column=1, sticky="ew")
        ttk.Button(controls, text="Обновить", command=self.refresh_ports).grid(
            row=0, column=2, padx=(6, 18)
        )

        ttk.Label(controls, text="Протокол").grid(row=0, column=3, sticky="w", padx=(0, 6))
        self.protocol_combo = ttk.Combobox(
            controls,
            textvariable=self.protocol_var,
            values=PROTOCOLS,
            state="readonly",
            width=14,
        )
        self.protocol_combo.grid(row=0, column=4, sticky="ew")

        ttk.Label(controls, text="Скорость, бод").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=(10, 0)
        )
        self.baud_combo = ttk.Combobox(
            controls, textvariable=self.baud_var, values=BAUDRATES, width=24
        )
        self.baud_combo.grid(row=1, column=1, sticky="ew", pady=(10, 0))
        ttk.Label(controls, text="Формат UART").grid(
            row=1, column=2, sticky="e", padx=(6, 6), pady=(10, 0)
        )
        self.format_combo = ttk.Combobox(
            controls,
            textvariable=self.format_var,
            values=UART_FORMATS,
            state="readonly",
            width=8,
        )
        self.format_combo.grid(
            row=1, column=3, sticky="ew", padx=(0, 18), pady=(10, 0)
        )

        self.connect_button = ttk.Button(
            controls,
            text="Подключить",
            command=self.toggle_connection,
            style="Accent.TButton",
        )
        self.connect_button.grid(row=1, column=4, sticky="ew", pady=(10, 0))

        ttk.Label(
            controls,
            textvariable=self.wiring_var,
            style="Subtitle.TLabel",
        ).grid(row=2, column=0, columnspan=5, sticky="w", pady=(9, 0))

        actions = ttk.Frame(controls, style="App.TFrame")
        actions.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(9, 0))
        self.scan_button = ttk.Button(
            actions, text="⚡ Автоскан", command=self.start_scan
        )
        self.scan_button.pack(side="left")
        self.record_button = ttk.Button(
            actions, textvariable=self.record_var, command=self.toggle_recording
        )
        self.record_button.pack(side="left", padx=(7, 0))
        self.replay_button = ttk.Button(
            actions, text="▶ Replay", command=self.start_replay
        )
        self.replay_button.pack(side="left", padx=(7, 0))
        self.csv_button = ttk.Button(
            actions, text="Экспорт CSV", command=self.export_session_csv
        )
        self.csv_button.pack(side="left", padx=(7, 0))
        self.clear_button = ttk.Button(
            actions, text="Очистить", command=self.clear_session
        )
        self.clear_button.pack(side="left", padx=(7, 0))

        status = ttk.Frame(outer, padding=(0, 10))
        status.pack(fill="x")
        self.status_label = ttk.Label(
            status, textvariable=self.connection_var, style="Status.TLabel"
        )
        self.status_label.pack(side="left")
        ttk.Label(status, textvariable=self.stats_var).pack(side="right")

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)

        channels = ttk.Frame(self.notebook, padding=(10, 7), style="App.TFrame")
        self.notebook.add(channels, text="КАНАЛЫ")
        channels.columnconfigure(1, weight=1)
        channels.columnconfigure(6, weight=1)
        for base_column in (0, 5):
            left_pad = (20, 0) if base_column else (0, 0)
            ttk.Label(channels, text="КАНАЛ", style="Header.TLabel").grid(
                row=0, column=base_column, padx=left_pad
            )
            ttk.Label(channels, text="ПОЛОЖЕНИЕ", style="Header.TLabel").grid(
                row=0, column=base_column + 1
            )
            ttk.Label(channels, text="RAW", style="Header.TLabel").grid(
                row=0, column=base_column + 2, padx=(10, 0)
            )
            ttk.Label(channels, text="µS", style="Header.TLabel").grid(
                row=0, column=base_column + 3, padx=(10, 0)
            )
            ttk.Label(channels, text="%", style="Header.TLabel").grid(
                row=0, column=base_column + 4, padx=(10, 0)
            )

        for index in range(MAX_DISPLAY_CHANNELS):
            raw_var = tk.StringVar(value="—")
            us_var = tk.StringVar(value="—")
            percent_var = tk.StringVar(value="—")
            bar = ttk.Progressbar(
                channels,
                maximum=100,
                value=0,
                style="Channel.Horizontal.TProgressbar",
            )
            bank = index // 16
            row = index % 16 + 1
            base_column = bank * 5
            channel_pad = (20, 0) if bank else (0, 0)
            ttk.Label(channels, text=f"CH {index + 1:02}", width=7).grid(
                row=row,
                column=base_column,
                sticky="w",
                padx=channel_pad,
                pady=2,
            )
            bar.grid(
                row=row,
                column=base_column + 1,
                sticky="ew",
                padx=(5, 0),
                pady=2,
            )
            ttk.Label(channels, textvariable=raw_var, width=6, anchor="e").grid(
                row=row, column=base_column + 2, padx=(10, 0)
            )
            ttk.Label(channels, textvariable=us_var, width=6, anchor="e").grid(
                row=row, column=base_column + 3, padx=(10, 0)
            )
            ttk.Label(channels, textvariable=percent_var, width=7, anchor="e").grid(
                row=row, column=base_column + 4, padx=(10, 0)
            )
            self.channel_bars.append(bar)
            self.raw_vars.append(raw_var)
            self.us_vars.append(us_var)
            self.percent_vars.append(percent_var)

        graph_tab = ttk.Frame(self.notebook, padding=12, style="App.TFrame")
        self.notebook.add(graph_tab, text="ГРАФИК / JITTER")
        graph_toolbar = ttk.Frame(graph_tab, style="App.TFrame")
        graph_toolbar.pack(fill="x", pady=(0, 10))
        ttk.Label(graph_toolbar, text="Канал").pack(side="left")
        ttk.Combobox(
            graph_toolbar,
            textvariable=self.graph_channel_var,
            values=tuple(f"CH {index:02}" for index in range(1, MAX_DISPLAY_CHANNELS + 1)),
            state="readonly",
            width=9,
        ).pack(side="left", padx=(6, 18))
        ttk.Label(graph_toolbar, text="Окно, сек").pack(side="left")
        ttk.Combobox(
            graph_toolbar,
            textvariable=self.graph_window_var,
            values=("2", "5", "10", "30", "60"),
            state="readonly",
            width=7,
        ).pack(side="left", padx=(6, 18))
        ttk.Checkbutton(
            graph_toolbar,
            text="Наложить CH1–CH4",
            variable=self.graph_overlay_var,
        ).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(
            graph_toolbar,
            text="Пауза",
            variable=self.graph_paused_var,
            command=self._toggle_graph_pause,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            graph_toolbar,
            text="Маркер",
            command=self._add_graph_marker,
        ).pack(side="left", padx=(0, 18))
        ttk.Label(
            graph_toolbar,
            textvariable=self.graph_stats_var,
            style="Status.TLabel",
        ).pack(side="left")

        self.graph_canvas = tk.Canvas(
            graph_tab,
            background=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            height=360,
        )
        self.graph_canvas.pack(fill="both", expand=True)
        ttk.Label(
            graph_tab,
            textvariable=self.metadata_var,
            style="Subtitle.TLabel",
            wraplength=1100,
        ).pack(fill="x", pady=(10, 0))

        raw_tab = ttk.Frame(self.notebook, padding=12, style="App.TFrame")
        self.notebook.add(raw_tab, text="RAW-КАДРЫ")
        raw_columns = ("time", "protocol", "frame", "length", "flags")
        self.raw_tree = ttk.Treeview(
            raw_tab,
            columns=raw_columns,
            show="headings",
            height=14,
        )
        raw_headings = {
            "time": "ВРЕМЯ",
            "protocol": "ПРОТОКОЛ",
            "frame": "ТИП КАДРА",
            "length": "БАЙТ",
            "flags": "СОСТОЯНИЕ",
        }
        raw_widths = {"time": 100, "protocol": 100, "frame": 360, "length": 70, "flags": 220}
        for column in raw_columns:
            self.raw_tree.heading(column, text=raw_headings[column])
            self.raw_tree.column(
                column,
                width=raw_widths[column],
                anchor="w" if column not in ("length",) else "center",
            )
        raw_scroll = ttk.Scrollbar(raw_tab, orient="vertical", command=self.raw_tree.yview)
        self.raw_tree.configure(yscrollcommand=raw_scroll.set)
        self.raw_tree.pack(side="left", fill="both", expand=True)
        raw_scroll.pack(side="right", fill="y")
        self.raw_tree.bind("<<TreeviewSelect>>", self._on_raw_selected)

        self._build_signal_lab_tab()
        self._build_packet_inspector_tab()
        self._build_io_lab_tab()

        details = ttk.LabelFrame(outer, text="Последний кадр", padding=8)
        details.pack(fill="x", pady=(10, 0))
        ttk.Label(details, textvariable=self.flags_var).pack(anchor="w")
        hex_entry = ttk.Entry(details, textvariable=self.hex_var, state="readonly")
        hex_entry.pack(fill="x", pady=(5, 0))

    def _build_signal_lab_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12, style="App.TFrame")
        self.notebook.add(tab, text="SIGNAL LAB")

        toolbar = ttk.Frame(tab, style="App.TFrame")
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, textvariable=self.signal_summary_var, style="Status.TLabel").pack(
            side="left"
        )
        ttk.Button(toolbar, text="HTML/PDF отчёт", command=self.export_report).pack(
            side="right"
        )

        settings = ttk.Frame(tab, style="App.TFrame")
        settings.pack(fill="x", pady=(0, 8))
        for label, variable, width in (
            ("Dropout, ms", self.dropout_threshold_var, 6),
            ("Jitter alarm, ms", self.jitter_threshold_var, 6),
            ("LQ alarm, %", self.lq_threshold_var, 6),
        ):
            ttk.Label(settings, text=label).pack(side="left", padx=(0, 5))
            ttk.Entry(settings, textvariable=variable, width=width).pack(
                side="left", padx=(0, 12)
            )
        ttk.Checkbutton(
            settings, text="Звуковые тревоги", variable=self.alarm_enabled_var
        ).pack(side="left")

        calibration = ttk.LabelFrame(tab, text="МАСТЕР КАЛИБРОВКИ", padding=8)
        calibration.pack(fill="x", pady=(0, 8))
        ttk.Label(
            calibration,
            text="Выберите функцию, нажмите старт и двигайте только нужный стик/переключатель:",
        ).pack(side="left")
        ttk.Combobox(
            calibration,
            textvariable=self.calibration_role_var,
            values=CalibrationWizard.ROLES,
            state="readonly",
            width=11,
        ).pack(side="left", padx=8)
        self.calibration_button = ttk.Button(
            calibration, text="Старт", command=self.toggle_calibration
        )
        self.calibration_button.pack(side="left")
        ttk.Label(
            calibration,
            textvariable=self.calibration_status_var,
            style="Subtitle.TLabel",
        ).pack(side="left", padx=12)

        body = ttk.Panedwindow(tab, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, style="App.TFrame")
        right = ttk.Frame(body, style="App.TFrame")
        body.add(left, weight=3)
        body.add(right, weight=2)

        columns = ("channel", "current", "min", "max", "jitter", "noise", "kind")
        self.metrics_tree = ttk.Treeview(left, columns=columns, show="headings", height=14)
        headings = {
            "channel": "КАНАЛ",
            "current": "СЕЙЧАС",
            "min": "MIN",
            "max": "MAX",
            "jitter": "JITTER µs",
            "noise": "ШУМ ЦЕНТРА",
            "kind": "ТИП",
        }
        for column in columns:
            self.metrics_tree.heading(column, text=headings[column])
            self.metrics_tree.column(column, width=80, anchor="center")
        self.metrics_tree.pack(fill="both", expand=True)

        self.stick_canvas = tk.Canvas(
            right,
            background=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            height=220,
        )
        self.stick_canvas.pack(fill="x")
        self.timing_canvas = tk.Canvas(
            right,
            background=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            height=105,
        )
        self.timing_canvas.pack(fill="x", pady=(8, 0))
        ttk.Label(right, text="СОБЫТИЯ / ТРЕВОГИ", style="Header.TLabel").pack(
            anchor="w", pady=(10, 4)
        )
        self.alert_tree = ttk.Treeview(
            right,
            columns=("time", "level", "message"),
            show="headings",
            height=8,
        )
        for column, heading, width in (
            ("time", "ВРЕМЯ", 75),
            ("level", "ТИП", 80),
            ("message", "СОБЫТИЕ", 270),
        ):
            self.alert_tree.heading(column, text=heading)
            self.alert_tree.column(column, width=width, anchor="w")
        self.alert_tree.pack(fill="both", expand=True)

    def _build_packet_inspector_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12, style="App.TFrame")
        self.notebook.add(tab, text="PACKET INSPECTOR")
        ttk.Label(
            tab,
            text="Выберите кадр во вкладке RAW-КАДРЫ — здесь появится разбор полей и CRC.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(0, 8))
        columns = ("offset", "length", "field", "value", "hex")
        self.inspector_tree = ttk.Treeview(tab, columns=columns, show="headings")
        for column, heading, width in (
            ("offset", "OFFSET", 70),
            ("length", "LEN", 55),
            ("field", "ПОЛЕ", 220),
            ("value", "ЗНАЧЕНИЕ", 360),
            ("hex", "HEX", 340),
        ):
            self.inspector_tree.heading(column, text=heading)
            self.inspector_tree.column(column, width=width, anchor="w")
        inspector_scroll = ttk.Scrollbar(
            tab, orient="vertical", command=self.inspector_tree.yview
        )
        self.inspector_tree.configure(yscrollcommand=inspector_scroll.set)
        self.inspector_tree.pack(side="left", fill="both", expand=True)
        inspector_scroll.pack(side="right", fill="y")

    def _build_io_lab_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12, style="App.TFrame")
        self.notebook.add(tab, text="DUAL PORT / TX")

        controls = ttk.LabelFrame(tab, text="ВТОРОЙ COM-ПОРТ", padding=10)
        controls.pack(fill="x")
        for column in range(6):
            controls.columnconfigure(column, weight=1 if column in (1, 3) else 0)
        ttk.Label(controls, text="Режим").grid(row=0, column=0, sticky="w")
        self.lab_mode_combo = ttk.Combobox(
            controls,
            textvariable=self.lab_mode_var,
            values=("Сравнение RX", "Конвертер TX", "Генератор TX"),
            state="readonly",
            width=18,
        )
        self.lab_mode_combo.grid(row=0, column=1, sticky="ew", padx=(6, 12))
        self.lab_mode_combo.bind("<<ComboboxSelected>>", self._on_lab_mode_changed)
        ttk.Label(controls, text="COM").grid(row=0, column=2, sticky="w")
        self.lab_port_combo = ttk.Combobox(
            controls, textvariable=self.lab_port_var, width=24
        )
        self.lab_port_combo.grid(row=0, column=3, sticky="ew", padx=(6, 12))
        ttk.Label(controls, text="Протокол").grid(row=0, column=4, sticky="w")
        self.lab_protocol_combo = ttk.Combobox(
            controls,
            textvariable=self.lab_protocol_var,
            values=PROTOCOLS,
            state="readonly",
            width=13,
        )
        self.lab_protocol_combo.grid(row=0, column=5, sticky="ew", padx=(6, 0))

        ttk.Label(controls, text="Бод").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            controls, textvariable=self.lab_baud_var, values=BAUDRATES, width=18
        ).grid(row=1, column=1, sticky="ew", padx=(6, 12), pady=(8, 0))
        ttk.Label(controls, text="UART").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Combobox(
            controls,
            textvariable=self.lab_format_var,
            values=UART_FORMATS,
            state="readonly",
            width=9,
        ).grid(row=1, column=3, sticky="w", padx=(6, 12), pady=(8, 0))
        self.lab_connect_button = ttk.Button(
            controls,
            text="Подключить",
            command=self.toggle_lab_port,
            style="Accent.TButton",
        )
        self.lab_connect_button.grid(
            row=1, column=4, columnspan=2, sticky="ew", pady=(8, 0)
        )
        ttk.Label(controls, textvariable=self.lab_status_var).grid(
            row=2, column=0, columnspan=6, sticky="w", pady=(8, 0)
        )
        self.lab_protocol_var.trace_add("write", self._on_lab_protocol_changed)

        compare = ttk.LabelFrame(tab, text="СРАВНЕНИЕ ПРИЁМНИКОВ", padding=10)
        compare.pack(fill="x", pady=(10, 0))
        ttk.Label(compare, textvariable=self.compare_summary_var, style="Status.TLabel").pack(
            anchor="w"
        )
        self.compare_tree = ttk.Treeview(
            compare,
            columns=("channel", "primary", "secondary", "delta"),
            show="headings",
            height=5,
        )
        for column, heading in (
            ("channel", "КАНАЛ"),
            ("primary", "RX 1, µs"),
            ("secondary", "RX 2, µs"),
            ("delta", "РАЗНИЦА"),
        ):
            self.compare_tree.heading(column, text=heading)
            self.compare_tree.column(column, width=130, anchor="center")
        self.compare_tree.pack(fill="x", pady=(8, 0))

        generator = ttk.LabelFrame(
            tab, text="ВИРТУАЛЬНЫЙ ПЕРЕДАТЧИК / ГЕНЕРАТОР", padding=10
        )
        generator.pack(fill="both", expand=True, pady=(10, 0))
        top = ttk.Frame(generator, style="App.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="Частота, Hz").pack(side="left")
        ttk.Combobox(
            top,
            textvariable=self.generator_rate_var,
            values=("25", "50", "100", "150", "250", "500"),
            state="readonly",
            width=7,
        ).pack(side="left", padx=6)
        ttk.Label(
            top,
            text="В режиме «Конвертер TX» сюда выводятся входные каналы; "
            "в «Генератор TX» — значения ползунков.",
            style="Subtitle.TLabel",
        ).pack(side="left", padx=12)
        sliders = ttk.Frame(generator, style="App.TFrame")
        sliders.pack(fill="both", expand=True, pady=(8, 0))
        for index, variable in enumerate(self.generator_channel_vars):
            row, column = divmod(index, 4)
            cell = ttk.Frame(sliders, style="App.TFrame")
            cell.grid(row=row, column=column, sticky="ew", padx=6, pady=5)
            sliders.columnconfigure(column, weight=1)
            ttk.Label(cell, text=f"CH{index + 1}").pack(anchor="w")
            ttk.Scale(cell, from_=988, to=2012, variable=variable).pack(fill="x")

    def refresh_ports(self) -> None:
        current = self.port_var.get()
        try:
            from serial.tools import list_ports

            ports = sorted(list_ports.comports(), key=lambda item: item.device)
            labels = [
                f"{item.device} — {item.description}" if item.description else item.device
                for item in ports
            ]
            self.port_combo["values"] = labels
            self.lab_port_combo["values"] = labels
            if current and any(label.startswith(current.split(" — ")[0]) for label in labels):
                pass
            else:
                self.port_var.set(labels[0] if labels else "")
            lab_current = self.lab_port_var.get()
            if not lab_current or not any(
                label.startswith(lab_current.split(" — ")[0]) for label in labels
            ):
                self.lab_port_var.set(labels[1] if len(labels) > 1 else "")
            if not labels:
                self.connection_var.set("COM-порты не найдены")
        except ModuleNotFoundError:
            self.connection_var.set("Не установлен pyserial")
            messagebox.showerror(
                "Нет зависимости",
                "Не найден pyserial.\n\nУстановите его командой:\npy -m pip install -r requirements.txt",
            )

    def toggle_calibration(self) -> None:
        if self.calibration.active_role is None:
            role = self.calibration_role_var.get()
            self.calibration.begin(role)
            self.calibration_button.configure(text="Зафиксировать")
            self.calibration_status_var.set(f"Двигайте только {role} по всему диапазону")
            return
        result = self.calibration.finish()
        self.calibration_button.configure(text="Старт")
        if result is None:
            self.calibration_status_var.set("Недостаточно данных")
            return
        role, channel, minimum, maximum = result
        self.calibration_status_var.set(
            f"{role} → CH{channel} • endpoints {minimum}/{maximum} µs"
        )

    def export_report(self) -> None:
        if self.analyzer.frame_count == 0:
            messagebox.showinfo("Нет данных", "Сначала примите RC-кадры.")
            return
        path = filedialog.asksaveasfilename(
            title="Экспорт отчёта Signal Lab",
            defaultextension=".html",
            filetypes=(("HTML report", "*.html"), ("PDF report", "*.pdf")),
        )
        if not path:
            return
        metrics = [
            self.analyzer.channel_metrics(index)
            for index in range(MAX_DISPLAY_CHANNELS)
        ]
        try:
            if path.lower().endswith(".pdf"):
                export_pdf_report(path, self.analyzer.snapshot(), metrics, self.analyzer.alerts)
            else:
                export_html_report(path, self.analyzer.snapshot(), metrics, self.analyzer.alerts)
            self.connection_var.set(f"Отчёт сохранён: {path}")
        except OSError as exc:
            messagebox.showerror("Ошибка отчёта", str(exc))

    def _on_lab_mode_changed(self, _event: object = None) -> None:
        transmitting = self.lab_mode_var.get() != "Сравнение RX"
        self.lab_protocol_combo.configure(
            values=OUTPUT_PROTOCOLS if transmitting else PROTOCOLS
        )
        if transmitting and self.lab_protocol_var.get() not in OUTPUT_PROTOCOLS:
            self.lab_protocol_var.set("SBUS")

    def _on_lab_protocol_changed(self, *_args: object) -> None:
        protocol = self.lab_protocol_var.get()
        spec = PROTOCOL_BY_NAME.get(protocol)
        if spec is not None:
            self.lab_baud_var.set(str(spec.baudrate))
            self.lab_format_var.set(spec.uart_format)

    def toggle_lab_port(self) -> None:
        if self.secondary_worker and self.secondary_worker.running:
            self.secondary_worker.stop()
            self.lab_status_var.set("Отключение второго RX…")
            return
        if self.output_worker and self.output_worker.running:
            self.output_worker.stop()
            self.lab_status_var.set("Остановка TX…")
            return
        port = self.lab_port_var.get().split(" — ", 1)[0].strip()
        if not port:
            messagebox.showwarning("Нет порта", "Выберите второй COM-порт.")
            return
        primary_port = self.port_var.get().split(" — ", 1)[0].strip()
        if self.worker and self.worker.running and primary_port == port:
            messagebox.showwarning(
                "Порт занят", "Основной reader уже использует этот COM-порт."
            )
            return
        try:
            baudrate = int(self.lab_baud_var.get().strip())
            if baudrate <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Неверная скорость", "Введите скорость больше нуля.")
            return
        config = SerialConfig(
            port,
            baudrate,
            self.lab_protocol_var.get(),
            self.lab_format_var.get(),
        )
        self.lab_connect_button.configure(text="Отключить")
        mode = self.lab_mode_var.get()
        if mode == "Сравнение RX":
            self.secondary_worker = SerialWorker(
                config, self.events, event_prefix="secondary_"
            )
            self.secondary_worker.start()
            self.lab_status_var.set(
                f"RX 2: {port} @ {baudrate} {config.uart_format} • {config.protocol}"
            )
        else:
            if config.protocol not in OUTPUT_PROTOCOLS:
                messagebox.showwarning(
                    "Нет кодировщика",
                    f"Передача {config.protocol} пока не поддерживается.",
                )
                self.lab_connect_button.configure(text="Подключить")
                return
            self.output_worker = SerialOutputWorker(config, self.events)
            self.output_worker.start()
            self.lab_status_var.set(
                f"TX: {port} @ {baudrate} {config.uart_format} • {config.protocol}"
            )

    def _generator_tick(self) -> None:
        try:
            if (
                self.output_worker
                and self.output_worker.running
                and self.lab_mode_var.get() == "Генератор TX"
            ):
                rate = max(1.0, float(self.generator_rate_var.get()))
                now = monotonic()
                if now - self.last_generator_frame_at >= 1.0 / rate:
                    values = tuple(
                        round(variable.get())
                        for variable in self.generator_channel_vars
                    )
                    self.output_worker.send(
                        encode_channels(self.lab_protocol_var.get(), values)
                    )
                    self.last_generator_frame_at = now
        except (ValueError, tk.TclError):
            pass
        self.after(5, self._generator_tick)

    def _toggle_graph_pause(self) -> None:
        self.graph_paused_at = monotonic() if self.graph_paused_var.get() else None

    def _add_graph_marker(self) -> None:
        self.graph_markers.append(self.graph_paused_at or monotonic())

    def _refresh_signal_lab(self) -> None:
        try:
            self.analyzer.dropout_threshold_ms = max(
                1.0, float(self.dropout_threshold_var.get())
            )
            self.analyzer.jitter_alert_ms = max(
                0.01, float(self.jitter_threshold_var.get())
            )
            self.analyzer.lq_alert_percent = max(
                0.0, min(100.0, float(self.lq_threshold_var.get()))
            )
        except ValueError:
            pass
        snapshot = self.analyzer.snapshot()
        self.signal_summary_var.set(
            f"Frames: {snapshot['frames']}   Rate: {snapshot['rate_hz']:.1f} Hz   "
            f"Jitter: {snapshot['jitter_ms']:.3f} ms   Dropouts: {snapshot['dropouts']}   "
            f"Longest gap: {snapshot['longest_gap_ms']:.1f} ms   "
            f"Lost/Failsafe: {snapshot['frame_lost']}/{snapshot['failsafe']}"
        )
        for item in self.metrics_tree.get_children():
            self.metrics_tree.delete(item)
        for index in range(MAX_DISPLAY_CHANNELS):
            metric = self.analyzer.channel_metrics(index)
            if not metric.samples:
                continue
            self.metrics_tree.insert(
                "",
                "end",
                values=(
                    f"CH {metric.channel:02}",
                    metric.current_us,
                    metric.minimum_us,
                    metric.maximum_us,
                    f"{metric.jitter_us:.2f}",
                    f"{metric.noise_us:.2f}",
                    metric.kind,
                ),
            )
        self._draw_sticks()
        self._draw_timing_histogram()
        self.after(250, self._refresh_signal_lab)

    def _draw_sticks(self) -> None:
        canvas = self.stick_canvas
        canvas.delete("all")
        width = max(360, canvas.winfo_width())
        height = max(180, canvas.winfo_height())
        role_defaults = {"Roll": 0, "Pitch": 1, "Throttle": 2, "Yaw": 3}
        role_channels = {
            role: self.calibration.mapping.get(role, default)
            for role, default in role_defaults.items()
        }
        boxes = (
            (width * 0.08, 25, width * 0.43, height - 25, "YAW / THROTTLE", "Yaw", "Throttle"),
            (width * 0.57, 25, width * 0.92, height - 25, "ROLL / PITCH", "Roll", "Pitch"),
        )
        for left, top, right, bottom, title, x_role, y_role in boxes:
            canvas.create_rectangle(left, top, right, bottom, outline=COLORS["border"], width=2)
            canvas.create_line((left + right) / 2, top, (left + right) / 2, bottom, fill=COLORS["border"])
            canvas.create_line(left, (top + bottom) / 2, right, (top + bottom) / 2, fill=COLORS["border"])
            x_us = self.analyzer.channel_current[role_channels[x_role]]
            y_us = self.analyzer.channel_current[role_channels[y_role]]
            x = left + max(0, min(1, (x_us - 988) / 1024)) * (right - left)
            y = bottom - max(0, min(1, (y_us - 988) / 1024)) * (bottom - top)
            canvas.create_oval(x - 7, y - 7, x + 7, y + 7, fill=COLORS["accent_hover"], outline="")
            canvas.create_text((left + right) / 2, 12, text=title, fill=COLORS["muted"], font=("Segoe UI", 9, "bold"))

    def _draw_timing_histogram(self) -> None:
        canvas = self.timing_canvas
        canvas.delete("all")
        width = max(360, canvas.winfo_width())
        height = max(90, canvas.winfo_height())
        values = list(self.analyzer.intervals_ms)
        canvas.create_text(
            8,
            8,
            text="FRAME INTERVAL HISTOGRAM",
            fill=COLORS["muted"],
            anchor="nw",
            font=("Segoe UI", 8, "bold"),
        )
        if not values:
            return
        upper = max(5.0, min(100.0, max(values)))
        bucket_count = 20
        buckets = [0] * bucket_count
        for value in values:
            index = min(bucket_count - 1, int(value / upper * bucket_count))
            buckets[index] += 1
        maximum = max(buckets) or 1
        left, top, right, bottom = 8, 24, width - 8, height - 18
        bar_width = (right - left) / bucket_count
        for index, count in enumerate(buckets):
            x1 = left + index * bar_width
            x2 = x1 + max(1, bar_width - 2)
            y1 = bottom - count / maximum * (bottom - top)
            canvas.create_rectangle(
                x1, y1, x2, bottom, fill=COLORS["accent"], outline=""
            )
        canvas.create_text(
            left,
            bottom + 10,
            text="0 ms",
            fill=COLORS["muted"],
            anchor="w",
            font=("Segoe UI", 7),
        )
        canvas.create_text(
            right,
            bottom + 10,
            text=f"{upper:.1f} ms",
            fill=COLORS["muted"],
            anchor="e",
            font=("Segoe UI", 7),
        )

    def _refresh_comparison(self) -> None:
        result = self.comparator.compare()
        if result is None or self.comparator.primary is None or self.comparator.secondary is None:
            return
        time_delta, deltas = result
        primary = self.comparator.primary[1].microseconds
        secondary = self.comparator.secondary[1].microseconds
        max_delta = max((abs(value) for value in deltas), default=0)
        self.compare_summary_var.set(
            f"Arrival delta: {time_delta:+.3f} ms   "
            f"Max channel difference: {max_delta} µs   Channels: {len(deltas)}"
        )
        for item in self.compare_tree.get_children():
            self.compare_tree.delete(item)
        for index, delta in enumerate(deltas[:16]):
            self.compare_tree.insert(
                "",
                "end",
                values=(f"CH {index + 1:02}", primary[index], secondary[index], f"{delta:+d}"),
            )

    def _on_protocol_changed(self, *_args: object) -> None:
        protocol = self.protocol_var.get()
        previous_default = PROTOCOL_DEFAULTS.get(self.last_protocol)
        if not self.baud_var.get().strip() or self.baud_var.get() == previous_default:
            self.baud_var.set(PROTOCOL_DEFAULTS[protocol])
        previous_format = SERIAL_FORMATS.get(self.last_protocol)
        if not self.format_var.get().strip() or self.format_var.get() == previous_format:
            self.format_var.set(SERIAL_FORMATS[protocol])
        self.wiring_var.set(PROTOCOL_BY_NAME[protocol].wiring_note)
        self.last_protocol = protocol

    def start_scan(self) -> None:
        if self.scan_worker and self.scan_worker.running:
            self.scan_worker.stop()
            self.connection_var.set("Автоскан остановлен")
            return
        if self.worker and self.worker.running:
            messagebox.showinfo("Порт занят", "Сначала отключитесь от COM-порта.")
            return
        port = self.port_var.get().split(" — ", 1)[0].strip()
        if not port:
            messagebox.showwarning("Нет порта", "Выберите COM-порт для сканирования.")
            return
        try:
            preferred_baudrate = int(self.baud_var.get().strip())
            if preferred_baudrate <= 0:
                preferred_baudrate = None
        except ValueError:
            preferred_baudrate = None
        self.scan_worker = ScanWorker(
            port,
            self.events,
            preferred_baudrate=preferred_baudrate,
            preferred_uart_format=self.format_var.get(),
        )
        self._set_controls_enabled(False)
        self.connect_button.configure(state="disabled")
        self.scan_button.configure(text="Остановить скан")
        self.scan_worker.start()

    def toggle_recording(self) -> None:
        if self.recorder.active:
            frames = self.recorder.frame_count
            path = self.recorder.path
            self.recorder.stop()
            self.record_var.set("● REC")
            self.connection_var.set(f"Запись сохранена: {frames} кадров • {path}")
            return
        path = filedialog.asksaveasfilename(
            title="Записать RC-сессию",
            defaultextension=".rclog",
            filetypes=(("RC Reader log", "*.rclog"), ("Все файлы", "*.*")),
        )
        if not path:
            return
        try:
            self.recorder.start(path)
            self.record_var.set("■ STOP REC")
            self.connection_var.set(f"Запись в {path}")
        except OSError as exc:
            messagebox.showerror("Ошибка записи", str(exc))

    def start_replay(self) -> None:
        if self.replay_worker and self.replay_worker.running:
            self.replay_worker.stop()
            return
        if self.worker and self.worker.running:
            messagebox.showinfo("Порт подключён", "Отключите COM-порт перед Replay.")
            return
        path = filedialog.askopenfilename(
            title="Открыть RC-сессию",
            filetypes=(("RC Reader log", "*.rclog"), ("Все файлы", "*.*")),
        )
        if not path:
            return
        self.replay_worker = ReplayWorker(path, self.events)
        self.replay_button.configure(text="■ Stop Replay")
        self.connection_var.set(f"Replay: {path}")
        self.replay_worker.start()

    def export_session_csv(self) -> None:
        if not self.session_frames:
            messagebox.showinfo("Нет данных", "Сначала примите или воспроизведите кадры.")
            return
        path = filedialog.asksaveasfilename(
            title="Экспорт каналов в CSV",
            defaultextension=".csv",
            filetypes=(("CSV", "*.csv"), ("Все файлы", "*.*")),
        )
        if not path:
            return
        try:
            export_csv(path, list(self.session_frames), MAX_DISPLAY_CHANNELS)
            self.connection_var.set(f"CSV сохранён: {path}")
        except OSError as exc:
            messagebox.showerror("Ошибка CSV", str(exc))

    def clear_session(self) -> None:
        self.frame_times.clear()
        self.frame_intervals_ms.clear()
        self.last_frame_at = None
        self.session_frames.clear()
        self.session_started_at = monotonic()
        self.analyzer.reset()
        self.comparator = DualComparator(MAX_DISPLAY_CHANNELS)
        for history in self.channel_history:
            history.clear()
        for item in self.raw_tree.get_children():
            self.raw_tree.delete(item)
        self.raw_frame_by_id.clear()
        for tree in (self.metrics_tree, self.alert_tree, self.inspector_tree, self.compare_tree):
            for item in tree.get_children():
                tree.delete(item)
        self.hex_var.set("—")
        self.metadata_var.set("Телеметрия и метаданные появятся после приёма")
        self.graph_stats_var.set("Ожидание данных")
        for index in range(MAX_DISPLAY_CHANNELS):
            self.channel_bars[index]["value"] = 0
            self.raw_vars[index].set("—")
            self.us_vars[index].set("—")
            self.percent_vars[index].set("—")
        self.stats_var.set("Кадры: 0   FPS: 0.0   Ошибки: 0   Байт: 0")

    def toggle_connection(self) -> None:
        if self.worker and self.worker.running:
            self.connection_var.set("Отключение…")
            self.worker.stop()
            return

        port = self.port_var.get().split(" — ", 1)[0].strip()
        if not port:
            messagebox.showwarning("Нет порта", "Выберите COM-порт.")
            return

        try:
            baudrate = int(self.baud_var.get().strip())
            if baudrate <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Неверная скорость", "Введите целую скорость больше нуля.")
            return

        config = SerialConfig(
            port=port,
            baudrate=baudrate,
            protocol=self.protocol_var.get(),
            uart_format=self.format_var.get(),
        )
        self.frame_times.clear()
        self.worker = SerialWorker(config, self.events)
        self._set_controls_enabled(False)
        self.connect_button.configure(text="Отключить")
        self.connection_var.set(
            f"Подключение к {port} @ {baudrate}, {config.protocol}, {config.uart_format}…"
        )
        self.worker.start()

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.port_combo.configure(state=state)
        self.baud_combo.configure(state=state)
        self.protocol_combo.configure(state="readonly" if enabled else "disabled")
        self.format_combo.configure(state="readonly" if enabled else "disabled")

    def _poll_events(self) -> None:
        latest_frame: ChannelFrame | None = None
        latest_stats: ParserStats | None = None

        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "frame":
                    latest_frame = payload  # type: ignore[assignment]
                    self._ingest_frame(latest_frame, "primary")
                elif kind == "replay_frame":
                    latest_frame = payload  # type: ignore[assignment]
                    self._ingest_frame(latest_frame, "primary")
                elif kind == "packet":
                    self._add_protocol_packet(payload, "RX1")  # type: ignore[arg-type]
                elif kind == "secondary_frame":
                    self._ingest_frame(payload, "secondary")  # type: ignore[arg-type]
                elif kind == "secondary_packet":
                    self._add_protocol_packet(payload, "RX2")  # type: ignore[arg-type]
                elif kind == "secondary_connected":
                    self.lab_status_var.set("Второй RX подключён — ожидание кадров")
                elif kind == "secondary_stats":
                    stats = payload  # type: ignore[assignment]
                    self.lab_status_var.set(
                        f"RX2 frames: {stats.channel_frames} • "
                        f"errors: {stats.invalid_frames} • bytes: {stats.bytes_received}"
                    )
                elif kind == "secondary_error":
                    self.lab_status_var.set(f"Ошибка RX2: {payload}")
                    messagebox.showerror("Ошибка второго COM-порта", str(payload))
                elif kind == "secondary_disconnected":
                    self.secondary_worker = None
                    self.lab_connect_button.configure(text="Подключить")
                    if not self.lab_status_var.get().startswith("Ошибка"):
                        self.lab_status_var.set("Второй RX отключён")
                elif kind == "output_connected":
                    self.lab_status_var.set("TX-порт подключён")
                elif kind == "output_error":
                    self.lab_status_var.set(f"Ошибка TX: {payload}")
                    messagebox.showerror("Ошибка TX-порта", str(payload))
                elif kind == "output_disconnected":
                    self.output_worker = None
                    self.lab_connect_button.configure(text="Подключить")
                    if not self.lab_status_var.get().startswith("Ошибка"):
                        self.lab_status_var.set("TX-порт отключён")
                elif kind == "stats":
                    latest_stats = payload  # type: ignore[assignment]
                elif kind == "connected":
                    self.connection_var.set("Подключено — ожидание кадров")
                elif kind == "error":
                    self.connection_var.set(f"Ошибка: {payload}")
                    messagebox.showerror("Ошибка COM-порта", str(payload))
                elif kind == "disconnected":
                    self._disconnected()
                elif kind == "scan_progress":
                    baudrate, uart_format, index, total = payload  # type: ignore[misc]
                    self.connection_var.set(
                        f"Автоскан {index}/{total}: все протоколы • "
                        f"{baudrate} бод • {uart_format}"
                    )
                elif kind == "scan_found":
                    if isinstance(payload, tuple):
                        spec, confidence = payload
                    else:
                        spec, confidence = payload, 99.0
                    self._scan_finished()
                    self.protocol_var.set(spec.name)
                    self.baud_var.set(str(spec.baudrate))
                    self.format_var.set(spec.uart_format)
                    self.connection_var.set(
                        f"Найден {spec.name} @ {spec.baudrate} {spec.uart_format} • "
                        f"уверенность {confidence:.0f}%"
                    )
                    self.after(100, self.toggle_connection)
                elif kind == "scan_done":
                    self._scan_finished()
                    observations = payload if isinstance(payload, tuple) else ()
                    if observations:
                        variants = ", ".join(
                            f"{spec.name} {spec.baudrate}/{spec.uart_format} ({score:.0f}%)"
                            for score, spec in observations
                        )
                        self.connection_var.set(
                            f"Нет уверенного совпадения. Возможные варианты: {variants}"
                        )
                    else:
                        self.connection_var.set(
                            "Автоскан: протокол не найден. Проверьте GND, RX и инверсию."
                        )
                elif kind == "scan_error":
                    self._scan_finished()
                    self.connection_var.set(f"Ошибка автоскана: {payload}")
                elif kind == "replay_done":
                    self.replay_button.configure(text="▶ Replay")
                    self.replay_worker = None
                    self.connection_var.set("Replay завершён")
                elif kind == "replay_error":
                    self.replay_button.configure(text="▶ Replay")
                    self.replay_worker = None
                    messagebox.showerror("Ошибка Replay", str(payload))
        except Empty:
            pass

        if latest_frame is not None:
            self._show_frame(latest_frame)
        if latest_stats is not None:
            self._show_stats(latest_stats)

        self.after(30, self._poll_events)

    def _ingest_frame(self, frame: ChannelFrame, source: str = "primary") -> None:
        now = monotonic()
        self.comparator.ingest(source, frame, now)
        if source == "secondary":
            elapsed = now - self.session_started_at
            self._add_raw_frame(elapsed, frame, "RX2")
            self._refresh_comparison()
            return

        if self.last_frame_at is not None:
            interval_ms = (now - self.last_frame_at) * 1000
            if interval_ms < 10_000:
                self.frame_intervals_ms.append(interval_ms)
        self.last_frame_at = now
        self.frame_times.append(now)
        elapsed = now - self.session_started_at
        self.session_frames.append((elapsed, frame))
        self.recorder.record(frame)
        self.calibration.ingest(frame)
        alerts = self.analyzer.ingest(frame, now)
        for alert in alerts:
            self.alert_tree.insert(
                "",
                0,
                values=(
                    f"{alert.timestamp - self.analyzer.started_at:.3f}",
                    alert.level,
                    alert.message,
                ),
            )
            if (
                self.alarm_enabled_var.get()
                and now - self.last_alert_sound_at >= 0.5
            ):
                self.bell()
                self.last_alert_sound_at = now

        for index, microseconds in enumerate(frame.microseconds[:MAX_DISPLAY_CHANNELS]):
            self.channel_history[index].append((now, microseconds))
        self._add_raw_frame(elapsed, frame, "RX1")
        self._refresh_comparison()

        if (
            self.output_worker
            and self.output_worker.running
            and self.lab_mode_var.get() == "Конвертер TX"
        ):
            try:
                self.output_worker.send(
                    encode_channels(self.lab_protocol_var.get(), frame.microseconds)
                )
            except ValueError:
                pass

    def _add_raw_frame(
        self, elapsed: float, frame: ChannelFrame, source: str = "RX1"
    ) -> None:
        flags = []
        if frame.failsafe:
            flags.append("FAILSAFE")
        if frame.frame_lost:
            flags.append("FRAME LOST")
        if not flags:
            flags.append("OK")
        item = self.raw_tree.insert(
            "",
            0,
            values=(
                f"{elapsed:9.3f}",
                f"{source} {frame.protocol}",
                frame.frame_name or "RC channels",
                len(frame.raw_frame),
                " • ".join(flags),
            ),
        )
        self.raw_frame_by_id[item] = frame
        children = self.raw_tree.get_children()
        if len(children) > 400:
            for old_item in children[400:]:
                self.raw_frame_by_id.pop(old_item, None)
                self.raw_tree.delete(old_item)

    def _add_protocol_packet(
        self, packet: ProtocolPacket, source: str = "RX1"
    ) -> None:
        elapsed = monotonic() - self.session_started_at
        item = self.raw_tree.insert(
            "",
            0,
            values=(
                f"{elapsed:9.3f}",
                f"{source} {packet.protocol}",
                packet.frame_name,
                len(packet.raw_frame),
                "TELEMETRY",
            ),
        )
        self.raw_frame_by_id[item] = packet
        children = self.raw_tree.get_children()
        if len(children) > 400:
            for old_item in children[400:]:
                self.raw_frame_by_id.pop(old_item, None)
                self.raw_tree.delete(old_item)

    def _on_raw_selected(self, _event: object = None) -> None:
        selected = self.raw_tree.selection()
        if not selected:
            return
        packet = self.raw_frame_by_id.get(selected[0])
        if packet is not None:
            self.hex_var.set(packet.raw_frame.hex(" ").upper())
            for item in self.inspector_tree.get_children():
                self.inspector_tree.delete(item)
            for field in inspect_packet(packet):
                self.inspector_tree.insert(
                    "",
                    "end",
                    values=(
                        field.offset,
                        field.length,
                        field.name,
                        field.value,
                        field.raw_hex,
                    ),
                )

    def _scan_finished(self) -> None:
        self.scan_worker = None
        self._set_controls_enabled(True)
        self.connect_button.configure(state="normal")
        self.scan_button.configure(text="⚡ Автоскан")

    def _show_frame(self, frame: ChannelFrame) -> None:
        now = monotonic()
        while self.frame_times and now - self.frame_times[0] > 1.0:
            self.frame_times.popleft()

        for index, (raw, microseconds) in enumerate(zip(frame.channels, frame.microseconds)):
            if index >= MAX_DISPLAY_CHANNELS:
                break
            if frame.percent_override is not None and index < len(frame.percent_override):
                percent = frame.percent_override[index]
            else:
                denominator = max(1, frame.channel_max - frame.channel_min)
                percent = max(
                    0.0,
                    min(
                        100.0,
                        (raw - frame.channel_min) * 100 / denominator,
                    ),
                )
            self.channel_bars[index]["value"] = percent
            self.raw_vars[index].set(str(raw))
            self.us_vars[index].set(str(microseconds))
            self.percent_vars[index].set(f"{percent:5.1f}")

        for index in range(min(len(frame.channels), MAX_DISPLAY_CHANNELS), MAX_DISPLAY_CHANNELS):
            self.channel_bars[index]["value"] = 0
            self.raw_vars[index].set("—")
            self.us_vars[index].set("—")
            self.percent_vars[index].set("—")

        yes_no = lambda value: "ДА" if value else "нет"
        if frame.protocol in ("SBUS", "F.Port"):
            self.flags_var.set(
                f"{frame.frame_name or frame.protocol}   "
                f"Failsafe: {yes_no(frame.failsafe)}   "
                f"Frame lost: {yes_no(frame.frame_lost)}   "
                f"CH17: {int(frame.digital_channel_17)}   "
                f"CH18: {int(frame.digital_channel_18)}"
            )
        elif frame.protocol == "SUMD":
            self.flags_var.set(
                f"SUMD • Каналов: {len(frame.channels)}   "
                f"Failsafe: {yes_no(frame.failsafe)}   CRC16: OK"
            )
        elif frame.protocol == "CRSF":
            self.flags_var.set(f"{frame.frame_name or 'CRSF'} • CRC8: OK")
        else:
            self.flags_var.set(
                f"{frame.frame_name or frame.protocol} • "
                f"Каналов: {len(frame.channels)} • Кадр валиден"
            )
        if frame.metadata:
            self.metadata_var.set(
                "   •   ".join(f"{key}: {value}" for key, value in frame.metadata)
            )
        else:
            self.metadata_var.set(f"{frame.protocol}: дополнительных данных пока нет")
        self.hex_var.set(frame.raw_frame.hex(" ").upper())
        self.connection_var.set(f"Приём {frame.protocol}: данные идут")

    def _show_stats(self, stats: ParserStats) -> None:
        now = monotonic()
        while self.frame_times and now - self.frame_times[0] > 1.0:
            self.frame_times.popleft()
        self.stats_var.set(
            f"RC-кадры: {stats.channel_frames}   FPS: {len(self.frame_times):.1f}   "
            f"Валидные: {stats.valid_frames}   Ошибки: {stats.invalid_frames}   "
            f"Байт: {stats.bytes_received}"
        )

    def _draw_graph(self) -> None:
        try:
            channel_index = max(
                0,
                min(
                    MAX_DISPLAY_CHANNELS - 1,
                    int(self.graph_channel_var.get().split()[-1]) - 1,
                ),
            )
            window_seconds = float(self.graph_window_var.get())
            now = self.graph_paused_at or monotonic()
            points = [
                (timestamp, value)
                for timestamp, value in self.channel_history[channel_index]
                if now - timestamp <= window_seconds
            ]
            series_indices = [channel_index]
            if self.graph_overlay_var.get():
                series_indices.extend(
                    index for index in range(4) if index != channel_index
                )
            series_points = {
                index: [
                    (timestamp, value)
                    for timestamp, value in self.channel_history[index]
                    if now - timestamp <= window_seconds
                ]
                for index in series_indices
            }

            canvas = self.graph_canvas
            canvas.delete("all")
            width = max(200, canvas.winfo_width())
            height = max(160, canvas.winfo_height())
            left, top, right, bottom = 58, 18, width - 18, height - 34

            if not points:
                canvas.create_text(
                    width / 2,
                    height / 2,
                    text="Нет данных для выбранного канала",
                    fill=COLORS["muted"],
                    font=("Segoe UI", 12),
                )
                self.graph_stats_var.set("Ожидание данных")
                self.after(120, self._draw_graph)
                return

            values = [value for _timestamp, value in points]
            all_values = [
                value
                for samples in series_points.values()
                for _timestamp, value in samples
            ]
            value_min = min(values)
            value_max = max(values)
            y_min = min(900, min(all_values) - 50)
            y_max = max(2100, max(all_values) + 50)
            if y_max <= y_min:
                y_max = y_min + 1

            for level in (1000, 1250, 1500, 1750, 2000):
                y = bottom - (level - y_min) * (bottom - top) / (y_max - y_min)
                if top <= y <= bottom:
                    canvas.create_line(
                        left, y, right, y, fill=COLORS["border"], dash=(3, 5)
                    )
                    canvas.create_text(
                        left - 8,
                        y,
                        text=str(level),
                        fill=COLORS["muted"],
                        anchor="e",
                        font=("Segoe UI", 8),
                    )
            for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
                x = left + fraction * (right - left)
                canvas.create_line(x, top, x, bottom, fill=COLORS["border"], dash=(2, 7))
                canvas.create_text(
                    x,
                    bottom + 16,
                    text=f"-{window_seconds * (1 - fraction):.1f}s",
                    fill=COLORS["muted"],
                    font=("Segoe UI", 8),
                )
            for marker_time in self.graph_markers:
                age = now - marker_time
                if 0 <= age <= window_seconds:
                    x = right - age * (right - left) / window_seconds
                    canvas.create_line(
                        x, top, x, bottom, fill="#F59E0B", width=2, dash=(5, 3)
                    )

            palette = ("#60A5FA", "#2DD4BF", "#F59E0B", "#F472B6")
            for palette_index, index in enumerate(series_indices):
                samples = series_points[index]
                if not samples:
                    continue
                coordinates: list[float] = []
                for timestamp, value in samples:
                    x = right - (now - timestamp) * (right - left) / window_seconds
                    y = bottom - (value - y_min) * (bottom - top) / (y_max - y_min)
                    coordinates.extend((x, y))
                color = palette[index % len(palette)]
                if len(coordinates) >= 4:
                    canvas.create_line(
                        *coordinates,
                        fill=color,
                        width=2 if index == channel_index else 1,
                        smooth=False,
                    )
                else:
                    canvas.create_oval(
                        coordinates[0] - 2,
                        coordinates[1] - 2,
                        coordinates[0] + 2,
                        coordinates[1] + 2,
                        fill=color,
                        outline="",
                    )
                if self.graph_overlay_var.get():
                    canvas.create_text(
                        right - 8,
                        top + 14 + palette_index * 16,
                        text=f"CH{index + 1}",
                        fill=color,
                        anchor="e",
                        font=("Segoe UI", 9, "bold"),
                    )

            mean_interval = (
                statistics.fmean(self.frame_intervals_ms)
                if self.frame_intervals_ms
                else 0.0
            )
            jitter = (
                statistics.pstdev(self.frame_intervals_ms)
                if len(self.frame_intervals_ms) > 1
                else 0.0
            )
            rate = 1000 / mean_interval if mean_interval > 0 else 0.0
            self.graph_stats_var.set(
                f"CH{channel_index + 1}: {values[-1]} µs   "
                f"min/max: {value_min}/{value_max}   "
                f"rate: {rate:.1f} Hz   interval jitter: {jitter:.3f} ms"
            )
        except (ValueError, tk.TclError):
            pass
        self.after(120, self._draw_graph)

    def _disconnected(self) -> None:
        self._set_controls_enabled(True)
        self.connect_button.configure(text="Подключить")
        if not self.connection_var.get().startswith("Ошибка"):
            self.connection_var.set("Не подключено")
        self.worker = None

    def _on_close(self) -> None:
        if self.worker:
            self.worker.stop()
        if self.secondary_worker:
            self.secondary_worker.stop()
        if self.output_worker:
            self.output_worker.stop()
        if self.scan_worker:
            self.scan_worker.stop()
        if self.replay_worker:
            self.replay_worker.stop()
        self.recorder.stop()
        self.destroy()


def run() -> None:
    app = RCReaderApp()
    app.mainloop()
