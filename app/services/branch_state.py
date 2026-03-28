from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.database import fetch_all, fetch_one


class BranchStateService:
    """Tracks branch-specific inventory, affordances, tags, relationships, and story hooks."""

    def __init__(self, connection: sqlite3.Connection, act_phase_ranges: dict[str, dict[str, int]]) -> None:
        self.connection = connection
        self.act_phase_ranges = act_phase_ranges

    def ensure_branch(self, branch_key: str) -> dict[str, Any]:
        existing = self._read_branch_row(branch_key)
        if existing is not None:
            return existing
        self.connection.execute(
            """
            INSERT INTO branch_state (branch_key, act_phase, branch_depth)
            VALUES (?, 'early', 0)
            """,
            (branch_key,),
        )
        self.connection.commit()
        return self._read_branch_row(branch_key) or {}

    def sync_branch_progress(self, branch_key: str, latest_story_node_id: int | None = None) -> dict[str, Any]:
        self.ensure_branch(branch_key)
        depth_row = fetch_one(
            self.connection,
            "SELECT COUNT(*) AS count FROM story_nodes WHERE branch_key = ?",
            (branch_key,),
        )
        branch_depth = max(int(depth_row["count"]) - 1, 0) if depth_row else 0
        act_phase = self.phase_for_depth(branch_depth)
        if latest_story_node_id is None:
            latest_node = fetch_one(
                self.connection,
                "SELECT id FROM story_nodes WHERE branch_key = ? ORDER BY id DESC LIMIT 1",
                (branch_key,),
            )
            latest_story_node_id = int(latest_node["id"]) if latest_node else None

        self.connection.execute(
            """
            UPDATE branch_state
            SET branch_depth = ?, act_phase = ?, latest_story_node_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE branch_key = ?
            """,
            (branch_depth, act_phase, latest_story_node_id, branch_key),
        )
        self.connection.commit()
        return self.get_branch_state(branch_key)

    def phase_for_depth(self, depth: int) -> str:
        for phase, bounds in self.act_phase_ranges.items():
            minimum = int(bounds.get("min_depth", 0))
            maximum = int(bounds.get("max_depth", 10**9))
            if minimum <= depth <= maximum:
                return phase
        return "late"

    def get_branch_state(self, branch_key: str) -> dict[str, Any]:
        branch = self._read_branch_row(branch_key) or self.ensure_branch(branch_key)
        branch["inventory"] = self.list_inventory(branch_key)
        branch["affordances"] = self.list_affordances(branch_key)
        branch["relationships"] = self.list_relationships(branch_key)
        branch["tags"] = self.list_branch_tags(branch_key)
        branch["active_hooks"] = self.list_hooks(branch_key, statuses=["active", "payoff_ready", "blocked"])
        branch["resolved_hooks"] = self.list_hooks(branch_key, statuses=["resolved"])
        branch["eligible_major_hooks"] = self.list_eligible_hooks(branch_key, importance="major")
        branch["blocked_major_hooks"] = self.list_ineligible_hooks(branch_key, importance="major")
        branch["recurring_entities"] = self.list_recurring_entities(branch_key)
        return branch

    def add_inventory_entry(
        self,
        *,
        branch_key: str,
        object_id: int,
        quantity: int = 1,
        status: str = "owned",
        source_node_id: int | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_branch(branch_key)
        existing = fetch_one(
            self.connection,
            "SELECT * FROM inventory_entries WHERE branch_key = ? AND object_id = ?",
            (branch_key, object_id),
        )
        if existing is None:
            cursor = self.connection.execute(
                """
                INSERT INTO inventory_entries (branch_key, object_id, quantity, status, source_node_id, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (branch_key, object_id, quantity, status, source_node_id, notes),
            )
            self.connection.commit()
            return fetch_one(self.connection, "SELECT * FROM inventory_entries WHERE id = ?", (cursor.lastrowid,)) or {}

        next_quantity = max(int(existing["quantity"]) + quantity, 0)
        self.connection.execute(
            """
            UPDATE inventory_entries
            SET quantity = ?, status = ?, source_node_id = COALESCE(?, source_node_id), notes = COALESCE(?, notes), updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (next_quantity, status, source_node_id, notes, existing["id"]),
        )
        self.connection.commit()
        return fetch_one(self.connection, "SELECT * FROM inventory_entries WHERE id = ?", (existing["id"],)) or {}

    def list_inventory(self, branch_key: str) -> list[dict[str, Any]]:
        return fetch_all(
            self.connection,
            """
            SELECT i.*, o.name AS object_name, o.canonical_summary AS object_summary
            FROM inventory_entries i
            JOIN objects o ON o.id = i.object_id
            WHERE i.branch_key = ?
            ORDER BY o.name
            """,
            (branch_key,),
        )

    def set_affordance(
        self,
        *,
        branch_key: str,
        name: str,
        description: str,
        source_object_id: int | None = None,
        source_character_id: int | None = None,
        availability_note: str | None = None,
        required_state_tags: list[str] | None = None,
        status: str = "unlocked",
        notes: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_branch(branch_key)
        existing = fetch_one(
            self.connection,
            "SELECT * FROM unlocked_affordances WHERE branch_key = ? AND name = ?",
            (branch_key, name.strip()),
        )
        serialized_tags = json.dumps(required_state_tags or [])
        if existing is None:
            cursor = self.connection.execute(
                """
                INSERT INTO unlocked_affordances (
                    branch_key, name, description, source_object_id, source_character_id,
                    availability_note, required_state_tags_json, status, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    branch_key,
                    name.strip(),
                    description.strip(),
                    source_object_id,
                    source_character_id,
                    availability_note,
                    serialized_tags,
                    status,
                    notes,
                ),
            )
            self.connection.commit()
            return self._affordance_row(cursor.lastrowid) or {}

        self.connection.execute(
            """
            UPDATE unlocked_affordances
            SET description = ?, source_object_id = COALESCE(?, source_object_id),
                source_character_id = COALESCE(?, source_character_id),
                availability_note = COALESCE(?, availability_note),
                required_state_tags_json = ?, status = ?, notes = COALESCE(?, notes),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                description.strip(),
                source_object_id,
                source_character_id,
                availability_note,
                serialized_tags,
                status,
                notes,
                existing["id"],
            ),
        )
        self.connection.commit()
        return self._affordance_row(existing["id"]) or {}

    def list_affordances(self, branch_key: str, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT *
            FROM unlocked_affordances
            WHERE branch_key = ?
        """
        params: list[Any] = [branch_key]
        if statuses:
            query += f" AND status IN ({','.join('?' for _ in statuses)})"
            params.extend(statuses)
        query += " ORDER BY id"
        rows = fetch_all(self.connection, query, tuple(params))
        for row in rows:
            row["required_state_tags"] = json.loads(row["required_state_tags_json"] or "[]")
        return rows

    def list_available_affordances(self, branch_key: str) -> list[dict[str, Any]]:
        tags = {row["tag"] for row in self.list_branch_tags(branch_key)}
        available: list[dict[str, Any]] = []
        for affordance in self.list_affordances(branch_key, statuses=["unlocked"]):
            required_tags = set(affordance.get("required_state_tags", []))
            if required_tags.issubset(tags):
                available.append(affordance)
        return available

    def set_relationship_state(
        self,
        *,
        branch_key: str,
        character_id: int,
        stance: str = "neutral",
        notes: str | None = None,
        state_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self.ensure_branch(branch_key)
        existing = fetch_one(
            self.connection,
            "SELECT * FROM relationship_states WHERE branch_key = ? AND character_id = ?",
            (branch_key, character_id),
        )
        serialized_tags = json.dumps(state_tags or [])
        if existing is None:
            cursor = self.connection.execute(
                """
                INSERT INTO relationship_states (branch_key, character_id, stance, notes, state_tags_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (branch_key, character_id, stance, notes, serialized_tags),
            )
            self.connection.commit()
            return self._relationship_row(cursor.lastrowid) or {}

        self.connection.execute(
            """
            UPDATE relationship_states
            SET stance = ?, notes = COALESCE(?, notes), state_tags_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (stance, notes, serialized_tags, existing["id"]),
        )
        self.connection.commit()
        return self._relationship_row(existing["id"]) or {}

    def list_relationships(self, branch_key: str) -> list[dict[str, Any]]:
        rows = fetch_all(
            self.connection,
            """
            SELECT r.*, c.name AS character_name
            FROM relationship_states r
            JOIN characters c ON c.id = r.character_id
            WHERE r.branch_key = ?
            ORDER BY c.name
            """,
            (branch_key,),
        )
        for row in rows:
            row["state_tags"] = json.loads(row["state_tags_json"] or "[]")
        return rows

    def add_branch_tag(
        self,
        *,
        branch_key: str,
        tag: str,
        tag_type: str = "state",
        source: str = "manual",
        notes: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_branch(branch_key)
        existing = fetch_one(
            self.connection,
            "SELECT * FROM branch_tags WHERE branch_key = ? AND tag = ? AND tag_type = ?",
            (branch_key, tag.strip(), tag_type),
        )
        if existing is not None:
            return existing
        cursor = self.connection.execute(
            """
            INSERT INTO branch_tags (branch_key, tag, tag_type, source, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (branch_key, tag.strip(), tag_type, source, notes),
        )
        self.connection.commit()
        return fetch_one(self.connection, "SELECT * FROM branch_tags WHERE id = ?", (cursor.lastrowid,)) or {}

    def list_branch_tags(self, branch_key: str, tag_type: str | None = None) -> list[dict[str, Any]]:
        if tag_type is None:
            return fetch_all(
                self.connection,
                "SELECT * FROM branch_tags WHERE branch_key = ? ORDER BY tag_type, tag",
                (branch_key,),
            )
        return fetch_all(
            self.connection,
            "SELECT * FROM branch_tags WHERE branch_key = ? AND tag_type = ? ORDER BY tag",
            (branch_key, tag_type),
        )

    def create_hook(
        self,
        *,
        branch_key: str,
        hook_type: str,
        importance: str,
        summary: str,
        linked_entity_type: str | None = None,
        linked_entity_id: int | None = None,
        introduced_at_depth: int | None = None,
        min_distance_to_payoff: int = 0,
        required_clue_tags: list[str] | None = None,
        required_state_tags: list[str] | None = None,
        status: str = "active",
        notes: str | None = None,
    ) -> dict[str, Any]:
        branch = self.sync_branch_progress(branch_key)
        cursor = self.connection.execute(
            """
            INSERT INTO story_hooks (
                branch_key, hook_type, importance, summary, linked_entity_type, linked_entity_id,
                introduced_at_depth, min_distance_to_payoff, required_clue_tags_json, required_state_tags_json,
                status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                branch_key,
                hook_type,
                importance,
                summary.strip(),
                linked_entity_type,
                linked_entity_id,
                branch["branch_depth"] if introduced_at_depth is None else introduced_at_depth,
                min_distance_to_payoff,
                json.dumps(required_clue_tags or []),
                json.dumps(required_state_tags or []),
                status,
                notes,
            ),
        )
        self.connection.commit()
        return self.get_hook(int(cursor.lastrowid)) or {}

    def update_hook(
        self,
        *,
        hook_id: int,
        status: str,
        progress_note: str | None = None,
        resolution_text: str | None = None,
        add_required_clue_tags: list[str] | None = None,
        add_required_state_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        hook = self.get_hook(hook_id)
        if hook is None:
            raise ValueError(f"Unknown hook id: {hook_id}")
        clue_tags = sorted(set(hook["required_clue_tags"]) | set(add_required_clue_tags or []))
        state_tags = sorted(set(hook["required_state_tags"]) | set(add_required_state_tags or []))
        self.connection.execute(
            """
            UPDATE story_hooks
            SET status = ?, notes = COALESCE(?, notes), resolution_text = COALESCE(?, resolution_text),
                required_clue_tags_json = ?, required_state_tags_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                status,
                progress_note,
                resolution_text,
                json.dumps(clue_tags),
                json.dumps(state_tags),
                hook_id,
            ),
        )
        self.connection.commit()
        return self.get_hook(hook_id) or {}

    def get_hook(self, hook_id: int) -> dict[str, Any] | None:
        hook = fetch_one(self.connection, "SELECT * FROM story_hooks WHERE id = ?", (hook_id,))
        if hook is None:
            return None
        hook["required_clue_tags"] = json.loads(hook["required_clue_tags_json"] or "[]")
        hook["required_state_tags"] = json.loads(hook["required_state_tags_json"] or "[]")
        return hook

    def list_hooks(self, branch_key: str, statuses: list[str] | None = None, importance: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM story_hooks WHERE branch_key = ?"
        params: list[Any] = [branch_key]
        if statuses:
            query += f" AND status IN ({','.join('?' for _ in statuses)})"
            params.extend(statuses)
        if importance:
            query += " AND importance = ?"
            params.append(importance)
        query += " ORDER BY id"
        rows = fetch_all(self.connection, query, tuple(params))
        for row in rows:
            row["required_clue_tags"] = json.loads(row["required_clue_tags_json"] or "[]")
            row["required_state_tags"] = json.loads(row["required_state_tags_json"] or "[]")
        return rows

    def list_hooks_with_readiness(
        self,
        branch_key: str,
        *,
        statuses: list[str] | None = None,
        importance: str | None = None,
    ) -> list[dict[str, Any]]:
        branch = self._read_branch_row(branch_key) or self.ensure_branch(branch_key)
        state_tags = {row["tag"] for row in self.list_branch_tags(branch_key, tag_type="state")}
        clue_tags = {row["tag"] for row in self.list_branch_tags(branch_key, tag_type="clue")}
        hooks = self.list_hooks(branch_key, statuses=statuses, importance=importance)
        for hook in hooks:
            hook["readiness"] = self._hook_readiness(
                hook,
                int(branch["branch_depth"]),
                state_tags,
                clue_tags,
            )
        return hooks

    def list_eligible_hooks(self, branch_key: str, importance: str | None = None) -> list[dict[str, Any]]:
        branch = self._read_branch_row(branch_key) or self.ensure_branch(branch_key)
        state_tags = {row["tag"] for row in self.list_branch_tags(branch_key, tag_type="state")}
        clue_tags = {row["tag"] for row in self.list_branch_tags(branch_key, tag_type="clue")}
        eligible: list[dict[str, Any]] = []
        for hook in self.list_hooks(branch_key, statuses=["active", "payoff_ready"], importance=importance):
            readiness = self._hook_readiness(hook, branch["branch_depth"], state_tags, clue_tags)
            hook["readiness"] = readiness
            if readiness["eligible"]:
                eligible.append(hook)
        return eligible

    def list_ineligible_hooks(self, branch_key: str, importance: str | None = None) -> list[dict[str, Any]]:
        branch = self._read_branch_row(branch_key) or self.ensure_branch(branch_key)
        state_tags = {row["tag"] for row in self.list_branch_tags(branch_key, tag_type="state")}
        clue_tags = {row["tag"] for row in self.list_branch_tags(branch_key, tag_type="clue")}
        ineligible: list[dict[str, Any]] = []
        for hook in self.list_hooks(branch_key, statuses=["active", "payoff_ready", "blocked"], importance=importance):
            readiness = self._hook_readiness(hook, branch["branch_depth"], state_tags, clue_tags)
            hook["readiness"] = readiness
            if not readiness["eligible"]:
                ineligible.append(hook)
        return ineligible

    def list_recurring_entities(self, branch_key: str) -> list[dict[str, Any]]:
        return fetch_all(
            self.connection,
            """
            SELECT entity_type, entity_id, COUNT(*) AS appearances
            FROM node_entities ne
            JOIN story_nodes sn ON sn.id = ne.story_node_id
            WHERE sn.branch_key = ?
            GROUP BY entity_type, entity_id
            HAVING COUNT(*) >= 1
            ORDER BY appearances DESC, entity_type, entity_id
            """,
            (branch_key,),
        )

    def _hook_is_eligible(
        self,
        hook: dict[str, Any],
        branch_depth: int,
        state_tags: set[str],
        clue_tags: set[str],
    ) -> bool:
        return self._hook_readiness(hook, branch_depth, state_tags, clue_tags)["eligible"]

    def _hook_readiness(
        self,
        hook: dict[str, Any],
        branch_depth: int,
        state_tags: set[str],
        clue_tags: set[str],
    ) -> dict[str, Any]:
        required_depth = int(hook["introduced_at_depth"]) + int(hook["min_distance_to_payoff"])
        distance_ready = branch_depth >= required_depth
        required_state_tags = set(hook.get("required_state_tags", []))
        required_clue_tags = set(hook.get("required_clue_tags", []))
        missing_state_tags = sorted(required_state_tags - state_tags)
        missing_clue_tags = sorted(required_clue_tags - clue_tags)
        conditions_ready = not missing_state_tags and not missing_clue_tags
        return {
            "eligible": distance_ready and conditions_ready and hook["status"] != "resolved",
            "distance_ready": distance_ready,
            "required_depth": required_depth,
            "current_depth": branch_depth,
            "remaining_distance": max(required_depth - branch_depth, 0),
            "conditions_ready": conditions_ready,
            "missing_state_tags": missing_state_tags,
            "missing_clue_tags": missing_clue_tags,
        }

    def _affordance_row(self, affordance_id: int) -> dict[str, Any] | None:
        affordance = fetch_one(self.connection, "SELECT * FROM unlocked_affordances WHERE id = ?", (affordance_id,))
        if affordance is None:
            return None
        affordance["required_state_tags"] = json.loads(affordance["required_state_tags_json"] or "[]")
        return affordance

    def _read_branch_row(self, branch_key: str) -> dict[str, Any] | None:
        return fetch_one(self.connection, "SELECT * FROM branch_state WHERE branch_key = ?", (branch_key,))

    def _relationship_row(self, relationship_id: int) -> dict[str, Any] | None:
        relationship = fetch_one(self.connection, "SELECT * FROM relationship_states WHERE id = ?", (relationship_id,))
        if relationship is None:
            return None
        relationship["state_tags"] = json.loads(relationship["state_tags_json"] or "[]")
        return relationship
