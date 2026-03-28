from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    database_path: Path
    comfyui_base_url: str
    comfyui_workflow_dir: Path
    comfyui_output_dir: Path

    @classmethod
    def from_env(cls, override_path: str | Path | None = None) -> "Settings":
        project_root = Path(__file__).resolve().parent.parent
        if override_path is not None:
            path = Path(override_path)
        else:
            env_value = os.getenv("CYOA_DB_PATH")
            if env_value:
                path = Path(env_value)
            else:
                path = project_root / "data" / "cyoa_world.db"
        comfyui_base_url = os.getenv("COMFYUI_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        workflow_dir = Path(os.getenv("COMFYUI_WORKFLOW_DIR", project_root / "workflows" / "comfyui"))
        output_dir = Path(os.getenv("COMFYUI_OUTPUT_DIR", project_root / "data" / "assets" / "comfy_output"))
        return cls(
            database_path=path,
            comfyui_base_url=comfyui_base_url,
            comfyui_workflow_dir=workflow_dir,
            comfyui_output_dir=output_dir,
        )
