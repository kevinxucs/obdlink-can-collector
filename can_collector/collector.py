"""STN command/prompt state machine. Never sends a diagnostic or CAN payload."""

from collections import deque
from dataclasses import dataclass
import re
import time

from .parser import ParseError, Tokenizer, parse_frame
from .session import Session


class AdapterError(RuntimeError):
    pass


@dataclass
class Config:
    bus: str = "hs"
    bitrate: int = 500000
    filters: tuple[tuple[str, str], ...] = ()
    duration: float | None = None
    command_timeout: float = 4
    idle_warning: float = 10
    max_restarts: int = 10


def parse_filter(value: str) -> tuple[str, str]:
    parts = value.upper().split(":")
    if len(parts) != 2 or any(not re.fullmatch(r"[0-9A-F]{1,8}", x) for x in parts):
        raise ValueError("filter must be hexadecimal ID:MASK, e.g. 202:7FF")
    pattern, mask = (int(x, 16) for x in parts)
    width = 8 if max(len(x) for x in parts) > 3 else 3
    limit = 0x1FFFFFFF if width == 8 else 0x7FF
    if pattern > limit or mask > limit:
        raise ValueError("filter exceeds CAN identifier width")
    return f"{pattern:0{width}X}", f"{mask:0{width}X}"


FIXED_COMMANDS = {"STPC", "ATD", "ATE0", "ATL0", "ATH1", "ATS0", "ATS1", "ATD1", "ATI", "STI",
                  "ATCAF0", "ATCFC0", "STCSEGR0", "STCMM0", "STFAC", "STP31", "STP51",
                  "STPO", "STM", "STPPMC"}


def allowed_command(command: str) -> bool:
    compact = command.replace(" ", "").upper()
    return (compact in FIXED_COMMANDS or bool(re.fullmatch(r"STPBR(?:125000|250000|500000)", compact))
            or bool(re.fullmatch(r"STFPA(?:[0-9A-F]{3},[0-9A-F]{3}|[0-9A-F]{8},[0-9A-F]{8})", compact)))


class Collector:
    def __init__(self, transport, session: Session, config: Config, progress=print):
        self.transport = transport
        self.session = session
        self.config = config
        self.progress = progress
        self.tokenizer = Tokenizer()
        self.pending = deque()
        self.monitoring = False
        self.awaiting_restart = False
        self.last_activity = time.monotonic()

    def send(self, command: str) -> None:
        if not allowed_command(command):
            raise AdapterError(f"Collector refused non-monitoring command: {command!r}")
        data = command.encode("ascii") + b"\r"
        self.session.raw("tx", data, self.session.stamp())
        self.transport.write(data)
        self.session.event("command", command=command)

    def read(self, timeout: float = 0.1):
        if self.pending:
            return self.pending.popleft()
        data = self.transport.read(timeout)
        if not data:
            return None
        stamp = self.session.stamp()
        self.session.raw("rx", data, stamp)
        self.pending.extend((token, stamp) for token in self.tokenizer.feed(data))
        return self.pending.popleft() if self.pending else None

    def wait_prompt(self, timeout: float, capture: bool = False) -> list[str]:
        lines = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            token = self.read(min(0.1, max(0, deadline - time.monotonic())))
            if token is None:
                continue
            line, stamp = token
            if line == ">":
                return lines
            lines.append(line)
            if capture:
                self.capture_line(line, stamp)
        raise AdapterError("Timed out waiting for adapter prompt; check pairing, power and other OBD apps")

    def command(self, command: str, *, optional: bool = False, identity: bool = False) -> list[str]:
        self.send(command)
        lines = self.wait_prompt(self.config.command_timeout)
        response = [x for x in lines if x.replace(" ", "").upper() != command.replace(" ", "").upper()]
        self.session.event("command_response", command=command, lines=response)
        if optional and response == ["?"]:
            self.session.event("unsupported_optional_command", command=command)
        elif identity:
            if not response or any(x in ("?", "ERROR") for x in response):
                raise AdapterError(f"Adapter identification failed: {command}: {response}")
        elif response != ["OK"]:
            raise AdapterError(f"Adapter rejected {command}: {response}")
        return response

    def initialize(self) -> None:
        # A bare CR in command mode could repeat somebody else's last OBD request.
        # X interrupts monitoring, or creates an invalid command when already idle.
        self.session.raw("tx", b"X", self.session.stamp())
        self.transport.write(b"X")
        try:
            self.wait_prompt(0.5)
        except AdapterError:
            self.session.raw("tx", b"\r", self.session.stamp())
            self.transport.write(b"\r")
            self.wait_prompt(self.config.command_timeout)
        self.command("STPC")
        self.command("ATD")
        self.command("STPC")
        for cmd in ("ATE0", "ATL0", "ATH1", "ATS0", "ATD1"):
            self.command(cmd)
        identities = {cmd: self.command(cmd, identity=True) for cmd in ("ATI", "STI")}
        self.session.manifest["adapter"] = identities
        # Older firmware without periodic messaging may reject this command.
        self.command("STPPMC", optional=True)
        for cmd in ("ATCAF0", "ATCFC0", "STCSEGR0", "STP31" if self.config.bus == "hs" else "STP51"):
            self.command(cmd)
        if self.config.bitrate != (500000 if self.config.bus == "hs" else 125000):
            self.command(f"STPBR {self.config.bitrate}")
        self.command("STCMM0")
        self.command("STFAC")
        for pattern, mask in (self.config.filters or (("000", "000"),)):
            self.command(f"STFPA {pattern},{mask}")
        self.command("STPO")
        self.session.manifest["status"] = "capturing"
        self.session.flush(force=True)

    def start(self) -> None:
        self.send("STM")
        self.monitoring = True
        self.awaiting_restart = False
        self.last_activity = time.monotonic()

    def capture_line(self, line: str, stamp) -> None:
        upper = line.upper()
        if "BUFFER FULL" in upper:
            prefix = line[:upper.index("BUFFER FULL")].strip()
            if prefix:
                # Some firmware appends the error to a complete or partial frame.
                self.capture_line(prefix, stamp)
            self.session.stats["overflow_events"] += 1
            self.session.event("adapter_error", message="BUFFER FULL")
            self.session.begin_gap("adapter buffer overflow")
            self.awaiting_restart = True
            self.progress("Buffer overflow: capture gap recorded; waiting for prompt to restart.")
            return
        if upper in ("STM", "STOPPED", "OK"):
            self.session.event("adapter_status", message=line)
            return
        if upper in ("CAN ERROR", "BUS ERROR", "BUS BUSY", "UNABLE TO CONNECT", "NO DATA", "?", "LV RESET", "OUT OF MEMORY") or "ERROR" in upper:
            self.session.event("adapter_error", message=line)
            self.session.begin_gap(line)
            # Only BUFFER FULL is automatically recoverable. Bus faults end capture.
            raise AdapterError(f"Adapter error during capture: {line}")
        try:
            frame = parse_frame(line)
        except ParseError as exc:
            self.session.stats["malformed_lines"] += 1
            self.session.event("malformed_line", line=line, reason=str(exc))
            self.session.begin_gap("malformed or unrecognized adapter output")
            return
        if self.session.gap_open and not self.awaiting_restart:
            self.session.end_gap()
        self.session.frame(frame, stamp)
        self.last_activity = time.monotonic()

    def capture(self, stop_requested=lambda: False) -> str:
        self.start()
        started = time.monotonic()
        last_report = started
        last_frames = 0
        last_idle_report = started
        prompt_deadline = None
        while not stop_requested():
            now = time.monotonic()
            if self.config.duration is not None and now - started >= self.config.duration:
                return "duration reached"
            token = self.read(0.1)
            if token is not None:
                line, stamp = token
                if line == ">":
                    self.monitoring = False
                    self.session.begin_gap("monitor returned to command prompt")
                    if self.session.stats["restarts"] >= self.config.max_restarts:
                        raise AdapterError("Monitor restart limit reached; narrow filters or use a faster CAN interface")
                    self.session.stats["restarts"] += 1
                    self.session.event("monitor_restart", number=self.session.stats["restarts"])
                    self.start()
                    prompt_deadline = None
                else:
                    self.capture_line(line, stamp)
                    if self.awaiting_restart and prompt_deadline is None:
                        prompt_deadline = time.monotonic() + self.config.command_timeout
            if prompt_deadline is not None and time.monotonic() >= prompt_deadline:
                raise AdapterError("Adapter overflowed but did not return a prompt")
            now = time.monotonic()
            if now - self.last_activity >= self.config.idle_warning and now - last_idle_report >= self.config.idle_warning:
                self.session.event("idle_warning", seconds_without_valid_frames=round(now - self.last_activity, 2))
                self.progress("No valid CAN frames recently. Check ignition, selected bus/bitrate and filters; monitoring continues.")
                last_idle_report = now
            if now - last_report >= 1:
                count = self.session.stats["frames"]
                self.progress(f"{count:,} frames | {(count-last_frames)/(now-last_report):.0f}/s | "
                              f"{len(self.session.id_counts)} IDs | {self.session.stats['overflow_events']} overflows | "
                              f"{self.session.stats['capture_gaps']} reported gaps")
                last_report, last_frames = now, count
            self.session.flush()
        return "stop requested"

    def stop(self) -> None:
        if self.monitoring:
            self.session.raw("tx", b"X", self.session.stamp())
            self.transport.write(b"X")
            self.wait_prompt(self.config.command_timeout, capture=True)
            self.monitoring = False
        self.command("STPC")
        trailing = self.tokenizer.finish()
        if trailing:
            self.session.stats["malformed_lines"] += 1
            self.session.event("truncated_line_at_end", line=trailing)
            self.session.begin_gap("truncated final adapter line")
