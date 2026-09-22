"""Hardware abstraction for Jetson PWM fan and thermal sensors.

Discovers sysfs nodes by hwmon `name` rather than hard-coded indices, because
hwmon numbering is not stable across boots.

Verified on NVIDIA Jetson AGX Thor Developer Kit, L4T R38.4.0:
    pwm-fan      -> pwm1 (0-255), pwm1_enable
    pwm_tach     -> rpm (read-only)
    thermal_zone -> tj/gpu/cpu/soc012/soc345

IMPORTANT semantics discovered by measurement:
  * pwm1_enable == 0 does NOT mean "manual mode"; it switches the fan OFF.
    Writing pwm1 while disabled has no effect on actual speed.
  * To drive the fan manually the order must be: write pwm1, THEN enable=1.
  * Measured range: pwm 77 -> ~3630 RPM, pwm 255 -> ~13650 RPM.
    The stock nvfancontrol profile caps the closed loop at 5371 RPM,
    leaving roughly 60% of the fan's capability unused.
"""

from __future__ import annotations

import errno
import glob
import os
import time
from dataclasses import dataclass

HWMON_ROOT = "/sys/class/hwmon"
THERMAL_ROOT = "/sys/class/thermal"

PWM_MIN = 0
PWM_MAX = 255

# Tegra thermal sensors intermittently return EAGAIN while a sample is in
# flight. Observed on thermal_zone*/temp under load: a plain read fails
# roughly one time in several. Retrying after a short pause always succeeds.
#
# Separately, a zone can be unreadable for as long as its hardware is
# power-gated. gpu-thermal returns EAGAIN on every read while the GPU is idle
# and railgated, so retries cannot help; see ThermalZone.try_temp_c.
_EAGAIN_RETRIES = 5
_EAGAIN_BACKOFF_S = 0.002


class HardwareError(RuntimeError):
    """Raised when expected sysfs nodes are missing or unreadable."""


def _read(path: str) -> str:
    """Read a sysfs attribute, retrying on EAGAIN.

    Uses unbuffered os.read because Python's buffered text layer masks
    EAGAIN as a confusing TypeError instead of surfacing the OSError.
    """
    last: OSError | None = None
    for attempt in range(_EAGAIN_RETRIES):
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY)
            return os.read(fd, 256).decode("ascii", "replace").strip()
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            last = exc
            time.sleep(_EAGAIN_BACKOFF_S * (attempt + 1))
        finally:
            if fd is not None:
                os.close(fd)
    raise HardwareError(f"{path} kept returning EAGAIN") from last


def _read_once(path: str) -> str:
    """Read a sysfs attribute without retrying.

    For callers that would rather miss a sample than block: a power-gated zone
    fails every attempt, so the retry ladder is 30 ms of pure waiting.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.read(fd, 256).decode("ascii", "replace").strip()
    finally:
        os.close(fd)


def _read_int(path: str) -> int:
    return int(_read(path))


def _read_int_once(path: str) -> int:
    return int(_read_once(path))


def _find_hwmon(name: str) -> str | None:
    """Return the hwmon directory whose `name` file matches, or None."""
    for entry in sorted(glob.glob(os.path.join(HWMON_ROOT, "hwmon*"))):
        name_file = os.path.join(entry, "name")
        try:
            if _read(name_file) == name:
                return entry
        except OSError:
            continue
    return None


@dataclass(frozen=True)
class ThermalZone:
    """A single thermal zone with its trip points."""

    name: str
    path: str
    critical_mc: int | None

    @property
    def temp_mc(self) -> int:
        """Current temperature in millidegrees Celsius."""
        return _read_int(os.path.join(self.path, "temp"))

    @property
    def temp_c(self) -> float:
        return self.temp_mc / 1000.0

    def try_temp_c(self) -> float | None:
        """Temperature, or None when the sensor is currently unavailable.

        Some zones are not merely slow but unreadable for long stretches:
        gpu-thermal returns EAGAIN indefinitely while the GPU is power-gated
        at idle, and starts working again once the GPU is in use. Retrying
        cannot fix that, so callers need to skip the zone rather than fail.

        A single unretried read is used here. The retry ladder exists for the
        intermittent EAGAIN of a sample in flight, but for a gated zone every
        attempt fails and the backoff is 30 ms of pure waiting. At a 1 Hz poll
        that is invisible; for a display refreshing several times a second it
        dominates the frame time. A caller that misses one sample of a
        transiently busy sensor simply picks it up on the next tick.
        """
        try:
            return _read_int_once(os.path.join(self.path, "temp")) / 1000.0
        except (HardwareError, OSError, ValueError):
            return None

    @property
    def critical_c(self) -> float | None:
        return None if self.critical_mc is None else self.critical_mc / 1000.0

    def margin_c(self) -> float | None:
        """Headroom to the critical trip point, in degrees Celsius.

        nvfancontrol's TMARGIN mode keys its profile off this value rather
        than absolute temperature, so we expose it directly.
        """
        crit = self.critical_c
        if crit is None:
            return None
        temp = self.try_temp_c()
        if temp is None:
            return None
        return crit - temp


def discover_zones() -> list[ThermalZone]:
    """Enumerate thermal zones and their critical trip points."""
    zones: list[ThermalZone] = []
    for path in sorted(glob.glob(os.path.join(THERMAL_ROOT, "thermal_zone*"))):
        try:
            name = _read(os.path.join(path, "type"))
        except OSError:
            continue

        critical: int | None = None
        for trip in sorted(glob.glob(os.path.join(path, "trip_point_*_type"))):
            try:
                if _read(trip) != "critical":
                    continue
                temp_file = trip.replace("_type", "_temp")
                critical = _read_int(temp_file)
                break
            except OSError:
                continue

        zones.append(ThermalZone(name=name, path=path, critical_mc=critical))
    return zones


@dataclass(frozen=True)
class PowerRail:
    """One INA3221 channel: a named supply rail with voltage and current.

    Only channels carrying an `inN_label` are exposed. The remaining inN nodes
    are shunt voltages and a sum, not independent rails, and presenting them
    as power would double-count.
    """

    name: str
    path: str
    index: int

    @property
    def millivolts(self) -> int | None:
        try:
            return _read_int(os.path.join(self.path, f"in{self.index}_input"))
        except (HardwareError, OSError, ValueError):
            return None

    @property
    def milliamps(self) -> int | None:
        try:
            return _read_int(os.path.join(self.path, f"curr{self.index}_input"))
        except (HardwareError, OSError, ValueError):
            return None

    def watts(self) -> float | None:
        """Instantaneous power, or None when either reading is unavailable."""
        mv = self.millivolts
        ma = self.milliamps
        if mv is None or ma is None:
            return None
        return mv * ma / 1_000_000.0


def discover_rails() -> list[PowerRail]:
    """Enumerate labelled INA3221 power rails.

    Power is worth showing next to temperature because it explains why the
    temperature will not come down: on a board whose heatsink is saturated,
    the rail draw is the actual lever, not the fan.
    """
    root = _find_hwmon("ina3221")
    if root is None:
        return []

    rails: list[PowerRail] = []
    for label_file in sorted(glob.glob(os.path.join(root, "in*_label"))):
        base = os.path.basename(label_file)
        try:
            index = int(base[2:-len("_label")])
        except ValueError:
            continue
        # curr<N>_input is what makes a channel a rail rather than a shunt
        # voltage reading, so require it.
        if not os.path.exists(os.path.join(root, f"curr{index}_input")):
            continue
        try:
            name = _read(label_file)
        except OSError:
            continue
        if not name or "shunt" in name.lower():
            continue
        rails.append(PowerRail(name=name, path=root, index=index))
    return rails


@dataclass(frozen=True)
class ThrottleAlert:
    """A thermal cooling device reporting throttle state or a frequency cap.

    Alert devices ('gpu-throttle-alert') step 0..2 and are the most direct
    evidence of throttling available; frequency devices ('devfreq-gpu-gpc-0')
    report the current cap out of max_state.
    """

    name: str
    path: str
    max_state: int

    @property
    def cur_state(self) -> int | None:
        try:
            return _read_int(os.path.join(self.path, "cur_state"))
        except (HardwareError, OSError, ValueError):
            return None

    @property
    def is_alert(self) -> bool:
        """True for throttle alerts, as opposed to frequency cap devices."""
        return self.name.endswith("-throttle-alert")

    def active(self) -> bool:
        """True when this device is currently throttling something."""
        state = self.cur_state
        return state is not None and state > 0


def discover_throttles() -> list[ThrottleAlert]:
    """Enumerate throttle alerts and frequency cap cooling devices.

    Skips the fan's own cooling device: it is an output of fan control, not a
    symptom, and showing it as a throttle would be misleading.

    Note the numbering cannot be trusted to sort: cooling_device10 sorts
    before cooling_device2 lexically, which is how pwm-fan was once misread as
    device11 when it is device7.
    """
    found: list[ThrottleAlert] = []
    for path in glob.glob(os.path.join(THERMAL_ROOT, "cooling_device*")):
        try:
            name = _read(os.path.join(path, "type"))
            max_state = _read_int(os.path.join(path, "max_state"))
        except (HardwareError, OSError, ValueError):
            continue
        if name == "pwm-fan":
            continue
        found.append(ThrottleAlert(name=name, path=path, max_state=max_state))

    # Alerts first, then frequency caps, each alphabetically: a stable order
    # the UI can rely on regardless of sysfs enumeration.
    return sorted(found, key=lambda d: (not d.is_alert, d.name))


class FanDevice:
    """Read/write access to the PWM fan and its tachometer.

    Writes require root. Reads work as an unprivileged user.
    """

    def __init__(self) -> None:
        pwm_dir = _find_hwmon("pwmfan")
        if pwm_dir is None:
            raise HardwareError(
                "No 'pwmfan' hwmon device found. "
                "This tool targets Jetson platforms with a PWM fan."
            )
        self._pwm = os.path.join(pwm_dir, "pwm1")
        self._enable = os.path.join(pwm_dir, "pwm1_enable")

        tach_dir = _find_hwmon("pwm_tach")
        self._rpm = os.path.join(tach_dir, "rpm") if tach_dir else None

    # ---- read paths -------------------------------------------------

    @property
    def pwm(self) -> int:
        return _read_int(self._pwm)

    @property
    def enabled(self) -> bool:
        """False means the fan is switched off, not 'manual mode'."""
        return _read_int(self._enable) != 0

    @property
    def rpm(self) -> int | None:
        """Measured speed, or None when no tachometer is present."""
        if self._rpm is None:
            return None
        try:
            return _read_int(self._rpm)
        except OSError:
            return None

    # ---- write paths ------------------------------------------------

    def set_pwm(self, value: int) -> None:
        """Set duty cycle and ensure the fan output stage is enabled.

        The write order matters: the kernel driver latches pwm1 only while
        the output is enabled, so a value written during enable==0 is lost.
        We therefore write the duty cycle first and enable afterwards,
        which also avoids a transient full-speed burst.
        """
        if not PWM_MIN <= value <= PWM_MAX:
            raise ValueError(f"pwm must be {PWM_MIN}-{PWM_MAX}, got {value}")
        self._write(self._pwm, value)
        self._write(self._enable, 1)

    def set_enabled(self, enabled: bool) -> None:
        """Enable or switch off the fan output stage."""
        self._write(self._enable, 1 if enabled else 0)

    def _write(self, path: str, value: int) -> None:
        try:
            with open(path, "w") as fh:
                fh.write(str(value))
        except PermissionError as exc:
            raise HardwareError(f"root required to write {path}") from exc
        except OSError as exc:
            raise HardwareError(f"failed writing {value} to {path}: {exc}") from exc
