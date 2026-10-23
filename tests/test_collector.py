import base64
from contextlib import redirect_stdout, redirect_stderr
import csv
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from can_collector.collector import AdapterError, Collector, Config, allowed_command, parse_filter
from can_collector.demo import DemoTransport
from can_collector.parser import ParseError, Tokenizer, parse_frame
from can_collector.session import Session
from can_collector.cli import main
from can_collector.transport import TransportError


ROOT = Path(__file__).resolve().parent.parent


class ParserTests(unittest.TestCase):
    def test_standard_and_short_frames(self):
        frame = parse_frame("202 8 1F 40 00 00 00 00 00 00")
        self.assertEqual(frame.can_id, 514)
        self.assertEqual(frame.data, bytes.fromhex("1F40000000000000"))
        self.assertFalse(frame.extended)
        self.assertEqual(parse_frame("080 2 01FF").data, b"\x01\xff")
        self.assertEqual(parse_frame("123 0").data, b"")

    def test_extended_formats_and_small_extended_id(self):
        for text in ("18DAF110 2 410C", "18 DA F1 10 2 41 0C"):
            frame = parse_frame(text)
            self.assertTrue(frame.extended)
            self.assertEqual(frame.can_id, 0x18DAF110)
            self.assertEqual(frame.dlc, 2)
        self.assertTrue(parse_frame("00000202 1 FF").extended)

    def test_remote_frames(self):
        frame = parse_frame("123 8 RTR")
        self.assertTrue(frame.remote)
        self.assertEqual(frame.data, b"")

    def test_compact_frames_keep_id_width_dlc_and_payload(self):
        for spaced, compact in (
            ("202 8 FFFFFFFFFFFFFFFF", "2028FFFFFFFFFFFFFFFF"),
            ("123 0", "1230"),
            ("18DAF110 2 410C", "18DAF1102410C"),
            ("00000202 1 FF", "000002021FF"),
            ("123 8 RTR", "1238RTR"),
            ("00000202 8 RTR", "000002028RTR"),
        ):
            with self.subTest(compact=compact):
                self.assertEqual(parse_frame(spaced), parse_frame(compact))

    def test_rejects_partial_missing_dlc_and_invalid_frames(self):
        for text in ("202 8 1F40", "202 FF FF FF FF FF FF FF FF", "800 0", "FFFFFFFF 0",
                     "202 9 000000000000000000", "202 1 GG", "202 0 FF", "2028FFFF", "123800RTR"):
            with self.subTest(text=text), self.assertRaises(ParseError):
                parse_frame(text)

    def test_chunking_cr_lf_prompts_and_non_ascii(self):
        tokenizer = Tokenizer()
        self.assertEqual(tokenizer.feed(b"202 2 01"), [])
        self.assertEqual(tokenizer.feed(b"FF\r\n>\r123 0\r"), ["202 2 01FF", ">", "123 0"])
        self.assertEqual(tokenizer.feed(b"\xff\r"), ["\\xff"])
        tokenizer.feed(b"partial")
        self.assertEqual(tokenizer.finish(), "partial")

    def test_overlong_line_is_bounded_and_visible(self):
        tokenizer = Tokenizer(limit=8)
        tokens = tokenizer.feed(b"F" * 100)
        self.assertEqual(len(tokens), 12)
        self.assertTrue(all(x.startswith("MALFORMED_OVERLONG_LINE") for x in tokens))
        self.assertLess(len(tokenizer.buffer), 8)


class ConfigTests(unittest.TestCase):
    def test_filters_are_hex_and_cannot_inject_commands(self):
        self.assertEqual(parse_filter("202:7ff"), ("202", "7FF"))
        self.assertEqual(parse_filter("18daf110:1fffffff"), ("18DAF110", "1FFFFFFF"))
        for text in ("800:7ff", "abc\rSTPX:fff", "202", "202:FFFFFFFF"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_filter(text)

    def test_payload_and_arbitrary_commands_are_refused(self):
        for command in ("010C", "STPX h:7DF,d:0100", "ATSH7DF", "STCMM1", "STM\r0100", "ATZ"):
            self.assertFalse(allowed_command(command))
        self.assertTrue(allowed_command("STCMM0"))


class CollectorTests(unittest.TestCase):
    def create(self, root, transport=None, **options):
        session = Session(Path(root) / "session", {"bus": "hs", "transport": "demo"})
        adapter = transport or DemoTransport()
        collector = Collector(adapter, session, Config(duration=0.5, **options), progress=lambda _: None)
        return session, adapter, collector

    def test_capture_and_shutdown_preserve_logs_without_own_stop_gap(self):
        with tempfile.TemporaryDirectory() as root:
            session, adapter, collector = self.create(root)
            collector.initialize()
            collector.capture()
            collector.stop()
            adapter.close()
            session.close("complete", "test")
            with (session.directory / "frames.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertGreater(len(rows), 20)
            self.assertEqual({x["can_id_hex"] for x in rows}, {"202", "215", "420"})
            self.assertEqual(session.stats["capture_gaps"], 0)
            self.assertEqual(session.stats["malformed_lines"], 0)
            self.assertTrue(all(int(x["dlc"]) * 2 == len(x["data_hex"]) for x in rows))
            self.assertTrue(all(allowed_command(x) for x in adapter.commands if x != "X"))
            self.assertIn("STCMM0", adapter.commands)
            self.assertNotIn("STMA", adapter.commands)
            self.assertEqual(adapter.commands[-1], "STPC")
            raw = [json.loads(x) for x in (session.directory / "raw.jsonl").read_text().splitlines()]
            self.assertTrue(any(x["direction"] == "rx" and base64.b64decode(x["base64"]) for x in raw))
            self.assertEqual(json.loads((session.directory / "session.json").read_text())["stats"]["frames"], len(rows))

    def test_overflow_is_reported_and_restarts_have_new_segment(self):
        with tempfile.TemporaryDirectory() as root:
            session, adapter, collector = self.create(root, DemoTransport(True))
            collector.initialize()
            collector.capture()
            collector.stop()
            session.close("complete", "test")
            self.assertEqual(session.stats["overflow_events"], 1)
            self.assertEqual(session.stats["restarts"], 1)
            self.assertEqual(session.stats["capture_gaps"], 1)
            self.assertEqual(session.stats["malformed_lines"], 1)
            with (session.directory / "frames.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual({x["segment"] for x in rows}, {"0", "1"})
            self.assertGreater(sum(x["segment"] == "1" for x in rows), 0)

    def test_restart_limit_terminates_instead_of_silently_dropping(self):
        with tempfile.TemporaryDirectory() as root:
            session, _, collector = self.create(root, DemoTransport(True), max_restarts=0)
            collector.initialize()
            with self.assertRaisesRegex(AdapterError, "restart limit"):
                collector.capture()
            collector.stop()
            session.close("failed", "restart limit")

    def test_configuration_rejection_aborts_before_monitoring(self):
        class RejectSilent(DemoTransport):
            def write(self, data):
                if data == b"STCMM0\r":
                    self.commands.append("STCMM0")
                    self.output.extend(b"?\r>")
                else:
                    super().write(data)

        with tempfile.TemporaryDirectory() as root:
            session, adapter, collector = self.create(root, RejectSilent())
            with self.assertRaisesRegex(AdapterError, "STCMM0"):
                collector.initialize()
            self.assertNotIn("STM", adapter.commands)
            self.assertNotIn("STPO", adapter.commands)
            session.close("failed", "configuration rejected")

    def test_selected_filter_is_explicit_and_not_replaced(self):
        with tempfile.TemporaryDirectory() as root:
            session, adapter, collector = self.create(root, filters=(("202", "7FF"),))
            collector.initialize()
            self.assertIn("STFPA202,7FF", adapter.commands)
            self.assertNotIn("STFPA000,000", adapter.commands)
            collector.stop()
            session.close("complete", "test")

    def test_existing_directory_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "keep.txt"
            marker.write_text("keep")
            with self.assertRaises(FileExistsError):
                Session(Path(root), {})
            self.assertEqual(marker.read_text(), "keep")


class IntegrationTests(unittest.TestCase):
    def test_no_frames_has_explicit_status_and_nonzero_exit(self):
        class SilentAdapter(DemoTransport):
            def read(self, timeout):
                if self.monitoring:
                    time.sleep(min(timeout, 0.01))
                    return b""
                return super().read(timeout)

        with tempfile.TemporaryDirectory() as root, patch("can_collector.cli.DemoTransport", SilentAdapter):
            output = Path(root) / "no-data"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = main(["demo", "--duration", "0.05", "--output", str(output)])
            self.assertEqual(result, 2)
            meta = json.loads((output / "session.json").read_text())
            self.assertEqual(meta["status"], "no_data")
            self.assertEqual(meta["stats"]["frames"], 0)

    def test_disconnect_preserves_frames_and_marks_failed_session(self):
        class DisconnectingAdapter(DemoTransport):
            def read(self, timeout):
                if self.count >= 3 and not self.output:
                    raise TransportError("simulated Bluetooth disconnect")
                return super().read(timeout)

        with tempfile.TemporaryDirectory() as root, patch("can_collector.cli.DemoTransport", DisconnectingAdapter):
            output = Path(root) / "disconnected"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = main(["demo", "--duration", "1", "--output", str(output)])
            self.assertEqual(result, 1)
            meta = json.loads((output / "session.json").read_text())
            self.assertEqual(meta["status"], "failed")
            self.assertGreater(meta["stats"]["frames"], 0)
            self.assertGreater(meta["stats"]["capture_gaps"], 0)
            events = [json.loads(x) for x in (output / "events.jsonl").read_text().splitlines()]
            self.assertTrue(any(x["type"] == "fatal_error" and "disconnect" in x["message"] for x in events))

    def test_real_serial_transport_with_fragmented_pseudo_terminal(self):
        master, slave = pty.openpty()
        port = os.ttyname(slave)
        adapter = DemoTransport()
        done = threading.Event()

        def emulate():
            while not done.is_set():
                ready, _, _ = select.select([master], [], [], 0.001)
                if ready:
                    data = os.read(master, 4096)
                    if data:
                        adapter.write(data)
                data = adapter.read(0.001)
                if data:
                    os.write(master, data)

        worker = threading.Thread(target=emulate, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as root:
                output = Path(root) / "real-serial"
                result = subprocess.run([sys.executable, "-m", "can_collector", "capture", "--port", port,
                                         "--duration", "0.3", "--output", str(output)],
                                        cwd=ROOT, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                meta = json.loads((output / "session.json").read_text())
                self.assertFalse(meta["synthetic"])
                self.assertGreater(meta["stats"]["frames"], 10)
                self.assertEqual(meta["status"], "complete")
                self.assertEqual(meta["stats"]["malformed_lines"], 0)
        finally:
            done.set()
            worker.join(timeout=2)
            os.close(master)
            os.close(slave)

    def test_sigterm_saves_capture_and_closes_monitor(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "signal"
            process = subprocess.Popen([sys.executable, "-m", "can_collector", "demo", "--duration", "30",
                                        "--output", str(output)], cwd=ROOT,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    manifest = output / "session.json"
                    if manifest.exists() and json.loads(manifest.read_text())["status"] == "capturing":
                        break
                    time.sleep(0.02)
                time.sleep(0.15)
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                meta = json.loads((output / "session.json").read_text())
                self.assertEqual(meta["status"], "complete")
                self.assertGreater(meta["stats"]["frames"], 0)
                self.assertEqual(meta["stats"]["capture_gaps"], 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main()
