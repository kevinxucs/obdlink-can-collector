import argparse
from collections import Counter
import csv
from dataclasses import asdict
import json
import math
from pathlib import Path
import signal
import sys

from .collector import AdapterError, Collector, Config, parse_filter
from .demo import DemoTransport
from .session import Session, default_directory
from .transport import BluetoothTransport, SerialTransport, TransportError, build_bridge, paired_devices, serial_ports


def positive(value: str) -> float:
    number = float(value)
    if number <= 0 or not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def filter_arg(value: str):
    try:
        return parse_filter(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="Passive OBDLink CAN collector for macOS. No diagnostic polling.")
    commands = parser.add_subparsers(dest="action", required=True)
    devices = commands.add_parser("devices", help="list paired OBD Bluetooth devices and serial ports")
    devices.add_argument("--all", action="store_true", help="also list paired Bluetooth devices without OBD in their name")
    commands.add_parser("build-bluetooth", help="compile the local macOS Bluetooth helper")
    capture = commands.add_parser("capture", help="record from a paired Bluetooth device or serial port")
    source = capture.add_mutually_exclusive_group()
    source.add_argument("--device", help="exact paired Bluetooth name or address; auto-detect one OBDLink if omitted")
    source.add_argument("--port", help="serial path, e.g. /dev/cu.OBDLinkMX-SPP")
    capture.add_argument("--baud", type=int, default=115200, help="serial host baud (not CAN bitrate), default 115200")
    demo = commands.add_parser("demo", help="record explicitly synthetic data, with no hardware")
    demo.add_argument("--simulate-overflow", action="store_true", help="inject one adapter overflow to exercise gap recovery")
    for command in (capture, demo):
        command.add_argument("--output", type=Path, help="new session directory; existing directories are never overwritten")
        command.add_argument("--duration", type=positive, default=3.0 if command is demo else None,
                             help="capture seconds after initialization; default: Ctrl-C (demo: 3 seconds)")
        command.add_argument("--bus", choices=("hs", "ms"), default="hs", help="hs: OBD pins 6/14; ms: pins 3/11")
        command.add_argument("--bitrate", choices=(125000, 250000, 500000), type=int,
                             help="default hs=500000, ms=125000")
        command.add_argument("--filter", action="append", type=filter_arg, default=[], metavar="ID:MASK",
                             help="adapter pass filter in hex, repeatable; default accepts all IDs")
        command.add_argument("--vehicle", default="", help="optional vehicle/year/market/transmission description")
        command.add_argument("--note", default="", help="optional recording notes")
        command.add_argument("--max-restarts", type=nonnegative, default=10,
                             help="maximum monitor restarts after prompt/overflow; no automatic filter changes")
        command.add_argument("--idle-warning", type=positive, default=10, help="seconds without frames before warning")
        command.add_argument("--command-timeout", type=positive, default=4, help="adapter command timeout in seconds")
    inspect = commands.add_parser("inspect", help="show capture quality and frame counts per CAN ID")
    inspect.add_argument("session", type=Path)
    return parser.parse_args(argv)


def inspect_session(path: Path) -> int:
    metadata = json.loads((path / "session.json").read_text())
    print(f"Session: {path.resolve()}")
    print(f"Status: {metadata['status']} | Synthetic: {metadata['synthetic']}")
    print("Clock: host receive timestamps; Bluetooth buffering affects observed rates.")
    print("Reported gaps are a lower bound; zero reported gaps does not prove lossless capture.")
    print(json.dumps(metadata["stats"], sort_keys=True))
    counts = Counter()
    dlcs = {}
    first = last = None
    with (path / "frames.csv").open(newline="") as stream:
        for row in csv.DictReader(stream):
            key = (row["bus"], row["can_id_hex"], row["extended"])
            counts[key] += 1
            dlcs.setdefault(key, set()).add(int(row["dlc"]))
            stamp = int(row["elapsed_ns"])
            first = stamp if first is None else first
            last = stamp
    elapsed = (last - first) / 1e9 if last is not None else 0
    print(f"{'Bus':<5} {'ID(hex)':<10} {'ID(dec)':>9} {'Ext':>4} {'Frames':>10} {'Observed/s':>12}  DLC")
    for key, count in sorted(counts.items()):
        bus, ident, ext = key
        rate = f"{count / elapsed:.1f}" if elapsed > 0 else "n/a"
        print(f"{bus:<5} {ident:<10} {int(ident,16):>9} {ext:>4} {count:>10} {rate:>12}  {','.join(map(str,sorted(dlcs[key])))}")
    return 0


def run_capture(args) -> int:
    config = Config(bus=args.bus, bitrate=args.bitrate or (500000 if args.bus == "hs" else 125000),
                    filters=tuple(args.filter), duration=args.duration, command_timeout=args.command_timeout,
                    idle_warning=args.idle_warning, max_restarts=args.max_restarts)
    source = "demo" if args.action == "demo" else ("serial" if args.port else "bluetooth")
    directory = args.output or default_directory()
    if directory.exists():
        raise ValueError(f"Output already exists: {directory}; choose a new session directory")
    # Resolve only paired adapters. Never choose generic incoming/debug serial ports.
    device = getattr(args, "device", None)
    if source == "bluetooth" and not device:
        matches = [x for x in paired_devices() if "obdlink" in x["name"].lower()]
        if len(matches) != 1:
            raise TransportError("No unique paired OBDLink found. Pair the MX+ in System Settings > Bluetooth, run devices, then supply --device.")
        device = matches[0]["address"]
    details = {**asdict(config), "transport": source, "vehicle": args.vehicle, "note": args.note,
               "device": device, "serial_port": getattr(args, "port", None),
               "coverage": "filtered subset" if args.filter else "all IDs on selected physical bus"}
    session = Session(directory, details)
    print(f"Recording directory: {directory.resolve()}", flush=True)
    transport = collector = None
    status, reason, result = "failed", "initialization did not complete", 1
    stop_flag = False

    def request_stop(signum, frame):
        nonlocal stop_flag
        stop_flag = True

    previous_handlers = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if source == "demo":
            transport = DemoTransport(args.simulate_overflow)
        elif source == "serial":
            transport = SerialTransport(args.port, args.baud)
        else:
            transport = BluetoothTransport(device)
        session.manifest["transport_info"] = transport.info
        collector = Collector(transport, session, config, progress=lambda text: print(text, flush=True))
        session.event("initializing")
        collector.initialize()
        print(f"Silent monitoring: {args.bus.upper()} CAN at {config.bitrate} bit/s. Ctrl-C to stop.", flush=True)
        reason = collector.capture(stop_requested=lambda: stop_flag)
        status, result = "complete", 0
    except (AdapterError, TransportError, OSError, ValueError, RuntimeError) as exc:
        reason = str(exc)
        session.event("fatal_error", message=reason)
        session.begin_gap(reason)
        print(f"Capture failed: {reason}", file=sys.stderr)
    finally:
        try:
            if collector is not None:
                try:
                    collector.stop()
                except (AdapterError, TransportError, OSError, ValueError) as exc:
                    session.event("shutdown_error", message=str(exc))
                    session.begin_gap("adapter shutdown could not be confirmed")
                    if result == 0:
                        status, reason, result = "failed", "adapter shutdown could not be confirmed", 1
            if transport is not None:
                try:
                    transport.close()
                except OSError as exc:
                    session.event("shutdown_error", message=str(exc))
                    status, reason, result = "failed", "transport close failed", 1
        finally:
            # Shutdown may drain the final complete frames from the adapter.
            if status == "complete" and not session.stats["frames"]:
                status, reason, result = "no_data", "capture ended without valid frames", 2
            for sig, previous in previous_handlers.items():
                signal.signal(sig, previous)
            session.close(status, reason)
            print(f"Saved {session.stats['frames']:,} frames, {session.stats['capture_gaps']} reported gaps: {directory.resolve()}", flush=True)
    return result


def main(argv=None) -> int:
    args = arguments(argv)
    try:
        if args.action == "build-bluetooth":
            print(build_bridge())
            return 0
        if args.action == "devices":
            print("Serial ports:")
            for port in serial_ports():
                print(f"  {port}")
            print("Paired Bluetooth devices" + (":" if args.all else " containing 'OBD':"))
            devices = paired_devices(args.all)
            for item in devices:
                print(f"  {item['name']}  {item['address']}")
            if not devices:
                print("  None visible. Power the MX+, enable pairing, and pair in System Settings > Bluetooth.")
            return 0
        if args.action == "inspect":
            return inspect_session(args.session)
        return run_capture(args)
    except (OSError, ValueError, TransportError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
