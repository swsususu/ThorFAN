"""ThorFAN command line interface.

Read-only subcommands work unprivileged; anything that drives the fan needs
root, which is checked up front with a clear message rather than failing on a
permission error deep in a write.

`set` and `mode` talk to a running daemon over its control socket so changes
take effect immediately. With no daemon running they fall back to persisting
the setting, because a CLI process that wrote pwm1 and exited would leave the
fan unmanaged and without the thermal interlock.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .core import control, vendor
from .core.config import Config
from .core.daemon import Daemon
from .core.hwmon import (
    PWM_MAX,
    PWM_MIN,
    HardwareError,
    FanDevice,
    discover_rails,
    discover_throttles,
    discover_zones,
)
from .core.policy import STOCK_RPM_CAP, Mode


def _require_root(action: str) -> None:
    if os.geteuid() != 0:
        sys.exit(f"thorfan: {action} requires root; re-run with sudo")


def _parse_speed(text: str) -> int:
    """Parse a duty cycle given as a percentage or a raw 0-255 value.

    Percentages are the natural unit for users ('90%'), while pwm is what the
    hardware takes. A bare number is treated as pwm to stay compatible with
    sysfs conventions, so the suffix is what disambiguates.
    """
    raw = text.strip().lower()
    if raw.endswith("%"):
        try:
            percent = float(raw[:-1])
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"invalid percentage: {text!r}") from None
        if not 0.0 <= percent <= 100.0:
            raise argparse.ArgumentTypeError(
                f"percentage must be 0-100, got {percent:g}")
        return round(percent / 100.0 * PWM_MAX)

    try:
        pwm = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a pwm value or a percentage like 90%%, got {text!r}"
        ) from None
    if not PWM_MIN <= pwm <= PWM_MAX:
        raise argparse.ArgumentTypeError(
            f"pwm must be {PWM_MIN}-{PWM_MAX}, got {pwm}")
    return pwm


def _describe(pwm: int) -> str:
    return f"pwm={pwm}/{PWM_MAX} ({pwm / PWM_MAX * 100:.0f}%)"


def cmd_status(args: argparse.Namespace) -> int:
    """Print current fan and thermal state."""
    fan = FanDevice()
    zones = discover_zones()

    rpm = fan.rpm
    print(f"fan     pwm={fan.pwm}/255 enabled={fan.enabled} "
          f"rpm={rpm if rpm is not None else 'n/a'}")
    print(f"vendor  nvfancontrol={'active' if vendor.is_active() else 'inactive'}")

    # Surface who is actually in charge; without this a user cannot tell a
    # live daemon from a stale config.
    try:
        reply = control.send({"action": "status"})
        extra = "  EMERGENCY OVERRIDE ACTIVE" if reply.get("emergency") else ""
        print(f"thorfan daemon running, mode={reply.get('mode')}{extra}")
    except control.DaemonNotRunning:
        print("thorfan daemon not running")
    except control.ControlError as exc:
        # The socket is root-only, so an unprivileged `status` can see that a
        # daemon exists but not query it. Say so plainly rather than reporting
        # a permission error as if something were broken.
        if os.geteuid() != 0 and os.path.exists(control.SOCKET_PATH):
            print("thorfan daemon running (re-run as root to see its mode)")
        else:
            print(f"thorfan daemon status unavailable: {exc}")
    print()

    print(f"{'zone':<18}{'temp':>8}{'critical':>10}{'margin':>9}")
    # A zone that cannot be read is shown rather than hidden: gpu-thermal is
    # unreadable while the GPU is power-gated, and silently dropping it would
    # look like the sensor had disappeared.
    readings = [(z, z.try_temp_c()) for z in zones]
    readings.sort(key=lambda pair: (pair[1] is not None, pair[1] or 0.0),
                  reverse=True)
    for zone, temp in readings:
        crit = zone.critical_c
        if temp is None:
            print(f"{zone.name:<18}{'unavailable':>8}"
                  f"{(f'{crit:.1f}C' if crit else 'n/a'):>10}{'n/a':>9}")
            continue
        margin = None if crit is None else crit - temp
        print(f"{zone.name:<18}{temp:7.1f}C"
              f"{(f'{crit:.1f}C' if crit else 'n/a'):>10}"
              f"{(f'{margin:.1f}C' if margin else 'n/a'):>9}")

    if any(temp is None for _, temp in readings):
        print()
        print("note: a zone reading 'unavailable' is power-gated, not broken; "
              "gpu-thermal does this while the GPU is idle")

    rails = discover_rails()
    if rails:
        print()
        total = 0.0
        for rail in rails:
            watts = rail.watts()
            if watts is None:
                print(f"{rail.name:<20}{'unavailable':>12}")
                continue
            total += watts
            print(f"{rail.name:<20}{watts:10.2f} W")
        print(f"{'total':<20}{total:10.2f} W")

    # Throttle alerts say directly what temperature only implies.
    throttles = [t for t in discover_throttles() if t.is_alert]
    active = [t for t in throttles if t.active()]
    if throttles:
        print()
        if active:
            names = ", ".join(t.name.replace("-throttle-alert", "")
                              for t in active)
            print(f"THROTTLING: {names}")
        else:
            print("throttling  none")

    capped = [t for t in discover_throttles()
              if not t.is_alert and t.active()]
    if capped:
        for cap in capped:
            print(f"  {cap.name} capped at {cap.cur_state}/{cap.max_state}")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    """Show the vendor profile and highlight its RPM ceiling."""
    points = vendor.read_profile(profile=args.profile)
    if not points:
        print(f"no '{args.profile}' profile found in {vendor.VENDOR_CONF}")
        return 1

    print(f"vendor profile '{args.profile}' from {vendor.VENDOR_CONF}")
    print("note: the first column is headroom below the critical temperature,")
    print("      not absolute temperature; smaller values mean hotter.")
    print()
    print(f"{'margin':>8}{'hyst':>6}{'pwm':>6}{'rpm':>8}")
    for p in points:
        print(f"{p.tmargin_c:8.0f}{p.hysteresis:6.0f}{p.pwm:6d}{p.rpm:8d}")

    ceiling = max(p.rpm for p in points)
    if ceiling <= STOCK_RPM_CAP:
        print()
        print(f"this profile never asks for more than {ceiling} RPM, while the")
        print("fan has been measured at roughly 13650 RPM at pwm 255")
    return 0


def _start_daemon_for(mode: Mode, pwm: int | None) -> int:
    """Start a control loop with the requested setting already applied.

    `set` and `mode` need a running loop, because the 100 C interlock lives
    there rather than in the write itself. Rather than telling the user to
    start one in another terminal, start it here and run in the foreground,
    which keeps the interlock alive and restores nvfancontrol on exit.
    """
    config = Config.load()
    config.mode = mode
    if pwm is not None:
        config.manual_pwm = pwm

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    target = _describe(pwm) if pwm is not None else f"mode {mode.value}"
    print(f"no daemon running; starting one with {target}")
    print("the fan returns to nvfancontrol when this exits (Ctrl-C)")
    print()
    try:
        Daemon(config=config).run()
    except control.AlreadyRunning as exc:
        # Another daemon appeared between our probe and this call.
        sys.exit(f"thorfan: {exc}")
    return 0


def _apply_or_persist(mode: Mode, pwm: int | None, persist: bool) -> int:
    """Send a change to a running daemon, or start one to apply it.

    Deliberately never writes pwm1 directly: the interlock that overrides a
    dangerous setting above 100 C lives in the daemon's loop, and a fan left at
    a fixed duty cycle by a process that has exited has no interlock at all.
    """
    request: dict = {"action": "set", "mode": mode.value}
    if pwm is not None:
        request["pwm"] = pwm

    try:
        reply = control.send(request)
    except control.DaemonNotRunning:
        if persist:
            # An explicit --save means the user wants the setting recorded for
            # the service to pick up, not a foreground loop right now.
            config = Config.load()
            config.mode = mode
            if pwm is not None:
                config.manual_pwm = pwm
            path = config.save()
            target = _describe(pwm) if pwm is not None else f"mode={mode.value}"
            print(f"no daemon running; saved {target} to {path}")
            print("it applies when the daemon or service next starts")
            return 0
        if mode is Mode.VENDOR:
            # Nothing to run: vendor mode is what the board does by itself.
            vendor.start()
            print(f"{vendor.SERVICE} is driving the fan")
            return 0
        return _start_daemon_for(mode, pwm)

    if mode is Mode.VENDOR:
        # We are not driving the fan any more, so reporting a duty cycle would
        # imply we had set it; that number is just whatever nvfancontrol has.
        print(f"applied mode=vendor; {vendor.SERVICE} is driving the fan again")
    else:
        applied = _describe(reply["pwm"]) if reply.get("pwm") is not None else ""
        print(f"applied mode={reply.get('mode')} {applied}".rstrip())
        # The tachometer lags a change by several seconds, so the rpm read
        # right now is the *previous* speed. Printing it as the result makes a
        # correct change look like it went the wrong way.
        print(f"{reply.get('hottest_zone')} {reply.get('max_temp_c')}C; "
              f"run 'thorfan status' in a few seconds for the settled rpm")

    if reply.get("emergency"):
        print("note: the 100C safety override is active, so the fan is at "
              "full speed regardless of this setting")

    if persist:
        config = Config.load()
        config.mode = mode
        if pwm is not None:
            config.manual_pwm = pwm
        print(f"persisted to {config.save()}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    """Apply a fixed duty cycle immediately when a daemon is running."""
    _require_root("setting the fan speed")
    return _apply_or_persist(Mode.MANUAL, args.speed, args.save)


def cmd_mode(args: argparse.Namespace) -> int:
    """Switch between vendor, curve, and manual control."""
    _require_root("changing the fan mode")
    return _apply_or_persist(Mode(args.mode), None, args.save)


def cmd_daemon(args: argparse.Namespace) -> int:
    """Run the control loop in the foreground."""
    _require_root("running the control loop")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    try:
        Daemon(socket_path=args.socket).run()
    except control.AlreadyRunning as exc:
        sys.exit(f"thorfan: {exc}")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    """Run the live display.

    Not privileged in itself: it reads sysfs, and control goes through the
    daemon's socket. Whether that socket is reachable depends on the 'thorfan'
    group, so an unprivileged run may end up read-only.
    """
    from .tui import run
    return run()


def cmd_restore(args: argparse.Namespace) -> int:
    """Hand the fan back to nvfancontrol."""
    _require_root("restoring vendor control")

    # Ask a running daemon to stand down first; otherwise it would keep
    # writing pwm1 and immediately fight the vendor daemon we just started.
    try:
        control.send({"action": "set", "mode": Mode.VENDOR.value})
        print("asked the running daemon to release the fan")
        return 0
    except control.DaemonNotRunning:
        pass
    except control.ControlError as exc:
        print(f"could not reach the daemon ({exc}); starting {vendor.SERVICE} anyway")

    vendor.start()
    print(f"{vendor.SERVICE} started; the fan is under vendor control")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thorfan",
        description="Fan control and thermal policy for NVIDIA Jetson AGX Thor",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="show fan and thermal state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("profile", help="show the vendor fan profile")
    p.add_argument("--profile", default="cool", help="profile name (default: cool)")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser(
        "set",
        help="set a fixed fan speed, effective immediately",
        description="Set a fixed fan speed as a percentage ('90%') or a raw "
                    "pwm value (0-255). Applied at once when a daemon is "
                    "running, otherwise saved for the next start.",
    )
    p.add_argument("speed", type=_parse_speed, metavar="SPEED",
                   help="duty cycle: '90%%' or a pwm value 0-255")
    p.add_argument("--save", action="store_true",
                   help="also write the setting to the config file")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("mode", help="switch control mode, effective immediately")
    p.add_argument("mode", choices=[m.value for m in Mode],
                   help="vendor: nvfancontrol drives; curve: follow the "
                        "configured curve; manual: hold a fixed duty cycle")
    p.add_argument("--save", action="store_true",
                   help="also write the setting to the config file")
    p.set_defaults(func=cmd_mode)

    p = sub.add_parser(
        "tui",
        help="live display with interactive control",
        description="Refreshing view of temperatures, fan speed, power rails "
                    "and throttle state. Arrow keys and number keys change "
                    "the fan speed when a daemon is running.",
    )
    p.set_defaults(func=cmd_tui)

    p = sub.add_parser("daemon", help="run the control loop")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--socket", default=control.SOCKET_PATH,
                   help=f"control socket path (default: {control.SOCKET_PATH})")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("restore", help="return control to nvfancontrol")
    p.set_defaults(func=cmd_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except HardwareError as exc:
        sys.exit(f"thorfan: {exc}")
    except vendor.ServiceError as exc:
        sys.exit(f"thorfan: {exc}")
    except control.ControlError as exc:
        sys.exit(f"thorfan: {exc}")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
