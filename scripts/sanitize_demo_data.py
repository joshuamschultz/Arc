#!/usr/bin/env -S python3 -u
"""Sanitize raw STIG/SCAP source files into demo-data/sanitized/.

One-shot script. Reads from demo-data/raw/ (gitignored — private real
data), applies deterministic substitutions defined in
~/.arc/capabilities/scap/sanitize.py (HOST_ALIASES, SUBSTITUTIONS,
OUTPUT_FILENAMES), and writes rebranded copies to demo-data/sanitized/.

Also writes the human-reviewable sanitize_map.toml under the extension's
data/ dir.

Run from the repo root::

    .venv/bin/python scripts/sanitize_demo_data.py

Re-running is safe — output is overwritten deterministically. The
sanitized files are committed to the repo; the raw files stay private.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAP_DIR = Path.home() / ".arc" / "capabilities" / "scap"

sys.path.insert(0, str(SCAP_DIR.parent))  # so `import scap` works

from scap import sanitize  # noqa: E402

RAW_DIR = REPO_ROOT / "demo-data" / "raw"
SANITIZED_DIR = REPO_ROOT / "demo-data" / "sanitized"
MAP_PATH = SCAP_DIR / "data" / "sanitize_map.toml"


def main() -> int:
    if not RAW_DIR.is_dir():
        print(f"Error: raw dir missing: {RAW_DIR}", file=sys.stderr)
        return 2

    SANITIZED_DIR.mkdir(parents=True, exist_ok=True)
    src_files = sorted(p for p in RAW_DIR.iterdir() if p.is_file() and not p.name.startswith("."))
    if not src_files:
        print(f"Error: no files in {RAW_DIR}", file=sys.stderr)
        return 2

    # Process only files explicitly registered in HOST_ALIASES — companion
    # exports (e.g. workstation .csv/.html that duplicate the .xml) are
    # skipped so we don't ship redundant unsanitized data.
    enrolled = [p for p in src_files if p.name in sanitize.HOST_ALIASES]
    skipped = [p for p in src_files if p.name not in sanitize.HOST_ALIASES]
    if skipped:
        print(f"Skipping {len(skipped)} unenrolled file(s) (not in HOST_ALIASES):")
        for p in skipped:
            print(f"  - {p.name}")
        print()

    print(f"Sanitizing {len(enrolled)} file(s) → {SANITIZED_DIR}")
    used_subs: dict[str, list[tuple[str, str]]] = {}
    total_replacements = 0
    for src in enrolled:
        out_name = sanitize.output_filename_for(src.name)
        dst = SANITIZED_DIR / out_name
        subs = sanitize.substitutions_for(src.name)
        used_subs[src.name] = subs
        counts = sanitize.sanitize_file(src, dst, subs)
        n_repl = sum(counts.values())
        total_replacements += n_repl
        if subs:
            print(f"  {src.name}")
            print(f"    -> {dst.name}  ({n_repl} substitutions across {len(subs)} pairs)")
            for orig, repl in subs:
                if counts.get(orig, 0) > 0:
                    print(f"       {orig!r:50s} -> {repl!r}  ({counts[orig]}x)")
        else:
            print(f"  {src.name}  -> {dst.name}  (no substitutions needed)")

    print(f"\nTotal replacements: {total_replacements}")
    sanitize.write_map_toml(MAP_PATH, used_subs)
    print(f"Wrote sanitize map: {MAP_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
