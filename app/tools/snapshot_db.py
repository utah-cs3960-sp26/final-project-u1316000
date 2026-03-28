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


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    settings = Settings.from_env()
    database_path = settings.database_path.resolve()
    if not database_path.exists():
        print(f"Database does not exist: {database_path}", file=sys.stderr)
        raise SystemExit(1)

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else database_path.parent / "db_snapshots"
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = sanitize_label(args.name) if args.name else timestamp
    snapshot_name = f"{database_path.stem}-{label}.db"
    snapshot_path = output_dir / snapshot_name

    if snapshot_path.exists():
        snapshot_name = f"{database_path.stem}-{label}-{timestamp}.db"
        snapshot_path = output_dir / snapshot_name

    shutil.copy2(database_path, snapshot_path)
    print(snapshot_path)


if __name__ == "__main__":
    main()
