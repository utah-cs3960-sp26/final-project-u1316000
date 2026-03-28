from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.services.assets import AssetService
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver
from app.services.story_graph import StoryGraphService


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
        self.story_graph = StoryGraphService(connection)

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

    def seed_opening_story(self, branch_key: str = "default") -> dict[str, Any]:
        existing_nodes = [
            node for node in self.story_graph.list_story_nodes()
            if node["branch_key"] == branch_key
        ]
        reset = self.soft_reset_opening_canon()
        protagonist_id = int(reset["protagonist"]["id"])
        location_id = int(reset["opening_location"]["id"])
        if existing_nodes:
            start_node = min(existing_nodes, key=lambda node: int(node["id"]))
            self._ensure_opening_hooks(
                branch_key=branch_key,
                protagonist_id=protagonist_id,
                location_id=location_id,
            )
            self.branch_state.sync_branch_progress(branch_key, latest_story_node_id=int(existing_nodes[-1]["id"]))
            return {
                "branch_key": branch_key,
                "start_node_id": int(start_node["id"]),
                "nodes_created": 0,
                "existing": True,
            }

        hero_present = [
            {
                "entity_type": "character",
                "entity_id": protagonist_id,
                "slot": "hero-center",
                "focus": True,
            }
        ]
        shared_refs = [
            {"entity_type": "location", "entity_id": location_id, "role": "current_scene"},
            {"entity_type": "character", "entity_id": protagonist_id, "role": "player"},
        ]

        opening = self.story_graph.create_story_node(
            branch_key=branch_key,
            title="The Tall Gnome Awakens",
            summary="The tall gnome wakes in the Mushroom Field, missing memories and noticing several impossible clues.",
            scene_text="The Tall Gnome wakes in the Mushroom Field with a bucket hat, five thumbs, and no memory of how he arrived.",
            dialogue_lines=[
                {"speaker": "Narrator", "text": "Cold dew clings to your coat as you wake in a field of larger-than-life mushrooms, their pale caps towering overhead like quiet moons hung on crooked stems."},
                {"speaker": "You", "text": "You push yourself upright and discover your body is wrong in a very specific way: you are still unmistakably a gnome, but stretched to the size of a human."},
                {"speaker": "Narrator", "text": "The memory of how you arrived here refuses to surface. There is only a raw blankness behind your eyes, like a torn page where a name should be."},
                {"speaker": "Narrator", "text": "Your normal hat is gone. In its place sits a red-and-white striped bucket hat, absurdly jaunty and deeply wrong, as if someone dressed you for a joke you cannot remember."},
                {"speaker": "Narrator", "text": "You lift your left hand toward the dawn and freeze. Five thumbs stare back at you, flexing with eerie coordination, as if they have always belonged there."},
                {"speaker": "Narrator", "text": "Beyond the mushroom trunks, something metallic taps three careful beats. In the grass nearby, silver tracks, your strange hat, and your impossible hand all seem to demand attention at once."},
            ],
            referenced_entities=shared_refs,
            present_entities=hero_present,
        )

        tracks = self.story_graph.create_story_node(
            branch_key=branch_key,
            title="Silver Tracks",
            summary="The strange grooves in the grass suggest a tiny vehicle and a deliberate sign.",
            scene_text="The silver tracks behave more like cart-grooves than footprints, and they end beneath a towering mushroom marked with velvet.",
            dialogue_lines=[
                {"speaker": "Narrator", "text": "You kneel beside the silver tracks and find they are not footprints at all, but two narrow grooves pressed into the earth as though a tiny carriage had rolled through the field with no horse to pull it."},
                {"speaker": "Narrator", "text": "The grooves stop beneath the largest mushroom in sight, where someone has tied a strip of velvet to the stem at exactly your eye level."},
                {"speaker": "You", "text": "You do not remember leaving it there, but the knot is one your hands know how to tie."},
            ],
            parent_node_id=int(opening["id"]),
            referenced_entities=shared_refs,
            present_entities=hero_present,
        )
        hat = self.story_graph.create_story_node(
            branch_key=branch_key,
            title="The Bucket Hat",
            summary="The new hat contains a warning and a mirror that behaves slightly wrong.",
            scene_text="The bucket hat fits too well, and the objects hidden in it seem to know more about you than you do.",
            dialogue_lines=[
                {"speaker": "Narrator", "text": "The bucket hat fits too well. Inside the brim, tiny letters have been stitched through the inner seam: NOT YOUR FIRST NAME."},
                {"speaker": "Narrator", "text": "Tucked in the band is a pressed violet and a sliver of mirror. When you angle the mirror just right, a second version of your face seems to blink a fraction too late."},
                {"speaker": "You", "text": "Whoever replaced your old hat knew exactly what would frighten you and exactly what would make you keep walking."},
            ],
            parent_node_id=int(opening["id"]),
            referenced_entities=shared_refs,
            present_entities=hero_present,
        )
        hand = self.story_graph.create_story_node(
            branch_key=branch_key,
            title="Five Thumbs",
            summary="The hand pattern suggests a mechanism or lock tied to your altered body.",
            scene_text="The five thumbs move in rhythm, and a stone plate in the soil seems shaped to receive them.",
            dialogue_lines=[
                {"speaker": "Narrator", "text": "You spread your left hand and watch the five thumbs curl inward in sequence, each one stopping as if it were matching a rhythm your head can almost hear."},
                {"speaker": "Narrator", "text": "In the wet soil, you find a half-buried stone plate shaped with five thumb-sized hollows. It looks less like a warning than a lock waiting for you to remember the key."},
                {"speaker": "You", "text": "Whatever happened to you, it did not happen by accident."},
            ],
            parent_node_id=int(opening["id"]),
            referenced_entities=shared_refs,
            present_entities=hero_present,
        )

        self.story_graph.create_choice(
            from_node_id=int(opening["id"]),
            choice_text="Examine the silver tracks in the grass",
            to_node_id=int(tracks["id"]),
        )
        self.story_graph.create_choice(
            from_node_id=int(opening["id"]),
            choice_text="Inspect the bucket hat and its stitched warning",
            to_node_id=int(hat["id"]),
        )
        self.story_graph.create_choice(
            from_node_id=int(opening["id"]),
            choice_text="Study your five-thumbed left hand",
            to_node_id=int(hand["id"]),
        )

        self._create_open_frontier_choices(int(tracks["id"]), [
            "Follow the grooves beneath the velvet-marked mushroom",
            "Pocket the velvet strip and listen for the tapping again",
            "Call into the mist and ask who set the knot",
        ])
        self._create_open_frontier_choices(int(hat["id"]), [
            "Study the late-blinking mirror more closely",
            "Trace the stitched warning around the brim",
            "Wear the bucket hat properly and listen for a response",
        ])
        self._create_open_frontier_choices(int(hand["id"]), [
            "Fit your hand against the stone plate",
            "Count the thumb hollows twice before touching them",
            "Step back and watch whether the plate reacts on its own",
        ])

        latest_node_id = max(int(tracks["id"]), int(hat["id"]), int(hand["id"]))
        self._ensure_opening_hooks(
            branch_key=branch_key,
            protagonist_id=protagonist_id,
            location_id=location_id,
        )
        self.branch_state.sync_branch_progress(branch_key, latest_story_node_id=latest_node_id)
        return {
            "branch_key": branch_key,
            "start_node_id": int(opening["id"]),
            "nodes_created": 4,
            "existing": False,
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
            display_class="character-fullbody",
            prompt_text=json.dumps(
                {
                    "source": "story_reset",
                    "notes": "Preferred protagonist portrait after bucket-hat soft reset.",
                }
            ),
        )
        cutout_result = self.assets.remove_background(
            source_image_path=str(resolved_source),
            output_name="main-character-bucket-hat-cutout.png",
            entity_type="character",
            asset_kind="portrait",
        )
        cutout_asset = self.assets.add_asset(
            entity_type="character",
            entity_id=int(protagonist["id"]),
            asset_kind="cutout",
            file_path=cutout_result["output_path"],
            display_class=cutout_result["display_class"],
            normalization=cutout_result["normalization"],
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

    def _create_open_frontier_choices(self, from_node_id: int, labels: list[str]) -> None:
        for label in labels:
            self.story_graph.create_choice(
                from_node_id=from_node_id,
                choice_text=label,
                to_node_id=None,
                status="open",
            )

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

    def _ensure_opening_hooks(self, *, branch_key: str, protagonist_id: int, location_id: int) -> None:
        existing_hooks = self.branch_state.list_hooks(branch_key)
        existing_summaries = {hook["summary"].strip().lower() for hook in existing_hooks}
        desired_hooks = [
            {
                "hook_type": "identity_mystery",
                "importance": "major",
                "summary": (
                    "The striped bucket hat, the lost first name, the amnesia, and waking in the Mushroom Field "
                    "all point to the same hidden past event."
                ),
                "linked_entity_type": "character",
                "linked_entity_id": protagonist_id,
                "introduced_at_depth": 0,
                "min_distance_to_payoff": 20,
                "required_clue_tags": [],
                "required_state_tags": [],
                "status": "active",
                "notes": (
                    "This is a long-range major hook. Do not pay it off early just because a later clue feels tempting."
                ),
            },
            {
                "hook_type": "body_mystery",
                "importance": "major",
                "summary": (
                    "The five-thumbed left hand and the protagonist's altered body suggest deliberate transformation, "
                    "tampering, or design."
                ),
                "linked_entity_type": "character",
                "linked_entity_id": protagonist_id,
                "introduced_at_depth": 0,
                "min_distance_to_payoff": 20,
                "required_clue_tags": [],
                "required_state_tags": [],
                "status": "active",
                "notes": (
                    "This is a long-range major hook tied to the protagonist's body and should not be solved in the opening stretch."
                ),
            },
        ]
        for hook in desired_hooks:
            if hook["summary"].strip().lower() in existing_summaries:
                continue
            self.branch_state.create_hook(branch_key=branch_key, **hook)
