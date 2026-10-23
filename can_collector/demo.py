"""Deterministic synthetic STN adapter for exercising acquisition without a car."""

import math
import time


class DemoTransport:
    def __init__(self, simulate_overflow: bool = False):
        self.info = {"type": "demo", "synthetic": True}
        self.output = bytearray()
        self.input = bytearray()
        self.monitoring = False
        self.started = None
        self.next_frame = 0.0
        self.count = 0
        self.simulate_overflow = simulate_overflow
        self.overflowed = False
        self.closed = False
        self.commands = []
        self.spaces = True

    def write(self, data: bytes) -> None:
        if data == b"X" and self.monitoring:
            self.monitoring = False
            self.output.extend(b"STOPPED\r>")
            return
        self.input.extend(data)
        while b"\r" in self.input:
            line, _, rest = self.input.partition(b"\r")
            self.input = bytearray(rest)
            command = line.decode("ascii").replace(" ", "").upper()
            self.commands.append(command)
            if command in ("ATD", "ATS1"):
                self.spaces = True
            elif command == "ATS0":
                self.spaces = False
            if command == "STM":
                self.monitoring = True
                self.started = self.started or time.monotonic()
                self.next_frame = time.monotonic()
            elif command in ("ATI", "STI"):
                self.output.extend(b"SYNTHETIC STN ADAPTER\r>")
            elif command == "X":
                self.output.extend(b"?\r>")
            else:
                self.output.extend(b"OK\r>")

    def read(self, timeout: float) -> bytes:
        if not self.output:
            time.sleep(min(timeout, 0.005))
        now = time.monotonic()
        if self.monitoring and now >= self.next_frame:
            t = now - self.started
            if self.simulate_overflow and t > 0.35 and not self.overflowed:
                self.output.extend(b"202 8 01 BUFFER FULL\r>" if self.spaces else b"202801BUFFER FULL\r>")
                self.monitoring = False
                self.overflowed = True
            else:
                rpm = int((1800 + 1000 * math.sin(t)) * 4)
                speed = int((35 + 20 * math.sin(t / 2)) * 100)
                pedal = int(100 + 60 * math.sin(t))
                engine = rpm.to_bytes(2, "big") + speed.to_bytes(2, "big") + bytes([pedal, 0, self.count % 256, 0])
                wheels = b"".join((speed + 10000 + i * 2).to_bytes(2, "big") for i in range(4))
                temperature = bytes([90 + 40, 0, 0, 0, 25 + 40, 0, 0, 0])
                for ident, payload in (("202", engine), ("215", wheels), ("420", temperature)):
                    separator = " " if self.spaces else ""
                    self.output.extend(f"{ident}{separator}8{separator}{payload.hex().upper()}\r".encode())
                self.count += 1
                self.next_frame = now + 0.02
        # Deliberately split lines across transport reads.
        result = bytes(self.output[:47])
        del self.output[:47]
        return result

    def close(self) -> None:
        self.closed = True
