"""Fan control policy: curves, safety interlocks, and the control loop.

Design priority is safety over user intent. A GUI that can stop the fan on a
board that idles at 108 C must never leave the fan stopped, so every code path
that relinquishes control restores the vendor daemon.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from enum import Enum

from .hwmon import PWM_MAX, PWM_MIN, FanDevice, HardwareError, ThermalZone

# Absolute temperature at which user settings are overridden entirely.
# Thor's critical trip is 114.5 C and the GPU begins throttling around 109 C,
# so we intervene well before either.
EMERGENCY_TEMP_C = 100.0

# Duty cycle applied while the emergency override is latched.
EMERGENCY_PWM = PWM_MAX

# Hysteresis: once latched, the override holds until the temperature drops
# this far below the trigger, preventing rapid oscillation at the boundary.
EMERGENCY_CLEAR_C = 95.0

# The stock nvfancontrol profile never exceeds this, despite the fan being
# capable of roughly 13650 RPM at pwm 255. Kept for reference in the UI.
STOCK_RPM_CAP = 5371


class Mode(Enum):
    """Who is driving the fan."""

    VENDOR = "vendor"   # nvfancontrol owns the fan; we only observe
    CURVE = "curve"     # our own curve drives the fan
    MANUAL = "manual"   # a fixed duty cycle set by the user


@dataclass(frozen=True)
class CurvePoint:
    """One vertex of a fan curve.

    `temp_c` is an absolute temperature, deliberately unlike nvfancontrol's
    TMARGIN profiles where the column is headroom below the critical trip.
    Absolute values are what users actually reason about.
    """

    temp_c: float
    pwm: int

    def __post_init__(self) -> None:
        if not PWM_MIN <= self.pwm <= PWM_MAX:
            raise ValueError(f"pwm must be {PWM_MIN}-{PWM_MAX}, got {self.pwm}")


@dataclass
class FanCurve:
    """A piecewise-linear mapping from temperature to duty cycle."""

    points: list[CurvePoint] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("a curve needs at least two points")
        self.points = sorted(self.points, key=lambda p: p.temp_c)
        temps = [p.temp_c for p in self.points]
        if len(set(temps)) != len(temps):
            raise ValueError("curve has duplicate temperatures")

    def pwm_for(self, temp_c: float) -> int:
        """Interpolate the duty cycle for a temperature.

        Values outside the curve clamp to the nearest endpoint.
        """
        pts = self.points
        if temp_c <= pts[0].temp_c:
            return pts[0].pwm
        if temp_c >= pts[-1].temp_c:
            return pts[-1].pwm

        temps = [p.temp_c for p in pts]
        i = bisect.bisect_right(temps, temp_c)
        lo, hi = pts[i - 1], pts[i]
        span = hi.temp_c - lo.temp_c
        ratio = (temp_c - lo.temp_c) / span
        return round(lo.pwm + ratio * (hi.pwm - lo.pwm))

    @classmethod
    def default(cls) -> FanCurve:
        """A curve that uses the fan's real capability.

        The stock profile holds ~5371 RPM even at 109 C. Measured points:
        pwm 77 -> 3630 RPM, pwm 97 -> 5400 RPM, pwm 255 -> 13650 RPM.
        Below 60 C this matches the vendor curve so idle noise is unchanged.
        """
        return cls([
            CurvePoint(40.0, 77),    # ~3600 RPM, vendor-equivalent idle
            CurvePoint(60.0, 90),    # ~5000 RPM
            CurvePoint(75.0, 120),   # ~6500 RPM
            CurvePoint(85.0, 165),   # ~8800 RPM
            CurvePoint(95.0, 210),   # ~11200 RPM
            CurvePoint(100.0, 255),  # full speed before throttling begins
        ])


@dataclass
class ControllerState:
    """A snapshot of the controller, suitable for display."""

    mode: Mode
    pwm: int
    rpm: int | None
    max_temp_c: float
    hottest_zone: str
    emergency: bool


class FanController:
    """Applies a policy to the fan, with a non-negotiable safety override.

    The controller never writes to the fan in VENDOR mode; nvfancontrol and
    this tool must not fight over the same sysfs node.
    """

    def __init__(
        self,
        fan: FanDevice,
        zones: list[ThermalZone],
        curve: FanCurve | None = None,
    ) -> None:
        if not zones:
            raise ValueError("at least one thermal zone is required")
        self._fan = fan
        self._zones = zones
        self._curve = curve or FanCurve.default()
        self._mode = Mode.VENDOR
        self._manual_pwm = 128
        self._emergency = False

    # ---- configuration ----------------------------------------------

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def curve(self) -> FanCurve:
        return self._curve

    def set_curve(self, curve: FanCurve) -> None:
        self._curve = curve

    def set_mode(self, mode: Mode) -> None:
        self._mode = mode

    def set_manual_pwm(self, pwm: int) -> None:
        if not PWM_MIN <= pwm <= PWM_MAX:
            raise ValueError(f"pwm must be {PWM_MIN}-{PWM_MAX}, got {pwm}")
        self._manual_pwm = pwm

    # ---- sensing -----------------------------------------------------

    def hottest(self) -> tuple[str, float]:
        """Return the name and temperature of the hottest readable zone.

        Zones whose sensor is currently unavailable are skipped: gpu-thermal
        returns EAGAIN on every read while the GPU is power-gated at idle, and
        treating that as fatal would stop the control loop. If no zone can be
        read at all we cannot make a safe decision, so that raises.
        """
        readings = [
            (z.name, temp) for z in self._zones
            if (temp := z.try_temp_c()) is not None
        ]
        if not readings:
            raise HardwareError("no thermal zone could be read")
        return max(readings, key=lambda pair: pair[1])

    # ---- control -----------------------------------------------------

    def _target_pwm(self, temp_c: float) -> int:
        if self._mode is Mode.MANUAL:
            return self._manual_pwm
        return self._curve.pwm_for(temp_c)

    def step(self) -> ControllerState:
        """Evaluate sensors once and drive the fan accordingly.

        Safe to call when not root as long as the mode is VENDOR.
        """
        try:
            zone_name, temp_c = self.hottest()
        except HardwareError:
            # Flying blind. In vendor mode nvfancontrol still has its own
            # protection, but if we are driving the fan we must assume the
            # worst and go to full speed rather than hold the last value.
            if self._mode is Mode.VENDOR:
                raise
            self._emergency = True
            self._fan.set_pwm(EMERGENCY_PWM)
            return ControllerState(
                mode=self._mode,
                pwm=EMERGENCY_PWM,
                rpm=self._fan.rpm,
                max_temp_c=float("nan"),
                hottest_zone="unknown",
                emergency=True,
            )

        # Latch on crossing the emergency threshold, release only after the
        # temperature has fallen back through the clear point.
        if temp_c >= EMERGENCY_TEMP_C:
            self._emergency = True
        elif temp_c <= EMERGENCY_CLEAR_C:
            self._emergency = False

        if self._mode is Mode.VENDOR:
            # Observe only. nvfancontrol is responsible for the fan, and it
            # has its own thermal protection, so we must not write here.
            return ControllerState(
                mode=self._mode,
                pwm=self._fan.pwm,
                rpm=self._fan.rpm,
                max_temp_c=temp_c,
                hottest_zone=zone_name,
                emergency=self._emergency,
            )

        pwm = EMERGENCY_PWM if self._emergency else self._target_pwm(temp_c)
        self._fan.set_pwm(pwm)

        return ControllerState(
            mode=self._mode,
            pwm=pwm,
            rpm=self._fan.rpm,
            max_temp_c=temp_c,
            hottest_zone=zone_name,
            emergency=self._emergency,
        )
