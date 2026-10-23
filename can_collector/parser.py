"""Parse STN output with ATH1/ATD1, with or without spaces."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Frame:
    can_id: int
    extended: bool
    dlc: int
    data: bytes
    remote: bool = False


class ParseError(ValueError):
    pass


def parse_frame(line: str) -> Frame:
    """Accept standard IDs, 8-digit extended IDs, or four-byte extended IDs."""
    parts = line.upper().split()
    if len(parts) == 1:
        compact = parts[0]
        remote = compact.endswith("RTR")
        core = compact[:-3] if remote else compact
        # With DLC printing enabled, 11-bit frames have an even number of
        # hex nibbles (3 ID + 1 DLC + payload); 29-bit frames have an odd count.
        width = 3 if len(core) % 2 == 0 else 8
        if len(core) >= width + 1:
            parts = [core[:width], core[width], "RTR" if remote else core[width + 1:]]
            if remote and len(core) != width + 1:
                raise ParseError("RTR frame contains unexpected payload")
    if len(parts) >= 5 and all(re.fullmatch(r"[0-9A-F]{2}", x) for x in parts[:4]):
        parts = ["".join(parts[:4]), *parts[4:]]
    if len(parts) < 2 or not re.fullmatch(r"(?:[0-9A-F]{3}|[0-9A-F]{8})", parts[0]):
        raise ParseError("missing or invalid CAN ID")
    extended = len(parts[0]) == 8
    can_id = int(parts[0], 16)
    if can_id > (0x1FFFFFFF if extended else 0x7FF):
        raise ParseError("CAN ID exceeds identifier width")
    if not re.fullmatch(r"[0-8]", parts[1]):
        raise ParseError("missing or invalid classic-CAN DLC")
    dlc = int(parts[1], 16)
    if parts[2:] == ["RTR"]:
        return Frame(can_id, extended, dlc, b"", True)
    payload = "".join(parts[2:])
    if not re.fullmatch(r"[0-9A-F]*", payload) or len(payload) != dlc * 2:
        raise ParseError("payload length does not match DLC")
    return Frame(can_id, extended, dlc, bytes.fromhex(payload))


class Tokenizer:
    """Frame arbitrary read chunks into lines and standalone '>' prompt tokens."""

    def __init__(self, limit: int = 4096):
        self.buffer = bytearray()
        self.limit = limit

    def feed(self, data: bytes) -> list[str]:
        result = []
        for value in data:
            if value in (10, 13, 62):
                if self.buffer:
                    # Original bytes are preserved separately in raw.jsonl.
                    result.append(self.buffer.decode("ascii", errors="backslashreplace").strip())
                    self.buffer.clear()
                if value == 62:
                    result.append(">")
            else:
                self.buffer.append(value)
                if len(self.buffer) >= self.limit:
                    result.append("MALFORMED_OVERLONG_LINE " + self.buffer.hex())
                    self.buffer.clear()
        return [x for x in result if x]

    def finish(self) -> str | None:
        if not self.buffer:
            return None
        result = self.buffer.decode("ascii", errors="backslashreplace")
        self.buffer.clear()
        return result
