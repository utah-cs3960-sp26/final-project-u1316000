from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS locations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        description TEXT,
        canonical_summary TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS characters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        description TEXT,
        home_location_id INTEGER,
        canonical_summary TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (home_location_id) REFERENCES locations(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS objects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        description TEXT,
        default_location_id INTEGER,
        canonical_summary TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (default_location_id) REFERENCES locations(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_type TEXT NOT NULL,
        subject_id INTEGER NOT NULL,
        relation_type TEXT NOT NULL,
        object_type TEXT NOT NULL,
        object_id INTEGER NOT NULL,
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(subject_type, subject_id, relation_type, object_type, object_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        fact_text TEXT NOT NULL,
        is_locked INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'manual',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS story_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_key TEXT NOT NULL DEFAULT 'default',
        parent_node_id INTEGER,
        title TEXT,
        scene_text TEXT NOT NULL,
        summary TEXT,
        dialogue_lines_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (parent_node_id) REFERENCES story_nodes(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS choices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_node_id INTEGER NOT NULL,
        choice_text TEXT NOT NULL,
        to_node_id INTEGER,
        status TEXT NOT NULL DEFAULT 'open',
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (from_node_id) REFERENCES story_nodes(id),
        FOREIGN KEY (to_node_id) REFERENCES story_nodes(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS node_entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        story_node_id INTEGER NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        role TEXT NOT NULL DEFAULT 'mentioned',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(story_node_id, entity_type, entity_id, role),
        FOREIGN KEY (story_node_id) REFERENCES story_nodes(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS story_node_present_entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        story_node_id INTEGER NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        slot TEXT NOT NULL,
        scale REAL,
        offset_x_percent REAL NOT NULL DEFAULT 0,
        offset_y_percent REAL NOT NULL DEFAULT 0,
        focus INTEGER NOT NULL DEFAULT 0,
        hidden_on_lines_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (story_node_id) REFERENCES story_nodes(id),
        UNIQUE(story_node_id, entity_type, entity_id, slot)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        asset_kind TEXT NOT NULL,
        file_path TEXT NOT NULL,
        display_class TEXT,
        normalization_json TEXT NOT NULL DEFAULT '{}',
        prompt_text TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generation_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        payload_json TEXT,
        result_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS branch_state (
        branch_key TEXT PRIMARY KEY,
        act_phase TEXT NOT NULL DEFAULT 'early',
        branch_depth INTEGER NOT NULL DEFAULT 0,
        latest_story_node_id INTEGER,
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (latest_story_node_id) REFERENCES story_nodes(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_key TEXT NOT NULL,
        object_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'owned',
        source_node_id INTEGER,
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (branch_key) REFERENCES branch_state(branch_key),
        FOREIGN KEY (object_id) REFERENCES objects(id),
        FOREIGN KEY (source_node_id) REFERENCES story_nodes(id),
        UNIQUE(branch_key, object_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS unlocked_affordances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_key TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL,
        source_object_id INTEGER,
        source_character_id INTEGER,
        availability_note TEXT,
        required_state_tags_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'unlocked',
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (branch_key) REFERENCES branch_state(branch_key),
        FOREIGN KEY (source_object_id) REFERENCES objects(id),
        FOREIGN KEY (source_character_id) REFERENCES characters(id),
        UNIQUE(branch_key, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relationship_states (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_key TEXT NOT NULL,
        character_id INTEGER NOT NULL,
        stance TEXT NOT NULL DEFAULT 'neutral',
        notes TEXT,
        state_tags_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (branch_key) REFERENCES branch_state(branch_key),
        FOREIGN KEY (character_id) REFERENCES characters(id),
        UNIQUE(branch_key, character_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS branch_tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_key TEXT NOT NULL,
        tag TEXT NOT NULL,
        tag_type TEXT NOT NULL DEFAULT 'state',
        source TEXT NOT NULL DEFAULT 'manual',
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (branch_key) REFERENCES branch_state(branch_key),
        UNIQUE(branch_key, tag, tag_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS story_hooks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_key TEXT NOT NULL,
        hook_type TEXT NOT NULL,
        importance TEXT NOT NULL DEFAULT 'minor',
        summary TEXT NOT NULL,
        payoff_concept TEXT,
        must_not_imply_json TEXT NOT NULL DEFAULT '[]',
        linked_entity_type TEXT,
        linked_entity_id INTEGER,
        introduced_at_depth INTEGER NOT NULL DEFAULT 0,
        min_distance_to_payoff INTEGER NOT NULL DEFAULT 0,
        min_distance_to_next_development INTEGER NOT NULL DEFAULT 0,
        last_development_depth INTEGER NOT NULL DEFAULT 0,
        required_clue_tags_json TEXT NOT NULL DEFAULT '[]',
        required_state_tags_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'active',
        notes TEXT,
        resolution_text TEXT,
        blocked_reason TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (branch_key) REFERENCES branch_state(branch_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS story_direction_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        note_type TEXT NOT NULL DEFAULT 'plotline',
        title TEXT NOT NULL,
        note_text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        priority INTEGER NOT NULL DEFAULT 2,
        related_entity_type TEXT,
        related_entity_id INTEGER,
        related_hook_id INTEGER,
        source_branch_key TEXT,
        notes TEXT,
        created_by TEXT NOT NULL DEFAULT 'manual',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worldbuilding_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        note_type TEXT NOT NULL DEFAULT 'world_pressure',
        title TEXT NOT NULL,
        note_text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        priority INTEGER NOT NULL DEFAULT 2,
        pressure INTEGER NOT NULL DEFAULT 2,
        source_branch_key TEXT,
        notes TEXT,
        created_by TEXT NOT NULL DEFAULT 'manual',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS loop_runtime_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        normal_runs_since_plan INTEGER NOT NULL DEFAULT 0,
        last_run_mode TEXT NOT NULL DEFAULT 'normal',
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
]


def connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def bootstrap_database(database_path: str | Path) -> None:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        _ensure_column(connection, "story_nodes", "dialogue_lines_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(connection, "story_node_present_entities", "offset_x_percent", "REAL NOT NULL DEFAULT 0")
        _ensure_column(connection, "story_node_present_entities", "offset_y_percent", "REAL NOT NULL DEFAULT 0")
        _ensure_column(connection, "assets", "display_class", "TEXT")
        _ensure_column(connection, "assets", "normalization_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(connection, "story_hooks", "payoff_concept", "TEXT")
        _ensure_column(connection, "story_hooks", "must_not_imply_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(connection, "story_hooks", "min_distance_to_next_development", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "story_hooks", "last_development_depth", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "worldbuilding_notes", "pressure", "INTEGER NOT NULL DEFAULT 2")
        connection.execute(
            """
            INSERT INTO loop_runtime_state (id, normal_runs_since_plan, last_run_mode)
            VALUES (1, 0, 'normal')
            ON CONFLICT(id) DO NOTHING
            """
        )
        connection.commit()


def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def fetch_all(connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def fetch_one(connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = connection.execute(query, params).fetchone()
    return dict(row) if row is not None else None
