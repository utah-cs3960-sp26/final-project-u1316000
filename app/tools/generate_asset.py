from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from app.config import Settings
from app.database import bootstrap_database
from app.services.assets import AssetService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate an image asset through a local ComfyUI workflow.")
    parser.add_argument("--asset-kind", required=True, choices=["background", "portrait", "object_render"])
    parser.add_argument("--entity-type", required=True, choices=["location", "character", "object"])
    parser.add_argument("--entity-id", required=True, type=int)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--workflow-name", default="text-to-image")
    parser.add_argument("--filename-base")
    parser.add_argument("--negative-prompt")
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--steps", type=int, default=25)
    parser.add_argument("--guidance-scale", type=float, default=4.0)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--remove-background", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    settings = Settings.from_env()
    bootstrap_database(settings.database_path)
    connection = sqlite3.connect(settings.database_path)
    connection.row_factory = sqlite3.Row
    try:
        service = AssetService(connection, project_root)
        result = service.generate_with_comfyui(
            workflow_path=settings.comfyui_workflow_dir / f"{args.workflow_name}.api.json",
            comfyui_base_url=settings.comfyui_base_url,
            comfyui_output_dir=settings.comfyui_output_dir,
            entity_type=args.entity_type,
            entity_id=args.entity_id,
            asset_kind=args.asset_kind,
            prompt=args.prompt,
            width=args.width,
            height=args.height,
            steps=args.steps,
            guidance_scale=args.guidance_scale,
            seed=args.seed,
            negative_prompt=args.negative_prompt,
            filename_base=args.filename_base,
            remove_background=args.remove_background,
        )
    except Exception as exc:
        print(f"Asset generation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        connection.close()

    print(result["output_path"])
    if result.get("cutout_asset"):
        print(result["cutout_asset"]["file_path"])


if __name__ == "__main__":
    main()
