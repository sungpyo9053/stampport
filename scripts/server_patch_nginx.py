#!/usr/bin/env python3
"""Idempotently inject the Stampport `include` line into the SSL server
block of /etc/nginx/sites-available/default.

Designed to run with sudo from `server_apply_nginx.sh`.

Behavior:
    - If the marker comment "# stampport-managed" is already present in
      the file, this script does nothing.
    - Otherwise it finds the first `server { ... }` block that contains
      `listen ... 443 ssl` and inserts the include line just before its
      closing brace, preserving the original indentation style.

It does NOT modify the existing root `location /` (ReviewDr).
A timestamped backup of the original file is created before any change.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path


INCLUDE_LINE = "    include snippets/stampport-locations.conf;  # stampport-managed"


def _find_ssl_server_close(text: str) -> int:
    """Return the index of the closing `}` of an SSL server block, or -1."""
    starts = [m.start() for m in re.finditer(r"server\s*\{", text)]
    for s in starts:
        depth = 0
        for i in range(s, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    block = text[s : i + 1]
                    if "listen" in block and (
                        "443" in block or " ssl" in block or "\tssl" in block
                    ):
                        return i
                    break
    return -1


def _find_first_server_close(text: str) -> int:
    m = re.search(r"server\s*\{", text)
    if not m:
        return -1
    s = m.start()
    depth = 0
    for i in range(s, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--conf", default="/etc/nginx/sites-available/default")
    p.add_argument("--backup-dir", default="/etc/nginx/backups")
    args = p.parse_args()

    conf = Path(args.conf)
    if not conf.is_file():
        print(f"nginx conf not found: {conf}", file=sys.stderr)
        return 1

    text = conf.read_text()

    if "stampport-managed" in text:
        print("include line already present — no change")
        return 0

    # Always back up before mutating.
    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{conf.name}.bak.{ts}"
    shutil.copy2(conf, backup)
    print(f"backup written: {backup}")

    close_idx = _find_ssl_server_close(text)
    block_kind = "ssl"
    if close_idx < 0:
        # Fall back to the first server block — better than nothing on
        # boxes where SSL is terminated upstream. The user can move the
        # include manually if needed.
        close_idx = _find_first_server_close(text)
        block_kind = "first"
    if close_idx < 0:
        print("no `server { ... }` block found in nginx conf", file=sys.stderr)
        return 2

    # Insert just before the closing brace, preserving the trailing
    # newline so the brace stays on its own line.
    insertion = "\n" + INCLUDE_LINE + "\n"
    new = text[:close_idx] + insertion + text[close_idx:]
    conf.write_text(new)
    print(f"injected include line into {block_kind} server block")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
