from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from app.services.assets import AssetService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download a Hugging Face model into the local project cache.")
    parser.add_argument("--repo", required=True, help="Hugging Face repo id, for example briaai/RMBG-2.0.")
    parser.add_argument(
        "--allow-pattern",
        action="append",
        dest="allow_patterns",
        help="Optional allow pattern to limit downloaded files. Can be provided multiple times.",
    )
    parser.add_argument("--local-dir-name", help="Optional custom directory name inside the local HF cache.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    connection = sqlite3.connect(":memory:")
    try:
        service = AssetService(connection, project_root)
        path = service.download_hf_model(
            repo_id=args.repo,
            allow_patterns=args.allow_patterns,
            local_dir_name=args.local_dir_name,
        )
    except Exception as exc:
        print(
            f"Download failed: {exc}\n"
            "If the repo is gated, run `hf auth login` and accept the model terms on Hugging Face first.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    finally:
        connection.close()

    print(path)


if __name__ == "__main__":
    main()
