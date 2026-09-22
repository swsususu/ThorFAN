"""Tests for the runtime control channel and speed parsing.

These cover the pieces that make `thorfan set 90%` take effect immediately:
the Unix socket protocol, the singleton guard, and the request handler's
validation. Hardware access is faked, so they run anywhere.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from thorfan.cli import _parse_speed
from thorfan.core import control
from thorfan.core.hwmon import PWM_MAX, HardwareError
from thorfan.core.policy import Mode

import argparse


# ---- speed parsing ----------------------------------------------------


def test_percentage_maps_to_pwm():
    assert _parse_speed("100%") == 255
    assert _parse_speed("90%") == 230   # 0.9 * 255 = 229.5, rounds to 230
    assert _parse_speed("0%") == 0


def test_percentage_accepts_fractions_and_spacing():
    assert _parse_speed(" 50.5% ") == 129


def test_bare_number_is_pwm_not_percent():
    """90 must mean pwm 90, not 90%; sysfs conventions win for bare numbers."""
    assert _parse_speed("90") == 90
    assert _parse_speed("255") == 255


def test_rejects_out_of_range_percentage():
    with pytest.raises(argparse.ArgumentTypeError, match="0-100"):
        _parse_speed("150%")


def test_rejects_out_of_range_pwm():
    with pytest.raises(argparse.ArgumentTypeError, match="pwm must be"):
        _parse_speed("300")


def test_rejects_nonsense():
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_speed("fast")


# ---- socket protocol --------------------------------------------------


@pytest.fixture
def server_path(tmp_path):
    return str(tmp_path / "thorfan.sock")


def _server(handler, path: str, **kwargs):
    """A ControlServer that does not try to chown to a real group.

    Tests run unprivileged, so a group chown would only produce warnings; the
    permission behaviour has its own dedicated tests below.
    """
    kwargs.setdefault("group", None)
    return control.ControlServer(handler, path, **kwargs)


def test_send_receives_handler_reply(server_path):
    with _server(lambda req: {"ok": True, "echo": req}, server_path):
        reply = control.send({"action": "status"}, server_path)
    assert reply["echo"] == {"action": "status"}


def test_send_raises_when_no_daemon(server_path):
    with pytest.raises(control.DaemonNotRunning):
        control.send({"action": "status"}, server_path)


def test_handler_error_becomes_control_error(server_path):
    def boom(req):
        raise ValueError("unknown mode 'telepathy'")

    with _server(boom, server_path):
        with pytest.raises(control.ControlError, match="telepathy"):
            control.send({"action": "set"}, server_path)


def test_handler_exception_does_not_kill_the_server(server_path):
    """A bad request must not take down the channel keeping the fan managed."""
    calls: list[dict] = []

    def handler(req):
        calls.append(req)
        if req.get("bad"):
            raise RuntimeError("nope")
        return {"ok": True}

    with _server(handler, server_path):
        with pytest.raises(control.ControlError):
            control.send({"bad": True}, server_path)
        assert control.send({"action": "status"}, server_path)["ok"] is True
    assert len(calls) == 2


def test_malformed_request_is_rejected_cleanly(server_path):
    with _server(lambda req: {"ok": True}, server_path):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(server_path)
            sock.sendall(b"not json at all\n")
            reply = json.loads(sock.recv(4096).decode().strip())
    assert reply["ok"] is False
    assert "malformed" in reply["error"]


def test_second_daemon_is_refused(server_path):
    """Two daemons writing pwm1 would oscillate, so startup must abort."""
    with _server(lambda req: {"ok": True}, server_path):
        with pytest.raises(control.AlreadyRunning):
            _server(lambda req: {"ok": True}, server_path).start()


def test_stale_socket_is_reclaimed(server_path, tmp_path):
    """A SIGKILLed daemon leaves a socket file behind; it must not block."""
    # Bind and abandon without unlinking, simulating a hard kill.
    orphan = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    orphan.bind(server_path)
    orphan.close()  # file remains, nothing listening

    with _server(lambda req: {"ok": True}, server_path):
        assert control.send({"action": "status"}, server_path)["ok"] is True


def test_socket_is_root_only_without_the_group(server_path, monkeypatch):
    """Widening access must be opt in via an existing 'thorfan' group."""
    import os
    import stat

    with control.ControlServer(lambda req: {"ok": True}, server_path,
                               group=None):
        mode = stat.S_IMODE(os.stat(server_path).st_mode)
    assert mode == 0o600


def test_missing_group_leaves_the_socket_root_only(server_path):
    """A group that does not exist must not be an error."""
    import os
    import stat

    with control.ControlServer(lambda req: {"ok": True}, server_path,
                               group="definitely-not-a-real-group"):
        mode = stat.S_IMODE(os.stat(server_path).st_mode)
        # Still serving, just not group-accessible.
        assert control.send({"action": "status"}, server_path)["ok"] is True
    assert mode == 0o600


def test_existing_group_widens_access(server_path, monkeypatch):
    """With the group present the socket becomes group-writable, for the TUI."""
    import grp
    import os
    import stat

    class FakeGroup:
        gr_gid = os.getgid()

    monkeypatch.setattr(control.grp, "getgrnam", lambda name: FakeGroup)

    with control.ControlServer(lambda req: {"ok": True}, server_path,
                               group="thorfan") as srv:
        mode = stat.S_IMODE(os.stat(server_path).st_mode)
    assert mode == 0o660


def test_close_removes_the_socket(server_path):
    import os

    server = _server(lambda req: {"ok": True}, server_path)
    server.start()
    assert os.path.exists(server_path)
    server.close()
    assert not os.path.exists(server_path)


def test_concurrent_requests_are_all_answered(server_path):
    """The GUI will poll while a user drags a slider; both must work."""
    results: list[bool] = []

    def worker():
        try:
            results.append(control.send({"action": "status"}, server_path)["ok"])
        except control.ControlError:
            results.append(False)

    with _server(lambda req: {"ok": True}, server_path):
        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    assert results == [True] * 5


# ---- daemon request handling -----------------------------------------


class FakeFan:
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
    def __init__(self, name: str, temp_c: float | None = 0.0,
                 critical_c: float | None = 114.5) -> None:
        self.name = name
        self._temp_c = temp_c
        self.critical_c = critical_c

    @property
    def temp_c(self) -> float:
        if self._temp_c is None:
            raise HardwareError(f"{self.name} is power-gated")
        return self._temp_c

    def try_temp_c(self) -> float | None:
        return self._temp_c

    def set(self, temp_c: float | None) -> None:
        self._temp_c = temp_c


@pytest.fixture(autouse=True)
def no_real_systemctl(monkeypatch):
    """Keep tests off the host's nvfancontrol service.

    Without this, constructing a Daemon in a driving mode tries to stop the
    real vendor daemon, which both fails without authentication and would be
    an unpleasant side effect of running the test suite on a Jetson.
    """
    from thorfan.core import vendor

    monkeypatch.setattr(vendor, "is_active", lambda: False)
    monkeypatch.setattr(vendor, "stop", lambda: None)
    monkeypatch.setattr(vendor, "start", lambda: None)


def _daemon(temp_c: float = 60.0, mode: Mode = Mode.CURVE):
    """A Daemon with faked hardware, for exercising request handling."""
    from thorfan.core.config import Config
    from thorfan.core.daemon import Daemon

    fan = FakeFan()
    zone = FakeZone("tj-thermal", temp_c)
    daemon = Daemon(
        config=Config(mode=mode),
        fan=fan,
        zones=[zone],
    )
    return daemon, fan, zone


def test_set_pwm_applies_immediately():
    """The point of the whole channel: no waiting for the next poll."""
    daemon, fan, _ = _daemon()
    reply = daemon.handle_request({"action": "set", "pwm": 230})
    assert reply["ok"] is True
    assert fan.writes == [230]
    assert reply["mode"] == "manual"
    assert reply["pwm"] == 230


def test_pwm_alone_implies_manual_mode():
    daemon, _, _ = _daemon()
    daemon.handle_request({"action": "set", "pwm": 200})
    assert daemon.handle_request({"action": "status"})["mode"] == "manual"


def test_set_mode_curve_resumes_the_curve():
    daemon, fan, _ = _daemon(temp_c=85.0)
    daemon.handle_request({"action": "set", "pwm": 0})
    assert fan.writes[-1] == 0
    daemon.handle_request({"action": "set", "mode": "curve"})
    # The default curve gives 165 at 85 C.
    assert fan.writes[-1] == 165


def test_vendor_mode_stops_writing():
    daemon, fan, _ = _daemon()
    daemon.handle_request({"action": "set", "mode": "vendor"})
    before = len(fan.writes)
    daemon.handle_request({"action": "status"})
    assert len(fan.writes) == before


def test_emergency_override_survives_a_runtime_set():
    """A user asking for a stopped fan at 105 C must still be overruled."""
    daemon, fan, _ = _daemon(temp_c=105.0)
    reply = daemon.handle_request({"action": "set", "pwm": 0})
    assert fan.writes[-1] == PWM_MAX
    assert reply["emergency"] is True


def test_rejects_unknown_action():
    daemon, _, _ = _daemon()
    with pytest.raises(ValueError, match="unknown action"):
        daemon.handle_request({"action": "levitate"})


def test_rejects_unknown_mode():
    daemon, _, _ = _daemon()
    with pytest.raises(ValueError, match="unknown mode"):
        daemon.handle_request({"action": "set", "mode": "telepathy"})


def test_rejects_out_of_range_pwm():
    daemon, _, _ = _daemon()
    with pytest.raises(ValueError, match="pwm must be"):
        daemon.handle_request({"action": "set", "pwm": 900})


def test_rejects_non_integer_pwm():
    daemon, _, _ = _daemon()
    with pytest.raises(ValueError, match="integer"):
        daemon.handle_request({"action": "set", "pwm": "fast"})


def test_rejects_boolean_pwm():
    """True is an int in Python; it must not be accepted as a duty cycle."""
    daemon, _, _ = _daemon()
    with pytest.raises(ValueError, match="integer"):
        daemon.handle_request({"action": "set", "pwm": True})


def test_rejects_empty_set():
    daemon, _, _ = _daemon()
    with pytest.raises(ValueError, match="nothing to set"):
        daemon.handle_request({"action": "set"})


def test_stop_action_requests_shutdown():
    daemon, _, _ = _daemon()
    reply = daemon.handle_request({"action": "stop"})
    assert reply["ok"] is True
    assert reply["stopping"] is True


def test_end_to_end_set_over_the_socket(tmp_path):
    """A full round trip: CLI-side send, server, handler, immediate write."""
    path = str(tmp_path / "thorfan.sock")
    daemon, fan, _ = _daemon(temp_c=70.0)

    with _server(daemon.handle_request, path):
        reply = control.send({"action": "set", "pwm": 230}, path)
        assert reply["pwm"] == 230
        assert fan.writes == [230]

        # And a second change lands without a restart.
        reply = control.send({"action": "set", "pwm": 128}, path)
        assert reply["pwm"] == 128
        assert fan.writes == [230, 128]


def test_vendor_mode_marks_pwm_as_not_ours():
    """The pwm seen in vendor mode is nvfancontrol's, not a setting of ours.

    Without this flag the CLI reported 'applied mode=vendor pwm=76 (30%)',
    which reads as though ThorFAN had set 30%.
    """
    daemon, _, _ = _daemon()
    reply = daemon.handle_request({"action": "set", "mode": "vendor"})
    assert reply["mode"] == "vendor"
    assert reply["pwm_is_ours"] is False


def test_driving_modes_do_not_set_the_flag():
    daemon, _, _ = _daemon()
    reply = daemon.handle_request({"action": "set", "pwm": 200})
    assert "pwm_is_ours" not in reply


def test_payload_is_valid_json_when_sensors_fail(tmp_path):
    """NaN is not valid JSON; an unreadable board must still reply cleanly."""
    path = str(tmp_path / "thorfan.sock")
    daemon, _, zone = _daemon()
    zone.set(None)  # every sensor now unreadable

    with _server(daemon.handle_request, path):
        reply = control.send({"action": "set", "pwm": 100}, path)

    assert reply["max_temp_c"] is None
    assert reply["hottest_zone"] == "unknown"
    assert reply["emergency"] is True
    assert reply["pwm"] == PWM_MAX  # blind means full speed


def test_telemetry_returns_everything_a_display_needs(tmp_path):
    path = str(tmp_path / "thorfan.sock")
    daemon, _, _ = _daemon(temp_c=80.0)

    with _server(daemon.handle_request, path):
        reply = control.send({"action": "telemetry"}, path)

    for key in ("zones", "rails", "throttles", "fan", "vendor_active", "mode"):
        assert key in reply
    assert reply["zones"][0]["name"] == "tj-thermal"
    assert reply["zones"][0]["temp_c"] == 80.0


def test_telemetry_reports_unreadable_zones_as_null(tmp_path):
    """A UI needs to show 'unavailable', so the row must not be dropped."""
    path = str(tmp_path / "thorfan.sock")
    daemon, _, zone = _daemon()
    zone.set(None)

    with _server(daemon.handle_request, path):
        reply = control.send({"action": "telemetry"}, path)

    assert reply["zones"][0]["temp_c"] is None


def test_daemon_runs_without_a_control_socket(tmp_path, monkeypatch):
    """An unusable socket must not stop the fan from being managed."""
    from thorfan.core import daemon as daemon_module

    daemon, fan, _ = _daemon(temp_c=70.0, mode=Mode.CURVE)

    def refuse(*args, **kwargs):
        raise control.ControlError("cannot listen")

    monkeypatch.setattr(daemon_module, "ControlServer", refuse)

    stopper = threading.Timer(0.3, daemon.request_stop)
    stopper.start()
    daemon.run(install_signal_handlers=False)
    stopper.cancel()

    # The loop still drove the fan: default curve gives 110 at 70 C.
    assert fan.writes and fan.writes[0] == 110
