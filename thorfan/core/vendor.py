"""Coordination with NVIDIA's nvfancontrol daemon.

Two independent controllers writing the same pwm1 node produce oscillation, so
ThorFAN stops nvfancontrol before taking over and restarts it on release.

The stock profile lives at /etc/nvfancontrol.conf. Notably that file belongs to
no dpkg package (the package ships templates under /etc/nvpower/nvfancontrol/
and nvpower copies the board-matched one into place), so editing it is safe
from package upgrades but may be reset by reflashing.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass

SERVICE = "nvfancontrol"
VENDOR_CONF = "/etc/nvfancontrol.conf"
VENDOR_CONF_BACKUP = "/etc/nvfancontrol.conf.thorfan-backup"

# Time allowed for the daemon to release or reclaim the fan.
_SETTLE_S = 1.0


class ServiceError(RuntimeError):
    """Raised when systemctl fails or is unavailable."""


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    if shutil.which("systemctl") is None:
        raise ServiceError("systemctl not found; unsupported init system")
    return subprocess.run(
        ["systemctl", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def is_active() -> bool:
    """True when nvfancontrol currently owns the fan."""
    try:
        return _systemctl("is-active", "--quiet", SERVICE).returncode == 0
    except (ServiceError, subprocess.TimeoutExpired):
        return False


def stop() -> None:
    """Stop the vendor daemon so ThorFAN can drive the fan."""
    result = _systemctl("stop", SERVICE)
    if result.returncode != 0:
        raise ServiceError(f"failed to stop {SERVICE}: {result.stderr.strip()}")
    time.sleep(_SETTLE_S)


def start() -> None:
    """Hand the fan back to the vendor daemon.

    Called on every exit path, including crashes, so the board is never left
    with an unmanaged fan.
    """
    result = _systemctl("start", SERVICE)
    if result.returncode != 0:
        raise ServiceError(f"failed to start {SERVICE}: {result.stderr.strip()}")


@dataclass(frozen=True)
class VendorProfilePoint:
    """One row of an nvfancontrol FAN_PROFILE table.

    `tmargin_c` is headroom below the group's max temperature, not an absolute
    temperature, when the profile declares TMARGIN ENABLED. Smaller means
    hotter. This inversion is the most common source of confusion when hand
    editing these files.
    """

    tmargin_c: float
    hysteresis: float
    pwm: int
    rpm: int


def parse_profile(text: str, profile: str = "cool") -> list[VendorProfilePoint]:
    """Extract one FAN_PROFILE table from nvfancontrol.conf contents."""
    points: list[VendorProfilePoint] = []
    inside = False
    depth = 0

    for line in text.splitlines():
        stripped = line.strip()
        if not inside:
            if stripped.startswith(f"FAN_PROFILE {profile}"):
                inside = True
                depth = stripped.count("{")
            continue

        depth += stripped.count("{")
        depth -= stripped.count("}")
        if depth <= 0:
            break

        if not stripped or stripped.startswith("#"):
            continue

        fields = stripped.split()
        if len(fields) < 4:
            continue
        try:
            points.append(VendorProfilePoint(
                tmargin_c=float(fields[0]),
                hysteresis=float(fields[1]),
                pwm=int(fields[2]),
                rpm=int(fields[3]),
            ))
        except ValueError:
            continue

    return points


def read_profile(path: str = VENDOR_CONF, profile: str = "cool") -> list[VendorProfilePoint]:
    """Read and parse a profile from disk; empty list when unreadable."""
    try:
        with open(path, "r") as fh:
            return parse_profile(fh.read(), profile)
    except OSError:
        return []


def backup_vendor_conf(path: str = VENDOR_CONF) -> str:
    """Copy the vendor config aside before any modification."""
    shutil.copy2(path, VENDOR_CONF_BACKUP)
    return VENDOR_CONF_BACKUP
