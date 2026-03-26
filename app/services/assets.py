from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from app.database import fetch_all, fetch_one


class AssetService:
    """Owns asset metadata, asset-job payloads, and local model helpers."""

    def __init__(self, connection: sqlite3.Connection, project_root: Path) -> None:
        self.connection = connection
        self.project_root = project_root

    def ensure_asset_directories(self) -> dict[str, Path]:
        directories = {
            "source": self.project_root / "data" / "assets" / "source",
            "generated": self.project_root / "data" / "assets" / "generated",
            "cutouts": self.project_root / "data" / "assets" / "cutouts",
            "hf_cache": self.project_root / "data" / "hf-cache",
        }
        for path in directories.values():
            path.mkdir(parents=True, exist_ok=True)
        return directories

    def list_assets(self) -> list[dict[str, Any]]:
        return fetch_all(self.connection, "SELECT * FROM assets ORDER BY id DESC")

    def add_asset(
        self,
        *,
        entity_type: str,
        entity_id: int,
        asset_kind: str,
        file_path: str,
        prompt_text: str | None = None,
        status: str = "ready",
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """
            INSERT INTO assets (entity_type, entity_id, asset_kind, file_path, prompt_text, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (entity_type, entity_id, asset_kind, file_path, prompt_text, status),
        )
        self.connection.commit()
        return fetch_one(self.connection, "SELECT * FROM assets WHERE id = ?", (cursor.lastrowid,)) or {}

    def enqueue_asset_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        cursor = self.connection.execute(
            """
            INSERT INTO generation_jobs (job_type, status, payload_json)
            VALUES (?, ?, ?)
            """,
            ("asset_request", "pending", json.dumps(payload)),
        )
        self.connection.commit()
        return fetch_one(self.connection, "SELECT * FROM generation_jobs WHERE id = ?", (cursor.lastrowid,)) or {}

    def download_hf_model(
        self,
        *,
        repo_id: str,
        allow_patterns: list[str] | None = None,
        local_dir_name: str | None = None,
    ) -> str:
        from huggingface_hub import snapshot_download

        directories = self.ensure_asset_directories()
        cache_dir = directories["hf_cache"]
        local_dir = cache_dir / (local_dir_name or repo_id.replace("/", "--"))
        result_path = snapshot_download(
            repo_id=repo_id,
            cache_dir=str(cache_dir),
            local_dir=str(local_dir),
            local_dir_use_symlinks=False,
            allow_patterns=allow_patterns,
        )
        return result_path

    def resolve_local_model_path(self, repo_id: str) -> Path | None:
        directories = self.ensure_asset_directories()
        candidate = directories["hf_cache"] / repo_id.replace("/", "--")
        return candidate if candidate.exists() else None

    def remove_background(
        self,
        *,
        source_image_path: str,
        output_name: str | None = None,
        model_repo: str = "briaai/RMBG-2.0",
        device: str = "auto",
    ) -> str:
        directories = self.ensure_asset_directories()
        input_path = Path(source_image_path).expanduser().resolve()
        if not input_path.exists():
            raise FileNotFoundError(f"Source image does not exist: {input_path}")

        if output_name:
            output_filename = output_name
        else:
            output_filename = f"{input_path.stem}-cutout.png"
        if not output_filename.lower().endswith(".png"):
            output_filename = f"{output_filename}.png"
        output_path = directories["cutouts"] / output_filename

        os.environ.setdefault("HF_HOME", str(directories["hf_cache"]))

        from PIL import Image
        import numpy as np
        image = Image.open(input_path).convert("RGB")
        original_size = image.size

        model_source = self.resolve_local_model_path(model_repo)
        if model_source is not None:
            onnx_model_path = model_source / "onnx" / "model.onnx"
            if onnx_model_path.exists():
                return self._remove_background_with_onnx(
                    input_path=input_path,
                    output_path=output_path,
                    onnx_model_path=onnx_model_path,
                    image=image,
                    original_size=original_size,
                )

        import torch
        from transformers import AutoModelForImageSegmentation

        image_size = (1024, 1024)

        image_array = np.asarray(image.resize(image_size), dtype=np.float32) / 255.0
        image_array = (image_array - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
            [0.229, 0.224, 0.225], dtype=np.float32
        )

        if device == "auto":
            resolved_device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            resolved_device = device

        input_tensor = torch.from_numpy(image_array.transpose(2, 0, 1)).unsqueeze(0).to(resolved_device)
        from_pretrained_target = str(model_source) if model_source is not None else model_repo

        model = AutoModelForImageSegmentation.from_pretrained(
            from_pretrained_target,
            trust_remote_code=True,
            local_files_only=model_source is not None,
            low_cpu_mem_usage=False,
        ).to(resolved_device)
        model.eval()

        with torch.no_grad():
            predictions = model(input_tensor)[-1].sigmoid().cpu().numpy()

        mask_array = (predictions[0].squeeze() * 255).astype("uint8")
        alpha_mask = Image.fromarray(mask_array, mode="L").resize(original_size)
        cutout = image.copy()
        cutout.putalpha(alpha_mask)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cutout.save(output_path)
        return str(output_path)

    def _remove_background_with_onnx(
        self,
        *,
        input_path: Path,
        output_path: Path,
        onnx_model_path: Path,
        image,
        original_size: tuple[int, int],
    ) -> str:
        import numpy as np
        import onnxruntime as ort
        from PIL import Image

        image_size = (1024, 1024)
        image_array = np.asarray(image.resize(image_size), dtype=np.float32) / 255.0
        image_array = (image_array - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
            [0.229, 0.224, 0.225], dtype=np.float32
        )
        input_tensor = image_array.transpose(2, 0, 1)[None, ...]

        session = ort.InferenceSession(str(onnx_model_path), providers=["CPUExecutionProvider"])
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name
        predictions = session.run([output_name], {input_name: input_tensor})[0]

        mask_array = (predictions[0].squeeze() * 255).clip(0, 255).astype("uint8")
        alpha_mask = Image.fromarray(mask_array, mode="L").resize(original_size)
        cutout = Image.open(input_path).convert("RGB")
        cutout.putalpha(alpha_mask)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cutout.save(output_path)
        return str(output_path)
