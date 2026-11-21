"""Inventory a stopped Python daemon's diagnostics and retained archives.

The JSON manifest contains source paths and metadata only. Import reads the original
files in place, so this command never rewrites an archive.
"""

import argparse
import json
import sqlite3
from pathlib import Path


def inventory(deployment: str, diagnostics: Path, state: Path) -> dict:
    """Collect every recorded turn and source file from one stopped deployment."""
    database = diagnostics / "diagnostics.sqlite3"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        turns = [dict(row) for row in connection.execute("SELECT * FROM turns ORDER BY id")]
    files = []
    for root, kind in ((diagnostics / "blobs", "diagnostics"), (state / "sessions", "retained")):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            files.append(
                {
                    "kind": kind,
                    "source_path": f"{kind}/{path.relative_to(root).as_posix()}",
                    "path": str(path.resolve()),
                }
            )
    return {
        "deployment": deployment,
        "diagnostics_dir": str(diagnostics.resolve()),
        "state_dir": str(state.resolve()),
        "turns": turns,
        "files": files,
    }


def main() -> None:
    """Write a deterministic manifest to a caller-chosen private path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", help="stable source deployment identity")
    parser.add_argument("diagnostics_dir", type=Path)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    document = inventory(args.deployment, args.diagnostics_dir, args.state_dir)
    args.manifest.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
