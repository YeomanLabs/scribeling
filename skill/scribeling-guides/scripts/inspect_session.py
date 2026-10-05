#!/usr/bin/env python3
"""
Print a compact summary of a recorded session.

guide.html embeds every screenshot as base64 and routinely runs past 500 KB, so
reading it directly is both useless and expensive. This reads steps.json instead
and prints one line per step, plus the problems worth fixing before rewriting.

    python inspect_session.py <session-dir>
    python inspect_session.py --latest [root-dir]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

DEFAULT_ROOT = Path.home() / "Documents" / "scribeling"


def latest_session(root: Path) -> Path | None:
    candidates = [p for p in root.glob("*") if (p / "steps.json").is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p / "steps.json").stat().st_mtime)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", nargs="?")
    ap.add_argument("--latest", action="store_true",
                    help="use the most recently modified session")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    args = ap.parse_args()

    if args.latest or not args.session:
        root = Path(args.session or args.root).expanduser()
        session = latest_session(root)
        if session is None:
            print(f"No session with a steps.json found under {root}")
            return 1
    else:
        session = Path(args.session).expanduser()

    manifest = session / "steps.json"
    if not manifest.is_file():
        print(f"No steps.json in {session}")
        return 1

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    steps = payload.get("steps", [])
    shots = session / "shots"

    print(f"session:      {session}")
    print(f"title:        {payload.get('title', '(none)')}")
    desc = payload.get("description", "")
    print(f"description:  {desc if desc else '(none)'}")
    print(f"author:       {payload.get('author') or '(none)'}")
    print(f"tags:         {', '.join(payload.get('tags', [])) or '(none)'}")
    prereqs = payload.get("prerequisites", [])
    print(f"prerequisites: {len(prereqs)}")
    print(f"steps:        {len(steps)} "
          f"({sum(1 for s in steps if s.get('hidden'))} hidden)")
    print()

    print("idx  action  img  window / caption")
    print("-" * 78)
    missing_images, blind, windows = [], [], []
    for s in steps:
        idx = s.get("index", "?")
        action = (s.get("action") or "click")[:6].ljust(6)
        image = s.get("image", "")
        has_img = bool(image) and (shots / image).is_file()
        flag = " ok " if has_img else " -- "
        if not has_img and s.get("action") not in ("uac", "note", "navigate"):
            missing_images.append(idx)
        caption = s.get("caption", "")
        if "highlighted control" in caption:
            blind.append(idx)
        mark = "H" if s.get("hidden") else " "
        windows.append(s.get("window", ""))
        url = f" -> {s['url']}" if s.get("url") else ""
        print(f"{str(idx):>3}{mark} {action} {flag} {(caption + url)[:58]}")

    print()
    print("windows seen:")
    for window, n in Counter(w for w in windows if w).most_common():
        print(f"  {n:>3}x  {window}")

    problems = []
    if blind:
        problems.append(f"{len(blind)} step(s) have no element name "
                        f"(UI Automation returned nothing): {blind}")
    if missing_images:
        problems.append(f"{len(missing_images)} step(s) are missing a screenshot: "
                        f"{missing_images}")
    dupes = [s.get("index") for a, s in zip(steps, steps[1:])
             if a.get("caption") == s.get("caption") and a.get("caption")]
    if dupes:
        problems.append(f"consecutive steps share a caption: {dupes}")
    if not desc.strip():
        problems.append("no description - the guide will have no lede")
    phases = {s.get("phase") or None for s in steps if not s.get("hidden")}
    if None in phases and len(phases) > 1:
        problems.append("only some steps have a phase - give every step one, "
                        "or none")

    print()
    if problems:
        print("worth fixing:")
        for item in problems:
            print(f"  - {item}")
    else:
        print("nothing obviously wrong")
    return 0


if __name__ == "__main__":
    sys.exit(main())
