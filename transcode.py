#!/usr/bin/env python3
"""Transcode a video library to a phone-sized, properly named copy.

Reads tools/library.json (produced by build_index.py) and writes

    <out>/<package>/<section>/<Title> [<id>].mp4

Measured on this corpus: 1080p H.264 sources average 1346 kbps; 720p HEVC at
these settings lands near 0.18 of the source bitrate, so the 105 GB package
comes out around 20 GB.

Resume is by output file, not by a state file. A run that is interrupted, or
re-run later, skips every output that already exists and passes a duration
check against its source. Encoding goes to a .part file that is only renamed
into place after that check passes, so a half-written file can never be
mistaken for a finished one.

Usage:
    python transcode.py                    # full run, NVENC, 2 workers
    python transcode.py --limit 3          # smoke test on 3 videos
    python transcode.py --encoder x265     # CPU encode, better bits-per-quality
    python transcode.py --dry-run          # print the plan, touch nothing
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import timedelta
from pathlib import Path

# Windows gives every child process its own console window, which means one
# flashing window per ffmpeg and ffprobe call when a GUI drives this. The flag
# does not exist off Windows, hence the getattr default.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Windows-reserved device names; a bare one of these cannot be a filename stem.
RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Characters Windows forbids in a filename, mapped to the closest readable
# stand-in rather than a single blanket replacement. A colon is nearly always a
# title/subtitle separator, a slash nearly always joins two related terms, and
# the rest carry no meaning worth preserving.
ILLEGAL_MAP = {
    ":": " -",
    "/": "-",
    "\\": "-",
    "|": "-",
    "?": "",
    "*": "",
    "<": "",
    ">": "",
    '"': "",
}
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Curly punctuation and dashes that survive fine in metadata but make filenames
# inconsistent across Windows, exFAT and Android. Normalised on the way out.
PUNCT_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2013": "-", "\u2014": " - ", "\u2015": "-", "\u2212": "-",
    "\u2026": "...", "\u00a0": " ",
}

ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,9}-\d+$")
MAX_STEM = 120

# Predicting output size as a fixed fraction of input size is wrong in both
# directions, badly. Measured on this encoder: talks at 1346 kbps came out at
# 18% of source, screen recordings at 2379-8129 kbps at 6%, and already-shrunk
# 720p HEVC at ~200 kbps at 97%. Output bitrate is set by the target quality and
# the content, not by how large the input happened to be.
#
# Anchor: 720p HEVC at cq 34 measured about 200 kbps of video across 306 talks.
# Rate roughly halves for every 6 steps of cq.
ANCHOR_CQ = 34
ANCHOR_VIDEO_KBPS = 200.0


def predicted_kbps(cq: int, audio_kbps: int) -> float:
    """Expected output bitrate, in kbps, for a given quality setting."""
    return ANCHOR_VIDEO_KBPS * (2 ** ((ANCHOR_CQ - cq) / 6.0)) + audio_kbps


def sanitize(name: str) -> str:
    """Make a title safe as a filename stem on Windows, exFAT and Android."""
    for src, dst in PUNCT_MAP.items():
        name = name.replace(src, dst)
    name = unicodedata.normalize("NFC", name)
    name = CONTROL_RE.sub("", name)
    for src, dst in ILLEGAL_MAP.items():
        name = name.replace(src, dst)
    # Tidy separator hyphens only. A hyphen inside a word ("DHCP-Assisted",
    # "Open-Source") is part of the title and must survive untouched.
    name = re.sub(r"-{2,}", " - ", name)
    name = re.sub(r"\s+-\s+", " - ", name)
    name = re.sub(r"\s+", " ", name).strip(" -")
    if len(name) > MAX_STEM:
        name = name[:MAX_STEM].rstrip(" -")
    # Trailing dots and spaces are silently dropped by Windows; strip them first.
    name = name.rstrip(". ")
    if name.upper() in RESERVED:
        name = f"_{name}"
    return name or "untitled"


def out_path(record: dict, out_root: Path) -> Path:
    stem = sanitize(record["title"])
    # Only tack the package id on when it looks like a catalogue code and adds
    # information; the one unlisted file is already named for its talk.
    if ID_RE.match(record["id"]):
        stem = f"{stem} [{record['id']}]"
        if len(stem) > MAX_STEM + 16:
            stem = f"{sanitize(record['title'])[:MAX_STEM - 20]} [{record['id']}]"
    # An empty package or section means no grouping at that level, so it must
    # not become a directory. sanitize() would turn "" into "untitled".
    parts = [sanitize(p) for p in (record["package"], record["section"]) if p]
    return out_root.joinpath(*parts, f"{stem}.mp4")


# Verification verdicts. These three must never collapse into two.
#   GOOD    the file was measured and is intact
#   BAD     the file was measured and is definitely wrong
#   UNKNOWN the file could not be measured; nothing is known about it
# Only BAD may cause an existing output to be deleted. Treating UNKNOWN as BAD
# destroys finished work on no evidence: an output that merely could not be
# measured gets deleted and re-encoded. Every deletion is logged with its
# reason, because a silent one leaves nothing to diagnose afterwards.
GOOD, BAD, UNKNOWN = "good", "bad", "unknown"

PROBE_RETRIES = 3
PROBE_BACKOFF = 2.0



def _run_probe(cmd: list[str], timeout: float) -> tuple[bool, str]:
    """Return (measured, stdout). measured is False when ffprobe could not run."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout, errors="replace",
                             creationflags=NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return False, ""
    if res.returncode != 0:
        return False, ""
    return True, res.stdout


def probe_duration(path: Path) -> tuple[bool, float | None]:
    """Container duration. Returns (measured, seconds).

    measured=False means ffprobe failed or timed out, which says nothing about
    the file. A readable file with an unparseable duration is measured=True,
    value None, because that IS a real defect in the output.
    """
    measured, out = _run_probe(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        timeout=120,
    )
    if not measured:
        return False, None
    try:
        return True, float(out.strip())
    except ValueError:
        return True, None


def probe_video_stream(path: Path) -> tuple[bool, float | None]:
    """Duration of the video stream itself, from the moov. (measured, seconds).

    Distinct from the container duration, which reports the LONGEST stream. A
    short video track beside full-length audio shows up here and nowhere else.
    """
    measured, out = _run_probe(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        timeout=120,
    )
    if not measured:
        return False, None
    try:
        return True, float(out.strip().split(",")[0])
    except (ValueError, IndexError):
        return True, None


def decode_verify(path: Path) -> tuple[str, str]:
    """Decode every video frame and report whether the decoder complained.

    This is the authoritative check and the only one that has never been wrong
    on this corpus. Roughly 50x realtime, so about 30s for a 25 minute talk.

    Read the STDERR, not the exit code. ffmpeg returns 0 on a truncated file
    while printing "partial file" and "Invalid NAL unit size" to stderr;
    trusting rc alone passes corrupt files silently.
    """
    try:
        res = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
             "-map", "0:v:0", "-f", "null", "-"],
            capture_output=True, text=True, errors="replace", timeout=3600,
            creationflags=NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return UNKNOWN, "decode could not be run (timeout or error)"
    noise = res.stderr.strip()
    if res.returncode != 0:
        return BAD, f"decode failed (exit {res.returncode}): {noise[:160]}"
    if noise:
        return BAD, f"decoder reported errors: {noise.splitlines()[0][:160]}"
    return GOOD, ""


def verify_output(path: Path, expected: float,
                  thorough: bool = False) -> tuple[str, str]:
    """Return (verdict, reason): GOOD, BAD or UNKNOWN.

    Inline (thorough=False) reads only container and video-stream durations.
    Both are instant, both have been reliable on every file in this corpus, and
    together they catch the failure that actually matters here: an encode that
    stopped early. A .part is renamed into place only after ffmpeg exits 0, so
    that is the whole inline risk surface.

    thorough=True adds a full decode. Reserve it for --verify.

    UNKNOWN means the file could not be measured and must never be treated as
    BAD: only BAD may delete an existing output.
    """
    for attempt in range(PROBE_RETRIES):
        if attempt:
            time.sleep(PROBE_BACKOFF * attempt)

        measured, duration = probe_duration(path)
        if not measured:
            continue
        if duration is None:
            return BAD, "output has no parseable duration"
        if abs(expected - duration) > max(2.0, expected * 0.005):
            return BAD, (f"container duration {duration:.1f}s, "
                         f"source {expected:.1f}s")

        measured, vdur = probe_video_stream(path)
        if not measured:
            continue
        if vdur is None:
            return BAD, "output has no readable video stream"
        if abs(expected - vdur) > max(2.0, expected * 0.005):
            return BAD, (f"video stream {vdur:.1f}s, source {expected:.1f}s "
                         f"(container claims {duration:.1f}s)")

        if not thorough:
            return GOOD, ""
        return decode_verify(path)

    return UNKNOWN, f"ffprobe could not read the file after {PROBE_RETRIES} attempts"


def build_cmd(record: dict, dest: Path, args: argparse.Namespace) -> list[str]:
    if args.encoder == "nvenc":
        video = [
            "-c:v", "hevc_nvenc", "-preset", args.nvenc_preset,
            "-rc", "vbr", "-cq", str(args.cq), "-b:v", "0",
        ]
    else:
        video = ["-c:v", "libx265", "-crf", str(args.cq), "-preset", args.x265_preset]

    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", record["source"],
        "-map", "0:v:0", "-map", "0:a:0?",
        # Never upscale, and force an even height: sources shorter than the
        # target keep their own height, which HEVC still requires to be even.
        # Every source here is 968px or taller so the min() always picks the
        # target, but an odd-height input would otherwise fail the encode.
        "-vf", f"scale=-2:trunc(min({args.height}\\,ih)/2)*2",
        *video,
        "-tag:v", "hvc1",
        "-c:a", "aac", "-b:a", f"{args.audio_kbps}k",
        "-ac", str(args.audio_channels),
        "-movflags", "+faststart",
        "-metadata", f"title={record['title']}",
        "-metadata", f"artist={record['speakers']}",
        "-metadata", f"album={args.collection} {record['section']}".strip(),
        str(dest),
    ]


class Counters:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = 0
        self.skipped = 0
        self.failed = 0
        # Outputs that exist but could not be measured this run. Not failures
        # and not successes: nothing is known about them, so they are neither
        # deleted nor trusted.
        self.unverified = 0
        self.out_bytes = 0
        self.src_seconds_done = 0.0
        # Source seconds actually run through the encoder. Skipped files are
        # credited to src_seconds_done instantly, so including them in the rate
        # makes the early ETA read far too low: three instant skips at startup
        # put the apparent rate near 20x against a true 11x.
        self.encoded_seconds = 0.0

    def add(self, *, done=0, skipped=0, failed=0, unverified=0, out_bytes=0,
            src_seconds=0.0, encoded_seconds=0.0) -> None:
        with self.lock:
            self.done += done
            self.skipped += skipped
            self.failed += failed
            self.unverified += unverified
            self.out_bytes += out_bytes
            self.src_seconds_done += src_seconds
            self.encoded_seconds += encoded_seconds


SESSION_LIMIT_MARKERS = (
    "OpenEncodeSessionEx failed",
    "No capable devices found",
    "out of memory",
)


def transcode_one(record: dict, args: argparse.Namespace, counters: Counters,
                  total: int, started: float, log_lock: threading.Lock,
                  log_fh) -> tuple[str, dict, str]:
    """Return (status, record, detail). status is done | skipped | failed."""
    dest = out_path(record, Path(args.out))
    expected = record["duration_s"]

    if dest.exists():
        verdict, reason = verify_output(dest, expected)
        if verdict == GOOD:
            counters.add(skipped=1, out_bytes=dest.stat().st_size, src_seconds=expected)
            return "skipped", record, str(dest)
        if verdict == UNKNOWN:
            # Could not measure it. That is not evidence the file is bad, and
            # deleting it would destroy finished work for no reason. Leave it
            # alone and surface it instead.
            counters.add(unverified=1, out_bytes=dest.stat().st_size,
                         src_seconds=expected)
            with log_lock:
                log_fh.write("UNVERIFIED\t" + record["id"] + "\t" + repr(reason) + "\t" + str(dest) + "\n")
                log_fh.flush()
            return "unverified", record, reason
        # verdict is BAD: measured, and definitely wrong. Replacing it is
        # correct, but never silently.
        with log_lock:
            print(f"  REPLACING {record['id']}: {reason}", file=sys.stderr, flush=True)
            log_fh.write("REPLACE\t" + record["id"] + "\t" + repr(reason) + "\t" + str(dest) + "\n")
            log_fh.flush()
        dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part.mp4")
    part.unlink(missing_ok=True)

    cmd = build_cmd(record, part, args)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace", creationflags=NO_WINDOW)
    stderr_tail = "\n".join(proc.stderr.strip().splitlines()[-6:])

    if proc.returncode != 0:
        part.unlink(missing_ok=True)
        counters.add(failed=1, src_seconds=expected)
        return "failed", record, stderr_tail or f"ffmpeg exit {proc.returncode}"

    verdict, reason = verify_output(part, expected)
    if verdict == BAD:
        part.unlink(missing_ok=True)
        counters.add(failed=1, src_seconds=expected)
        return "failed", record, reason
    if verdict == UNKNOWN:
        # The encode reported success but the check could not run. Keep the
        # .part rather than discarding a possibly-good encode; the next run
        # deletes it and redoes the file, and the evidence survives until then.
        counters.add(failed=1, src_seconds=expected)
        return "failed", record, f"{reason} (kept {part.name} for inspection)"

    part.replace(dest)
    size = dest.stat().st_size
    counters.add(done=1, out_bytes=size, src_seconds=expected,
                 encoded_seconds=expected)

    with log_lock:
        n = counters.done + counters.skipped + counters.failed
        elapsed = time.time() - started
        # Rate from encoded work only; skips would inflate it.
        rate = counters.encoded_seconds / elapsed if elapsed else 0
        remaining = args.total_seconds - counters.src_seconds_done
        eta = timedelta(seconds=int(remaining / rate)) if rate > 0 else "?"
        out_kbps = size * 8 / expected / 1000 if expected else 0
        print(
            f"[{n}/{total}] {record['id']}  {out_kbps:>4.0f} kbps  "
            f"{size / 1e6:>6.1f} MB  ({record['src_kbps']} kbps src)  "
            f"ETA {eta}  {dest.name}",
            flush=True,
        )
        log_fh.write(f"OK\t{record['id']}\t{size}\t{out_kbps:.0f}\t{dest}\n")
        log_fh.flush()

    return "done", record, str(dest)


def verify_library(records: list[dict], out_root: Path, workers: int = 4) -> int:
    """Thoroughly check every expected output. Reports; deletes nothing.

    Single threaded and run with nothing else touching the disk, which is the
    only condition under which the tail check has proven reliable.
    """
    good = bad = unknown = missing = 0
    problems: list[tuple[str, str, str]] = []
    lock = threading.Lock()
    started = time.time()

    present = [(r, out_path(r, out_root)) for r in records]
    missing = sum(1 for _, d in present if not d.is_file())
    present = [(r, d) for r, d in present if d.is_file()]

    print(f"verifying {len(present)} outputs under {out_root}")
    print("(container duration, video stream duration, then a full decode)")
    print(f"{workers} parallel decoders, roughly 50x realtime each\n")

    def check(item):
        record, dest = item
        return record, verify_output(dest, record["duration_s"], thorough=True)

    done_n = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for record, (verdict, reason) in pool.map(check, present):
            with lock:
                done_n += 1
                if verdict == GOOD:
                    good += 1
                elif verdict == BAD:
                    bad += 1
                    problems.append((record["id"], "BAD", reason))
                    print(f"  BAD      {record['id']}: {reason}", flush=True)
                else:
                    unknown += 1
                    problems.append((record["id"], "UNKNOWN", reason))
                    print(f"  UNKNOWN  {record['id']}: {reason}", flush=True)
                if done_n % 25 == 0:
                    el = time.time() - started
                    rate = done_n / el if el else 0
                    left = (len(present) - done_n) / rate if rate else 0
                    print(f"  ... {done_n}/{len(present)} checked, "
                          f"{timedelta(seconds=int(left))} left", flush=True)

    print(f"\n{'-' * 60}")
    print(f"good {good}, bad {bad}, unverifiable {unknown}, missing {missing}")
    if problems:
        print("\nNothing was deleted. To redo a specific file, delete it and re-run"
              " the transcode.")
        return 1
    if missing:
        print("\nRun the transcode to produce the missing files.")
        return 1
    print("\nEvery expected output is present and intact.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", default="library.json")
    ap.add_argument("--out", default="output")
    ap.add_argument("--encoder", choices=["nvenc", "x265"], default="nvenc")
    ap.add_argument("--cq", type=int, default=34,
                    help="NVENC cq or x265 crf. Lower is bigger and better. Default 34.")
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--collection", default="",
                    help="collection name written into each file's album tag")
    # Defaults suit speech: 64 kbps mono is transparent for a lecture and
    # halves the audio budget. Music needs stereo and far more bitrate, so a
    # recorded performance wants roughly --audio-kbps 160 --audio-channels 2.
    ap.add_argument("--audio-kbps", type=int, default=64)
    ap.add_argument("--audio-channels", type=int, default=1,
                    help="1 for speech (default), 2 for music")
    ap.add_argument("--nvenc-preset", default="p6")
    ap.add_argument("--x265-preset", default="faster")
    ap.add_argument("--workers", type=int, default=0,
                    help="concurrent ffmpeg jobs. Default 2 for nvenc, 1 for x265.")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N videos")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-workers", type=int, default=4,
                    help="parallel decoders for --verify (default 4)")
    ap.add_argument("--verify", action="store_true",
                    help="thoroughly verify existing outputs and exit; encodes "
                         "nothing and deletes nothing. Run it when the machine "
                         "is otherwise idle.")
    args = ap.parse_args()

    if args.workers == 0:
        args.workers = 2 if args.encoder == "nvenc" else 1

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("ERROR: ffmpeg and ffprobe must be on PATH", file=sys.stderr)
        return 1

    lib_path = Path(args.library)
    if not lib_path.is_file():
        print(f"ERROR: library not found: {lib_path}. Run build_index.py first.", file=sys.stderr)
        return 1
    records = json.loads(lib_path.read_text(encoding="utf-8"))

    out_root = Path(args.out).resolve()
    # Bound the blast radius: never write to a drive root, never to the package
    # itself, and never anywhere a source file lives.
    if out_root.parent == out_root:
        print(f"ERROR: refusing to use a drive root as output: {out_root}", file=sys.stderr)
        return 1
    for record in records:
        src = Path(record["source"]).resolve()
        if out_root == src.parent or out_root in src.parents:
            print(f"ERROR: output dir {out_root} contains source files", file=sys.stderr)
            return 1

    missing = [r["id"] for r in records if not Path(r["source"]).is_file()]
    if missing:
        print(f"ERROR: {len(missing)} source files missing, first: {missing[0]}", file=sys.stderr)
        return 1

    if args.limit:
        records = records[: args.limit]

    if args.verify:
        return verify_library(records, out_root, workers=args.verify_workers)
    args.total_seconds = sum(r["duration_s"] for r in records)

    # Collisions would silently overwrite one talk with another.
    dests: dict[Path, str] = {}
    for record in records:
        dest = out_path(record, out_root)
        if dest in dests:
            print(f"ERROR: filename collision between {dests[dest]} and {record['id']}: {dest}",
                  file=sys.stderr)
            return 1
        dests[dest] = record["id"]

    print(f"source:  {len(records)} videos, {args.total_seconds / 3600:.1f} hours, "
          f"{sum(r['size_bytes'] for r in records) / 1e9:.1f} GB")
    print(f"output:  {out_root}")
    print(f"encoder: {args.encoder} @ {args.height}p, "
          f"{'cq' if args.encoder == 'nvenc' else 'crf'} {args.cq}, "
          f"{args.workers} worker(s)")

    # Re-encoding video that is already at or below the target bitrate costs
    # hours and a generation of quality to save almost nothing. Pointing the
    # tool at its own output is an easy mistake; say so rather than grinding
    # through it silently.
    target = predicted_kbps(args.cq, args.audio_kbps)
    already = [r for r in records if r["src_kbps"] <= target * 1.15]
    if already:
        share = len(already) / len(records)
        print(f"\nNOTE: {len(already)} of {len(records)} sources are already at or "
              f"below the target of about {target:.0f} kbps.")
        if share > 0.5:
            print("      Most of this library is already compressed. Re-encoding it "
                  "will save little\n      and lose quality. Check you are not "
                  "pointing at output from a previous run.")
    print()

    if args.dry_run:
        for record in records[:20]:
            print(f"  {record['id']} -> {out_path(record, out_root).relative_to(out_root)}")
        if len(records) > 20:
            print(f"  ... and {len(records) - 20} more")
        return 0

    out_root.mkdir(parents=True, exist_ok=True)
    counters = Counters()
    started = time.time()
    log_lock = threading.Lock()
    failures: list[tuple[dict, str]] = []
    unverified: list[tuple[dict, str]] = []

    with (out_root / "transcode.log").open("a", encoding="utf-8") as log_fh:
        log_fh.write(f"\n# run {time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"encoder={args.encoder} cq={args.cq} height={args.height}\n")
        log_fh.flush()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [
                pool.submit(transcode_one, r, args, counters, len(records),
                            started, log_lock, log_fh)
                for r in records
            ]
            for fut in concurrent.futures.as_completed(futures):
                status, record, detail = fut.result()
                if status == "unverified":
                    unverified.append((record, detail))
                if status == "failed":
                    failures.append((record, detail))
                    with log_lock:
                        print(f"  FAILED {record['id']}: {detail}", file=sys.stderr, flush=True)
                        log_fh.write(f"FAIL\t{record['id']}\t{detail!r}\n")
                        log_fh.flush()

        # A GPU session-limit failure is a scheduling problem, not a bad file.
        # Retry those one at a time before calling anything broken.
        retry = [(r, d) for r, d in failures if any(m in d for m in SESSION_LIMIT_MARKERS)]
        if retry and args.workers > 1:
            print(f"\nretrying {len(retry)} encoder-session failures serially ...", flush=True)
            still_failed = [(r, d) for r, d in failures if (r, d) not in retry]
            for record, _ in retry:
                status, record, detail = transcode_one(
                    record, args, counters, len(records), started, log_lock, log_fh
                )
                if status == "failed":
                    still_failed.append((record, detail))
                else:
                    counters.add(failed=-1)
            failures = still_failed

    elapsed = time.time() - started
    src_gb = sum(r["size_bytes"] for r in records) / 1e9
    print(f"\n{'-' * 60}")
    print(f"encoded {counters.done}, skipped {counters.skipped}, "
          f"failed {counters.failed}, unverified {counters.unverified}")
    print(f"output  {counters.out_bytes / 1e9:.1f} GB from {src_gb:.1f} GB source "
          f"({counters.out_bytes / (src_gb * 1e9) * 100:.0f}% of original)")
    print(f"elapsed {timedelta(seconds=int(elapsed))}")
    if elapsed > 0:
        print(f"speed   {counters.encoded_seconds / elapsed:.1f}x realtime "
              f"({counters.encoded_seconds / 3600:.1f}h of video encoded)")

    if unverified:
        print(f"\n{len(unverified)} file(s) could not be verified this run. They were"
              f" LEFT IN PLACE, not deleted:", file=sys.stderr)
        for record, detail in unverified:
            print(f"  {record['id']} ({record['title'][:60]}): {detail}", file=sys.stderr)
        print("Re-run to check them again when the disk is less busy.", file=sys.stderr)

    if failures:
        print(f"\n{len(failures)} FAILURE(S):", file=sys.stderr)
        for record, detail in failures:
            print(f"  {record['id']} ({record['title'][:60]}): {detail}", file=sys.stderr)
        print("\nRe-run the same command to retry only these; finished files are skipped.",
              file=sys.stderr)
        return 1

    if unverified:
        return 2

    print("\nAll files transcoded and length-verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
