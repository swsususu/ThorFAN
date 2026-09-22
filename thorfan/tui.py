"""Live terminal display with interactive fan control.

Uses curses from the standard library rather than a rendering framework: the
core of this tool has no third-party dependencies, and a live display is a
core feature, not an optional extra. Drawing the bars by hand also makes it
possible to mark the stock 5371 RPM ceiling, which is the whole point of the
project.

Control requires a control loop, because the 100 C interlock lives there. Run
as root with no daemon present, this starts one in a background thread for the
lifetime of the display, so `sudo thorfan tui` is a single self-contained
command. An already-running daemon is used as-is rather than displaced, and an
unprivileged run without socket access falls back to a read-only display.
"""

from __future__ import annotations

import curses
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .core import control, vendor
from .core.config import Config
from .core.daemon import Daemon
from .core.hwmon import (
    PWM_MAX,
    FanDevice,
    HardwareError,
    discover_rails,
    discover_throttles,
    discover_zones,
)
from .core.policy import STOCK_RPM_CAP, Mode

REFRESH_S = 1.0

# Measured at pwm 255 on an AGX Thor dev kit; used to scale the rpm bar so the
# stock ceiling sits where it actually falls.
FAN_MAX_RPM = 13650

# Temperature bar range. Starting at 30 C rather than 0 spends the width on the
# part of the range that varies.
TEMP_MIN_C = 30.0
TEMP_MAX_C = 115.0

# Colour thresholds, chosen from the board's own limits: the GPU starts
# throttling near 109 C and the emergency override engages at 100 C.
TEMP_WARN_C = 85.0
TEMP_HOT_C = 100.0

_STEP_PERCENT = 5

# Braille gives four vertical sub-positions per character cell, which is what
# makes a readable chart possible in a few terminal rows. Bit layout of a
# braille cell, as offsets from U+2800:
#     1 8       left column top to bottom: 1 2 4 64
#     2 16      right column top to bottom: 8 16 32 128
#     4 32
#    64 128
_BRAILLE_BASE = 0x2800
_BRAILLE_DOTS = (
    (0x01, 0x08),   # row 0: top
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),   # row 3: bottom
)

# Enough history for a few minutes at the default refresh, bounded so a display
# left open overnight does not grow without limit.
HISTORY_LEN = 600

CHART_ROWS = 6


class History:
    """A bounded series of samples for the chart.

    Stores None for missing samples rather than skipping them, so a gap in a
    power-gated sensor stays visible as a gap instead of being papered over by
    interpolation between distant points.
    """

    def __init__(self, length: int = HISTORY_LEN) -> None:
        self._temps: deque[float | None] = deque(maxlen=length)
        self._pwm: deque[float | None] = deque(maxlen=length)
        self._source: str | None = None

    def add(self, temp_c: float | None, pwm: int | None,
            source: str | None = None) -> None:
        self._temps.append(temp_c)
        self._pwm.append(None if pwm is None else pwm / PWM_MAX * 100.0)
        self._source = source

    @property
    def source(self) -> str | None:
        """Which zone the most recent temperature sample came from.

        Worth surfacing: the chart follows whichever zone is hottest, so a
        label is the only way to know what the curve represents.
        """
        return self._source

    @property
    def temps(self) -> list[float | None]:
        return list(self._temps)

    @property
    def pwm_percent(self) -> list[float | None]:
        return list(self._pwm)

    def __len__(self) -> int:
        return len(self._temps)


def braille_rows(series: list[float | None], width: int, rows: int,
                 low: float, high: float) -> list[str]:
    """Plot a series as braille text, newest sample at the right edge.

    Returns `rows` strings of at most `width` characters. Each cell holds two
    columns and four rows of dots, so the effective resolution is 2*width by
    4*rows. Missing samples leave their column blank.
    """
    if width <= 0 or rows <= 0 or high <= low:
        return [""] * max(0, rows)

    columns = width * 2
    # Show the most recent samples; older ones scroll off the left.
    tail = series[-columns:]
    # Right-align so a partly filled history grows from the left instead of
    # stretching, which would make the time axis lie.
    tail = [None] * (columns - len(tail)) + tail

    height = rows * 4
    cells = [[0] * width for _ in range(rows)]

    for index, value in enumerate(tail):
        if value is None:
            continue
        ratio = (value - low) / (high - low)
        ratio = min(1.0, max(0.0, ratio))
        # Dot 0 is the top of the chart, so invert: a high value is a low index.
        level = int(round((1.0 - ratio) * (height - 1)))
        cell_row, dot_row = divmod(level, 4)
        cell_col, dot_col = divmod(index, 2)
        cells[cell_row][cell_col] |= _BRAILLE_DOTS[dot_row][dot_col]

    return ["".join(chr(_BRAILLE_BASE + bits) for bits in row)
            for row in cells]


@dataclass
class Snapshot:
    """One frame of data, however it was obtained."""

    zones: list[dict] = field(default_factory=list)
    rails: list[dict] = field(default_factory=list)
    throttles: list[dict] = field(default_factory=list)
    fan: dict = field(default_factory=dict)
    mode: str | None = None
    emergency: bool = False
    vendor_active: bool = False
    daemon: bool = False
    error: str | None = None


class Reader:
    """Provides snapshots from the daemon, falling back to direct sysfs reads.

    The daemon is preferred because it knows the mode and the interlock state,
    which sysfs cannot tell us. Direct reads keep the display useful when no
    daemon is running.
    """

    # systemctl costs about 6 ms, far too much per frame, and the vendor
    # daemon's state does not change on its own.
    _VENDOR_CHECK_S = 5.0

    def __init__(self) -> None:
        self._fan: FanDevice | None = None
        self._zones: list | None = None
        self._rails: list | None = None
        self._throttles: list | None = None
        self._vendor_active = False
        self._vendor_checked = 0.0

    def _vendor_state(self) -> bool:
        now = time.monotonic()
        if now - self._vendor_checked >= self._VENDOR_CHECK_S:
            self._vendor_active = vendor.is_active()
            self._vendor_checked = now
        return self._vendor_active

    def snapshot(self) -> Snapshot:
        try:
            reply = control.send({"action": "telemetry"})
        except control.ControlError:
            return self._direct()

        return Snapshot(
            zones=reply.get("zones", []),
            rails=reply.get("rails", []),
            throttles=reply.get("throttles", []),
            fan=reply.get("fan", {}),
            mode=reply.get("mode"),
            emergency=bool(reply.get("emergency")),
            vendor_active=bool(reply.get("vendor_active")),
            daemon=True,
        )

    def _direct(self) -> Snapshot:
        """Read sysfs ourselves. No mode or interlock state is available."""
        try:
            if self._fan is None:
                self._fan = FanDevice()
                self._zones = discover_zones()
                self._rails = discover_rails()
                self._throttles = discover_throttles()
        except HardwareError as exc:
            return Snapshot(error=str(exc))

        assert self._zones is not None
        assert self._rails is not None
        assert self._throttles is not None

        def safe(read):
            try:
                return read()
            except Exception:
                return None

        return Snapshot(
            zones=[
                {"name": z.name, "temp_c": z.try_temp_c(),
                 "critical_c": z.critical_c}
                for z in self._zones
            ],
            rails=[{"name": r.name, "watts": r.watts()} for r in self._rails],
            throttles=[
                {"name": t.name, "cur_state": t.cur_state,
                 "max_state": t.max_state, "is_alert": t.is_alert}
                for t in self._throttles
            ],
            fan={
                "pwm": safe(lambda: self._fan.pwm),
                "rpm": safe(lambda: self._fan.rpm),
                "enabled": safe(lambda: self._fan.enabled),
            },
            vendor_active=self._vendor_state(),
            daemon=False,
        )


class Display:
    """Renders snapshots and turns keystrokes into control requests."""

    C_NORMAL = 1
    C_GOOD = 2
    C_WARN = 3
    C_HOT = 4
    C_DIM = 5
    C_HEAD = 6

    def __init__(self, screen, reader: Reader,
                 startup_note: str | None = None) -> None:
        self._screen = screen
        self._reader = reader
        self._message = startup_note or ""
        self._message_until = time.monotonic() + 5.0 if startup_note else 0.0
        self._colour = False
        self._history = History()
        self._show_chart = True

    # ---- setup -------------------------------------------------------

    def setup(self) -> None:
        curses.curs_set(0)
        self._screen.nodelay(True)
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(self.C_NORMAL, -1, -1)
            curses.init_pair(self.C_GOOD, curses.COLOR_GREEN, -1)
            curses.init_pair(self.C_WARN, curses.COLOR_YELLOW, -1)
            curses.init_pair(self.C_HOT, curses.COLOR_RED, -1)
            curses.init_pair(self.C_DIM, curses.COLOR_BLUE, -1)
            curses.init_pair(self.C_HEAD, curses.COLOR_CYAN, -1)
            self._colour = True
        except curses.error:
            # Monochrome terminals still get a usable layout.
            self._colour = False

    def _attr(self, pair: int, bold: bool = False) -> int:
        attr = curses.color_pair(pair) if self._colour else 0
        return attr | (curses.A_BOLD if bold else 0)

    # ---- drawing helpers ---------------------------------------------

    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        """Write text, clipped to the window.

        curses raises when writing to the last cell, and a resize can make any
        coordinate invalid, so every write is bounded and errors are ignored.
        """
        height, width = self._screen.getmaxyx()
        if not 0 <= y < height or x >= width:
            return
        try:
            self._screen.addnstr(y, x, text, max(0, width - x - 1), attr)
        except curses.error:
            pass

    @staticmethod
    def _bar(value: float, maximum: float, width: int) -> tuple[int, int]:
        """Split a bar into filled and empty cell counts."""
        if maximum <= 0 or width <= 0:
            return 0, max(0, width)
        ratio = min(1.0, max(0.0, value / maximum))
        filled = int(round(ratio * width))
        return filled, width - filled

    def _temp_attr(self, temp_c: float) -> int:
        if temp_c >= TEMP_HOT_C:
            return self._attr(self.C_HOT, bold=True)
        if temp_c >= TEMP_WARN_C:
            return self._attr(self.C_WARN)
        return self._attr(self.C_GOOD)

    def _draw_bar(self, y: int, x: int, width: int, value: float,
                  maximum: float, attr: int, marker: int | None = None) -> None:
        """Draw a bar, optionally with a ceiling marker drawn over it.

        The marker is how the stock 5371 RPM cap is shown in place: a number in
        a table does not convey that the vendor profile stops at 40% of what
        the fan can do.
        """
        filled, empty = self._bar(value, maximum, width)
        self._put(y, x, "█" * filled, attr)
        self._put(y, x + filled, "░" * empty, self._attr(self.C_DIM))
        if marker is not None and 0 < marker < width:
            self._put(y, x + marker, "┃", self._attr(self.C_HEAD, bold=True))

    # ---- sections ----------------------------------------------------

    def _draw_header(self, snap: Snapshot, width: int) -> int:
        title = "ThorFAN"
        self._put(0, 0, title, self._attr(self.C_HEAD, bold=True))

        if snap.daemon:
            state = f"mode: {snap.mode}"
            attr = self._attr(self.C_GOOD)
        elif snap.error:
            state = "no hardware"
            attr = self._attr(self.C_HOT)
        else:
            state = "read-only (no daemon)"
            attr = self._attr(self.C_WARN)
        self._put(0, len(title) + 2, state, attr)

        if snap.emergency:
            self._put(0, len(title) + 4 + len(state), "EMERGENCY OVERRIDE",
                      self._attr(self.C_HOT, bold=True))
        elif snap.vendor_active:
            self._put(0, len(title) + 4 + len(state), "nvfancontrol active",
                      self._attr(self.C_DIM))

        self._put(1, 0, "─" * max(0, width - 1), self._attr(self.C_DIM))
        return 2

    def _draw_fan(self, y: int, snap: Snapshot, width: int) -> int:
        bar_x = 26
        bar_w = max(10, width - bar_x - 14)

        pwm = snap.fan.get("pwm")
        rpm = snap.fan.get("rpm")

        if pwm is None:
            self._put(y, 0, "fan   pwm unavailable", self._attr(self.C_HOT))
            return y + 1

        percent = pwm / PWM_MAX * 100
        label = f"fan   pwm {pwm:3d}/{PWM_MAX} ({percent:3.0f}%)"
        self._put(y, 0, label, self._attr(self.C_NORMAL, bold=True))
        self._draw_bar(y, bar_x, bar_w, pwm, PWM_MAX,
                       self._attr(self.C_GOOD))
        if snap.fan.get("enabled") is False:
            # pwm1_enable == 0 switches the fan off; a duty cycle shown next to
            # a stopped fan would be a dangerous half-truth.
            self._put(y, bar_x + bar_w + 1, "OUTPUT OFF",
                      self._attr(self.C_HOT, bold=True))
        y += 1

        if rpm is None:
            self._put(y, 6, "rpm unavailable", self._attr(self.C_DIM))
            return y + 1

        marker, _ = self._bar(STOCK_RPM_CAP, FAN_MAX_RPM, bar_w)
        self._put(y, 0, f"      {rpm:5d} rpm", self._attr(self.C_NORMAL))
        self._draw_bar(y, bar_x, bar_w, rpm, FAN_MAX_RPM,
                       self._attr(self.C_HEAD), marker=marker)
        self._put(y, bar_x + bar_w + 1, f"max {FAN_MAX_RPM}",
                  self._attr(self.C_DIM))
        y += 1
        self._put(y, bar_x + marker, f"┗ stock cap {STOCK_RPM_CAP}",
                  self._attr(self.C_DIM))
        return y + 1

    def _draw_temps(self, y: int, snap: Snapshot, width: int) -> int:
        bar_x = 26
        bar_w = max(10, width - bar_x - 14)

        readable = [z for z in snap.zones if z.get("temp_c") is not None]
        hottest = max((z["temp_c"] for z in readable), default=None)

        self._put(y, 0, "temps", self._attr(self.C_NORMAL, bold=True))
        for zone in sorted(snap.zones,
                           key=lambda z: (z.get("temp_c") is not None,
                                          z.get("temp_c") or 0.0),
                           reverse=True):
            name = zone["name"].replace("-thermal", "")
            temp = zone.get("temp_c")
            if temp is None:
                self._put(y, 6, f"{name:<10} power-gated",
                          self._attr(self.C_DIM))
                y += 1
                continue

            # The hottest zone is what the chart plots and what the curve and
            # interlock act on, so mark it rather than leaving the reader to
            # compare five numbers.
            lead = "*" if temp == hottest else " "
            self._put(y, 6, f"{name:<10}{lead}{temp:5.1f} C",
                      self._temp_attr(temp))
            self._draw_bar(
                y, bar_x, bar_w,
                max(0.0, temp - TEMP_MIN_C), TEMP_MAX_C - TEMP_MIN_C,
                self._temp_attr(temp),
            )
            crit = zone.get("critical_c")
            if crit:
                self._put(y, bar_x + bar_w + 1, f"crit {crit:.0f}",
                          self._attr(self.C_DIM))
            y += 1
        return y

    def _draw_power(self, y: int, snap: Snapshot, width: int) -> int:
        if not snap.rails:
            return y

        self._put(y, 0, "power", self._attr(self.C_NORMAL, bold=True))
        total = 0.0
        for rail in snap.rails:
            watts = rail.get("watts")
            name = rail["name"]
            if watts is None:
                self._put(y, 6, f"{name:<18} unavailable",
                          self._attr(self.C_DIM))
                y += 1
                continue
            total += watts
            self._put(y, 6, f"{name:<18}{watts:6.2f} W",
                      self._attr(self.C_NORMAL))
            y += 1

        # Worth stating plainly: raising the fan ceiling bought only 5.8 C on
        # the tested board, so the rail draw is the lever that actually matters.
        self._put(y, 6, f"{'total':<18}{total:6.2f} W",
                  self._attr(self.C_HEAD, bold=True))
        return y + 1

    def _draw_throttles(self, y: int, snap: Snapshot, width: int) -> int:
        if not snap.throttles:
            return y

        alerts = [t for t in snap.throttles if t.get("is_alert")]
        caps = [t for t in snap.throttles if not t.get("is_alert")]

        if alerts:
            self._put(y, 0, "throt", self._attr(self.C_NORMAL, bold=True))
            x = 6
            for alert in alerts:
                name = alert["name"].replace("-throttle-alert", "")
                state = alert.get("cur_state")
                active = state is not None and state > 0
                glyph = "●" if active else "○"
                attr = (self._attr(self.C_HOT, bold=True) if active
                        else self._attr(self.C_DIM))
                text = f"{glyph} {name}  "
                self._put(y, x, text, attr)
                x += len(text) + 1
            y += 1

        # Frequency caps are noisy, so only report the ones actually capping.
        capped = [c for c in caps
                  if (c.get("cur_state") or 0) > 0]
        if capped:
            x = 6
            self._put(y, 0, "freq", self._attr(self.C_NORMAL, bold=True))
            for cap in capped:
                name = cap["name"].replace("devfreq-", "").replace("cpufreq-", "")
                text = f"{name} {cap['cur_state']}/{cap['max_state']}  "
                self._put(y, x, text, self._attr(self.C_WARN))
                x += len(text) + 1
            y += 1
        return y

    def _draw_chart(self, y: int, snap: Snapshot, width: int) -> int:
        """Plot recent temperature and duty cycle history.

        Two series on one chart, sharing the vertical axis by coincidence
        rather than by unit: temperature is scaled over its own range and duty
        cycle over 0-100%. That is a compromise, but the useful reading is the
        *shape* of the response, how the fan reacts to a temperature rise, and
        for that the two curves need to sit together.
        """
        if len(self._history) < 2:
            self._put(y, 0, "chart  collecting samples...",
                      self._attr(self.C_DIM))
            return y + 1

        chart_x = 8
        chart_w = max(10, width - chart_x - 8)

        temp_rows = braille_rows(self._history.temps, chart_w, CHART_ROWS,
                                 TEMP_MIN_C, TEMP_MAX_C)
        pwm_rows = braille_rows(self._history.pwm_percent, chart_w, CHART_ROWS,
                                0.0, 100.0)

        self._put(y, 0, "chart", self._attr(self.C_NORMAL, bold=True))

        for row in range(CHART_ROWS):
            # Duty cycle first so the temperature curve draws over it where
            # they overlap; temperature is what the user is watching.
            self._put(y + row, chart_x, pwm_rows[row],
                      self._attr(self.C_DIM))
            temp_line = temp_rows[row]
            for column, glyph in enumerate(temp_line):
                if glyph != chr(_BRAILLE_BASE):
                    self._put(y + row, chart_x + column, glyph,
                              self._attr(self.C_HEAD))

            # Axis labels on the right, top row and bottom row only.
            if row == 0:
                self._put(y + row, chart_x + chart_w + 1, f"{TEMP_MAX_C:.0f}C",
                          self._attr(self.C_DIM))
            elif row == CHART_ROWS - 1:
                self._put(y + row, chart_x + chart_w + 1, f"{TEMP_MIN_C:.0f}C",
                          self._attr(self.C_DIM))

        y += CHART_ROWS
        span_s = len(self._history) * REFRESH_S
        self._put(y, chart_x, f"last {span_s:.0f}s", self._attr(self.C_DIM))

        # Name the plotted zone: the chart follows whichever is hottest, so
        # without a label the curve does not say what it represents.
        source = self._history.source
        label = (source.replace("-thermal", "") if source else "none")
        self._put(y, chart_x + 14, f"temp {label}", self._attr(self.C_HEAD))
        self._put(y, chart_x + 16 + len(label) + 4, "duty",
                  self._attr(self.C_DIM))
        return y + 1

    def _draw_footer(self, y: int, snap: Snapshot, width: int) -> None:
        height, _ = self._screen.getmaxyx()
        y = min(y + 1, height - 3)
        self._put(y, 0, "─" * max(0, width - 1), self._attr(self.C_DIM))

        if snap.daemon:
            keys = ("[↑/↓] ±5%   [1-9] 10-90%   [0] 100%   "
                    "[c]urve  [m]anual  [v]endor  [g]raph  [q]uit")
        else:
            keys = "[g]raph   [q]uit    re-run with sudo to control the fan"
        self._put(y + 1, 0, keys, self._attr(self.C_DIM))

        if self._message and time.monotonic() < self._message_until:
            self._put(y + 2, 0, self._message,
                      self._attr(self.C_HEAD, bold=True))

    # ---- interaction -------------------------------------------------

    def _notify(self, text: str, seconds: float = 3.0) -> None:
        self._message = text
        self._message_until = time.monotonic() + seconds

    def _send(self, request: dict, description: str) -> None:
        try:
            control.send(request)
            self._notify(description)
        except control.DaemonNotRunning:
            self._notify("no daemon running; cannot change the fan")
        except control.ControlError as exc:
            self._notify(f"rejected: {exc}")

    def _adjust(self, snap: Snapshot, delta_percent: int) -> None:
        """Nudge the duty cycle relative to what the fan is doing now.

        Relative to the observed pwm rather than a remembered target, so the
        first keypress after arriving from vendor or curve mode does something
        predictable instead of jumping.
        """
        current = snap.fan.get("pwm")
        if current is None:
            self._notify("cannot read the current duty cycle")
            return
        step = round(delta_percent / 100 * PWM_MAX)
        target = max(0, min(PWM_MAX, current + step))
        self._send({"action": "set", "pwm": target},
                   f"set {target}/{PWM_MAX} ({target / PWM_MAX * 100:.0f}%)")

    def handle_key(self, key: int, snap: Snapshot) -> bool:
        """Act on one keystroke. Returns False to quit."""
        if key in (ord("q"), ord("Q"), 27):
            return False

        if key in (curses.KEY_UP, ord("k"), ord("+")):
            self._adjust(snap, _STEP_PERCENT)
        elif key in (curses.KEY_DOWN, ord("j"), ord("-")):
            self._adjust(snap, -_STEP_PERCENT)
        elif ord("1") <= key <= ord("9"):
            percent = (key - ord("0")) * 10
            pwm = round(percent / 100 * PWM_MAX)
            self._send({"action": "set", "pwm": pwm}, f"set {percent}%")
        elif key == ord("0"):
            self._send({"action": "set", "pwm": PWM_MAX}, "set 100%")
        elif key in (ord("c"), ord("C")):
            self._send({"action": "set", "mode": Mode.CURVE.value},
                       "switched to curve mode")
        elif key in (ord("m"), ord("M")):
            self._send({"action": "set", "mode": Mode.MANUAL.value},
                       "switched to manual mode")
        elif key in (ord("v"), ord("V")):
            self._send({"action": "set", "mode": Mode.VENDOR.value},
                       "handed the fan back to nvfancontrol")
        elif key in (ord("g"), ord("G")):
            self._show_chart = not self._show_chart
            self._notify("chart on" if self._show_chart else "chart off")
        return True

    # ---- main loop ---------------------------------------------------

    def run(self) -> int:
        self.setup()
        snap = self._reader.snapshot()
        self._record(snap)
        next_refresh = time.monotonic() + REFRESH_S

        while True:
            now = time.monotonic()
            if now >= next_refresh:
                snap = self._reader.snapshot()
                self._record(snap)
                next_refresh = now + REFRESH_S

            self._screen.erase()
            height, width = self._screen.getmaxyx()
            if snap.error:
                self._put(0, 0, f"thorfan: {snap.error}",
                          self._attr(self.C_HOT, bold=True))
                self._put(2, 0, "[q]uit", self._attr(self.C_DIM))
            else:
                y = self._draw_header(snap, width)
                y = self._draw_fan(y, snap, width)
                y = self._draw_temps(y + 1, snap, width)
                y = self._draw_power(y + 1, snap, width)
                y = self._draw_throttles(y + 1, snap, width)
                # The chart is the first thing to go in a short terminal: the
                # live readings are what must always be visible.
                if self._show_chart and height >= y + CHART_ROWS + 5:
                    y = self._draw_chart(y + 1, snap, width)
                self._draw_footer(y, snap, width)
            self._screen.refresh()

            # Poll for keys frequently so input feels immediate, while data
            # refreshes on its own slower schedule.
            curses.napms(50)
            try:
                key = self._screen.getch()
            except curses.error:
                key = -1
            if key == curses.KEY_RESIZE:
                continue
            if key != -1 and not self.handle_key(key, snap):
                return 0

    def _record(self, snap: Snapshot) -> None:
        """Append one sample to the chart history.

        Tracks the hottest readable zone rather than a fixed one, because which
        zone is hottest changes with the workload and tj-thermal is itself the
        aggregate maximum. The zone's name is recorded so the chart can say
        what it is plotting.
        """
        readable = [(z["name"], z["temp_c"]) for z in snap.zones
                    if z.get("temp_c") is not None]
        if readable:
            name, temp = max(readable, key=lambda pair: pair[1])
        else:
            name, temp = None, None
        self._history.add(temp, snap.fan.get("pwm"), source=name)


class EmbeddedDaemon:
    """Runs a control loop for the lifetime of the display, when needed.

    The display needs a control loop to change anything, because the thermal
    interlock lives in the loop rather than in the write itself. Requiring the
    user to start one in a second terminal is a poor interface, so this starts
    one in-process when it can.

    Three cases, in order:
      * a daemon is already running: use it, never displace it, because two
        loops writing pwm1 would oscillate;
      * running as root: start a loop in a background thread, stopped on exit,
        which restores nvfancontrol through the usual FanOwnership path;
      * otherwise: do nothing, and the display stays read-only.

    Logging is silenced while embedded: the daemon's log lines would be drawn
    over the curses layout.
    """

    def __init__(self) -> None:
        self._daemon: Daemon | None = None
        self._thread: threading.Thread | None = None
        self.note: str | None = None

    def __enter__(self) -> EmbeddedDaemon:
        if control.probe():
            self.note = "using the running daemon"
            return self

        if os.geteuid() != 0:
            self.note = "read-only: run with sudo, or join the thorfan group"
            return self

        try:
            self._daemon = Daemon()
        except HardwareError as exc:
            self.note = f"no control: {exc}"
            return self

        # curses owns the terminal, so daemon logs must not reach it.
        logging.getLogger("thorfan").addHandler(logging.NullHandler())
        logging.getLogger("thorfan").propagate = False

        self._thread = threading.Thread(
            target=self._run, name="thorfan-embedded", daemon=True
        )
        self._thread.start()

        # Give the loop a moment to bind its socket so the first frame already
        # shows live control rather than briefly claiming to be read-only.
        for _ in range(20):
            if control.probe():
                break
            time.sleep(0.05)
        self.note = "control loop started for this session"
        return self

    def _run(self) -> None:
        assert self._daemon is not None
        try:
            # Signal handlers can only be installed on the main thread, and
            # curses already owns SIGINT handling for us.
            self._daemon.run(install_signal_handlers=False)
        except Exception:
            # Nothing can be printed without corrupting the display; the
            # display will simply show that control is unavailable.
            pass

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._daemon is not None:
            # Stopping the loop runs FanOwnership.release, handing the fan back
            # to nvfancontrol. Never leave the board with an unmanaged fan.
            self._daemon.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=10)
        return False


def run() -> int:
    """Entry point for `thorfan tui`."""
    reader = Reader()
    with EmbeddedDaemon() as embedded:
        return curses.wrapper(
            lambda scr: Display(scr, reader, embedded.note).run()
        )
