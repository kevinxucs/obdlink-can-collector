# OBDLink CAN collector for macOS

[![CI](https://github.com/kevinxucs/obdlink-can-collector/actions/workflows/ci.yml/badge.svg)](https://github.com/kevinxucs/obdlink-can-collector/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Records raw classic-CAN frames from an OBDLink MX+ and discovers candidate signal fields offline. Known ND2 RaceChrono definitions provide training examples and correlation anchors; inferred fields retain their evidence and uncertainty.

Uses Python 3.11+ with **no Python dependencies**. The Bluetooth transport compiles a small Swift helper against Apple's IOBluetooth framework. A POSIX serial transport is also included.

## Install

Requirements: macOS, Python 3.11 or newer, Apple Command Line Tools or Xcode, and a powered OBDLink MX+ paired with the Mac. Install Command Line Tools with `xcode-select --install` if needed.

```sh
git clone https://github.com/kevinxucs/obdlink-can-collector.git
cd obdlink-can-collector
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
can-collect --help
```

The installed `can-collect` command and `python -m can_collector` provide the same interface. You can also run `python3 -m can_collector` directly from a source checkout without installing the package. The Swift source is included in the package; its helper is compiled locally on first use.

## Start here

Run these commands from this directory:

```sh
python3 -m can_collector devices
```

The first Bluetooth command builds `.build/BluetoothBridge`. Apple Command Line Tools or Xcode is required. If missing, install with `xcode-select --install`. macOS may ask for Bluetooth access; allow it for the application running the command. If enumeration remains empty, check System Settings > Privacy & Security > Bluetooth and restart your terminal after changing permission.

Power the MX+ from the car, enable pairing with its physical button, and pair it in **System Settings > Bluetooth**. Close RaceChrono, OBDLink and other apps connected to the adapter. Run `devices` again to obtain the exact paired name or address. The generic `/dev/cu.Bluetooth-Incoming-Port` is not your adapter.

If the MX+ is the only paired OBDLink, start a recording with:

```sh
python3 -m can_collector capture --vehicle 'MX-5 ND2'
```

Or select its exact name explicitly (replace the example suffix with yours):

```sh
python3 -m can_collector capture --device 'OBDLink MX+ 12345' --duration 60
```

The default is HS-CAN, OBD pins 6/14, 500 kbit/s. This is the starting configuration for the ND2 MX-5. `Ctrl-C` stops monitoring, closes the CAN protocol, and flushes all files. `SIGTERM` also requests a clean stop. No PID list, annotations or external sensors are required to collect data.

The native Bluetooth connection reuses macOS's cached Serial Port service from pairing and opens its RFCOMM channel asynchronously while processing callbacks. It does not require an outgoing serial port. If your installation provides a working outgoing serial port, it can also be selected directly:

```sh
python3 -m can_collector capture --port /dev/cu.YOUR-OBDLINK-PORT --duration 60
```

`--baud` configures the host serial port, default 115200. It does not change CAN bitrate. It is unused by native Bluetooth.

## Verify everything without hardware

```sh
python3 -m can_collector demo --simulate-overflow --output captures/demo
python3 -m can_collector inspect captures/demo
python3 -m unittest discover -s tests -v
```

The demo generates explicitly **synthetic** engine, wheel-speed and temperature-shaped messages. `--simulate-overflow` injects a partial frame and one `BUFFER FULL` condition, then exercises recovery. Synthetic sessions are marked in their manifest and in `inspect`; never use them as real vehicle measurements.

## Capture files

Each run creates a new `captures/<UTC timestamp>_<unique suffix>/` directory. `--output PATH` chooses a different **new** directory. Existing directories are never overwritten.

| File | Contents |
| --- | --- |
| `session.json` | Schema version, vehicle/notes, adapter identity, bus/filters, transport, status, statistics and clock semantics |
| `frames.csv` | Canonical frame table with payload bytes, identifiers, DLC, host time, monotonic elapsed time, segment and sequence |
| `frames.log` | candump-compatible export for tools such as SavvyCAN |
| `raw.jsonl` | Exact received and transmitted byte chunks encoded as base64, with host timestamps |
| `events.jsonl` | Configuration responses, gaps, overflows, malformed lines, restarts and errors |

Frame columns:

```text
seq,segment,host_time_ns,elapsed_ns,bus,can_id,can_id_hex,extended,dlc,remote,data_hex
```

`can_id` is decimal. `can_id_hex` is hexadecimal without a prefix and retains identifier width. `data_hex` contains exactly DLC bytes for a data frame, without spaces. RTR frames have `remote=1`, their requested DLC, and an empty payload. CSV sequence numbers are local accepted-frame counters, not vehicle counters.

**Timestamps are host receive times, not CAN hardware timestamps.** Frames completed in one read share a timestamp, and Bluetooth buffering affects observed timing. Use `elapsed_ns` for relative time; wall time can change if the computer adjusts its clock. A new `segment` begins when valid reception resumes after a reported gap. An overflow's actual loss may begin before its notification; avoid interpreting segment boundaries as exact bus-time discontinuities.

The logs are append-only, flushed approximately once per second and fsynced at orderly shutdown. `session.json` is replaced atomically. A crash or power failure may lose buffered data and leave the status as `capturing`; raw and CSV records already written remain usable. An abrupt kill cannot guarantee a clean adapter shutdown.

## Recording quality

`inspect` reports capture quality and message counts:

```sh
python3 -m can_collector inspect captures/YOUR-SESSION
```

Watch the live counts, reported gaps, malformed lines and overflow events. **Zero reported gaps does not prove lossless capture.** Adapter or Bluetooth losses may not be reported. No missing frame count is fabricated.

An overflow is recorded even if it is appended to a partial frame. The collector waits for the adapter prompt and restarts `STM`, up to `--max-restarts` (default 10). It does not silently change filters. A disconnected transport or a bus error ends the session with the captured data preserved. Reconnect and begin a new recording; automatic Bluetooth reconnect is intentionally outside this first version.

If broad capture overflows, repeat with narrower adapter pass filters. All IDs and masks below are **hexadecimal**:

```sh
# One ID: 0x202 (decimal 514).
python3 -m can_collector capture --filter 202:7FF

# IDs 0x000 through 0x0FF.
python3 -m can_collector capture --filter 000:700

# Several exact IDs, combined as pass alternatives.
python3 -m can_collector capture --filter 202:7FF --filter 215:7FF
```

Filters limit recording coverage, and the manifest records the selected subset. Separate filtered recordings cannot establish simultaneous timing between groups. Leave known IDs included if they may contain unexplained fields. Use a dedicated CAN interface if you need broad lossless acquisition or precise bus timestamps.

Other supported configurations:

```sh
# MS-CAN uses pins 3/11 at 125 kbit/s by default. Check the vehicle's wiring first.
python3 -m can_collector capture --bus ms

# Change the selected bus's bitrate explicitly.
python3 -m can_collector capture --bus hs --bitrate 250000
```

No frames for 10 seconds produces a warning, not an automatic reset. Check ignition state, bus, bitrate and filters. A timed capture with no valid frames exits with status 2. Other failures exit with status 1. A completed recording exits with status 0 even when explicitly reported gaps occurred; inspect quality before using it for inference.

## Adapter behavior

The collector configures raw ISO 11898 (`STP 31` for HS-CAN or `STP 51` for MS-CAN), enables CAN IDs and DLCs, disables formatting/flow control/segmentation, selects `STCMM 0`, and uses filtered raw monitoring via `STM`. It removes wire-format spaces with `ATS0` to reduce output bandwidth, while keeping identifiers, DLCs and every payload byte. Both compact and spaced formats can be parsed. All configuration commands are checked. It aborts if silent monitoring or another required setting is rejected. Older firmware may reject the optional command that clears periodic messages.

Only an internal allowlist of adapter setup, identification, monitoring and protocol-close commands can be transmitted. There is no arbitrary command interface and no diagnostic polling, CAN payload injection, diagnostic discovery, ECU programming or PID scanning. Silence here describes `STM` monitoring mode with CAN acknowledgments disabled; it is not a hardware transmit-disconnect guarantee during every setup/teardown transition.

The initial synchronization uses an invalid `X` command or a monitoring interrupt rather than an unguarded blank carriage return, which could repeat a previous OBD request. Command traffic is included in `raw.jsonl` for review.

## Tests and hardware validation

Tests cover split transport reads, standard/extended IDs, DLC validation, truncated frames, bounded line buffers, malformed output, overflow recovery, restart limits, rejection of unsafe/unsupported commands, clean shutdown, real POSIX serial I/O through a pseudo-terminal, and process termination.

Physical MX+ Bluetooth acquisition has been verified on a macOS laptop with adapter firmware `STN2255 v5.13.1`. Broad acquisition exceeded the adapter output capacity even with compact output; filtered acquisition was also verified. Use filters for longer recordings, and assess each session's quality statistics.

Keep other Bluetooth hosts disconnected while using the MX+ on the Mac; pairing alone does not establish the data connection. Turning off Bluetooth on a phone that normally uses the adapter can help isolate connection problems.

The simulator and pseudo-terminal additionally validate framing, overflow handling and shutdown without requiring vehicle hardware.

These are observations from development, not a throughput guarantee for another vehicle or computer. Real captures and their analysis are excluded from this repository and from package distributions. Generate a synthetic session with `demo` to explore the format.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests and packaging checks. GitHub Actions runs the test suite and compiles the Bluetooth helper on macOS, then checks that installable distributions include the helper source and exclude capture/analysis directories.

Changes are listed in [CHANGELOG.md](CHANGELOG.md). The project is licensed under the [MIT License](LICENSE).

## Sources

- [OBDLink Family Reference and Programming Manual, revision F](https://www.scantool.net/scantool/downloads/682/obdlink_frpm_f.pdf), particularly protocol presets, monitoring, filters and error reporting.
- [Apple IOBluetoothDevice](https://developer.apple.com/documentation/iobluetooth/iobluetoothdevice), for paired-device access, service discovery and RFCOMM connections.
- [MX-5 ND2 channel research](https://github.com/gitgc/mx5-miata-nd2-obd-can), by gitgc (MIT), for the inference engine's 15 seed definitions.
- [Sixth-generation Mazda/MX-5 community DBC](https://github.com/berumiya/CAN_DBC_6thGenMazda), decoded and maintained by berumiya 990S labo ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)). Selected definitions are translated into this project's bit convention for a separate attribution lookup. They do not train the model or supply correlation anchors. Market/year/calibration differences are possible.

## Offline signal inference

Analyze the three newest **complete, real captures with frames**:

```sh
python3 -m can_collector infer
```

This reads existing files without opening Bluetooth or transmitting to the adapter. Each run creates a new `analysis/inference_TIMESTAMP/` directory; captures and existing analysis directories are never overwritten. Failed, empty and synthetic runs are excluded from automatic selection. Use `--latest 0` for all eligible recordings, or choose explicit recording directories:

```sh
python3 -m can_collector infer captures/FIRST captures/SECOND \
  --output analysis/my-inference --top 100
```

`--include-incomplete` permits automatic selection of failed runs that contain frames. Explicit directories may also be incomplete; their status and loss counts appear in the report. Synthetic inputs remain excluded. Repeated evidence must come from the same vehicle/configuration; select directories explicitly when collecting from multiple vehicles.

The engine:

- Fits encoding priors and a nearest-example shape model from known seed channels. Only varying channels train dynamics; the model reports class support and leave-one-entire-signal-out validation, including abstentions when training is insufficient.
- Enumerates contiguous packed 1–16-bit fields in both byte orders and byte-aligned 24/32-bit fields. It evaluates signed alternatives when they reduce jumps, preserves exact affine aliases across all frames, and excludes known seed bits from rediscovery.
- Detects modular counters with repeated compatible wraps, rejects padded counter containers and signed counter aliases, and ranks continuous/state candidates. Fast binary activity receives no switch bonus. Overlapping alternatives are reduced to primary nonoverlapping candidates; boundaries remain hypotheses.
- Compares candidates with varying seeds using same-session, same-bus, same-segment time bins, level/change correlation, split-half checks and circular-shift controls. It never joins separate recordings or interpolates across gaps. `--bin-seconds` defaults to 0.1 seconds.
- Finds coincident state changes in multiple message IDs, then consults the separately attributed lookup to distinguish additional published mappings from unnamed fields.

Scores are **ranking heuristics, not confidence probabilities or significance tests**. A single cold start cannot distinguish every switch, permission flag and engine-state signal. Constant channels cannot teach a mapping, and signal shapes alone cannot establish units, signedness, exact bit boundaries or physical meaning. Short stationary recordings often leave most dynamic training classes unsupported. Multiplexing and checksum algorithms are not automatically solved.

Output files include `report.md`, `candidates.json`, `candidates.csv`, full per-frame `traces.csv`, `model.json`, `catalog.json`, `events.json`, a definition snapshot and `run.json` with settings and SHA-256 hashes of inputs/code. If `gnuplot` is already installed, a candidate timeline PNG and reproducible plot files are also generated; it is optional.

Export an editable definition list, then use your own known channels:

```sh
python3 -m can_collector infer --export-definitions known-signals.json
python3 -m can_collector infer --definitions known-signals.json
```

The JSON replaces the built-in definitions. Each definition has a CAN ID, bus, extended flag, `field` (`start`, `width`, `order`, `signed`), shape `kind` (`state` or `continuous`), numeric `factor`/`offset`, optional `clamp` or gear `thresholds`, unit, source and `role`. Use `seed` to train/anchor; use `validation` only for attributed comparison after selection. No equations are evaluated as code. BE starts count bits MSB0 and LE starts LSB0, with zero-based bytes; these are **not DBC sawtooth start-bit numbers**.

Inference tests use independently generated packed signals, switches, counters, random decoys and rare carry transitions. They check recovery, separation of sessions/segments, constant-reference rejection, held-out leakage, attribution isolation and preservation of source files.
