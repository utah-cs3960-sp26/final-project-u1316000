from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from app.config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a manual snapshot of the current SQLite database.")
    parser.add_argument(
        "--name",
        help="Optional label for the snapshot filename. Defaults to a timestamped name.",
    )
    parser.add_argument(
        "--output-dir",
        help="Optional snapshot directory. Defaults to data/db_snapshots beside the project database.",
    )
    return parser


def sanitize_label(label: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in label.strip())
    cleaned = cleaned.strip("-_")
    return cleaned or "snapshot"


def create_snapshot(
    database_path: str | Path,
    *,
    name: str | None = None,
    output_dir: str | Path | None = None,
) -> Path:
    resolved_database_path = Path(database_path).expanduser().resolve()
    if not resolved_database_path.exists():
        raise FileNotFoundError(f"Database does not exist: {resolved_database_path}")

    resolved_output_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else resolved_database_path.parent / "db_snapshots"
    )
    resolved_output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = sanitize_label(name) if name else timestamp
    snapshot_name = f"{resolved_database_path.stem}-{label}.db"
    snapshot_path = resolved_output_dir / snapshot_name

    if snapshot_path.exists():
        snapshot_name = f"{resolved_database_path.stem}-{label}-{timestamp}.db"
        snapshot_path = resolved_output_dir / snapshot_name

    shutil.copy2(resolved_database_path, snapshot_path)
    return snapshot_path


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    settings = Settings.from_env()
    try:
        snapshot_path = create_snapshot(
            settings.database_path,
            name=args.name,
            output_dir=args.output_dir,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    print(snapshot_path)


if __name__ == "__main__":
    main()
