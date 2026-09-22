"""The ThorFAN control loop, run as a privileged daemon.

Ownership of the fan is transferred explicitly: nvfancontrol is stopped on
entry and restarted on every exit path. The context manager guarantees this
even on unhandled exceptions or SIGTERM.

The daemon also serves a Unix socket so `thorfan set` can change the fan while
it runs. Runtime changes go through the controller rather than writing pwm1
directly, which keeps the thermal interlock in force.
"""

from __future__ import annotations

import logging
import math
import signal
import threading
from types import FrameType

from . import vendor
from .config import Config
from .control import ControlError, ControlServer, SOCKET_PATH
from .hwmon import (
    PWM_MAX,
    PWM_MIN,
    FanDevice,
    discover_rails,
    discover_throttles,
    discover_zones,
)
from .policy import ControllerState, FanController, Mode

log = logging.getLogger("thorfan")


class FanOwnership:
    """Holds the fan away from nvfancontrol for the duration of a block.

    Restoring the vendor daemon is the single most important safety property
    of this tool: leaving the fan unmanaged on a board that runs at 108 C
    risks hardware damage.

    Ownership can also be taken and released mid-flight, because the mode is
    switchable at runtime. `acquire` and `release` are idempotent.
    """

    def __init__(self, fan: FanDevice, take_over: bool) -> None:
        self._fan = fan
        self._take_over = take_over
        self._stopped_vendor = False

    @property
    def owned(self) -> bool:
        return self._stopped_vendor

    def acquire(self) -> None:
        """Stop nvfancontrol so ThorFAN can drive the fan."""
        if self._stopped_vendor or not vendor.is_active():
            return
        log.info("stopping %s to take over the fan", vendor.SERVICE)
        vendor.stop()
        self._stopped_vendor = True

    def release(self) -> None:
        """Hand the fan back, forcing full speed if that fails."""
        if not self._stopped_vendor:
            return
        log.info("restoring %s", vendor.SERVICE)
        try:
            vendor.start()
            self._stopped_vendor = False
        except vendor.ServiceError:
            # Last resort: if the vendor daemon will not come back, leave
            # the fan at full speed rather than at whatever we last set.
            log.exception("could not restart %s; forcing full speed",
                          vendor.SERVICE)
            try:
                self._fan.set_pwm(PWM_MAX)
            except Exception:
                log.exception("failed to force full speed")

    def __enter__(self) -> FanOwnership:
        if self._take_over:
            self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False  # never suppress exceptions


class Daemon:
    """Polls sensors and applies the configured policy until stopped.

    Accepts runtime commands over a Unix socket so settings can be changed
    without a restart. Commands mutate the controller under a lock and then
    wake the loop, so a change is reflected within milliseconds instead of at
    the next poll.
    """

    def __init__(self, config: Config | None = None,
                 socket_path: str = SOCKET_PATH,
                 fan: FanDevice | None = None,
                 zones: list | None = None) -> None:
        # fan and zones are injectable so the control logic can be tested
        # without Jetson sysfs present.
        self._config = config or Config.load()
        self._fan = fan or FanDevice()
        self._zones = zones or discover_zones()
        # Rails and throttle alerts are observational: they explain why the
        # temperature is what it is, but never feed control decisions, so a
        # board without them simply reports none.
        self._rails = discover_rails()
        self._throttles = discover_throttles()
        self._controller = FanController(
            self._fan, self._zones, self._config.curve
        )
        self._controller.set_mode(self._config.mode)
        self._controller.set_manual_pwm(self._config.manual_pwm)
        self._wake = threading.Event()
        self._last: ControllerState | None = None
        self._socket_path = socket_path
        self._ownership: FanOwnership | None = None
        # Guards controller mutation against the control thread. The control
        # loop holds it only while stepping, which is a pair of sysfs writes.
        self._lock = threading.Lock()

    @property
    def state(self) -> ControllerState | None:
        return self._last

    def request_stop(self, signum: int | None = None,
                     frame: FrameType | None = None) -> None:
        """Signal-safe shutdown request."""
        if signum is not None:
            log.info("received signal %s, shutting down", signum)
        self._wake.set()

    # ---- runtime control ---------------------------------------------

    def handle_request(self, request: dict) -> dict:
        """Apply one control request. Runs on the control thread.

        Raises on invalid input; ControlServer turns that into an error reply.
        """
        action = request.get("action")
        if action == "status":
            return {"ok": True, **self._status_payload()}
        if action == "telemetry":
            return {"ok": True, **self._status_payload(),
                    **self._telemetry_payload()}
        if action == "set":
            return self._handle_set(request)
        if action == "stop":
            self.request_stop()
            return {"ok": True, "stopping": True}
        raise ValueError(f"unknown action {action!r}")

    def _handle_set(self, request: dict) -> dict:
        """Change mode, duty cycle, or curve, effective immediately."""
        mode_name = request.get("mode")
        pwm = request.get("pwm")

        mode = None
        if mode_name is not None:
            try:
                mode = Mode(mode_name)
            except ValueError:
                raise ValueError(f"unknown mode {mode_name!r}") from None

        if pwm is not None:
            if isinstance(pwm, bool) or not isinstance(pwm, int):
                raise ValueError("pwm must be an integer")
            if not PWM_MIN <= pwm <= PWM_MAX:
                raise ValueError(f"pwm must be {PWM_MIN}-{PWM_MAX}, got {pwm}")
            if mode is None:
                mode = Mode.MANUAL  # a duty cycle only means anything manually

        if mode is None:
            raise ValueError("nothing to set; give a mode or a pwm value")

        with self._lock:
            # Ownership must change before the next step writes pwm1, and must
            # be released when returning to vendor mode so nvfancontrol can
            # resume rather than the fan being left on our last value.
            if mode is Mode.VENDOR:
                self._release_ownership()
            else:
                self._acquire_ownership()

            if pwm is not None:
                self._controller.set_manual_pwm(pwm)
            self._controller.set_mode(mode)
            self._apply_now()

        log.info("runtime change: mode=%s pwm=%s", mode.value,
                 pwm if pwm is not None else "unchanged")
        return {"ok": True, "applied": True, **self._status_payload()}

    def _acquire_ownership(self) -> None:
        if self._ownership is not None:
            self._ownership.acquire()

    def _release_ownership(self) -> None:
        if self._ownership is not None:
            self._ownership.release()

    def _apply_now(self) -> None:
        """Drive the fan immediately rather than waiting for the next poll.

        Called with the lock held. A failure here is logged and left to the
        control loop to retry, because reporting it to the client is less
        important than keeping the loop alive.
        """
        try:
            self._last = self._controller.step()
        except Exception:
            log.exception("could not apply the change immediately")

    def _status_payload(self) -> dict:
        state = self._last
        if state is None:
            return {"mode": self._controller.mode.value}
        # max_temp_c is NaN when no sensor could be read. json.dumps would
        # emit a bare NaN, which is not valid JSON, so send null instead.
        temp = state.max_temp_c
        payload = {
            "mode": state.mode.value,
            "pwm": state.pwm,
            "rpm": state.rpm,
            "max_temp_c": None if math.isnan(temp) else round(temp, 1),
            "hottest_zone": state.hottest_zone,
            "emergency": state.emergency,
        }
        if state.mode is Mode.VENDOR:
            # In vendor mode pwm is an observation of what nvfancontrol is
            # doing, not a value we chose. Labelling it makes that explicit so
            # callers cannot present it as a ThorFAN setting.
            payload["pwm_is_ours"] = False
        return payload

    def _telemetry_payload(self) -> dict:
        """Everything a live display needs, in one round trip.

        Read fresh rather than from the last control step, because a display
        refreshes faster than the poll interval. Sensors that fail are reported
        as null instead of omitted, so a UI can show 'unavailable' rather than
        silently dropping a row.
        """
        return {
            "zones": [
                {
                    "name": z.name,
                    "temp_c": z.try_temp_c(),
                    "critical_c": z.critical_c,
                }
                for z in self._zones
            ],
            "rails": [
                {"name": r.name, "watts": r.watts()} for r in self._rails
            ],
            "throttles": [
                {
                    "name": t.name,
                    "cur_state": t.cur_state,
                    "max_state": t.max_state,
                    "is_alert": t.is_alert,
                }
                for t in self._throttles
            ],
            "fan": {
                "pwm": self._safe(lambda: self._fan.pwm),
                "rpm": self._safe(lambda: self._fan.rpm),
                "enabled": self._safe(lambda: self._fan.enabled),
            },
            # Whether we own the fan is known locally; asking systemctl costs
            # about 6 ms, which is too much on a display's refresh path.
            "vendor_active": (self._ownership is None
                              or not self._ownership.owned),
        }

    @staticmethod
    def _safe(read):
        """Return a sensor reading, or None when it cannot be read."""
        try:
            return read()
        except Exception:
            return None

    # ---- main loop ----------------------------------------------------

    def run(self, install_signal_handlers: bool = True) -> None:
        """Run the control loop until a stop is requested.

        `install_signal_handlers` must be False when running in a thread:
        signal.signal only works on the main thread, and an embedded daemon
        relies on its host calling request_stop instead.
        """
        take_over = self._config.mode is not Mode.VENDOR

        if install_signal_handlers:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, self.request_stop)

        log.info(
            "starting in %s mode, polling every %.1fs, %d thermal zones",
            self._config.mode.value, self._config.poll_interval_s,
            len(self._zones),
        )

        with FanOwnership(self._fan, take_over) as ownership:
            self._ownership = ownership
            # The control socket is opened inside the ownership block so it
            # never outlives our ability to actually drive the fan. Failing to
            # open it is not fatal: an embedded daemon is still driving the fan
            # correctly even if no external client can connect.
            try:
                server = ControlServer(self.handle_request, self._socket_path)
                server.start()
            except ControlError:
                log.warning("running without a control socket", exc_info=True)
                server = None
            else:
                log.info("accepting runtime commands on %s", server.path)

            try:
                while not self._wake.is_set():
                    with self._lock:
                        try:
                            self._last = self._controller.step()
                        except Exception:
                            # A transient sysfs failure must not kill the loop
                            # and thereby strand the fan; retry next tick.
                            log.exception("control step failed")
                    # Runtime changes are applied synchronously by the control
                    # thread, so the loop only needs to wake for a stop.
                    self._wake.wait(self._config.poll_interval_s)
            finally:
                if server is not None:
                    server.close()
                self._ownership = None

        log.info("stopped")
