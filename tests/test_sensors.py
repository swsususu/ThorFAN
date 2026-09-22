"""Tests for power rail and throttle discovery.

These use fake sysfs trees because the real ones only exist on a Jetson, and
because the interesting cases (a missing rail, a shunt-only channel) are hard
to produce on demand.
"""

from __future__ import annotations

import os

import pytest

from thorfan.core import hwmon


def _write(path: str, value: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(value)


@pytest.fixture
def fake_hwmon(tmp_path, monkeypatch):
    """An hwmon root containing an ina3221 with three labelled rails."""
    root = tmp_path / "hwmon"
    ina = root / "hwmon5"
    _write(str(ina / "name"), "ina3221\n")

    rails = [
        (1, "VDD_GPU", "12040", "640"),
        (2, "VDD_CPU_SOC_MSS", "12040", "960"),
        (3, "VIN_SYS_5V0", "5088", "1740"),
    ]
    for index, label, mv, ma in rails:
        _write(str(ina / f"in{index}_label"), f"{label}\n")
        _write(str(ina / f"in{index}_input"), f"{mv}\n")
        _write(str(ina / f"curr{index}_input"), f"{ma}\n")

    # A shunt voltage channel: labelled, but with no current reading.
    _write(str(ina / "in7_label"), "sum of shunt voltages\n")
    _write(str(ina / "in7_input"), "3200\n")

    # An unlabelled channel with a current reading, which is also not a rail.
    _write(str(ina / "in4_input"), "1240\n")
    _write(str(ina / "curr4_input"), "1600\n")

    monkeypatch.setattr(hwmon, "HWMON_ROOT", str(root))
    return root


@pytest.fixture
def fake_thermal(tmp_path, monkeypatch):
    """A thermal root with alerts, frequency caps, and the fan."""
    root = tmp_path / "thermal"
    devices = [
        (0, "cpufreq-cpu0", "0", "48"),
        (7, "pwm-fan", "0", "4"),
        (8, "cpu-throttle-alert", "0", "2"),
        (9, "gpu-throttle-alert", "1", "2"),
        (10, "soc012-throttle-alert", "0", "2"),
        (12, "devfreq-gpu-gpc-0", "18", "169"),
    ]
    for index, name, cur, maximum in devices:
        base = root / f"cooling_device{index}"
        _write(str(base / "type"), f"{name}\n")
        _write(str(base / "cur_state"), f"{cur}\n")
        _write(str(base / "max_state"), f"{maximum}\n")

    monkeypatch.setattr(hwmon, "THERMAL_ROOT", str(root))
    return root


# ---- rails ------------------------------------------------------------


def test_discovers_labelled_rails_only(fake_hwmon):
    names = [r.name for r in hwmon.discover_rails()]
    assert names == ["VDD_GPU", "VDD_CPU_SOC_MSS", "VIN_SYS_5V0"]


def test_shunt_channel_is_excluded(fake_hwmon):
    """Summing shunt voltages into total power would double-count."""
    assert all("shunt" not in r.name.lower() for r in hwmon.discover_rails())


def test_unlabelled_channel_is_excluded(fake_hwmon):
    """in4 has a current reading but no label; it is not a named rail."""
    assert all(r.index != 4 for r in hwmon.discover_rails())


def test_watts_is_volts_times_amps(fake_hwmon):
    gpu = next(r for r in hwmon.discover_rails() if r.name == "VDD_GPU")
    assert gpu.watts() == pytest.approx(12.040 * 0.640, rel=1e-6)


def test_rail_with_unreadable_current_returns_none(fake_hwmon):
    rail = next(r for r in hwmon.discover_rails() if r.name == "VDD_GPU")
    os.remove(os.path.join(rail.path, f"curr{rail.index}_input"))
    assert rail.watts() is None


def test_no_ina3221_yields_no_rails(tmp_path, monkeypatch):
    monkeypatch.setattr(hwmon, "HWMON_ROOT", str(tmp_path / "empty"))
    assert hwmon.discover_rails() == []


# ---- throttles --------------------------------------------------------


def test_fan_cooling_device_is_excluded(fake_thermal):
    """The fan is an output of control, not a symptom of throttling."""
    assert all(t.name != "pwm-fan" for t in hwmon.discover_throttles())


def test_alerts_sort_before_frequency_caps(fake_thermal):
    found = hwmon.discover_throttles()
    alert_positions = [i for i, t in enumerate(found) if t.is_alert]
    cap_positions = [i for i, t in enumerate(found) if not t.is_alert]
    assert max(alert_positions) < min(cap_positions)


def test_ordering_does_not_depend_on_device_number(fake_thermal):
    """cooling_device10 sorts before cooling_device2 lexically.

    That numbering trap is how pwm-fan was once misidentified as device11 when
    it is device7, so the order must come from the name, not the path.
    """
    alerts = [t.name for t in hwmon.discover_throttles() if t.is_alert]
    assert alerts == sorted(alerts)


def test_active_reflects_cur_state(fake_thermal):
    found = {t.name: t for t in hwmon.discover_throttles()}
    assert found["gpu-throttle-alert"].active() is True
    assert found["cpu-throttle-alert"].active() is False


def test_is_alert_distinguishes_device_kinds(fake_thermal):
    found = {t.name: t for t in hwmon.discover_throttles()}
    assert found["gpu-throttle-alert"].is_alert is True
    assert found["devfreq-gpu-gpc-0"].is_alert is False


def test_unreadable_state_is_not_active(fake_thermal):
    alert = next(t for t in hwmon.discover_throttles() if t.is_alert)
    os.remove(os.path.join(alert.path, "cur_state"))
    assert alert.cur_state is None
    assert alert.active() is False


def test_no_cooling_devices_yields_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(hwmon, "THERMAL_ROOT", str(tmp_path / "empty"))
    assert hwmon.discover_throttles() == []
