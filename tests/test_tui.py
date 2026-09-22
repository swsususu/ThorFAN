"""Tests for the live display's pure logic.

Rendering needs a terminal, so what is tested here is everything that decides
*what* to draw: bar scaling, colour thresholds, keystroke handling, and the
fallback when no daemon is running. The drawing calls themselves are exercised
against a fake window to catch layout errors that would crash at runtime.
"""

from __future__ import annotations

import curses
import time

import pytest

from thorfan.core.hwmon import PWM_MAX
from thorfan.core.policy import STOCK_RPM_CAP
from thorfan.tui import (
    CHART_ROWS,
    FAN_MAX_RPM,
    TEMP_HOT_C,
    TEMP_MAX_C,
    TEMP_MIN_C,
    TEMP_WARN_C,
    Display,
    History,
    Snapshot,
    braille_rows,
)


# ---- bar scaling ------------------------------------------------------


def test_bar_endpoints():
    assert Display._bar(0, 100, 20) == (0, 20)
    assert Display._bar(100, 100, 20) == (20, 0)
    assert Display._bar(50, 100, 20) == (10, 10)


def test_bar_clamps_out_of_range():
    assert Display._bar(-10, 100, 20) == (0, 20)
    assert Display._bar(500, 100, 20) == (20, 0)


def test_bar_handles_degenerate_sizes():
    """A zero width or maximum must not divide by zero or go negative."""
    assert Display._bar(5, 0, 20) == (0, 20)
    assert Display._bar(5, 100, 0) == (0, 0)


def test_stock_cap_marker_lands_below_halfway():
    """The point of the marker: the vendor profile stops at ~40% of the fan."""
    width = 50
    marker, _ = Display._bar(STOCK_RPM_CAP, FAN_MAX_RPM, width)
    assert 0 < marker < width // 2


# ---- colour thresholds -----------------------------------------------


class FakeWindow:
    """Records writes and reports a fixed size."""

    def __init__(self, height: int = 40, width: int = 100) -> None:
        self._height = height
        self._width = width
        self.writes: list[tuple[int, int, str]] = []
        self.keys: list[int] = []

    def getmaxyx(self):
        return self._height, self._width

    def addnstr(self, y, x, text, n, attr=0):
        if y >= self._height or x >= self._width:
            raise curses.error("out of bounds")
        self.writes.append((y, x, text[:n]))

    def erase(self):
        self.writes.clear()

    def refresh(self):
        pass

    def getch(self):
        return self.keys.pop(0) if self.keys else -1

    def nodelay(self, flag):
        pass

    def text(self) -> str:
        return "\n".join(t for _, _, t in self.writes)


def _display(window: FakeWindow | None = None) -> Display:
    display = Display(window or FakeWindow(), reader=None)  # type: ignore[arg-type]
    display._colour = False  # avoid curses colour setup in tests
    return display


def test_temp_thresholds_are_ordered():
    """The bands must be ordered and inside the bar's range to be meaningful."""
    assert TEMP_MIN_C < TEMP_WARN_C < TEMP_HOT_C < TEMP_MAX_C


def test_hot_temperatures_are_emphasised_even_without_colour():
    """Monochrome terminals still need to distinguish a dangerous reading."""
    d = _display()
    assert d._temp_attr(50.0) == 0
    assert d._temp_attr(TEMP_HOT_C) & curses.A_BOLD


def test_hot_threshold_matches_the_interlock():
    """The display should turn red exactly when the override would engage."""
    from thorfan.core.policy import EMERGENCY_TEMP_C

    assert TEMP_HOT_C == EMERGENCY_TEMP_C


# ---- rendering does not crash ----------------------------------------


def _snapshot(**overrides) -> Snapshot:
    base = dict(
        zones=[
            {"name": "tj-thermal", "temp_c": 85.0, "critical_c": 114.5},
            {"name": "gpu-thermal", "temp_c": None, "critical_c": 114.5},
        ],
        rails=[
            {"name": "VDD_GPU", "watts": 14.68},
            {"name": "VIN_SYS_5V0", "watts": None},
        ],
        throttles=[
            {"name": "gpu-throttle-alert", "cur_state": 1, "max_state": 2,
             "is_alert": True},
            {"name": "cpu-throttle-alert", "cur_state": 0, "max_state": 2,
             "is_alert": True},
            {"name": "devfreq-gpu-gpc-0", "cur_state": 18, "max_state": 169,
             "is_alert": False},
        ],
        fan={"pwm": 120, "rpm": 6500, "enabled": True},
        mode="curve",
        emergency=False,
        vendor_active=False,
        daemon=True,
    )
    base.update(overrides)
    return Snapshot(**base)


def test_full_render_produces_output():
    window = FakeWindow()
    d = _display(window)
    snap = _snapshot()
    y = d._draw_header(snap, 100)
    y = d._draw_fan(y, snap, 100)
    y = d._draw_temps(y, snap, 100)
    y = d._draw_power(y, snap, 100)
    y = d._draw_throttles(y, snap, 100)
    d._draw_footer(y, snap, 100)

    text = window.text()
    assert "ThorFAN" in text
    assert "curve" in text
    assert "VDD_GPU" in text
    assert "stock cap" in text


def test_render_survives_a_tiny_terminal():
    """A narrow window must clip, not raise."""
    window = FakeWindow(height=6, width=20)
    d = _display(window)
    snap = _snapshot()
    y = d._draw_header(snap, 20)
    y = d._draw_fan(y, snap, 20)
    y = d._draw_temps(y, snap, 20)
    y = d._draw_power(y, snap, 20)
    y = d._draw_throttles(y, snap, 20)
    d._draw_footer(y, snap, 20)  # must not raise


def test_unreadable_zone_is_labelled_not_zeroed():
    window = FakeWindow()
    d = _display(window)
    d._draw_temps(0, _snapshot(), 100)
    assert "power-gated" in window.text()
    assert "0.0 C" not in window.text()


def test_disabled_fan_output_is_called_out():
    """pwm1_enable == 0 stops the fan; a duty cycle alone would mislead."""
    window = FakeWindow()
    d = _display(window)
    d._draw_fan(0, _snapshot(fan={"pwm": 200, "rpm": 0, "enabled": False}), 100)
    assert "OUTPUT OFF" in window.text()


def test_active_throttle_is_distinguished():
    window = FakeWindow()
    d = _display(window)
    d._draw_throttles(0, _snapshot(), 100)
    text = window.text()
    assert "● gpu" in text    # active
    assert "○ cpu" in text    # idle


def test_only_active_frequency_caps_are_shown():
    window = FakeWindow()
    d = _display(window)
    snap = _snapshot(throttles=[
        {"name": "devfreq-gpu-gpc-0", "cur_state": 0, "max_state": 169,
         "is_alert": False},
    ])
    d._draw_throttles(0, snap, 100)
    assert "gpu-gpc" not in window.text()


def test_power_total_is_summed():
    window = FakeWindow()
    d = _display(window)
    snap = _snapshot(rails=[
        {"name": "A", "watts": 10.0},
        {"name": "B", "watts": 5.5},
    ])
    d._draw_power(0, snap, 100)
    assert "15.50 W" in window.text()


def test_missing_rail_does_not_break_the_total():
    window = FakeWindow()
    d = _display(window)
    d._draw_power(0, _snapshot(), 100)
    text = window.text()
    assert "unavailable" in text
    assert "14.68 W" in text


def test_read_only_header_when_no_daemon():
    window = FakeWindow()
    d = _display(window)
    d._draw_header(_snapshot(daemon=False, mode=None), 100)
    assert "read-only" in window.text()


def test_emergency_is_announced():
    window = FakeWindow()
    d = _display(window)
    d._draw_header(_snapshot(emergency=True), 100)
    assert "EMERGENCY" in window.text()


# ---- keystrokes -------------------------------------------------------


class RecordingDisplay(Display):
    """Captures requests instead of sending them."""

    def __init__(self) -> None:
        super().__init__(FakeWindow(), reader=None)  # type: ignore[arg-type]
        self._colour = False
        self.sent: list[dict] = []
        self.notices: list[str] = []

    def _send(self, request, description):
        self.sent.append(request)
        self.notices.append(description)

    def _notify(self, text, seconds=3.0):
        self.notices.append(text)


def test_q_quits():
    d = RecordingDisplay()
    assert d.handle_key(ord("q"), _snapshot()) is False


def test_escape_quits():
    d = RecordingDisplay()
    assert d.handle_key(27, _snapshot()) is False


def test_number_keys_map_to_percentages():
    d = RecordingDisplay()
    d.handle_key(ord("9"), _snapshot())
    assert d.sent == [{"action": "set", "pwm": round(0.9 * PWM_MAX)}]


def test_zero_means_full_speed():
    d = RecordingDisplay()
    d.handle_key(ord("0"), _snapshot())
    assert d.sent == [{"action": "set", "pwm": PWM_MAX}]


def test_up_arrow_steps_relative_to_current():
    d = RecordingDisplay()
    d.handle_key(curses.KEY_UP, _snapshot(fan={"pwm": 100, "rpm": 5000,
                                               "enabled": True}))
    assert d.sent == [{"action": "set", "pwm": 100 + round(0.05 * PWM_MAX)}]


def test_down_arrow_steps_down():
    d = RecordingDisplay()
    d.handle_key(curses.KEY_DOWN, _snapshot(fan={"pwm": 100, "rpm": 5000,
                                                 "enabled": True}))
    assert d.sent == [{"action": "set", "pwm": 100 - round(0.05 * PWM_MAX)}]


def test_stepping_clamps_at_the_top():
    d = RecordingDisplay()
    d.handle_key(curses.KEY_UP, _snapshot(fan={"pwm": 250, "rpm": 13000,
                                               "enabled": True}))
    assert d.sent == [{"action": "set", "pwm": PWM_MAX}]


def test_stepping_clamps_at_zero():
    d = RecordingDisplay()
    d.handle_key(curses.KEY_DOWN, _snapshot(fan={"pwm": 5, "rpm": 500,
                                                 "enabled": True}))
    assert d.sent == [{"action": "set", "pwm": 0}]


def test_stepping_without_a_reading_is_refused():
    d = RecordingDisplay()
    d.handle_key(curses.KEY_UP, _snapshot(fan={"pwm": None}))
    assert d.sent == []
    assert any("cannot read" in n for n in d.notices)


def test_mode_keys():
    d = RecordingDisplay()
    for key, mode in ((ord("c"), "curve"), (ord("m"), "manual"),
                      (ord("v"), "vendor")):
        d.sent.clear()
        d.handle_key(key, _snapshot())
        assert d.sent == [{"action": "set", "mode": mode}]


def test_unknown_key_does_nothing():
    d = RecordingDisplay()
    assert d.handle_key(ord("z"), _snapshot()) is True
    assert d.sent == []


# ---- embedded daemon --------------------------------------------------


def test_existing_daemon_is_never_displaced(monkeypatch):
    """Two control loops writing pwm1 would oscillate against each other."""
    from thorfan import tui

    monkeypatch.setattr(tui.control, "probe", lambda *a, **k: True)
    started: list[bool] = []
    monkeypatch.setattr(tui, "Daemon", lambda *a, **k: started.append(True))

    with tui.EmbeddedDaemon() as embedded:
        assert started == []
        assert "running daemon" in embedded.note


def test_unprivileged_run_stays_read_only(monkeypatch):
    from thorfan import tui

    monkeypatch.setattr(tui.control, "probe", lambda *a, **k: False)
    monkeypatch.setattr(tui.os, "geteuid", lambda: 1000)
    started: list[bool] = []
    monkeypatch.setattr(tui, "Daemon", lambda *a, **k: started.append(True))

    with tui.EmbeddedDaemon() as embedded:
        assert started == []
        assert "read-only" in embedded.note


def test_missing_hardware_is_reported_not_raised(monkeypatch):
    """A non-Jetson host should get a message, not a traceback."""
    from thorfan import tui
    from thorfan.core.hwmon import HardwareError

    monkeypatch.setattr(tui.control, "probe", lambda *a, **k: False)
    monkeypatch.setattr(tui.os, "geteuid", lambda: 0)

    def boom(*a, **k):
        raise HardwareError("No 'pwmfan' hwmon device found")

    monkeypatch.setattr(tui, "Daemon", boom)

    with tui.EmbeddedDaemon() as embedded:
        assert "no control" in embedded.note
        assert "pwmfan" in embedded.note


def test_embedded_daemon_is_stopped_on_exit(monkeypatch):
    """Exiting must release the fan, or the board is left unmanaged."""
    from thorfan import tui

    monkeypatch.setattr(tui.control, "probe", lambda *a, **k: False)
    monkeypatch.setattr(tui.os, "geteuid", lambda: 0)

    class FakeDaemon:
        def __init__(self) -> None:
            self.stopped = False
            self.signal_handlers = None

        def run(self, install_signal_handlers=True):
            self.signal_handlers = install_signal_handlers
            while not self.stopped:
                time.sleep(0.01)

        def request_stop(self):
            self.stopped = True

    fake = FakeDaemon()
    monkeypatch.setattr(tui, "Daemon", lambda *a, **k: fake)

    with tui.EmbeddedDaemon():
        pass

    assert fake.stopped is True
    # Signal handlers cannot be installed off the main thread.
    assert fake.signal_handlers is False


def test_startup_note_is_shown_in_the_footer():
    window = FakeWindow()
    d = Display(window, reader=None, startup_note="control loop started")  # type: ignore[arg-type]
    d._colour = False
    d._draw_footer(0, _snapshot(), 100)
    assert "control loop started" in window.text()


# ---- chart ------------------------------------------------------------

BLANK = chr(0x2800)


def test_braille_returns_the_requested_shape():
    rows = braille_rows([50.0] * 20, width=30, rows=4, low=0.0, high=100.0)
    assert len(rows) == 4
    assert all(len(r) == 30 for r in rows)


def test_high_values_plot_at_the_top():
    rows = braille_rows([100.0] * 10, width=10, rows=4, low=0.0, high=100.0)
    assert rows[0].strip(BLANK)      # top row has dots
    assert not rows[-1].strip(BLANK)  # bottom row is empty


def test_low_values_plot_at_the_bottom():
    rows = braille_rows([0.0] * 10, width=10, rows=4, low=0.0, high=100.0)
    assert not rows[0].strip(BLANK)
    assert rows[-1].strip(BLANK)


def test_values_are_clamped_to_the_axis():
    """An out-of-range sample must not write outside the grid."""
    rows = braille_rows([-50.0, 500.0], width=4, rows=3, low=0.0, high=100.0)
    assert len(rows) == 3
    assert rows[0].strip(BLANK)   # the 500 clamps to the top
    assert rows[-1].strip(BLANK)  # the -50 clamps to the bottom


def test_missing_samples_leave_gaps():
    """A power-gated sensor must read as a gap, not an interpolated line."""
    series = [50.0] * 4 + [None] * 8 + [50.0] * 4
    rows = braille_rows(series, width=8, rows=2, low=0.0, high=100.0)
    joined = "".join(rows)
    assert BLANK in joined


def test_newest_sample_is_at_the_right_edge():
    """Time must run left to right, with now at the right."""
    # One high sample at the end, everything else low.
    series = [0.0] * 20 + [100.0]
    rows = braille_rows(series, width=12, rows=4, low=0.0, high=100.0)
    top = rows[0]
    # The only dots in the top row must be in the final cell.
    assert top[-1] != BLANK
    assert all(c == BLANK for c in top[:-1])


def test_short_history_is_right_aligned_not_stretched():
    """Stretching two samples across the width would make the axis lie."""
    rows = braille_rows([100.0, 100.0], width=10, rows=2, low=0.0, high=100.0)
    assert rows[0][:5] == BLANK * 5
    assert rows[0][-1] != BLANK


def test_degenerate_chart_sizes_are_safe():
    assert braille_rows([1.0], width=0, rows=3, low=0, high=10) == [""] * 3
    assert braille_rows([1.0], width=5, rows=0, low=0, high=10) == []
    # An inverted or zero-width axis cannot be scaled.
    assert braille_rows([1.0], width=5, rows=2, low=10, high=10) == [""] * 2


def test_history_is_bounded():
    h = History(length=5)
    for i in range(20):
        h.add(float(i), 100)
    assert len(h) == 5
    assert h.temps == [15.0, 16.0, 17.0, 18.0, 19.0]


def test_history_converts_pwm_to_percent():
    h = History()
    h.add(60.0, PWM_MAX)
    h.add(60.0, 0)
    assert h.pwm_percent == [100.0, 0.0]


def test_history_keeps_missing_samples():
    h = History()
    h.add(None, None)
    assert h.temps == [None]
    assert h.pwm_percent == [None]


def test_record_tracks_the_hottest_zone():
    """Which zone is hottest changes with the workload."""
    d = _display()
    d._record(_snapshot(zones=[
        {"name": "cpu-thermal", "temp_c": 70.0, "critical_c": 114.5},
        {"name": "gpu-thermal", "temp_c": 95.0, "critical_c": 114.5},
        {"name": "tj-thermal", "temp_c": None, "critical_c": 114.5},
    ]))
    assert d._history.temps == [95.0]


def test_record_names_the_plotted_zone():
    """The chart follows the hottest zone, so it must say which that is."""
    d = _display()
    d._record(_snapshot(zones=[
        {"name": "cpu-thermal", "temp_c": 70.0, "critical_c": 114.5},
        {"name": "gpu-thermal", "temp_c": 95.0, "critical_c": 114.5},
    ]))
    assert d._history.source == "gpu-thermal"


def test_plotted_zone_can_change_between_samples():
    d = _display()
    d._record(_snapshot(zones=[
        {"name": "cpu-thermal", "temp_c": 90.0, "critical_c": 114.5},
        {"name": "gpu-thermal", "temp_c": 70.0, "critical_c": 114.5},
    ]))
    assert d._history.source == "cpu-thermal"
    d._record(_snapshot(zones=[
        {"name": "cpu-thermal", "temp_c": 70.0, "critical_c": 114.5},
        {"name": "gpu-thermal", "temp_c": 90.0, "critical_c": 114.5},
    ]))
    assert d._history.source == "gpu-thermal"


def test_chart_legend_names_the_zone():
    window = FakeWindow()
    d = _display(window)
    for _ in range(5):
        d._record(_snapshot(zones=[
            {"name": "tj-thermal", "temp_c": 85.0, "critical_c": 114.5},
        ]))
    d._draw_chart(0, _snapshot(), 100)
    assert "temp tj" in window.text()


def test_record_handles_a_fully_unreadable_board():
    d = _display()
    d._record(_snapshot(zones=[
        {"name": "gpu-thermal", "temp_c": None, "critical_c": 114.5},
    ], fan={"pwm": None}))
    assert d._history.temps == [None]


def test_chart_waits_for_enough_samples():
    window = FakeWindow()
    d = _display(window)
    d._record(_snapshot())
    d._draw_chart(0, _snapshot(), 100)
    assert "collecting" in window.text()


def test_chart_draws_once_history_exists():
    window = FakeWindow()
    d = _display(window)
    for _ in range(5):
        d._record(_snapshot())
    d._draw_chart(0, _snapshot(), 100)
    text = window.text()
    assert "collecting" not in text
    assert "temp" in text and "duty" in text


def test_chart_can_be_toggled():
    d = RecordingDisplay()
    assert d._show_chart is True
    d.handle_key(ord("g"), _snapshot())
    assert d._show_chart is False
    d.handle_key(ord("g"), _snapshot())
    assert d._show_chart is True
