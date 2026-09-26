"""Socket transport between the Blender addon and the MCP server.

Design notes
------------
bpy is only valid on Blender's main thread, and a background thread gets a
stripped down ``bpy.context`` that breaks most operators. So there are no
worker threads at all: a ``bpy.app.timers`` callback polls the listening socket
non-blockingly and handles each request inline, on the main thread, with a
fully populated context.

Consequences worth knowing:
* Commands can never interleave with renders or modal dialogs, because the
  timer simply does not run while the UI is busy. A client waiting on a long
  command will hit its own timeout instead of corrupting the file.
* A ``--background`` Blender has no event loop, so timers never fire and the
  bridge cannot work there. Start Blender with a window (``-m`` for a
  minimised one) instead.
* The wire protocol is newline delimited JSON so it stays trivially debuggable.
"""

from __future__ import annotations

import ast
import contextlib
import json
import logging
import socket
import traceback

import bpy
from mathutils import Color, Euler, Matrix, Quaternion, Vector

log = logging.getLogger("blender_mcp_bridge")

MAX_MESSAGE_BYTES = 64 * 1024 * 1024
DEFAULT_TIMEOUT = 300.0
POLL_INTERVAL = 0.02

# Commands that tear down and rebuild the whole bpy context. They are only
# safe on the main thread, which is where we always run, but they are worth
# flagging because they will also drop the bridge's own state.
CONTEXT_RELOADING = {"file_op"}

DIRECT_UNSAFE_MESSAGE = (
    "This Blender is running with --background, which has no event loop, so the "
    "bridge cannot accept commands. Start Blender normally (optionally with -m "
    "for a minimised window) and enable the MCP Bridge addon there."
)


def to_jsonable(value):
    """Recursively convert a Blender value into something json can encode."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (Vector, Color, Quaternion, Euler)):
        return to_jsonable(list(value))
    if isinstance(value, Matrix):
        return [to_jsonable(list(row)) for row in value]
    if isinstance(value, bpy.types.ID):
        return getattr(value, "name", str(value))
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [to_jsonable(item) for item in items]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if hasattr(value, "_mcp_result"):
        return to_jsonable(value.value)
    if hasattr(value, "to_dict"):
        with contextlib.suppress(Exception):
            return to_jsonable(value.to_dict())
    if hasattr(value, "__iter__"):
        with contextlib.suppress(Exception):
            return to_jsonable(list(value))
    return str(value)


def dumps(payload) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False)


def revive_nested_json(value, depth: int = 0):
    """Parse back objects an MCP client serialised as strings.

    Some clients flatten nested structures such as ``parameters={"size": 70}``
    into a string, which reaches bpy as a str and produces confusing "expected
    a float type, not str" errors. Both JSON and Python repr spellings are
    accepted. Only values *inside* a dict or list are touched, so top level
    string arguments (paths, Python source, operator ids) are never rewritten.
    """
    if depth and isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[":
            for parse in (json.loads, ast.literal_eval):
                try:
                    return revive_nested_json(parse(text), depth)
                except (ValueError, SyntaxError, TypeError):
                    continue
        return value
    if isinstance(value, dict):
        return {key: revive_nested_json(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [revive_nested_json(item, depth + 1) for item in value]
    return value


@contextlib.contextmanager
def view3d_override(**extra):
    """Run a block with a usable VIEW_3D area/region context.

    ``bpy.ops`` calls issued from a timer callback have no area context, which
    makes anything viewport related fail. This finds a real VIEW_3D area and
    injects it together with the window and screen that own it.

    The window/screen/area triple has to be consistent: passing an area from
    ``bpy.data.screens`` without its window raises "Area set with window &
    screen set to None", so areas are only ever taken from a live window.
    """
    kwargs: dict[str, object] = {}
    manager = bpy.context.window_manager
    windows = list(getattr(manager, "windows", [])) if manager else []
    if not windows and bpy.context.window is not None:
        windows = [bpy.context.window]

    for window in windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            kwargs["window"] = window
            kwargs["screen"] = screen
            kwargs["area"] = area
            if region is not None:
                kwargs["region"] = region
                kwargs["space_data"] = area.spaces.active
            break
        if "area" in kwargs:
            break

    if "area" not in kwargs and bpy.context.area is not None:
        # no VIEW_3D anywhere; hand over whatever area we do have rather than
        # injecting a mismatched one
        kwargs["area"] = bpy.context.area
        kwargs["region"] = bpy.context.region
        if bpy.context.window is not None:
            kwargs["window"] = bpy.context.window
        if bpy.context.screen is not None:
            kwargs["screen"] = bpy.context.screen

    kwargs.update({k: v for k, v in extra.items() if v is not None})
    with bpy.context.temp_override(**kwargs):
        yield


class CommandError(Exception):
    """Raised by command handlers for user facing, actionable errors."""


class _Connection:
    __slots__ = ("sock", "buffer")

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.sock.setblocking(False)
        self.buffer = bytearray()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    def send(self, payload: dict) -> None:
        with contextlib.suppress(OSError):
            self.sock.sendall((dumps(payload) + "\n").encode("utf-8"))


class BridgeServer:
    """Main-thread, timer-driven socket server for MCP clients."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9876):
        self.host = host
        self.port = int(port)
        self._sock: socket.socket | None = None
        self._connections: list[_Connection] = []
        self._handlers: dict = {}
        self._timer_registered = False
        self.request_count = 0
        self.last_error = ""
        self.last_request_at = 0.0

    # ------------------------------------------------------------------ setup
    def register_handlers(self, handlers: dict) -> None:
        self._handlers = handlers

    def is_running(self) -> bool:
        return self._sock is not None and self._timer_registered

    def unsupported_reason(self) -> str:
        if bpy.app.background:
            return DIRECT_UNSAFE_MESSAGE
        return ""

    def start(self) -> None:
        if self.is_running():
            return
        reason = self.unsupported_reason()
        if reason:
            raise RuntimeError(reason)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(8)
        self._sock.setblocking(False)
        self.port = self._sock.getsockname()[1]
        bpy.app.timers.register(self._pump, persistent=True)
        self._timer_registered = True
        log.info("MCP bridge listening on %s:%s", self.host, self.port)

    def stop(self) -> None:
        if self._timer_registered:
            with contextlib.suppress(Exception):
                bpy.app.timers.unregister(self._pump)
            self._timer_registered = False
        for connection in self._connections:
            connection.close()
        self._connections.clear()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    # ----------------------------------------------------------------- pump
    def _pump(self) -> float:
        """Timer callback on the main thread: accept, read, answer."""
        self._accept_pending()
        for connection in list(self._connections):
            if not self._serve(connection):
                self._connections.remove(connection)
                connection.close()
        return POLL_INTERVAL

    def _accept_pending(self) -> None:
        while self._sock is not None:
            try:
                client, address = self._sock.accept()
            except (BlockingIOError, socket.timeout):
                return
            except OSError as exc:
                self.last_error = f"accept failed: {exc}"
                return
            # _Connection sets its own non-blocking mode; setting it here first
            # would be immediately overwritten.
            self._connections.append(_Connection(client))
            if self.request_count == 0:
                log.info("MCP client connected from %s", address)

    def _serve(self, connection: _Connection) -> bool:
        """Read whatever is available; handle every complete line. False = drop."""
        try:
            chunk = connection.sock.recv(65536)
        except (BlockingIOError, socket.timeout):
            return True
        except OSError:
            return False
        if not chunk:
            return False
        connection.buffer.extend(chunk)
        while b"\n" in connection.buffer:
            raw, _, rest = connection.buffer.partition(b"\n")
            connection.buffer = bytearray(rest)
            raw = raw.strip()
            if not raw:
                continue
            if len(raw) > MAX_MESSAGE_BYTES:
                connection.send({"ok": False, "error": "Request exceeds maximum message size"})
                continue
            connection.send(self._handle_raw(raw))
        return True

    def _handle_raw(self, raw: bytes) -> dict:
        self.request_count += 1
        self.last_request_at = _now()
        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Malformed JSON request: {exc}"}
        if not isinstance(request, dict):
            return {"ok": False, "error": "Request must be a JSON object"}
        params = revive_nested_json(request.get("params") or {})
        if not isinstance(params, dict):
            return {"ok": False, "error": "Request 'params' must be a JSON object"}
        return self._run(str(request.get("command", "")), params)

    # ------------------------------------------------------------- execution
    def _run(self, command: str, params: dict) -> dict:
        handler = self._handlers.get(command)
        if handler is None:
            return {
                "ok": False,
                "error": (
                    f"Unknown command {command!r}. "
                    f"Available commands: {', '.join(sorted(self._handlers))}"
                ),
            }
        if command in CONTEXT_RELOADING:
            log.info("MCP Bridge: %s reloads the bpy context", command)
        try:
            data = handler(params)
        except CommandError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{command}: {exc}"
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        return {"ok": True, "data": data}

    # ------------------------------------------------------------------ info
    def ping(self) -> bool:
        """Round-trip health check used by the MCP client before dispatching."""
        return self.is_running()


def _now() -> float:
    import time

    return time.time()


SERVER = BridgeServer()
