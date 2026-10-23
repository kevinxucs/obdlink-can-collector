"""Append-only capture artifacts and an atomically updated session manifest."""

import base64
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import time
import uuid

from . import __version__
from .parser import Frame


FIELDS = ["seq", "segment", "host_time_ns", "elapsed_ns", "bus", "can_id",
          "can_id_hex", "extended", "dlc", "remote", "data_hex"]


class Session:
    def __init__(self, directory: Path, config: dict):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False)
        self.start_mono_ns = time.monotonic_ns()
        self.start_wall_ns = time.time_ns()
        self.raw_file = (directory / "raw.jsonl").open("x", buffering=262144)
        self.events_file = (directory / "events.jsonl").open("x", buffering=65536)
        self.csv_file = (directory / "frames.csv").open("x", newline="", buffering=262144)
        self.candump_file = (directory / "frames.log").open("x", buffering=262144)
        self.csv = csv.DictWriter(self.csv_file, fieldnames=FIELDS)
        self.csv.writeheader()
        self.stats = Counter(frames=0, rx_bytes=0, malformed_lines=0, overflow_events=0,
                             restarts=0, capture_gaps=0)
        self.id_counts = Counter()
        self.segment = 0
        self.gap_open = False
        self.last_flush = time.monotonic()
        self.manifest = {
            "schema_version": 1, "collector_version": __version__, "status": "initializing",
            "created_utc": datetime.now(timezone.utc).isoformat(), "start_host_time_ns": self.start_wall_ns,
            "clock": {"frame_timestamp": "host receive, not CAN hardware time",
                      "elapsed_ns": "host monotonic nanoseconds since session creation",
                      "batching": "frames completed in one read share a host timestamp"},
            "platform": platform.platform(), "config": config,
            "loss_accounting": "reported gaps are a lower bound; unreported adapter/link loss cannot be ruled out",
            "synthetic": config.get("transport") == "demo",
        }
        self.save()

    def stamp(self) -> tuple[int, int]:
        return time.time_ns(), time.monotonic_ns() - self.start_mono_ns

    def raw(self, direction: str, data: bytes, stamp: tuple[int, int]) -> None:
        self.raw_file.write(json.dumps({"host_time_ns": stamp[0], "elapsed_ns": stamp[1],
                                        "direction": direction, "base64": base64.b64encode(data).decode()}) + "\n")
        if direction == "rx":
            self.stats["rx_bytes"] += len(data)

    def event(self, kind: str, **details) -> None:
        wall, elapsed = self.stamp()
        self.events_file.write(json.dumps({"host_time_ns": wall, "elapsed_ns": elapsed,
                                          "type": kind, **details}, ensure_ascii=True) + "\n")

    def frame(self, frame: Frame, stamp: tuple[int, int]) -> None:
        self.stats["frames"] += 1
        ident = f"{frame.can_id:08X}" if frame.extended else f"{frame.can_id:03X}"
        self.id_counts[(ident, frame.extended)] += 1
        self.csv.writerow({"seq": self.stats["frames"], "segment": self.segment,
                           "host_time_ns": stamp[0], "elapsed_ns": stamp[1],
                           "bus": self.manifest["config"]["bus"], "can_id": frame.can_id,
                           "can_id_hex": ident, "extended": int(frame.extended),
                           "dlc": frame.dlc, "remote": int(frame.remote), "data_hex": frame.data.hex().upper()})
        # candump timestamps have microsecond display precision only; CSV is canonical.
        seconds, nanos = divmod(stamp[0], 1_000_000_000)
        payload = f"R{frame.dlc}" if frame.remote else frame.data.hex().upper()
        self.candump_file.write(f"({seconds}.{nanos // 1000:06d}) {self.manifest['config']['bus']} {ident}#{payload}\n")

    def begin_gap(self, reason: str) -> None:
        if not self.gap_open:
            self.gap_open = True
            self.stats["capture_gaps"] += 1
            self.event("gap_start", reason=reason, segment=self.segment,
                       note="loss may have started before this notification; missing frame count unknown")

    def end_gap(self) -> None:
        if self.gap_open:
            self.segment += 1
            self.event("gap_end", segment=self.segment, note="valid reception resumed; actual loss duration/count unknown")
            self.gap_open = False

    def save(self) -> None:
        self.manifest["stats"] = dict(self.stats)
        self.manifest["ids"] = [{"can_id_hex": ident, "extended": ext, "frames": count}
                                for (ident, ext), count in sorted(self.id_counts.items())]
        temporary = self.directory / "session.json.tmp"
        temporary.write_text(json.dumps(self.manifest, indent=2) + "\n")
        temporary.replace(self.directory / "session.json")

    def flush(self, force: bool = False) -> None:
        if force or time.monotonic() - self.last_flush >= 1:
            for stream in (self.raw_file, self.events_file, self.csv_file, self.candump_file):
                stream.flush()
                if force:
                    os.fsync(stream.fileno())
            self.save()
            self.last_flush = time.monotonic()

    def close(self, status: str, reason: str) -> None:
        self.event("session_end", status=status, reason=reason)
        self.manifest.update(status=status, end_reason=reason,
                             ended_utc=datetime.now(timezone.utc).isoformat(),
                             duration_s=(time.monotonic_ns() - self.start_mono_ns) / 1e9,
                             gap_open_at_end=self.gap_open)
        self.flush(force=True)
        for stream in (self.raw_file, self.events_file, self.csv_file, self.candump_file):
            stream.close()


def default_directory() -> Path:
    return Path("captures") / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6])
