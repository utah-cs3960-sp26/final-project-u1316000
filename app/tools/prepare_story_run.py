from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import connect, fetch_one
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver
from app.services.generation import LLMGenerationService
from app.services.story_notes import StoryDirectionService
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
    parser.add_argument("--plan", action="store_true", help="Force planning mode instead of a normal scene-writing run.")
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
                "remaining_development_distance": (hook.get("readiness") or {}).get("remaining_development_distance"),
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
                "remaining_development_distance": (hook.get("readiness") or {}).get("remaining_development_distance"),
                "missing_clue_tags": (hook.get("readiness") or {}).get("missing_clue_tags", []),
                "missing_state_tags": (hook.get("readiness") or {}).get("missing_state_tags", []),
            }
            for hook in context.get("blocked_major_hooks", [])
        ],
        "developable_major_hooks": [
            {
                "id": hook["id"],
                "summary": hook["summary"],
                "payoff_concept": hook.get("payoff_concept"),
                "must_not_imply": hook.get("must_not_imply", []),
            }
            for hook in context.get("developable_major_hooks", [])
        ],
        "blocked_major_developments": [
            {
                "id": hook["id"],
                "summary": hook["summary"],
                "payoff_concept": hook.get("payoff_concept"),
                "must_not_imply": hook.get("must_not_imply", []),
                "remaining_development_distance": (hook.get("readiness") or {}).get("remaining_development_distance"),
            }
            for hook in context.get("blocked_major_developments", [])
        ],
        "global_direction_notes": [
            {
                "id": note["id"],
                "note_type": note["note_type"],
                "title": note["title"],
                "note_text": note["note_text"],
                "status": note["status"],
                "priority": note["priority"],
            }
            for note in context.get("global_direction_notes", [])
        ],
        "available_affordances": [
            {
                "name": affordance["name"],
                "description": affordance["description"],
            }
            for affordance in context.get("available_affordances", [])
        ],
        "branch_tags": [tag["tag"] for tag in context.get("branch_tags", [])],
        "branch_shape": {
            "merge_pressure_level": (context.get("branch_shape") or {}).get("merge_pressure_level"),
            "should_prefer_divergence": (context.get("branch_shape") or {}).get("should_prefer_divergence"),
            "merge_only_streak": (context.get("branch_shape") or {}).get("merge_only_streak"),
            "merge_only_count": (context.get("branch_shape") or {}).get("merge_only_count"),
            "reason": (context.get("branch_shape") or {}).get("reason"),
            "recent_nodes": (context.get("branch_shape") or {}).get("recent_nodes", [])[:4],
        },
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


def build_validation_checklist(*, branch_shape: dict[str, Any] | None = None) -> list[str]:
    checklist = [
        "Return valid GenerationCandidate JSON only; do not wrap it in prose.",
        "Include at least one choice.",
        "Usually return 2 or 3 choices; 1 is okay for a forced beat; 4+ should be rare.",
        "Every choice must include notes in this exact pattern: `Goal: ... Intent: ...`.",
        "In choice notes, Goal means the immediate purpose of taking the option. Intent means the broader direction, future possibility, branch shape, or likely payoff lane the option is meant to open or reinforce.",
        "If you introduce a brand-new canonical location, character, or object, declare it in new_locations, new_characters, or new_objects with a short readable description.",
        "If you introduce a new unresolved mystery or unanswered question, create or extend a hook.",
        "Use a mix of lyrical narration and clearer spoken dialogue; the player voice should usually be one of the clearest in the scene.",
        "Prefer clear weird over murky weird. If a line sounds evocative but cannot be paraphrased plainly, rewrite it.",
        "Be especially clear when a line introduces a clue, a rule, a system behavior, or a consequence.",
        "For major hooks, include a payoff_concept describing the intended direction of the later payoff.",
        "If a hook is still on development cooldown, do not explore it, advance it, or even strongly hint at it yet.",
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
        "Generate art on demand. If a place, character, or object is only future-facing and not yet on-screen or immediately reachable, defer its art until a later scene truly needs it.",
        "If the scene sparks a medium- or long-range idea you want future workers to remember, add a global_direction_note instead of assuming the idea will survive implicitly.",
        "If a choice clearly means travel, arrival, boarding, departure, or being sent somewhere else, strongly prefer a new linked location unless it is truly the same place from nearly the same visual framing.",
        "If the player has clearly arrived somewhere new, `no new art required` is usually the wrong conclusion.",
        "If a location has not already been visually defined, give it a distinct whimsical-fantasy identity that stays readable and not overly complicated for image generation.",
    ]
    if branch_shape and branch_shape.get("should_prefer_divergence"):
        checklist.append(
            "This branch is currently over-merged. Open at least one fresh path this run instead of only reconverging into existing scenes."
        )
    return checklist


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
            {"choice_text": "", "notes": "Goal:  Intent: "},
            {"choice_text": "", "notes": "Goal:  Intent: "},
        ],
        "new_locations": [],
        "new_characters": [],
        "new_objects": [],
        "entity_references": [],
        "scene_present_entities": [],
        "fact_updates": [],
        "relation_updates": [],
        "new_hooks": [],
        "hook_updates": [
            {
                "hook_id": 0,
                "status": "active",
                "progress_note": "",
                "next_min_distance_to_development": 0,
            }
        ],
        "global_direction_notes": [],
        "inventory_changes": [],
        "affordance_changes": [],
        "relationship_changes": [],
        "asset_requests": [],
        "discovered_clue_tags": [],
        "discovered_state_tags": [],
    }


def build_planning_policy(story_bible: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "chance": 0.25,
        "min_normal_runs_between_plans": 2,
        "frontier_count": 4,
        "ideas_per_run": 3,
    }
    configured = story_bible.get("planning_mode", {})
    return {
        "chance": float(configured.get("chance", defaults["chance"])),
        "min_normal_runs_between_plans": int(
            configured.get("min_normal_runs_between_plans", defaults["min_normal_runs_between_plans"])
        ),
        "frontier_count": int(configured.get("frontier_count", defaults["frontier_count"])),
        "ideas_per_run": int(configured.get("ideas_per_run", defaults["ideas_per_run"])),
    }


def get_loop_runtime_state(connection) -> dict[str, Any]:
    return fetch_one(
        connection,
        "SELECT normal_runs_since_plan, last_run_mode, updated_at FROM loop_runtime_state WHERE id = 1",
    ) or {
        "normal_runs_since_plan": 0,
        "last_run_mode": "normal",
        "updated_at": None,
    }


def resolve_planning_roll() -> float:
    override = os.environ.get("CYOA_PLANNING_ROLL")
    if override is not None:
        try:
            return max(0.0, min(1.0, float(override)))
        except ValueError:
            pass
    return random.random()


def decide_run_mode(*, force_plan: bool, runtime_state: dict[str, Any], planning_policy: dict[str, Any]) -> tuple[str, str]:
    if force_plan:
        return "planning", "forced by --plan"

    normals_since_plan = int(runtime_state.get("normal_runs_since_plan") or 0)
    minimum_gap = int(planning_policy["min_normal_runs_between_plans"])
    if normals_since_plan < minimum_gap:
        return "normal", f"planning cooldown active: {normals_since_plan}/{minimum_gap} normal runs completed"

    roll = resolve_planning_roll()
    chance = float(planning_policy["chance"])
    if roll < chance:
        return "planning", f"random planning trigger: roll {roll:.3f} < chance {chance:.2f}"
    return "normal", f"normal run selected: roll {roll:.3f} >= chance {chance:.2f}"


def record_run_mode(connection, run_mode: str) -> dict[str, Any]:
    if run_mode == "planning":
        connection.execute(
            """
            UPDATE loop_runtime_state
            SET normal_runs_since_plan = 0,
                last_run_mode = 'planning',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )
    else:
        connection.execute(
            """
            UPDATE loop_runtime_state
            SET normal_runs_since_plan = normal_runs_since_plan + 1,
                last_run_mode = 'normal',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )
    connection.commit()
    return get_loop_runtime_state(connection)


def select_frontier_item(
    story: StoryGraphService,
    branch_state: BranchStateService,
    *,
    branch_key: str | None,
    choice_id: int | None,
    mode: str,
    branching_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frontier = story.list_frontier(
        branch_state_service=branch_state,
        branch_key=branch_key,
        limit=100,
        mode=mode,
        branching_policy=branching_policy,
    )
    if choice_id is not None:
        for item in frontier:
            if int(item["choice_id"]) == choice_id:
                return item
        raise ValueError(f"choice_id {choice_id} is not present in the current frontier.")
    if not frontier:
        raise ValueError("No open frontier items are available.")
    return frontier[0]


def list_frontier_items(
    story: StoryGraphService,
    branch_state: BranchStateService,
    *,
    branch_key: str | None,
    mode: str,
    branching_policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return story.list_frontier(
        branch_state_service=branch_state,
        branch_key=branch_key,
        limit=100,
        mode=mode,
        branching_policy=branching_policy,
    )


def select_planning_targets(
    frontier_items: list[dict[str, Any]],
    *,
    forced_choice_id: int | None,
    target_count: int,
) -> list[dict[str, Any]]:
    if forced_choice_id is not None:
        forced_item = next((item for item in frontier_items if int(item["choice_id"]) == forced_choice_id), None)
        if forced_item is None:
            raise ValueError(f"choice_id {forced_choice_id} is not present in the current frontier.")
        same_scene = [
            item
            for item in frontier_items
            if int(item["from_node_id"]) == int(forced_item["from_node_id"]) and int(item["choice_id"]) != forced_choice_id
        ]
        selected = [forced_item, *same_scene[: max(target_count - 1, 0)]]
        seen = {int(item["choice_id"]) for item in selected}
        for item in frontier_items:
            if len(selected) >= target_count:
                break
            if int(item["choice_id"]) in seen:
                continue
            selected.append(item)
            seen.add(int(item["choice_id"]))
        return selected

    clustered_groups: dict[int, list[dict[str, Any]]] = {}
    for item in frontier_items:
        clustered_groups.setdefault(int(item["from_node_id"]), []).append(item)

    best_group = max(
        clustered_groups.values(),
        key=lambda group: (len(group), max(float(item["selection_score"]) for item in group)),
        default=[],
    )
    selected: list[dict[str, Any]] = []
    seen_choice_ids: set[int] = set()
    if len(best_group) >= 2:
        for item in best_group[:target_count]:
            selected.append(item)
            seen_choice_ids.add(int(item["choice_id"]))
    for item in frontier_items:
        if len(selected) >= target_count:
            break
        if int(item["choice_id"]) in seen_choice_ids:
            continue
        selected.append(item)
        seen_choice_ids.add(int(item["choice_id"]))
    return selected


def read_ideas_file(project_root: Path) -> dict[str, Any]:
    ideas_path = project_root / "IDEAS.md"
    content = ideas_path.read_text(encoding="utf-8") if ideas_path.exists() else ""
    return {
        "path": str(ideas_path),
        "current_content": content,
    }


def build_planning_target_packet(
    *,
    frontier_item: dict[str, Any],
    args: argparse.Namespace,
    canon: CanonResolver,
    llm_generation: LLMGenerationService,
    branch_state: BranchStateService,
    story_notes: StoryDirectionService,
    story: StoryGraphService,
) -> dict[str, Any]:
    preview_payload = {
        "branch_key": frontier_item["branch_key"],
        "choice_id": frontier_item["choice_id"],
        "current_node_id": frontier_item["from_node_id"],
        "branch_summary": frontier_item["branch_summary"],
        "requested_choice_count": args.requested_choice_count,
    }
    context = llm_generation.build_context(
        branch_key=frontier_item["branch_key"],
        canon=canon,
        branch_state=branch_state,
        story_notes=story_notes,
        story_graph=story,
        focus_entity_ids=[],
        current_node_id=int(frontier_item["from_node_id"]),
        branch_summary=frontier_item["branch_summary"],
        requested_choice_count=args.requested_choice_count,
    )
    choice = story.get_choice(int(frontier_item["choice_id"])) or {}
    return {
        "choice_id": frontier_item["choice_id"],
        "choice_text": frontier_item["choice_text"],
        "pre_change_url": (
            f"{args.play_base_url.rstrip('/')}/play?branch_key={frontier_item['branch_key']}"
            f"&scene={frontier_item['from_node_id']}"
        ),
        "existing_choice_notes": choice.get("notes"),
        "existing_choice_planning": choice.get("planning"),
        "frontier_item": frontier_item,
        "context_summary": summarize_context(context),
        "focus_canon_slice": build_focus_canon_slice(context, canon),
    }


def build_normal_packet(
    *,
    args: argparse.Namespace,
    selected: dict[str, Any],
    context: dict[str, Any],
    canon: CanonResolver,
) -> dict[str, Any]:
    return {
        "run_mode": "normal",
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
        "preview_payload": {
            "branch_key": selected["branch_key"],
            "choice_id": selected["choice_id"],
            "current_node_id": selected["from_node_id"],
            "branch_summary": selected["branch_summary"],
            "requested_choice_count": args.requested_choice_count,
        },
        "context_summary": summarize_context(context),
        "focus_canon_slice": build_focus_canon_slice(context, canon),
        "validation_checklist": build_validation_checklist(branch_shape=context.get("branch_shape")),
        "candidate_template": build_candidate_template(selected["branch_key"]),
        "endpoint_contract": {
            "validate_generation": "POST /jobs/validate-generation with the GenerationCandidate JSON as the request body.",
            "apply_generation": (
                "POST /jobs/apply-generation with branch_key, parent_node_id, choice_id, and candidate."
            ),
            "story_notes": (
                "Use global_direction_notes inside the GenerationCandidate for new planning memory, or POST /story-notes directly to add/update out-of-world direction notes."
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
            "validate it, apply it if valid, generate any required art, report the pre-change URL, "
            "report the concrete choice id(s) a human should click from that state to reach the new content, "
            "and explicitly say whether you added any hooks, global direction notes, or IDEAS.md entries. "
            "Then stop. "
            "Do not browse the repo unless the loop is blocked."
        ),
    }


def build_planning_packet(
    *,
    args: argparse.Namespace,
    planning_policy: dict[str, Any],
    planning_reason: str,
    runtime_before: dict[str, Any],
    runtime_after: dict[str, Any],
    ideas_file: dict[str, Any],
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "run_mode": "planning",
        "message": (
            "Everything is already wired through. This time the loop selected planning mode. "
            "Do not generate or apply a new story scene in this run. Use this packet to strengthen medium-range direction, "
            "append new ideas, and improve future frontier choice notes so later normal runs can be bolder without losing continuity."
        ),
        "planning_reason": planning_reason,
        "planning_policy": planning_policy,
        "runtime_state_before": runtime_before,
        "runtime_state_after": runtime_after,
        "ideas_file": ideas_file,
        "planning_targets": targets,
        "endpoint_contract": {
            "update_choice_notes": "POST /choices/{choice_id} with JSON {'notes': 'Goal: ... Intent: ...'} to strengthen future direction for that frontier choice.",
            "story_notes": "POST /story-notes to add structured global planning memory when a medium- or long-range direction deserves to persist across workers.",
            "ideas_file": "Append directly to IDEAS.md when you have fun future-facing ideas for scenes, characters, locations, factions, systems, events, or plotlines.",
        },
        "manual_commands": {
            "prepare_normal": "python -m app.tools.prepare_story_run",
            "prepare_plan": "python -m app.tools.prepare_story_run --plan",
            "update_choice_notes": "POST /choices/{choice_id}",
            "create_story_note": "POST /story-notes",
        },
        "next_action": (
            "Run now. Do not ask the human for permission. This is planning mode. "
            "Append exactly "
            f"{planning_policy['ideas_per_run']} new ideas to IDEAS.md, then update the notes on each planning target "
            "with clearer Goal/Intent direction for future workers. Decide whether any existing hook or global idea is worth steering toward "
            "from those targets, even if it will take several later scenes to matter. If useful, add one or two structured story direction notes. "
            "Do not generate, validate, or apply a new story scene in this run. "
            "At the end, report which choice ids you updated, whether you added story notes, and whether you appended IDEAS.md."
        ),
    }


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
        story_notes = StoryDirectionService(connection)
        branching_policy = llm_generation.story_bible.get("branching_policy")
        frontier_items = list_frontier_items(
            story,
            branch_state,
            branch_key=args.branch_key,
            mode=args.mode,
            branching_policy=branching_policy,
        )
        if not frontier_items:
            raise ValueError("No open frontier items are available.")

        planning_policy = build_planning_policy(llm_generation.story_bible)
        runtime_before = get_loop_runtime_state(connection)
        run_mode, planning_reason = decide_run_mode(
            force_plan=args.plan,
            runtime_state=runtime_before,
            planning_policy=planning_policy,
        )
        runtime_after = record_run_mode(connection, run_mode)

        if run_mode == "planning":
            target_count = max(1, min(planning_policy["frontier_count"], 4))
            selected_targets = select_planning_targets(
                frontier_items,
                forced_choice_id=args.choice_id,
                target_count=target_count,
            )
            packet = build_planning_packet(
                args=args,
                planning_policy=planning_policy,
                planning_reason=planning_reason,
                runtime_before=runtime_before,
                runtime_after=runtime_after,
                ideas_file=read_ideas_file(project_root),
                targets=[
                    build_planning_target_packet(
                        frontier_item=item,
                        args=args,
                        canon=canon,
                        llm_generation=llm_generation,
                        branch_state=branch_state,
                        story_notes=story_notes,
                        story=story,
                    )
                    for item in selected_targets
                ],
            )
        else:
            selected = select_frontier_item(
                story,
                branch_state,
                branch_key=args.branch_key,
                choice_id=args.choice_id,
                mode=args.mode,
                branching_policy=branching_policy,
            )
            context = llm_generation.build_context(
                branch_key=selected["branch_key"],
                canon=canon,
                branch_state=branch_state,
                story_notes=story_notes,
                story_graph=story,
                focus_entity_ids=[],
                current_node_id=int(selected["from_node_id"]),
                branch_summary=selected["branch_summary"],
                requested_choice_count=args.requested_choice_count,
            )
            packet = build_normal_packet(
                args=args,
                selected=selected,
                context=context,
                canon=canon,
            )
            packet["planning_policy"] = planning_policy
            packet["runtime_state_before"] = runtime_before
            packet["runtime_state_after"] = runtime_after
            packet["selection_reason"] = planning_reason
        if args.full_context:
            if run_mode == "planning":
                packet["full_context"] = {
                    "planning_targets": [target["context_summary"] for target in packet["planning_targets"]],
                    "ideas_file": packet["ideas_file"],
                }
            else:
                packet["full_context"] = context
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        connection.close()

    print(json.dumps(packet, indent=2))


if __name__ == "__main__":
    main()
