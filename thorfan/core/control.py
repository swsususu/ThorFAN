"""Runtime control channel between the CLI and a running daemon.

A Unix socket lets `thorfan set` change the fan immediately while the daemon
keeps its control loop, and therefore its thermal interlock, running. Writing
pwm1 directly from a short-lived CLI process would leave the fan unmanaged the
moment that process exits, which is exactly the failure mode this project
exists to avoid.

The protocol is one JSON object per line: request, then response. The socket is
root-only unless a `thorfan` group exists, in which case it is group-writable
so an unprivileged TUI or GUI can connect. Widening access is opt in, because
anything able to reach the socket can drive the fan.
"""

from __future__ import annotations

import grp
import json
import logging
import os
import socket
import threading
from typing import Callable

SOCKET_PATH = "/run/thorfan.sock"

# When this group exists the socket is group-readable, so a TUI or GUI can run
# unprivileged. Without it the socket stays root-only: widening access is opt
# in, because anything that can reach the socket can drive the fan.
SOCKET_GROUP = "thorfan"

_TIMEOUT_S = 5.0
_MAX_MESSAGE = 64 * 1024
_ACCEPT_POLL_S = 0.5

log = logging.getLogger("thorfan")


class ControlError(RuntimeError):
    """Raised when the control socket is absent or unusable."""


class DaemonNotRunning(ControlError):
    """Nothing is listening on the control socket."""


class AlreadyRunning(ControlError):
    """Another daemon already holds the control socket.

    Distinct from other bind failures because two daemons writing pwm1 would
    oscillate against each other; this must abort startup rather than degrade.
    """


def _recv_line(sock: socket.socket) -> bytes:
    """Read one newline-terminated message, bounded in size."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b"\n" in chunk:
            break
        if total > _MAX_MESSAGE:
            raise ControlError("message too large")
    return b"".join(chunks).split(b"\n", 1)[0]


def send(request: dict, path: str = SOCKET_PATH,
         timeout: float = _TIMEOUT_S) -> dict:
    """Send one request to a running daemon and return its reply.

    Raises DaemonNotRunning when no daemon is listening, so callers can fall
    back to persisting the setting instead.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall((json.dumps(request) + "\n").encode())
            data = _recv_line(sock)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise DaemonNotRunning(f"no thorfan daemon listening on {path}") from exc
    except PermissionError as exc:
        raise ControlError(f"root required to use {path}") from exc
    except TimeoutError as exc:
        raise ControlError("daemon did not respond in time") from exc
    except OSError as exc:
        raise ControlError(f"control request failed: {exc}") from exc

    if not data:
        raise ControlError("daemon closed the connection without replying")
    try:
        reply = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ControlError("malformed reply from daemon") from exc
    if not isinstance(reply, dict):
        raise ControlError("malformed reply from daemon")
    if not reply.get("ok"):
        raise ControlError(str(reply.get("error", "daemon rejected the request")))
    return reply


def probe(path: str = SOCKET_PATH, timeout: float = 1.0) -> bool:
    """True when a daemon is listening. Used for the singleton check."""
    if not os.path.exists(path):
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(path)
        except OSError:
            return False
    return True


def _apply_permissions(path: str, group: str | None) -> None:
    """Restrict the socket, widening to a group only when one exists.

    Failing to chown is not fatal: a root-only socket still works for the CLI,
    and refusing to start the daemon over a cosmetic permission problem would
    leave the fan unmanaged, which is worse.
    """
    os.chmod(path, 0o600)
    if group is None:
        return
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        log.debug("group %s does not exist; socket stays root-only", group)
        return
    try:
        os.chown(path, -1, gid)
        os.chmod(path, 0o660)
        log.info("socket group %s may connect", group)
    except OSError as exc:
        log.warning("could not grant group %s access to %s: %s",
                    group, path, exc)


class ControlServer:
    """Serves control requests on a Unix socket in a background thread.

    Requests are handled serially: they are trivial, and serialising them
    keeps the handler's locking obvious.
    """

    def __init__(self, handler: Callable[[dict], dict],
                 path: str = SOCKET_PATH,
                 group: str | None = SOCKET_GROUP) -> None:
        self._handler = handler
        self._path = path
        self._group = group
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def path(self) -> str:
        return self._path

    def start(self) -> None:
        """Bind the socket and begin serving.

        Refuses to start when another daemon is live, and clears the socket
        when it is merely stale (left behind by a SIGKILL).
        """
        if probe(self._path):
            raise AlreadyRunning(
                f"another thorfan daemon is already running ({self._path})"
            )
        try:
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if os.path.exists(self._path):
                os.unlink(self._path)  # stale socket from a killed daemon
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(self._path)
            _apply_permissions(self._path, self._group)
            sock.listen(4)
            sock.settimeout(_ACCEPT_POLL_S)
        except OSError as exc:
            raise ControlError(f"cannot listen on {self._path}: {exc}") from exc

        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, name="thorfan-control", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        """Stop serving and remove the socket."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_ACCEPT_POLL_S * 4)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def __enter__(self) -> ControlServer:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            with conn:
                self._handle_connection(conn)

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(_TIMEOUT_S)
            line = _recv_line(conn)
            if not line:
                return
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            reply = self._handler(request)
        except json.JSONDecodeError:
            reply = {"ok": False, "error": "malformed request"}
        except Exception as exc:  # noqa: BLE001
            # A bad request must never take down the control channel, and
            # never the control loop that is keeping the fan alive.
            reply = {"ok": False, "error": str(exc) or exc.__class__.__name__}
        try:
            conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass  # client gave up; nothing useful to do
