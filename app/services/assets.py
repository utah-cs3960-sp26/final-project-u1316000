from __future__ import annotations

import copy
import json
import os
import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

from app.database import fetch_all, fetch_one

GLOBAL_STYLE_PREFIX = (
    "Style: Cinematic epic fantasy concept art, in the style of Jordan Grimmer and Tobias Roetsch. "
    "Ethereal, hyper-detailed digital painting, heavy rim-lighting, 'dark but bright' high contrast, "
    "vibrant magical atmosphere, 8k resolution, volumetric lighting, sense of wonder and adventure."
)

DEFAULT_ASSET_DIMENSIONS: dict[str, tuple[int, int]] = {
    "background": (1600, 896),
    "portrait": (1024, 1536),
    "object_render": (1024, 1024),
}

SUBJECT_ONLY_SUFFIX = (
    "Plain white background. Styling applies to the subject only. Make sure subject is centered and the "
    "subject is the only thing visible besides the background. Make sure the full body is in view. "
    "Show the subject once only. Not a character sheet, model sheet, turnaround, reference lineup, or "
    "multiple-angle composition."
)

BACKGROUND_ENVIRONMENT_SUFFIX = (
    "Environment scene only. No characters, no hands, no bodies, no faces, no portraits, no close foreground "
    "subject, no viewer-facing protagonist, no person-shaped silhouette, and no staged hero object dominating "
    "the frame. Show the setting itself as a wide establishing shot with clear depth and spacious landscape "
    "composition suitable for a story scene backdrop. Important objects may appear only if they are naturally "
    "embedded in the environment, not isolated like product shots."
)

BACKGROUND_NEGATIVE_SUFFIX = (
    "character, person, portrait, hand, hands, face, body, figure, close-up subject, centered subject, "
    "isolated object, white background, cutout, product shot"
)

PORTRAIT_FRAME_SUFFIX = (
    "Single character only. Full-body framing required (head-to-toe fully visible). "
    "Do not crop off feet, hat, hands, or any body parts. Keep the character centered with generous margins "
    "on all sides so background removal and normalization work cleanly. Use one single pose from one camera "
    "angle only. No front/side/back views, no duplicate figures, no extra poses, no inset heads, no "
    "expression sheet, and no design callouts."
)
PORTRAIT_NEGATIVE_SUFFIX = (
    "character sheet, model sheet, turnaround, reference sheet, lineup, multiple views, multiple angles, "
    "front view, side view, back view, split panel, collage, duplicate person, extra body, extra pose, "
    "expression sheet, design callouts, pose sheet"
)

OBJECT_ISOLATION_SUFFIX = (
    "Single object only on plain white background. No extra props, no hands, no characters, no scenery, "
    "no text overlays, no labels, no stand-ins, and no framing objects. Keep the entire object fully in frame "
    "with generous margins on all sides so cutout extraction is clean."
)
DETAIL_GUIDANCE_SUFFIX = (
    "Describe the subject or scene richly with mood, lighting, physical details, scale, materials, "
    "environment storytelling, and any important hooks. Do not specify art style directions beyond the "
    "content itself."
)


def default_dimensions_for_asset_kind(asset_kind: str) -> tuple[int, int]:
    return DEFAULT_ASSET_DIMENSIONS.get(asset_kind, (1024, 1024))


class ComfyUIClient:
    def __init__(self, base_url: str, timeout_seconds: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def submit_workflow(self, workflow: dict[str, Any]) -> str:
        response = httpx.post(f"{self.base_url}/prompt", json={"prompt": workflow}, timeout=30.0)
        response.raise_for_status()
        data = response.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI did not return a prompt_id: {data}")
        return str(prompt_id)

    def wait_for_history(self, prompt_id: str, *, poll_interval: float = 1.5) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        history_url = f"{self.base_url}/history/{prompt_id}"
        while time.monotonic() < deadline:
            response = httpx.get(history_url, timeout=30.0)
            response.raise_for_status()
            history = response.json()
            if history:
                if prompt_id in history:
                    return history[prompt_id]
                return history
            time.sleep(poll_interval)
        raise TimeoutError(f"Timed out waiting for ComfyUI prompt {prompt_id}.")


class AssetService:
    """Owns asset metadata, asset-job payloads, local model helpers, and ComfyUI generation."""

    def __init__(self, connection: sqlite3.Connection, project_root: Path) -> None:
        self.connection = connection
        self.project_root = project_root

    def ensure_asset_directories(self) -> dict[str, Path]:
        directories = {
            "source": self.project_root / "data" / "assets" / "source",
            "generated": self.project_root / "data" / "assets" / "generated",
            "cutouts": self.project_root / "data" / "assets" / "cutouts",
            "comfy_output": self.project_root / "data" / "assets" / "comfy_output",
            "hf_cache": self.project_root / "data" / "hf-cache",
        }
        for path in directories.values():
            path.mkdir(parents=True, exist_ok=True)
        return directories

    def list_assets(self) -> list[dict[str, Any]]:
        return fetch_all(self.connection, "SELECT * FROM assets ORDER BY id DESC")

    def get_latest_asset(
        self,
        *,
        entity_type: str,
        entity_id: int,
        asset_kind: str,
    ) -> dict[str, Any] | None:
        return fetch_one(
            self.connection,
            """
            SELECT *
            FROM assets
            WHERE entity_type = ? AND entity_id = ? AND asset_kind = ? AND status = 'ready'
            ORDER BY id DESC
            LIMIT 1
            """,
            (entity_type, entity_id, asset_kind),
        )

    def get_preferred_asset(
        self,
        *,
        entity_type: str,
        entity_id: int,
        preferred_kinds: list[str],
    ) -> dict[str, Any] | None:
        for asset_kind in preferred_kinds:
            asset = self.get_latest_asset(entity_type=entity_type, entity_id=entity_id, asset_kind=asset_kind)
            if asset is not None:
                return asset
        return None

    def media_url_for_path(self, file_path: str | Path) -> str | None:
        resolved_path = Path(file_path).expanduser().resolve()
        asset_root = (self.project_root / "data" / "assets").resolve()
        try:
            relative_path = resolved_path.relative_to(asset_root)
        except ValueError:
            return None
        try:
            version = resolved_path.stat().st_mtime_ns
        except FileNotFoundError:
            version = None
        base_url = f"/media/{relative_path.as_posix()}"
        if version is None:
            return base_url
        return f"{base_url}?v={version}"

    def resolve_scene_assets(self, scene_definition: dict[str, Any]) -> dict[str, Any]:
        resolved_scene = copy.deepcopy(scene_definition)
        location_entity_id = resolved_scene.get("location_entity_id")
        background_asset = None
        if location_entity_id is not None:
            background_asset = self.get_latest_asset(
                entity_type="location",
                entity_id=int(location_entity_id),
                asset_kind="background",
            )
        resolved_scene["background_url"] = (
            self.media_url_for_path(background_asset["file_path"]) if background_asset is not None else None
        )

        actors: list[dict[str, Any]] = []
        for entity in resolved_scene.get("present_entities", []):
            entity_type = entity["entity_type"]
            entity_id = int(entity["entity_id"])
            preferred_kinds = ["cutout"]
            if entity_type == "object":
                preferred_kinds.append("object_render")
            elif entity_type == "character":
                preferred_kinds.append("portrait")
            asset = self.get_preferred_asset(
                entity_type=entity_type,
                entity_id=entity_id,
                preferred_kinds=preferred_kinds,
            )
            raw_scale = entity.get("scale")
            raw_offset_x = entity.get("offset_x_percent")
            raw_offset_y = entity.get("offset_y_percent")
            actor = {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "slot": entity["slot"],
                "focus": bool(entity.get("focus", False)),
                "scale": float(raw_scale) if raw_scale is not None else 1.0,
                "offset_x_percent": float(raw_offset_x) if raw_offset_x is not None else 0.0,
                "offset_y_percent": float(raw_offset_y) if raw_offset_y is not None else 0.0,
                "hidden_on_lines": list(entity.get("hidden_on_lines", [])),
                "use_player_fallback": bool(entity.get("use_player_fallback", False)),
                "asset_kind": asset["asset_kind"] if asset is not None else None,
                "display_class": self.get_asset_display_class(asset) if asset is not None else None,
                "asset_url": self.media_url_for_path(asset["file_path"]) if asset is not None else None,
            }
            actors.append(actor)
        resolved_scene["actors"] = actors
        return resolved_scene

    def add_asset(
        self,
        *,
        entity_type: str,
        entity_id: int,
        asset_kind: str,
        file_path: str,
        display_class: str | None = None,
        normalization: dict[str, Any] | None = None,
        prompt_text: str | None = None,
        status: str = "ready",
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """
            INSERT INTO assets (entity_type, entity_id, asset_kind, file_path, display_class, normalization_json, prompt_text, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                entity_id,
                asset_kind,
                file_path,
                display_class,
                json.dumps(normalization or {}),
                prompt_text,
                status,
            ),
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

    def generate_with_comfyui(
        self,
        *,
        workflow_path: str | Path,
        comfyui_base_url: str,
        comfyui_output_dir: str | Path,
        entity_type: str,
        entity_id: int,
        asset_kind: str,
        prompt: str,
        width: int | None,
        height: int | None,
        steps: int,
        guidance_scale: float,
        seed: int | None = None,
        negative_prompt: str | None = None,
        filename_base: str | None = None,
        metadata: dict[str, Any] | None = None,
        remove_background: bool = False,
    ) -> dict[str, Any]:
        directories = self.ensure_asset_directories()
        workflow = self.load_workflow_template(workflow_path)
        resolved_seed = int(seed if seed is not None else random.randint(1, 2**53 - 1))
        should_remove_background = remove_background or asset_kind in {"portrait", "object_render"}
        final_prompt = self.compose_generation_prompt(asset_kind=asset_kind, user_prompt=prompt)
        resolved_negative_prompt = self.compose_negative_prompt(
            asset_kind=asset_kind,
            user_negative_prompt=negative_prompt,
        )
        resolved_width, resolved_height = self.resolve_generation_dimensions(
            asset_kind=asset_kind,
            width=width,
            height=height,
        )
        safe_name = self.build_filename_base(
            entity_type=entity_type,
            entity_id=entity_id,
            asset_kind=asset_kind,
            prompt=prompt,
            filename_base=filename_base,
        )
        filename_prefix = f"{asset_kind}/{safe_name}"
        prepared_workflow = self.prepare_comfyui_workflow(
            workflow,
            prompt=final_prompt,
            negative_prompt=resolved_negative_prompt,
            width=resolved_width,
            height=resolved_height,
            steps=steps,
            guidance_scale=guidance_scale,
            seed=resolved_seed,
            filename_prefix=filename_prefix,
        )

        client = ComfyUIClient(comfyui_base_url)
        prompt_id = client.submit_workflow(prepared_workflow)
        history = client.wait_for_history(prompt_id)
        output_path, image_info = self.resolve_comfyui_output_path(history, Path(comfyui_output_dir))
        if not output_path.exists():
            raise FileNotFoundError(f"ComfyUI reported an output file that does not exist: {output_path}")

        final_path = self.import_generated_asset(
            source_path=output_path,
            entity_type=entity_type,
            entity_id=entity_id,
            asset_kind=asset_kind,
            filename_base=safe_name,
            generated_root=directories["generated"],
        )

        generation_metadata = {
            "backend": "comfyui",
            "workflow_path": str(Path(workflow_path).resolve()),
            "prompt_id": prompt_id,
            "prompt": prompt,
            "final_prompt": final_prompt,
            "negative_prompt": resolved_negative_prompt,
            "width": resolved_width,
            "height": resolved_height,
            "steps": steps,
            "guidance_scale": guidance_scale,
            "seed": resolved_seed,
            "filename_prefix": filename_prefix,
            "background_removal_applied": should_remove_background,
            "comfyui_output": image_info,
            "metadata": metadata or {},
        }

        generated_asset = self.add_asset(
            entity_type=entity_type,
            entity_id=entity_id,
            asset_kind=asset_kind,
            file_path=str(final_path),
            display_class=self.infer_display_class(entity_type=entity_type, asset_kind=asset_kind),
            prompt_text=json.dumps(generation_metadata),
        )

        cutout_asset = None
        if should_remove_background:
            cutout_name = f"{safe_name}-cutout.png"
            cutout_result = self.remove_background(
                source_image_path=str(final_path),
                output_name=cutout_name,
                entity_type=entity_type,
                asset_kind=asset_kind,
            )
            cutout_asset = self.add_asset(
                entity_type=entity_type,
                entity_id=entity_id,
                asset_kind="cutout",
                file_path=cutout_result["output_path"],
                display_class=cutout_result["display_class"],
                normalization=cutout_result["normalization"],
                prompt_text=json.dumps({"source_asset_id": generated_asset["id"], **generation_metadata}),
            )

        return {
            "prompt_id": prompt_id,
            "output_path": str(final_path),
            "asset": generated_asset,
            "cutout_asset": cutout_asset,
        }

    def compose_generation_prompt(self, *, asset_kind: str, user_prompt: str) -> str:
        sections = [GLOBAL_STYLE_PREFIX, "", user_prompt.strip(), "", DETAIL_GUIDANCE_SUFFIX]
        if asset_kind == "background":
            sections.extend(["", BACKGROUND_ENVIRONMENT_SUFFIX])
        elif asset_kind == "portrait":
            sections.extend(["", SUBJECT_ONLY_SUFFIX, "", PORTRAIT_FRAME_SUFFIX])
        elif asset_kind == "object_render":
            sections.extend(["", SUBJECT_ONLY_SUFFIX, "", OBJECT_ISOLATION_SUFFIX])
        return "\n".join(part for part in sections if part is not None)

    def compose_negative_prompt(self, *, asset_kind: str, user_negative_prompt: str | None) -> str | None:
        parts = [part.strip() for part in [user_negative_prompt] if part and part.strip()]
        if asset_kind == "background":
            parts.append(BACKGROUND_NEGATIVE_SUFFIX)
        elif asset_kind == "portrait":
            parts.append(PORTRAIT_NEGATIVE_SUFFIX)
        if not parts:
            return None
        return ", ".join(parts)

    def resolve_generation_dimensions(
        self,
        *,
        asset_kind: str,
        width: int | None,
        height: int | None,
    ) -> tuple[int, int]:
        default_width, default_height = default_dimensions_for_asset_kind(asset_kind)
        return width or default_width, height or default_height

    def load_workflow_template(self, workflow_path: str | Path) -> dict[str, Any]:
        path = Path(workflow_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Workflow file does not exist: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def prepare_comfyui_workflow(
        self,
        workflow: dict[str, Any],
        *,
        prompt: str,
        negative_prompt: str | None,
        width: int,
        height: int,
        steps: int,
        guidance_scale: float,
        seed: int,
        filename_prefix: str,
    ) -> dict[str, Any]:
        prepared = copy.deepcopy(workflow)
        save_node = self.find_node(prepared, class_type="SaveImage")
        latent_node = self.find_node(prepared, class_type="EmptySD3LatentImage")
        sampler_node = self.find_node(prepared, class_type="KSampler")
        positive_node = self.find_node(prepared, class_type="CLIPTextEncode", title_contains="Positive Prompt")
        negative_node = self.find_node(prepared, class_type="CLIPTextEncode", title_contains="Negative Prompt")

        save_node["inputs"]["filename_prefix"] = filename_prefix
        latent_node["inputs"]["width"] = width
        latent_node["inputs"]["height"] = height
        sampler_node["inputs"]["seed"] = seed
        sampler_node["inputs"]["steps"] = steps
        sampler_node["inputs"]["cfg"] = guidance_scale
        positive_node["inputs"]["text"] = prompt
        if negative_prompt is not None:
            negative_node["inputs"]["text"] = negative_prompt
        return prepared

    def find_node(
        self,
        workflow: dict[str, Any],
        *,
        class_type: str,
        title_contains: str | None = None,
    ) -> dict[str, Any]:
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            if node.get("class_type") != class_type:
                continue
            title = str(node.get("_meta", {}).get("title", ""))
            if title_contains is not None and title_contains not in title:
                continue
            return node
        raise KeyError(f"Could not find node class_type={class_type!r} title_contains={title_contains!r}")

    def resolve_comfyui_output_path(self, history: dict[str, Any], output_root: Path) -> tuple[Path, dict[str, Any]]:
        outputs = history.get("outputs", history)
        for node_output in outputs.values():
            if not isinstance(node_output, dict):
                continue
            images = node_output.get("images") or []
            if images:
                image_info = images[0]
                filename = image_info.get("filename")
                if not filename:
                    continue
                subfolder = image_info.get("subfolder") or ""
                resolved = output_root / subfolder / filename
                return resolved.resolve(), image_info
        raise RuntimeError(f"ComfyUI history did not contain any image outputs: {history}")

    def import_generated_asset(
        self,
        *,
        source_path: Path,
        entity_type: str,
        entity_id: int,
        asset_kind: str,
        filename_base: str,
        generated_root: Path,
    ) -> Path:
        destination_dir = generated_root / asset_kind
        destination_dir.mkdir(parents=True, exist_ok=True)
        suffix = source_path.suffix or ".png"
        destination = destination_dir / f"{entity_type}_{entity_id}_{filename_base}{suffix}"
        counter = 2
        while destination.exists():
            destination = destination_dir / f"{entity_type}_{entity_id}_{filename_base}_v{counter}{suffix}"
            counter += 1
        destination.write_bytes(source_path.read_bytes())
        return destination

    def build_filename_base(
        self,
        *,
        entity_type: str,
        entity_id: int,
        asset_kind: str,
        prompt: str,
        filename_base: str | None,
    ) -> str:
        candidate = filename_base or f"{entity_type}-{entity_id}-{asset_kind}"
        if filename_base is None:
            candidate = f"{candidate}-{prompt[:48]}"
        normalized = re.sub(r"[^a-z0-9]+", "-", candidate.lower()).strip("-")
        return normalized[:80] or f"{entity_type}-{entity_id}-{asset_kind}"

    def remove_background(
        self,
        *,
        source_image_path: str,
        output_name: str | None = None,
        model_repo: str = "briaai/RMBG-2.0",
        device: str = "auto",
        entity_type: str | None = None,
        asset_kind: str | None = None,
    ) -> dict[str, Any]:
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
        display_class = self.infer_display_class(entity_type=entity_type, asset_kind=asset_kind or "cutout")

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
                    display_class=display_class,
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
        cutout, normalization = self.normalize_cutout_frame(cutout, display_class=display_class)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cutout.save(output_path)
        return {
            "output_path": str(output_path),
            "display_class": display_class,
            "normalization": normalization,
        }

    def _remove_background_with_onnx(
        self,
        *,
        input_path: Path,
        output_path: Path,
        onnx_model_path: Path,
        image,
        original_size: tuple[int, int],
        display_class: str,
    ) -> dict[str, Any]:
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
        cutout, normalization = self.normalize_cutout_frame(cutout, display_class=display_class)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cutout.save(output_path)
        return {
            "output_path": str(output_path),
            "display_class": display_class,
            "normalization": normalization,
        }

    def trim_transparent_canvas(self, image):
        alpha_channel = image.getchannel("A")
        bounding_box = alpha_channel.getbbox()
        if bounding_box is None:
            return image
        return image.crop(bounding_box)

    def infer_display_class(self, *, entity_type: str | None, asset_kind: str | None) -> str | None:
        if asset_kind == "background" or entity_type == "location":
            return "background-scene"
        if entity_type == "character" or asset_kind == "portrait":
            return "character-fullbody"
        if entity_type == "object" or asset_kind == "object_render":
            return "object-featured"
        return None

    def get_asset_display_class(self, asset: dict[str, Any]) -> str | None:
        return asset.get("display_class") or self.infer_display_class(
            entity_type=asset.get("entity_type"),
            asset_kind=asset.get("asset_kind"),
        )

    def normalize_cutout_frame(self, image, *, display_class: str | None):
        from PIL import Image

        trimmed = self.trim_transparent_canvas(image)
        alpha_channel = trimmed.getchannel("A")
        bounding_box = alpha_channel.getbbox()
        if bounding_box is None:
            normalization = {
                "method": "none",
                "display_class": display_class,
                "canvas_size": list(trimmed.size),
                "content_size": [0, 0],
            }
            return trimmed, normalization

        content_width, content_height = trimmed.size

        if display_class == "character-fullbody":
            canvas_size = (1024, 1536)
            target_height = int(canvas_size[1] * 0.9)
            scale = target_height / max(content_height, 1)
            scaled_width = max(1, int(round(content_width * scale)))
            scaled_height = max(1, int(round(content_height * scale)))
            resized = trimmed.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
            x = max(0, (canvas_size[0] - scaled_width) // 2)
            baseline = int(canvas_size[1] * 0.96)
            y = max(0, baseline - scaled_height)
        elif display_class == "object-featured":
            canvas_size = (1024, 1024)
            target_width = int(canvas_size[0] * 0.82)
            target_height = int(canvas_size[1] * 0.62)
            scale = min(target_width / max(content_width, 1), target_height / max(content_height, 1))
            scaled_width = max(1, int(round(content_width * scale)))
            scaled_height = max(1, int(round(content_height * scale)))
            resized = trimmed.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
            x = max(0, (canvas_size[0] - scaled_width) // 2)
            y = max(0, (canvas_size[1] - scaled_height) // 2)
        else:
            canvas_size = trimmed.size
            scaled_width = content_width
            scaled_height = content_height
            resized = trimmed
            canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
            x = 0
            y = 0

        canvas.alpha_composite(resized, (x, y))
        normalization = {
            "method": "standard_frame",
            "display_class": display_class,
            "canvas_size": [canvas_size[0], canvas_size[1]],
            "content_size": [scaled_width, scaled_height],
            "content_offset": [x, y],
            "content_ratio": {
                "width": round(scaled_width / max(canvas_size[0], 1), 4),
                "height": round(scaled_height / max(canvas_size[1], 1), 4),
            },
        }
        return canvas, normalization
