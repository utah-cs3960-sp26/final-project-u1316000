from __future__ import annotations

import sqlite3
from typing import Any

from app.database import fetch_all, fetch_one


class WorldbuildingService:
    """Stores reusable world-pressure memory outside the playable scene graph."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def list_notes(
        self,
        *,
        statuses: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM worldbuilding_notes"
        params: list[Any] = []
        if statuses:
            query += f" WHERE status IN ({','.join('?' for _ in statuses)})"
            params.extend(statuses)
        query += " ORDER BY status = 'active' DESC, pressure DESC, priority DESC, updated_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        return fetch_all(self.connection, query, tuple(params))

    def get_note(self, note_id: int) -> dict[str, Any] | None:
        return fetch_one(self.connection, "SELECT * FROM worldbuilding_notes WHERE id = ?", (note_id,))

    def find_note_by_title(
        self,
        *,
        note_type: str,
        title: str,
        source_branch_key: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_type = note_type.strip().lower()
        normalized_title = title.strip().lower()
        rows = fetch_all(
            self.connection,
            """
            SELECT *
            FROM worldbuilding_notes
            WHERE lower(trim(title)) = ?
            ORDER BY
                CASE
                    WHEN lower(trim(note_type)) = ? THEN 0
                    ELSE 1
                END,
                CASE
                    WHEN source_branch_key = ? THEN 0
                    WHEN source_branch_key IS NULL THEN 1
                    ELSE 2
                END,
                status = 'active' DESC,
                pressure DESC,
                priority DESC,
                id ASC
            """,
            (normalized_title, normalized_type, source_branch_key),
        )
        return rows[0] if rows else None

    def create_note(
        self,
        *,
        note_type: str,
        title: str,
        note_text: str,
        status: str = "active",
        priority: int = 2,
        pressure: int = 2,
        source_branch_key: str | None = None,
        notes: str | None = None,
        created_by: str = "manual",
    ) -> dict[str, Any]:
        existing = self.find_note_by_title(
            note_type=note_type,
            title=title,
            source_branch_key=source_branch_key,
        )
        if existing is not None:
            return self.update_note(
                int(existing["id"]),
                note_type=note_type,
                title=title,
                note_text=note_text,
                status=status,
                priority=priority,
                pressure=pressure,
                source_branch_key=source_branch_key,
                notes=notes,
            )
        cursor = self.connection.execute(
            """
            INSERT INTO worldbuilding_notes (
                note_type, title, note_text, status, priority, pressure,
                source_branch_key, notes, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                note_type.strip(),
                title.strip(),
                note_text.strip(),
                status,
                priority,
                pressure,
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
        pressure: int | None = None,
        source_branch_key: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get_note(note_id)
        if existing is None:
            raise ValueError(f"Unknown worldbuilding note id: {note_id}")
        self.connection.execute(
            """
            UPDATE worldbuilding_notes
            SET note_type = ?,
                title = ?,
                note_text = ?,
                status = ?,
                priority = ?,
                pressure = ?,
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
                pressure if pressure is not None else existing["pressure"],
                source_branch_key if source_branch_key is not None else existing["source_branch_key"],
                notes if notes is not None else existing["notes"],
                note_id,
            ),
        )
        self.connection.commit()
        return self.get_note(note_id) or {}
