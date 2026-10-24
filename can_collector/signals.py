"""Explicit, auditable signal definitions; no executable expressions or dependencies.

BE start counts bits from the MSB of byte zero. LE start counts from its LSB.
These are NOT DBC sawtooth start-bit numbers. Byte offsets are zero based.
"""
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path


@dataclass(frozen=True, order=True)
class Field:
    start: int
    width: int
    order: str = "big"
    signed: bool = False

    def __post_init__(self):
        if self.order not in ("big", "little") or self.start < 0 or not 1 <= self.width <= 64:
            raise ValueError("invalid field order/start/width")
        if self.start + self.width > 64:
            raise ValueError("field exceeds classic CAN payload")

    def decode(self, data: bytes) -> int:
        if self.start + self.width > len(data) * 8:
            raise ValueError("field exceeds payload DLC")
        shift = len(data) * 8 - self.start - self.width if self.order == "big" else self.start
        value = (int.from_bytes(data, self.order) >> shift) & ((1 << self.width) - 1)
        return value - (1 << self.width) if self.signed and value & (1 << (self.width - 1)) else value

    def physical_bits(self) -> frozenset[int]:
        """Physical positions numbered MSB0, independent of extraction order."""
        return frozenset(i if self.order == "big" else (i // 8 * 8 + 7 - i % 8)
                         for i in range(self.start, self.start + self.width))

    def describe(self) -> str:
        if self.width == 1:
            bit = next(iter(self.physical_bits()))
            return f"byte {bit // 8} bit {7 - bit % 8} (LSB=0)"
        if self.start % 8 == 0 and self.width % 8 == 0:
            label = f"byte {self.start // 8}" if self.width == 8 else f"bytes {self.start // 8}..{(self.start + self.width) // 8 - 1}"
        else:
            label = f"start {self.start}, {self.width} bits ({'MSB0' if self.order == 'big' else 'LSB0'})"
        return f"{label}, {'BE' if self.order == 'big' else 'LE'}, {'signed' if self.signed else 'unsigned'}"


@dataclass(frozen=True)
class Definition:
    name: str
    can_id: int
    field: Field
    kind: str = "continuous"
    factor: float = 1
    offset: float = 0
    unit: str = "raw"
    role: str = "seed"
    source: str = "user"
    bus: str = "hs"
    extended: bool = False
    clamp: tuple[float, float] | None = None
    thresholds: tuple[float, ...] = ()

    def __post_init__(self):
        if self.kind not in ("continuous", "state") or self.role not in ("seed", "validation"):
            raise ValueError("definition kind must be continuous/state; role seed/validation")
        if not 0 <= self.can_id <= (0x1FFFFFFF if self.extended else 0x7FF):
            raise ValueError("invalid CAN identifier")
        if not self.name or not all(math.isfinite(v) for v in (self.factor, self.offset, *self.thresholds)):
            raise ValueError("invalid definition name or numeric transform")
        if self.clamp is not None and (len(self.clamp) != 2 or not all(map(math.isfinite, self.clamp)) or self.clamp[0] > self.clamp[1]):
            raise ValueError("invalid clamp")

    def decode(self, data: bytes) -> float:
        raw = self.field.decode(data)
        value = (7 - sum(raw > threshold for threshold in self.thresholds)) if self.thresholds else raw * self.factor + self.offset
        return min(max(value, self.clamp[0]), self.clamp[1]) if self.clamp else value

    def matches(self, key) -> bool:
        bus, ident, extended, dlc = key
        return (bus, ident, extended) == (self.bus, self.can_id, self.extended) and self.field.start + self.field.width <= dlc * 8


GITGC = "https://github.com/gitgc/mx5-miata-nd2-obd-can"
DBC = "https://github.com/berumiya/CAN_DBC_6thGenMazda/blob/main/MX5ND_6thGenMazda_HSCAN.dbc"


def default_definitions() -> list[Definition]:
    """RaceChrono seeds, plus a SEPARATE attribution-only community lookup.

    Validation entries NEVER train the model or provide correlation anchors.
    Selected published formulas checked 2026-10-23. Market/calibration can differ.
    """
    def seed(name, ident, start, width, factor=1, offset=0, unit="raw", **kw):
        return Definition(name, ident, Field(start, width), factor=factor, offset=offset,
                          unit=unit, source=GITGC, **kw)

    result = [
        seed("engine_rpm", 0x202, 0, 16, .25, unit="rpm"),
        seed("vehicle_speed", 0x202, 16, 16, 1 / 360, unit="m/s"),
        seed("accelerator", 0x202, 32, 8, .4, unit="%"),
        seed("steering_angle", 0x086, 0, 16, -.1, 1600, "deg"),
        seed("brake_pedal", 0x078, 28, 12, 1 / 2.56, -156 / 2.56, "%", clamp=(0, 100)),
        seed("clutch", 0x050, 24, 8, 100, -100, "%", kind="state"),
        seed("coolant", 0x420, 0, 8, 1, -40, "degC"),
        seed("intake_air", 0x420, 32, 8, 1, -40, "degC"),
        seed("ambient_air", 0x420, 48, 16, .0025, -131.84, "degC"),
        seed("fuel_level", 0x43F, 0, 16, -100 / 34944, 40895 * 100 / 34944, "%"),
        seed("gear_selected", 0x165, 48, 16, kind="state", thresholds=(800, 1100, 1400, 1800, 2500, 4000)),
    ]
    for name, start in (("wheel_fl", 0), ("wheel_fr", 16), ("wheel_rl", 32), ("wheel_rr", 48)):
        result.append(seed(name, 0x215, start, 16, 1 / 360, -10000 / 360, "m/s"))

    def lookup(name, ident, start, width, factor=1, offset=0, unit="raw", kind="continuous"):
        return Definition(name, ident, Field(start, width), kind=kind, factor=factor, offset=offset,
                          unit=unit, role="validation", source=DBC)

    result += [
        lookup("manifold_absolute_pressure", 0x0FD, 48, 8, 1, 2, "kPa"),
        lookup("lateral_acceleration_raw", 0x075, 16, 16, .001, -2, "g"),
        lookup("longitudinal_acceleration_raw", 0x076, 16, 16, .001, -2, "g"),
        lookup("yaw_rate_raw", 0x075, 32, 16, .01, -180, "deg/s"),
        lookup("ignition_key_state", 0x050, 6, 2, kind="state"),
        lookup("ignition_key_state_inverse", 0x050, 61, 3, kind="state"),
        lookup("steering_absolute_angle", 0x082, 16, 16, .05, -1600, "deg"),
        lookup("barometric_pressure", 0x166, 36, 12, .1, -267, "kPa"),
        lookup("brake_booster_pressure_2", 0x166, 12, 12, .1, 0, "kPa"),
        lookup("vacuum_sensor_1", 0x325, 0, 12, .1, 0, "kPa"),
        lookup("intake_air_sensor_1", 0x4FA, 32, 8, 1, -40, "degC"),
        lookup("parking_brake", 0x09F, 3, 1, kind="state"),
        lookup("reverse_flag_maybe", 0x09F, 0, 1, kind="state"),
    ]
    return result


def definitions_json(definitions):
    return {"schema_version": 1, "bit_convention": "BE MSB0; LE LSB0; zero-based bytes; not DBC start bits",
            "definitions": [asdict(d) for d in definitions]}


def load_definitions(path: Path | None = None) -> list[Definition]:
    if path is None:
        return default_definitions()
    data = json.loads(path.read_text())
    if data.get("schema_version") != 1:
        raise ValueError("unsupported definitions schema")
    result = []
    for item in data["definitions"]:
        item = dict(item)
        item["can_id"] = int(item["can_id"], 0) if isinstance(item["can_id"], str) else item["can_id"]
        item["field"] = Field(**item["field"])
        if item.get("clamp") is not None:
            item["clamp"] = tuple(item["clamp"])
        item["thresholds"] = tuple(item.get("thresholds", ()))
        result.append(Definition(**item))
    if len({d.name for d in result}) != len(result):
        raise ValueError("definition names must be unique")
    return result
