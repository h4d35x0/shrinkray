#!/usr/bin/env python3
"""Build library.json, the input every other script in this toolkit reads.

Two modes.

INDEXED (default). Conference recording packages usually ship a "Start Here"
HTML page per section that maps opaque filenames to real titles and speakers.
Any *.html one directory below the root that links to `movies/...` is treated
as such an index; nothing is hardcoded to a particular conference or year.

    <root>/<Section>/Whatever Start Here.html
    <root>/<Section>/movies/<id>.mp4

SCAN (--scan). No index needed. Every video under the root becomes a record,
titled from its filename and grouped by its parent directory. Use this for any
folder of video: a band's recorded sets, lecture captures, camera footage.

Either way it writes:
    <out>/library.json   one record per source video
    <out>/library.csv    the same, for eyeballing in a spreadsheet

In indexed mode, videos the HTML does not mention are still emitted, with
section "Unlisted" and their filename stem as the title, so nothing is silently
dropped.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import subprocess
import sys
from pathlib import Path

# Windows gives every child process its own console window, which means one
# flashing window per ffmpeg and ffprobe call when a GUI drives this. The flag
# does not exist off Windows, hence the getattr default.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm", ".ts", ".mpg", ".mpeg"}

# <h2 id="Track 1">Track 1</h2> marks the start of a section's listing.
SECTION_RE = re.compile(r'<h2 id="([^"]+)">')

# <h2><a href='movies/<id>.mp4' ... >Title</a></h2>
#   <p class="filename">...</p>
#   <p><strong>Speaker(s):</strong> names</p>
ENTRY_RE = re.compile(
    r"<h2><a href='movies/(?P<vid>[^'/]+)\.mp4'[^>]*>(?P<title>.*?)</a></h2>"
    r"(?P<tail>.*?)(?=<h2|\Z)",
    re.S,
)
SPEAKER_RE = re.compile(r"Speaker\(s\):</strong>\s*(?P<sp>.*?)</p>", re.S)

TAG_RE = re.compile(r"<[^>]+>")


def clean(fragment: str) -> str:
    """HTML fragment to plain text: strip tags, unescape entities, squeeze spaces."""
    text = html.unescape(TAG_RE.sub("", fragment))
    return re.sub(r"\s+", " ", text).strip()


def discover_indexes(root: Path) -> list[Path]:
    """Every HTML file one level down that actually links to videos.

    Matching on content rather than filename keeps this working across
    packages that name their index page differently.
    """
    found = []
    for path in sorted(root.glob("*/*.html")):
        try:
            if "movies/" in path.read_text(encoding="utf-8", errors="replace"):
                found.append(path)
        except OSError:
            continue
    return found


def parse_index(path: Path) -> dict[str, dict[str, str]]:
    """Return {video_id: {"title", "speakers", "section"}} for one HTML index.

    The file opens with a table of contents whose anchors are '#nn' rather than
    'movies/<id>.mp4', so ENTRY_RE cannot match inside it. Sections are sliced
    off the first real section heading onward.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")

    starts = [(m.start(), m.group(1)) for m in SECTION_RE.finditer(raw)]
    if not starts:
        return {}

    spans = []
    for i, (pos, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(raw)
        spans.append((name, raw[pos:end]))

    out: dict[str, dict[str, str]] = {}
    for section, body in spans:
        for m in ENTRY_RE.finditer(body):
            vid = m.group("vid")
            sp = SPEAKER_RE.search(m.group("tail"))
            record = {
                "title": clean(m.group("title")),
                "speakers": clean(sp.group("sp")) if sp else "",
                "section": section,
            }
            if vid in out and out[vid] != record:
                print(f"  warning: {vid} listed twice with different metadata",
                      file=sys.stderr)
            out[vid] = record
    return out


def probe(path: Path) -> tuple[float, int, int, int]:
    """Return (duration_seconds, size_bytes, width, height) for a video."""
    res = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration,size",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, check=True,
        creationflags=NO_WINDOW,
    )
    vals = [v for v in res.stdout.split() if v]
    width, height, duration, size = vals[0], vals[1], vals[2], vals[3]
    return float(duration), int(size), int(width), int(height)


def find_videos(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True,
                    help="directory holding the videos")
    ap.add_argument("--out", default=".",
                    help="where to write library.json and library.csv")
    ap.add_argument("--scan", action="store_true",
                    help="ignore any HTML index; title from filename, section "
                         "from parent directory")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    out_dir = Path(args.out).resolve()
    if not root.is_dir():
        print(f"ERROR: root not found: {root}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    meta: dict[str, dict[str, str]] = {}
    if not args.scan:
        indexes = discover_indexes(root)
        if not indexes:
            print(f"No HTML index found under {root}.", file=sys.stderr)
            print("Re-run with --scan to build the library from filenames.",
                  file=sys.stderr)
            return 1
        for idx in indexes:
            found = parse_index(idx)
            print(f"{idx.parent.name}: {len(found)} entries from {idx.name}")
            meta.update(found)

    videos = find_videos(root)
    if not videos:
        print(f"ERROR: no video files under {root}", file=sys.stderr)
        return 1

    records = []
    unlisted = 0
    print(f"probing {len(videos)} videos ...")
    for path in videos:
        vid = path.stem
        info = meta.get(vid)
        if info is None:
            rel = path.relative_to(root)
            # Group by the deepest meaningful directory, skipping a "movies"
            # bucket that only exists to hold the files.
            parts = [p for p in rel.parts[:-1] if p.lower() != "movies"]
            # Empty means "no grouping": the output goes straight into the
            # destination rather than under an invented directory.
            section = parts[-1] if parts else ""
            if not args.scan:
                # An index exists but does not mention this file. Say so rather
                # than filing it under a directory name: it is the only signal
                # that the index and the folder disagree.
                unlisted += 1
                section = "Unlisted"
            info = {"title": vid, "speakers": "", "section": section}
        rel_parts = path.relative_to(root).parts
        duration, size, width, height = probe(path)
        records.append({
            "id": vid,
            "title": info["title"],
            "speakers": info["speakers"],
            "section": info["section"],
            # Top-level grouping directory, or the root's own name when the
            # videos sit directly inside it.
            "package": rel_parts[0] if len(rel_parts) > 1 else "",
            "source": str(path),
            "duration_s": round(duration, 3),
            "size_bytes": size,
            "width": width,
            "height": height,
            "src_kbps": round(size * 8 / duration / 1000) if duration else 0,
        })

    records.sort(key=lambda r: (r["package"], r["section"], r["title"].lower()))

    (out_dir / "library.json").write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    with (out_dir / "library.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)

    total_s = sum(r["duration_s"] for r in records)
    total_b = sum(r["size_bytes"] for r in records)
    print(f"\n{len(records)} videos, {total_s / 3600:.1f} hours, {total_b / 1e9:.1f} GB")
    if not args.scan:
        print(f"titled from index: {len(records) - unlisted}   unlisted: {unlisted}")
    print(f"wrote {out_dir / 'library.json'}")
    print(f"wrote {out_dir / 'library.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
