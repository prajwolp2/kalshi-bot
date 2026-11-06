from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def _open_text(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else open(path, encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, plain or gzip (.gz). A truncated final line (e.g. after a crash) is reported and skipped."""
    if not path.exists():
        return []
    rows, bad = [], 0
    with _open_text(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                bad += 1
    if bad:
        print(f"warning: skipped {bad} unreadable line(s) in {path}", file=sys.stderr)
    return rows


def gzip_file(path: Path) -> Path:
    """Compress path to path.gz, check it reads back with the same number of lines, then delete the original.

    Written to a temporary name first and renamed, so a crash never leaves a half-written .gz behind.
    """
    out = path.with_name(path.name + ".gz")
    tmp = path.with_name(path.name + ".gz.tmp")
    lines = 0
    with open(path, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            lines += chunk.count(b"\n")
            dst.write(chunk)
    with gzip.open(tmp, "rb") as f:
        check = sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 20), b""))
    if check != lines:
        tmp.unlink()
        raise OSError(f"compressed copy of {path} has {check} lines, expected {lines}; original kept")
    os.replace(tmp, out)
    path.unlink()
    return out
