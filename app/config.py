from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    database_path: Path

    @classmethod
    def from_env(cls, override_path: str | Path | None = None) -> "Settings":
        if override_path is not None:
            path = Path(override_path)
        else:
            env_value = os.getenv("CYOA_DB_PATH")
            if env_value:
                path = Path(env_value)
            else:
                project_root = Path(__file__).resolve().parent.parent
                path = project_root / "data" / "cyoa_world.db"
        return cls(database_path=path)

