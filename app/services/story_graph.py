from __future__ import annotations

import sqlite3
from typing import Any

from app.database import fetch_all, fetch_one


class StoryGraphService:
    """Owns story nodes, choices, and their links to canonical entities."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def list_story_nodes(self) -> list[dict[str, Any]]:
        nodes = fetch_all(self.connection, "SELECT * FROM story_nodes ORDER BY id")
        for node in nodes:
            node["choices"] = fetch_all(
                self.connection,
                """
                SELECT * FROM choices
                WHERE from_node_id = ?
                ORDER BY id
                """,
                (node["id"],),
            )
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
        referenced_entities: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """
            INSERT INTO story_nodes (branch_key, parent_node_id, title, scene_text, summary)
            VALUES (?, ?, ?, ?, ?)
            """,
            (branch_key, parent_node_id, title, scene_text.strip(), summary),
        )
        node_id = cursor.lastrowid
        for entity in referenced_entities or []:
            self.link_entity(
                story_node_id=node_id,
                entity_type=entity["entity_type"],
                entity_id=entity["entity_id"],
                role=entity.get("role", "mentioned"),
            )
        self.connection.commit()
        return self.get_story_node(node_id) or {}

    def get_story_node(self, node_id: int) -> dict[str, Any] | None:
        node = fetch_one(self.connection, "SELECT * FROM story_nodes WHERE id = ?", (node_id,))
        if node is None:
            return None
        node["choices"] = fetch_all(
            self.connection,
            "SELECT * FROM choices WHERE from_node_id = ? ORDER BY id",
            (node_id,),
        )
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
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """
            INSERT INTO choices (from_node_id, choice_text, to_node_id, status, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (from_node_id, choice_text.strip(), to_node_id, status, notes),
        )
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
            "relations",
            "facts",
            "story_nodes",
            "choices",
            "assets",
            "generation_jobs",
        ]
        counts: dict[str, int] = {}
        for table_name in table_names:
            row = fetch_one(self.connection, f"SELECT COUNT(*) AS count FROM {table_name}")
            counts[table_name] = int(row["count"]) if row else 0
        return counts

