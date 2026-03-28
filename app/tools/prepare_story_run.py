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
    parser.add_argument("--full-context", action="store_true", help="Include the full raw generation context in the packet.")
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
                "payoff_concept": hook.get("payoff_concept"),
                "must_not_imply": hook.get("must_not_imply", []),
            }
            for hook in context.get("active_hooks", [])
        ],
        "eligible_major_hooks": [
            {
                "id": hook["id"],
                "summary": hook["summary"],
                "payoff_concept": hook.get("payoff_concept"),
                "must_not_imply": hook.get("must_not_imply", []),
            }
            for hook in context.get("eligible_major_hooks", [])
        ],
        "blocked_major_hooks": [
            {
                "id": hook["id"],
                "summary": hook["summary"],
                "payoff_concept": hook.get("payoff_concept"),
                "must_not_imply": hook.get("must_not_imply", []),
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


def build_focus_canon_slice(context: dict[str, Any], canon: CanonResolver) -> dict[str, Any]:
    current_node = context.get("current_node") or {}
    entity_refs = current_node.get("entities", []) or []
    locations: list[dict[str, Any]] = []
    characters: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []

    for ref in entity_refs:
        entity_type = ref.get("entity_type")
        entity_id = ref.get("entity_id")
        if entity_type == "location" and entity_id is not None:
            location = canon.get_location(int(entity_id))
            if location is not None:
                locations.append(location)
        elif entity_type == "character" and entity_id is not None:
            character = canon.get_character(int(entity_id))
            if character is not None:
                characters.append(character)
        elif entity_type == "object" and entity_id is not None:
            obj = canon.get_object(int(entity_id))
            if obj is not None:
                objects.append(obj)

    recurring_entities = context.get("recurring_entities", [])[:5]
    recurring_details: list[dict[str, Any]] = []
    for recurring in recurring_entities:
        entity_type = recurring.get("entity_type")
        entity_id = recurring.get("entity_id")
        record: dict[str, Any] | None = None
        if entity_type == "location" and entity_id is not None:
            record = canon.get_location(int(entity_id))
        elif entity_type == "character" and entity_id is not None:
            record = canon.get_character(int(entity_id))
        elif entity_type == "object" and entity_id is not None:
            record = canon.get_object(int(entity_id))
        if record is not None:
            recurring_details.append(
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "name": record.get("name"),
                    "canonical_summary": record.get("canonical_summary"),
                    "appearances": recurring.get("appearances"),
                }
            )

    return {
        "current_scene_entities": {
            "locations": locations,
            "characters": characters,
            "objects": objects,
        },
        "recurring_entities": recurring_details,
    }


def build_validation_checklist() -> list[str]:
    return [
        "Return valid GenerationCandidate JSON only; do not wrap it in prose.",
        "Include at least one choice.",
        "Usually return 2 or 3 choices; 1 is okay for a forced beat; 4+ should be rare.",
        "If you introduce a new unresolved mystery or unanswered question, create or extend a hook.",
        "For major hooks, include a payoff_concept describing the intended direction of the later payoff.",
        "A good payoff_concept should describe the general shape of the later answer, not just tie the mystery to the nearest currently available local system.",
        "Broad direction does not mean vague direction: if the likely later answer already points at a known character, place, or system, say that directly.",
        "Use must_not_imply on hooks when there are tempting wrong shortcuts future workers should avoid.",
        "If you introduce a placeholder mystery entity like an unseen voice or unknown figure, create a hook and link it to the current_scene location or another relevant entity when possible.",
        "Do not resolve blocked major hooks; only resolve or strongly advance major hooks that are eligible.",
        "For blocked major hooks, prefer suggestive clues, provenance hints, or eerie resonance over explicit local-system instructions or ownership claims unless multiple prior clues already support that connection.",
        "Do not resolve any hook before min_distance_to_payoff and required clue/state tags allow it.",
        "Do not reference unavailable affordances in choice.required_affordances.",
        "Use target_node_id only for valid same-branch merge candidates.",
        "Do not invent new locked facts.",
        "If the scene introduces a new recurring character, new linked location, or reusable visually important object, plan the post-apply asset generation.",
        "If a choice clearly means travel, arrival, boarding, departure, or being sent somewhere else, strongly prefer a new linked location unless it is truly the same place from nearly the same visual framing.",
        "If the player has clearly arrived somewhere new, `no new art required` is usually the wrong conclusion.",
        "If a location has not already been visually defined, give it a distinct whimsical-fantasy identity that stays readable and not overly complicated for image generation.",
    ]


def build_candidate_template(branch_key: str) -> dict[str, Any]:
    return {
        "branch_key": branch_key,
        "scene_title": "",
        "scene_summary": "",
        "scene_text": "",
        "dialogue_lines": [
            {"speaker": "Narrator", "text": ""},
        ],
        "choices": [
            {"choice_text": ""},
            {"choice_text": ""},
        ],
        "entity_references": [],
        "scene_present_entities": [],
        "fact_updates": [],
        "relation_updates": [],
        "new_hooks": [],
        "hook_updates": [],
        "inventory_changes": [],
        "affordance_changes": [],
        "relationship_changes": [],
        "asset_requests": [],
        "discovered_clue_tags": [],
        "discovered_state_tags": [],
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
                "Use this packet, continue the worker loop immediately, return one GenerationCandidate JSON, "
                "validate it, apply it if valid, generate any required art, and stop. "
                "Do not summarize this packet and wait for permission unless the human explicitly asked for discussion only."
            ),
            "pre_change_url": (
                f"{args.play_base_url.rstrip('/')}/play?branch_key={selected['branch_key']}"
                f"&scene={selected['from_node_id']}"
            ),
            "selected_frontier_item": selected,
            "preview_payload": preview_payload,
            "context_summary": summarize_context(context),
            "focus_canon_slice": build_focus_canon_slice(context, canon),
            "validation_checklist": build_validation_checklist(),
            "candidate_template": build_candidate_template(selected["branch_key"]),
            "endpoint_contract": {
                "validate_generation": "POST /jobs/validate-generation with the GenerationCandidate JSON as the request body.",
                "apply_generation": (
                    "POST /jobs/apply-generation with branch_key, parent_node_id, choice_id, and candidate."
                ),
                "generate_assets_after_apply": (
                    "POST /assets/generate after apply when new recurring characters, linked locations, or reusable important objects need art."
                ),
            },
            "manual_commands": {
                "prepare": "python -m app.tools.prepare_story_run",
                "validate": "POST /jobs/validate-generation",
                "apply": "POST /jobs/apply-generation",
            },
            "next_action": (
                "Run now. Do not ask the human for permission. Return one GenerationCandidate JSON only, "
                "validate it, apply it if valid, generate any required art, report the pre-change URL, and stop. "
                "Do not browse the repo unless the loop is blocked."
            ),
        }
        if args.full_context:
            packet["full_context"] = context
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        connection.close()

    print(json.dumps(packet, indent=2))


if __name__ == "__main__":
    main()
