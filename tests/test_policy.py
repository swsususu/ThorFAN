"""Tests for fan curve interpolation and the safety override."""

from __future__ import annotations

import pytest

from thorfan.core.hwmon import PWM_MAX, HardwareError
from thorfan.core.policy import (
    EMERGENCY_CLEAR_C,
    EMERGENCY_TEMP_C,
    ControllerState,
    CurvePoint,
    FanController,
    FanCurve,
    Mode,
)


class FakeFan:
    """Records writes so tests can assert on control decisions."""

    def __init__(self) -> None:
        self.pwm = 100
        self.enabled = True
        self.rpm = 5000
        self.writes: list[int] = []

    def set_pwm(self, value: int) -> None:
        self.pwm = value
        self.writes.append(value)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled


class FakeZone:
    def __init__(self, name: str, temp_c: float | None = 0.0) -> None:
        self.name = name
        self._temp_c = temp_c

    @property
    def temp_c(self) -> float:
        if self._temp_c is None:
            raise HardwareError(f"{self.name} is power-gated")
        return self._temp_c

    def try_temp_c(self) -> float | None:
        """Mirrors ThermalZone: None when the sensor is unavailable."""
        return self._temp_c

    def set(self, temp_c: float | None) -> None:
        self._temp_c = temp_c


# ---- curve ----------------------------------------------------------


def test_curve_sorts_unordered_points():
    curve = FanCurve([CurvePoint(90, 200), CurvePoint(40, 80)])
    assert [p.temp_c for p in curve.points] == [40, 90]


def test_curve_rejects_single_point():
    with pytest.raises(ValueError, match="two points"):
        FanCurve([CurvePoint(50, 100)])


def test_curve_rejects_duplicate_temps():
    with pytest.raises(ValueError, match="duplicate"):
        FanCurve([CurvePoint(50, 100), CurvePoint(50, 200)])


def test_curve_point_rejects_out_of_range_pwm():
    with pytest.raises(ValueError, match="pwm must be"):
        CurvePoint(50, 300)


def test_curve_clamps_below_and_above():
    curve = FanCurve([CurvePoint(40, 80), CurvePoint(100, 255)])
    assert curve.pwm_for(-10) == 80
    assert curve.pwm_for(20) == 80
    assert curve.pwm_for(200) == 255


def test_curve_interpolates_midpoint():
    curve = FanCurve([CurvePoint(40, 80), CurvePoint(60, 120)])
    assert curve.pwm_for(50) == 100


def test_curve_hits_exact_vertices():
    curve = FanCurve([CurvePoint(40, 80), CurvePoint(60, 120), CurvePoint(80, 200)])
    assert curve.pwm_for(40) == 80
    assert curve.pwm_for(60) == 120
    assert curve.pwm_for(80) == 200


def test_default_curve_reaches_full_speed_before_throttling():
    """The stock profile caps at 5371 RPM; ours must not."""
    curve = FanCurve.default()
    assert curve.pwm_for(100) == PWM_MAX
    # And it should stay quiet at idle, matching the vendor curve.
    assert curve.pwm_for(40) == 77


def test_default_curve_is_monotonic():
    curve = FanCurve.default()
    pwms = [curve.pwm_for(t) for t in range(30, 110, 2)]
    assert pwms == sorted(pwms)


# ---- controller --------------------------------------------------------


def _controller(temp_c: float, mode: Mode = Mode.CURVE):
    fan = FakeFan()
    zone = FakeZone("tj-thermal", temp_c)
    ctrl = FanController(fan, [zone])
    ctrl.set_mode(mode)
    return ctrl, fan, zone


def test_vendor_mode_never_writes():
    """Fighting nvfancontrol over the same sysfs node must be impossible."""
    ctrl, fan, zone = _controller(105.0, Mode.VENDOR)
    state = ctrl.step()
    assert fan.writes == []
    assert state.mode is Mode.VENDOR
    # Even an emergency must not provoke a write in vendor mode.
    assert state.emergency is True


def test_curve_mode_writes_interpolated_value():
    ctrl, fan, _ = _controller(60.0)
    ctrl.set_curve(FanCurve([CurvePoint(40, 80), CurvePoint(80, 160)]))
    state = ctrl.step()
    assert fan.writes == [120]
    assert state.pwm == 120


def test_manual_mode_uses_fixed_pwm():
    ctrl, fan, _ = _controller(50.0, Mode.MANUAL)
    ctrl.set_manual_pwm(140)
    ctrl.step()
    assert fan.writes == [140]


def test_manual_mode_rejects_bad_pwm():
    ctrl, _, _ = _controller(50.0, Mode.MANUAL)
    with pytest.raises(ValueError):
        ctrl.set_manual_pwm(999)


def test_emergency_overrides_manual_stop():
    """A user asking for a stopped fan at 105 C must be overruled."""
    ctrl, fan, _ = _controller(105.0, Mode.MANUAL)
    ctrl.set_manual_pwm(0)
    state = ctrl.step()
    assert fan.writes == [PWM_MAX]
    assert state.emergency is True


def test_emergency_latches_with_hysteresis():
    ctrl, fan, zone = _controller(50.0, Mode.MANUAL)
    ctrl.set_manual_pwm(0)

    ctrl.step()
    assert fan.writes[-1] == 0

    # Cross the trigger: override engages.
    zone.set(EMERGENCY_TEMP_C + 0.5)
    assert ctrl.step().emergency is True
    assert fan.writes[-1] == PWM_MAX

    # Between clear and trigger the override stays latched.
    zone.set((EMERGENCY_CLEAR_C + EMERGENCY_TEMP_C) / 2)
    assert ctrl.step().emergency is True
    assert fan.writes[-1] == PWM_MAX

    # Only below the clear point does user intent resume.
    zone.set(EMERGENCY_CLEAR_C - 1.0)
    assert ctrl.step().emergency is False
    assert fan.writes[-1] == 0


def test_hottest_zone_wins():
    fan = FakeFan()
    zones = [FakeZone("cpu", 70.0), FakeZone("gpu", 95.0), FakeZone("soc", 80.0)]
    ctrl = FanController(fan, zones)
    name, temp = ctrl.hottest()
    assert name == "gpu"
    assert temp == 95.0


def test_unreadable_zone_is_skipped():
    """gpu-thermal is unreadable while the GPU is power-gated at idle.

    Observed on a Thor dev kit: 300/300 reads of thermal_zone1/temp returned
    EAGAIN with the GPU idle, and worked again under load. Treating that as
    fatal would stop the control loop.
    """
    fan = FakeFan()
    zones = [FakeZone("gpu", None), FakeZone("tj", 75.0)]
    ctrl = FanController(fan, zones)
    assert ctrl.hottest() == ("tj", 75.0)

    ctrl.set_mode(Mode.CURVE)
    state = ctrl.step()
    assert state.hottest_zone == "tj"
    assert fan.writes == [120]  # default curve at 75 C


def test_all_zones_unreadable_forces_full_speed():
    """With no temperature at all we must assume the worst, not hold."""
    fan = FakeFan()
    ctrl = FanController(fan, [FakeZone("gpu", None), FakeZone("tj", None)])
    ctrl.set_mode(Mode.MANUAL)
    ctrl.set_manual_pwm(0)

    state = ctrl.step()
    assert fan.writes == [PWM_MAX]
    assert state.emergency is True
    assert state.hottest_zone == "unknown"


def test_all_zones_unreadable_raises_in_vendor_mode():
    """In vendor mode nvfancontrol owns protection; we must not write."""
    fan = FakeFan()
    ctrl = FanController(fan, [FakeZone("gpu", None)])
    ctrl.set_mode(Mode.VENDOR)
    with pytest.raises(HardwareError, match="no thermal zone"):
        ctrl.step()
    assert fan.writes == []


def test_zone_recovering_resumes_normal_control():
    fan = FakeFan()
    gated = FakeZone("gpu", None)
    ctrl = FanController(fan, [gated, FakeZone("tj", 50.0)])
    ctrl.set_mode(Mode.CURVE)
    ctrl.step()

    # GPU comes out of railgate and is now the hottest zone.
    gated.set(85.0)
    state = ctrl.step()
    assert state.hottest_zone == "gpu"
    assert fan.writes[-1] == 165  # default curve at 85 C


def test_controller_requires_a_zone():
    with pytest.raises(ValueError, match="thermal zone"):
        FanController(FakeFan(), [])
