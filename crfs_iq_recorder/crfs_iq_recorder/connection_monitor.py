"""Sensor HTTP availability. SFTP problems are tracked separately."""

from __future__ import annotations

from dataclasses import dataclass

LINK_CHECK_INTERVAL_MS = 10_000
LINK_CHECK_TIMEOUT_S = 4.0

CONNECTED = "connected"
RECONNECTING = "reconnecting"
DISCONNECTED = "disconnected"
IDLE = "idle"
CHECKING = "checking"

_LABELS = {
    CONNECTED: "Connected",
    RECONNECTING: "Reconnecting...",
    DISCONNECTED: "Disconnected",
    IDLE: "No sensor IP",
    CHECKING: "Checking…",
}

_STYLES = {
    CONNECTED: "ok",
    RECONNECTING: "wait",
    DISCONNECTED: "bad",
    IDLE: "idle",
    CHECKING: "wait",
}


def connection_identity(
    host: str,
    port: str | int = "",
    use_https: bool = False,
    username: str = "",
    password: str = "",
    demo: bool = False,
) -> tuple:
    return (
        (host or "").strip().casefold(),
        str(port or "").strip(),
        bool(use_https),
        username or "",
        password or "",
        bool(demo),
    )


@dataclass
class LinkMonitor:
    state: str = IDLE
    failures: int = 0
    identity: tuple = ()
    demo: bool = False

    def label(self) -> str:
        if self.demo and self.state == CONNECTED:
            return "Demo"
        return _LABELS.get(self.state, _LABELS[IDLE])

    def style(self) -> str:
        if self.demo and self.state == CONNECTED:
            return "ok"
        return _STYLES.get(self.state, "idle")

    def reset(self, identity: tuple, *, demo: bool, has_host: bool) -> None:
        self.identity = identity
        self.demo = bool(demo)
        self.failures = 0
        if self.demo:
            self.state = CONNECTED
        elif not has_host:
            self.state = IDLE
        else:
            self.state = CHECKING

    def note_success(self) -> bool:
        self.failures = 0
        if self.state != CONNECTED:
            self.state = CONNECTED
            return True
        return False

    def note_failure(self) -> bool:
        self.failures += 1
        if self.failures == 1:
            if self.state != RECONNECTING:
                self.state = RECONNECTING
                return True
            return False
        if self.state != DISCONNECTED:
            self.state = DISCONNECTED
            return True
        return False
