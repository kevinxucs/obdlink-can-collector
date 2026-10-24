import csv
import io
import json
import math
from pathlib import Path
import random
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from can_collector.cli import main
from can_collector.inference import (InferenceEngine, Recording, SeedModel, Stream,
                                     association, bins, coincident_events, counter_evidence,
                                     load_recording, select_recordings)
from can_collector.inference_report import export_analysis
from can_collector.signals import Definition, Field, definitions_json, load_definitions


def recording(name, messages):
    streams = {}
    for ident, rows in messages.items():
        key = ("hs", ident, False, len(rows[0][2]))
        streams[key] = Stream(key, rows)
    return Recording(Path(name), {"status": "complete", "synthetic": True, "stats": {"capture_gaps": 0}},
                     streams, max(rows[-1][0] for rows in messages.values()),
                     sum(len(rows) for rows in messages.values()), {})


class FieldTests(unittest.TestCase):
    def test_cross_byte_fields_and_sign(self):
        for value in (0, 1, 1701, 4095):
            self.assertEqual(Field(4, 12, "little").decode((value << 4).to_bytes(2, "little")), value)
            self.assertEqual(Field(4, 12, "big").decode(value.to_bytes(2, "big")), value)
        self.assertEqual(Field(0, 8, signed=True).decode(bytes([255])), -1)
        self.assertEqual(Field(0, 1).physical_bits(), Field(7, 1, "little").physical_bits())
        with self.assertRaises(ValueError):
            Field(0, 16).decode(b"\x00")

    def test_custom_definitions_roundtrip_no_eval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "definitions.json"
            seed = Definition("custom", 0x100, Field(4, 12, "little"), factor=.25)
            path.write_text(json.dumps(definitions_json([seed])))
            self.assertEqual(load_definitions(path), [seed])
            data = json.loads(path.read_text())
            data["definitions"][0]["equation"] = "__import__('os')"
            path.write_text(json.dumps(data))
            with self.assertRaises(TypeError):
                load_definitions(path)


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Ground truth is generated independently of the inference code. A
        # genuinely packed LE analog field, a BE copy, a switch and decoys.
        rng = random.Random(182)
        messages = {ident: [] for ident in (0x100, 0x101, 0x300, 0x301, 0x302, 0x303, 0x304)}
        for i in range(360):
            t = i * .1
            value = round(2000 + 1250 * math.sin(i / 13) + 500 * math.cos(i / 19))
            switch = int((i // 31) % 2 == 0)
            for ident, payload in (
                (0x100, (value * 2 + 10).to_bytes(2, "big")),
                (0x101, bytes([switch])),
                (0x300, (value << 4).to_bytes(2, "little")),
                (0x301, bytes([0xA0 | switch])),
                (0x302, bytes([(i * 5) % 256])),
                (0x303, bytes([rng.randrange(256)])),
                (0x304, value.to_bytes(2, "big")),
            ):
                messages[ident].append((t, 0, payload))
        cls.r = recording("fixture", messages)
        cls.seeds = [Definition("analog", 0x100, Field(0, 16)),
                     Definition("switch", 0x101, Field(0, 8), kind="state")]
        cls.engine = InferenceEngine([cls.r], cls.seeds)
        cls.candidates, cls.counts = cls.engine.run(100)

    def test_recovers_packed_little_endian_and_big_endian(self):
        for ident, expected in ((0x300, Field(4, 12, "little")), (0x304, Field(0, 16))):
            matches = [c for c in self.candidates if c.key[1] == ident and
                       (c.field == expected or expected in [Field(**f) for f in c.aliases])]
            self.assertTrue(matches, f"missing packed field 0x{ident:X}")
            self.assertGreater(max(abs(a["r"]) for c in matches for a in c.associations if a["anchor"] == "analog"), .99)
            self.assertLess(matches[0].associations[0]["shift_control"]["max_shifted_abs_r"], .9)

    def test_seed_bits_are_not_rediscovered(self):
        self.assertFalse(any(c.key[1] in (0x100, 0x101) for c in self.candidates))

    def test_counter_cannot_return_as_signed_continuous_signal(self):
        matches = [c for c in self.candidates if c.key[1] == 0x302]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].kind, "counter")
        self.assertEqual(matches[0].counter["step"], 5)
        self.assertEqual(matches[0].counter["modulus"], 256)
        analog = max(c.score for c in self.candidates if c.key[1] == 0x300)
        noise = max((c.score for c in self.candidates if c.key[1] == 0x303), default=0)
        self.assertGreater(analog, noise + 10)

    def test_state_bit_is_recovered(self):
        matches = [c for c in self.candidates if c.key[1] == 0x301]
        self.assertTrue(any(c.field == Field(7, 1) and c.kind == "state" for c in matches))

    def test_validation_catalog_does_not_train_or_change_ranking(self):
        added = Definition("secret_name", 0x300, Field(4, 12, "little"), role="validation")
        other = InferenceEngine([self.r], self.seeds + [added])
        candidates, _ = other.run(100)
        self.assertEqual(other.model.export(), self.engine.model.export())
        self.assertEqual([(c.key, c.field, c.score) for c in candidates],
                         [(c.key, c.field, c.score) for c in self.candidates])
        self.assertTrue(any(c.catalog_matches for c in candidates))

    def test_constant_seed_and_nonconcurrent_sessions_produce_no_association(self):
        const = recording("constant", {0x100: [(i*.1, 0, b"\x00\x64") for i in range(100)],
                                       0x300: [(i*.1, 0, bytes([i])) for i in range(100)]})
        engine = InferenceEngine([const], self.seeds)
        candidates, _ = engine.run()
        self.assertEqual(engine.model.examples, [])
        self.assertTrue(all(not c.associations for c in candidates))
        # A reference in a DIFFERENT recording cannot explain a candidate.
        only_ref = recording("reference_only", {0x100: self.r.streams[("hs", 0x100, False, 2)].rows})
        only_unknown = recording("candidate_only", {0x300: self.r.streams[("hs", 0x300, False, 2)].rows})
        candidates, _ = InferenceEngine([only_ref, only_unknown], self.seeds).run()
        self.assertTrue(all(not c.associations for c in candidates))

    def test_held_out_signal_excludes_every_recording(self):
        model = SeedModel.fit([self.r, self.r], [self.seeds[0]])
        validation = model.export()["validation"]
        self.assertEqual(validation["scored"], 0)
        self.assertEqual(validation["abstained"], 2)

    def test_segments_not_interpolated_and_constant_reference_rejected(self):
        candidate = {(0, i): i for i in range(30)}
        reference = {(1, i): i for i in range(30)}
        self.assertIsNone(association(candidate, reference))
        self.assertIsNone(association(candidate, {(0, i): 1 for i in range(30)}))
        rows = [(0.0, 0, b""), (10.0, 1, b"")]
        self.assertEqual(len(bins(rows, [0, 100])), 2)

    def test_counter_requires_compatible_wraps_and_ignores_segment_boundary(self):
        values = [(i * 5) % 256 for i in range(400)]
        rows = [(i*.01, int(i >= 200), b"") for i in range(400)]
        e = counter_evidence(Field(0, 8), [(rows, values)])
        self.assertEqual(e["transitions"], 398)
        self.assertIsNone(counter_evidence(Field(0, 16), [(rows, [v + 4096 for v in values])]))
        self.assertIsNone(counter_evidence(Field(0, 24), [(rows, [(v << 16) | 7 for v in values])]))

    def test_signed_signal_recovery_requires_reduced_jumps(self):
        rows = [(i*.1, 0, (round(1000*math.sin(i/13)) % 65536).to_bytes(2, "big")) for i in range(240)]
        r = recording("signed", {0x100: rows, 0x300: rows})
        seeds = [Definition("signed_reference", 0x100, Field(0, 16, signed=True))]
        candidates, _ = InferenceEngine([r], seeds).run()
        self.assertTrue(any(c.key[1] == 0x300 and c.field == Field(0, 16, signed=True) for c in candidates))

    def test_shared_state_events_use_multiple_ids_and_skip_gap_changes(self):
        events = coincident_events([self.r], self.candidates, self.seeds)
        self.assertTrue(events)
        self.assertTrue(all(e["distinct_messages"] >= 2 for e in events))
        rows = [(i*.1, int(i>=50), bytes([int(i>=50)])) for i in range(100)]
        r = recording("gap", {0x100: rows, 0x101: rows})
        definitions = [Definition("a", 0x100, Field(0, 8), kind="state"),
                       Definition("b", 0x101, Field(0, 8), kind="state")]
        self.assertEqual(coincident_events([r], [], definitions), [])

    def test_aliases_use_full_capture_including_unsampled_transient(self):
        messages = {0x300: [(i*.01, 0, (2048 if i == 997 else 2000 + i % 8).to_bytes(2, "big")) for i in range(2000)]}
        r = recording("brief_carry", messages)
        candidates, _ = InferenceEngine([r], []).run()
        for c in candidates:
            vv = [c.field.decode(row[2]) for row in r.streams[c.key].rows]
            for alias in c.aliases:
                av = [Field(**alias).decode(row[2]) for row in r.streams[c.key].rows]
                # An alias must remain an affine transform at the rare carry.
                from can_collector.inference import affine_signature
                self.assertEqual(affine_signature(vv), affine_signature(av))


class InputAndCliTests(unittest.TestCase):
    def test_selection_skips_synthetic_failed_and_empty_and_loader_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, status, synthetic, count in (("a", "complete", False, 1), ("b", "failed", False, 1),
                                                     ("c", "complete", True, 1), ("d", "complete", False, 0)):
                path = root / name
                path.mkdir()
                (path / "session.json").write_text(json.dumps({"status": status, "synthetic": synthetic,
                                                                "stats": {"frames": count}}))
            paths, skipped = select_recordings(root)
            self.assertEqual([p.name for p in paths], ["a"])
            self.assertEqual(len(skipped), 3)
            paths, _ = select_recordings(root, include_incomplete=True)
            self.assertEqual([p.name for p in paths], ["a", "b"])
            (root / "a" / "frames.csv").write_text("seq,segment,elapsed_ns,bus,can_id,extended,dlc,remote,data_hex\n1,0,0,hs,256,0,2,0,01\n")
            with self.assertRaisesRegex(ValueError, "Payload length"):
                load_recording(root / "a")
            with self.assertRaisesRegex(ValueError, "Synthetic"):
                load_recording(root / "c")

    def test_cli_exports_editable_seeds_and_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            target = Path(tmp) / "definitions.json"
            self.assertEqual(main(["infer", "--export-definitions", str(target)]), 0)
            original = target.read_bytes()
            self.assertEqual(main(["infer", "--export-definitions", str(target)]), 1)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(len(load_definitions(target)), 28)
            output = Path(tmp) / "existing"
            output.mkdir()
            self.assertEqual(main(["infer", "--output", str(output)]), 1)

    def test_engine_export_is_reproducible_and_read_only(self):
        # A temporary source directory is sufficient: no adapter/mock transport.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "capture"
            source.mkdir()
            for name in ("session.json", "frames.csv"):
                (source / name).write_text("preserve me\n")
            from can_collector.inference import digest
            r = recording(str(source), {0x300: [(i*.1, 0, bytes([i % 7])) for i in range(100)]})
            r.hashes = {name: digest(source / name) for name in ("session.json", "frames.csv")}
            engine = InferenceEngine([r], [])
            candidates, counts = engine.run()
            with patch("can_collector.inference_report.shutil.which", return_value=None):
                report = export_analysis(root / "analysis", engine, candidates, counts, [])
                self.assertTrue(report.exists())
                self.assertTrue(json.loads((report.parent / "candidates.json").read_text()))
                with self.assertRaises(ValueError):
                    export_analysis(root / "analysis", engine, candidates, counts, [])
                with self.assertRaises(ValueError):
                    export_analysis(source / "nested", engine, candidates, counts, [])
            self.assertTrue(all(digest(source / name) == value for name, value in r.hashes.items()))


if __name__ == "__main__":
    unittest.main()
