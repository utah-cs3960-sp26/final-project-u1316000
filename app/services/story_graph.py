from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.database import fetch_all, fetch_one
from app.models import GenerationCandidate
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver


class StoryGraphService:
    """Owns story nodes, choices, and their links to canonical entities."""

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
        return fetch_all(self.connection, "SELECT * FROM choices ORDER BY id")

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
                scene_choices.append(
                    {
                        "id": int(choice["id"]),
                        "label": choice["choice_text"],
                        "target": str(choice["to_node_id"]) if choice["to_node_id"] is not None else None,
                        "resolved": choice["to_node_id"] is not None,
                        "status": choice["status"],
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
        dialogue_lines: list[dict[str, Any]] | None = None,
        referenced_entities: list[dict[str, Any]] | None = None,
        present_entities: list[dict[str, Any]] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """
            INSERT INTO story_nodes (branch_key, parent_node_id, title, scene_text, summary, dialogue_lines_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (branch_key, parent_node_id, title, scene_text.strip(), summary, json.dumps(dialogue_lines or [])),
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

    def list_frontier(
        self,
        *,
        branch_state_service: BranchStateService,
        branch_key: str | None = None,
        limit: int = 20,
        mode: str = "auto",
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
        for row in rows:
            branch = branch_state_service.get_branch_state(row["branch_key"])
            score, reason = self._score_frontier_item(row=row, branch=branch)
            item = {
                "branch_key": row["branch_key"],
                "from_node_id": row["from_node_id"],
                "choice_id": row["id"],
                "choice_text": row["choice_text"],
                "depth": branch["branch_depth"],
                "current_act_phase": branch["act_phase"],
                "branch_summary": row["from_node_summary"] or row["from_node_title"] or row["from_node_text"][:200],
                "active_hooks": branch["active_hooks"],
                "eligible_major_hooks": branch["eligible_major_hooks"],
                "blocked_major_hooks": branch["blocked_major_hooks"],
                "available_affordances": branch_state_service.list_available_affordances(row["branch_key"]),
                "recurring_entities": branch["recurring_entities"],
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
        with self.connection:
            node = self.create_story_node(
                branch_key=request_branch_key,
                title=candidate.scene_title,
                scene_text=candidate.scene_text,
                summary=candidate.scene_summary,
                parent_node_id=parent_node_id,
                dialogue_lines=[line.model_dump() for line in candidate.dialogue_lines],
                referenced_entities=[reference.model_dump() for reference in candidate.entity_references],
                present_entities=[entity.model_dump() for entity in candidate.scene_present_entities],
                commit=False,
            )
            new_node_id = int(node["id"])

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
            for choice in candidate.choices:
                target_node_id = choice.target_node_id
                if target_node_id is not None:
                    target_node = self.get_story_node(target_node_id)
                    if target_node is None:
                        raise ValueError(f"Unknown merge target node id: {target_node_id}")
                    if target_node["branch_key"] != request_branch_key:
                        raise ValueError("Merged choice target must belong to the same branch.")
                created_choices.append(
                    self.create_choice(
                        from_node_id=new_node_id,
                        choice_text=choice.choice_text,
                        to_node_id=target_node_id,
                        status="fulfilled" if target_node_id is not None else "open",
                        notes=(
                            json.dumps(
                                {
                                    "required_affordances": choice.required_affordances,
                                    "notes": choice.notes,
                                    "target_node_id": target_node_id,
                                }
                            )
                            if choice.required_affordances or choice.notes
                            or target_node_id is not None
                            else None
                        ),
                        commit=False,
                    )
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

            for hook in candidate.new_hooks:
                introduced_at_depth = int(branch_state_service.ensure_branch(request_branch_key)["branch_depth"]) + 1
                self.connection.execute(
                    """
                    INSERT INTO story_hooks (
                        branch_key, hook_type, importance, summary, linked_entity_type, linked_entity_id,
                        introduced_at_depth, min_distance_to_payoff, required_clue_tags_json, required_state_tags_json,
                        status, notes
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (
                        request_branch_key,
                        hook.hook_type,
                        hook.importance,
                        hook.summary,
                        hook.linked_entity_type,
                        hook.linked_entity_id,
                        introduced_at_depth,
                        hook.min_distance_to_payoff,
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
                        required_clue_tags_json = ?, required_state_tags_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        hook_update.status,
                        hook_update.progress_note,
                        hook_update.resolution_text,
                        json.dumps(updated_clue_tags),
                        json.dumps(updated_state_tags),
                        hook_update.hook_id,
                    ),
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
        ]
        counts: dict[str, int] = {}
        for table_name in table_names:
            row = fetch_one(self.connection, f"SELECT COUNT(*) AS count FROM {table_name}")
            counts[table_name] = int(row["count"]) if row else 0
        return counts

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
        return [{"speaker": "Narrator", "text": node["scene_text"]}]

    def _score_frontier_item(self, *, row: dict[str, Any], branch: dict[str, Any]) -> tuple[float, str]:
        active_hooks = len(branch["active_hooks"])
        eligible_major = len(branch["eligible_major_hooks"])
        blocked_major = len(branch["blocked_major_hooks"])
        affordances = len(branch["affordances"])
        recurring_entities = len(branch["recurring_entities"])
        depth = int(branch["branch_depth"])

        score = 50.0
        score += min(active_hooks, 5) * 4.0
        score += min(eligible_major, 2) * 5.0
        score += min(affordances, 3) * 2.5
        score += min(recurring_entities, 5) * 1.5
        score -= min(blocked_major, 3) * 2.0
        score -= depth * 1.25

        if eligible_major > 0:
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
