from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sqlite3
from typing import Any

from app.database import fetch_all, fetch_one
from app.models import ChoiceReplace, GenerationCandidate
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver
from app.services.story_notes import StoryDirectionService


class StoryGraphService:
    """Owns story nodes, choices, and their links to canonical entities."""

    SAME_LOCATION_PRESSURE_THRESHOLD = 4
    ISOLATION_PRESSURE_THRESHOLD = 6
    NEW_CHARACTER_PRESSURE_THRESHOLD = 6

    NONVISUAL_SPEAKER_PATTERNS = (
        re.compile(r"\bunseen\b", re.IGNORECASE),
        re.compile(r"\bunknown\b", re.IGNORECASE),
        re.compile(r"\bmysterious\b", re.IGNORECASE),
        re.compile(r"\(o\.s\.\)", re.IGNORECASE),
        re.compile(r"\boffscreen\b", re.IGNORECASE),
        re.compile(r"\bover (the )?(radio|speaker|intercom)\b", re.IGNORECASE),
    )
    AUTO_STAGE_CHARACTER_SLOTS = ("left-support", "right-support")
    CHOICE_NOTES_PATTERN = re.compile(
        r"(?:goal\s*:\s*(?P<goal>.+?)\s+intent\s*:\s*(?P<intent>.+))|"
        r"(?:next_node\s*:\s*(?P<next_node>.+?)\s+further_goals\s*:\s*(?P<further_goals>.+))",
        re.IGNORECASE | re.DOTALL,
    )
    INSPECT_ACTION_PATTERN = re.compile(r"\b(look|listen|inspect|examine|read|study|watch|judge)\b", re.IGNORECASE)
    FOLLOW_ACTION_PATTERN = re.compile(r"\b(follow|trace|descend|deeper)\b", re.IGNORECASE)
    TOUCH_ACTION_PATTERN = re.compile(r"\b(touch|press|grip|hold)\b", re.IGNORECASE)
    STEP_BACK_ACTION_PATTERN = re.compile(r"\b(step back|turn back|back away|wait|let .* watch|observe)\b", re.IGNORECASE)
    SOCIAL_ACTION_PATTERN = re.compile(r"\b(ask|speak|call|answer|tell|bargain|warn|hide from|follow them|join)\b", re.IGNORECASE)
    LOCATION_TRANSITION_PATTERN = re.compile(r"\b(board|ride|enter|arrive|reach|head to|go to|return to|climb|cross|step into)\b", re.IGNORECASE)
    _CHOICE_BINDING_UNSET = object()

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def list_story_nodes(self) -> list[dict[str, Any]]:
        nodes = fetch_all(self.connection, "SELECT * FROM story_nodes ORDER BY id")
        for node in nodes:
            node["dialogue_lines"] = self._decode_dialogue_lines(node)
            node["choices"] = fetch_all(
                self.connection,
                """
                SELECT * FROM choices
                WHERE from_node_id = ?
                ORDER BY id
                """,
                (node["id"],),
            )
            self._decode_choice_rows(node["choices"])
            node["present_entities"] = self._list_present_entities(node["id"])
            node["entities"] = fetch_all(
                self.connection,
                """
                SELECT entity_type, entity_id, role
                FROM node_entities
                WHERE story_node_id = ?
                ORDER BY id
                """,
                (node["id"],),
            )
        return nodes

    def list_choices(self) -> list[dict[str, Any]]:
        choices = fetch_all(self.connection, "SELECT * FROM choices ORDER BY id")
        self._decode_choice_rows(choices)
        return choices

    def count_open_choices(self, *, branch_key: str | None = None) -> int:
        query = """
            SELECT COUNT(*) AS count
            FROM choices c
            JOIN story_nodes sn ON sn.id = c.from_node_id
            WHERE c.to_node_id IS NULL AND c.status = 'open'
        """
        params: list[Any] = []
        if branch_key is not None:
            query += " AND sn.branch_key = ?"
            params.append(branch_key)
        row = fetch_one(self.connection, query, tuple(params))
        return int(row["count"]) if row else 0

    def count_total_choices_for_node(self, node_id: int) -> int:
        row = fetch_one(
            self.connection,
            "SELECT COUNT(*) AS count FROM choices WHERE from_node_id = ?",
            (node_id,),
        )
        return int(row["count"]) if row else 0

    def count_open_choices_for_node(self, node_id: int) -> int:
        row = fetch_one(
            self.connection,
            "SELECT COUNT(*) AS count FROM choices WHERE from_node_id = ? AND to_node_id IS NULL AND status = 'open'",
            (node_id,),
        )
        return int(row["count"]) if row else 0

    def build_frontier_budget_state(
        self,
        *,
        branch_key: str | None = None,
        branching_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        budget = (branching_policy or {}).get("frontier_budget") or {}
        soft_limit = int(budget.get("soft_open_choice_limit", 48))
        hard_limit = int(budget.get("hard_open_choice_limit", 72))
        open_choice_count = self.count_open_choices(branch_key=branch_key)
        if open_choice_count >= hard_limit:
            pressure = "hard"
            reason = "Open frontier is above the hard limit. Fresh branching should be exceptional."
        elif open_choice_count >= soft_limit:
            pressure = "soft"
            reason = "Open frontier is above the soft limit. Prefer merges, closures, and narrow continuation."
        else:
            pressure = "normal"
            reason = "Open frontier is within budget."
        return {
            "open_choice_count": open_choice_count,
            "soft_open_choice_limit": soft_limit,
            "hard_open_choice_limit": hard_limit,
            "pressure_level": pressure,
            "within_budget": pressure == "normal",
            "reason": reason,
        }

    def get_branch_start_node(self, branch_key: str) -> dict[str, Any] | None:
        return fetch_one(
            self.connection,
            "SELECT * FROM story_nodes WHERE branch_key = ? ORDER BY id ASC LIMIT 1",
            (branch_key,),
        )

    def get_branch_player_story(self, branch_key: str = "default") -> dict[str, Any]:
        nodes = fetch_all(
            self.connection,
            "SELECT * FROM story_nodes WHERE branch_key = ? ORDER BY id ASC",
            (branch_key,),
        )
        if not nodes:
            return {"title": "Untitled Adventure", "start_scene": None, "scenes": {}}

        scenes: dict[str, Any] = {}
        for node in nodes:
            node_id = int(node["id"])
            entities = fetch_all(
                self.connection,
                """
                SELECT entity_type, entity_id, role
                FROM node_entities
                WHERE story_node_id = ?
                ORDER BY id
                """,
                (node_id,),
            )
            current_location = next(
                (
                    entity
                    for entity in entities
                    if entity["entity_type"] == "location" and entity.get("role") == "current_scene"
                ),
                None,
            )
            if current_location is None:
                current_location = next((entity for entity in entities if entity["entity_type"] == "location"), None)
            location_name = None
            if current_location is not None:
                location = fetch_one(self.connection, "SELECT name FROM locations WHERE id = ?", (current_location["entity_id"],))
                location_name = location["name"] if location else None

            scene_choices = []
            for choice in fetch_all(
                self.connection,
                "SELECT * FROM choices WHERE from_node_id = ? ORDER BY id",
                (node_id,),
            ):
                self._decode_choice(choice)
                if choice["status"] in {"parked", "closed"}:
                    continue
                scene_choices.append(
                    {
                        "id": int(choice["id"]),
                        "label": choice["choice_text"],
                        "target": str(choice["to_node_id"]) if choice["to_node_id"] is not None else None,
                        "resolved": choice["to_node_id"] is not None,
                        "status": choice["status"],
                        "notes": choice.get("notes_data", {}).get("notes") if isinstance(choice.get("notes_data"), dict) else choice.get("notes"),
                        "next_node": (choice.get("planning") or {}).get("next_node") or (choice.get("planning") or {}).get("goal"),
                        "further_goals": (choice.get("planning") or {}).get("further_goals") or (choice.get("planning") or {}).get("intent"),
                        "intent": (choice.get("planning") or {}).get("further_goals") or (choice.get("planning") or {}).get("intent"),
                    }
                )

            present_entities = self._list_present_entities(node_id)
            for entity in present_entities:
                if entity["entity_type"] == "character" and entity["entity_id"] == 1:
                    entity["use_player_fallback"] = True

            scenes[str(node_id)] = {
                "node_id": node_id,
                "title": node.get("title"),
                "summary": node.get("summary"),
                "node_kind": node.get("node_kind") or "normal",
                "auto_continue_to_scene": (
                    str(node["auto_continue_to_node_id"])
                    if node.get("auto_continue_to_node_id") is not None
                    else None
                ),
                "location": location_name or "Unknown",
                "location_entity_id": int(current_location["entity_id"]) if current_location is not None else None,
                "lines": self._decode_dialogue_lines(node),
                "choices": scene_choices,
                "present_entities": present_entities,
            }

        start_node = nodes[0]
        return {
            "title": start_node.get("title") or "Adventure",
            "start_scene": str(start_node["id"]),
            "scenes": scenes,
        }

    def list_merge_candidates(
        self,
        branch_key: str,
        *,
        exclude_node_ids: list[int] | None = None,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        excluded = set(exclude_node_ids or [])
        rows = fetch_all(
            self.connection,
            """
            SELECT id, title, summary, scene_text
            FROM story_nodes
            WHERE branch_key = ?
            ORDER BY id ASC
            """,
            (branch_key,),
        )
        candidates: list[dict[str, Any]] = []
        for row in rows:
            node_id = int(row["id"])
            if node_id in excluded:
                continue
            choices = fetch_all(
                self.connection,
                "SELECT id, choice_text, to_node_id FROM choices WHERE from_node_id = ? ORDER BY id",
                (node_id,),
            )
            candidates.append(
                {
                    "node_id": node_id,
                    "title": row["title"],
                    "summary": row["summary"] or row["scene_text"][:180],
                    "choice_count": len(choices),
                }
            )
        return candidates[:limit]

    def list_jobs(self) -> list[dict[str, Any]]:
        return fetch_all(self.connection, "SELECT * FROM generation_jobs ORDER BY id DESC")

    def create_story_node(
        self,
        *,
        branch_key: str,
        title: str | None,
        scene_text: str,
        summary: str | None,
        parent_node_id: int | None = None,
        node_kind: str = "normal",
        auto_continue_to_node_id: int | None = None,
        dialogue_lines: list[dict[str, Any]] | None = None,
        referenced_entities: list[dict[str, Any]] | None = None,
        present_entities: list[dict[str, Any]] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """
            INSERT INTO story_nodes (
                branch_key,
                parent_node_id,
                title,
                scene_text,
                summary,
                node_kind,
                auto_continue_to_node_id,
                dialogue_lines_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                branch_key,
                parent_node_id,
                title,
                scene_text.strip(),
                summary,
                node_kind,
                auto_continue_to_node_id,
                json.dumps(dialogue_lines or []),
            ),
        )
        node_id = cursor.lastrowid
        for entity in referenced_entities or []:
            self.link_entity(
                story_node_id=node_id,
                entity_type=entity["entity_type"],
                entity_id=entity["entity_id"],
                role=entity.get("role", "mentioned"),
            )
        for present_entity in present_entities or []:
            self.link_present_entity(
                story_node_id=node_id,
                entity_type=present_entity["entity_type"],
                entity_id=present_entity["entity_id"],
                slot=present_entity["slot"],
                scale=present_entity.get("scale"),
                offset_x_percent=present_entity.get("offset_x_percent", 0.0),
                offset_y_percent=present_entity.get("offset_y_percent", 0.0),
                focus=bool(present_entity.get("focus", False)),
                hidden_on_lines=present_entity.get("hidden_on_lines", []),
            )
        if commit:
            self.connection.commit()
        return self.get_story_node(node_id) or {}

    def get_story_node(self, node_id: int) -> dict[str, Any] | None:
        node = fetch_one(self.connection, "SELECT * FROM story_nodes WHERE id = ?", (node_id,))
        if node is None:
            return None
        node["dialogue_lines"] = self._decode_dialogue_lines(node)
        node["choices"] = fetch_all(
            self.connection,
            "SELECT * FROM choices WHERE from_node_id = ? ORDER BY id",
            (node_id,),
        )
        self._decode_choice_rows(node["choices"])
        node["present_entities"] = self._list_present_entities(node_id)
        node["entities"] = fetch_all(
            self.connection,
            """
            SELECT entity_type, entity_id, role
            FROM node_entities
            WHERE story_node_id = ?
            ORDER BY id
            """,
            (node_id,),
        )
        return node

    def _clone_referenced_entities_for_node(self, node_id: int) -> list[dict[str, Any]]:
        return fetch_all(
            self.connection,
            """
            SELECT entity_type, entity_id, role
            FROM node_entities
            WHERE story_node_id = ?
            ORDER BY id
            """,
            (node_id,),
        )

    def _clone_present_entities_for_node(self, node_id: int) -> list[dict[str, Any]]:
        return [
            {
                "entity_type": entity["entity_type"],
                "entity_id": int(entity["entity_id"]),
                "slot": entity["slot"],
                "scale": entity.get("scale"),
                "offset_x_percent": entity.get("offset_x_percent", 0.0),
                "offset_y_percent": entity.get("offset_y_percent", 0.0),
                "focus": bool(entity.get("focus", False)),
                "hidden_on_lines": list(entity.get("hidden_on_lines", [])),
            }
            for entity in self._list_present_entities(node_id)
        ]

    def list_lineage_node_ids(self, node_id: int) -> list[int]:
        lineage: list[int] = []
        current_id: int | None = node_id
        seen: set[int] = set()
        while current_id is not None and current_id not in seen:
            seen.add(current_id)
            row = fetch_one(
                self.connection,
                "SELECT id, parent_node_id FROM story_nodes WHERE id = ?",
                (current_id,),
            )
            if row is None:
                break
            lineage.append(int(row["id"]))
            parent_node_id = row.get("parent_node_id")
            current_id = int(parent_node_id) if parent_node_id is not None else None
        lineage.reverse()
        return lineage

    def get_node_depth(self, node_id: int) -> int:
        lineage = self.list_lineage_node_ids(node_id)
        return max(len(lineage) - 1, 0)

    def list_lineage_entity_ids(self, node_id: int, entity_type: str) -> set[int]:
        lineage_ids = self.list_lineage_node_ids(node_id)
        if not lineage_ids:
            return set()
        placeholders = ",".join("?" for _ in lineage_ids)
        rows = fetch_all(
            self.connection,
            f"""
            SELECT DISTINCT entity_id
            FROM node_entities
            WHERE entity_type = ?
              AND story_node_id IN ({placeholders})
            UNION
            SELECT DISTINCT entity_id
            FROM story_node_present_entities
            WHERE entity_type = ?
              AND story_node_id IN ({placeholders})
            """,
            (entity_type, *lineage_ids, entity_type, *lineage_ids),
        )
        return {int(row["entity_id"]) for row in rows if row.get("entity_id") is not None}

    def create_choice(
        self,
        *,
        from_node_id: int,
        choice_text: str,
        to_node_id: int | None = None,
        status: str = "open",
        notes: str | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """
            INSERT INTO choices (from_node_id, choice_text, to_node_id, status, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (from_node_id, choice_text.strip(), to_node_id, status, notes),
        )
        if commit:
            self.connection.commit()
        return fetch_one(self.connection, "SELECT * FROM choices WHERE id = ?", (cursor.lastrowid,)) or {}

    def update_choice_notes(
        self,
        choice_id: int,
        notes: str,
        *,
        idea_binding: dict[str, Any] | None | object = _CHOICE_BINDING_UNSET,
    ) -> dict[str, Any]:
        existing = fetch_one(self.connection, "SELECT * FROM choices WHERE id = ?", (choice_id,))
        existing_payload: dict[str, Any] | None = None
        if existing is not None and existing.get("notes"):
            try:
                decoded = json.loads(existing["notes"])
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                existing_payload = decoded

        if idea_binding is self._CHOICE_BINDING_UNSET:
            resolved_binding = (
                existing_payload.get("idea_binding")
                if isinstance(existing_payload, dict)
                else None
            )
        else:
            resolved_binding = idea_binding

        if existing_payload is not None or resolved_binding is not None:
            stored_notes: str = json.dumps(
                {
                    **(
                        {
                            key: value
                            for key, value in existing_payload.items()
                            if key not in {"notes", "idea_binding"}
                        }
                        if isinstance(existing_payload, dict)
                        else {}
                    ),
                    "notes": notes,
                    **({"idea_binding": resolved_binding} if resolved_binding is not None else {}),
                }
            )
        else:
            stored_notes = notes

        self.connection.execute(
            """
            UPDATE choices
            SET notes = ?
            WHERE id = ?
            """,
            (stored_notes, choice_id),
        )
        self.connection.commit()
        choice = fetch_one(self.connection, "SELECT * FROM choices WHERE id = ?", (choice_id,))
        if choice is None:
            return {}
        self._decode_choice(choice)
        return choice

    def replace_choice(self, choice_id: int, payload: ChoiceReplace) -> dict[str, Any]:
        existing = fetch_one(self.connection, "SELECT * FROM choices WHERE id = ?", (choice_id,))
        if existing is None:
            raise ValueError(f"Unknown choice id: {choice_id}")
        self.connection.execute(
            """
            UPDATE choices
            SET choice_text = ?,
                to_node_id = ?,
                status = ?,
                notes = ?,
                created_at = created_at
            WHERE id = ?
            """,
            (
                payload.choice_text.strip(),
                payload.to_node_id,
                payload.status,
                payload.notes,
                choice_id,
            ),
        )
        self.connection.commit()
        choice = fetch_one(self.connection, "SELECT * FROM choices WHERE id = ?", (choice_id,))
        if choice is None:
            return {}
        self._decode_choice(choice)
        return choice

    def park_choices(self, choice_ids: list[int]) -> int:
        unique_ids = sorted({int(choice_id) for choice_id in choice_ids})
        if not unique_ids:
            return 0
        placeholders = ",".join("?" for _ in unique_ids)
        cursor = self.connection.execute(
            f"UPDATE choices SET status = 'parked' WHERE id IN ({placeholders}) AND status = 'open'",
            tuple(unique_ids),
        )
        self.connection.commit()
        return int(cursor.rowcount or 0)

    def unpark_all_stuck_choices(self) -> int:
        rows = fetch_all(
            self.connection,
            """
            SELECT w.choice_id
            FROM worker_choice_failures w
            JOIN choices c ON c.id = w.choice_id
            WHERE c.status = 'parked'
            """,
        )
        count = 0
        for row in rows:
            self.set_choice_status(row["choice_id"], "open")
            count += 1
        return count

    def set_choice_status(self, choice_id: int, status: str) -> dict[str, Any]:
        self.connection.execute(
            "UPDATE choices SET status = ? WHERE id = ?",
            (status, choice_id),
        )
        self.connection.commit()
        if status == "open":
            self.clear_choice_failure(choice_id)
        choice = fetch_one(self.connection, "SELECT * FROM choices WHERE id = ?", (choice_id,))
        if choice is None:
            return {}
        self._decode_choice(choice)
        return choice

    def list_closed_leaf_candidates(
        self,
        *,
        branch_key: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                leaf.id AS leaf_node_id,
                leaf.parent_node_id,
                leaf.title AS leaf_title,
                parent.title AS parent_title,
                traversed.id AS traversed_choice_id,
                traversed.choice_text AS traversed_choice_text
            FROM story_nodes leaf
            JOIN story_nodes parent ON parent.id = leaf.parent_node_id
            JOIN choices traversed ON traversed.to_node_id = leaf.id
            LEFT JOIN choices child ON child.from_node_id = leaf.id
            WHERE leaf.parent_node_id IS NOT NULL
        """
        params: list[Any] = []
        if branch_key is not None:
            query += " AND leaf.branch_key = ?"
            params.append(branch_key)
        query += """
            GROUP BY leaf.id, leaf.parent_node_id, leaf.title, parent.title, traversed.id, traversed.choice_text
            HAVING SUM(CASE WHEN child.to_node_id IS NULL AND child.status = 'open' THEN 1 ELSE 0 END) = 0
            ORDER BY leaf.id DESC
            LIMIT ?
        """
        params.append(limit)
        return fetch_all(self.connection, query, tuple(params))

    def get_choice(self, choice_id: int) -> dict[str, Any] | None:
        choice = fetch_one(self.connection, "SELECT * FROM choices WHERE id = ?", (choice_id,))
        if choice is None:
            return None
        self._decode_choice(choice)
        return choice

    def link_entity(self, *, story_node_id: int, entity_type: str, entity_id: int, role: str = "mentioned") -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO node_entities (story_node_id, entity_type, entity_id, role)
            VALUES (?, ?, ?, ?)
            """,
            (story_node_id, entity_type, entity_id, role),
        )

    def link_present_entity(
        self,
        *,
        story_node_id: int,
        entity_type: str,
        entity_id: int,
        slot: str,
        scale: float | None = None,
        offset_x_percent: float = 0.0,
        offset_y_percent: float = 0.0,
        focus: bool = False,
        hidden_on_lines: list[int] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO story_node_present_entities (
                story_node_id, entity_type, entity_id, slot, scale, offset_x_percent, offset_y_percent, focus, hidden_on_lines_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                story_node_id,
                entity_type,
                entity_id,
                slot,
                scale,
                offset_x_percent,
                offset_y_percent,
                int(focus),
                json.dumps(hidden_on_lines or []),
            ),
        )

    def describe_branch_shape(
        self,
        branch_key: str,
        *,
        branching_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        policy = branching_policy or {}
        anti_overmerge = policy.get("anti_overmerge", policy)
        recent_window = int(anti_overmerge.get("recent_window", 6))
        merge_only_streak_limit = int(anti_overmerge.get("merge_only_streak_limit", 2))
        merge_only_count_limit = int(anti_overmerge.get("merge_only_count_limit", 3))

        rows = fetch_all(
            self.connection,
            """
            SELECT id, title, summary
            FROM story_nodes
            WHERE branch_key = ? AND parent_node_id IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (branch_key, recent_window),
        )

        recent_nodes: list[dict[str, Any]] = []
        merge_only_count = 0
        mixed_merge_count = 0
        nodes_opening_fresh_paths = 0
        merge_only_streak = 0
        same_location_streak = 0
        single_actor_scene_streak = 0
        new_character_gap_streak = 0
        action_family_counts: dict[str, int] = {}
        repeated_action_family: str | None = None
        recent_primary_location_id: int | None = None

        for index, row in enumerate(rows):
            node = self.get_story_node(int(row["id"])) or {}
            choices = node.get("choices") or []
            total_choices = len(choices)
            merge_choices = sum(1 for choice in choices if choice["to_node_id"] is not None)
            fresh_choices = sum(1 for choice in choices if choice["to_node_id"] is None)
            is_merge_only = total_choices > 0 and merge_choices > 0 and fresh_choices == 0
            opens_fresh_path = fresh_choices > 0
            is_mixed = merge_choices > 0 and fresh_choices > 0
            current_scene = next(
                (
                    entity for entity in (node.get("entities") or [])
                    if entity.get("entity_type") == "location" and entity.get("role") == "current_scene"
                ),
                None,
            )
            current_scene_location_id = (
                int(current_scene["entity_id"]) if current_scene and current_scene.get("entity_id") is not None else None
            )
            if index == same_location_streak and current_scene_location_id is not None:
                if recent_primary_location_id is None or current_scene_location_id == recent_primary_location_id:
                    same_location_streak += 1
                    recent_primary_location_id = current_scene_location_id

            character_ids = {
                int(entity["entity_id"])
                for entity in (node.get("entities") or [])
                if entity.get("entity_type") == "character" and entity.get("entity_id") is not None
            } | {
                int(entity["entity_id"])
                for entity in (node.get("present_entities") or [])
                if entity.get("entity_type") == "character" and entity.get("entity_id") is not None
            }
            has_character_pressure = (
                len(character_ids) >= 2
                or any(
                    entity.get("entity_type") == "character"
                    and entity.get("slot") != "hero-center"
                    for entity in (node.get("present_entities") or [])
                )
                or any(
                    entity.get("entity_type") == "character"
                    and entity.get("role") in {"introduced", "new_character", "speaker", "present"}
                    for entity in (node.get("entities") or [])
                )
            )
            if index == single_actor_scene_streak and not has_character_pressure:
                single_actor_scene_streak += 1

            introduces_brand_new_character = any(
                entity.get("entity_type") == "character"
                and entity.get("role") == "new_character"
                for entity in (node.get("entities") or [])
            )
            if index == new_character_gap_streak and not introduces_brand_new_character:
                new_character_gap_streak += 1

            node_action_families = {
                family
                for choice in choices
                if (family := self._classify_choice_action_family(choice.get("choice_text") or "")) != "other"
            }
            for family in node_action_families:
                action_family_counts[family] = action_family_counts.get(family, 0) + 1

            if is_merge_only:
                merge_only_count += 1
            if is_mixed:
                mixed_merge_count += 1
            if opens_fresh_path:
                nodes_opening_fresh_paths += 1
            if index == merge_only_streak and is_merge_only:
                merge_only_streak += 1

            recent_nodes.append(
                {
                    "node_id": int(row["id"]),
                    "title": row["title"],
                    "summary": row["summary"],
                    "total_choices": total_choices,
                    "merge_choices": merge_choices,
                    "fresh_choices": fresh_choices,
                    "is_merge_only": is_merge_only,
                    "opens_fresh_path": opens_fresh_path,
                    "current_scene_location_id": current_scene_location_id,
                    "has_character_pressure": has_character_pressure,
                    "introduces_brand_new_character": introduces_brand_new_character,
                    "action_families": sorted(node_action_families),
                }
            )

        if action_family_counts:
            repeated_action_family = max(
                action_family_counts.items(),
                key=lambda item: (item[1], item[0]),
            )[0]

        should_prefer_divergence = (
            merge_only_streak >= merge_only_streak_limit
            or merge_only_count >= merge_only_count_limit
        )
        if should_prefer_divergence:
            merge_pressure_level = "high"
            reason = (
                "This branch has reconverged too often recently. The next expansion should open at least one fresh path."
            )
        elif merge_only_count > 0 or mixed_merge_count > 0:
            merge_pressure_level = "medium"
            reason = (
                "This branch has used some quick merges recently. Another merge is still possible, but divergence should be considered first."
            )
        else:
            merge_pressure_level = "low"
            reason = "This branch has room for a quick merge if it truly fits, but fresh divergence is still welcome."

        return {
            "recent_window": recent_window,
            "merge_only_streak": merge_only_streak,
            "merge_only_count": merge_only_count,
            "mixed_merge_count": mixed_merge_count,
            "nodes_opening_fresh_paths": nodes_opening_fresh_paths,
            "same_location_streak": same_location_streak,
            "single_actor_scene_streak": single_actor_scene_streak,
            "new_character_gap_streak": new_character_gap_streak,
            "recent_action_family_counts": action_family_counts,
            "repeated_action_family": repeated_action_family,
            "merge_pressure_level": merge_pressure_level,
            "should_prefer_divergence": should_prefer_divergence,
            "reason": reason,
            "recent_nodes": recent_nodes,
        }

    def _classify_choice_action_family(self, choice_text: str) -> str:
        text = (choice_text or "").strip().lower()
        if not text:
            return "other"
        if self.SOCIAL_ACTION_PATTERN.search(text):
            return "social"
        if self.LOCATION_TRANSITION_PATTERN.search(text):
            return "travel"
        if self.FOLLOW_ACTION_PATTERN.search(text):
            return "follow"
        if self.TOUCH_ACTION_PATTERN.search(text):
            return "touch"
        if self.STEP_BACK_ACTION_PATTERN.search(text):
            return "step_back"
        if self.INSPECT_ACTION_PATTERN.search(text):
            return "inspect"
        return "other"

    def list_frontier(
        self,
        *,
        branch_state_service: BranchStateService,
        branch_key: str | None = None,
        limit: int = 20,
        mode: str = "auto",
        branching_policy: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT c.*, sn.branch_key, sn.summary AS from_node_summary, sn.title AS from_node_title, sn.scene_text AS from_node_text
            FROM choices c
            JOIN story_nodes sn ON sn.id = c.from_node_id
            WHERE c.to_node_id IS NULL AND c.status = 'open'
        """
        params: list[Any] = []
        if branch_key:
            query += " AND sn.branch_key = ?"
            params.append(branch_key)
        query += " ORDER BY c.created_at ASC, c.id ASC"
        rows = fetch_all(self.connection, query, tuple(params))
        items: list[dict[str, Any]] = []
        global_budget = self.build_frontier_budget_state(branch_key=branch_key, branching_policy=branching_policy)
        distinct_from_node_ids = {int(row["from_node_id"]) for row in rows}
        node_depths = {
            node_id: self.get_node_depth(node_id)
            for node_id in distinct_from_node_ids
        }
        max_frontier_depth = max(node_depths.values(), default=0)
        for row in rows:
            branch = branch_state_service.get_branch_state(row["branch_key"])
            branch_shape = self.describe_branch_shape(row["branch_key"], branching_policy=branching_policy)
            choice = self.get_choice(int(row["id"])) or {}
            node_depth = node_depths.get(int(row["from_node_id"]), 0)
            score, reason = self._score_frontier_item(
                row=row,
                branch=branch,
                branch_shape=branch_shape,
                global_budget=global_budget,
                choice=choice,
                node_depth=node_depth,
                max_frontier_depth=max_frontier_depth,
            )
            item = {
                "branch_key": row["branch_key"],
                "from_node_id": row["from_node_id"],
                "choice_id": row["id"],
                "choice_text": row["choice_text"],
                "depth": node_depth,
                "current_act_phase": branch["act_phase"],
                "branch_summary": row["from_node_summary"] or row["from_node_title"] or row["from_node_text"][:200],
                "active_hooks": branch["active_hooks"],
                "eligible_major_hooks": branch["eligible_major_hooks"],
                "blocked_major_hooks": branch["blocked_major_hooks"],
                "available_affordances": branch_state_service.list_available_affordances(row["branch_key"]),
                "recurring_entities": branch["recurring_entities"],
                "branch_shape": branch_shape,
                "frontier_budget_state": global_budget,
                "from_node_total_choice_count": self.count_total_choices_for_node(int(row["from_node_id"])),
                "from_node_open_choice_count": self.count_open_choices_for_node(int(row["from_node_id"])),
                "bound_idea": choice.get("idea_binding"),
                "selection_score": score,
                "selection_reason": reason,
                "created_at": row["created_at"],
            }
            items.append(item)
        if mode == "auto":
            items.sort(key=lambda item: (-float(item["selection_score"]), item["created_at"], int(item["choice_id"])))
        else:
            items.sort(key=lambda item: (item["created_at"], int(item["choice_id"])))
        return items[:limit]

    def apply_generation_candidate(
        self,
        *,
        request_branch_key: str,
        parent_node_id: int,
        choice_id: int | None,
        candidate: GenerationCandidate,
        branch_state_service: BranchStateService,
        canon: CanonResolver,
    ) -> dict[str, Any]:
        if candidate.branch_key != request_branch_key:
            raise ValueError("Request branch_key must match candidate.branch_key.")

        validation = branch_state_service.sync_branch_progress(request_branch_key)
        _ = validation
        parent_node = self.get_story_node(parent_node_id)
        if parent_node is None:
            raise ValueError(f"Unknown parent node id: {parent_node_id}")
        if parent_node["branch_key"] != request_branch_key:
            raise ValueError("Parent node does not belong to the requested branch.")
        if choice_id is not None:
            choice = fetch_one(self.connection, "SELECT * FROM choices WHERE id = ?", (choice_id,))
            if choice is None:
                raise ValueError(f"Unknown choice id: {choice_id}")
            if int(choice["from_node_id"]) != parent_node_id:
                raise ValueError("choice_id does not belong to parent_node_id.")
            if choice["to_node_id"] is not None:
                raise ValueError("choice_id is already fulfilled.")
        development_depth = int(branch_state_service.ensure_branch(request_branch_key)["branch_depth"]) + 1
        created_location_ids_by_name: dict[str, int] = {}
        for location in candidate.new_locations:
            created_location = canon.create_or_get_location(
                name=location.name,
                description=location.description,
                canonical_summary=location.canonical_summary,
            )
            if location.name.strip() and created_location.get("id") is not None:
                created_location_ids_by_name[location.name.strip().lower()] = int(created_location["id"])
        inherited_referenced_entities = self._inherit_referenced_entities(
            parent_node=parent_node,
            candidate=candidate,
        )
        candidate_declares_current_scene = any(
            reference.entity_type == "location" and reference.role == "current_scene"
            for reference in candidate.entity_references
        )
        if not candidate_declares_current_scene and len(candidate.new_locations) == 1:
            only_new_location = candidate.new_locations[0]
            new_location_id = created_location_ids_by_name.get((only_new_location.name or "").strip().lower())
            if new_location_id is not None:
                inherited_referenced_entities = [
                    item
                    for item in inherited_referenced_entities
                    if not (item.get("entity_type") == "location" and item.get("role") == "current_scene")
                ]
                inherited_referenced_entities.append(
                    {
                        "entity_type": "location",
                        "entity_id": int(new_location_id),
                        "role": "current_scene",
                    }
                )
        floating_intro_reference_ids = {
            int(intro.character_id)
            for intro in candidate.floating_character_introductions
        }
        existing_reference_keys = {
            (item["entity_type"], int(item["entity_id"]))
            for item in inherited_referenced_entities
            if item.get("entity_id") is not None
        }
        for character_id in sorted(floating_intro_reference_ids):
            key = ("character", character_id)
            if key in existing_reference_keys:
                continue
            inherited_referenced_entities.append(
                {
                    "entity_type": "character",
                    "entity_id": character_id,
                    "role": "introduced",
                }
            )
            existing_reference_keys.add(key)
        inherited_present_entities = self._inherit_present_entities(
            parent_node=parent_node,
            candidate=candidate,
        )
        floating_intro_text = "\n\n".join(
            intro.intro_text.strip()
            for intro in candidate.floating_character_introductions
            if intro.intro_text.strip()
        )
        scene_text = candidate.scene_text.strip()
        if floating_intro_text:
            scene_text = f"{floating_intro_text}\n\n{scene_text}" if scene_text else floating_intro_text
        with self.connection:
            node = self.create_story_node(
                branch_key=request_branch_key,
                title=candidate.scene_title,
                scene_text=scene_text,
                summary=candidate.scene_summary,
                parent_node_id=parent_node_id,
                dialogue_lines=[line.model_dump() for line in candidate.dialogue_lines],
                referenced_entities=inherited_referenced_entities,
                present_entities=inherited_present_entities,
                commit=False,
            )
            new_node_id = int(node["id"])
            transition_specs_by_choice_index = {
                int(spec.choice_list_index): spec
                for spec in candidate.transition_nodes
            }

            if choice_id is not None:
                self.connection.execute(
                    """
                    UPDATE choices
                    SET to_node_id = ?, status = CASE WHEN status = 'open' THEN 'fulfilled' ELSE status END
                    WHERE id = ?
                    """,
                    (new_node_id, choice_id),
                )

            created_choices: list[dict[str, Any]] = []
            created_transition_nodes: list[dict[str, Any]] = []
            for choice_index, choice in enumerate(candidate.choices):
                target_node_id = choice.target_node_id
                target_current_node = bool(choice.target_current_node)
                if target_node_id is not None:
                    target_node = self.get_story_node(target_node_id)
                    if target_node is None:
                        raise ValueError(f"Unknown merge target node id: {target_node_id}")
                    if target_node["branch_key"] != request_branch_key:
                        raise ValueError("Merged choice target must belong to the same branch.")
                applied_target_node_id = target_node_id
                transition_spec = transition_specs_by_choice_index.get(choice_index)
                if target_current_node and transition_spec is None:
                    raise ValueError("Self-merge choices require a transition node.")
                if (target_node_id is not None or target_current_node) and transition_spec is not None:
                    auto_continue_target_id = new_node_id if target_current_node else target_node_id
                    transition_node = self.create_story_node(
                        branch_key=request_branch_key,
                        title=transition_spec.scene_title,
                        scene_text=transition_spec.scene_text,
                        summary=transition_spec.scene_summary,
                        parent_node_id=new_node_id,
                        node_kind="transition",
                        auto_continue_to_node_id=auto_continue_target_id,
                        dialogue_lines=[line.model_dump() for line in transition_spec.dialogue_lines],
                        referenced_entities=(
                            [reference.model_dump() for reference in transition_spec.entity_references]
                            if transition_spec.entity_references
                            else self._clone_referenced_entities_for_node(new_node_id)
                        ),
                        present_entities=(
                            [entity.model_dump() for entity in transition_spec.scene_present_entities]
                            if transition_spec.scene_present_entities
                            else self._clone_present_entities_for_node(new_node_id)
                        ),
                        commit=False,
                    )
                    created_transition_nodes.append(transition_node)
                    applied_target_node_id = int(transition_node["id"])
                created_choices.append(
                    self.create_choice(
                        from_node_id=new_node_id,
                        choice_text=choice.choice_text,
                        to_node_id=applied_target_node_id,
                        status=(
                            "closed"
                            if choice.choice_class == "ending" and applied_target_node_id is None
                            else "fulfilled" if applied_target_node_id is not None else "open"
                        ),
                        notes=(
                            json.dumps(
                                {
                                    "required_affordances": choice.required_affordances,
                                    "notes": choice.notes,
                                    "choice_class": choice.choice_class,
                                    "ending_category": choice.ending_category,
                                    "target_node_id": target_node_id,
                                    "target_current_node": target_current_node,
                                    "transition_node_id": (
                                        applied_target_node_id if transition_spec is not None else None
                                    ),
                                }
                            )
                            if choice.required_affordances or choice.notes
                            or applied_target_node_id is not None
                            or target_current_node
                            or choice.choice_class is not None
                            or choice.ending_category is not None
                            else None
                        ),
                        commit=False,
                    )
                )

            created_character_ids_by_name: dict[str, int] = {}
            for character in candidate.new_characters:
                home_location_id = None
                if character.home_location_name:
                    home_location_id = self._resolve_or_create_named_entity(
                        entity_type="location",
                        name=character.home_location_name,
                    )
                created_character = canon.create_or_get_character(
                    name=character.name,
                    description=character.description,
                    canonical_summary=character.canonical_summary,
                    home_location_id=home_location_id,
                )
                if character.name.strip() and created_character.get("id") is not None:
                    created_character_ids_by_name[character.name.strip().lower()] = int(created_character["id"])

            for obj in candidate.new_objects:
                default_location_id = None
                if obj.default_location_name:
                    default_location_id = self._resolve_or_create_named_entity(
                        entity_type="location",
                        name=obj.default_location_name,
                    )
                canon.create_or_get_object(
                    name=obj.name,
                    description=obj.description,
                    canonical_summary=obj.canonical_summary,
                    default_location_id=default_location_id,
                )

            for fact in candidate.fact_updates:
                entity_id = self._resolve_or_create_entity_id(canon=canon, fact=fact)
                self.connection.execute(
                    """
                    INSERT INTO facts (entity_type, entity_id, fact_text, is_locked, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (fact.entity_type, entity_id, fact.fact_text.strip(), int(fact.is_locked), fact.source),
                )

            for relation in candidate.relation_updates:
                subject_id = canon.resolve_entity_id(relation.subject_type, relation.subject_name)
                object_id = canon.resolve_entity_id(relation.object_type, relation.object_name)
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO relations (subject_type, subject_id, relation_type, object_type, object_id, notes)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        relation.subject_type,
                        subject_id,
                        relation.relation_type,
                        relation.object_type,
                        object_id,
                        relation.notes,
                    ),
                )

            self._attach_visible_speaking_characters(
                story_node_id=new_node_id,
                candidate=candidate,
                canon=canon,
                created_character_ids_by_name=created_character_ids_by_name,
            )

            for hook in candidate.new_hooks:
                self.connection.execute(
                    """
                    INSERT INTO story_hooks (
                        branch_key, hook_type, importance, summary, payoff_concept, must_not_imply_json, linked_entity_type, linked_entity_id,
                        introduced_at_depth, min_distance_to_payoff, min_distance_to_next_development, last_development_depth,
                        required_clue_tags_json, required_state_tags_json,
                        status, notes
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (
                        request_branch_key,
                        hook.hook_type,
                        hook.importance,
                        hook.summary,
                        hook.payoff_concept,
                        json.dumps(hook.must_not_imply),
                        hook.linked_entity_type,
                        hook.linked_entity_id,
                        development_depth,
                        hook.min_distance_to_payoff,
                        hook.min_distance_to_next_development,
                        development_depth,
                        json.dumps(hook.required_clue_tags),
                        json.dumps(hook.required_state_tags),
                        hook.notes,
                    ),
                )

            for hook_update in candidate.hook_updates:
                existing_hook = branch_state_service.get_hook(hook_update.hook_id)
                if existing_hook is None:
                    raise ValueError(f"Unknown hook id referenced in hook_updates: {hook_update.hook_id}")
                updated_clue_tags = sorted(set(existing_hook["required_clue_tags"]) | set(hook_update.add_required_clue_tags))
                updated_state_tags = sorted(set(existing_hook["required_state_tags"]) | set(hook_update.add_required_state_tags))
                self.connection.execute(
                    """
                    UPDATE story_hooks
                    SET status = ?, notes = COALESCE(?, notes), resolution_text = COALESCE(?, resolution_text),
                        min_distance_to_next_development = COALESCE(?, min_distance_to_next_development),
                        last_development_depth = ?,
                        required_clue_tags_json = ?, required_state_tags_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        hook_update.status,
                        hook_update.progress_note,
                        hook_update.resolution_text,
                        hook_update.next_min_distance_to_development,
                        development_depth,
                        json.dumps(updated_clue_tags),
                        json.dumps(updated_state_tags),
                        hook_update.hook_id,
                    ),
                )

            if candidate.global_direction_notes:
                story_notes = StoryDirectionService(self.connection)
                for direction_note in candidate.global_direction_notes:
                    story_notes.create_note(
                        note_type=direction_note.note_type,
                        title=direction_note.title,
                        note_text=direction_note.note_text,
                        status=direction_note.status,
                        priority=direction_note.priority,
                        related_entity_type=direction_note.related_entity_type,
                        related_entity_id=direction_note.related_entity_id,
                        related_hook_id=direction_note.related_hook_id,
                        source_branch_key=direction_note.source_branch_key or request_branch_key,
                        notes=direction_note.notes,
                        created_by="generation_apply",
                    )

            for inventory_change in candidate.inventory_changes:
                object_id = inventory_change.object_id
                if object_id is None and inventory_change.object_name:
                    object_id = self._resolve_or_create_named_entity(
                        entity_type="object",
                        name=inventory_change.object_name,
                    )
                if object_id is None:
                    raise ValueError("Inventory changes require object_id or object_name.")
                self._apply_inventory_change(
                    branch_key=request_branch_key,
                    object_id=object_id,
                    action=inventory_change.action,
                    quantity=inventory_change.quantity,
                    notes=inventory_change.notes,
                    source_node_id=new_node_id,
                )

            for affordance_change in candidate.affordance_changes:
                self._apply_affordance_change(branch_key=request_branch_key, change=affordance_change.model_dump())

            for relationship_change in candidate.relationship_changes:
                self._apply_relationship_change(branch_key=request_branch_key, change=relationship_change.model_dump())

            for tag in candidate.discovered_clue_tags:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO branch_tags (branch_key, tag, tag_type, source, notes)
                    VALUES (?, ?, 'clue', 'generation_apply', ?)
                    """,
                    (request_branch_key, tag, f"Discovered while applying node {new_node_id}"),
                )
            for tag in candidate.discovered_state_tags:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO branch_tags (branch_key, tag, tag_type, source, notes)
                    VALUES (?, ?, 'state', 'generation_apply', ?)
                    """,
                    (request_branch_key, tag, f"Learned while applying node {new_node_id}"),
                )

            for asset_request in candidate.asset_requests:
                self.connection.execute(
                    """
                    INSERT INTO generation_jobs (job_type, status, payload_json)
                    VALUES (?, 'pending', ?)
                    """,
                    ("asset_request", json.dumps(asset_request.model_dump())),
                )

            branch_state_service.sync_branch_progress(request_branch_key, latest_story_node_id=new_node_id)
            applied_node = self.get_story_node(new_node_id) or node

        return {
            "node": applied_node,
            "created_choices": created_choices,
            "created_transition_nodes": [self.get_story_node(int(node["id"])) for node in created_transition_nodes],
            "fulfilled_choice_id": choice_id,
        }

    def create_job(self, *, job_type: str, payload_json: str | None = None, status: str = "pending") -> dict[str, Any]:
        cursor = self.connection.execute(
            """
            INSERT INTO generation_jobs (job_type, status, payload_json)
            VALUES (?, ?, ?)
            """,
            (job_type, status, payload_json),
        )
        self.connection.commit()
        return fetch_one(self.connection, "SELECT * FROM generation_jobs WHERE id = ?", (cursor.lastrowid,)) or {}

    def counts(self) -> dict[str, int]:
        table_names = [
            "locations",
            "characters",
            "objects",
            "relations",
            "facts",
            "story_nodes",
            "story_node_present_entities",
            "choices",
            "assets",
            "generation_jobs",
            "branch_state",
            "inventory_entries",
            "unlocked_affordances",
            "relationship_states",
            "branch_tags",
            "story_hooks",
            "story_direction_notes",
            "worldbuilding_notes",
            "worker_choice_failures",
        ]
        counts: dict[str, int] = {}
        for table_name in table_names:
            row = fetch_one(self.connection, f"SELECT COUNT(*) AS count FROM {table_name}")
            counts[table_name] = int(row["count"]) if row else 0
        return counts

    def get_choice_failure(self, choice_id: int) -> dict[str, Any] | None:
        return fetch_one(
            self.connection,
            "SELECT * FROM worker_choice_failures WHERE choice_id = ?",
            (choice_id,),
        )

    def clear_choice_failure(self, choice_id: int) -> None:
        self.connection.execute(
            "DELETE FROM worker_choice_failures WHERE choice_id = ?",
            (choice_id,),
        )
        self.connection.commit()

    def record_choice_worker_failure(
        self,
        *,
        choice_id: int,
        error: str,
        auto_park_threshold: int = 5,
    ) -> dict[str, Any]:
        choice = fetch_one(self.connection, "SELECT * FROM choices WHERE id = ?", (choice_id,))
        if choice is None:
            raise ValueError(f"Unknown choice id: {choice_id}")
        self.connection.execute(
            """
            INSERT INTO worker_choice_failures (choice_id, failed_run_count, last_failed_at, last_error, updated_at)
            VALUES (?, 1, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(choice_id) DO UPDATE SET
                failed_run_count = worker_choice_failures.failed_run_count + 1,
                last_failed_at = CURRENT_TIMESTAMP,
                last_error = excluded.last_error,
                updated_at = CURRENT_TIMESTAMP
            """,
            (choice_id, error),
        )
        record = self.get_choice_failure(choice_id) or {}
        failed_run_count = int(record.get("failed_run_count") or 0)
        if failed_run_count >= auto_park_threshold and choice.get("status") == "open":
            self.connection.execute(
                "UPDATE choices SET status = 'parked' WHERE id = ?",
                (choice_id,),
            )
            self.connection.execute(
                """
                UPDATE worker_choice_failures
                SET auto_parked_at = COALESCE(auto_parked_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE choice_id = ?
                """,
                (choice_id,),
            )
        self.connection.commit()
        return self.get_choice_failure(choice_id) or record

    def list_stuck_frontier_choices(self, *, limit: int = 10) -> list[dict[str, Any]]:
        return fetch_all(
            self.connection,
            """
            SELECT
                w.choice_id,
                w.failed_run_count,
                w.last_failed_at,
                w.last_error,
                w.auto_parked_at,
                c.from_node_id,
                c.choice_text,
                c.status,
                sn.branch_key,
                sn.title AS from_node_title
            FROM worker_choice_failures w
            JOIN choices c ON c.id = w.choice_id
            JOIN story_nodes sn ON sn.id = c.from_node_id
            WHERE c.status IN ('open', 'parked')
            ORDER BY
                CASE WHEN c.status = 'parked' THEN 1 ELSE 0 END DESC,
                w.failed_run_count DESC,
                w.last_failed_at DESC,
                w.choice_id DESC
            LIMIT ?
            """,
            (limit,),
        )

    def _list_present_entities(self, story_node_id: int) -> list[dict[str, Any]]:
        rows = fetch_all(
            self.connection,
            """
            SELECT entity_type, entity_id, slot, scale, offset_x_percent, offset_y_percent, focus, hidden_on_lines_json
            FROM story_node_present_entities
            WHERE story_node_id = ?
            ORDER BY id
            """,
            (story_node_id,),
        )
        for row in rows:
            row["focus"] = bool(row["focus"])
            row["hidden_on_lines"] = json.loads(row["hidden_on_lines_json"] or "[]")
            row.pop("hidden_on_lines_json", None)
        return rows

    def _decode_dialogue_lines(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            lines = json.loads(node.get("dialogue_lines_json") or "[]")
        except json.JSONDecodeError:
            lines = []
        if lines:
            return lines
        scene_text = (node.get("scene_text") or "").strip()
        if not scene_text:
            return []
        paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", scene_text) if paragraph.strip()]
        if not paragraphs:
            paragraphs = [scene_text]
        return [{"speaker": "Narrator", "text": paragraph} for paragraph in paragraphs]

    def _inherit_referenced_entities(
        self,
        *,
        parent_node: dict[str, Any],
        candidate: GenerationCandidate,
    ) -> list[dict[str, Any]]:
        references = [reference.model_dump() for reference in candidate.entity_references]
        has_current_scene = any(
            reference.get("role") == "current_scene" and reference.get("entity_type") == "location"
            for reference in references
        )
        if has_current_scene:
            return references
        parent_current_scene = next(
            (entity for entity in (parent_node.get("entities") or []) if entity.get("role") == "current_scene"),
            None,
        )
        if parent_current_scene is not None:
            references.append(
                {
                    "entity_type": parent_current_scene["entity_type"],
                    "entity_id": int(parent_current_scene["entity_id"]),
                    "role": parent_current_scene.get("role", "current_scene"),
                }
            )
        return references

    def _inherit_present_entities(
        self,
        *,
        parent_node: dict[str, Any],
        candidate: GenerationCandidate,
    ) -> list[dict[str, Any]]:
        if candidate.scene_present_entities:
            return [entity.model_dump() for entity in candidate.scene_present_entities]
        return [
            {
                "entity_type": entity["entity_type"],
                "entity_id": int(entity["entity_id"]),
                "slot": entity["slot"],
                "scale": entity.get("scale"),
                "offset_x_percent": float(entity.get("offset_x_percent") or 0.0),
                "offset_y_percent": float(entity.get("offset_y_percent") or 0.0),
                "focus": bool(entity.get("focus", False)),
                "hidden_on_lines": list(entity.get("hidden_on_lines", [])),
            }
            for entity in (parent_node.get("present_entities") or [])
        ]

    def _decode_choice_rows(self, choices: list[dict[str, Any]]) -> None:
        for choice in choices:
            self._decode_choice(choice)

    def _decode_choice(self, choice: dict[str, Any]) -> None:
        raw_notes = choice.get("notes")
        if not raw_notes:
            choice["notes_data"] = None
            choice["planning"] = None
            choice["idea_binding"] = None
            choice["choice_class"] = None
            choice["ending_category"] = None
            return
        try:
            decoded = json.loads(raw_notes)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            choice["notes_data"] = decoded
            planning_source = decoded.get("notes")
            choice["idea_binding"] = decoded.get("idea_binding")
            choice["choice_class"] = decoded.get("choice_class")
            choice["ending_category"] = decoded.get("ending_category")
        else:
            choice["notes_data"] = None
            planning_source = raw_notes
            choice["idea_binding"] = None
            choice["choice_class"] = None
            choice["ending_category"] = None
        choice["planning"] = self._parse_choice_planning(planning_source)

    def _parse_choice_planning(self, raw_notes: str | None) -> dict[str, str] | None:
        if not raw_notes:
            return None
        match = self.CHOICE_NOTES_PATTERN.search(raw_notes.strip())
        if match is None:
            return None
        next_node = (match.group("next_node") or match.group("goal") or "").strip()
        further_goals = (match.group("further_goals") or match.group("intent") or "").strip()
        if not next_node or not further_goals:
            return None
        return {
            "next_node": next_node,
            "further_goals": further_goals,
        }

    def _speaker_is_nonvisual(self, speaker: str) -> bool:
        normalized = (speaker or "").strip()
        return any(pattern.search(normalized) for pattern in self.NONVISUAL_SPEAKER_PATTERNS)

    def _attach_visible_speaking_characters(
        self,
        *,
        story_node_id: int,
        candidate: GenerationCandidate,
        canon: CanonResolver,
        created_character_ids_by_name: dict[str, int],
    ) -> None:
        present_entities = self._list_present_entities(story_node_id)
        present_character_ids = {
            int(entity["entity_id"])
            for entity in present_entities
            if entity.get("entity_type") == "character"
        }
        used_slots = {str(entity["slot"]) for entity in present_entities if entity.get("entity_type") == "character"}
        referenced_character_ids = {
            int(row["entity_id"])
            for row in fetch_all(
                self.connection,
                "SELECT entity_id FROM node_entities WHERE story_node_id = ? AND entity_type = 'character'",
                (story_node_id,),
            )
            if row.get("entity_id") is not None
        }
        available_slots = [slot for slot in self.AUTO_STAGE_CHARACTER_SLOTS if slot not in used_slots]
        seen_speakers: set[str] = set()

        for line in candidate.dialogue_lines:
            speaker = (line.speaker or "").strip()
            lowered = speaker.lower()
            if not speaker or lowered in {"narrator", "you"} or self._speaker_is_nonvisual(speaker):
                continue
            if lowered in seen_speakers:
                continue
            seen_speakers.add(lowered)

            character_id = created_character_ids_by_name.get(lowered)
            if character_id is None:
                existing_character = canon.find_character_by_name(speaker)
                if existing_character is None or existing_character.get("id") is None:
                    continue
                character_id = int(existing_character["id"])

            if character_id not in referenced_character_ids:
                self.link_entity(
                    story_node_id=story_node_id,
                    entity_type="character",
                    entity_id=character_id,
                    role="new_character" if lowered in created_character_ids_by_name else "mentioned",
                )
                referenced_character_ids.add(character_id)
            if character_id in present_character_ids or not available_slots:
                continue
            slot = available_slots.pop(0)
            self.link_present_entity(
                story_node_id=story_node_id,
                entity_type="character",
                entity_id=character_id,
                slot=slot,
                focus=False,
            )
            present_character_ids.add(character_id)

    def _score_frontier_item(
        self,
        *,
        row: dict[str, Any],
        branch: dict[str, Any],
        branch_shape: dict[str, Any],
        global_budget: dict[str, Any],
        choice: dict[str, Any] | None = None,
        node_depth: int = 0,
        max_frontier_depth: int = 0,
    ) -> tuple[float, str]:
        active_hooks = len(branch["active_hooks"])
        eligible_major = len(branch["eligible_major_hooks"])
        blocked_major = len(branch["blocked_major_hooks"])
        affordances = len(branch["affordances"])
        recurring_entities = len(branch["recurring_entities"])
        sibling_open_choices = self.count_open_choices_for_node(int(row["from_node_id"]))
        total_choices = self.count_total_choices_for_node(int(row["from_node_id"]))
        choice = choice or {}
        choice_class = choice.get("choice_class")
        has_bound_idea = choice.get("idea_binding") is not None

        score = 50.0
        score += min(active_hooks, 5) * 4.0
        score += min(eligible_major, 2) * 5.0
        score += min(affordances, 3) * 2.5
        score += min(recurring_entities, 5) * 1.5
        score -= min(blocked_major, 3) * 2.0
        score -= node_depth * 1.25
        score -= max(sibling_open_choices - 1, 0) * 2.5
        score -= max(total_choices - 2, 0) * 1.25
        if has_bound_idea:
            score += 6.0
        if choice_class == "inspection":
            score -= 4.0
        elif choice_class in {"progress", "commitment", "location_transition"}:
            score += 2.0
        elif choice_class == "ending":
            score += 1.0
        if branch_shape.get("single_actor_scene_streak", 0) >= self.ISOLATION_PRESSURE_THRESHOLD:
            score += 2.5
        if branch_shape.get("new_character_gap_streak", 0) >= self.NEW_CHARACTER_PRESSURE_THRESHOLD:
            score += 2.5
        if branch_shape.get("same_location_streak", 0) >= self.SAME_LOCATION_PRESSURE_THRESHOLD:
            score += 2.5
        if branch_shape.get("should_prefer_divergence"):
            score += 8.0
        elif branch_shape.get("merge_pressure_level") == "medium":
            score += 3.0
        if global_budget.get("pressure_level") == "soft":
            score += 2.0
        elif global_budget.get("pressure_level") == "hard":
            score += 5.0

        created_at = row.get("created_at")
        age_hours = 0.0
        if created_at:
            try:
                created_dt = datetime.strptime(str(created_at), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age_hours = max((datetime.now(timezone.utc) - created_dt).total_seconds() / 3600.0, 0.0)
                score += min(age_hours / 6.0, 8.0)
            except ValueError:
                pass

        depth_gap = max(max_frontier_depth - node_depth, 0)
        rebalance_bonus = 0.0
        if depth_gap >= 8 and age_hours >= 6.0:
            rebalance_bonus += min((depth_gap - 7) * 0.5, 12.0)
            rebalance_bonus += min((age_hours - 6.0) / 12.0, 4.0)
            score += rebalance_bonus

        if rebalance_bonus >= 6.0:
            reason = "Rebalance target: this older shallow frontier choice has fallen far behind the branch's deepest active paths and should catch up now."
        elif has_bound_idea:
            reason = "High-priority continuity target: this leaf carries a bound medium-range idea and should be revisited."
        elif branch_shape.get("should_prefer_divergence"):
            reason = "Strong divergence target: this branch has quick-merged too often and should open a fresh path now."
        elif branch_shape.get("new_character_gap_streak", 0) >= self.NEW_CHARACTER_PRESSURE_THRESHOLD:
            reason = "New-character target: this branch has gone too long without introducing a brand-new character and should add one now."
        elif branch_shape.get("same_location_streak", 0) >= self.SAME_LOCATION_PRESSURE_THRESHOLD:
            reason = "Location-transition target: this branch has lingered in one place and this menu should open a location_transition path."
        elif branch_shape.get("single_actor_scene_streak", 0) >= self.ISOLATION_PRESSURE_THRESHOLD:
            reason = "Isolation-pressure target: this branch has stayed protagonist-only too long and needs another person or faction pressure onstage."
        elif global_budget.get("pressure_level") in {"soft", "hard"} and sibling_open_choices > 1:
            reason = "Frontier-control target: this parent already has several active siblings, so a merge or closure here would reduce branch sprawl."
        elif eligible_major > 0:
            reason = "Strong candidate: the branch has eligible long-running hooks ready for careful advancement."
        elif affordances > 0:
            reason = "Good breadth target: this branch has unlocked affordances worth recurring naturally."
        elif active_hooks > 0:
            reason = "Good continuity target: this branch carries unresolved hooks that should stay alive."
        else:
            reason = "Breadth target: this older open branch end helps the world expand without tunneling too deep."
        return round(score, 2), reason

    def _resolve_or_create_entity_id(self, *, canon: CanonResolver, fact: Any) -> int:
        if fact.entity_id is not None:
            return fact.entity_id
        if fact.entity_type == "world":
            return 0
        if fact.entity_name:
            try:
                return canon.resolve_entity_id(fact.entity_type, fact.entity_name)
            except ValueError:
                return self._resolve_or_create_named_entity(entity_type=fact.entity_type, name=fact.entity_name)
        raise ValueError("Fact updates require entity_id or entity_name.")

    def _resolve_or_create_named_entity(self, *, entity_type: str, name: str) -> int:
        slug = CanonResolver.slugify(name)
        table_map = {
            "location": "locations",
            "character": "characters",
            "object": "objects",
        }
        table_name = table_map.get(entity_type)
        if table_name is None:
            raise ValueError(f"Unsupported entity type: {entity_type}")
        existing = fetch_one(self.connection, f"SELECT id FROM {table_name} WHERE slug = ?", (slug,))
        if existing is not None:
            return int(existing["id"])
        cursor = self.connection.execute(
            f"INSERT INTO {table_name} (slug, name) VALUES (?, ?)",
            (slug, name.strip()),
        )
        return int(cursor.lastrowid)

    def _apply_inventory_change(
        self,
        *,
        branch_key: str,
        object_id: int,
        action: str,
        quantity: int,
        notes: str | None,
        source_node_id: int,
    ) -> None:
        existing = fetch_one(
            self.connection,
            "SELECT * FROM inventory_entries WHERE branch_key = ? AND object_id = ?",
            (branch_key, object_id),
        )
        delta = quantity if action == "add" else -quantity
        if existing is None:
            next_quantity = max(delta, 0)
            status = "owned" if next_quantity > 0 else "lost"
            self.connection.execute(
                """
                INSERT INTO inventory_entries (branch_key, object_id, quantity, status, source_node_id, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (branch_key, object_id, next_quantity, status, source_node_id, notes),
            )
            return
        next_quantity = max(int(existing["quantity"]) + delta, 0)
        status = "owned" if next_quantity > 0 else "lost"
        self.connection.execute(
            """
            UPDATE inventory_entries
            SET quantity = ?, status = ?, source_node_id = ?, notes = COALESCE(?, notes), updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (next_quantity, status, source_node_id, notes, existing["id"]),
        )

    def _apply_affordance_change(self, *, branch_key: str, change: dict[str, Any]) -> None:
        existing = fetch_one(
            self.connection,
            "SELECT * FROM unlocked_affordances WHERE branch_key = ? AND name = ?",
            (branch_key, change["name"]),
        )
        status_map = {
            "unlock": "unlocked",
            "restore": "unlocked",
            "suspend": "suspended",
            "retire": "retired",
        }
        next_status = status_map[change["action"]]
        serialized_tags = json.dumps(change.get("required_state_tags", []))
        if existing is None:
            self.connection.execute(
                """
                INSERT INTO unlocked_affordances (
                    branch_key, name, description, source_object_id, source_character_id,
                    availability_note, required_state_tags_json, status, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    branch_key,
                    change["name"],
                    change.get("description") or change["name"],
                    change.get("source_object_id"),
                    change.get("source_character_id"),
                    change.get("availability_note"),
                    serialized_tags,
                    next_status,
                    change.get("notes"),
                ),
            )
            return
        self.connection.execute(
            """
            UPDATE unlocked_affordances
            SET description = COALESCE(?, description),
                source_object_id = COALESCE(?, source_object_id),
                source_character_id = COALESCE(?, source_character_id),
                availability_note = COALESCE(?, availability_note),
                required_state_tags_json = ?,
                status = ?,
                notes = COALESCE(?, notes),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                change.get("description"),
                change.get("source_object_id"),
                change.get("source_character_id"),
                change.get("availability_note"),
                serialized_tags,
                next_status,
                change.get("notes"),
                existing["id"],
            ),
        )

    def _apply_relationship_change(self, *, branch_key: str, change: dict[str, Any]) -> None:
        existing = fetch_one(
            self.connection,
            "SELECT * FROM relationship_states WHERE branch_key = ? AND character_id = ?",
            (branch_key, change["character_id"]),
        )
        serialized_tags = json.dumps(change.get("state_tags", []))
        if existing is None:
            self.connection.execute(
                """
                INSERT INTO relationship_states (branch_key, character_id, stance, notes, state_tags_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    branch_key,
                    change["character_id"],
                    change.get("stance", "neutral"),
                    change.get("notes"),
                    serialized_tags,
                ),
            )
            return
        self.connection.execute(
            """
            UPDATE relationship_states
            SET stance = ?, notes = COALESCE(?, notes), state_tags_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                change.get("stance", "neutral"),
                change.get("notes"),
                serialized_tags,
                existing["id"],
            ),
        )
