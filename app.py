import argparse
import json
import math
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from trenball.dpi import enable_dpi_awareness

# Must run before any Tk window is created (fixes region offset on scaled displays).
enable_dpi_awareness()

from trenball.capture import grab_region
from trenball.config import load_config, save_config
from trenball.region_selector import Region, select_region
from trenball.session_guard import (
    is_workstation_locked,
    looks_like_blank_capture,
    prevent_sleep,
)
from trenball.trend_decoder import (
    decode_trend_grid,
    describe_obstruction,
    frame_is_obscured,
)
from trenball.trend_tracker import (
    UNALIGNED_DETAIL,
    diff_grids,
    max_rounds_in,
    recover_new_beads,
    summarize_streaks,
)
from trenball.telegram_alerts import AlertRule, count_10x_in_recent_points, send_telegram_message
from trenball.selenium_collector import CrashRound, SeleniumCrashCollector

BEAD_TO_EVENT = {
    "G": (1, "2x+"),
    "O": (-1, "2x-"),
    "Y": (1, "10x"),
}


class PlusMinusGraphApp(tk.Tk):
    # 10,000 rounds is only a few days of continuous Crash tracking and made
    # the collector appear to stop while it was still receiving events.
    MAX_STEPS = 1_000_000
    MAX_BASE_NEW_BEADS_PER_POLL = 3
    # A round cannot finish faster than this, so it bounds how many beads may
    # legitimately appear over a given stretch of time.
    MIN_SECONDS_PER_ROUND = 4.0
    DETAIL_HEIGHT = 420
    OVERVIEW_HEIGHT = 130
    SIDE_PANEL_WIDTH = 320
    TEN_X_LIMITS = tuple(range(10, 101, 10))
    WINDOW_OPTIONS = [
        ("60", 60),
        ("120", 120),
        ("250", 250),
        ("500", 500),
        ("1000", 1000),
        ("All", None),
    ]
    LAYOUT_OPTIONS = [
        ("Wide", "wide"),
        ("Stacked", "stacked"),
        ("Split", "split"),
    ]
    SESSION_DATA_DIR = Path(__file__).resolve().parent / "data"

    def __init__(self, initial_session: str | None = None) -> None:
        super().__init__()
        self.history_view = initial_session is not None
        self.title("2X Graph - Analysis Dashboard")
        self.geometry("1460x920")
        self.minsize(1180, 760)
        self.configure(bg="#08111f")

        self.series = [0]
        self.point_timestamps = [self._now_iso()]
        self.point_event_types = ["start"]
        self.view_start = 0
        self.window_size = 250
        self.auto_follow = tk.BooleanVar(value=True)
        self.layout_mode = "wide"
        self.show_10x_marks = True
        self.current_file_path = None
        self.last_saved_at = None
        self.auto_save_active = False
        self.is_dirty = False
        self.detail_plot_state = None
        self.overview_drag_start_x = None
        self.overview_drag_current_x = None

        self.metric_values = {}
        self.insight_values = []
        self.layout_buttons = {}
        self.window_buttons = {}
        self.ten_x_count_labels = {}

        trenball_cfg = load_config()
        self.crash_url = str(trenball_cfg.get("crash_url", "https://bc.game/game/crash"))
        self.selenium_headless = bool(trenball_cfg.get("selenium_headless", False))
        self.crash_url_var = tk.StringVar(value=self.crash_url)
        self.selenium_headless_var = tk.BooleanVar(value=self.selenium_headless)
        self.collector_events = queue.Queue()
        self.collector_stop_event = None
        self.collector_thread = None
        self.collector_timer_id = None
        self.last_crash_round = None
        self.trenball_region = Region.from_dict(trenball_cfg.get("region"))
        self.trenball_poll_seconds = int(trenball_cfg.get("poll_seconds", 20))
        self.trenball_grid_rows = int(trenball_cfg.get("grid_rows", 5))
        self.trenball_running = False
        self.trenball_timer_id = None
        self.trenball_prev_decode = None
        self.trenball_pending_decode = None
        self.trenball_reject_candidate = None
        self.trenball_paused_for_lock = False
        self.trenball_obscured_since = None
        self.trenball_last_trusted_at = None
        self.trenball_interval_var = tk.StringVar(value=str(self.trenball_poll_seconds))
        legacy_alert = trenball_cfg.get("telegram_alert")
        credentials = trenball_cfg.get("telegram_credentials", {})
        if not isinstance(credentials, dict):
            credentials = {}
        legacy_data = legacy_alert if isinstance(legacy_alert, dict) else {}
        self.telegram_bot_token = str(credentials.get("bot_token", legacy_data.get("bot_token", ""))).strip()
        self.telegram_chat_id = str(credentials.get("chat_id", legacy_data.get("chat_id", ""))).strip()
        configured_rules = trenball_cfg.get("telegram_alerts")
        if isinstance(configured_rules, list):
            self.telegram_rules = [AlertRule.from_dict(item) for item in configured_rules]
        elif isinstance(legacy_alert, dict):
            self.telegram_rules = [AlertRule.from_dict(legacy_alert)]
        else:
            self.telegram_rules = []
        self.telegram_alert_inside_bands = [False] * len(self.telegram_rules)
        no_ten_x_config = trenball_cfg.get("telegram_no_10x_alert", {})
        if not isinstance(no_ten_x_config, dict):
            no_ten_x_config = {}
        self.telegram_no_10x_enabled = bool(no_ten_x_config.get("enabled", True))
        self.telegram_no_10x_points = max(1, int(no_ten_x_config.get("points", 50)))
        self.telegram_no_10x_alert_sent = False
        self.app_alert_timer_id = None

        self._build_layout()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_ui()
        self._refresh_trenball_status()
        if initial_session:
            try:
                self._load_session_file(initial_session)
                self.title(f"2X Graph - {Path(initial_session).name}")
                self.header_status.configure(text=f"Historical: {Path(initial_session).name}")
            except Exception as exc:
                error = str(exc)
                self.after(0, lambda detail=error: messagebox.showerror(
                    "Load failed", f"Could not load the session.\n\n{detail}", parent=self
                ))

    def _build_layout(self) -> None:
        self.root_frame = tk.Frame(self, bg="#08111f", padx=22, pady=22)
        self.root_frame.pack(fill="both", expand=True)
        self.root_frame.columnconfigure(0, weight=23)
        self.root_frame.rowconfigure(2, weight=1)

        self._build_header()
        self._build_app_alert()
        self._build_body()

    def _build_header(self) -> None:
        header = self._card(self.root_frame, bg="#0f1c31", padx=22, pady=20)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)

        title = tk.Label(
            header,
            text="2X Graph History" if self.history_view else "2X Graph",
            bg="#0f1c31",
            fg="#eef4ff",
            font=("Segoe UI", 28, "bold"),
        )
        title.grid(row=0, column=0, sticky="w")

        subtitle = tk.Label(
            header,
            text=(
                "Read-only trend view for a saved session."
                if self.history_view
                else "See the trend clearly, zoom the visible window, and keep the controls where you want them."
            ),
            bg="#0f1c31",
            fg="#9bb0cd",
            font=("Segoe UI", 11),
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(6, 0))

        self.header_status = tk.Label(
            header,
            text="Ready",
            bg="#173156",
            fg="#dce9ff",
            font=("Segoe UI", 10, "bold"),
            padx=14,
            pady=8,
        )
        self.header_status.grid(row=0, column=1, rowspan=2, sticky="e")

    def _build_app_alert(self) -> None:
        self.app_alert_frame = tk.Frame(
            self.root_frame,
            bg="#3a2b12",
            padx=18,
            pady=14,
            highlightthickness=1,
            highlightbackground="#d99a2b",
        )
        self.app_alert_frame.columnconfigure(1, weight=1)

        tk.Label(
            self.app_alert_frame,
            text="!",
            bg="#d99a2b",
            fg="#171006",
            width=2,
            font=("Segoe UI", 16, "bold"),
        ).grid(row=0, column=0, rowspan=2, sticky="ns", padx=(0, 14))

        self.app_alert_title = tk.Label(
            self.app_alert_frame,
            text="Alert triggered",
            bg="#3a2b12",
            fg="#ffd982",
            anchor="w",
            font=("Segoe UI", 12, "bold"),
        )
        self.app_alert_title.grid(row=0, column=1, sticky="ew")

        self.app_alert_message = tk.Label(
            self.app_alert_frame,
            text="",
            bg="#3a2b12",
            fg="#fff3d5",
            anchor="w",
            justify="left",
            font=("Segoe UI", 10),
        )
        self.app_alert_message.grid(row=1, column=1, sticky="ew", pady=(4, 0))

        tk.Button(
            self.app_alert_frame,
            text="Close",
            command=self._hide_app_alert,
            relief="flat",
            bd=0,
            bg="#5a431d",
            fg="#fff3d5",
            activebackground="#725727",
            activeforeground="#ffffff",
            font=("Segoe UI", 9, "bold"),
            padx=12,
            pady=6,
            cursor="hand2",
        ).grid(row=0, column=2, rowspan=2, sticky="e", padx=(16, 0))

    def _show_app_alert(self, title: str, message: str) -> None:
        if self.app_alert_timer_id is not None:
            self.after_cancel(self.app_alert_timer_id)
        self.app_alert_title.configure(text=title)
        self.app_alert_message.configure(text=message)
        self.app_alert_frame.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        self.app_alert_frame.tkraise()
        self.app_alert_timer_id = self.after(15_000, self._hide_app_alert)

    def _hide_app_alert(self) -> None:
        if self.app_alert_timer_id is not None:
            self.after_cancel(self.app_alert_timer_id)
            self.app_alert_timer_id = None
        self.app_alert_frame.grid_remove()

    def _build_body(self) -> None:
        body = tk.Frame(self.root_frame, bg="#08111f")
        body.grid(row=2, column=0, sticky="nsew", pady=(18, 0))
        # Fixed narrow settings column; chart column takes all remaining width.
        body.columnconfigure(0, weight=0, minsize=self.SIDE_PANEL_WIDTH)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_side_panel(body)
        self._build_chart_panel(body)
        if self.history_view:
            self.side_panel.grid_remove()
            body.columnconfigure(0, weight=1, minsize=0)
            body.columnconfigure(1, weight=0)
            self.chart_card.grid_configure(column=0)

    def _configure_side_notebook_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Side.TNotebook", background="#08111f", borderwidth=0)
        style.configure(
            "Side.TNotebook.Tab",
            background="#13233d",
            foreground="#9bb0cd",
            padding=[14, 7],
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Side.TNotebook.Tab",
            background=[("selected", "#5d86ff")],
            foreground=[("selected", "#eef4ff")],
        )

    def _build_side_panel(self, parent: tk.Widget) -> None:
        # Fixed-width sidebar; height follows the body so Trend View stays dominant.
        side_panel = tk.Frame(parent, bg="#08111f", width=self.SIDE_PANEL_WIDTH)
        self.side_panel = side_panel
        side_panel.grid(row=0, column=0, sticky="ns", padx=(0, 14))
        side_panel.grid_propagate(False)
        side_panel.columnconfigure(0, weight=1)
        side_panel.rowconfigure(0, weight=1)

        def _sync_side_height(event: tk.Event, panel: tk.Frame = side_panel) -> None:
            if event.widget is parent and event.height > 1:
                panel.configure(height=event.height)

        parent.bind("<Configure>", _sync_side_height, add="+")

        self._configure_side_notebook_style()
        self.side_notebook = ttk.Notebook(side_panel, style="Side.TNotebook")
        self.side_notebook.grid(row=0, column=0, sticky="nsew")

        main_tab = tk.Frame(self.side_notebook, bg="#08111f")
        track_tab = tk.Frame(self.side_notebook, bg="#08111f")
        main_tab.columnconfigure(0, weight=1)
        track_tab.columnconfigure(0, weight=1)

        self.side_notebook.add(main_tab, text="Main")
        self.side_notebook.add(track_tab, text="Auto Track")

        self._build_main_side_stack(main_tab)
        self._build_track_tab(track_tab)

    def _build_main_side_stack(self, parent: tk.Widget) -> None:
        """Original Controls + Stats + 10X + Insights stack (pre-auto-track layout)."""
        self.actions_card = self._card(parent, bg="#0f1c31", padx=18, pady=18)
        self.actions_card.grid(row=0, column=0, sticky="ew")
        self.actions_card.columnconfigure(0, weight=1)
        self.actions_card.rowconfigure(4, weight=1)

        self._section_title(
            self.actions_card,
            "Controls",
            "Record 2X+, 2X-, 10X, undo, clear, save or load sessions, and change the button layout.",
        ).grid(row=0, column=0, sticky="ew")

        layout_row = tk.Frame(self.actions_card, bg="#0f1c31")
        layout_row.grid(row=1, column=0, sticky="ew", pady=(16, 14))
        layout_row.columnconfigure(0, weight=1)
        layout_row.columnconfigure(1, weight=1)
        layout_row.columnconfigure(2, weight=1)

        for index, (label, mode) in enumerate(self.LAYOUT_OPTIONS):
            button = self._mode_button(layout_row, label, lambda value=mode: self._set_layout_mode(value))
            button.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 8, 0))
            self.layout_buttons[mode] = button

        session_row = tk.Frame(self.actions_card, bg="#0f1c31")
        session_row.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        session_row.columnconfigure(0, weight=1)
        session_row.columnconfigure(1, weight=1)
        session_row.columnconfigure(2, weight=1)
        session_row.columnconfigure(3, weight=1)

        self.save_session_button = self._utility_button(session_row, "Save Session", "#173156", self._save_session_dialog)
        self.save_session_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.load_session_button = self._utility_button(session_row, "Load Session", "#1f3b63", self._load_session_dialog)
        self.load_session_button.grid(row=0, column=1, sticky="ew", padx=(0, 8))

        self.history_button = self._utility_button(session_row, "History", "#3a2f5a", self._open_history_browser)
        self.history_button.grid(row=0, column=2, sticky="ew", padx=(0, 8))

        self.clear_session_button = self._utility_button(session_row, "Clear", "#5a2430", self._clear_session)
        self.clear_session_button.grid(row=0, column=3, sticky="ew")

        self.file_status_label = tk.Label(
            self.actions_card,
            text="",
            bg="#13233d",
            fg="#cfe0fb",
            anchor="w",
            justify="left",
            wraplength=max(160, self.SIDE_PANEL_WIDTH - 56),
            padx=14,
            pady=12,
            font=("Segoe UI", 9),
            highlightthickness=1,
            highlightbackground="#1b2c48",
        )
        self.file_status_label.grid(row=3, column=0, sticky="ew", pady=(0, 14))

        self.action_grid = tk.Frame(self.actions_card, bg="#0f1c31", height=300)
        self.action_grid.grid(row=4, column=0, sticky="nsew")
        self.action_grid.grid_propagate(False)

        self.plus_button = self._action_button(
            self.action_grid,
            "2X +",
            "Bigger 2X",
            "#25b56a",
            "#0d2419",
            lambda: self._add_step(1, "2x+"),
        )
        self.minus_button = self._action_button(
            self.action_grid,
            "2X -",
            "Less 2X",
            "#f45b69",
            "#2a1115",
            lambda: self._add_step(-1, "2x-"),
        )
        self.ten_x_button = self._action_button(
            self.action_grid,
            "10X",
            "Marked 2X+",
            "#d8a316",
            "#3e3210",
            lambda: self._add_step(1, "10x"),
        )
        self.undo_button = self._action_button(
            self.action_grid,
            "Undo",
            "Remove last move",
            "#5d86ff",
            "#101a34",
            self._undo,
        )
        self._render_action_buttons()

        self.stats_card = self._card(parent, bg="#0f1c31", padx=18, pady=18)
        self.stats_card.grid(row=1, column=0, sticky="ew", pady=(18, 0))
        self.stats_card.columnconfigure(0, weight=1)
        self.stats_card.columnconfigure(1, weight=1)

        self._section_title(self.stats_card, "Quick Stats", "Read the series without guessing from the line.").grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="ew",
        )

        metric_specs = [
            ("Steps", "steps"),
            ("Current", "current"),
            ("Highest", "highest"),
            ("Lowest", "lowest"),
            ("2X+ Count", "up_count"),
            ("2X- Count", "down_count"),
        ]
        for index, (label, key) in enumerate(metric_specs):
            row = index // 2 + 1
            column = index % 2
            card, value_label = self._metric_card(self.stats_card, label)
            card.grid(row=row, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0), pady=(14, 0))
            self.metric_values[key] = value_label

        self.ten_x_card = self._card(parent, bg="#0f1c31", padx=18, pady=18)
        self.ten_x_card.grid(row=2, column=0, sticky="ew", pady=(18, 0))
        self.ten_x_card.columnconfigure(0, weight=1)
        self.ten_x_card.columnconfigure(1, weight=1)

        self._section_title(
            self.ten_x_card,
            "10X Counts",
            "Recent 10X events over the latest 10 to 100 recorded moves.",
        ).grid(row=0, column=0, columnspan=2, sticky="ew")

        for index, limit in enumerate(self.TEN_X_LIMITS):
            label = tk.Label(
                self.ten_x_card,
                text="",
                bg="#13233d",
                fg="#eef4ff",
                anchor="w",
                justify="left",
                padx=14,
                pady=10,
                font=("Segoe UI", 10, "bold"),
                highlightthickness=1,
                highlightbackground="#1b2c48",
            )
            row = index // 2 + 1
            column = index % 2
            label.grid(
                row=row,
                column=column,
                sticky="ew",
                padx=(0 if column == 0 else 10, 0),
                pady=(14 if row == 1 else 10, 0),
            )
            self.ten_x_count_labels[limit] = label

        self.insights_card = self._card(parent, bg="#0f1c31", padx=18, pady=18)
        self.insights_card.grid(row=3, column=0, sticky="ew", pady=(18, 0))
        self.insights_card.columnconfigure(0, weight=1)

        self._section_title(self.insights_card, "Insights", "Surface the important story in the data.").grid(
            row=0,
            column=0,
            sticky="ew",
        )

        for index in range(3):
            label = tk.Label(
                self.insights_card,
                text="",
                bg="#13233d",
                fg="#d7e4fb",
                anchor="w",
                justify="left",
                wraplength=max(160, self.SIDE_PANEL_WIDTH - 56),
                padx=14,
                pady=12,
                font=("Segoe UI", 10),
            )
            label.grid(row=index + 1, column=0, sticky="ew", pady=(14 if index == 0 else 10, 0))
            self.insight_values.append(label)

    def _build_track_tab(self, parent: tk.Widget) -> None:
        self.track_card = self._card(parent, bg="#0f1c31", padx=18, pady=18)
        self.track_card.grid(row=0, column=0, sticky="ew")
        self.track_card.columnconfigure(0, weight=1)

        self._section_title(
            self.track_card,
            "WebSocket Auto Track",
            "Chrome receives Crash results through WebSocket. Keep the browser open and log in if requested.",
        ).grid(row=0, column=0, sticky="ew")

        tk.Label(self.track_card, text="Crash URL", bg="#0f1c31", fg="#9bb0cd", font=("Segoe UI", 10)).grid(row=1, column=0, sticky="w", pady=(16, 5))
        self.crash_url_entry = tk.Entry(
            self.track_card, textvariable=self.crash_url_var, bg="#13233d", fg="#eef4ff",
            insertbackground="#eef4ff", relief="flat", font=("Segoe UI", 9)
        )
        self.crash_url_entry.grid(row=2, column=0, sticky="ew", ipady=7)
        self.headless_check = tk.Checkbutton(
            self.track_card, text="Run Chrome headless", variable=self.selenium_headless_var,
            bg="#0f1c31", fg="#cfe0fb", selectcolor="#13233d", activebackground="#0f1c31",
            activeforeground="#eef4ff", font=("Segoe UI", 10)
        )
        self.headless_check.grid(row=3, column=0, sticky="w", pady=(10, 0))

        track_row2 = tk.Frame(self.track_card, bg="#0f1c31")
        track_row2.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        track_row2.columnconfigure(0, weight=1)
        track_row2.columnconfigure(1, weight=1)

        self.start_track_button = self._utility_button(
            track_row2, "Start Track", "#1f5a3a", self._start_trenball_tracking
        )
        self.start_track_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.stop_track_button = self._utility_button(
            track_row2, "Stop", "#5a2430", self._stop_trenball_tracking
        )
        self.stop_track_button.grid(row=0, column=1, sticky="ew")
        self.stop_track_button.configure(state="disabled")

        self.telegram_alert_button = self._utility_button(
            self.track_card, "Telegram Alerts...", "#24506b", self._open_telegram_alert_dialog
        )
        self.telegram_alert_button.grid(row=5, column=0, sticky="ew", pady=(12, 0))

        self.trenball_status_label = tk.Label(
            self.track_card,
            text="",
            bg="#13233d",
            fg="#cfe0fb",
            anchor="w",
            justify="left",
            wraplength=max(160, self.SIDE_PANEL_WIDTH - 56),
            padx=14,
            pady=12,
            font=("Segoe UI", 9),
            highlightthickness=1,
            highlightbackground="#1b2c48",
        )
        self.trenball_status_label.grid(row=6, column=0, sticky="ew", pady=(14, 0))

    def _open_telegram_alert_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Telegram Alerts")
        dialog.configure(bg="#0f1c31")
        dialog.geometry("760x620")
        dialog.minsize(680, 560)
        dialog.transient(self)
        dialog.grab_set()

        working_rules = [AlertRule.from_dict(rule.to_dict()) for rule in self.telegram_rules]
        bot_token = tk.StringVar(value=self.telegram_bot_token)
        chat_id = tk.StringVar(value=self.telegram_chat_id)
        no_ten_x_enabled = tk.BooleanVar(value=self.telegram_no_10x_enabled)
        no_ten_x_points = tk.StringVar(value=str(self.telegram_no_10x_points))
        name = tk.StringVar(value="Alert")
        enabled = tk.BooleanVar(value=True)
        expected = tk.StringVar(value="0")
        tolerance = tk.StringVar(value="3")
        unlimited = tk.BooleanVar(value=True)
        forward_steps = tk.StringVar(value="100")
        selected_index: int | None = None

        frame = tk.Frame(dialog, bg="#0f1c31", padx=22, pady=20)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(5, weight=1)
        tk.Label(frame, text="Telegram Alerts", bg="#0f1c31", fg="#eef4ff", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        tk.Label(frame, text="One bot token and group/chat ID are shared by every alert.", bg="#0f1c31", fg="#9bb0cd", font=("Segoe UI", 9)).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 14))

        def add_field(row: int, label: str, variable: tk.StringVar, show: str | None = None, parent=frame) -> tk.Entry:
            tk.Label(frame, text=label, bg="#0f1c31", fg="#cfe0fb").grid(row=row, column=0, sticky="w", pady=5)
            entry = tk.Entry(frame, textvariable=variable, show=show or "", bg="#13233d", fg="#eef4ff", insertbackground="#eef4ff", relief="flat")
            entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(16, 0), pady=5, ipady=5)
            return entry

        add_field(2, "Global bot token", bot_token, show="*")
        add_field(3, "Global group/chat ID", chat_id)
        no_ten_row = tk.Frame(frame, bg="#0f1c31")
        no_ten_row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        tk.Checkbutton(
            no_ten_row, text="Alert when 0 or 1 × 10x appears in", variable=no_ten_x_enabled,
            bg="#0f1c31", fg="#eef4ff", selectcolor="#13233d",
            activebackground="#0f1c31", activeforeground="#eef4ff",
        ).pack(side="left")
        tk.Entry(
            no_ten_row, textvariable=no_ten_x_points, width=6, bg="#13233d",
            fg="#eef4ff", insertbackground="#eef4ff", relief="flat", justify="center",
        ).pack(side="left", padx=(8, 6), ipady=4)
        tk.Label(no_ten_row, text="points", bg="#0f1c31", fg="#cfe0fb").pack(side="left")

        list_frame = tk.Frame(frame, bg="#0f1c31")
        list_frame.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(14, 10))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        columns = ("name", "target", "tolerance", "range", "enabled")
        table = ttk.Treeview(list_frame, columns=columns, show="headings", height=7, selectmode="browse")
        for key, label, width in (("name", "Name", 170), ("target", "Target", 80), ("tolerance", "+/-", 60), ("range", "Steps", 180), ("enabled", "Enabled", 70)):
            table.heading(key, text=label)
            table.column(key, width=width, anchor="w" if key in {"name", "range"} else "center")
        table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        table.configure(yscrollcommand=scrollbar.set)

        editor = tk.Frame(frame, bg="#13233d", padx=14, pady=12)
        editor.grid(row=6, column=0, columnspan=3, sticky="ew")
        editor.columnconfigure(1, weight=1)

        def editor_field(row: int, label: str, variable: tk.StringVar) -> tk.Entry:
            tk.Label(editor, text=label, bg="#13233d", fg="#cfe0fb").grid(row=row, column=0, sticky="w", pady=4)
            entry = tk.Entry(editor, textvariable=variable, bg="#0f1c31", fg="#eef4ff", insertbackground="#eef4ff", relief="flat")
            entry.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=4, ipady=4)
            return entry

        editor_field(0, "Name", name)
        editor_field(1, "Expected graph value", expected)
        editor_field(2, "Tolerance (+/-)", tolerance)
        forward_entry = editor_field(3, "Next number of steps", forward_steps)
        checks = tk.Frame(editor, bg="#13233d")
        checks.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        tk.Checkbutton(checks, text="Enabled", variable=enabled, bg="#13233d", fg="#eef4ff", selectcolor="#0f1c31", activebackground="#13233d", activeforeground="#eef4ff").pack(side="left")
        tk.Checkbutton(checks, text="Unlimited forward steps", variable=unlimited, bg="#13233d", fg="#eef4ff", selectcolor="#0f1c31", activebackground="#13233d", activeforeground="#eef4ff").pack(side="left", padx=(18, 0))

        def sync_range_state(*_args: object) -> None:
            state = "disabled" if unlimited.get() else "normal"
            forward_entry.configure(state=state)

        unlimited.trace_add("write", sync_range_state)
        sync_range_state()

        def refresh_table(select: int | None = None) -> None:
            table.delete(*table.get_children())
            for index, rule in enumerate(working_rules):
                step_range = "Unlimited" if rule.unlimited else f"{rule.start_step:,}-{rule.end_step:,}"
                table.insert("", "end", iid=str(index), values=(rule.name, f"{rule.expected_value:+d}", rule.tolerance, step_range, "Yes" if rule.enabled else "No"))
            if select is not None and 0 <= select < len(working_rules):
                table.selection_set(str(select))
                table.focus(str(select))

        def read_editor() -> AlertRule | None:
            try:
                step_limit = int(forward_steps.get())
                new_rule = AlertRule(
                    name=name.get().strip() or f"Alert {len(working_rules) + 1}", enabled=enabled.get(),
                    expected_value=int(expected.get()), tolerance=int(tolerance.get()),
                    unlimited=unlimited.get(), start_step=self.step_count + 1,
                    end_step=self.step_count + step_limit,
                )
                if new_rule.tolerance < 0 or step_limit < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid alert", "Use whole numbers, a tolerance of 0 or more, and at least one forward step.", parent=dialog)
                return None
            return new_rule

        def select_rule(_event=None) -> None:
            nonlocal selected_index
            selected = table.selection()
            if not selected:
                return
            selected_index = int(selected[0])
            rule = working_rules[selected_index]
            name.set(rule.name); enabled.set(rule.enabled)
            expected.set(str(rule.expected_value)); tolerance.set(str(rule.tolerance))
            unlimited.set(rule.unlimited)
            forward_steps.set(str(max(1, rule.end_step - rule.start_step + 1)))

        def add_rule() -> None:
            nonlocal selected_index
            rule = read_editor()
            if rule is None:
                return
            working_rules.append(rule)
            selected_index = len(working_rules) - 1
            refresh_table(selected_index)

        def update_rule() -> None:
            if selected_index is None:
                messagebox.showinfo("Select an alert", "Select an alert to update.", parent=dialog)
                return
            rule = read_editor()
            if rule is not None:
                working_rules[selected_index] = rule
                refresh_table(selected_index)

        def remove_rule() -> None:
            nonlocal selected_index
            if selected_index is None:
                return
            working_rules.pop(selected_index)
            selected_index = None
            refresh_table()

        table.bind("<<TreeviewSelect>>", select_rule)
        refresh_table(0 if working_rules else None)
        if working_rules:
            select_rule()

        rule_buttons = tk.Frame(frame, bg="#0f1c31")
        rule_buttons.grid(row=7, column=0, sticky="w", pady=(10, 0))
        self._utility_button(rule_buttons, "Add", "#1f5a3a", add_rule).pack(side="left")
        self._utility_button(rule_buttons, "Update", "#1f3b63", update_rule).pack(side="left", padx=(8, 0))
        self._utility_button(rule_buttons, "Remove", "#5a2430", remove_rule).pack(side="left", padx=(8, 0))

        def save_all() -> None:
            token, group = bot_token.get().strip(), chat_id.get().strip()
            try:
                missing_points = int(no_ten_x_points.get())
                if missing_points < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid 10x alert", "The missing-10x point count must be a positive whole number.", parent=dialog)
                return
            if (any(rule.enabled for rule in working_rules) or no_ten_x_enabled.get()) and (not token or not group):
                messagebox.showerror("Missing credentials", "Global bot token and group/chat ID are required for enabled alerts.", parent=dialog)
                return
            self.telegram_bot_token = token
            self.telegram_chat_id = group
            self.telegram_rules = working_rules
            self.telegram_alert_inside_bands = [False] * len(working_rules)
            self.telegram_no_10x_enabled = no_ten_x_enabled.get()
            self.telegram_no_10x_points = missing_points
            self.telegram_no_10x_alert_sent = False
            self._persist_trenball_config()
            dialog.destroy()
            self._refresh_trenball_status(f"Saved {len(working_rules)} Telegram alert(s)")

        buttons = tk.Frame(frame, bg="#0f1c31")
        buttons.grid(row=7, column=1, columnspan=2, sticky="e", pady=(10, 0))
        self._utility_button(buttons, "Cancel", "#3a4252", dialog.destroy).pack(side="left", padx=(0, 8))
        self._utility_button(buttons, "Save All", "#1f5a3a", save_all).pack(side="left")
        dialog.wait_visibility()
        dialog.focus_force()

    def _build_chart_panel(self, parent: tk.Widget) -> None:
        self.chart_card = self._card(parent, bg="#0f1c31", padx=18, pady=18)
        self.chart_card.grid(row=0, column=1, sticky="nsew")
        self.chart_card.columnconfigure(0, weight=1)
        self.chart_card.rowconfigure(2, weight=1)

        top_bar = tk.Frame(self.chart_card, bg="#0f1c31")
        top_bar.grid(row=0, column=0, sticky="ew")
        top_bar.columnconfigure(0, weight=1)
        top_bar.columnconfigure(1, weight=0)
        top_bar.columnconfigure(2, weight=0)

        title_block = self._section_title(
            top_bar,
            "Trend View",
            "Main chart shows a readable window. Overview chart keeps the full history visible.",
            wraplength=720,
        )
        title_block.grid(row=0, column=0, sticky="w")

        zoom_bar = tk.Frame(top_bar, bg="#0f1c31")
        zoom_bar.grid(row=0, column=1, sticky="e", padx=(20, 16))
        for index, (label, size) in enumerate(self.WINDOW_OPTIONS):
            button = self._mode_button(zoom_bar, label, lambda value=size: self._set_window_size(value))
            button.grid(row=0, column=index, padx=(0 if index == 0 else 8, 0))
            self.window_buttons[size] = button

        self.follow_toggle = tk.Checkbutton(
            top_bar,
            text="Follow latest",
            variable=self.auto_follow,
            command=self._refresh_ui,
            bg="#0f1c31",
            fg="#d7e4fb",
            activebackground="#0f1c31",
            activeforeground="#eef4ff",
            selectcolor="#173156",
            font=("Segoe UI", 10, "bold"),
            bd=0,
            highlightthickness=0,
        )
        self.follow_toggle.grid(row=0, column=2, sticky="e")

        self.range_label = tk.Label(
            self.chart_card,
            text="",
            bg="#0f1c31",
            fg="#9bb0cd",
            font=("Segoe UI", 10),
            anchor="w",
        )
        self.range_label.grid(row=1, column=0, sticky="ew", pady=(8, 14))

        self.detail_canvas = tk.Canvas(
            self.chart_card,
            bg="#0a1526",
            highlightthickness=0,
            height=self.DETAIL_HEIGHT,
            bd=0,
        )
        self.detail_canvas.grid(row=2, column=0, sticky="nsew")

        navigator = tk.Frame(self.chart_card, bg="#0f1c31")
        navigator.grid(row=3, column=0, sticky="ew", pady=(16, 12))
        navigator.columnconfigure(1, weight=1)

        nav_label = tk.Label(
            navigator,
            text="Window Position",
            bg="#0f1c31",
            fg="#9bb0cd",
            font=("Segoe UI", 10, "bold"),
        )
        nav_label.grid(row=0, column=0, sticky="w", padx=(0, 14))

        self.position_scale = tk.Scale(
            navigator,
            from_=0,
            to=0,
            orient="horizontal",
            showvalue=False,
            command=self._on_position_change,
            bg="#0f1c31",
            fg="#d7e4fb",
            troughcolor="#1b2c48",
            activebackground="#5d86ff",
            highlightthickness=0,
            bd=0,
            sliderlength=24,
            resolution=1,
        )
        self.position_scale.grid(row=0, column=1, sticky="ew")

        self.position_label = tk.Label(
            navigator,
            text="",
            bg="#0f1c31",
            fg="#d7e4fb",
            font=("Segoe UI", 10),
            width=18,
            anchor="e",
        )
        self.position_label.grid(row=0, column=2, sticky="e", padx=(14, 0))

        overview_wrap = tk.Frame(self.chart_card, bg="#0f1c31")
        overview_wrap.grid(row=4, column=0, sticky="ew")
        overview_wrap.columnconfigure(0, weight=1)

        overview_label = tk.Label(
            overview_wrap,
            text="Full History Overview",
            bg="#0f1c31",
            fg="#9bb0cd",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        )
        overview_label.grid(row=0, column=0, sticky="w", pady=(0, 8))

        self.overview_canvas = tk.Canvas(
            overview_wrap,
            bg="#0c182a",
            highlightthickness=0,
            height=self.OVERVIEW_HEIGHT,
            bd=0,
        )
        self.overview_canvas.grid(row=1, column=0, sticky="ew")

        self.footer_note = tk.Label(
            self.chart_card,
            text="",
            bg="#0f1c31",
            fg="#9bb0cd",
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
        )
        self.footer_note.grid(row=5, column=0, sticky="ew", pady=(12, 0))

        self.detail_canvas.bind("<Configure>", lambda _event: self._draw_charts())
        self.detail_canvas.bind("<Motion>", self._on_detail_hover)
        self.detail_canvas.bind("<Leave>", lambda _event: self._clear_detail_hover())
        self.overview_canvas.bind("<Configure>", lambda _event: self._draw_charts())
        self.overview_canvas.bind("<ButtonPress-1>", self._start_overview_drag)
        self.overview_canvas.bind("<B1-Motion>", self._update_overview_drag)
        self.overview_canvas.bind("<ButtonRelease-1>", self._finish_overview_drag)

    def _bind_shortcuts(self) -> None:
        if self.history_view:
            return
        self.bind("<Left>", lambda _event: self._add_step(-1, "2x-"))
        self.bind("<Right>", lambda _event: self._add_step(1, "2x+"))
        self.bind("<Up>", lambda _event: self._add_step(1, "2x+"))
        self.bind("<Down>", lambda _event: self._add_step(-1, "2x-"))
        self.bind("<BackSpace>", lambda _event: self._undo())
        self.bind("<Control-s>", lambda _event: self._save_session_dialog())
        self.bind("<Control-o>", lambda _event: self._load_session_dialog())
        self.bind("<Control-n>", lambda _event: self._clear_session())

    def _card(self, parent: tk.Widget, bg: str, padx: int, pady: int) -> tk.Frame:
        frame = tk.Frame(parent, bg=bg, padx=padx, pady=pady, highlightthickness=1, highlightbackground="#1b2c48")
        accent = tk.Frame(frame, bg="#5d86ff", height=3)
        accent.place(relx=0.0, rely=0.0, relwidth=1.0)
        return frame

    def _section_title(
        self,
        parent: tk.Widget,
        title: str,
        subtitle: str,
        *,
        wraplength: int | None = None,
    ) -> tk.Frame:
        frame = tk.Frame(parent, bg=parent.cget("bg"))
        frame.columnconfigure(0, weight=1)
        # Default wrap suits the narrow settings sidebar; chart titles pass a larger wrap.
        wrap = self.SIDE_PANEL_WIDTH - 56 if wraplength is None else wraplength
        tk.Label(
            frame,
            text=title,
            bg=parent.cget("bg"),
            fg="#eef4ff",
            font=("Segoe UI", 17, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        tk.Label(
            frame,
            text=subtitle,
            bg=parent.cget("bg"),
            fg="#9bb0cd",
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=max(120, wrap),
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        return frame

    def _mode_button(self, parent: tk.Widget, text: str, command) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
            activeforeground="#eef4ff",
            highlightthickness=0,
        )

    def _utility_button(self, parent: tk.Widget, text: str, bg: str, command) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            relief="flat",
            bd=0,
            padx=12,
            pady=11,
            bg=bg,
            fg="#eef4ff",
            activebackground=bg,
            activeforeground="#ffffff",
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
            highlightthickness=0,
        )

    def _action_button(
        self,
        parent: tk.Widget,
        title: str,
        subtitle: str,
        bg: str,
        subtitle_bg: str,
        command,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=f"{title}\n{subtitle}",
            command=command,
            justify="center",
            relief="flat",
            bd=0,
            bg=bg,
            fg="#ffffff",
            activebackground=bg,
            activeforeground="#ffffff",
            font=("Segoe UI", 20, "bold"),
            padx=14,
            pady=18,
            highlightthickness=0,
            cursor="hand2",
        )

    def _metric_card(self, parent: tk.Widget, title: str) -> tuple[tk.Frame, tk.Label]:
        frame = tk.Frame(parent, bg="#13233d", padx=14, pady=12, highlightthickness=1, highlightbackground="#1b2c48")
        frame.columnconfigure(0, weight=1)
        tk.Label(
            frame,
            text=title,
            bg="#13233d",
            fg="#9bb0cd",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        value = tk.Label(
            frame,
            text="0",
            bg="#13233d",
            fg="#eef4ff",
            font=("Segoe UI", 18, "bold"),
            anchor="w",
        )
        value.grid(row=1, column=0, sticky="w", pady=(6, 0))
        return frame, value

    def _set_layout_mode(self, mode: str) -> None:
        if self.layout_mode == mode:
            return
        self.layout_mode = mode
        self._render_action_buttons()
        self._update_mode_buttons(self.layout_buttons, self.layout_mode)

    def _render_action_buttons(self) -> None:
        for widget in (self.plus_button, self.minus_button, self.ten_x_button, self.undo_button):
            widget.grid_forget()

        for row in range(4):
            self.action_grid.grid_rowconfigure(row, weight=0, uniform="")
        for column in range(3):
            self.action_grid.grid_columnconfigure(column, weight=0, uniform="")

        if self.layout_mode == "stacked":
            self.action_grid.grid_rowconfigure(0, weight=1, uniform="stack")
            self.action_grid.grid_rowconfigure(1, weight=1, uniform="stack")
            self.action_grid.grid_rowconfigure(2, weight=1, uniform="stack")
            self.action_grid.grid_rowconfigure(3, weight=1, uniform="stack")
            self.action_grid.grid_columnconfigure(0, weight=1)
            self.plus_button.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
            self.minus_button.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
            self.ten_x_button.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
            self.undo_button.grid(row=3, column=0, sticky="nsew")
        elif self.layout_mode == "split":
            self.action_grid.grid_rowconfigure(0, weight=1, uniform="split")
            self.action_grid.grid_rowconfigure(1, weight=1, uniform="split")
            self.action_grid.grid_rowconfigure(2, weight=1, uniform="split")
            self.action_grid.grid_columnconfigure(0, weight=2, uniform="split")
            self.action_grid.grid_columnconfigure(1, weight=1, uniform="split")
            self.plus_button.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 10), pady=(0, 10))
            self.minus_button.grid(row=0, column=1, sticky="nsew", pady=(0, 10))
            self.ten_x_button.grid(row=1, column=1, sticky="nsew", pady=(0, 10))
            self.undo_button.grid(row=2, column=0, columnspan=2, sticky="nsew")
        else:
            self.action_grid.grid_rowconfigure(0, weight=1, uniform="wide")
            self.action_grid.grid_rowconfigure(1, weight=1, uniform="wide")
            self.action_grid.grid_columnconfigure(0, weight=1, uniform="wide")
            self.action_grid.grid_columnconfigure(1, weight=1, uniform="wide")
            self.action_grid.grid_columnconfigure(2, weight=1, uniform="wide")
            self.plus_button.grid(row=0, column=0, sticky="nsew", padx=(0, 10), pady=(0, 10))
            self.minus_button.grid(row=0, column=1, sticky="nsew", padx=(0, 10), pady=(0, 10))
            self.ten_x_button.grid(row=0, column=2, sticky="nsew", pady=(0, 10))
            self.undo_button.grid(row=1, column=0, columnspan=3, sticky="nsew")

    def _set_window_size(self, size: int | None) -> None:
        self.window_size = size
        self._refresh_ui()

    def _save_session_dialog(self) -> None:
        initial_file = self._default_session_filename()
        if self.current_file_path:
            initial_file = Path(self.current_file_path).name

        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save 2X Graph Session",
            defaultextension=".json",
            initialfile=initial_file,
            filetypes=[("2X Graph session", "*.json"), ("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            saved_at = self._write_session_file(path)
        except Exception as exc:
            messagebox.showerror("Save failed", f"Could not save the session.\n\n{exc}", parent=self)
            return

        self.focus_force()
        messagebox.showinfo(
            "Session saved",
            f"Saved {self.step_count:,} steps at {self._format_timestamp(saved_at)}.\n\n{path}",
            parent=self,
        )

    def _load_session_dialog(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Load 2X Graph Session",
            filetypes=[("2X Graph session", "*.json"), ("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            self._stop_trenball_tracking(silent=True)
            self._load_session_file(path)
        except Exception as exc:
            messagebox.showerror("Load failed", f"Could not load the session.\n\n{exc}", parent=self)
            return

        self.focus_force()
        messagebox.showinfo(
            "Session loaded",
            f"Loaded {self.step_count:,} steps from:\n\n{path}",
            parent=self,
        )

    def _open_history_browser(self) -> None:
        directory = filedialog.askdirectory(
            parent=self,
            title="Select 2X Graph History Directory",
            initialdir=str(self.SESSION_DATA_DIR),
            mustexist=True,
        )
        if not directory:
            return

        paths = sorted(
            Path(directory).glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        browser = tk.Toplevel(self)
        browser.title(f"2X Graph History - {directory}")
        browser.geometry("820x520")
        browser.minsize(620, 360)
        browser.configure(bg="#08111f")
        browser.transient(self)

        frame = tk.Frame(browser, bg="#08111f", padx=20, pady=20)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame, text="Saved 2X Graph Sessions", bg="#08111f", fg="#eef4ff",
            font=("Segoe UI", 18, "bold"), anchor="w",
        ).pack(fill="x")
        directory_label = tk.Label(
            frame, text=directory, bg="#08111f", fg="#9bb0cd",
            font=("Segoe UI", 9), anchor="w", wraplength=760,
        )
        directory_label.pack(fill="x", pady=(4, 14))

        table_frame = tk.Frame(frame, bg="#08111f")
        table_frame.pack(fill="both", expand=True)
        columns = ("file", "steps", "modified")
        table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        table.heading("file", text="Session file")
        table.heading("steps", text="Steps")
        table.heading("modified", text="Modified")
        table.column("file", width=470, anchor="w")
        table.column("steps", width=90, anchor="e")
        table.column("modified", width=170, anchor="center")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scrollbar.set)
        table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        file_by_item: dict[str, Path] = {}
        invalid_count = 0
        for path in paths:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                series, _timestamps, _events, _settings = self._parse_session_payload(payload)
                steps = max(0, len(series) - 1)
            except Exception:
                invalid_count += 1
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            item_id = table.insert("", "end", values=(path.name, f"{steps:,}", modified))
            file_by_item[item_id] = path

        status = tk.Label(
            frame, bg="#08111f", fg="#9bb0cd", font=("Segoe UI", 9), anchor="w"
        )
        status.pack(fill="x", pady=(12, 0))
        status.configure(text=f"{len(file_by_item)} session(s)" + (f"; {invalid_count} invalid JSON file(s) skipped" if invalid_count else ""))

        def open_selected(_event=None) -> None:
            selection = table.selection()
            if not selection:
                messagebox.showinfo("Select a session", "Select a JSON session first.", parent=browser)
                return
            self._launch_session_window(file_by_item[selection[0]], browser)

        buttons = tk.Frame(frame, bg="#08111f")
        buttons.pack(fill="x", pady=(14, 0))
        self._utility_button(buttons, "Close", "#3a4252", browser.destroy).pack(side="right")
        self._utility_button(buttons, "Open in New Window", "#1f5a3a", open_selected).pack(side="right", padx=(0, 8))
        table.bind("<Double-1>", open_selected)
        if file_by_item:
            first = next(iter(file_by_item))
            table.selection_set(first)
            table.focus(first)
        else:
            status.configure(text="No valid 2X Graph JSON sessions found in this directory.")

    def _launch_session_window(self, path: Path, parent: tk.Widget) -> None:
        try:
            if getattr(sys, "frozen", False):
                command = [sys.executable, "--session", str(path)]
            else:
                command = [sys.executable, str(Path(__file__).resolve()), "--session", str(path)]
            subprocess.Popen(command, cwd=str(Path(__file__).resolve().parent))
        except Exception as exc:
            messagebox.showerror("Could not open window", str(exc), parent=parent)

    def _clear_session(self) -> None:
        if self.step_count == 0 and not self.is_dirty:
            self._stop_trenball_tracking(silent=True)
            self.trenball_prev_decode = None
            self.trenball_pending_decode = None
            self.trenball_reject_candidate = None
            self.header_status.configure(text="Already clear")
            return

        confirm = messagebox.askyesno(
            "Clear session",
            "Clear the current graph history?\n\n"
            "This removes all recorded steps from this session. "
            "Saved files on disk are not deleted.",
            parent=self,
        )
        if not confirm:
            return

        self._stop_trenball_tracking(silent=True)
        self.trenball_prev_decode = None
        self.trenball_pending_decode = None
        self.trenball_reject_candidate = None
        self.series = [0]
        self.point_timestamps = [self._now_iso()]
        self.point_event_types = ["start"]
        self.view_start = 0
        self.current_file_path = None
        self.last_saved_at = None
        self.auto_save_active = False
        self.is_dirty = False
        self._refresh_ui()
        self._refresh_trenball_status()
        self.header_status.configure(text="Cleared")

    def _build_auto_session_path(self) -> Path:
        self.SESSION_DATA_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base_name = f"2x-graph-session-{stamp}"
        candidate = self.SESSION_DATA_DIR / f"{base_name}.json"
        suffix = 1
        while candidate.exists():
            candidate = self.SESSION_DATA_DIR / f"{base_name}-{suffix:02d}.json"
            suffix += 1
        return candidate

    def _ensure_auto_save_file(self) -> str:
        if self.current_file_path:
            path = Path(self.current_file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path = self._build_auto_session_path()
        self._write_session_file(str(path))
        return str(path)

    def _autosave_session(self) -> bool:
        if not self.auto_save_active or not self.current_file_path:
            return False

        try:
            self._write_session_file(self.current_file_path)
        except Exception as exc:
            self.is_dirty = True
            self._refresh_ui()
            self._refresh_trenball_status(f"Autosave failed: {exc}")
            self.header_status.configure(text="Autosave failed")
        return True

    def _write_session_file(self, path: str) -> str:
        saved_at = self._now_iso()
        payload = self._build_session_payload(saved_at=saved_at)
        with open(path, "w", encoding="utf-8") as file_handle:
            json.dump(payload, file_handle, indent=2)

        self.current_file_path = path
        self.last_saved_at = saved_at
        self.is_dirty = False
        self._refresh_ui()
        return saved_at

    def _load_session_file(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as file_handle:
            payload = json.load(file_handle)

        series, timestamps, event_types, settings = self._parse_session_payload(payload)
        self.series = series
        self.point_timestamps = timestamps
        self.point_event_types = event_types
        self.layout_mode = settings["layout_mode"]
        self.window_size = settings["window_size"]
        self.auto_follow.set(settings["auto_follow"])
        self.show_10x_marks = True
        self.current_file_path = path
        self.last_saved_at = settings["saved_at"]
        self.auto_save_active = False
        self.is_dirty = False
        self.view_start = min(settings["view_start"], self._max_view_start())
        self._render_action_buttons()
        self._refresh_ui()

    def _build_session_payload(self, saved_at: str) -> dict:
        points = []
        for index, value in enumerate(self.series):
            point = {
                "index": index,
                "value": value,
                "timestamp": self.point_timestamps[index],
                "event_type": self.point_event_types[index],
            }
            if index > 0:
                point["delta"] = value - self.series[index - 1]
            points.append(point)

        return {
            "format": "2x-graph-session",
            "version": 1,
            "created_at": self.point_timestamps[0],
            "saved_at": saved_at,
            "step_count": self.step_count,
            "layout_mode": self.layout_mode,
            "window_size": self.window_size,
            "auto_follow": self.auto_follow.get(),
            "show_10x_marks": self.show_10x_marks,
            "view_start": self.view_start,
            "points": points,
        }

    def _parse_session_payload(self, payload: dict) -> tuple[list[int], list[str], list[str], dict]:
        if not isinstance(payload, dict):
            raise ValueError("The selected file is not a valid session object.")

        points = payload.get("points")
        if not isinstance(points, list) or not points:
            raise ValueError("The selected file does not contain any saved points.")

        series = []
        timestamps = []
        event_types = []
        for index, point in enumerate(points):
            if not isinstance(point, dict):
                raise ValueError("A saved point is malformed.")

            value = point.get("value")
            timestamp = point.get("timestamp")
            if not isinstance(value, int):
                raise ValueError(f"Point {index} has an invalid value.")
            if not isinstance(timestamp, str) or not timestamp.strip():
                raise ValueError(f"Point {index} is missing its timestamp.")

            event_type = point.get("event_type")
            if index > 0:
                delta = value - series[-1]
                if delta not in (-1, 1):
                    raise ValueError("Loaded data must move only by +1 or -1 at each step.")
                if event_type not in {"2x+", "2x-", "10x"}:
                    event_type = "2x+" if delta > 0 else "2x-"
                if delta < 0 and event_type != "2x-":
                    raise ValueError("Negative steps must use the 2x- event type.")
                if delta > 0 and event_type not in {"2x+", "10x"}:
                    raise ValueError("Positive steps must use the 2x+ or 10x event type.")
            else:
                event_type = "start"

            series.append(value)
            timestamps.append(timestamp)
            event_types.append(event_type)

        if len(series) - 1 > self.MAX_STEPS:
            raise ValueError(f"This file has more than {self.MAX_STEPS:,} steps.")

        valid_layout_modes = {mode for _, mode in self.LAYOUT_OPTIONS}
        layout_mode = payload.get("layout_mode")
        if layout_mode not in valid_layout_modes:
            layout_mode = "wide"

        valid_window_sizes = {size for _, size in self.WINDOW_OPTIONS}
        window_size = payload.get("window_size")
        if window_size not in valid_window_sizes:
            window_size = 250

        view_start = payload.get("view_start", 0)
        if not isinstance(view_start, int):
            view_start = 0

        return series, timestamps, event_types, {
            "layout_mode": layout_mode,
            "window_size": window_size,
            "auto_follow": bool(payload.get("auto_follow", True)),
            "show_10x_marks": bool(payload.get("show_10x_marks", True)),
            "view_start": max(0, view_start),
            "saved_at": payload.get("saved_at") if isinstance(payload.get("saved_at"), str) else None,
        }

    def _default_session_filename(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"2x-graph-session-{stamp}.json"

    def _now_iso(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _format_timestamp(self, value: str | None) -> str:
        if not value:
            return "Not available"

        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        return parsed.strftime("%Y-%m-%d %H:%M:%S %z")

    def _on_position_change(self, raw_value: str) -> None:
        max_start = self._max_view_start()
        if max_start == 0:
            return

        requested = max(0, min(max_start, int(float(raw_value))))
        if requested != max_start:
            self.auto_follow.set(False)
        self.view_start = requested
        self._refresh_ui(keep_scale_position=True)

    def _overview_plot_bounds(self) -> tuple[int, int, int]:
        plot_left = 16
        plot_right = max(plot_left + 1, self.overview_canvas.winfo_width() - 16)
        usable_width = max(1, plot_right - plot_left)
        return plot_left, plot_right, usable_width

    def _overview_index_from_x(self, x: float) -> int:
        plot_left, plot_right, usable_width = self._overview_plot_bounds()
        clamped_x = min(max(x, plot_left), plot_right)
        if len(self.series) <= 1:
            return 0
        ratio = (clamped_x - plot_left) / usable_width
        return int(round(ratio * (len(self.series) - 1)))

    def _clear_overview_drag(self, *, redraw: bool = True) -> None:
        self.overview_drag_start_x = None
        self.overview_drag_current_x = None
        if redraw:
            self._draw_overview_chart()

    def _start_overview_drag(self, event: tk.Event) -> None:
        plot_left, plot_right, _usable_width = self._overview_plot_bounds()
        self.overview_drag_start_x = min(max(event.x, plot_left), plot_right)
        self.overview_drag_current_x = self.overview_drag_start_x
        self._draw_overview_chart()

    def _update_overview_drag(self, event: tk.Event) -> None:
        if self.overview_drag_start_x is None:
            return
        plot_left, plot_right, _usable_width = self._overview_plot_bounds()
        self.overview_drag_current_x = min(max(event.x, plot_left), plot_right)
        self._draw_overview_chart()

    def _apply_overview_selection(self, start_index: int, end_index: int) -> None:
        if len(self.series) <= 1:
            return

        start = max(0, min(start_index, end_index))
        end = min(len(self.series) - 1, max(start_index, end_index))
        selected_points = end - start + 1

        if selected_points >= len(self.series):
            self.window_size = None
            self.view_start = 0
        else:
            self.window_size = selected_points
            self.view_start = min(start, len(self.series) - selected_points)

        self.auto_follow.set(False)
        self._refresh_ui()

    def _finish_overview_drag(self, event: tk.Event) -> None:
        if self.overview_drag_start_x is None:
            return

        plot_left, plot_right, _usable_width = self._overview_plot_bounds()
        release_x = min(max(event.x, plot_left), plot_right)
        drag_width = abs(release_x - self.overview_drag_start_x)
        start_x = self.overview_drag_start_x
        self._clear_overview_drag(redraw=False)

        if drag_width < 8:
            self._seek_from_overview_x(release_x)
            return

        self._apply_overview_selection(
            self._overview_index_from_x(start_x),
            self._overview_index_from_x(release_x),
        )

    def _seek_from_overview_x(self, x: float) -> None:
        max_start = self._max_view_start()
        if max_start == 0:
            return

        total_points = len(self.series)
        window_points = self._window_points()
        center_index = self._overview_index_from_x(x)
        start = center_index - window_points // 2
        self.view_start = max(0, min(max_start, start))
        if self.view_start != max_start:
            self.auto_follow.set(False)
        self._refresh_ui()

    def _seek_from_overview(self, event: tk.Event) -> None:
        self._seek_from_overview_x(event.x)

    def _add_step(self, step: int, event_type: str) -> None:
        if self.step_count >= self.MAX_STEPS:
            return

        self.series.append(self.series[-1] + step)
        self.point_timestamps.append(self._now_iso())
        self.point_event_types.append(event_type)
        self._check_telegram_alert()
        self.is_dirty = True
        if self.auto_follow.get():
            self.view_start = self._max_view_start()
        if not self._autosave_session():
            self._refresh_ui()

    def _add_steps_from_beads(self, beads: list[str]) -> int:
        added = 0
        for bead in beads:
            mapping = BEAD_TO_EVENT.get(bead)
            if mapping is None:
                continue
            if self.step_count >= self.MAX_STEPS:
                break
            step, event_type = mapping
            self.series.append(self.series[-1] + step)
            self.point_timestamps.append(self._now_iso())
            self.point_event_types.append(event_type)
            added += 1
            self._check_telegram_alert()

        if added:
            self.is_dirty = True
            if self.auto_follow.get():
                self.view_start = self._max_view_start()
            if not self._autosave_session():
                self._refresh_ui()
        return added

    def _on_close(self) -> None:
        prompt_parts = ["Close 2X Graph?"]
        if self.is_dirty:
            prompt_parts.append("You have unsaved changes in this session.")
        if self.trenball_running:
            prompt_parts.append("Auto Track is running and will be stopped.")
        prompt_parts.append("This will close the app.")

        confirm = messagebox.askyesno(
            "Exit 2X Graph",
            "\n\n".join(prompt_parts),
            parent=self,
        )
        if not confirm:
            self.focus_force()
            return

        self._stop_trenball_tracking(silent=True)
        prevent_sleep(False)
        self.destroy()

    def _persist_trenball_config(self) -> None:
        save_config(
            {
                "crash_url": self.crash_url,
                "selenium_headless": self.selenium_headless,
                "telegram_credentials": {
                    "bot_token": self.telegram_bot_token,
                    "chat_id": self.telegram_chat_id,
                },
                "telegram_alerts": [rule.to_dict() for rule in self.telegram_rules],
                "telegram_no_10x_alert": {
                    "enabled": self.telegram_no_10x_enabled,
                    "points": self.telegram_no_10x_points,
                },
            }
        )

    def _check_telegram_alert(self) -> None:
        if len(self.telegram_alert_inside_bands) != len(self.telegram_rules):
            self.telegram_alert_inside_bands = [False] * len(self.telegram_rules)
        triggered: list[AlertRule] = []
        for index, rule in enumerate(self.telegram_rules):
            matches = rule.matches(self.step_count, self.series[-1])
            if matches and not self.telegram_alert_inside_bands[index]:
                triggered.append(rule)
            self.telegram_alert_inside_bands[index] = matches
        recent_10x_count, available_points = count_10x_in_recent_points(
            self.point_event_types, self.telegram_no_10x_points
        )
        low_10x_condition = (
            available_points >= self.telegram_no_10x_points and recent_10x_count <= 1
        )
        if not low_10x_condition:
            self.telegram_no_10x_alert_sent = False
        missing_10x_triggered = (
            self.telegram_no_10x_enabled
            and low_10x_condition
            and not self.telegram_no_10x_alert_sent
        )
        if not triggered and not missing_10x_triggered:
            return
        messages = [
            f"2X Graph alert [{rule.name}]: value {self.series[-1]:+d} reached "
            f"target {rule.expected_value:+d} +/- {rule.tolerance} at step {self.step_count}."
            for rule in triggered
        ]
        if missing_10x_triggered:
            messages.append(
                f"2X Graph alert [10x scarcity]: only {recent_10x_count} × 10x "
                f"result(s) in the latest {self.telegram_no_10x_points} points. Current graph value: "
                f"{self.series[-1]:+d} at step {self.step_count}."
            )
            self.telegram_no_10x_alert_sent = True

        alert_names = [rule.name for rule in triggered]
        if missing_10x_triggered:
            alert_names.append("10x scarcity")
        detail_lines = [
            message.removeprefix("2X Graph alert ")
            for message in messages
        ]
        self._show_app_alert(
            "Alert triggered: " + ", ".join(alert_names),
            "\n".join(detail_lines) + f"\n{datetime.now().strftime('%H:%M:%S')}",
        )

        token = self.telegram_bot_token
        chat_id = self.telegram_chat_id
        if not token or not chat_id:
            self._refresh_trenball_status("Telegram alert skipped: credentials are not configured")
            return

        def deliver() -> None:
            try:
                for message in messages:
                    send_telegram_message(token, chat_id, message)
            except Exception as exc:
                error_status = f"Telegram alert failed: {exc}"
                self.after(0, lambda status=error_status: self._telegram_delivery_done(status))
            else:
                count = len(messages)
                self.after(0, lambda: self._telegram_delivery_done(f"Sent {count} Telegram alert(s)"))

        threading.Thread(target=deliver, daemon=True).start()

    def _telegram_delivery_done(self, status: str) -> None:
        self._refresh_trenball_status(status)

    def _refresh_trenball_status(self, extra: str | None = None) -> None:
        if self.trenball_region is None:
            region_text = "Region: not set"
        else:
            r = self.trenball_region
            region_text = (
                f"Region: {r.width}x{r.height} @ ({r.left}, {r.top})\n"
                f"{r.monitor_hint()}"
            )

        if self.trenball_running and self.trenball_paused_for_lock:
            state_text = "Paused — Windows is locked (unlock to resume)"
        elif self.trenball_running:
            state_text = f"Tracking every {self.trenball_poll_seconds}s (keep-awake on)"
        else:
            state_text = "Idle"

        lines = [state_text, region_text]
        if extra:
            lines.append(extra)
        self.trenball_status_label.configure(text="\n".join(lines))

    def _select_trenball_region(self) -> None:
        self.withdraw()
        self.update()
        try:
            region = select_region(self)
        finally:
            self.deiconify()

        if region is None:
            self._refresh_trenball_status("Selection cancelled")
            return

        self.trenball_region = region
        self._persist_trenball_config()
        monitor = region.monitor_hint()
        self._refresh_trenball_status(
            f"Area saved on {monitor} — Preview or Start Track\n"
            f"{region.width}x{region.height} @ ({region.left}, {region.top})"
        )
        self.header_status.configure(text=f"Area set ({monitor})")

    def _replace_graph_with_beads(self, beads: list[str]) -> int:
        """Reset the session and rebuild the graph from a trenball bead sequence."""
        self.series = [0]
        self.point_timestamps = [self._now_iso()]
        self.point_event_types = ["start"]
        self.view_start = 0
        self.current_file_path = None
        self.last_saved_at = None
        self.auto_save_active = False
        self.is_dirty = False
        if not beads:
            self._refresh_ui()
            return 0
        return self._add_steps_from_beads(beads)

    def _preview_trenball(self) -> None:
        if self.trenball_region is None:
            messagebox.showinfo("No region", "Select the trenball area first.", parent=self)
            return
        try:
            image = grab_region(self.trenball_region)
            if frame_is_obscured(image):
                reason = describe_obstruction(image)
                self._refresh_trenball_status(
                    f"Preview skipped — {reason}.\nTry again once the road is clear."
                )
                messagebox.showwarning(
                    "Road covered",
                    f"Something is drawn over the road right now ({reason}).\n\n"
                    "Reading it would load a wrong series, so nothing was changed. "
                    "A 100x+ win animation passes after a few seconds — preview again then.",
                    parent=self,
                )
                return

            decoded = decode_trend_grid(image, rows=self.trenball_grid_rows)
            seq = decoded.flat_sequence()
            bead_count = sum(1 for col in decoded.columns for c in col if c != ".")
            if self.step_count > 0:
                confirm = messagebox.askyesno(
                    "Load into graph",
                    f"Replace the current graph with this trenball snapshot?\n\n"
                    f"{len(seq)} beads in road order ({bead_count} cells)\n"
                    f"{summarize_streaks(seq)}\n\n"
                    "If many beads are missing, re-select the FULL 5-row strip.\n"
                    "Auto Track will then continue from this state.",
                    parent=self,
                )
                if not confirm:
                    self._refresh_trenball_status(
                        f"Preview only (not loaded): {len(seq)} beads\n{summarize_streaks(seq)}"
                    )
                    return

            added = self._replace_graph_with_beads(seq)
            # Baseline for auto-track so the next poll only appends NEW beads.
            self.trenball_prev_decode = decoded
            self.trenball_pending_decode = None
            self.trenball_reject_candidate = None
            self.trenball_paused_for_lock = False
            self.trenball_obscured_since = None
            self._mark_frame_trusted()
            self._refresh_trenball_status(
                f"Loaded {added} beads into graph\n{summarize_streaks(seq)}\n"
                "Start Track to continue from this state."
            )
            self.header_status.configure(text=f"Loaded {added} beads")
        except Exception as exc:
            messagebox.showerror("Preview failed", str(exc), parent=self)

    def _reset_trenball_baseline(self) -> None:
        """
        Forget what auto-track believes is on screen, without touching the graph.

        Use this after the road drifts out of sync: the next poll re-reads the
        strip as a fresh baseline and keeps appending to the existing series.
        """
        self.trenball_prev_decode = None
        self.trenball_pending_decode = None
        self.trenball_reject_candidate = None
        self.trenball_paused_for_lock = False
        self.trenball_obscured_since = None
        self.trenball_last_trusted_at = None

        if self.trenball_running:
            extra = "Baseline reset — re-syncing on next poll. Series kept."
        else:
            extra = "Baseline reset — Preview or Start Track to re-sync. Series kept."
        self._refresh_trenball_status(extra)
        self.header_status.configure(text=f"Baseline reset ({self.step_count:,} steps kept)")

    def _read_trenball_interval(self) -> bool:
        try:
            self.trenball_poll_seconds = max(5, int(self.trenball_interval_var.get().strip()))
        except ValueError:
            messagebox.showerror("Invalid interval", "Interval must be an integer (seconds).", parent=self)
            return False
        self.trenball_interval_var.set(str(self.trenball_poll_seconds))
        return True

    def _start_trenball_tracking(self) -> None:
        if self.trenball_region is None:
            messagebox.showinfo("No region", "Select the trenball area first.", parent=self)
            return
        if not self._read_trenball_interval():
            return

        self.auto_save_active = True
        try:
            session_path = self._ensure_auto_save_file()
        except Exception as exc:
            self.auto_save_active = False
            messagebox.showerror(
                "Autosave failed",
                f"Could not create or update the tracking session file.\n\n{exc}",
                parent=self,
            )
            return

        self._persist_trenball_config()
        self.trenball_running = True
        self.trenball_paused_for_lock = False
        # Keep preview baseline when present so tracking continues from loaded state.
        continue_from_preview = self.trenball_prev_decode is not None
        if not continue_from_preview:
            self.trenball_prev_decode = None
        self.trenball_pending_decode = None
        self.trenball_reject_candidate = None
        self.trenball_obscured_since = None
        self.trenball_last_trusted_at = None
        prevent_sleep(True)
        self.start_track_button.configure(state="disabled")
        self.stop_track_button.configure(state="normal")
        session_name = Path(session_path).name
        status_text = f"Baseline on next poll...\nAutosaving to {session_name}"
        if continue_from_preview:
            n = len(self.trenball_prev_decode.flat_sequence())
            status_text = f"Continuing from loaded state ({n} beads)...\nAutosaving to {session_name}"
            self._refresh_trenball_status(f"Continuing from loaded state ({n} beads)…")
            self.header_status.configure(text="Auto track on")
        else:
            self._refresh_trenball_status("Baseline on next poll…")
            self.header_status.configure(text="Auto track on")
        self._refresh_trenball_status(status_text)
        self._trenball_tick()

    def _stop_trenball_tracking(self, silent: bool = False) -> None:
        self.trenball_running = False
        self.trenball_paused_for_lock = False
        prevent_sleep(False)
        if self.trenball_timer_id is not None:
            self.after_cancel(self.trenball_timer_id)
            self.trenball_timer_id = None
        self.start_track_button.configure(state="normal")
        self.stop_track_button.configure(state="disabled")
        if not silent:
            self._refresh_trenball_status("Stopped")
            self.header_status.configure(text="Auto track off")

    def _trenball_tick(self) -> None:
        if not self.trenball_running:
            return
        self._trenball_poll_once()
        self.trenball_timer_id = self.after(self.trenball_poll_seconds * 1000, self._trenball_tick)

    def _max_new_beads_for_poll(self) -> int:
        """Small tolerance for several rounds completing between captures."""
        return self._max_new_beads_over(self.trenball_poll_seconds)

    def _max_new_beads_over(self, seconds: float) -> int:
        """How many rounds could really have finished in `seconds`."""
        return max_rounds_in(
            seconds,
            self.MIN_SECONDS_PER_ROUND,
            self.MAX_BASE_NEW_BEADS_PER_POLL,
        )

    def _seconds_since_trusted_frame(self) -> float:
        """Time the road has gone unobserved, i.e. how long rounds could hide."""
        if self.trenball_last_trusted_at is None:
            return float(self.trenball_poll_seconds)
        return max(0.0, time.monotonic() - self.trenball_last_trusted_at)

    def _mark_frame_trusted(self) -> None:
        self.trenball_last_trusted_at = time.monotonic()

    def _transition_rejection_reason(self, previous, current, diff) -> str | None:
        """
        Reject only frames that cannot be reconciled with the tracked road.

        A whole column scrolling out of the crop is normal, so column counts and
        scroll overlap are not used here — diff_grids already falls back to
        win/loss streak alignment for those frames.
        """
        if previous is None:
            return None

        prev_seq = previous.flat_sequence()
        curr_seq = current.flat_sequence()

        if prev_seq and not curr_seq:
            return "all beads disappeared"
        if diff.detail == UNALIGNED_DETAIL:
            return "road no longer matches the tracked win/loss series"

        max_new = self._max_new_beads_for_poll()
        if len(diff.new_beads) > max_new:
            return (
                f"implausible jump of {len(diff.new_beads)} beads "
                f"(limit {max_new})"
            )
        return None

    def _handle_obscured_frame(self, image, reason: str) -> None:
        """
        Drop a frame that has something painted over the road.

        A 100x+ win plays a cat-and-rainbow animation across the strip for
        several seconds. The rainbow is drawn in the bead colours, so the frame
        still decodes into an ordinary looking road while beads underneath are
        missing, recoloured, and reordered. Reading it invents rounds and
        re-reads old ones, so the frame is discarded and the trusted baseline
        stays untouched until the road is visible again.

        Anything half-decided is dropped with it: confirming a change takes two
        trustworthy frames in a row, and a re-sync must never be authorised by a
        picture that is known to be wrong.
        """
        first_of_event = self.trenball_obscured_since is None
        if first_of_event:
            self.trenball_obscured_since = time.monotonic()

        self.trenball_pending_decode = None
        self.trenball_reject_candidate = None

        covered_for = time.monotonic() - self.trenball_obscured_since
        self._refresh_trenball_status(
            f"Road covered — {reason}.\n"
            f"Frame ignored ({covered_for:.0f}s so far); series and baseline kept."
        )
        self.header_status.configure(text="Waiting: road covered")

    def _handle_suspicious_frame(self, image, decoded, rejection: str) -> None:
        """
        Skip a frame that cannot be reconciled, but recover if it persists.

        A one-off overlay or stray text vanishes by the next poll, so the first
        odd frame is ignored. When the same picture is still there, the screen
        really did move on, so the tracked series is re-anchored inside the new
        one and the rounds in between are appended.
        """
        self.trenball_pending_decode = None

        candidate = self.trenball_reject_candidate
        stable = candidate is not None and self._frames_are_consistent(candidate, decoded)
        if not stable:
            self.trenball_reject_candidate = decoded
            self._refresh_trenball_status(
                f"Skipped suspicious frame: {rejection}\n"
                "Tracked series kept; re-checking next poll."
            )
            self.header_status.configure(text="Skipped suspicious frame")
            return

        self.trenball_reject_candidate = None
        prev_seq = (
            self.trenball_prev_decode.flat_sequence()
            if self.trenball_prev_decode is not None
            else []
        )
        unobserved_seconds = self._seconds_since_trusted_frame()
        recovery = recover_new_beads(prev_seq, decoded.flat_sequence())
        self.trenball_prev_decode = decoded
        self._mark_frame_trusted()

        if recovery is None:
            self._refresh_trenball_status(
                f"Re-synced to the new screen ({rejection}).\n"
                "Series could not be matched, so nothing was added. "
                "Graph kept; tracking continues from here."
            )
            self.header_status.configure(text="Re-synced (no reliable diff)")
            return

        new_beads, how = recovery
        if not new_beads:
            self._refresh_trenball_status(
                f"Re-synced to the new screen ({how}). No missed rounds."
            )
            self.header_status.configure(text="Re-synced, no new rounds")
            return

        # A re-sync appends whatever follows the anchor, so a misread road can
        # hand back a long tail of rounds that the clock says cannot have been
        # played yet. Those beads are old ones read twice, not missed history.
        allowed = self._max_new_beads_over(unobserved_seconds)
        if len(new_beads) > allowed:
            self._refresh_trenball_status(
                f"Re-synced to the new screen ({rejection}).\n"
                f"Discarded {len(new_beads)} bead(s) that would need more than "
                f"{unobserved_seconds:.0f}s to play (limit {allowed}). "
                "Graph kept; tracking continues from here."
            )
            self.header_status.configure(
                text=f"Re-synced, discarded {len(new_beads)} impossible bead(s)"
            )
            return

        added = self._add_steps_from_beads(new_beads)
        self._refresh_trenball_status(
            f"Re-synced after suspicious frame ({how}).\n"
            f"+{added} recovered: {''.join(new_beads)}"
        )
        self.header_status.configure(text=f"Re-synced +{added}: {''.join(new_beads)}")

    def _frames_are_consistent(self, earlier, later) -> bool:
        """True when `later` is the same screen as `earlier`, or a growth of it."""
        if earlier.signature() == later.signature():
            return True
        follow = diff_grids(earlier, later)
        if follow.detail == UNALIGNED_DETAIL:
            return False
        return len(follow.new_beads) <= self._max_new_beads_for_poll()

    def _candidate_is_confirmed(self, pending, current) -> bool:
        """
        Require a changed frame to survive one more poll. A real new round
        remains present (or gains more rounds); temporary letters disappear.
        """
        if pending.signature() == current.signature():
            return True
        follow = diff_grids(pending, current)
        return (
            self._transition_rejection_reason(pending, current, follow) is None
            and follow.detail != UNALIGNED_DETAIL
        )

    def _trenball_poll_once(self) -> None:
        if self.trenball_region is None:
            return

        if is_workstation_locked():
            self.trenball_paused_for_lock = True
            self._refresh_trenball_status("Waiting for unlock…")
            self.header_status.configure(text="Paused: locked")
            return

        try:
            image = grab_region(self.trenball_region)
            if looks_like_blank_capture(image):
                self.trenball_paused_for_lock = True
                self._refresh_trenball_status(
                    "Capture is black (locked/display off) — unlock / wake display"
                )
                self.header_status.configure(text="Paused: blank capture")
                return

            if self.trenball_paused_for_lock:
                # Session just became capturable again — re-baseline so a long lock
                # gap does not dump a huge backlog as one burst of false "new" beads.
                self.trenball_paused_for_lock = False
                self.trenball_prev_decode = None
                self.trenball_pending_decode = None
                self._refresh_trenball_status("Resumed after lock — re-baselining…")
                self.header_status.configure(text="Track resumed")


            if frame_is_obscured(image):
                self._handle_obscured_frame(image, describe_obstruction(image))
                return
            self.trenball_obscured_since = None

            decoded = decode_trend_grid(image, rows=self.trenball_grid_rows)
            diff = diff_grids(self.trenball_prev_decode, decoded)

            if diff.kind == "first":
                self.trenball_prev_decode = decoded
                self.trenball_pending_decode = None
                self._mark_frame_trusted()
                self._refresh_trenball_status(
                    f"Baseline set ({len(decoded.flat_sequence())} beads visible)"
                )
                self.header_status.configure(text="Track baseline set")
                return

            rejection = self._transition_rejection_reason(
                self.trenball_prev_decode, decoded, diff
            )
            if rejection:
                self._handle_suspicious_frame(image, decoded, rejection)
                return

            self.trenball_reject_candidate = None

            if not diff.new_beads:
                had_pending = self.trenball_pending_decode is not None
                self.trenball_pending_decode = None
                # Safe aligned scroll/no-change frames may become the new
                # baseline; a transient candidate never replaced the old one.
                self.trenball_prev_decode = decoded
                self._mark_frame_trusted()
                if had_pending:
                    self._refresh_trenball_status(
                        "Transient change disappeared — skipped. Original track kept."
                    )
                    self.header_status.configure(text="Transient frame skipped")
                else:
                    self._refresh_trenball_status("No change")
                return

            if self.trenball_pending_decode is None:
                self.trenball_pending_decode = decoded
                self._refresh_trenball_status(
                    f"Change detected ({''.join(diff.new_beads)}); "
                    "waiting one poll to confirm…"
                )
                self.header_status.configure(text="Confirming detected change")
                return

            if not self._candidate_is_confirmed(
                self.trenball_pending_decode, decoded
            ):
                # The prior candidate was likely temporary text. Keep the
                # trusted baseline and treat this frame as a fresh candidate.
                self.trenball_pending_decode = decoded
                self._refresh_trenball_status(
                    "Previous transient frame skipped; confirming current frame…"
                )
                self.header_status.configure(text="Transient frame skipped")
                return

            added = self._add_steps_from_beads(diff.new_beads)
            self.trenball_prev_decode = decoded
            self.trenball_pending_decode = None
            self._mark_frame_trusted()
            self._refresh_trenball_status(
                f"+{added} confirmed from {diff.kind}: {''.join(diff.new_beads)}"
            )
            self.header_status.configure(text=f"Tracked +{added}: {''.join(diff.new_beads)}")
        except Exception as exc:
            self._refresh_trenball_status(f"Error: {exc}")
            self.header_status.configure(text="Track error")

    # Selenium definitions intentionally replace the former screenshot tracker
    # methods above while keeping session and graph behavior unchanged.
    def _refresh_trenball_status(self, extra: str | None = None) -> None:
        state = "WebSocket collector running" if self.trenball_running else "WebSocket collector idle"
        lines = [state, self.crash_url]
        if self.last_crash_round is not None:
            lines.append(
                f"Last: {self.last_crash_round.multiplier:g}x "
                f"(game {self.last_crash_round.game_id or 'unknown'})"
            )
        if extra:
            lines.append(extra)
        self.trenball_status_label.configure(text="\n".join(lines))

    def _start_trenball_tracking(self) -> None:
        if self.trenball_running:
            return
        url = self.crash_url_var.get().strip()
        if not url.startswith(("https://", "http://")):
            messagebox.showerror("Invalid URL", "Enter a full http:// or https:// Crash URL.", parent=self)
            return

        self.auto_save_active = True
        try:
            session_path = self._ensure_auto_save_file()
        except Exception as exc:
            self.auto_save_active = False
            messagebox.showerror("Autosave failed", str(exc), parent=self)
            return

        self.crash_url = url
        self.selenium_headless = self.selenium_headless_var.get()
        self._persist_trenball_config()
        while True:
            try:
                self.collector_events.get_nowait()
            except queue.Empty:
                break
        self.collector_stop_event = threading.Event()
        collector = SeleniumCrashCollector(
            self.crash_url, self.collector_events, self.collector_stop_event,
            headless=self.selenium_headless,
        )
        self.collector_thread = threading.Thread(target=collector.run, daemon=True, name="crash-websocket")
        self.trenball_running = True
        self.start_track_button.configure(state="disabled")
        self.stop_track_button.configure(state="normal")
        self.crash_url_entry.configure(state="disabled")
        self.headless_check.configure(state="disabled")
        prevent_sleep(True)
        self.collector_thread.start()
        self._refresh_trenball_status(f"Starting Chrome...\nAutosaving to {Path(session_path).name}")
        self.header_status.configure(text="Starting WebSocket track")
        self._drain_collector_events()

    def _stop_trenball_tracking(self, silent: bool = False) -> None:
        was_running = self.trenball_running
        self.trenball_running = False
        if self.collector_stop_event is not None:
            self.collector_stop_event.set()
        if self.collector_timer_id is not None:
            try:
                self.after_cancel(self.collector_timer_id)
            except tk.TclError:
                pass
            self.collector_timer_id = None
        prevent_sleep(False)
        self.start_track_button.configure(state="normal")
        self.stop_track_button.configure(state="disabled")
        self.crash_url_entry.configure(state="normal")
        self.headless_check.configure(state="normal")
        if was_running and not silent:
            self._refresh_trenball_status("Stopped; Chrome is closing")
            self.header_status.configure(text="WebSocket track off")

    def _drain_collector_events(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, payload = self.collector_events.get_nowait()
                if kind == "round":
                    self._add_crash_round(payload)
                elif kind == "status":
                    self._refresh_trenball_status(str(payload))
                    self.header_status.configure(text=str(payload).splitlines()[0][:60])
                elif kind == "stopped" and self.trenball_running:
                    self._stop_trenball_tracking(silent=True)
        except queue.Empty:
            pass
        if self.trenball_running:
            self.collector_timer_id = self.after(100, self._drain_collector_events)

    def _add_crash_round(self, result: CrashRound) -> None:
        self.last_crash_round = result
        if result.multiplier >= 10:
            step, event_type = 1, "10x"
        elif result.multiplier >= 2:
            step, event_type = 1, "2x+"
        else:
            step, event_type = -1, "2x-"
        self._add_step(step, event_type)
        self._refresh_trenball_status(f"Received {result.multiplier:g}x")
        self.header_status.configure(text=f"Tracked {result.multiplier:g}x")

    def _undo(self) -> None:
        if self.step_count == 0:
            return

        self.series.pop()
        self.point_timestamps.pop()
        self.point_event_types.pop()
        self.is_dirty = True
        self.view_start = min(self.view_start, self._max_view_start())
        if not self._autosave_session():
            self._refresh_ui()

    @property
    def step_count(self) -> int:
        return len(self.series) - 1

    def _refresh_ui(self, keep_scale_position: bool = False) -> None:
        self._update_metrics()
        self._update_10x_counts()
        self._update_session_status()
        self._update_mode_buttons(self.layout_buttons, self.layout_mode)
        self._update_mode_buttons(self.window_buttons, self.window_size)
        self._sync_position_scale(keep_scale_position=keep_scale_position)
        self._update_summary_text()
        self._draw_charts()

        at_limit = self.step_count >= self.MAX_STEPS
        self.plus_button.configure(state="disabled" if at_limit else "normal")
        self.minus_button.configure(state="disabled" if at_limit else "normal")
        self.ten_x_button.configure(state="disabled" if at_limit else "normal")
        self.undo_button.configure(state="normal" if self.step_count else "disabled")

    def _update_mode_buttons(self, button_map: dict, active_key) -> None:
        for key, button in button_map.items():
            is_active = key == active_key
            button.configure(
                bg="#5d86ff" if is_active else "#13233d",
                fg="#eef4ff" if is_active else "#9bb0cd",
                activebackground="#5d86ff" if is_active else "#1b2c48",
            )

    def _sync_position_scale(self, keep_scale_position: bool) -> None:
        max_start = self._max_view_start()
        if self.auto_follow.get():
            self.view_start = max_start
        else:
            self.view_start = max(0, min(self.view_start, max_start))

        self.position_scale.configure(to=max_start)
        self.position_scale.configure(state="normal" if max_start > 0 else "disabled")
        if not keep_scale_position or int(self.position_scale.get()) != self.view_start:
            self.position_scale.set(self.view_start)

    def _window_points(self) -> int:
        if self.window_size is None:
            return len(self.series)
        return min(self.window_size, len(self.series))

    def _max_view_start(self) -> int:
        return max(0, len(self.series) - self._window_points())

    def _visible_range(self) -> tuple[int, int]:
        start = min(self.view_start, self._max_view_start())
        end = start + self._window_points() - 1
        return start, end

    def _update_metrics(self) -> None:
        plus_count, minus_count = self._move_counts()
        current = self.series[-1]
        highest = max(self.series)
        lowest = min(self.series)

        self.metric_values["steps"].configure(text=f"{self.step_count:,}")
        current_label = self.metric_values["current"]
        current_label.configure(
            text=f"{current:+d}",
            fg="#2dd881" if current > 0 else "#ff7a84" if current < 0 else "#eef4ff",
        )
        self.metric_values["highest"].configure(text=f"{highest:+d}", fg="#eef4ff")
        self.metric_values["lowest"].configure(text=f"{lowest:+d}", fg="#eef4ff")
        self.metric_values["up_count"].configure(text=f"{plus_count:,}")
        self.metric_values["down_count"].configure(text=f"{minus_count:,}")

    def _update_session_status(self) -> None:
        started_at = self._format_timestamp(self.point_timestamps[0])
        last_move_at = self._format_timestamp(self.point_timestamps[-1])
        saved_at = self._format_timestamp(self.last_saved_at)
        file_name = Path(self.current_file_path).name if self.current_file_path else "No file yet"

        if self.auto_save_active and self.current_file_path and not self.is_dirty:
            state_text = "Auto-saved"
        elif self.current_file_path and not self.is_dirty:
            state_text = "Saved"
        elif self.current_file_path and self.is_dirty:
            state_text = "Unsaved changes"
        else:
            state_text = "New session"

        self.file_status_label.configure(
            text=(
                f"{state_text} | File: {file_name}\n"
                f"Started: {started_at}\n"
                f"Last move: {last_move_at} | Last saved: {saved_at}"
            )
        )

    def _update_10x_counts(self) -> None:
        for limit, label in self.ten_x_count_labels.items():
            count = self._count_recent_10x_events(limit)
            label.configure(text=f"10X count / recent {limit}: {count:,}")

    def _update_summary_text(self) -> None:
        start, end = self._visible_range()
        visible_values = self.series[start : end + 1]
        visible_delta = visible_values[-1] - visible_values[0]
        visible_high = max(visible_values)
        visible_low = min(visible_values)
        plus_count, minus_count = self._move_counts()
        current_streak, streak_sign = self._current_streak()
        longest_streak, longest_sign = self._longest_streak()
        visible_10x_count = sum(1 for index in range(max(1, start), end + 1) if self._is_10x_event(index))

        window_name = "All history" if self.window_size is None else f"{self.window_size} points"
        self.header_status.configure(text=f"{window_name} | {self.series[-1]:+d}")

        self.range_label.configure(
            text=(
                f"Viewing steps {start:,} to {end:,} of {self.step_count:,} | "
                f"Visible change {visible_delta:+d} | Visible range {visible_low:+d} to {visible_high:+d}"
            )
        )

        if self.window_size is None:
            self.position_label.configure(text="Full history")
        else:
            self.position_label.configure(text=f"Start {start:,}")

        follow_text = (
            "Following latest moves."
            if self.auto_follow.get()
            else "Manual view. Drag the slider to inspect earlier sections, or drag across the overview to select an area to watch."
        )
        self.footer_note.configure(
            text=(
                f"{follow_text} Use Left or Down for 2X-, Right or Up for 2X+, and Backspace for undo. "
                f"Use the 10X button to record a marked 2X+ point. "
                f"Use Ctrl+S to save and Ctrl+O to load. Hover the main chart to inspect a point. "
                f"The overview chart always keeps the whole 10,000-step path visible."
            )
        )

        move_total = max(1, plus_count + minus_count)
        positive_ratio = plus_count / move_total
        visible_word = "up" if visible_delta > 0 else "down" if visible_delta < 0 else "flat"
        streak_word = "up" if streak_sign > 0 else "down" if streak_sign < 0 else "flat"
        longest_word = "up" if longest_sign > 0 else "down" if longest_sign < 0 else "flat"

        self.insight_values[0].configure(
            text=(
                f"Balance: {plus_count:,} up vs {minus_count:,} down. Positive rate is {positive_ratio:.1%}. "
                f"Total 10X events: {self._count_recent_10x_events(self.step_count):,}."
            )
        )
        self.insight_values[1].configure(
            text=f"Streaks: current {streak_word} streak is {current_streak}. Longest {longest_word} streak is {longest_streak}."
        )
        self.insight_values[2].configure(
            text=(
                f"Visible window: {visible_word} {visible_delta:+d}. Local high is {visible_high:+d} and local low is {visible_low:+d}. "
                f"Visible 10X points: {visible_10x_count}. "
                f"Last move time is {self._format_timestamp(self.point_timestamps[-1])}."
            )
        )

    def _draw_charts(self) -> None:
        self._draw_detail_chart()
        self._draw_overview_chart()

    def _draw_detail_chart(self) -> None:
        canvas = self.detail_canvas
        canvas.delete("all")
        self.detail_plot_state = None

        width = max(canvas.winfo_width(), 400)
        height = max(canvas.winfo_height(), self.DETAIL_HEIGHT)
        left = 72
        right = width - 24
        top = 22
        bottom = height - 48
        usable_width = max(1, right - left)
        usable_height = max(1, bottom - top)

        canvas.create_rectangle(0, 0, width, height, fill="#0a1526", outline="")
        canvas.create_rectangle(left, top, right, bottom, outline="#1b2c48", width=1)

        start, end = self._visible_range()
        values = self.series[start : end + 1]
        min_value = min(values)
        max_value = max(values)
        span = max(2, max_value - min_value)
        pad = max(1, math.ceil(span * 0.1))
        chart_min = min_value - pad
        chart_max = max_value + pad
        chart_span = max(1, chart_max - chart_min)

        def map_x(local_index: int) -> float:
            if len(values) == 1:
                return left + usable_width / 2
            return left + usable_width * (local_index / (len(values) - 1))

        def map_y(value: int) -> float:
            return bottom - (value - chart_min) / chart_span * usable_height

        self.detail_plot_state = {
            "left": left,
            "right": right,
            "top": top,
            "bottom": bottom,
            "start": start,
            "values": values,
            "chart_min": chart_min,
            "chart_span": chart_span,
            "usable_width": usable_width,
            "usable_height": usable_height,
        }

        ticks = self._y_ticks(chart_min, chart_max)
        for tick in ticks:
            y = map_y(tick)
            canvas.create_line(left, y, right, y, fill="#18304f", dash=(3, 5))
            canvas.create_text(
                left - 12,
                y,
                text=f"{tick:+d}",
                fill="#9bb0cd",
                font=("Segoe UI", 10),
                anchor="e",
            )

        if chart_min <= 0 <= chart_max:
            zero_y = map_y(0)
            canvas.create_line(left, zero_y, right, zero_y, fill="#5278b2", width=1)

        x_step = self._x_tick_step(len(values))
        for local_index in range(0, len(values), x_step):
            x = map_x(local_index)
            canvas.create_line(x, top, x, bottom, fill="#12233d")
            canvas.create_text(
                x,
                bottom + 20,
                text=str(start + local_index),
                fill="#7389a8",
                font=("Segoe UI", 9),
            )
        if len(values) > 1:
            canvas.create_text(
                right,
                bottom + 20,
                text=str(end),
                fill="#7389a8",
                font=("Segoe UI", 9),
                anchor="e",
            )

        points = []
        for local_index, value in enumerate(values):
            points.extend((map_x(local_index), map_y(value)))

        if len(points) >= 4:
            canvas.create_line(points, fill="#17345a", width=6, capstyle=tk.ROUND, joinstyle=tk.ROUND)
            canvas.create_line(points, fill="#66b2ff", width=3, capstyle=tk.ROUND, joinstyle=tk.ROUND)

        for local_index, value in enumerate(values):
            x = map_x(local_index)
            y = map_y(value)
            if len(values) <= 80:
                fill = "#2dd881" if value >= 0 else "#ff7a84"
                canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=fill, outline="", width=1)

        last_x = map_x(len(values) - 1)
        last_y = map_y(values[-1])
        canvas.create_oval(last_x - 7, last_y - 7, last_x + 7, last_y + 7, fill="#ffffff", outline="#66b2ff", width=2)
        canvas.create_text(
            min(right - 4, last_x + 56),
            max(top + 14, last_y - 18),
            text=f"{values[-1]:+d}",
            fill="#eef4ff",
            font=("Segoe UI", 10, "bold"),
            anchor="e",
        )

        max_index = values.index(max(values))
        min_index = values.index(min(values))
        self._tag_point(canvas, map_x(max_index), map_y(max(values)), f"Peak {max(values):+d}", "#25b56a", top, right)
        self._tag_point(canvas, map_x(min_index), map_y(min(values)), f"Low {min(values):+d}", "#f45b69", top, right)

    def _draw_overview_chart(self) -> None:
        canvas = self.overview_canvas
        canvas.delete("all")

        width = max(canvas.winfo_width(), 400)
        height = max(canvas.winfo_height(), self.OVERVIEW_HEIGHT)
        left = 16
        right = width - 16
        top = 16
        bottom = height - 18
        usable_width = max(1, right - left)
        usable_height = max(1, bottom - top)

        canvas.create_rectangle(0, 0, width, height, fill="#0c182a", outline="")
        canvas.create_rectangle(left, top, right, bottom, outline="#1b2c48", width=1)

        values = self.series
        min_value = min(values)
        max_value = max(values)
        span = max(2, max_value - min_value)
        pad = max(1, math.ceil(span * 0.08))
        chart_min = min_value - pad
        chart_max = max_value + pad
        chart_span = max(1, chart_max - chart_min)

        def map_x(index: int) -> float:
            if len(values) == 1:
                return left + usable_width / 2
            return left + usable_width * (index / (len(values) - 1))

        def map_y(value: int) -> float:
            return bottom - (value - chart_min) / chart_span * usable_height

        if chart_min <= 0 <= chart_max:
            canvas.create_line(left, map_y(0), right, map_y(0), fill="#27456f")

        points = []
        for index, value in enumerate(values):
            points.extend((map_x(index), map_y(value)))
        if len(points) >= 4:
            canvas.create_line(points, fill="#6faeff", width=2, capstyle=tk.ROUND, joinstyle=tk.ROUND)

        start, end = self._visible_range()
        start_x = map_x(start)
        end_x = map_x(end)
        if start == end:
            end_x = start_x + 2

        canvas.create_rectangle(
            start_x,
            top,
            end_x,
            bottom,
            outline="#dbe7ff",
            width=2,
            fill="#5d86ff",
            stipple="gray25",
        )

        if self.overview_drag_start_x is not None and self.overview_drag_current_x is not None:
            drag_left = min(self.overview_drag_start_x, self.overview_drag_current_x)
            drag_right = max(self.overview_drag_start_x, self.overview_drag_current_x)
            if abs(drag_right - drag_left) >= 2:
                canvas.create_rectangle(
                    drag_left,
                    top + 4,
                    drag_right,
                    bottom - 4,
                    outline="#f2c94c",
                    width=2,
                    dash=(4, 3),
                    fill="#f2c94c",
                    stipple="gray25",
                )

                selected_points = abs(
                    self._overview_index_from_x(drag_right) - self._overview_index_from_x(drag_left)
                ) + 1
                label = f"Watch {selected_points:,} pts"
                text_x = min(right - 10, max(left + 10, (drag_left + drag_right) / 2))
                text_y = top - 4
                text_id = canvas.create_text(
                    text_x,
                    text_y,
                    text=label,
                    fill="#f2c94c",
                    font=("Segoe UI", 9, "bold"),
                    anchor="s",
                )
                box = canvas.bbox(text_id)
                if box:
                    pad = 5
                    canvas.create_rectangle(
                        box[0] - pad,
                        box[1] - pad,
                        box[2] + pad,
                        box[3] + pad,
                        fill="#13233d",
                        outline="#f2c94c",
                        width=1,
                    )
                    canvas.tag_raise(text_id)

    def _tag_point(self, canvas: tk.Canvas, x: float, y: float, text: str, accent: str, top: int, right: int) -> None:
        box_width = 82
        box_height = 22
        box_x = min(max(x + 10, 86), right - box_width - 6)
        box_y = max(top + 6, y - box_height - 8)
        canvas.create_rectangle(box_x, box_y, box_x + box_width, box_y + box_height, fill="#13233d", outline=accent, width=1)
        canvas.create_text(
            box_x + box_width / 2,
            box_y + box_height / 2,
            text=text,
            fill="#eef4ff",
            font=("Segoe UI", 9, "bold"),
        )

    def _on_detail_hover(self, event: tk.Event) -> None:
        state = self.detail_plot_state
        if not state:
            self._clear_detail_hover()
            return

        if not (state["left"] <= event.x <= state["right"] and state["top"] <= event.y <= state["bottom"]):
            self._clear_detail_hover()
            return

        values = state["values"]
        if not values:
            self._clear_detail_hover()
            return

        if len(values) == 1:
            local_index = 0
            point_x = state["left"] + state["usable_width"] / 2
        else:
            ratio = (event.x - state["left"]) / max(1, state["right"] - state["left"])
            local_index = int(round(ratio * (len(values) - 1)))
            local_index = max(0, min(len(values) - 1, local_index))
            point_x = state["left"] + state["usable_width"] * (local_index / (len(values) - 1))

        value = values[local_index]
        point_y = state["bottom"] - (value - state["chart_min"]) / state["chart_span"] * state["usable_height"]
        global_index = state["start"] + local_index
        timestamp = self._format_timestamp(self.point_timestamps[global_index])
        event_label = self._event_label(global_index)
        delta_text = ""
        if global_index > 0:
            delta = self.series[global_index] - self.series[global_index - 1]
            delta_text = f" | move {delta:+d}"

        self._clear_detail_hover()
        canvas = self.detail_canvas
        canvas.create_line(point_x, state["top"], point_x, state["bottom"], fill="#f2c94c", dash=(4, 4), tags="hover")
        canvas.create_oval(point_x - 5, point_y - 5, point_x + 5, point_y + 5, fill="#ffffff", outline="#f2c94c", width=2, tags="hover")

        event_text = f" | {event_label}" if event_label else ""
        text = f"step {global_index:,}{event_text} | value {value:+d}{delta_text}\n{timestamp}"
        text_x = point_x + 12 if point_x < (state["left"] + state["right"]) / 2 else point_x - 12
        anchor = "nw" if point_x < (state["left"] + state["right"]) / 2 else "ne"
        text_id = canvas.create_text(
            text_x,
            max(state["top"] + 10, point_y - 8),
            text=text,
            fill="#eef4ff",
            font=("Segoe UI", 9, "bold"),
            anchor=anchor,
            justify="left",
            tags="hover",
        )
        box = canvas.bbox(text_id)
        if box:
            pad = 8
            canvas.create_rectangle(
                box[0] - pad,
                box[1] - pad,
                box[2] + pad,
                box[3] + pad,
                fill="#13233d",
                outline="#f2c94c",
                width=1,
                tags="hover",
            )
            canvas.tag_raise(text_id)

    def _clear_detail_hover(self) -> None:
        self.detail_canvas.delete("hover")

    def _event_label(self, index: int) -> str:
        if not (0 <= index < len(self.point_event_types)):
            return ""
        labels = {
            "start": "Start",
            "2x+": "2X+",
            "2x-": "2X-",
            "10x": "10X",
        }
        return labels.get(self.point_event_types[index], "")

    def _is_10x_event(self, index: int) -> bool:
        return 0 < index < len(self.point_event_types) and self.point_event_types[index] == "10x"

    def _count_recent_10x_events(self, limit: int) -> int:
        if limit <= 0 or self.step_count <= 0:
            return 0

        start_index = max(1, len(self.point_event_types) - limit)
        count = 0
        for index in range(start_index, len(self.point_event_types)):
            if self.point_event_types[index] == "10x":
                count += 1
        return count

    def _move_counts(self) -> tuple[int, int]:
        plus_count = 0
        minus_count = 0
        for index in range(1, len(self.series)):
            delta = self.series[index] - self.series[index - 1]
            if delta > 0:
                plus_count += 1
            elif delta < 0:
                minus_count += 1
        return plus_count, minus_count

    def _current_streak(self) -> tuple[int, int]:
        if len(self.series) < 2:
            return 0, 0

        last_delta = self.series[-1] - self.series[-2]
        if last_delta == 0:
            return 0, 0

        streak = 1
        for index in range(len(self.series) - 2, 0, -1):
            delta = self.series[index] - self.series[index - 1]
            if delta == last_delta:
                streak += 1
            else:
                break
        return streak, 1 if last_delta > 0 else -1

    def _longest_streak(self) -> tuple[int, int]:
        if len(self.series) < 2:
            return 0, 0

        longest = 1
        longest_sign = 1 if self.series[1] - self.series[0] > 0 else -1
        current = 1
        previous_sign = 1 if self.series[1] - self.series[0] > 0 else -1

        for index in range(2, len(self.series)):
            delta = self.series[index] - self.series[index - 1]
            sign = 1 if delta > 0 else -1
            if sign == previous_sign:
                current += 1
            else:
                current = 1
                previous_sign = sign
            if current > longest:
                longest = current
                longest_sign = sign
        return longest, longest_sign

    def _y_ticks(self, minimum: int, maximum: int) -> list[int]:
        span = max(1, maximum - minimum)
        rough_step = max(1, math.ceil(span / 4))
        start = math.floor(minimum / rough_step) * rough_step
        end = math.ceil(maximum / rough_step) * rough_step
        ticks = list(range(start, end + rough_step, rough_step))
        if 0 not in ticks and minimum < 0 < maximum:
            ticks.append(0)
        ticks = sorted(set(ticks))
        if len(ticks) > 7:
            step = max(1, math.ceil(len(ticks) / 7))
            ticks = ticks[::step]
            if ticks[-1] != end:
                ticks.append(end)
        return ticks

    def _x_tick_step(self, point_count: int) -> int:
        if point_count <= 30:
            return 5
        if point_count <= 80:
            return 10
        if point_count <= 180:
            return 20
        if point_count <= 360:
            return 40
        if point_count <= 750:
            return 80
        return 120


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--session")
    args, _unknown = parser.parse_known_args()
    app = PlusMinusGraphApp(initial_session=args.session)
    app.mainloop()
