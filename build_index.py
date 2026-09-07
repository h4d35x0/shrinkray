#!/usr/bin/env python3
"""Parse the two DEF CON 34 "Start Here" HTML indexes into a machine-readable library.

Reads:
  <root>/Tracks/DefCon 34 Tracks Start Here.html
  <root>/Creator Stages/DefCon 34 Creator Stages Start Here.html

Writes:
  <out>/library.json   one record per source video
  <out>/library.csv    the same, for eyeballing in a spreadsheet

Every mp4 under <root> is emitted, including any the HTML does not reference;
those get section "Unlisted" and their filename stem as the title, so nothing is
silently dropped.
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

# The two package sections and the HTML index that describes each one.
SOURCES = [
    ("Tracks", "Tracks/DefCon 34 Tracks Start Here.html"),
    ("Creator Stages", "Creator Stages/DefCon 34 Creator Stages Start Here.html"),
]

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


def parse_index(path: Path) -> dict[str, dict[str, str]]:
    """Return {video_id: {"title", "speakers", "section"}} for one HTML index.

    The file opens with a table of contents whose anchors are '#nn' rather than
    'movies/<id>.mp4', so ENTRY_RE cannot match inside it. Sections are
    sliced off the first real section heading onward.
    """
    raw = path.read_text(encoding="utf-8")

    starts = [(m.start(), m.group(1)) for m in SECTION_RE.finditer(raw)]
    if not starts:
        raise ValueError(f"no section headings found in {path}")

    # Slice the document into [section_name, section_body] spans.
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
                print(f"  warning: {vid} listed twice with different metadata", file=sys.stderr)
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
    )
    vals = [v for v in res.stdout.split() if v]
    width, height, duration, size = vals[0], vals[1], vals[2], vals[3]
    return float(duration), int(size), int(width), int(height)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True,
                    help="package root holding 'Tracks' and 'Creator Stages'")
    ap.add_argument("--out", default=".",
                    help="directory to write library.json and library.csv into")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    out_dir = Path(args.out).resolve()
    if not root.is_dir():
        print(f"ERROR: root not found: {root}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    meta: dict[str, dict[str, str]] = {}
    for label, rel in SOURCES:
        idx = root / rel
        if not idx.is_file():
            print(f"ERROR: missing index {idx}", file=sys.stderr)
            return 1
        found = parse_index(idx)
        print(f"{label}: parsed {len(found)} entries from {idx.name}")
        meta.update(found)

    videos = sorted(root.glob("*/movies/*.mp4"))
    if not videos:
        print(f"ERROR: no mp4 files under {root}", file=sys.stderr)
        return 1

    records = []
    unlisted = 0
    print(f"probing {len(videos)} videos ...")
    for path in videos:
        vid = path.stem
        info = meta.get(vid)
        if info is None:
            unlisted += 1
            info = {"title": vid, "speakers": "", "section": "Unlisted"}
        duration, size, width, height = probe(path)
        records.append({
            "id": vid,
            "title": info["title"],
            "speakers": info["speakers"],
            "section": info["section"],
            # The top-level package folder: "Tracks" or "Creator Stages".
            "package": path.relative_to(root).parts[0],
            "source": str(path),
            "duration_s": round(duration, 3),
            "size_bytes": size,
            "width": width,
            "height": height,
            "src_kbps": round(size * 8 / duration / 1000) if duration else 0,
        })

    records.sort(key=lambda r: (r["package"], r["section"], r["title"].lower()))

    (out_dir / "library.json").write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with (out_dir / "library.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)

    total_s = sum(r["duration_s"] for r in records)
    total_b = sum(r["size_bytes"] for r in records)
    print(f"\n{len(records)} videos, {total_s / 3600:.1f} hours, {total_b / 1e9:.1f} GB")
    print(f"titled from index: {len(records) - unlisted}   unlisted: {unlisted}")
    print(f"wrote {out_dir / 'library.json'}")
    print(f"wrote {out_dir / 'library.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
