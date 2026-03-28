from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.services.assets import AssetService
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver


class StorySetupService:
    """Seeds the resettable opening canon and refreshes protagonist-facing assets."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        project_root: Path,
        story_bible: dict[str, Any],
    ) -> None:
        self.connection = connection
        self.project_root = project_root
        self.story_bible = story_bible
        self.canon = CanonResolver(connection)
        self.assets = AssetService(connection, project_root)
        self.branch_state = BranchStateService(connection, story_bible["acts"])

    def soft_reset_opening_canon(self) -> dict[str, Any]:
        protagonist = self.story_bible["protagonist"]
        title = protagonist["name"]
        protagonist_summary = protagonist["summary"]
        location_description = (
            "A misty field of towering mushrooms where the story opens, half serene and half deeply suspicious."
        )
        location_summary = (
            "The whimsical opening field where the tall gnome wakes among giant mushrooms and uncanny clues."
        )

        opening_location = self.canon.create_or_get_location(
            name="Mushroom Field",
            description=location_description,
            canonical_summary=location_summary,
        )
        self.connection.execute(
            """
            UPDATE locations
            SET description = ?, canonical_summary = ?
            WHERE id = ?
            """,
            (location_description, location_summary, int(opening_location["id"])),
        )
        self.connection.commit()
        opening_location = self.canon.get_location(int(opening_location["id"])) or opening_location

        protagonist_record = self.canon.create_or_get_character(
            name=title,
            description=protagonist_summary,
            canonical_summary=(
                "An abnormally tall gnome with five thumbs on the left hand, a red-and-white striped bucket hat, "
                "and a dangerous gap in memory."
            ),
        )
        protagonist_canonical_summary = (
            "An abnormally tall gnome with five thumbs on the left hand, a red-and-white striped bucket hat, "
            "and a dangerous gap in memory."
        )
        self.connection.execute(
            """
            UPDATE characters
            SET description = ?, canonical_summary = ?
            WHERE id = ?
            """,
            (protagonist_summary, protagonist_canonical_summary, int(protagonist_record["id"])),
        )
        self.connection.commit()
        protagonist_record = self.canon.get_character(int(protagonist_record["id"])) or protagonist_record

        for fact_text in protagonist.get("locked_facts", []):
            self._ensure_locked_fact(
                entity_type="character",
                entity_id=int(protagonist_record["id"]),
                fact_text=fact_text,
                source="story_reset",
            )
        self._ensure_locked_fact(
            entity_type="world",
            entity_id=0,
            fact_text=(
                "The world is whimsical, surreal, and sincere; bizarre things are real and consequential, not punchlines."
            ),
            source="story_reset",
        )

        default_branch = self.branch_state.ensure_branch("default")
        return {
            "story_bible_title": self.story_bible["title"],
            "opening_location": opening_location,
            "protagonist": protagonist_record,
            "default_branch": default_branch,
        }

    def refresh_protagonist_assets(self, source_image_path: str | Path | None = None) -> dict[str, Any]:
        protagonist = self.canon.find_character_by_name(self.story_bible["protagonist"]["name"])
        if protagonist is None:
            protagonist = self.soft_reset_opening_canon()["protagonist"]

        resolved_source = Path(
            source_image_path
            or self.project_root / "data" / "assets" / "comfy_output" / "portrait" / "main-character-no-cutout.png"
        ).expanduser().resolve()
        if not resolved_source.exists():
            raise FileNotFoundError(f"Protagonist portrait source image does not exist: {resolved_source}")

        portrait_asset = self.assets.add_asset(
            entity_type="character",
            entity_id=int(protagonist["id"]),
            asset_kind="portrait",
            file_path=str(resolved_source),
            prompt_text=json.dumps(
                {
                    "source": "story_reset",
                    "notes": "Preferred protagonist portrait after bucket-hat soft reset.",
                }
            ),
        )
        cutout_path = self.assets.remove_background(
            source_image_path=str(resolved_source),
            output_name="main-character-bucket-hat-cutout.png",
        )
        cutout_asset = self.assets.add_asset(
            entity_type="character",
            entity_id=int(protagonist["id"]),
            asset_kind="cutout",
            file_path=cutout_path,
            prompt_text=json.dumps(
                {
                    "source_asset_id": portrait_asset["id"],
                    "source": "story_reset",
                    "notes": "Preferred protagonist cutout after bucket-hat soft reset.",
                }
            ),
        )
        return {
            "protagonist": protagonist,
            "portrait_asset": portrait_asset,
            "cutout_asset": cutout_asset,
        }

    def _ensure_locked_fact(self, *, entity_type: str, entity_id: int, fact_text: str, source: str) -> None:
        normalized = fact_text.strip().lower()
        for fact in self.canon.list_facts():
            if (
                fact["entity_type"] == entity_type
                and int(fact["entity_id"]) == entity_id
                and fact["fact_text"].strip().lower() == normalized
            ):
                return
        self.canon.add_fact(
            entity_type=entity_type,
            entity_id=entity_id,
            fact_text=fact_text,
            is_locked=True,
            source=source,
        )
