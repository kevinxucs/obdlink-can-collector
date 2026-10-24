"""Offline CAN field discovery with seed-trained priors and explicit evidence.

Scores are ranking heuristics, never probabilities of a physical interpretation.
Sessions, buses, extended IDs, DLCs and capture segments remain separate.
"""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field as dc_field
from functools import reduce
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics

from .signals import Field


ENGINE_VERSION = "1.0"


@dataclass
class Stream:
    key: tuple  # bus, CAN ID, extended, DLC
    rows: list  # (seconds since first session frame, segment, payload)


@dataclass
class Recording:
    path: Path
    metadata: dict
    streams: dict
    duration: float
    frames: int
    hashes: dict

    @property
    def name(self):
        return self.path.name


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def select_recordings(root: Path, latest: int = 3, include_incomplete: bool = False):
    eligible, skipped = [], []
    for manifest in sorted(root.glob("*/session.json")):
        metadata = json.loads(manifest.read_text())
        reason = None
        if metadata.get("synthetic", False):
            reason = "synthetic"
        elif not metadata.get("stats", {}).get("frames", 0):
            reason = "no frames"
        elif metadata.get("status") != "complete" and not include_incomplete:
            reason = "incomplete (use --include-incomplete to analyze explicitly)"
        if reason:
            skipped.append({"path": str(manifest.parent.resolve()), "reason": reason})
        else:
            eligible.append((metadata.get("created_utc", manifest.parent.name), manifest.parent))
    paths = [path for _, path in sorted(eligible)]
    return (paths[-latest:] if latest else paths), skipped


def load_recording(path: Path, allow_synthetic: bool = False) -> Recording:
    path = path.resolve()
    metadata = json.loads((path / "session.json").read_text())
    if metadata.get("synthetic", False) and not allow_synthetic:
        raise ValueError(f"Synthetic capture excluded: {path}")
    groups = defaultdict(list)
    first = last = previous_seq = None
    total = 0
    with (path / "frames.csv").open(newline="") as stream:
        for row in csv.DictReader(stream):
            stamp, seq = int(row["elapsed_ns"]), int(row["seq"])
            if last is not None and (stamp < last or seq <= previous_seq):
                raise ValueError(f"Out of order timestamp/sequence in {path}")
            first = stamp if first is None else first
            last, previous_seq = stamp, seq
            ident, dlc = int(row["can_id"]), int(row["dlc"])
            extended = bool(int(row["extended"]))
            if not 0 <= dlc <= 8 or not 0 <= ident <= (0x1FFFFFFF if extended else 0x7FF):
                raise ValueError(f"Invalid CAN ID or DLC in {path}")
            data = bytes.fromhex(row["data_hex"])
            total += 1
            if int(row["remote"]):
                continue
            if len(data) != dlc:
                raise ValueError(f"Payload length mismatch in {path}")
            groups[(row["bus"], ident, extended, dlc)].append(((stamp - first) / 1e9, int(row["segment"]), data))
    if not total:
        raise ValueError(f"Capture contains no frames: {path}")
    if metadata.get("stats", {}).get("frames") != total:
        raise ValueError(f"Manifest/CSV frame counts differ in {path}")
    return Recording(path, metadata, {key: Stream(key, rows) for key, rows in groups.items()},
                     (last - first) / 1e9, total,
                     {name: digest(path / name) for name in ("session.json", "frames.csv")})


def corr(x, y):
    if len(x) < 8 or len(x) != len(y):
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    xx = sum((v - mx) ** 2 for v in x)
    yy = sum((v - my) ** 2 for v in y)
    if xx <= 1e-20 or yy <= 1e-20:
        return None
    return max(-1.0, min(1.0, sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(xx * yy)))


def indices(n, limit=768):
    if n <= limit:
        return list(range(n))
    # Consecutive prefix for packet structure; uniform coverage for later states.
    prefix = limit // 4
    return sorted(set(range(prefix)) | {round(i * (n - 1) / (limit - prefix - 1)) for i in range(limit - prefix)})


def quantile(values, fraction):
    return sorted(values)[round((len(values) - 1) * fraction)] if values else 0


def feature_vector(values, adjacency=None):
    if not values:
        return None
    low, high = min(values), max(values)
    if high == low:
        return None
    adjacency = adjacency if adjacency is not None else list(range(1, len(values)))
    delta = [values[i] - values[i - 1] for i in adjacency]
    changed = [abs(d) / (high - low) for d in delta if d]
    counts = Counter(values)
    entropy = -sum((n / len(values)) * math.log2(n / len(values)) for n in counts.values())
    directions = [1 if d > 0 else -1 for d in delta if d]
    reversals = sum(a != b for a, b in zip(directions, directions[1:])) / max(1, len(directions) - 1)
    return [min(1, math.log2(len(counts)) / 8), len(changed) / max(1, len(delta)),
            quantile(changed, .5), quantile(changed, .95), entropy / max(1, math.log2(len(counts))), reversals]


class SeedModel:
    """Small supervised nearest-example model + encoding frequency prior.

    No synthetic examples are mixed into real training. Unexcited seed channels
    cannot teach dynamics. Leave-one-signal-out validation excludes ALL sessions
    of that signal; if its class has no training support it abstains.
    """
    def __init__(self, seeds, examples):
        self.seeds = seeds
        self.examples = examples

    @classmethod
    def fit(cls, recordings, definitions):
        seeds = [d for d in definitions if d.role == "seed"]
        examples = []
        for d in seeds:
            for recording in recordings:
                for key, stream in recording.streams.items():
                    if not d.matches(key):
                        continue
                    values = [d.decode(row[2]) for row in stream.rows]
                    adjacent = [j for j in range(1, len(values)) if stream.rows[j][1] == stream.rows[j - 1][1]]
                    features = feature_vector(values, adjacent)
                    if features is not None and len(values) >= 20:
                        examples.append({"signal": d.name, "recording": recording.name, "kind": d.kind, "features": features})
        return cls(seeds, examples)

    def predict(self, features, exclude=None):
        examples = [e for e in self.examples if e["signal"] != exclude]
        if features is None or not examples:
            return None
        nearest = sorted((math.dist(features, e["features"]), e["kind"], e["signal"]) for e in examples)[:3]
        votes = defaultdict(float)
        for distance, kind, _ in nearest:
            votes[kind] += 1 / (.15 + distance)
        label = max(votes, key=votes.get)
        return {"kind": label, "distance": nearest[0][0], "nearest_seed": nearest[0][2],
                "supported_classes": sorted({e["kind"] for e in examples})}

    def encoding_prior(self, f):
        widths = Counter(d.field.width for d in self.seeds)
        orders = Counter(d.field.order for d in self.seeds)
        if not self.seeds:
            return 0
        return .5 * (widths[f.width] / len(self.seeds) + orders[f.order] / len(self.seeds))

    def export(self):
        folds = []
        for e in self.examples:
            prediction = self.predict(e["features"], exclude=e["signal"])
            support = {x["kind"] for x in self.examples if x["signal"] != e["signal"]}
            if e["kind"] not in support:
                prediction = None
            folds.append({"signal": e["signal"], "recording": e["recording"], "expected": e["kind"],
                          "predicted": prediction["kind"] if prediction else None})
        scored = [x for x in folds if x["predicted"] is not None]
        return {"type": "nearest labeled shape examples + encoding prior", "features": ["log_unique", "change_fraction", "median_step_over_range", "p95_step_over_range", "normalized_entropy", "reversal_fraction"],
                "examples": self.examples, "seed_count": len(self.seeds),
                "width_counts": dict(Counter(d.field.width for d in self.seeds)),
                "dynamic_class_support": dict(Counter(e["kind"] for e in self.examples)),
                "validation": {"method": "leave one entire signal out across all recordings", "folds": folds,
                               "scored": len(scored), "abstained": len(folds) - len(scored),
                               "accuracy_when_scored": sum(x["expected"] == x["predicted"] for x in scored) / len(scored) if scored else None},
                "limitation": "Few excited seeds may leave classes unsupported. Shape similarity does not identify physical meaning, scale or units."}


def enumerate_fields(dlc, active, blocked=frozenset()):
    """Contiguous BE/LE 1..16 bits; byte-aligned 24/32-bit fields.

    A field must have active endpoints, or a conventional byte-aligned width.
    The latter retains full-byte interpretations when high bits stayed constant.
    """
    seen = set()
    for order in ("big", "little"):
        for width in (*range(1, 17), 24, 32):
            for start in range(dlc * 8 - width + 1):
                if width > 16 and start % 8:
                    continue
                f = Field(start, width, order)
                bits = f.physical_bits()
                if bits & blocked or not bits & active:
                    continue
                endpoints = Field(start, 1, order).physical_bits() | Field(start + width - 1, 1, order).physical_bits()
                conventional = start % 8 == 0 and width in (8, 16, 24, 32)
                if not conventional and not endpoints <= active:
                    continue
                # Byte-local BE and LE extraction are identical; retain BE once.
                signature = tuple(sorted(bits)) if width <= 8 and len({b // 8 for b in bits}) == 1 else (order, start, width)
                if signature in seen:
                    continue
                seen.add(signature)
                yield f


def active_bits(streams):
    payloads = [row[2] for stream in streams for row in stream.rows]
    if not payloads:
        return frozenset()
    baseline = int.from_bytes(payloads[0], "big")
    changes = reduce(int.__or__, (int.from_bytes(d, "big") ^ baseline for d in payloads), 0)
    nbits = len(payloads[0]) * 8
    return frozenset(i for i in range(nbits) if changes & (1 << (nbits - 1 - i)))


def counter_evidence(f, series):
    deltas = Counter()
    wrap_deltas = Counter()
    modulus = 1 << f.width
    for rows, values in series:
        for i in range(1, len(rows)):
            if rows[i][1] != rows[i - 1][1]:
                continue
            delta = (values[i] - values[i - 1]) % modulus
            deltas[delta] += 1
            if values[i] < values[i - 1]:
                wrap_deltas[delta] += 1
    if not deltas:
        return None
    step, count = deltas.most_common(1)[0]
    total = deltas.total()
    # Non-unit steps are common on this bus. Require repeated wraps, not a ramp.
    wraps = wrap_deltas[step]
    # A constant prefix around an 8-bit counter is NOT a wider counter: its
    # observed downward wraps must obey the proposed modulus as well.
    # An even increment leaves fixed low bits: the actual sequence field is
    # narrower. Do not let a padded counter consume neighboring sensor bits.
    if step and math.gcd(step, modulus) == 1 and count / total >= .95 and total >= 30 and wraps >= 2 and wraps / max(1, wrap_deltas.total()) >= .95 and f.width >= 2:
        return {"step": step, "modulus": modulus, "agreement": count / total, "transitions": total, "wraps": wraps}
    return None


def bins(rows, values, step=.1):
    grouped = defaultdict(list)
    for (t, segment, _), value in zip(rows, values):
        grouped[(segment, int(t / step + 1e-8))].append(value)
    return {key: statistics.median(v) for key, v in grouped.items()}


def association(candidate_bins, anchor_bins):
    common = sorted(candidate_bins.keys() & anchor_bins.keys())
    if len(common) < 20:
        return None
    x, y = [candidate_bins[k] for k in common], [anchor_bins[k] for k in common]
    r = corr(x, y)
    if r is None:
        return None
    dx, dy = [], []
    for i in range(1, len(common)):
        if common[i][0] == common[i - 1][0] and common[i][1] == common[i - 1][1] + 1:
            dx.append(x[i] - x[i - 1])
            dy.append(y[i] - y[i - 1])
    delta_r = corr(dx, dy)
    transitions = sum(a != b for a, b in zip(y, y[1:]))
    # Split-time consistency is evidence, not an independent statistical test.
    halves = [corr(x[:len(x)//2], y[:len(y)//2]), corr(x[len(x)//2:], y[len(y)//2:])]
    replicated = all(v is not None and abs(v) >= .7 and v * r > 0 for v in halves)
    return {"r": r, "delta_r": delta_r, "bins": len(common), "anchor_transitions": transitions,
            "split_half_r": halves, "split_half_consistent": replicated}


def shifted_control(candidate_bins, anchor_bins):
    """Circular shift diagnostic within segments; NOT a p-value or FDR claim."""
    observed = association(candidate_bins, anchor_bins)
    if observed is None:
        return None
    groups = defaultdict(list)
    for key in sorted(candidate_bins.keys() & anchor_bins.keys()):
        groups[key[0]].append(key)
    controls = []
    for fraction in (.15, .25, .35, .5, .65, .75, .85):
        shifted = {}
        for keys in groups.values():
            offset = max(1, round(len(keys) * fraction))
            for i, key in enumerate(keys):
                shifted[key] = anchor_bins[keys[(i + offset) % len(keys)]]
        control = association(candidate_bins, shifted)
        if control:
            controls.append(abs(control["r"]))
    return {"observed_abs_r": abs(observed["r"]), "max_shifted_abs_r": max(controls, default=0),
            "margin": abs(observed["r"]) - max(controls, default=0), "shifts": len(controls),
            "interpretation": "diagnostic only; shared trends/one event and multiple comparisons can still produce high correlation"}


def canonical_key(f):
    # Prefer a complete byte/word over arbitrary fragments when behavior is equal.
    conventional = f.start % 8 == 0 and f.width in (8, 16, 24, 32)
    return (0 if f.width == 1 else 1 if conventional else 2, f.width, f.order != "big", f.signed, f.start)


def affine_signature(values):
    """Exact integer-affine aliases share a signature; across ALL sessions."""
    if not values or min(values) == max(values):
        return None
    first = values[0]
    differences = [v - first for v in values]
    divisor = reduce(math.gcd, differences, 0)
    sign = next(v for v in differences if v) // abs(next(v for v in differences if v))
    normalized = (str(v // divisor * sign) for v in differences)
    return hashlib.sha256(",".join(normalized).encode()).digest()


@dataclass
class Candidate:
    key: tuple
    field: Field
    score: float
    kind: str
    features: list
    aliases: list = dc_field(default_factory=list)
    associations: list = dc_field(default_factory=list)
    counter: dict | None = None
    prediction: dict | None = None
    per_session: list = dc_field(default_factory=list)
    catalog_matches: list = dc_field(default_factory=list)
    caveats: list = dc_field(default_factory=list)
    id: str = ""

    def export(self):
        result = asdict(self)
        bus, ident, extended, dlc = self.key
        result["can_id_hex"] = f"{ident:08X}" if extended else f"{ident:03X}"
        result["location"] = self.field.describe()
        result["novelty"] = "published community definition; additional to seed list" if self.catalog_matches else "unverified field; meaning unknown"
        return result


def summarize_series(rows, values):
    transitions = []
    previous = None
    for (t, segment, _), v in zip(rows, values):
        if previous is None or v != previous:
            if len(transitions) < 30:
                transitions.append({"t": t, "raw": v, "segment": segment})
        previous = v
    pairs = [(values[i - 1], values[i]) for i in range(1, len(values)) if rows[i][1] == rows[i - 1][1]]
    return {"samples": len(values), "min": min(values), "max": max(values), "unique": len(set(values)),
            "transitions_within_segments": sum(a != b for a, b in pairs), "first_changes": transitions,
            "changes_truncated": len(transitions) >= 30}


class InferenceEngine:
    def __init__(self, recordings, definitions, bin_seconds=.1, progress=lambda message: None):
        self.recordings = recordings
        self.definitions = definitions
        self.bin_seconds = bin_seconds
        self.progress = progress
        self.model = SeedModel.fit(recordings, definitions)
        self.scanned = 0
        self.dynamic = 0
        self.anchors = {}
        for ri, recording in enumerate(recordings):
            anchors = []
            for d in definitions:
                if d.role != "seed":
                    continue
                for key, stream in recording.streams.items():
                    if d.matches(key):
                        values = [d.decode(row[2]) for row in stream.rows]
                        if len(set(values)) > 1:
                            anchors.append((d, key, bins(stream.rows, values, bin_seconds)))
            self.anchors[ri] = anchors

    def run(self, top=100):
        keys = sorted({key for r in self.recordings for key in r.streams})
        all_candidates = []
        for ki, key in enumerate(keys):
            if ki % 10 == 0:
                self.progress(f"Profiling message {ki + 1}/{len(keys)}; {self.scanned:,} field interpretations checked")
            streams = [(ri, r.streams[key]) for ri, r in enumerate(self.recordings) if key in r.streams]
            active = active_bits([s for _, s in streams])
            blocked = frozenset().union(*(d.field.physical_bits() for d in self.definitions if d.role == "seed" and d.matches(key)))
            profiles = [(ri, s, indices(len(s.rows))) for ri, s in streams]
            aliases = {}
            for f in enumerate_fields(key[3], active, blocked):
                # Exact alias tests use ALL frames, including short transients
                # that a uniform profiling sample might miss.
                values = [f.decode(row[2]) for _, s, _ in profiles for row in s.rows]
                variants = [(f, values)]
                if f.width >= 8 and any(v >= (1 << (f.width - 1)) for v in values) and any(v < (1 << (f.width - 1)) for v in values):
                    signed_values = [v - (1 << f.width) if v >= (1 << (f.width - 1)) else v for v in values]
                    variation = [0, 0]
                    offset = 0
                    for _, s, _ in profiles:
                        for i in range(1, len(s.rows)):
                            if s.rows[i][1] == s.rows[i - 1][1]:
                                variation[0] += abs(values[offset+i] - values[offset+i-1])
                                variation[1] += abs(signed_values[offset+i] - signed_values[offset+i-1])
                        offset += len(s.rows)
                    # Signedness must reduce actual jumps. Normalizing by a
                    # larger signed range could otherwise reward bad boundaries.
                    if variation[1] < .9 * variation[0]:
                        variants.append((Field(f.start, f.width, f.order, True), signed_values))
                for variant, vv in variants:
                    self.scanned += 1
                    signature = affine_signature(vv)
                    if signature is None:
                        continue
                    self.dynamic += 1
                    if signature in aliases:
                        group = aliases[signature]
                        group.append(variant)
                    else:
                        aliases[signature] = [variant]
            message_candidates = []
            for group in aliases.values():
                f = min(group, key=canonical_key)
                counter = None
                # An affine alias with constant padding can hide the real
                # counter width. Test the narrower extraction before choosing
                # the conventional byte/word representation.
                for cf in sorted((a for a in group if not a.signed), key=lambda a: (-a.width, canonical_key(a))):
                    cf_series = [(s.rows, [cf.decode(row[2]) for row in s.rows]) for _, s, _ in profiles]
                    evidence = counter_evidence(cf, cf_series)
                    if evidence:
                        f, counter = cf, evidence
                        break
                selected = []
                full = []
                adjacent = []
                offset = 0
                series_features = []
                for ri, s, ii in profiles:
                    values = [f.decode(row[2]) for row in s.rows]
                    aa = [j for j in range(1, len(values)) if s.rows[j][1] == s.rows[j - 1][1]]
                    ff = feature_vector(values, aa)
                    if ff:
                        series_features.append(ff)
                    full.append((s.rows, values))
                    selected.extend(values)
                    adjacent.extend(j + offset for j in aa)
                    offset += len(values)
                if not series_features:
                    # Changes only BETWEEN recordings carry no dynamic evidence.
                    continue
                features = [statistics.fmean(v) for v in zip(*series_features)]
                n_unique = len(set(selected))
                prediction = self.model.predict(features)
                change_rates = []
                for rows, values in full:
                    changed = sum(values[i] != values[i - 1] for i in range(1, len(rows)) if rows[i][1] == rows[i - 1][1])
                    span = sum(max(0, rows[i][0] - rows[i - 1][0]) for i in range(1, len(rows)) if rows[i][1] == rows[i - 1][1])
                    change_rates.append(changed / max(.1, span))
                # Every numeric LSB is binary. That alone does not make it a
                # control switch; fast bit activity receives no state bonus.
                kind = ("counter" if counter else "state" if n_unique <= 8 and max(change_rates, default=0) <= 2
                        else "bit_activity" if n_unique <= 8
                        else "continuous" if features[2] < .15 and features[3] < .5 else "high_entropy_or_mixed")
                trained_similarity = max(0, 1 - prediction["distance"]) if prediction and prediction["kind"] == kind else 0
                score = 10 + 5 * self.model.encoding_prior(f) + 5 * trained_similarity
                if kind == "state":
                    score += 20 + (4 if f.width == 1 else 0) - 4 * max(0, math.log2(n_unique) - 1)
                elif kind == "continuous":
                    score += 20 + 10 * max(0, 1 - features[2] * 5)
                elif kind == "counter":
                    score = 10 + 15 * counter["agreement"]
                else:
                    score -= 10
                associations = []
                if not counter:
                    for (ri, s, _), (_, values) in zip(profiles, full):
                        cb = bins(s.rows, values, self.bin_seconds)
                        for d, anchor_key, ab in self.anchors[ri]:
                            if anchor_key[0] != key[0] or anchor_key[:3] == key[:3]:
                                continue  # avoid same-message timing/bit leakage
                            evidence = association(cb, ab)
                            if evidence and abs(evidence["r"]) >= .65:
                                if d.kind == "state":
                                    anchor_stream = self.recordings[ri].streams[anchor_key]
                                    av = [d.decode(row[2]) for row in anchor_stream.rows]
                                    evidence["anchor_transitions"] = sum(av[j] != av[j-1] for j in range(1, len(av)) if anchor_stream.rows[j][1] == anchor_stream.rows[j-1][1])
                                associations.append({"recording": self.recordings[ri].name, "anchor": d.name, **evidence})
                    if associations:
                        strongest = max(associations, key=lambda a: abs(a["r"]))
                        score += 20 * abs(strongest["r"])
                        if strongest["delta_r"] is not None:
                            score += 10 * abs(strongest["delta_r"])
                            if abs(strongest["delta_r"]) > .9:
                                # Reward retained resolution when two overlapping
                                # fields both look correlated: a truncated high
                                # byte must not beat the exact packed signal.
                                score += min(8, max(0, -math.log10(1 - abs(strongest["r"]) + 1e-8) - 2) * 2)
                        if strongest["split_half_consistent"]:
                            score += 5
                    if not associations:
                        if f.width > 1 and (f.start % 8 or f.width % 8):
                            score -= 4  # weak packing prior; an anchor can override it
                        score -= max(0, f.width - 16) / 2
                sessions = []
                for (ri, s, _), (_, values) in zip(profiles, full):
                    sessions.append({"recording": self.recordings[ri].name, **summarize_series(s.rows, values)})
                variable_sessions = sum(x["unique"] > 1 for x in sessions)
                score += min(6, (variable_sessions - 1) * 3)
                candidate = Candidate(key, f, round(score, 3), kind, features,
                                      [asdict(a) for a in sorted(group, key=canonical_key) if a != f],
                                      associations, counter, prediction, sessions)
                if len(group) > 1:
                    candidate.caveats.append("Other bit widths/endianness have identical affine behavior in these recordings; boundaries/sign/scale remain ambiguous.")
                if any(x.metadata.get("stats", {}).get("capture_gaps", 0) for x in self.recordings):
                    candidate.caveats.append("Capture gaps and Bluetooth batching limit timing/correlation evidence.")
                message_candidates.append(candidate)
            # Packet counters outrank every signed/shifted/container alias of
            # those same bits. This is structural exclusion, not a physical name.
            counters = sorted((c for c in message_candidates if c.counter),
                              key=lambda c: (-c.field.width, canonical_key(c.field)))
            structural = []
            for c in counters:
                if not any(c.field.physical_bits() & x.field.physical_bits() for x in structural):
                    structural.append(c)
            counter_bits = frozenset().union(*(c.field.physical_bits() for c in structural))
            message_candidates = [c for c in message_candidates if not c.field.physical_bits() & counter_bits]
            # Reject broad containers of cleaner subfields (counter + status, etc.).
            message_candidates.sort(key=lambda c: (-c.score, canonical_key(c.field)))
            retained = []
            for candidate in message_candidates:
                bits = candidate.field.physical_bits()
                if any(bits & x.field.physical_bits() for x in retained):
                    continue
                retained.append(candidate)
            all_candidates.extend(retained + structural)
        all_candidates.sort(key=lambda c: (-c.score, c.key, canonical_key(c.field)))
        selected = all_candidates[:top]
        # Published validation definitions are consulted ONLY AFTER selection.
        self.annotate(selected)
        for i, c in enumerate(selected, 1):
            c.id = f"C{i:03}"
            for a in c.associations:
                ri = next(i for i, r in enumerate(self.recordings) if r.name == a["recording"])
                stream = self.recordings[ri].streams[c.key]
                cb = bins(stream.rows, [c.field.decode(row[2]) for row in stream.rows], self.bin_seconds)
                ab = next(v for d, k, v in self.anchors[ri] if d.name == a["anchor"])
                a["shift_control"] = shifted_control(cb, ab)
            if any(a["anchor_transitions"] < 4 for a in c.associations):
                c.caveats.append("Association uses fewer than four anchor transitions; one start sequence cannot identify a function.")
        return selected, {"message_streams": len(keys), "interpretations_scanned": self.scanned,
                          "dynamic_interpretations": self.dynamic, "retained_nonoverlapping_fields": len(all_candidates),
                          "reported_fields": len(selected)}

    def annotate(self, candidates):
        for c in candidates:
            fields = [c.field, *(Field(**f) for f in c.aliases)]
            for d in self.definitions:
                if d.role != "validation" or not d.matches(c.key):
                    continue
                if not any(f.physical_bits() == d.field.physical_bits() for f in fields):
                    continue
                ranges = []
                for r in self.recordings:
                    if c.key not in r.streams:
                        continue
                    values = [d.decode(row[2]) for row in r.streams[c.key].rows]
                    ranges.append({"recording": r.name, "min": min(values), "max": max(values), "unique": len(set(values))})
                c.catalog_matches.append({"name": d.name, "definition": asdict(d), "decoded_ranges": ranges,
                                          "status": "published hypothesis; calibration/function not independently verified"})


def catalog_inventory(recordings, definitions):
    """Report observed definitions even if constant; these are not discoveries."""
    result = []
    for d in definitions:
        sessions = []
        for r in recordings:
            for key, s in r.streams.items():
                if d.matches(key):
                    values = [d.decode(row[2]) for row in s.rows]
                    sessions.append({"recording": r.name, **summarize_series(s.rows, values)})
        result.append({"definition": asdict(d), "observed": bool(sessions),
                       "excited": any(s["unique"] > 1 for s in sessions), "sessions": sessions})
    return result


def coincident_events(recordings, candidates, definitions, window=.25):
    """Unlabeled state-change clusters; independent of the validation lookup.

    At least two different message IDs must change. Never compare across a gap.
    These are candidate shared events, not identified physical actions.
    """
    result = []
    for r in recordings:
        changes = []
        fields = [(c.id, c.key, c.field.decode) for c in candidates if c.kind == "state" and c.key in r.streams]
        for d in definitions:
            if d.role == "seed" and d.kind == "state":
                fields.extend(("seed:" + d.name, key, d.decode) for key in r.streams if d.matches(key))
        for label, key, decode in fields:
            rows = r.streams[key].rows
            previous = decode(rows[0][2])
            for before, row in zip(rows, rows[1:]):
                value = decode(row[2])
                if row[1] == before[1] and value != previous:
                    changes.append({"t": row[0], "segment": row[1], "field": label,
                                    "can_id": key[1], "bus": key[0], "extended": key[2],
                                    "before": previous, "after": value})
                previous = value
        clusters = []
        for change in sorted(changes, key=lambda x: (x["segment"], x["t"], x["field"])):
            if clusters and change["segment"] == clusters[-1][0]["segment"] and change["t"] - clusters[-1][0]["t"] <= window:
                clusters[-1].append(change)
            else:
                clusters.append([change])
        for cluster in clusters:
            distinct = len({(c["bus"], c["can_id"], c["extended"]) for c in cluster})
            if distinct >= 2:
                result.append({"recording": r.name, "start_t": cluster[0]["t"], "end_t": cluster[-1]["t"],
                               "distinct_messages": distinct, "changes": cluster,
                               "interpretation": "coincident state changes; physical action unknown"})
    return sorted(result, key=lambda x: (-x["distinct_messages"], -len(x["changes"]), x["recording"], x["start_t"]))
