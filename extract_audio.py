#!/usr/bin/env python3
"""Extract audio-only versions of the transcoded library.

The transcoded mp4s already carry a 64 kbps mono AAC track, so this is a
STREAM COPY, not a re-encode: no quality loss and no GPU, a few minutes for the
whole library rather than hours. 173.5 hours of speech comes to roughly 5 GB.

    <out>/<package>/<section>/<Title> [<id>].m4a

Naming, foldering and metadata match the video library exactly, so the same
talk is findable in both. Resume works the same way too: an output that already
exists and passes a duration check is skipped.

Usage:
    python extract_audio.py                 # all of it
    python extract_audio.py --limit 3       # smoke test
    python extract_audio.py --dry-run
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcode import (  # noqa: E402
    BAD, GOOD, PROBE_BACKOFF, PROBE_RETRIES, UNKNOWN,
    _run_probe, out_path, probe_duration,
)

# Windows gives every child process its own console window, which means one
# flashing window per ffmpeg and ffprobe call when a GUI drives this. The flag
# does not exist off Windows, hence the getattr default.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def probe_audio_stream(path: Path) -> tuple[bool, float | None]:
    """Duration of the audio stream itself. Returns (measured, seconds)."""
    measured, out = _run_probe(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        timeout=120,
    )
    if not measured:
        return False, None
    try:
        return True, float(out.strip().split(",")[0])
    except (ValueError, IndexError):
        return True, None


def verify_audio(path: Path, expected: float) -> tuple[str, str]:
    """Audio-only counterpart to transcode.verify_output.

    transcode's version requires a video stream and so rejects every one of
    these files. Same three-verdict contract: only BAD may delete an output,
    UNKNOWN means the file could not be measured and is left alone.
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
            return BAD, f"container duration {duration:.1f}s, source {expected:.1f}s"

        measured, adur = probe_audio_stream(path)
        if not measured:
            continue
        if adur is None:
            return BAD, "output has no readable audio stream"
        if abs(expected - adur) > max(2.0, expected * 0.005):
            return BAD, f"audio stream {adur:.1f}s, source {expected:.1f}s"
        return GOOD, ""

    return UNKNOWN, f"ffprobe could not read the file after {PROBE_RETRIES} attempts"


def audio_path(record: dict, video_root: Path, audio_root: Path) -> Path:
    """Mirror the video library's layout, swapping the extension."""
    rel = out_path(record, video_root).relative_to(video_root)
    return audio_root / rel.with_suffix(".m4a")


def extract_one(record: dict, video_root: Path, audio_root: Path,
                counters: dict, lock: threading.Lock, total: int,
                started: float) -> tuple[str, dict, str]:
    src = out_path(record, video_root)
    dest = audio_path(record, video_root, audio_root)
    expected = record["duration_s"]

    if not src.is_file():
        with lock:
            counters["failed"] += 1
        return "failed", record, "source video not found; transcode it first"

    if dest.exists():
        verdict, _ = verify_audio(dest, expected)
        if verdict == GOOD:
            with lock:
                counters["skipped"] += 1
                counters["bytes"] += dest.stat().st_size
            return "skipped", record, str(dest)
        dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part.m4a")
    part.unlink(missing_ok=True)

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(src),
        "-map", "0:a:0",
        "-c:a", "copy",          # stream copy: the AAC track is already what we want
        "-vn",
        "-movflags", "+faststart",
        "-map_metadata", "0",
        str(part),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace", creationflags=NO_WINDOW)
    if proc.returncode != 0:
        part.unlink(missing_ok=True)
        with lock:
            counters["failed"] += 1
        tail = "\n".join(proc.stderr.strip().splitlines()[-3:])
        return "failed", record, tail or f"ffmpeg exit {proc.returncode}"

    verdict, reason = verify_audio(part, expected)
    if verdict != GOOD:
        part.unlink(missing_ok=True)
        with lock:
            counters["failed"] += 1
        return "failed", record, reason

    part.replace(dest)
    size = dest.stat().st_size
    with lock:
        counters["done"] += 1
        counters["bytes"] += size
        n = counters["done"] + counters["skipped"] + counters["failed"]
        el = time.time() - started
        eta = timedelta(seconds=int((total - n) * el / n)) if n else "?"
        print(f"[{n}/{total}] {record['id']}  {size / 1e6:6.1f} MB  "
              f"ETA {eta}  {dest.name}", flush=True)
    return "done", record, str(dest)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", default="library.json")
    ap.add_argument("--video", default="output")
    ap.add_argument("--out", default="audio")
    ap.add_argument("--workers", type=int, default=4,
                    help="stream copy is I/O bound, not CPU bound (default 4)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    records = json.loads(Path(args.library).read_text(encoding="utf-8"))
    video_root = Path(args.video).resolve()
    audio_root = Path(args.out).resolve()

    if audio_root.parent == audio_root:
        print(f"ERROR: refusing a drive root as output: {audio_root}", file=sys.stderr)
        return 1
    if audio_root == video_root:
        print("ERROR: audio output must differ from the video library", file=sys.stderr)
        return 1

    if args.limit:
        records = records[: args.limit]

    print(f"source: {video_root}")
    print(f"output: {audio_root}")
    print(f"{len(records)} talks, stream copy of the existing AAC track, "
          f"{args.workers} workers\n")

    if args.dry_run:
        for r in records[:10]:
            print(f"  {r['id']} -> {audio_path(r, video_root, audio_root).relative_to(audio_root)}")
        if len(records) > 10:
            print(f"  ... and {len(records) - 10} more")
        return 0

    audio_root.mkdir(parents=True, exist_ok=True)
    counters = {"done": 0, "skipped": 0, "failed": 0, "bytes": 0}
    lock = threading.Lock()
    started = time.time()
    failures = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(extract_one, r, video_root, audio_root,
                            counters, lock, len(records), started)
                for r in records]
        for fut in concurrent.futures.as_completed(futs):
            status, record, detail = fut.result()
            if status == "failed":
                failures.append((record, detail))
                print(f"  FAILED {record['id']}: {detail}", file=sys.stderr, flush=True)

    el = time.time() - started
    print(f"\n{'-' * 60}")
    print(f"extracted {counters['done']}, skipped {counters['skipped']}, "
          f"failed {counters['failed']}")
    print(f"total {counters['bytes'] / 1e9:.2f} GB in {timedelta(seconds=int(el))}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):", file=sys.stderr)
        for record, detail in failures:
            print(f"  {record['id']}: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
