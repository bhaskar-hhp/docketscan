#!/usr/bin/env python3
"""Build the static (GitHub Pages) copy of the UI into docs/.

    python3 scripts/build_static.py

The only difference from static/index.html is a `config.js` script tag; the
Worker URL lives in docs/config.js so it can be changed without a rebuild.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "static" / "index.html"
OUT = ROOT / "docs" / "index.html"
TAG = '  <script src="config.js"></script>\n'

def main() -> int:
    html = SRC.read_text(encoding="utf-8")
    # Guard on the actual tag: the page text also mentions "docs/config.js",
    # so a bare "config.js" substring test silently skips the injection.
    if TAG.strip() not in html:
        marker = "<script>\n"
        idx = html.rfind(marker)
        if idx == -1:
            print("could not find the main <script> block", file=sys.stderr)
            return 1
        html = html[:idx] + TAG + html[idx:]
    if TAG.strip() not in html:
        print("config.js tag was not injected", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    (OUT.parent / ".nojekyll").touch()

    cfg = OUT.parent / "config.js"
    if not cfg.exists():
        cfg.write_text(
            '// Point this at your deployed Cloudflare Worker.\n'
            '// Get the URL from `npx wrangler deploy` output.\n'
            'window.DOCKETSCAN_API = "";\n',
            encoding="utf-8",
        )

    print(f"wrote {OUT.relative_to(ROOT)} ({len(html)} bytes)")
    print(f"config: {cfg.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
