from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from app.services.assets import AssetService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remove image background with a Hugging Face RMBG model.")
    parser.add_argument("--input", required=True, help="Path to the source image.")
    parser.add_argument("--output-name", help="Optional output filename, defaults to <input>-cutout.png.")
    parser.add_argument("--model-repo", default="briaai/RMBG-2.0", help="Hugging Face model repo id.")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"], help="Inference device.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    connection = sqlite3.connect(":memory:")
    try:
        service = AssetService(connection, project_root)
        output_path = service.remove_background(
            source_image_path=args.input,
            output_name=args.output_name,
            model_repo=args.model_repo,
            device=args.device,
        )
    except Exception as exc:
        print(
            f"Background removal failed: {exc}\n"
            "If the model repo is gated, run `hf auth login` and accept the model terms first.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    finally:
        connection.close()

    print(output_path)


if __name__ == "__main__":
    main()
