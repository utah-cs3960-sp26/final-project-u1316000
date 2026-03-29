from __future__ import annotations

import re
import sqlite3
from typing import Any

from app.database import fetch_all, fetch_one


class CanonResolver:
    """Owns canonical world operations and duplicate-safe inserts."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @staticmethod
    def slugify(value: str) -> str:
        value = value.strip().lower()
        value = re.sub(r"[^a-z0-9]+", "-", value)
        value = re.sub(r"-{2,}", "-", value).strip("-")
        return value or "unnamed"

    def list_locations(self) -> list[dict[str, Any]]:
        return fetch_all(self.connection, "SELECT * FROM locations ORDER BY name")

    def list_characters(self) -> list[dict[str, Any]]:
        return fetch_all(
            self.connection,
            """
            SELECT c.*, l.name AS home_location_name
            FROM characters c
            LEFT JOIN locations l ON l.id = c.home_location_id
            ORDER BY c.name
            """,
        )

    def list_objects(self) -> list[dict[str, Any]]:
        return fetch_all(
            self.connection,
            """
            SELECT o.*, l.name AS default_location_name
            FROM objects o
            LEFT JOIN locations l ON l.id = o.default_location_id
            ORDER BY o.name
            """,
        )

    def list_facts(self) -> list[dict[str, Any]]:
        return fetch_all(self.connection, "SELECT * FROM facts ORDER BY id")

    def list_relations(self) -> list[dict[str, Any]]:
        return fetch_all(self.connection, "SELECT * FROM relations ORDER BY id")

    def find_location_by_name(self, name: str) -> dict[str, Any] | None:
        slug = self.slugify(name)
        return fetch_one(self.connection, "SELECT * FROM locations WHERE slug = ?", (slug,))

    def find_character_by_name(self, name: str) -> dict[str, Any] | None:
        slug = self.slugify(name)
        return fetch_one(self.connection, "SELECT * FROM characters WHERE slug = ?", (slug,))

    def find_object_by_name(self, name: str) -> dict[str, Any] | None:
        slug = self.slugify(name)
        return fetch_one(self.connection, "SELECT * FROM objects WHERE slug = ?", (slug,))

    def get_location(self, location_id: int) -> dict[str, Any] | None:
        return fetch_one(self.connection, "SELECT * FROM locations WHERE id = ?", (location_id,))

    def get_character(self, character_id: int) -> dict[str, Any] | None:
        return fetch_one(self.connection, "SELECT * FROM characters WHERE id = ?", (character_id,))

    def get_object(self, object_id: int) -> dict[str, Any] | None:
        return fetch_one(self.connection, "SELECT * FROM objects WHERE id = ?", (object_id,))

    def create_or_get_location(
        self,
        *,
        name: str,
        description: str | None = None,
        canonical_summary: str | None = None,
    ) -> dict[str, Any]:
        existing = self.find_location_by_name(name)
        if existing is not None:
            if description or canonical_summary:
                self._update_entity_metadata_if_missing(
                    table_name="locations",
                    entity_id=int(existing["id"]),
                    description=description,
                    canonical_summary=canonical_summary,
                )
                existing = self.get_location(int(existing["id"]))
            return existing or {}

        slug = self.slugify(name)
        cursor = self.connection.execute(
            """
            INSERT INTO locations (slug, name, description, canonical_summary)
            VALUES (?, ?, ?, ?)
            """,
            (slug, name.strip(), description, canonical_summary),
        )
        self.connection.commit()
        return self.get_location(cursor.lastrowid) or {}

    def create_or_get_character(
        self,
        *,
        name: str,
        description: str | None = None,
        canonical_summary: str | None = None,
        home_location_id: int | None = None,
    ) -> dict[str, Any]:
        existing = self.find_character_by_name(name)
        if existing is not None:
            if home_location_id and not existing.get("home_location_id"):
                self.connection.execute(
                    "UPDATE characters SET home_location_id = ? WHERE id = ?",
                    (home_location_id, existing["id"]),
                )
                self.connection.commit()
                existing = self.get_character(existing["id"])
            if description or canonical_summary:
                self._update_entity_metadata_if_missing(
                    table_name="characters",
                    entity_id=int(existing["id"]),
                    description=description,
                    canonical_summary=canonical_summary,
                )
                existing = self.get_character(int(existing["id"]))
            return existing or {}

        slug = self.slugify(name)
        cursor = self.connection.execute(
            """
            INSERT INTO characters (slug, name, description, home_location_id, canonical_summary)
            VALUES (?, ?, ?, ?, ?)
            """,
            (slug, name.strip(), description, home_location_id, canonical_summary),
        )
        self.connection.commit()
        return self.get_character(cursor.lastrowid) or {}

    def create_or_get_object(
        self,
        *,
        name: str,
        description: str | None = None,
        canonical_summary: str | None = None,
        default_location_id: int | None = None,
    ) -> dict[str, Any]:
        existing = self.find_object_by_name(name)
        if existing is not None:
            if default_location_id and not existing.get("default_location_id"):
                self.connection.execute(
                    "UPDATE objects SET default_location_id = ? WHERE id = ?",
                    (default_location_id, existing["id"]),
                )
                self.connection.commit()
                existing = self.get_object(existing["id"])
            if description or canonical_summary:
                self._update_entity_metadata_if_missing(
                    table_name="objects",
                    entity_id=int(existing["id"]),
                    description=description,
                    canonical_summary=canonical_summary,
                )
                existing = self.get_object(int(existing["id"]))
            return existing or {}

        slug = self.slugify(name)
        cursor = self.connection.execute(
            """
            INSERT INTO objects (slug, name, description, default_location_id, canonical_summary)
            VALUES (?, ?, ?, ?, ?)
            """,
            (slug, name.strip(), description, default_location_id, canonical_summary),
        )
        self.connection.commit()
        return self.get_object(cursor.lastrowid) or {}

    def add_relation(
        self,
        *,
        subject_type: str,
        subject_id: int,
        relation_type: str,
        object_type: str,
        object_id: int,
        notes: str | None = None,
    ) -> dict[str, Any]:
        existing = fetch_one(
            self.connection,
            """
            SELECT * FROM relations
            WHERE subject_type = ? AND subject_id = ? AND relation_type = ? AND object_type = ? AND object_id = ?
            """,
            (subject_type, subject_id, relation_type, object_type, object_id),
        )
        if existing is not None:
            return existing

        cursor = self.connection.execute(
            """
            INSERT INTO relations (subject_type, subject_id, relation_type, object_type, object_id, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (subject_type, subject_id, relation_type, object_type, object_id, notes),
        )
        self.connection.commit()
        return fetch_one(self.connection, "SELECT * FROM relations WHERE id = ?", (cursor.lastrowid,)) or {}

    def add_fact(
        self,
        *,
        entity_type: str,
        entity_id: int,
        fact_text: str,
        is_locked: bool = False,
        source: str = "manual",
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """
            INSERT INTO facts (entity_type, entity_id, fact_text, is_locked, source)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entity_type, entity_id, fact_text.strip(), int(is_locked), source),
        )
        self.connection.commit()
        return fetch_one(self.connection, "SELECT * FROM facts WHERE id = ?", (cursor.lastrowid,)) or {}

    def resolve_entity_id(self, entity_type: str, name: str) -> int:
        if entity_type == "location":
            record = self.find_location_by_name(name)
        elif entity_type == "character":
            record = self.find_character_by_name(name)
        elif entity_type == "object":
            record = self.find_object_by_name(name)
        else:
            raise ValueError(f"Unsupported entity type: {entity_type}")

        if record is None:
            raise ValueError(f"Could not resolve {entity_type} named '{name}'.")
        return int(record["id"])

    def resolve_spatial_relation(self, *, anchor_location_id: int, relation_type: str) -> dict[str, Any] | None:
        return fetch_one(
            self.connection,
            """
            SELECT l.*
            FROM relations r
            JOIN locations l ON l.id = r.subject_id
            WHERE r.subject_type = 'location'
              AND r.object_type = 'location'
              AND r.relation_type = ?
              AND r.object_id = ?
            ORDER BY l.id
            LIMIT 1
            """,
            (relation_type, anchor_location_id),
        )

    def _update_entity_metadata_if_missing(
        self,
        *,
        table_name: str,
        entity_id: int,
        description: str | None,
        canonical_summary: str | None,
    ) -> None:
        self.connection.execute(
            f"""
            UPDATE {table_name}
            SET description = COALESCE(description, ?),
                canonical_summary = COALESCE(canonical_summary, ?)
            WHERE id = ?
            """,
            (description, canonical_summary, entity_id),
        )
        self.connection.commit()
