"""Socket client for the Blender MCP bridge addon."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876
DEFAULT_TIMEOUT = 300.0
_CONNECT_TIMEOUT = 2.0


class BlenderError(RuntimeError):
    """An error reported by the Blender side or the transport."""


def find_blender_executable() -> str | None:
    override = os.environ.get("BLENDER_EXE")
    if override and os.path.isfile(override):
        return override
    candidates: list[str] = []
    program_files = {os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramW6432", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")}
    for root in program_files:
        base = os.path.join(root, "Blender Foundation")
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base), reverse=True):
            exe = os.path.join(base, entry, "blender.exe")
            if os.path.isfile(exe):
                candidates.append(exe)
    local = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")
    if os.path.isdir(local):
        for entry in sorted(os.listdir(local), reverse=True):
            exe = os.path.join(local, entry, "blender.exe")
            if os.path.isfile(exe):
                candidates.append(exe)
    which = shutil_which("blender")
    if which:
        candidates.append(which)
    return candidates[0] if candidates else None


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


class BlenderBridge:
    """Stateless, one-request-per-connection client for the addon socket."""

    def __init__(self, host: str | None = None, port: int | None = None,
                 timeout: float | None = None):
        self.host = host or os.environ.get("BLENDER_MCP_HOST", DEFAULT_HOST)
        self.port = int(port or os.environ.get("BLENDER_MCP_PORT", DEFAULT_PORT))
        self.timeout = float(timeout or os.environ.get("BLENDER_MCP_TIMEOUT", DEFAULT_TIMEOUT))
        self._instance: subprocess.Popen | None = None
        self._instance_attempted = False

    # ----------------------------------------------------------------- status
    def is_available(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), _CONNECT_TIMEOUT):
                return True
        except OSError:
            return False

    def status(self) -> dict:
        if not self.is_available():
            return {
                "connected": False,
                "host": self.host,
                "port": self.port,
                "hint": (
                    "Start Blender (a window is required - a --background Blender "
                    "has no event loop) and make sure the 'MCP Bridge' addon is "
                    "enabled. It starts automatically; otherwise press Start in "
                    "3D View > Sidebar > MCP. Set BLENDER_MCP_PORT if it uses a "
                    "different port."
                ),
            }
        return {"connected": True, **self.call("ping", timeout=15.0)}

    # ------------------------------------------------------------------- call
    def call(self, command: str, params: dict[str, Any] | None = None,
             timeout: float | None = None) -> dict:
        params = params or {}
        deadline_timeout = float(timeout if timeout is not None else self.timeout)
        payload = {
            "id": command,
            "command": command,
            "params": params,
            "timeout": deadline_timeout,
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            with socket.create_connection((self.host, self.port), _CONNECT_TIMEOUT) as sock:
                sock.settimeout(deadline_timeout + _CONNECT_TIMEOUT)
                sock.sendall(raw + b"\n")
                chunks = bytearray()
                while b"\n" not in chunks:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.extend(chunk)
        except socket.timeout as exc:
            raise BlenderError(
                f"No response from Blender within {deadline_timeout:g}s. "
                "The Blender UI may be busy with a long render or a modal dialog."
            ) from exc
        except OSError as exc:
            raise BlenderError(self._connection_hint(exc)) from exc

        line = bytes(chunks).split(b"\n", 1)[0].strip()
        if not line:
            raise BlenderError("Blender closed the connection without responding.")
        try:
            response = json.loads(line.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise BlenderError(f"Malformed response from Blender: {exc}") from exc
        if not response.get("ok"):
            message = response.get("error") or "Unknown Blender error"
            traceback_text = response.get("traceback")
            if traceback_text:
                message = f"{message}\n\nBlender traceback:\n{traceback_text}"
            raise BlenderError(message)
        return response.get("data") or {}

    def _connection_hint(self, exc: OSError) -> str:
        if isinstance(exc, ConnectionRefusedError):
            return (
                f"Connection refused on {self.host}:{self.port}. "
                "The MCP Bridge addon is not running. Enable it in "
                "Blender: Edit > Preferences > Add-ons > 'MCP Bridge', then "
                "press Start in 3D View > Sidebar > MCP. "
                "Set BLENDER_MCP_PORT if the bridge uses a non-default port."
            )
        return f"Could not reach the Blender bridge on {self.host}:{self.port}: {exc}"

    # ------------------------------------------------------------ autolaunch
    def maybe_launch_instance(self) -> bool:
        """Start a small unfocused Blender window with the bridge already enabled.

        Opt-in via ``BLENDER_MCP_AUTOLAUNCH=1``. A real window is mandatory:
        the bridge is driven by Blender's own timer loop, which a
        ``--background`` process never runs.
        """
        if self.is_available():
            return True
        flag = os.environ.get("BLENDER_MCP_AUTOLAUNCH", "").lower()
        if self._instance_attempted or flag not in {"1", "true", "yes"}:
            return self.is_available()
        self._instance_attempted = True
        executable = find_blender_executable()
        if not executable:
            raise BlenderError(
                "BLENDER_MCP_AUTOLAUNCH is set but no blender.exe was found. "
                "Set BLENDER_EXE to the full path of your Blender executable."
            )
        environment = dict(os.environ, BLENDER_MCP_PORT=str(self.port))
        self._instance = subprocess.Popen(  # noqa: S603
            [executable, "--no-window-focus", "--window-geometry", "0", "0", "900", "700"],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        for _ in range(200):
            if self.is_available():
                return True
            if self._instance.poll() is not None:
                stderr = b""
                if self._instance.stderr:
                    stderr = self._instance.stderr.read() or b""
                raise BlenderError(
                    "The Blender instance exited immediately:\n"
                    + stderr.decode("utf-8", "replace")[-2000:]
                )
            time.sleep(0.15)
        raise BlenderError(
            f"Blender did not open the bridge on {self.host}:{self.port} within 30s. "
            "Check that the 'MCP Bridge' addon is enabled."
        )

    def shutdown(self) -> None:
        if self._instance is not None and self._instance.poll() is None:
            self._instance.terminate()
            try:
                self._instance.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self._instance.kill()
            self._instance = None


BRIDGE = BlenderBridge()
