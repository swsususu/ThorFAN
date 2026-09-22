"""Persistence for ThorFAN settings.

Config lives at /etc/thorfan/config.json when running as the system daemon,
and falls back to the user's config dir for unprivileged inspection.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .policy import CurvePoint, FanCurve, Mode

SYSTEM_CONFIG = "/etc/thorfan/config.json"


def user_config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "thorfan", "config.json")


def default_config_path() -> str:
    """Where to read and write settings.

    Root always uses the system path, even when the file does not exist yet.
    Falling back to the user path under sudo would write to /root/.config,
    which looks like it works (the daemon is also root) but leaves the setting
    somewhere no one expects and which a different privileged caller, or a
    daemon started with a clean environment, would not read.

    Unprivileged callers get the system config when one exists, so that
    `thorfan status` reflects what the daemon is actually using, and their own
    config directory otherwise.
    """
    if os.geteuid() == 0:
        return SYSTEM_CONFIG
    if os.path.exists(SYSTEM_CONFIG):
        return SYSTEM_CONFIG
    return user_config_path()


@dataclass
class Config:
    """User-facing settings, independent of hardware state."""

    mode: Mode = Mode.VENDOR
    manual_pwm: int = 128
    curve: FanCurve = None  # type: ignore[assignment]
    poll_interval_s: float = 2.0

    def __post_init__(self) -> None:
        if self.curve is None:
            self.curve = FanCurve.default()
        if self.poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")

    # ---- serialisation ----------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "mode": self.mode.value,
            "manual_pwm": self.manual_pwm,
            "poll_interval_s": self.poll_interval_s,
            "curve": [
                {"temp_c": p.temp_c, "pwm": p.pwm} for p in self.curve.points
            ],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Config:
        """Build a config from parsed JSON, falling back to defaults.

        Unknown modes and malformed curves degrade to safe defaults rather
        than raising, so a hand-edited file cannot prevent startup.
        """
        try:
            mode = Mode(raw.get("mode", Mode.VENDOR.value))
        except ValueError:
            mode = Mode.VENDOR

        points = raw.get("curve") or []
        curve: FanCurve
        try:
            curve = FanCurve([
                CurvePoint(float(p["temp_c"]), int(p["pwm"])) for p in points
            ])
        except (KeyError, TypeError, ValueError):
            curve = FanCurve.default()

        manual = raw.get("manual_pwm", 128)
        if not isinstance(manual, int) or not 0 <= manual <= 255:
            manual = 128

        interval = raw.get("poll_interval_s", 2.0)
        if not isinstance(interval, (int, float)) or interval <= 0:
            interval = 2.0

        return cls(
            mode=mode,
            manual_pwm=manual,
            curve=curve,
            poll_interval_s=float(interval),
        )

    # ---- io ----------------------------------------------------------

    def save(self, path: str | None = None) -> str:
        target = path or default_config_path()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = f"{target}.tmp"
        with open(tmp, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, target)  # atomic, so a crash cannot truncate config
        return target

    @classmethod
    def load(cls, path: str | None = None) -> Config:
        target = path or default_config_path()
        try:
            with open(target, "r") as fh:
                return cls.from_dict(json.load(fh))
        except FileNotFoundError:
            return cls()
        except (json.JSONDecodeError, OSError):
            # A corrupt config must not block the fan from being managed.
            return cls()
