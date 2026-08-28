from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_upstream_parity.py"
START = "// coordination starts"
END = "function afterCoordination("


def _fixture(tmp_path: Path, body: str) -> tuple[Path, Path]:
    source = f"before\n{START}\n{body}\n{END}\nafter\n"
    region = source[source.index(START) : source.index(END)]
    manifest = {
        "repository": "example/repo",
        "reference_commit": "a" * 40,
        "source_path": "plugin.js",
        "start_marker": START,
        "end_marker": END,
        "coordination_sha256": hashlib.sha256(region.encode()).hexdigest(),
    }
    manifest_path = tmp_path / "manifest.json"
    source_path = tmp_path / "plugin.js"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    source_path.write_text(source, encoding="utf-8")
    return manifest_path, source_path


def _run(manifest: Path, source: Path) -> tuple[int, dict]:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--manifest",
            str(manifest),
            "--source-file",
            str(source),
            "--source-commit",
            "b" * 40,
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def test_unchanged_coordination_region_passes_even_if_full_source_changes(tmp_path: Path):
    manifest, source = _fixture(tmp_path, "same behavior")
    source.write_text(source.read_text() + "unrelated tail change\n", encoding="utf-8")
    code, report = _run(manifest, source)
    assert code == 0
    assert report["status"] == "same"


def test_changed_coordination_region_reports_drift(tmp_path: Path):
    manifest, source = _fixture(tmp_path, "old behavior")
    source.write_text(source.read_text().replace("old behavior", "new behavior"), encoding="utf-8")
    code, report = _run(manifest, source)
    assert code == 2
    assert report["status"] == "drift"


def test_missing_markers_report_drift_but_missing_file_is_operational_error(tmp_path: Path):
    manifest, source = _fixture(tmp_path, "behavior")
    source.write_text("markers removed", encoding="utf-8")
    assert _run(manifest, source)[0] == 2
    code, report = _run(manifest, tmp_path / "missing.js")
    assert code == 1
    assert report["status"] == "error"
