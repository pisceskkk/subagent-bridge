#!/usr/bin/env python3
"""Minimal Codex app-server client for Subagent Bridge experiments.

Safe defaults: ``probe`` only performs the handshake and lists threads.  The
``fork`` and ``wake`` commands require an explicit thread id and ``--execute``.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any


class ProtocolError(RuntimeError):
    pass


class UnixWebSocket:
    """Small RFC 6455 text client for app-server Unix control sockets."""

    def __init__(self, path: str, timeout: float) -> None:
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.socket.connect(path)
        self.buffer = bytearray()
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.socket.sendall(request)
        while b"\r\n\r\n" not in self.buffer:
            self.buffer.extend(self.socket.recv(4096))
        headers, remainder = bytes(self.buffer).split(b"\r\n\r\n", 1)
        self.buffer = bytearray(remainder)
        if not headers.startswith(b"HTTP/1.1 101"):
            raise ProtocolError(f"WebSocket upgrade failed: {headers!r}")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(header + masked)

    def send_json(self, message: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(message, separators=(",", ":")).encode())

    def _read_exact(self, length: int) -> bytes:
        while len(self.buffer) < length:
            chunk = self.socket.recv(max(4096, length - len(self.buffer)))
            if not chunk:
                raise ProtocolError("WebSocket closed unexpectedly")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:length])
        del self.buffer[:length]
        return result

    def receive_json(self) -> dict[str, Any]:
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise ProtocolError("WebSocket server closed the connection")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode != 0x1:
                continue
            return json.loads(payload)

    def request(self, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.send_json({"id": request_id, "method": method, "params": params})
        while True:
            message = self.receive_json()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise ProtocolError(json.dumps(message["error"], ensure_ascii=False))
            return message["result"]

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        finally:
            self.socket.close()


def unix_websocket_probe(path: str, timeout: float) -> dict[str, Any]:
    client = UnixWebSocket(path, timeout)
    try:
        initialized = client.request(
            0,
            "initialize",
            {
                "clientInfo": {
                    "name": "subagent_bridge_demo",
                    "title": "Subagent Bridge demo",
                    "version": "0.1.0",
                }
            },
        )
        client.send_json({"method": "initialized", "params": {}})
        threads = client.request(1, "thread/list", {"limit": 5, "sortKey": "updated_at"})
        return {"initialize": initialized, "threads": threads}
    finally:
        client.close()


def unix_websocket_start(
    path: str, timeout: float, cwd: str, prompt: str
) -> dict[str, Any]:
    """Create a thread and run one turn through an existing Unix app-server."""
    client = UnixWebSocket(path, timeout)
    try:
        initialized = client.request(
            0,
            "initialize",
            {
                "clientInfo": {
                    "name": "subagent_bridge_demo",
                    "title": "Subagent Bridge demo",
                    "version": "0.1.0",
                }
            },
        )
        client.send_json({"method": "initialized", "params": {}})
        started_thread = client.request(
            1,
            "thread/start",
            {
                "cwd": cwd,
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "serviceName": "subagent_bridge_demo",
            },
        )
        thread = started_thread["thread"]
        started_turn = client.request(
            2,
            "turn/start",
            {
                "threadId": thread["id"],
                "input": [{"type": "text", "text": prompt}],
            },
        )
        turn = started_turn["turn"]
        deadline = time.monotonic() + timeout
        events: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            message = client.receive_json()
            method = message.get("method")
            params = message.get("params", {})
            if method in {
                "thread/status/changed",
                "turn/started",
                "item/completed",
                "turn/completed",
            }:
                events.append(message)
            if (
                method == "turn/completed"
                and params.get("threadId") == thread["id"]
                and params.get("turn", {}).get("id") == turn["id"]
            ):
                return {
                    "initialize": initialized,
                    "thread": thread,
                    "turn": turn,
                    "events": events,
                }
        raise ProtocolError(f"timed out waiting for turn {turn['id']}")
    finally:
        client.close()


@dataclass
class AppServerClient:
    process: subprocess.Popen[str]
    timeout: float = 15.0

    def __post_init__(self) -> None:
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending: list[dict[str, Any]] = []
        self._next_id = 1
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._copy_stderr, daemon=True).start()

    @classmethod
    def start(cls, command: list[str], timeout: float) -> "AppServerClient":
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return cls(process, timeout)

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError as exc:
                self._messages.put({"_decode_error": str(exc), "_line": line})

    def _copy_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            print(f"app-server stderr: {line}", end="", file=sys.stderr)

    def send(self, message: dict[str, Any]) -> None:
        if self.process.poll() is not None:
            raise ProtocolError(f"app-server exited with {self.process.returncode}")
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            for index, message in enumerate(self._pending):
                if message.get("id") == request_id:
                    self._pending.pop(index)
                    return self._unwrap(message)
            try:
                message = self._messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if message.get("id") == request_id:
                return self._unwrap(message)
            self._pending.append(message)
        raise ProtocolError(f"timed out waiting for {method}")

    @staticmethod
    def _unwrap(message: dict[str, Any]) -> dict[str, Any]:
        if "_decode_error" in message:
            raise ProtocolError(f"invalid JSON from app-server: {message['_line']!r}")
        if "error" in message:
            raise ProtocolError(json.dumps(message["error"], ensure_ascii=False))
        result = message.get("result")
        if not isinstance(result, dict):
            raise ProtocolError(f"malformed response: {message!r}")
        return result

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "subagent_bridge_demo",
                    "title": "Subagent Bridge demo",
                    "version": "0.1.0",
                }
            },
        )
        self.notify("initialized", {})
        return result

    def wait_for_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                message = self._messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            params = message.get("params", {})
            if (
                message.get("method") == "turn/completed"
                and params.get("threadId") == thread_id
                and params.get("turn", {}).get("id") == turn_id
            ):
                return params
            self._pending.append(message)
        raise ProtocolError(f"timed out waiting for turn {turn_id}")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)


def planned_messages(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.action == "fork":
        return [
            {"method": "thread/fork", "params": {"threadId": args.thread_id}},
            {"method": "turn/start", "params": {"threadId": "<fork-result>", "input": [{"type": "text", "text": args.prompt}]}},
        ]
    return [
        {"method": "thread/resume", "params": {"threadId": args.thread_id}},
        {"method": "turn/start", "params": {"threadId": args.thread_id, "input": [{"type": "text", "text": args.prompt}]}},
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=os.environ.get("CODEX_BIN", "codex"))
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--connect-managed",
        action="store_true",
        help="connect through `codex app-server proxy` instead of starting a new app-server",
    )
    parser.add_argument(
        "--managed-socket",
        default=None,
        help="explicit control socket path for --connect-managed",
    )
    parser.add_argument(
        "--unix-websocket",
        default=None,
        help="connect directly to an app-server Unix WebSocket socket (probe only)",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("probe", help="handshake and list stored threads; no model turn")
    start = sub.add_parser("start", help="create a thread and start its first model turn")
    start.add_argument("--cwd", default=os.getcwd())
    start.add_argument("--prompt", required=True)
    start.add_argument("--execute", action="store_true", help="actually create the thread and turn")
    resume_check = sub.add_parser("resume-check", help="resume a thread without starting a model turn")
    resume_check.add_argument("--thread-id", required=True)
    for action in ("fork", "wake"):
        command = sub.add_parser(action)
        command.add_argument("--thread-id", required=True)
        command.add_argument("--prompt", required=True)
        command.add_argument("--execute", action="store_true", help="actually start a model turn")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.action in {"fork", "wake"} and not args.execute:
        print(json.dumps(planned_messages(args), ensure_ascii=False, indent=2))
        return 0
    if args.action == "start" and not args.execute:
        print(
            json.dumps(
                [
                    {"method": "thread/start", "params": {"cwd": args.cwd}},
                    {
                        "method": "turn/start",
                        "params": {
                            "threadId": "<thread-result>",
                            "input": [{"type": "text", "text": args.prompt}],
                        },
                    },
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.connect_managed:
        command = [args.codex, "app-server", "proxy"]
        if args.managed_socket:
            command.extend(["--sock", args.managed_socket])
    else:
        command = [args.codex, "app-server", "--listen", "stdio://"]
    if args.unix_websocket:
        if args.action not in {"probe", "start"}:
            print("error: --unix-websocket supports probe and start", file=sys.stderr)
            return 2
        try:
            if args.action == "probe":
                result = unix_websocket_probe(args.unix_websocket, args.timeout)
            else:
                result = unix_websocket_start(
                    args.unix_websocket, args.timeout, args.cwd, args.prompt
                )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except (OSError, KeyError, ProtocolError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    client = AppServerClient.start(command, args.timeout)
    try:
        initialized = client.initialize()
        if args.action == "probe":
            threads = client.request("thread/list", {"limit": 5, "sortKey": "updated_at"})
            print(json.dumps({"initialize": initialized, "threads": threads}, ensure_ascii=False, indent=2))
            return 0

        if args.action == "resume-check":
            thread = client.request("thread/resume", {"threadId": args.thread_id})["thread"]
            print(json.dumps({"thread": thread}, ensure_ascii=False, indent=2))
            return 0

        if args.action == "fork":
            thread = client.request("thread/fork", {"threadId": args.thread_id})["thread"]
            target_id = thread["id"]
        else:
            thread = client.request("thread/resume", {"threadId": args.thread_id})["thread"]
            target_id = args.thread_id
        started = client.request(
            "turn/start",
            {"threadId": target_id, "input": [{"type": "text", "text": args.prompt}]},
        )
        turn_id = started["turn"]["id"]
        completed = client.wait_for_turn(target_id, turn_id)
        print(json.dumps({"thread": thread, "completed": completed}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, KeyError, ProtocolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
