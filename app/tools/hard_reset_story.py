from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import connect
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver
from app.services.generation import LLMGenerationService
from app.services.story_notes import StoryDirectionService
from app.services.story_graph import StoryGraphService
from app.services.story_setup import StorySetupService
from app.services.worldbuilding import WorldbuildingService
from app.tools.snapshot_db import create_snapshot, sanitize_label


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Snapshot the current story tree, wipe live continuity, and reseed a fresh opening state."
    )
    parser.add_argument(
        "--branch-key",
        default="default",
        help="Branch key to reseed after the wipe. Defaults to default.",
    )
    parser.add_argument(
        "--snapshot-label",
        default="pre-hard-reset",
        help="Label for the database/tree snapshot files.",
    )
    return parser


def export_tree_snapshot(
    *,
    snapshot_dir: Path,
    snapshot_label: str,
    story: StoryGraphService,
    canon: CanonResolver,
    branch_state: BranchStateService,
    story_notes: StoryDirectionService,
    worldbuilding: WorldbuildingService,
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_label = sanitize_label(snapshot_label)
    tree_snapshot_path = snapshot_dir / f"story-tree-{safe_label}-{timestamp}.json"
    payload: dict[str, Any] = {
        "created_at": datetime.now().astimezone().isoformat(),
        "counts": story.counts(),
        "locations": canon.list_locations(),
        "characters": canon.list_characters(),
        "objects": canon.list_objects(),
        "facts": canon.list_facts(),
        "relations": canon.list_relations(),
        "branch_state": branch_state.get_branch_state("default"),
        "story_nodes": story.list_story_nodes(),
        "story_direction_notes": story_notes.list_notes(),
        "worldbuilding_notes": worldbuilding.list_notes(),
    }
    tree_snapshot_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tree_snapshot_path


def collect_story_specific_idea_terms(canon: CanonResolver, *, protagonist_name: str, opening_location_name: str) -> list[str]:
    protagonist_key = protagonist_name.strip().lower()
    opening_location_key = opening_location_name.strip().lower()
    terms: list[str] = []
    for location in canon.list_locations():
        name = (location.get("name") or "").strip()
        if name and name.lower() != opening_location_key:
            terms.append(name)
    for character in canon.list_characters():
        name = (character.get("name") or "").strip()
        if name and name.lower() != protagonist_key:
            terms.append(name)
    for obj in canon.list_objects():
        name = (obj.get("name") or "").strip()
        if name:
            terms.append(name)
    return sorted(set(terms), key=str.lower)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    settings = Settings.from_env()
    project_root = Path(__file__).resolve().parents[2]
    llm_generation = LLMGenerationService(project_root)
    snapshot_path = create_snapshot(settings.database_path, name=args.snapshot_label)
    snapshot_dir = snapshot_path.parent

    with connect(settings.database_path) as connection:
        canon = CanonResolver(connection)
        branch_state = BranchStateService(connection, llm_generation.story_bible["acts"])
        story = StoryGraphService(connection)
        story_notes = StoryDirectionService(connection)
        worldbuilding = WorldbuildingService(connection)

        tree_snapshot_path = export_tree_snapshot(
            snapshot_dir=snapshot_dir,
            snapshot_label=args.snapshot_label,
            story=story,
            canon=canon,
            branch_state=branch_state,
            story_notes=story_notes,
            worldbuilding=worldbuilding,
        )

        protagonist_name = llm_generation.story_bible["protagonist"]["name"]
        opening_location_name = "Mushroom Field"
        story_specific_idea_terms = collect_story_specific_idea_terms(
            canon,
            protagonist_name=protagonist_name,
            opening_location_name=opening_location_name,
        )

        setup = StorySetupService(
            connection,
            project_root=project_root,
            story_bible=llm_generation.story_bible,
        )
        result = setup.hard_reset_story(
            branch_key=args.branch_key,
            ideas_path=project_root / "IDEAS.md",
            story_specific_idea_terms=story_specific_idea_terms,
        )

    print(
        json.dumps(
            {
                "database_snapshot": str(snapshot_path),
                "tree_snapshot": str(tree_snapshot_path),
                **result,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
