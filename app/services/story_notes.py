from __future__ import annotations

import sqlite3
from typing import Any

from app.database import fetch_all, fetch_one


class StoryDirectionService:
    """Stores out-of-world planning notes that help future workers steer longer plotlines."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def list_notes(
        self,
        *,
        statuses: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM story_direction_notes"
        params: list[Any] = []
        if statuses:
            query += f" WHERE status IN ({','.join('?' for _ in statuses)})"
            params.extend(statuses)
        query += " ORDER BY status = 'active' DESC, priority DESC, updated_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        return fetch_all(self.connection, query, tuple(params))

    def get_note(self, note_id: int) -> dict[str, Any] | None:
        return fetch_one(self.connection, "SELECT * FROM story_direction_notes WHERE id = ?", (note_id,))

    def create_note(
        self,
        *,
        note_type: str,
        title: str,
        note_text: str,
        status: str = "active",
        priority: int = 2,
        related_entity_type: str | None = None,
        related_entity_id: int | None = None,
        related_hook_id: int | None = None,
        source_branch_key: str | None = None,
        notes: str | None = None,
        created_by: str = "manual",
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """
            INSERT INTO story_direction_notes (
                note_type, title, note_text, status, priority,
                related_entity_type, related_entity_id, related_hook_id,
                source_branch_key, notes, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                note_type.strip(),
                title.strip(),
                note_text.strip(),
                status,
                priority,
                related_entity_type,
                related_entity_id,
                related_hook_id,
                source_branch_key,
                notes,
                created_by,
            ),
        )
        self.connection.commit()
        return self.get_note(int(cursor.lastrowid)) or {}

    def update_note(
        self,
        note_id: int,
        *,
        note_type: str | None = None,
        title: str | None = None,
        note_text: str | None = None,
        status: str | None = None,
        priority: int | None = None,
        related_entity_type: str | None = None,
        related_entity_id: int | None = None,
        related_hook_id: int | None = None,
        source_branch_key: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get_note(note_id)
        if existing is None:
            raise ValueError(f"Unknown story direction note id: {note_id}")
        self.connection.execute(
            """
            UPDATE story_direction_notes
            SET note_type = ?,
                title = ?,
                note_text = ?,
                status = ?,
                priority = ?,
                related_entity_type = ?,
                related_entity_id = ?,
                related_hook_id = ?,
                source_branch_key = ?,
                notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                note_type.strip() if note_type is not None else existing["note_type"],
                title.strip() if title is not None else existing["title"],
                note_text.strip() if note_text is not None else existing["note_text"],
                status if status is not None else existing["status"],
                priority if priority is not None else existing["priority"],
                related_entity_type if related_entity_type is not None else existing["related_entity_type"],
                related_entity_id if related_entity_id is not None else existing["related_entity_id"],
                related_hook_id if related_hook_id is not None else existing["related_hook_id"],
                source_branch_key if source_branch_key is not None else existing["source_branch_key"],
                notes if notes is not None else existing["notes"],
                note_id,
            ),
        )
        self.connection.commit()
        return self.get_note(note_id) or {}
