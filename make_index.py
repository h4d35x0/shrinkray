#!/usr/bin/env python3
"""Write a browsable index of the transcoded phone library.

Scans the output tree produced by transcode.py, matches each file back to its
library.json record, and writes into the output root:

    index.html    one page, grouped by track, links relative to itself
    library.csv   the same data as a spreadsheet

Only files that actually exist on disk are listed, so running this against a
partial transcode gives an honest picture of what is finished.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcode import out_path  # noqa: E402  same naming rules as the encoder


def hms(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DEF CON 34 talks</title>
<style>
  :root {{ color-scheme: light dark; --fg: #1a1a1a; --dim: #666; --line: #ddd; --bg: #fff; --accent: #0a7; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg: #e8e8e8; --dim: #999; --line: #333; --bg: #141414; --accent: #3d8; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 1rem; background: var(--bg); color: var(--fg);
         font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  header {{ max-width: 60rem; margin: 0 auto 1.5rem; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
  .meta {{ color: var(--dim); font-size: .85rem; }}
  main {{ max-width: 60rem; margin: 0 auto; }}
  h2 {{ font-size: 1rem; text-transform: uppercase; letter-spacing: .06em;
        color: var(--dim); margin: 2rem 0 .5rem; padding-bottom: .3rem;
        border-bottom: 1px solid var(--line); }}
  ul {{ list-style: none; margin: 0; padding: 0; }}
  li {{ padding: .55rem 0; border-bottom: 1px solid var(--line); }}
  a {{ color: var(--accent); text-decoration: none; font-weight: 500; }}
  a:hover {{ text-decoration: underline; }}
  .sub {{ color: var(--dim); font-size: .82rem; margin-top: .15rem; }}
  #q {{ width: 100%; max-width: 60rem; padding: .6rem .8rem; font-size: 1rem;
        border: 1px solid var(--line); border-radius: .4rem;
        background: var(--bg); color: var(--fg); margin-bottom: .5rem; }}
</style>
</head>
<body>
<header>
  <h1>DEF CON 34</h1>
  <div class="meta">{count} talks &middot; {hours:.1f} hours &middot; {gb:.1f} GB</div>
</header>
<main>
  <input id="q" type="search" placeholder="Filter by title or speaker" autocomplete="off">
  {body}
</main>
<script>
  const q = document.getElementById('q');
  const items = [...document.querySelectorAll('li')];
  const groups = [...document.querySelectorAll('section')];
  q.addEventListener('input', () => {{
    const t = q.value.toLowerCase();
    items.forEach(li => {{
      li.hidden = t !== '' && !li.dataset.s.includes(t);
    }});
    groups.forEach(g => {{
      g.hidden = ![...g.querySelectorAll('li')].some(li => !li.hidden);
    }});
  }});
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", default="library.json")
    ap.add_argument("--out", default="output")
    args = ap.parse_args()

    out_root = Path(args.out).resolve()
    if not out_root.is_dir():
        print(f"ERROR: output dir not found: {out_root}", file=sys.stderr)
        return 1

    records = json.loads(Path(args.library).read_text(encoding="utf-8"))

    present, missing = [], []
    for record in records:
        dest = out_path(record, out_root)
        if dest.is_file():
            record = dict(record, rel=dest.relative_to(out_root).as_posix(),
                          out_bytes=dest.stat().st_size)
            present.append(record)
        else:
            missing.append(record["id"])

    if not present:
        print("ERROR: no transcoded files found. Run transcode.py first.", file=sys.stderr)
        return 1

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in present:
        groups[(record["package"], record["section"])].append(record)

    chunks = []
    for (package, section), items in sorted(groups.items()):
        rows = []
        for record in sorted(items, key=lambda r: r["title"].lower()):
            speakers = record["speakers"]
            sub = f"{hms(record['duration_s'])} &middot; {record['out_bytes'] / 1e6:.0f} MB"
            if speakers:
                sub = f"{html.escape(speakers)} &middot; {sub}"
            search = html.escape(f"{record['title']} {speakers}".lower(), quote=True)
            # Spaces and brackets in a filename must be percent-encoded or the
            # link breaks in strict browsers and in anything that re-parses it.
            href = quote(record["rel"], safe="/")
            rows.append(
                f'    <li data-s="{search}">'
                f'<a href="{html.escape(href, quote=True)}">'
                f'{html.escape(record["title"])}</a>'
                f'<div class="sub">{sub}</div></li>'
            )
        chunks.append(
            f'  <section>\n  <h2>{html.escape(package)} &middot; {html.escape(section)} '
            f'({len(items)})</h2>\n  <ul>\n' + "\n".join(rows) + "\n  </ul>\n  </section>"
        )

    total_s = sum(r["duration_s"] for r in present)
    total_b = sum(r["out_bytes"] for r in present)
    (out_root / "index.html").write_text(
        PAGE.format(count=len(present), hours=total_s / 3600, gb=total_b / 1e9,
                    body="\n".join(chunks)),
        encoding="utf-8",
    )

    fields = ["id", "title", "speakers", "package", "section",
              "duration_s", "out_bytes", "src_kbps", "rel"]
    with (out_root / "library.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(present)

    print(f"indexed {len(present)} files, {total_s / 3600:.1f} hours, {total_b / 1e9:.1f} GB")
    if missing:
        print(f"not yet transcoded: {len(missing)} (first: {missing[0]})")
    print(f"wrote {out_root / 'index.html'}")
    print(f"wrote {out_root / 'library.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
