#!/usr/bin/env python3
"""End-to-end smoke test. Runs the whole pipeline on generated video.

Uses synthetic clips from ffmpeg's lavfi source, so it needs no fixtures in the
repository and runs identically on Windows, macOS and Linux. It encodes with
libx265 because CI runners have no NVENC.

    python tests/smoke.py

Exits non-zero on the first failure, with the reason.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    """Detail is only shown on failure; on a pass it is just noise in a CI log."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}", flush=True)
    if not ok:
        if detail:
            for line in str(detail).strip().splitlines()[-8:]:
                print(f"        {line}", flush=True)
        failures.append(f"{label}: {str(detail).strip()[:200]}" if detail else label)
    return ok


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, errors="replace",
                          creationflags=NO_WINDOW, **kw)


def script(name: str, *args: str) -> subprocess.CompletedProcess:
    return run([sys.executable, str(ROOT / name), *args], cwd=str(ROOT))


def make_clip(path: Path, seconds: int = 4) -> None:
    """A high-bitrate 720p clip, so there is something real to shrink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    r = run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-b:v", "4000k", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        "-movflags", "+faststart", str(path),
    ])
    if r.returncode != 0 or not path.is_file():
        raise SystemExit(f"could not generate test clip: {r.stderr[:400]}")


def main() -> int:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"ERROR: {tool} not on PATH", file=sys.stderr)
            return 2

    print(f"python  {sys.version.split()[0]}")
    print(f"platform {sys.platform}")
    ver = run(["ffmpeg", "-version"]).stdout.splitlines()[0][:60]
    print(f"ffmpeg  {ver}\n")

    with tempfile.TemporaryDirectory(prefix="shrinkray-smoke-") as tmp:
        tmp = Path(tmp)
        src, out = tmp / "src", tmp / "out"

        # Three clips, one of them in a subdirectory, to exercise grouping.
        make_clip(src / "clip one.mp4")
        make_clip(src / "clip two.mp4")
        make_clip(src / "nested" / "clip three.mp4")
        src_bytes = sum(p.stat().st_size for p in src.rglob("*.mp4"))
        print(f"generated 3 clips, {src_bytes / 1e6:.1f} MB\n")

        print("build_index --scan")
        r = script("build_index.py", "--scan", "--root", str(src), "--out", str(out))
        check("build_index exits 0", r.returncode == 0, r.stderr[-200:])
        lib = out / "library.json"
        if not check("library.json written", lib.is_file()):
            return report()
        records = json.loads(lib.read_text(encoding="utf-8"))
        check("all 3 videos indexed", len(records) == 3, f"got {len(records)}")

        print("\ntranscode --encoder x265")
        r = script("transcode.py", "--library", str(lib), "--out", str(out),
                   "--encoder", "x265", "--cq", "30", "--workers", "2")
        check("transcode exits 0", r.returncode == 0, r.stdout[-300:] + r.stderr[-300:])
        outs = [p for p in out.rglob("*.mp4") if not p.name.endswith(".part.mp4")]
        check("3 outputs produced", len(outs) == 3, f"got {len(outs)}")
        check("no .part files left behind", not list(out.rglob("*.part.mp4")))
        out_bytes = sum(p.stat().st_size for p in outs)
        check("output is smaller than source", out_bytes < src_bytes,
              f"{out_bytes / 1e6:.1f} MB vs {src_bytes / 1e6:.1f} MB")
        check("subdirectory preserved",
              any(p.parent.name == "nested" for p in outs))

        print("\ntranscode --verify (full decode)")
        r = script("transcode.py", "--library", str(lib), "--out", str(out), "--verify")
        check("verify exits 0", r.returncode == 0, r.stdout[-300:])
        check("verify reports no bad files", "bad 0" in r.stdout,
              r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")

        print("\nresume: re-running must skip finished work")
        r = script("transcode.py", "--library", str(lib), "--out", str(out),
                   "--encoder", "x265", "--cq", "30")
        check("re-run exits 0", r.returncode == 0)
        check("re-run skipped all 3, encoded none", "encoded 0, skipped 3" in r.stdout,
              [ln for ln in r.stdout.splitlines() if "skipped" in ln][:1])

        print("\nmake_index")
        r = script("make_index.py", "--library", str(lib), "--out", str(out),
                   "--title", "Smoke Test")
        check("make_index exits 0", r.returncode == 0, r.stderr[-200:])
        page = out / "index.html"
        check("index.html written", page.is_file())
        if page.is_file():
            html = page.read_text(encoding="utf-8")
            check("index carries the title", "Smoke Test" in html)
            check("index lists all 3", html.count("</a>") >= 3)

        print("\nextract_audio")
        r = script("extract_audio.py", "--library", str(lib), "--video", str(out),
                   "--out", str(out / "audio"))
        check("extract_audio exits 0", r.returncode == 0,
              r.stdout[-300:] + r.stderr[-300:])
        m4a = list((out / "audio").rglob("*.m4a"))
        check("3 audio files produced", len(m4a) == 3, f"got {len(m4a)}")
        check("audio is smaller than video", sum(p.stat().st_size for p in m4a) < out_bytes)

    return report()


def report() -> int:
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
