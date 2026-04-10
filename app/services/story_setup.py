from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.models import GenerationCandidate
from app.services.assets import AssetService
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver
from app.services.story_graph import StoryGraphService
from app.services.story_notes import StoryDirectionService
from app.services.worldbuilding import WorldbuildingService


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
        self.story_notes = StoryDirectionService(connection)
        self.worldbuilding = WorldbuildingService(connection)

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

    def hard_reset_story(
        self,
        *,
        branch_key: str = "default",
        ideas_path: Path | None = None,
        story_specific_idea_terms: list[str] | None = None,
    ) -> dict[str, Any]:
        self._clear_story_database()
        reset = self.soft_reset_opening_canon()
        protagonist_id = int(reset["protagonist"]["id"])
        location_id = int(reset["opening_location"]["id"])

        self._retune_opening_canon_for_reboot(
            protagonist_id=protagonist_id,
            location_id=location_id,
        )

        opening_candidate = self._build_reboot_opening_candidate(
            branch_key=branch_key,
            protagonist_id=protagonist_id,
            location_id=location_id,
        )
        opening_node = self.story_graph.create_story_node(
            branch_key=branch_key,
            title=opening_candidate.scene_title,
            scene_text=opening_candidate.scene_text,
            summary=opening_candidate.scene_summary,
            dialogue_lines=[line.model_dump() for line in opening_candidate.dialogue_lines],
            referenced_entities=[reference.model_dump() for reference in opening_candidate.entity_references],
            present_entities=[entity.model_dump(exclude={"use_player_fallback"}) for entity in opening_candidate.scene_present_entities],
        )

        created_choices: list[dict[str, Any]] = []
        for choice in opening_candidate.choices:
            notes_payload = {
                "notes": choice.notes,
                "choice_class": choice.choice_class,
            }
            if choice.ending_category is not None:
                notes_payload["ending_category"] = choice.ending_category
            created_choice = self.story_graph.create_choice(
                from_node_id=int(opening_node["id"]),
                choice_text=choice.choice_text,
                to_node_id=choice.target_node_id,
                status="open" if choice.target_node_id is None else "fulfilled",
                notes=json.dumps(notes_payload),
            )
            created_choices.append(self.story_graph.get_choice(int(created_choice["id"])) or created_choice)

        self.branch_state.add_branch_tag(
            branch_key=branch_key,
            tag="enumerators-closing-in",
            tag_type="state",
            source="story_reset",
            notes="The opening scene begins under imminent survey pressure from the king's brass enumerators.",
        )

        created_hooks = self._create_reboot_hooks(
            branch_key=branch_key,
            protagonist_id=protagonist_id,
        )
        self._create_reboot_story_notes(
            branch_key=branch_key,
            protagonist_id=protagonist_id,
            hooks_by_key=created_hooks,
        )
        self._create_reboot_worldbuilding(branch_key=branch_key)
        restored_assets = self._restore_reboot_assets(
            protagonist_id=protagonist_id,
            location_id=location_id,
        )

        self.branch_state.sync_branch_progress(branch_key, latest_story_node_id=int(opening_node["id"]))

        pruned_ideas = None
        if ideas_path is not None:
            pruned_ideas = self.prune_story_specific_ideas(
                ideas_path=ideas_path,
                story_specific_terms=story_specific_idea_terms or [],
            )

        return {
            "branch_key": branch_key,
            "start_node_id": int(opening_node["id"]),
            "opening_choice_ids": [int(choice["id"]) for choice in created_choices],
            "opening_title": opening_candidate.scene_title,
            "opening_summary": opening_candidate.scene_summary,
            "worldbuilding_note_count": len(self.worldbuilding.list_notes()),
            "story_direction_note_count": len(self.story_notes.list_notes()),
            "hook_count": len(self.branch_state.list_hooks(branch_key)),
            "asset_count": len(self.assets.list_assets()),
            "restored_assets": restored_assets,
            "pruned_ideas": pruned_ideas,
            "counts": self.story_graph.counts(),
        }

    def prune_story_specific_ideas(
        self,
        *,
        ideas_path: Path,
        story_specific_terms: list[str],
    ) -> dict[str, Any]:
        resolved_path = ideas_path.expanduser().resolve()
        if not resolved_path.exists():
            return {
                "path": str(resolved_path),
                "removed_count": 0,
                "removed_entries": [],
            }

        banned_terms = sorted(
            {
                term.strip().lower()
                for term in story_specific_terms
                if term and term.strip()
            },
            key=len,
            reverse=True,
        )
        if not banned_terms:
            return {
                "path": str(resolved_path),
                "removed_count": 0,
                "removed_entries": [],
            }

        kept_lines: list[str] = []
        removed_entries: list[str] = []
        in_open_ideas = False
        for raw_line in resolved_path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("## "):
                in_open_ideas = stripped == "## Open Ideas"
                kept_lines.append(raw_line)
                continue
            if in_open_ideas and stripped.startswith("- ["):
                lowered = stripped.lower()
                if any(term in lowered for term in banned_terms):
                    removed_entries.append(stripped)
                    continue
            kept_lines.append(raw_line)

        normalized_lines: list[str] = []
        blank_streak = 0
        for line in kept_lines:
            if line.strip():
                blank_streak = 0
                normalized_lines.append(line)
                continue
            blank_streak += 1
            if blank_streak <= 1:
                normalized_lines.append("")

        resolved_path.write_text("\n".join(normalized_lines).rstrip() + "\n", encoding="utf-8")
        return {
            "path": str(resolved_path),
            "removed_count": len(removed_entries),
            "removed_entries": removed_entries,
        }

    def _clear_story_database(self) -> None:
        self.connection.commit()
        self.connection.execute("PRAGMA foreign_keys = OFF")
        tables = [
            "generation_jobs",
            "assets",
            "story_node_present_entities",
            "node_entities",
            "choices",
            "story_nodes",
            "inventory_entries",
            "unlocked_affordances",
            "relationship_states",
            "branch_tags",
            "story_hooks",
            "story_direction_notes",
            "worldbuilding_notes",
            "relations",
            "facts",
            "objects",
            "characters",
            "locations",
            "branch_state",
            "loop_runtime_state",
        ]
        for table_name in tables:
            self.connection.execute(f"DELETE FROM {table_name}")
        self.connection.execute("DELETE FROM sqlite_sequence")
        self.connection.execute(
            """
            INSERT INTO loop_runtime_state (id, normal_runs_since_plan, last_run_mode)
            VALUES (1, 0, 'normal')
            """
        )
        self.connection.commit()
        self.connection.execute("PRAGMA foreign_keys = ON")

    def _retune_opening_canon_for_reboot(self, *, protagonist_id: int, location_id: int) -> None:
        self.connection.execute(
            """
            UPDATE locations
            SET description = ?, canonical_summary = ?
            WHERE id = ?
            """,
            (
                "A dew-cold meadow of towering mushrooms above buried green glass, freshly strung with counting wires while brass enumerators sweep the field at dawn.",
                "The opening field: beautiful, damp, and quietly under survey by forces that count bodies, routes, and names.",
                location_id,
            ),
        )
        self.connection.execute(
            """
            UPDATE characters
            SET home_location_id = ?, canonical_summary = ?
            WHERE id = ?
            """,
            (
                location_id,
                "An abnormally tall gnome with five thumbs on the left hand, a striped bucket hat of unknown origin, and the unnerving sense that somebody altered them for a purpose.",
                protagonist_id,
            ),
        )
        self.connection.commit()

    def _build_reboot_opening_candidate(
        self,
        *,
        branch_key: str,
        protagonist_id: int,
        location_id: int,
    ) -> GenerationCandidate:
        return GenerationCandidate.model_validate(
            {
                "branch_key": branch_key,
                "scene_title": "Before the Counting Bell",
                "scene_summary": (
                    "The Tall Gnome wakes in the Mushroom Field just before a brass survey patrol crosses it, "
                    "with counting wires underfoot, a bucket hat somebody left for him, and a green glass seam "
                    "in the soil that stirs uneasily at the curse in his altered hand."
                ),
                "scene_text": (
                    "You wake belly-down in wet blue grass beneath mushrooms tall enough to hide cottages. "
                    "Between the stalks, thin black counting wires have been strung sometime in the night, and "
                    "every quiet hum through them makes your five left thumbs twitch in sequence.\n\n"
                    "Your striped bucket hat is cold with dew and warm at the brim. It feels like an ordinary hat "
                    "that belonged in a real life before this one, except for the tiny stitches hidden inside the "
                    "band and the place where somebody burned through a proper name. Each time the wires sing, you "
                    "get the stubborn feeling that whoever left the hat on you meant it to guide you.\n\n"
                    "Far away, a brass bell rings seven clipped notes. You do not know why that sound fills you "
                    "with the certainty that something is coming to count you, misname you, and carry you off in "
                    "paperwork. From deeper in the mist, you hear one heavier metallic step where no wagon should be. "
                    "Between two mushroom roots, a green line of buried glass has surfaced through the mud like a "
                    "window trying to breathe.\n\n"
                    "If the field is being surveyed at dawn, you have very little time to decide whether to climb, "
                    "descend, or hold onto the few strange clues somebody left behind before the field decides for you."
                ),
                "dialogue_lines": [
                    {
                        "speaker": "Narrator",
                        "text": "You wake belly-down in wet blue grass beneath mushrooms tall enough to hide cottages, while thin black counting wires hum low between the stalks.",
                    },
                    {
                        "speaker": "Narrator",
                        "text": "Every pulse through the wires makes your five left thumbs twitch in sequence, as though your hand already knows the pattern being counted.",
                    },
                    {
                        "speaker": "Narrator",
                        "text": "Inside the striped bucket hat, somebody has stitched small guiding marks and burned through the place where a proper name ought to be.",
                    },
                    {
                        "speaker": "You",
                        "text": "I do not know who dressed me for this, but they expected me to move before sunrise.",
                    },
                    {
                        "speaker": "Narrator",
                        "text": "A brass bell rings seven clipped notes somewhere beyond the mist, and a green seam of buried glass pushes up through the mud between two mushroom roots like a hidden window beginning to open.",
                    },
                ],
                "choices": [
                    {
                        "choice_text": "Trace the counting-wires to the green glass seam under the roots",
                        "notes": "Goal: find where the wires lead before the patrol reaches the field. Intent: open the buried-glass storyline and let the field reveal one more strange rule before the larger mystery rushes in.",
                        "choice_class": "progress",
                    },
                    {
                        "choice_text": "Search the striped hat for a clue about who left it on you",
                        "notes": "Goal: find a personal trace of whoever left the hat on you before dawn. Intent: let the hat become the first backstory breadcrumb without forcing larger answers too early.",
                        "choice_class": "inspection",
                    },
                    {
                        "choice_text": "Climb the tallest mushroom and watch what the survey patrol is dragging through the mist",
                        "notes": "Goal: confirm what immediate danger is moving through the field. Intent: bring outside pressure onstage early and maybe glimpse one strange machine or patrol detail without explaining the whole power behind it yet.",
                        "choice_class": "progress",
                    },
                ],
                "entity_references": [
                    {"entity_type": "location", "entity_id": location_id, "role": "current_scene"},
                    {"entity_type": "character", "entity_id": protagonist_id, "role": "player"},
                ],
                "scene_present_entities": [
                    {
                        "entity_type": "character",
                        "entity_id": protagonist_id,
                        "slot": "hero-center",
                        "focus": True,
                    }
                ],
            }
        )

    def _create_reboot_hooks(self, *, branch_key: str, protagonist_id: int) -> dict[str, dict[str, Any]]:
        hook_specs = {
            "hat": {
                "hook_type": "identity_mystery",
                "importance": "major",
                "summary": (
                    "The striped bucket hat, the missing first name, the amnesia, and waking in the Mushroom Field "
                    "all point to the same hidden past event, and the hat was likely left by someone trying to guide or protect him."
                ),
                "payoff_concept": (
                    "The hat came from a friend, ally, or protector in the protagonist's missing past, and clues around it "
                    "should help recover the personal history behind the same event that caused the amnesia, the field arrival, "
                    "and the broader tightening pressure gathering beyond the field."
                ),
                "must_not_imply": [
                    "Do not reduce the hat to impersonal route hardware, ordinary uniform gear, or a one-scene magical gadget.",
                    "Do not let the first nearby NPC fully explain who gave the hat or why it was left with him.",
                ],
                "linked_entity_type": "character",
                "linked_entity_id": protagonist_id,
                "introduced_at_depth": 0,
                "min_distance_to_payoff": 20,
                "min_distance_to_next_development": 6,
                "required_clue_tags": [],
                "required_state_tags": [],
                "status": "active",
                "notes": (
                    "Long-range major hook. Early scenes may let the hat produce one or two personal breadcrumbs, but "
                    "they should leave plenty of room for unrelated local arcs before the shared past event comes into focus."
                ),
            },
            "hand": {
                "hook_type": "body_mystery",
                "importance": "major",
                "summary": (
                    "The five-thumbed hand and stretched gnome body are the aftermath of a deliberate curse, mutilation, "
                    "or hostile alteration tied to the same hidden event that erased his memory and left him in the field."
                ),
                "payoff_concept": (
                    "A foe, state agent, or enemy force altered the protagonist on purpose, and that injury now entangles "
                    "him with the tightening survey apparatus, its enumerators, and its stranger metal walkers. Systems may react to "
                    "the curse, but the hand is not a gift or privileged access token."
                ),
                "must_not_imply": [
                    "Do not explain the body alteration as a random mutation, harmless oddity, or beneficial upgrade.",
                    "Do not treat the five thumbs as a privileged passkey whose main purpose is opening local locks.",
                ],
                "linked_entity_type": "character",
                "linked_entity_id": protagonist_id,
                "introduced_at_depth": 0,
                "min_distance_to_payoff": 20,
                "min_distance_to_next_development": 6,
                "required_clue_tags": [],
                "required_state_tags": [],
                "status": "active",
                "notes": (
                    "Long-range major hook. Early scenes may show unease, pain, or suspicious reactions around the altered body, "
                    "but they should not hurry into naming the culprit or explaining the whole curse."
                ),
            },
        }

        created_hooks: dict[str, dict[str, Any]] = {}
        for key, hook in hook_specs.items():
            created_hooks[key] = self.branch_state.create_hook(branch_key=branch_key, **hook)
        return created_hooks

    def _create_reboot_story_notes(
        self,
        *,
        branch_key: str,
        protagonist_id: int,
        hooks_by_key: dict[str, dict[str, Any]],
    ) -> None:
        notes = [
            {
                "note_type": "plotline",
                "title": "One Past Event Exists, But It Can Wait",
                "note_text": (
                    "Keep linking the bucket hat, the five-thumb curse, the amnesia, and waking in the Mushroom Field "
                    "to the same hidden past event. Let the hat provide the first personal breadcrumb, but allow plenty of unrelated "
                    "detours, local arcs, and present-tense trouble before the deeper backstory starts opening up."
                ),
                "priority": 5,
                "related_entity_type": "character",
                "related_entity_id": protagonist_id,
                "related_hook_id": hooks_by_key["hat"]["id"],
            },
            {
                "note_type": "plotline",
                "title": "Strange Brass Forces Before Named Rulers",
                "note_text": (
                    "Introduce brass survey pressure and odd mechanical patrols gradually. Early scenes may show one strange walker, "
                    "one alarming inspection, or one unnerving rumor, but should not hurry into naming the ruler behind it or explaining the whole regime too soon."
                ),
                "priority": 5,
                "related_entity_type": "character",
                "related_entity_id": protagonist_id,
                "related_hook_id": hooks_by_key["hand"]["id"],
            },
        ]
        for note in notes:
            self.story_notes.create_note(
                **note,
                status="active",
                source_branch_key=branch_key,
                created_by="story_reset",
            )

    def _create_reboot_worldbuilding(self, *, branch_key: str) -> None:
        notes = [
            {
                "note_type": "regime_pressure",
                "title": "Brass Survey Teams at Dawn",
                "note_text": (
                    "Brass enumerators and survey engines have begun sweeping outlying districts at dawn. A few stranger metal walkers have appeared with them, but common people do not yet agree on what they are or who exactly commands them. Anything miscounted, unnamed, or physically irregular risks being claimed as an administrative error and removed."
                ),
                "priority": 5,
                "pressure": 5,
            },
            {
                "note_type": "hidden_infrastructure",
                "title": "Glass Villages Beneath the Field",
                "note_text": (
                    "Old green-glass settlements and sealed registry passages still run beneath certain mushroom fields. "
                    "Their windows, lifts, and listening seams only surface when specific routes or bodies are recognized."
                ),
                "priority": 4,
                "pressure": 4,
            },
            {
                "note_type": "smuggling_network",
                "title": "Names Travel Better Than Bodies",
                "note_text": (
                    "There are covert routes for moving names, permits, and identities separately from the people who own them. "
                    "Courier marks, hats, ribbons, and papers can outlast the memories of their carriers, especially when allies are trying to move someone ahead of the new counting machines."
                ),
                "priority": 4,
                "pressure": 4,
            },
            {
                "note_type": "ambient_danger",
                "title": "Survey Weather Makes Bad Choices Permanent",
                "note_text": (
                    "When the counting bells ring under bad weather, routes close hard and official mistakes become difficult to reverse. "
                    "A branch can die from delay, capture, or bad timing just as easily as from violence."
                ),
                "priority": 3,
                "pressure": 3,
            },
        ]
        for note in notes:
            self.worldbuilding.create_note(
                **note,
                status="active",
                source_branch_key=branch_key,
                created_by="story_reset",
            )

    def _restore_reboot_assets(self, *, protagonist_id: int, location_id: int) -> list[dict[str, Any]]:
        asset_specs = [
            {
                "entity_type": "location",
                "entity_id": location_id,
                "asset_kind": "background",
                "file_path": self.project_root / "data" / "assets" / "generated" / "background" / "location_1_mushroom-field-opening.png",
                "display_class": "background-scene",
            },
            {
                "entity_type": "character",
                "entity_id": protagonist_id,
                "asset_kind": "portrait",
                "file_path": self.project_root / "data" / "assets" / "comfy_output" / "portrait" / "main-character-no-cutout.png",
                "display_class": "character-fullbody",
            },
            {
                "entity_type": "character",
                "entity_id": protagonist_id,
                "asset_kind": "cutout",
                "file_path": self.project_root / "data" / "assets" / "cutouts" / "main-character-bucket-hat-normalized-cutout.png",
                "display_class": "character-fullbody",
            },
        ]
        restored_assets: list[dict[str, Any]] = []
        for asset in asset_specs:
            resolved_path = Path(asset["file_path"]).expanduser().resolve()
            if not resolved_path.exists():
                continue
            restored_assets.append(
                self.assets.add_asset(
                    entity_type=asset["entity_type"],
                    entity_id=asset["entity_id"],
                    asset_kind=asset["asset_kind"],
                    file_path=str(resolved_path),
                    display_class=asset["display_class"],
                    normalization={},
                    prompt_text=json.dumps(
                        {
                            "source": "hard_reset_story",
                            "notes": "Re-registered opening asset after wiping the live continuity database.",
                        }
                    ),
                )
            )
        return restored_assets

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
        existing_by_summary = {
            hook["summary"].strip().lower(): hook
            for hook in existing_hooks
        }
        desired_hooks = [
            {
                "hook_type": "identity_mystery",
                "importance": "major",
                "summary": (
                    "The striped bucket hat, the lost first name, the amnesia, and waking in the Mushroom Field "
                    "all point to the same hidden past event, and the hat was likely left by someone trying to guide or protect him."
                ),
                "payoff_concept": (
                    "The bucket hat came from a friend, ally, or protector in the protagonist's missing past, and clues around it should help recover the same hidden event that caused the amnesia, the field arrival, and the wider tightening pressure gathering beyond the field."
                ),
                "must_not_imply": [
                    "Do not reduce the bucket hat to impersonal route hardware, ordinary tram uniform gear, or platform-issued clothing.",
                    "Do not fully reveal who gave the hat or why it was left with him until much later.",
                ],
                "linked_entity_type": "character",
                "linked_entity_id": protagonist_id,
                "introduced_at_depth": 0,
                "min_distance_to_payoff": 20,
                "min_distance_to_next_development": 6,
                "required_clue_tags": [],
                "required_state_tags": [],
                "status": "active",
                "notes": (
                    "This is a long-range major hook. Let the hat provide only sparse early breadcrumbs and leave room for unrelated arcs before the deeper past starts opening."
                ),
            },
            {
                "hook_type": "body_mystery",
                "importance": "major",
                "summary": (
                    "The five-thumbed left hand and the protagonist's altered body suggest a deliberate curse, "
                    "mutilation, or hostile alteration tied to the same hidden event."
                ),
                "payoff_concept": (
                    "The altered hand and stretched body are the result of deliberate intervention tied to the protagonist's missing past and later entangled with the tightening survey apparatus; they are not a random fantasy oddity or privileged access token."
                ),
                "must_not_imply": [
                    "Do not explain the altered body as a simple local platform side effect, harmless oddity, or beneficial upgrade.",
                    "Do not treat the five thumbs as a joke mutation or privileged passkey with no larger meaning.",
                ],
                "linked_entity_type": "character",
                "linked_entity_id": protagonist_id,
                "introduced_at_depth": 0,
                "min_distance_to_payoff": 20,
                "min_distance_to_next_development": 6,
                "required_clue_tags": [],
                "required_state_tags": [],
                "status": "active",
                "notes": (
                    "This is a long-range major hook tied to the protagonist's body and should stay uneasy and partially unexplained through the opening stretch."
                ),
            },
        ]
        for hook in desired_hooks:
            existing = existing_by_summary.get(hook["summary"].strip().lower())
            if existing is None:
                existing = next(
                    (
                        candidate
                        for candidate in existing_hooks
                        if candidate["hook_type"] == hook["hook_type"]
                        and candidate["importance"] == hook["importance"]
                        and (
                            (
                                candidate.get("linked_entity_type") == hook["linked_entity_type"]
                                and int(candidate.get("linked_entity_id") or 0) == int(hook["linked_entity_id"])
                            )
                            or (
                                candidate.get("linked_entity_type") in {None, "", "null"}
                                and candidate.get("linked_entity_id") in {None, 0, "0"}
                            )
                        )
                    ),
                    None,
                )
            if existing is None:
                self.branch_state.create_hook(branch_key=branch_key, **hook)
                continue
            self.connection.execute(
                """
                UPDATE story_hooks
                SET hook_type = ?,
                    importance = ?,
                    payoff_concept = ?,
                    must_not_imply_json = ?,
                    linked_entity_type = ?,
                    linked_entity_id = ?,
                    introduced_at_depth = ?,
                    min_distance_to_payoff = ?,
                    min_distance_to_next_development = ?,
                    last_development_depth = ?,
                    required_clue_tags_json = ?,
                    required_state_tags_json = ?,
                    status = ?,
                    notes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    hook["hook_type"],
                    hook["importance"],
                    hook["payoff_concept"],
                    json.dumps(hook["must_not_imply"]),
                    hook["linked_entity_type"],
                    hook["linked_entity_id"],
                    hook["introduced_at_depth"],
                    hook["min_distance_to_payoff"],
                    hook["min_distance_to_next_development"],
                    hook["introduced_at_depth"],
                    json.dumps(hook["required_clue_tags"]),
                    json.dumps(hook["required_state_tags"]),
                    hook["status"],
                    hook["notes"],
                    existing["id"],
                ),
            )
        self.connection.commit()
