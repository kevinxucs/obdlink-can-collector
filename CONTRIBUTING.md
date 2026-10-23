# Contributing

## Set up

Use macOS with Python 3.11+ and Apple Command Line Tools or Xcode. From a checkout:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

The collector has no runtime Python dependencies. Tests use the standard library. The native Bluetooth transport uses Apple's IOBluetooth framework.

## Verify a change

```sh
python -m unittest discover -s tests -v
python -m can_collector build-bluetooth
python -m can_collector demo --simulate-overflow --output captures/demo-check
python -m can_collector inspect captures/demo-check
```

Choose a new output directory if `captures/demo-check` already exists. Tests and the demo do not connect to a vehicle. Building the helper does not open a Bluetooth connection.

For packaging changes:

```sh
python -m pip install build twine
python -m build
python -m twine check dist/*
```

Check that the wheel and source archive contain `can_collector/native/BluetoothBridge.swift` and exclude `captures/`, `analysis/`, compiled helpers and virtual environments. CI also installs the wheel and exercises its CLI outside the checkout.

## Pull requests and issues

Describe the concrete behavior, how to reproduce it, and how you verified the change. Add focused regression coverage for parser, protocol, transport or session-format changes. Keep the adapter command allowlist explicit, preserve raw data and gap reporting, and document format changes.

For connection problems, include macOS and Python versions, adapter model/firmware, the command used, and relevant error messages. Label synthetic data clearly. Prefer a minimal synthetic reproduction; do not attach a full vehicle capture by default. Remove Bluetooth addresses, VINs, local usernames and other personal information from any excerpts you choose to share.

Local recordings and analysis belong in the ignored `captures/` and `analysis/` directories. They are not project fixtures. Use generated synthetic data for reproducible examples and tests.
