#!/usr/bin/env python3
"""Detect changes to the official Hermes Bot Mode coordination region."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "parity" / "hermes-bot-mode.json"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _download_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-discord-botrooms"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _download_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-discord-botrooms"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def _region(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def check(
    manifest_path: Path,
    *,
    source_file: Path | None = None,
    source_commit: str = "",
) -> tuple[int, dict[str, Any]]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        repository = str(manifest["repository"])
        path = str(manifest["source_path"])
        if source_file is not None:
            latest_commit = source_commit or "local"
            source = source_file.read_text(encoding="utf-8")
        else:
            commit = _download_json(f"https://api.github.com/repos/{repository}/commits/main")
            latest_commit = str(commit["sha"])
            source = _download_text(
                f"https://raw.githubusercontent.com/{repository}/{latest_commit}/{path}"
            )
        source_hash = _sha256(source)
        try:
            region = _region(source, str(manifest["start_marker"]), str(manifest["end_marker"]))
        except ValueError:
            return 2, {
                "status": "drift",
                "reason": "coordination markers are missing or reordered",
                "reference_commit": manifest["reference_commit"],
                "latest_commit": latest_commit,
                "reference_coordination_sha256": manifest["coordination_sha256"],
                "latest_coordination_sha256": None,
                "latest_source_sha256": source_hash,
            }
        latest_hash = _sha256(region)
        status = "same" if latest_hash == manifest["coordination_sha256"] else "drift"
        return (0 if status == "same" else 2), {
            "status": status,
            "reason": "coordination region unchanged" if status == "same" else "coordination region changed",
            "reference_commit": manifest["reference_commit"],
            "latest_commit": latest_commit,
            "reference_coordination_sha256": manifest["coordination_sha256"],
            "latest_coordination_sha256": latest_hash,
            "latest_source_sha256": source_hash,
        }
    except (OSError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return 1, {
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--source-commit", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    code, result = check(
        args.manifest,
        source_file=args.source_file,
        source_commit=args.source_commit,
    )
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"{result['status']}: {result['reason']}")
    return code


if __name__ == "__main__":
    sys.exit(main())
