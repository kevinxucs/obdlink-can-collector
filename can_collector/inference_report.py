"""Reproducible offline inference artifacts, with attribution and uncertainty."""
from dataclasses import asdict
from datetime import datetime, timezone
import csv
import json
import math
from pathlib import Path
import shutil
import subprocess

from .inference import ENGINE_VERSION, bins, catalog_inventory, coincident_events, digest
from .signals import definitions_json


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def export_analysis(directory, engine, candidates, counts, skipped):
    directory = directory.resolve()
    if directory.exists():
        raise ValueError(f"Output already exists: {directory}; choose a new analysis directory")
    # Never write into capture directories, including an as-yet nonexistent one.
    for r in engine.recordings:
        if directory == r.path.resolve() or r.path.resolve() in directory.parents:
            raise ValueError("Analysis output must be outside the source capture directory")
    directory.mkdir(parents=True)
    definitions = definitions_json(engine.definitions)
    write_json(directory / "definitions.json", definitions)
    write_json(directory / "model.json", engine.model.export())
    inventory = catalog_inventory(engine.recordings, engine.definitions)
    events = coincident_events(engine.recordings, candidates, engine.definitions)
    write_json(directory / "catalog.json", inventory)
    write_json(directory / "events.json", events)
    write_json(directory / "candidates.json", [c.export() for c in candidates])
    sources = [{"path": str(r.path), "name": r.name, "status": r.metadata.get("status"),
                "synthetic": r.metadata.get("synthetic", False), "frames": r.frames,
                "duration_seconds": r.duration, "stats": r.metadata.get("stats"),
                "filters": r.metadata.get("config", {}).get("filters"), "sha256": r.hashes}
               for r in engine.recordings]
    write_json(directory / "run.json", {
        "schema_version": 1, "engine_version": ENGINE_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(), "sources": sources,
        "skipped": skipped, "counts": counts, "bin_seconds": engine.bin_seconds,
        "definitions_sha256": digest(directory / "definitions.json"),
        "clock": "seconds since first frame in EACH recording; host receive time",
        "rank_score": "heuristic; not a probability or statistical significance",
        "novelty": "Additional to seed catalog only. Unverified fields are not proven new worldwide.",
        "engine_source_sha256": {p.name: digest(p) for p in (Path(__file__), Path(__file__).with_name("inference.py"), Path(__file__).with_name("signals.py"))},
    })
    with (directory / "candidates.csv").open("w", newline="") as stream:
        columns = ["candidate", "bus", "can_id_hex", "extended", "dlc", "start", "width", "order", "signed", "location", "kind", "score", "published_names", "strongest_anchor", "r", "delta_r", "variable_recordings", "aliases"]
        writer = csv.DictWriter(stream, columns)
        writer.writeheader()
        for c in candidates:
            strongest = max(c.associations, key=lambda a: abs(a["r"]), default={})
            writer.writerow({"candidate": c.id, "bus": c.key[0], "can_id_hex": c.export()["can_id_hex"],
                             "extended": int(c.key[2]), "dlc": c.key[3], **asdict(c.field),
                             "location": c.field.describe(), "kind": c.kind, "score": c.score,
                             "published_names": ";".join(d["name"] for d in c.catalog_matches),
                             "strongest_anchor": strongest.get("anchor", ""), "r": strongest.get("r", ""),
                             "delta_r": strongest.get("delta_r", ""),
                             "variable_recordings": sum(s["unique"] > 1 for s in c.per_session), "aliases": len(c.aliases)})
    with (directory / "traces.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["candidate", "recording", "t", "segment", "raw"])
        for c in candidates:
            for r in engine.recordings:
                if c.key in r.streams:
                    for t, segment, data in r.streams[c.key].rows:
                        writer.writerow([c.id, r.name, f"{t:.9f}", segment, c.field.decode(data)])
    plots = plot_candidates(directory, engine, candidates)
    (directory / "report.md").write_text(render_report(engine, candidates, counts, sources, inventory, plots, events))
    # All source files are read-only and must still match their input hashes.
    for r in engine.recordings:
        for name, expected in r.hashes.items():
            if digest(r.path / name) != expected:
                raise RuntimeError(f"Source changed during analysis: {r.path / name}")
    return directory / "report.md"


def plot_candidates(directory, engine, candidates):
    """Optional scientific plots with the installed standard plotting tool."""
    gnuplot = shutil.which("gnuplot")
    if not gnuplot:
        return []
    recording = max(engine.recordings, key=lambda r: r.duration)
    shown = [c for c in candidates if c.kind != "counter" and c.key in recording.streams
             and next(s["unique"] for s in c.per_session if s["recording"] == recording.name) > 1][:8]
    if not shown:
        return []
    commands = ["set terminal pngcairo size 1600,1800 font 'Arial,13'", "set output 'candidates.png'",
                f"set multiplot layout {len(shown)},1 title {json.dumps(recording.name + ': candidate raw values (host time)')}",
                "set grid xtics ytics", "set key off", "set lmargin 12", "set rmargin 4", "set format y '%g'", "set xlabel 'Seconds since first frame'", "set ylabel 'Raw'"]
    for c in shown:
        s = recording.streams[c.key]
        data_path = directory / f"{c.id}.dat"
        binned = bins(s.rows, [c.field.decode(row[2]) for row in s.rows], engine.bin_seconds)
        # Explicit blank lines keep gnuplot from connecting capture segments.
        previous = None
        with data_path.open("w") as stream:
            for (segment, slot), value in sorted(binned.items()):
                if previous is not None and (segment != previous[0] or slot != previous[1] + 1):
                    stream.write("\n")
                stream.write(f"{slot * engine.bin_seconds:.9f} {value}\n")
                previous = (segment, slot)
        title = f"{c.id}: 0x{c.export()['can_id_hex']} {c.field.describe()} [{c.kind}]"
        interval = max(1e-9, (max(binned.values()) - min(binned.values())) / 4)
        magnitude = 10 ** math.floor(math.log10(interval))
        tick = next(v * magnitude for v in (1, 2, 5, 10) if v * magnitude >= interval)
        commands += [f"set ytics {tick:g}", f"set title {json.dumps(title)}", f"plot '{data_path.name}' using 1:2 with lines lw 1.5 lc rgb '#226b9c'"]
    commands.append("unset multiplot")
    (directory / "plot.gp").write_text("\n".join(commands) + "\n")
    try:
        result = subprocess.run([gnuplot, "plot.gp"], cwd=directory, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        (directory / "plot-error.txt").write_text(str(exc))
        return []
    if result.returncode:
        (directory / "plot-error.txt").write_text(result.stderr)
        return []
    return ["candidates.png"]


def render_report(engine, candidates, counts, sources, inventory, plots, events):
    lines = ["# CAN inference results", "", "Offline analysis of existing recordings. No adapter connection, diagnostic polling or CAN transmission.", "",
             "Scores rank fields worth investigating; they are not confidence probabilities. Physical names below come from a separately attributed community lookup, consulted after candidate selection. Unnamed fields remain hypotheses, with no inferred units or calibration.", "",
             "## Recording coverage", "", "| Recording | Frames | Span (s) | Gaps | Coverage |", "| --- | ---: | ---: | ---: | --- |"]
    for s in sources:
        lines.append(f"| {s['name']} | {s['frames']:,} | {s['duration_seconds']:.3f} | {s['stats'].get('capture_gaps', 0)} | {s['filters'] or 'all IDs'} |")
    lines += ["", "Separate sessions are never joined into one timeline. Correlation requires overlapping time bins in the same recording and segment. No forward fill/interpolation across gaps. Timestamps are Bluetooth host receive times; losses can start before an overflow notification.", "",
              f"Scanned {counts['interpretations_scanned']:,} field interpretations in {counts['message_streams']} message streams (bus, ID, extended flag, DLC). Retained {counts['retained_nonoverlapping_fields']} fields after affine-alias grouping and overlapping-container suppression; exported the top {len(candidates)}.", "",
              "## Training and validation", ""]
    model = engine.model.export()
    lines += [f"The {model['seed_count']} seed definitions train encoding-width/order priors. Dynamic training support: `{model['dynamic_class_support']}`. Constant channels are excluded from dynamic training and correlation, since agreement at a constant value carries no identification evidence.", "",
              f"Leave-one-entire-signal-out validation scored {model['validation']['scored']} folds and abstained on {model['validation']['abstained']}. Accuracy where scored: {model['validation']['accuracy_when_scored']}. All sessions of the held-out signal are excluded together. This evaluates shape classification only, not physical mapping.", "",
              "Unsupported classes use explicit state/smoothness rules. Counters require a dominant nonzero modular increment and repeated wraps on consecutive frames within segments. High-entropy fields can be checksums, mixed fields or data; no checksum algorithm is claimed without verification.", "",
              "## Additional published mappings observed", "", "These are additional to the RaceChrono seed list, **not new discoveries in the wider CAN community**. They are read using the published field definition, not a guessed conversion from a correlation.", "",
              "| Mapping | CAN ID / field | Observed range | Excited | Source |", "| --- | --- | --- | --- | --- |"]
    for item in inventory:
        d = item["definition"]
        if d["role"] != "validation" or not item["observed"]:
            continue
        low = min(s["min"] for s in item["sessions"])
        high = max(s["max"] for s in item["sessions"])
        matched = [c.id for c in candidates if any(x["name"] == d["name"] for x in c.catalog_matches)]
        status = "variable" if item["excited"] else "constant; function not tested"
        lines.append(f"| {d['name']} {'('+','.join(matched)+')' if matched else '(lookup only)'} | 0x{d['can_id']:03X}, {d['field']['start']}:{d['field']['width']} BE/MSB0 | {low:.4g}–{high:.4g} {d['unit']} | {status} | [community DBC]({d['source']}) |")
    lines += ["", "Market/year/calibration differences remain possible. A variable field matching a published layout supports investigation; a plausible constant value does not validate its function.", "",
              "## Ranked field candidates", "", "Byte offsets are zero-based. Single-bit labels use LSB=0 within the byte. Other BE starts count MSB0; LE starts count LSB0. These starts are not DBC sawtooth numbers.", "",
              "| Candidate | ID | Field | Shape | Score | Best seed association | Published lookup |", "| --- | --- | --- | --- | ---: | --- | --- |"]
    for c in candidates[:35]:
        strongest = max(c.associations, key=lambda a: abs(a["r"]), default=None)
        association = f"{strongest['anchor']}: r={strongest['r']:.3f}, Δr={strongest['delta_r']:.3f}" if strongest and strongest['delta_r'] is not None else f"{strongest['anchor']}: r={strongest['r']:.3f}" if strongest else "none informative"
        names = ", ".join(x["name"] for x in c.catalog_matches) or "unknown"
        lines.append(f"| {c.id} | 0x{c.export()['can_id_hex']} | {c.field.describe()} | {c.kind} | {c.score:.1f} | {association} | {names} |")
    lines += ["", "## Shared state-change events", "",
              "Detected without physical event labels or the validation lookup. A cluster requires changes in at least two message IDs within 0.25 seconds, in the same recording and segment. Coincidence suggests shared state; it does not identify a control or prove causality. The strongest eight clusters are shown.", "",
              "| Recording | Time range (s) | IDs changing | Fields |", "| --- | --- | ---: | --- |"]
    for e in events[:8]:
        fields = ", ".join(sorted({x["field"] for x in e["changes"]}))
        lines.append(f"| {e['recording']} | {e['start_t']:.3f}–{e['end_t']:.3f} | {e['distinct_messages']} | {fields} |")
    lines += ["", "## Evidence for leading unknown fields", ""]
    unknown = [c for c in candidates if not c.catalog_matches and c.kind != "counter"][:12]
    for c in unknown:
        lines += [f"### {c.id}: 0x{c.export()['can_id_hex']} — {c.field.describe()}", "",
                  f"Shape: {c.kind}. Meaning, scale and signedness are unverified. {len(c.aliases)} equivalent field interpretations across all source frames.", ""]
        for s in c.per_session:
            changes = ", ".join(f"{x['t']:.3f}s→{x['raw']}" for x in s["first_changes"][:12])
            lines.append(f"- {s['recording']}: raw {s['min']}..{s['max']}, {s['unique']} values, {s['transitions_within_segments']} changes within segments. First states: {changes}{' …' if s['changes_truncated'] or len(s['first_changes']) > 12 else ''}.")
        lines.append("")
        for a in c.associations:
            control = a.get("shift_control") or {}
            lines += [f"Association with `{a['anchor']}` in {a['recording']}: r={a['r']:.4f}; delta correlation={a['delta_r']}; {a['bins']} common bins; {a['anchor_transitions']} anchor transitions. Split-half correlations={a['split_half_r']}. Shift-control margin={control.get('margin')}. This is association, not proof of function.", ""]
        if c.kind == "state":
            lines += ["Next validation: repeat the relevant control action several times with ignition/engine state held steady. Separate clutch presses from engine starts; then capture start/stop separately. A one-event match cannot distinguish a control switch, permission flag and operating-state flag.", ""]
        else:
            lines += ["Next validation: repeat a controlled change of the suspected quantity while retaining an informative known reference in the same capture. Require repeatable level and change agreement before assigning a name or scale.", ""]
    counters = [c for c in candidates if c.counter]
    lines += ["## Packet structure candidates", "", "Counter labels describe observed sequence structure only; they do not identify an OEM signal. No exact total lost-frame count is derived from them.", ""]
    for c in counters[:20]:
        e = c.counter
        lines.append(f"- {c.id}: 0x{c.export()['can_id_hex']}, {c.field.describe()}: +{e['step']} modulo {e['modulus']}, {100*e['agreement']:.2f}% of {e['transitions']} within-segment transitions, {e['wraps']} wraps.")
    lines += ["", "## Limits and files", "",
              "Unknown boundary/endian alternatives are preserved in candidates.json. Affine-equivalent traces cannot establish true bit width or units. Single-event, trend and high correlation matches may share a common cause. Seven circular shifts within segments are a diagnostic control, not a p-value; there is no multiple-testing significance claim.", "",
              "The short full-bus capture and stationary cold start cannot establish signals that never vary. Multiplexed frames are not automatically decoded; a selector or counter may otherwise look like a state. No semantic mapping is promoted to confirmed automatically.", "",
              "- [Machine-readable candidates](candidates.json), [candidate table](candidates.csv), [full raw candidate traces](traces.csv).",
              "- [Fitted model and held-out results](model.json), [definition snapshot](definitions.json), [observed catalog](catalog.json).",
              "- [Coincident state-change clusters](events.json).",
              "- [Run settings, source paths and SHA-256 hashes](run.json). Source capture files are unchanged."]
    for plot in plots:
        lines += [f"- [Candidate timeline plot]({plot})."]
    lines.append("")
    return "\n".join(lines)
