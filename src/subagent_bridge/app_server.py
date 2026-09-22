"""Codex app-server client for the shared Unix WebSocket control socket."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import socket
import struct
import threading
from typing import Any

WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class AppServerError(RuntimeError):
    """Base error for transport and JSON-RPC failures."""


class AppServerDisconnected(AppServerError):
    pass


class AppServerResponseError(AppServerError):
    def __init__(self, error: dict[str, Any]):
        self.error = error
        super().__init__(json.dumps(error, ensure_ascii=False))


class UnixWebSocketTransport:
    """Minimal RFC 6455 client over an AF_UNIX socket."""

    def __init__(self, path: str, timeout: float = 15.0):
        self.path = path
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.settimeout(timeout)
        self._socket.connect(path)
        self._initialize_connected_socket(timeout)

    @classmethod
    def from_connected_socket(
        cls, connected_socket: socket.socket, timeout: float = 15.0
    ) -> "UnixWebSocketTransport":
        """Build a transport from an already connected AF_UNIX socket."""
        self = cls.__new__(cls)
        self.path = "<connected-socket>"
        self._socket = connected_socket
        self._socket.settimeout(timeout)
        self._initialize_connected_socket(timeout)
        return self

    def _initialize_connected_socket(self, timeout: float) -> None:
        self._buffer = bytearray()
        self._send_lock = threading.Lock()
        self._closed = False
        self._upgrade()
        # The reader owns blocking behavior after the handshake. Request
        # timeouts are implemented above the transport and close wakes it.
        self._socket.settimeout(None)

    def _upgrade(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self._socket.sendall(request)
        while b"\r\n\r\n" not in self._buffer:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise AppServerDisconnected("socket closed during WebSocket upgrade")
            self._buffer.extend(chunk)
            if len(self._buffer) > 64 * 1024:
                raise AppServerError("WebSocket upgrade headers exceed 64 KiB")
        headers, remainder = bytes(self._buffer).split(b"\r\n\r\n", 1)
        self._buffer = bytearray(remainder)
        lines = headers.decode("ascii", errors="strict").split("\r\n")
        if lines[0] != "HTTP/1.1 101 Switching Protocols":
            raise AppServerError(f"WebSocket upgrade failed: {lines[0]}")
        fields: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                fields[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii"), usedforsecurity=False).digest()
        ).decode("ascii")
        if fields.get("sec-websocket-accept") != expected:
            raise AppServerError("invalid Sec-WebSocket-Accept header")

    def _read_exact(self, length: int) -> bytes:
        while len(self._buffer) < length:
            try:
                chunk = self._socket.recv(max(4096, length - len(self._buffer)))
            except OSError as exc:
                raise AppServerDisconnected(str(exc)) from exc
            if not chunk:
                raise AppServerDisconnected("app-server socket closed")
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:length])
        del self._buffer[:length]
        return result

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if len(payload) > MAX_MESSAGE_BYTES:
            raise AppServerError("outgoing WebSocket message exceeds limit")
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        elif len(payload) <= 0xFFFF:
            header.extend((0x80 | 126,))
            header.extend(struct.pack("!H", len(payload)))
        else:
            header.extend((0x80 | 127,))
            header.extend(struct.pack("!Q", len(payload)))
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        with self._send_lock:
            try:
                self._socket.sendall(header + masked)
            except OSError as exc:
                raise AppServerDisconnected(str(exc)) from exc

    def send_json(self, message: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(message, separators=(",", ":")).encode())

    def receive_json(self) -> dict[str, Any]:
        fragments = bytearray()
        fragmented = False
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            if second & 0x80:
                raise AppServerError("server WebSocket frames must not be masked")
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if length > MAX_MESSAGE_BYTES or len(fragments) + length > MAX_MESSAGE_BYTES:
                raise AppServerError("incoming WebSocket message exceeds limit")
            payload = self._read_exact(length)
            if opcode == 0x8:
                raise AppServerDisconnected("app-server sent a close frame")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1 and not fragmented:
                fragments.extend(payload)
                fragmented = not final
            elif opcode == 0x0 and fragmented:
                fragments.extend(payload)
                fragmented = not final
            else:
                raise AppServerError(f"unsupported WebSocket opcode sequence: {opcode}")
            if final:
                try:
                    value = json.loads(fragments.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AppServerError(f"invalid JSON text frame: {exc}") from exc
                if not isinstance(value, dict):
                    raise AppServerError("JSON-RPC message must be an object")
                return value

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()


class CodexAppServerClient:
    """Concurrent JSON-RPC client with notification and server-request queues."""

    def __init__(self, transport: UnixWebSocketTransport):
        self.transport = transport
        self.notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self.server_requests: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending: dict[int, queue.Queue[dict[str, Any] | BaseException]] = {}
        self._pending_lock = threading.Lock()
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="codex-app-server", daemon=True)
        self._reader.start()

    @classmethod
    def connect(cls, path: str, timeout: float = 15.0) -> "CodexAppServerClient":
        return cls(UnixWebSocketTransport(path, timeout))

    def _allocate_id(self) -> int:
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    def _read_loop(self) -> None:
        failure: BaseException | None = None
        try:
            while not self._closed.is_set():
                message = self.transport.receive_json()
                request_id = message.get("id")
                if request_id is not None and ("result" in message or "error" in message):
                    with self._pending_lock:
                        waiter = self._pending.get(request_id)
                    if waiter is not None:
                        waiter.put(message)
                    continue
                if request_id is not None and "method" in message:
                    self.server_requests.put(message)
                else:
                    self.notifications.put(message)
        except BaseException as exc:
            failure = exc
        finally:
            self._closed.set()
            terminal = failure or AppServerDisconnected("app-server client closed")
            with self._pending_lock:
                waiters = list(self._pending.values())
            for waiter in waiters:
                waiter.put(terminal)

    def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 15.0
    ) -> dict[str, Any]:
        if self._closed.is_set():
            raise AppServerDisconnected("app-server client is closed")
        request_id = self._allocate_id()
        waiter: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = waiter
        try:
            message: dict[str, Any] = {"id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            self.transport.send_json(message)
            try:
                response = waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise TimeoutError(f"timed out waiting for {method}") from exc
            if isinstance(response, BaseException):
                raise response
            if "error" in response:
                raise AppServerResponseError(response["error"])
            result = response.get("result")
            if not isinstance(result, dict):
                raise AppServerError(f"malformed response for {method}: {response!r}")
            return result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self.transport.send_json(message)

    def initialize(
        self,
        *,
        name: str = "subagent_bridge",
        title: str = "Subagent Bridge",
        version: str = "0.1.0",
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {"clientInfo": {"name": name, "title": title, "version": version}},
            timeout,
        )
        self.notify("initialized", {})
        return result

    def close(self) -> None:
        self._closed.set()
        self.transport.close()
        self._reader.join(timeout=2)
