"""Two local transports: macOS RFCOMM helper and POSIX serial device."""

import base64
from collections import deque
import glob
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time


class TransportError(RuntimeError):
    pass


ROOT = Path(__file__).resolve().parent.parent


def build_bridge() -> Path:
    if sys.platform != "darwin":
        raise TransportError("Native Bluetooth transport requires macOS; use --port instead.")
    source = Path(__file__).resolve().parent / "native" / "BluetoothBridge.swift"
    binary = ROOT / ".build" / "BluetoothBridge"
    if binary.exists() and binary.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return binary
    if not shutil.which("xcrun"):
        raise TransportError("Install Apple's Command Line Tools with xcode-select --install.")
    binary.parent.mkdir(exist_ok=True)
    try:
        result = subprocess.run([
            "xcrun", "swiftc", "-swift-version", "5", "-O", "-framework", "IOBluetooth",
            "-module-cache-path", str(ROOT / ".build" / "module-cache"),
            str(source), "-o", str(binary),
        ], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise TransportError("Bluetooth helper compilation timed out. Verify Xcode/Command Line Tools installation.") from exc
    if result.returncode:
        raise TransportError("Bluetooth helper compilation failed:\n" + result.stderr)
    return binary


def paired_devices(show_all: bool = False) -> list[dict]:
    args = [str(build_bridge()), "list"] + (["--all"] if show_all else [])
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired as exc:
        raise TransportError("Bluetooth enumeration timed out. Check macOS Bluetooth permissions.") from exc
    try:
        event = json.loads(result.stdout)
    except ValueError as exc:
        raise TransportError("Cannot enumerate Bluetooth devices: " + (result.stderr.strip() or "no helper response")) from exc
    if event.get("type") == "error":
        raise TransportError(event["message"])
    return event["devices"]


def serial_ports() -> list[str]:
    return sorted(glob.glob("/dev/cu.*"))


class BluetoothTransport:
    def __init__(self, device: str):
        self.process = subprocess.Popen(
            [str(build_bridge()), "connect", device], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            start_new_session=True,
        )
        self.buffer = bytearray()
        self.events = deque()
        self.stderr = bytearray()
        self.info = {}
        try:
            deadline = time.monotonic() + 50
            while time.monotonic() < deadline:
                event = self._next_event(min(0.5, max(0, deadline - time.monotonic())))
                if event and event["type"] == "ready":
                    self.info = event
                    return
                if event and event["type"] == "data":
                    # Startup bytes precede adapter synchronization and are stale.
                    continue
                if event and event["type"] == "status":
                    print(f"Bluetooth: {event.get('stage')} (channel {event.get('channel')}, {event.get('service_source')})", flush=True)
            raise TransportError("Bluetooth connect timed out. Check pairing, adapter power and Bluetooth permissions.")
        except BaseException:
            self.close()
            raise

    def _next_event(self, timeout: float) -> dict | None:
        if self.events:
            event = self.events.popleft()
            if event["type"] == "error":
                raise TransportError(event["message"])
            return event
        deadline = time.monotonic() + timeout
        while True:
            streams = [self.process.stdout, self.process.stderr]
            ready, _, _ = select.select(streams, [], [], max(0, deadline - time.monotonic()))
            for stream in ready:
                chunk = os.read(stream.fileno(), 65536)
                if stream is self.process.stderr:
                    if chunk:
                        self.stderr.extend(chunk)
                        self.stderr = self.stderr[-8192:]
                elif not chunk:
                    raise TransportError("Bluetooth bridge exited. " + self.stderr.decode(errors="replace").strip())
                else:
                    self.buffer.extend(chunk)
                    while b"\n" in self.buffer:
                        line, _, rest = self.buffer.partition(b"\n")
                        self.buffer = bytearray(rest)
                        try:
                            self.events.append(json.loads(line))
                        except ValueError as exc:
                            raise TransportError("Invalid Bluetooth bridge output") from exc
            if self.events:
                return self._next_event(0)
            if time.monotonic() >= deadline:
                return None

    def read(self, timeout: float) -> bytes:
        event = self._next_event(timeout)
        return base64.b64decode(event["base64"], validate=True) if event and event["type"] == "data" else b""

    def write(self, data: bytes) -> None:
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TransportError("Bluetooth connection lost while writing") from exc

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()


class SerialTransport:
    """Dependency-free macOS/POSIX serial stream, with an exclusive device lock."""

    def __init__(self, port: str, baud: int = 115200):
        import fcntl
        import termios
        import tty

        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.original = None
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.original = termios.tcgetattr(self.fd)
            speed = getattr(termios, f"B{baud}", None)
            if speed is None:
                raise TransportError(f"Unsupported serial baud rate: {baud}")
            tty.setraw(self.fd, termios.TCSANOW)
            attrs = termios.tcgetattr(self.fd)
            attrs[2] |= termios.CLOCAL | termios.CREAD
            attrs[4] = attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            self.info = {"type": "serial", "port": port, "baud": baud}
        except BaseException:
            self.close()
            raise

    def read(self, timeout: float) -> bytes:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return b""
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return b""
        if not data:
            raise TransportError("Serial device disconnected")
        return data

    def write(self, data: bytes) -> None:
        deadline = time.monotonic() + 3
        while data:
            _, ready, _ = select.select([], [self.fd], [], max(0, deadline - time.monotonic()))
            if not ready:
                raise TransportError("Serial write timed out")
            try:
                count = os.write(self.fd, data)
            except BlockingIOError:
                continue
            data = data[count:]

    def close(self) -> None:
        import termios
        if self.fd is not None:
            try:
                if self.original is not None:
                    termios.tcsetattr(self.fd, termios.TCSANOW, self.original)
            except OSError:
                pass
            finally:
                os.close(self.fd)
                self.fd = None
