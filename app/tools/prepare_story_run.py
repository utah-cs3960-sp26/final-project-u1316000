from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import connect
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver
from app.services.generation import LLMGenerationService
from app.services.story_graph import StoryGraphService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare one compact story-worker packet so an LLM can continue the story without repo spelunking."
    )
    parser.add_argument("--branch-key", help="Optional branch key to constrain frontier selection.")
    parser.add_argument("--choice-id", type=int, help="Optional specific frontier choice id to prepare.")
    parser.add_argument("--mode", default="auto", choices=["auto", "manual"])
    parser.add_argument("--requested-choice-count", type=int, default=2)
    parser.add_argument("--play-base-url", default="http://127.0.0.1:8001")
    return parser


def summarize_context(context: dict[str, Any]) -> dict[str, Any]:
    current_node = context.get("current_node") or {}
    return {
        "branch_key": context["branch_key"],
        "act_phase": context["branch_state"]["act_phase"],
        "branch_depth": context["branch_state"]["branch_depth"],
        "current_node": {
            "id": current_node.get("id"),
            "title": current_node.get("title"),
            "summary": current_node.get("summary"),
        },
        "active_hooks": [
            {
                "id": hook["id"],
                "importance": hook["importance"],
                "summary": hook["summary"],
            }
            for hook in context.get("active_hooks", [])
        ],
        "eligible_major_hooks": [
            {
                "id": hook["id"],
                "summary": hook["summary"],
            }
            for hook in context.get("eligible_major_hooks", [])
        ],
        "blocked_major_hooks": [
            {
                "id": hook["id"],
                "summary": hook["summary"],
                "remaining_distance": (hook.get("readiness") or {}).get("remaining_distance"),
                "missing_clue_tags": (hook.get("readiness") or {}).get("missing_clue_tags", []),
                "missing_state_tags": (hook.get("readiness") or {}).get("missing_state_tags", []),
            }
            for hook in context.get("blocked_major_hooks", [])
        ],
        "available_affordances": [
            {
                "name": affordance["name"],
                "description": affordance["description"],
            }
            for affordance in context.get("available_affordances", [])
        ],
        "branch_tags": [tag["tag"] for tag in context.get("branch_tags", [])],
        "merge_candidates": [
            {
                "node_id": candidate["node_id"],
                "title": candidate["title"],
                "summary": candidate["summary"],
            }
            for candidate in context.get("merge_candidates", [])
        ],
        "requested_choice_count": context.get("requested_choice_count"),
    }


def select_frontier_item(
    story: StoryGraphService,
    branch_state: BranchStateService,
    *,
    branch_key: str | None,
    choice_id: int | None,
    mode: str,
) -> dict[str, Any]:
    frontier = story.list_frontier(
        branch_state_service=branch_state,
        branch_key=branch_key,
        limit=100,
        mode=mode,
    )
    if choice_id is not None:
        for item in frontier:
            if int(item["choice_id"]) == choice_id:
                return item
        raise ValueError(f"choice_id {choice_id} is not present in the current frontier.")
    if not frontier:
        raise ValueError("No open frontier items are available.")
    return frontier[0]


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    settings = Settings.from_env()
    llm_generation = LLMGenerationService(project_root)

    connection = connect(settings.database_path)
    try:
        canon = CanonResolver(connection)
        story = StoryGraphService(connection)
        branch_state = BranchStateService(connection, llm_generation.story_bible["acts"])
        selected = select_frontier_item(
            story,
            branch_state,
            branch_key=args.branch_key,
            choice_id=args.choice_id,
            mode=args.mode,
        )
        preview_payload = {
            "branch_key": selected["branch_key"],
            "choice_id": selected["choice_id"],
            "current_node_id": selected["from_node_id"],
            "branch_summary": selected["branch_summary"],
            "requested_choice_count": args.requested_choice_count,
        }
        context = llm_generation.build_context(
            branch_key=selected["branch_key"],
            canon=canon,
            branch_state=branch_state,
            story_graph=story,
            focus_entity_ids=[],
            current_node_id=int(selected["from_node_id"]),
            branch_summary=selected["branch_summary"],
            requested_choice_count=args.requested_choice_count,
        )
        packet = {
            "message": (
                "Everything is already wired through. Your job is to continue the story, not inspect the repo. "
                "Use this packet, return one GenerationCandidate JSON, validate it, apply it, and stop."
            ),
            "pre_change_url": (
                f"{args.play_base_url.rstrip('/')}/play?branch_key={selected['branch_key']}"
                f"&scene={selected['from_node_id']}"
            ),
            "selected_frontier_item": selected,
            "preview_payload": preview_payload,
            "context_summary": summarize_context(context),
            "full_context": context,
            "next_action": "Return one GenerationCandidate JSON only. Do not browse the repo unless the loop is blocked.",
        }
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        connection.close()

    print(json.dumps(packet, indent=2))


if __name__ == "__main__":
    main()
