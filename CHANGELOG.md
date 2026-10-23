# Changelog

## 0.1.1 — 2026-10-22

- Record classic-CAN frames through native macOS Bluetooth or a POSIX serial port.
- Preserve raw adapter traffic, canonical CSV, candump-compatible logs, metadata and quality events.
- Support HS-CAN, MS-CAN, explicit bitrates and repeatable adapter pass filters.
- Report overflows and gaps, restart monitoring within an explicit limit, and flush recordings on shutdown.
- Include a synthetic demo and tests for framing, recovery, serial I/O and process termination.
- Reuse paired Bluetooth service records and open RFCOMM asynchronously to handle macOS connection callbacks.
- Use compact adapter output to reduce bandwidth while retaining identifiers, DLCs and payloads.
- Add GitHub CI, packaging metadata and contribution documentation. Exclude real captures and analysis from source control and distributions.
