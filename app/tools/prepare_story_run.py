from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import connect, fetch_one
from app.services.assets import AssetService
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver
from app.services.generation import LLMGenerationService
from app.services.story_notes import StoryDirectionService
from app.services.story_graph import StoryGraphService
from app.services.worldbuilding import WorldbuildingService

SAME_LOCATION_PRESSURE_THRESHOLD = 4
ISOLATION_PRESSURE_THRESHOLD = 6
NEW_CHARACTER_PRESSURE_THRESHOLD = 6


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


def _text_mentions_any_name(value: Any, names: set[str]) -> bool:
    if not names:
        return False
    if isinstance(value, str):
        lowered = value.lower()
        return any(name in lowered for name in names)
    if isinstance(value, list):
        return any(_text_mentions_any_name(item, names) for item in value)
    if isinstance(value, dict):
        return any(_text_mentions_any_name(item, names) for item in value.values())
    return False


def _annotate_text_with_character_status(
    value: Any,
    status_labels_by_name: dict[str, str],
) -> Any:
    if not status_labels_by_name:
        return value
    if isinstance(value, str):
        annotated = value
        for lowered_name, labeled_name in sorted(
            status_labels_by_name.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            plain_name = labeled_name.split(" [", 1)[0]
            if labeled_name in annotated:
                continue
            annotated = re.sub(
                rf"(?<![A-Za-z0-9]){re.escape(plain_name)}(?![A-Za-z0-9])",
                labeled_name,
                annotated,
                flags=re.IGNORECASE,
            )
        return annotated
    if isinstance(value, list):
        return [_annotate_text_with_character_status(item, status_labels_by_name) for item in value]
    if isinstance(value, dict):
        return {
            key: _annotate_text_with_character_status(item, status_labels_by_name)
            for key, item in value.items()
        }
    return value


def summarize_context(
    context: dict[str, Any],
    *,
    off_path_character_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    current_node = context.get("current_node") or {}
    off_path_character_labels = off_path_character_labels or {}

    active_hooks = [
        _annotate_text_with_character_status(
            {
                "id": hook["id"],
                "importance": hook["importance"],
                "summary": hook["summary"],
                "payoff_concept": hook.get("payoff_concept"),
                "must_not_imply": hook.get("must_not_imply", []),
                "remaining_development_distance": (hook.get("readiness") or {}).get("remaining_development_distance"),
            },
            off_path_character_labels,
        )
        for hook in context.get("active_hooks", [])[:4]
    ]
    eligible_major_hooks = [
        _annotate_text_with_character_status(
            {
                "id": hook["id"],
                "summary": hook["summary"],
                "payoff_concept": hook.get("payoff_concept"),
                "must_not_imply": hook.get("must_not_imply", []),
            },
            off_path_character_labels,
        )
        for hook in context.get("eligible_major_hooks", [])[:2]
    ]
    blocked_major_hooks = [
        _annotate_text_with_character_status(
            {
                "id": hook["id"],
                "summary": hook["summary"],
                "remaining_distance": (hook.get("readiness") or {}).get("remaining_distance"),
                "remaining_development_distance": (hook.get("readiness") or {}).get("remaining_development_distance"),
                "rule": "Do not advance this hook yet.",
            },
            off_path_character_labels,
        )
        for hook in context.get("blocked_major_hooks", [])[:2]
    ]
    developable_major_hooks = [
        _annotate_text_with_character_status(
            {
                "id": hook["id"],
                "summary": hook["summary"],
                "payoff_concept": hook.get("payoff_concept"),
                "must_not_imply": hook.get("must_not_imply", []),
            },
            off_path_character_labels,
        )
        for hook in context.get("developable_major_hooks", [])[:2]
    ]
    blocked_major_developments = [
        _annotate_text_with_character_status(
            {
                "id": hook["id"],
                "summary": hook["summary"],
                "remaining_development_distance": (hook.get("readiness") or {}).get("remaining_development_distance"),
                "rule": "Do not develop this hook yet.",
            },
            off_path_character_labels,
        )
        for hook in context.get("blocked_major_developments", [])[:2]
    ]
    global_direction_notes = [
        _annotate_text_with_character_status(
            {
                "id": note["id"],
                "note_type": note["note_type"],
                "title": note["title"],
                "note_text": note["note_text"],
                "status": note["status"],
                "priority": note["priority"],
            },
            off_path_character_labels,
        )
        for note in context.get("global_direction_notes", [])[:3]
    ]
    worldbuilding_notes = [
        {
            "id": note["id"],
            "note_type": note["note_type"],
            "title": note["title"],
            "note_text": note["note_text"],
            "status": note["status"],
            "priority": note["priority"],
            "pressure": note.get("pressure", 2),
        }
        for note in context.get("worldbuilding_notes", [])[:3]
    ]
    recent_nodes = [
        _annotate_text_with_character_status(node, off_path_character_labels)
        for node in (context.get("branch_shape") or {}).get("recent_nodes", [])[:3]
    ]
    merge_candidates = [
        _annotate_text_with_character_status(
            {
                "node_id": candidate["node_id"],
                "title": candidate["title"],
                "summary": candidate["summary"],
            },
            off_path_character_labels,
        )
        for candidate in context.get("merge_candidates", [])[:3]
    ]

    return {
        "branch_key": context["branch_key"],
        "act_phase": context["branch_state"]["act_phase"],
        "branch_depth": context["branch_state"]["branch_depth"],
        "current_node": {
            "id": current_node.get("id"),
            "title": current_node.get("title"),
            "summary": current_node.get("summary"),
        },
        "active_hooks": active_hooks,
        "eligible_major_hooks": eligible_major_hooks,
        "blocked_major_hooks": blocked_major_hooks,
        "developable_major_hooks": developable_major_hooks,
        "blocked_major_developments": blocked_major_developments,
        "global_direction_notes": global_direction_notes,
        "available_affordance_names": [
            affordance["name"]
            for affordance in context.get("available_affordances", [])
            if affordance.get("name")
        ],
        "branch_tags": [tag["tag"] for tag in context.get("branch_tags", [])],
        "branch_shape": {
            "merge_pressure_level": (context.get("branch_shape") or {}).get("merge_pressure_level"),
            "should_prefer_divergence": (context.get("branch_shape") or {}).get("should_prefer_divergence"),
            "merge_only_streak": (context.get("branch_shape") or {}).get("merge_only_streak"),
            "merge_only_count": (context.get("branch_shape") or {}).get("merge_only_count"),
            "same_location_streak": (context.get("branch_shape") or {}).get("same_location_streak"),
            "single_actor_scene_streak": (context.get("branch_shape") or {}).get("single_actor_scene_streak"),
            "recent_action_family_counts": (context.get("branch_shape") or {}).get("recent_action_family_counts", {}),
            "repeated_action_family": (context.get("branch_shape") or {}).get("repeated_action_family"),
            "reason": (context.get("branch_shape") or {}).get("reason"),
            "recent_nodes": recent_nodes,
        },
        "frontier_budget_state": context.get("frontier_budget_state"),
        "worldbuilding_notes": worldbuilding_notes,
        "merge_candidates": merge_candidates,
        "arc_exit_candidate": context.get("arc_exit_candidate"),
        "requested_choice_count": context.get("requested_choice_count"),
    }


def build_compact_selected_frontier_item(
    selected: dict[str, Any],
    *,
    choice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    compact = {
        "branch_key": selected.get("branch_key"),
        "choice_id": selected.get("choice_id"),
        "choice_text": selected.get("choice_text"),
        "from_node_id": selected.get("from_node_id"),
        "depth": selected.get("depth"),
        "branch_summary": selected.get("branch_summary"),
        "selection_reason": selected.get("selection_reason"),
        "selection_score": selected.get("selection_score"),
    }
    if choice is not None:
        notes_data = choice.get("notes_data") if isinstance(choice.get("notes_data"), dict) else None
        compact["existing_choice_notes"] = notes_data.get("notes") if notes_data else choice.get("notes")
        compact["existing_choice_planning"] = choice.get("planning")
        compact["bound_idea"] = choice.get("idea_binding")
        compact["choice_class"] = choice.get("choice_class")
        compact["ending_category"] = choice.get("ending_category")
    return compact


def build_choice_handoff(choice: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not choice:
        return None
    planning = choice.get("planning") or {}
    next_node = (planning.get("next_node") or planning.get("goal") or "").strip()
    further_goals = (planning.get("further_goals") or planning.get("intent") or "").strip()
    if not next_node and not further_goals:
        return None
    return {
        "rule": (
            "Treat NEXT_NODE as the concrete immediate result your new scene should actually deliver or clearly pivot from. "
            "Use NEXT_NODE as a base for your scene, but expand and elaborate on it. Do not simply repeat it. "
            "Treat FURTHER_GOALS as the medium-range pressure that should keep moving after the immediate beat lands."
        ),
        "next_node": next_node,
        "further_goals": further_goals,
    }


def build_focus_canon_slice(
    context: dict[str, Any],
    canon: CanonResolver,
    *,
    allowed_entity_ids_by_type: dict[str, set[int]] | None = None,
) -> dict[str, Any]:
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
        allowed_ids = (allowed_entity_ids_by_type or {}).get(entity_type or "")
        if entity_id is not None and allowed_ids is not None and int(entity_id) not in allowed_ids:
            continue
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


def build_asset_availability_summary(
    *,
    context: dict[str, Any],
    canon: CanonResolver,
    assets: AssetService,
) -> list[dict[str, Any]]:
    current_node = context.get("current_node") or {}
    entity_keys: set[tuple[str, int]] = set()
    for reference in current_node.get("entities", []) or []:
        entity_type = reference.get("entity_type")
        entity_id = reference.get("entity_id")
        if entity_type in {"location", "character", "object"} and entity_id is not None:
            entity_keys.add((entity_type, int(entity_id)))
    for present in current_node.get("present_entities", []) or []:
        entity_type = present.get("entity_type")
        entity_id = present.get("entity_id")
        if entity_type in {"character", "object"} and entity_id is not None:
            entity_keys.add((entity_type, int(entity_id)))

    summary: list[dict[str, Any]] = []
    for entity_type, entity_id in sorted(entity_keys):
        if entity_type == "location":
            record = canon.get_location(entity_id)
            relevant_kinds = ["background"]
        elif entity_type == "character":
            record = canon.get_character(entity_id)
            relevant_kinds = ["portrait", "cutout"]
        else:
            record = canon.get_object(entity_id)
            relevant_kinds = ["object_render", "cutout"]
        if record is None:
            continue
        available_asset_kinds = [
            asset_kind
            for asset_kind in relevant_kinds
            if assets.get_latest_asset(entity_type=entity_type, entity_id=entity_id, asset_kind=asset_kind) is not None
        ]
        summary.append(
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "name": record.get("name"),
                "available_asset_kinds": available_asset_kinds,
            }
        )
    return summary


def build_path_character_continuity_summary(
    *,
    selected: dict[str, Any],
    story: StoryGraphService,
    canon: CanonResolver,
) -> dict[str, Any]:
    parent_node_id = int(selected["from_node_id"])
    encountered_ids = story.list_lineage_entity_ids(parent_node_id, "character")
    encountered_characters: list[dict[str, Any]] = []
    for character_id in sorted(encountered_ids):
        character = canon.get_character(character_id)
        if character is None:
            continue
        encountered_characters.append(
            {
                "id": int(character["id"]),
                "name": character.get("name"),
                "canonical_summary": character.get("canonical_summary"),
            }
        )
    return {
        "rule": (
            "Only name canonical characters that have already appeared on this path, "
            "unless you are explicitly introducing them in-scene now."
        ),
        "encountered_characters": encountered_characters,
    }


def build_path_location_continuity_summary(
    *,
    selected: dict[str, Any],
    story: StoryGraphService,
    canon: CanonResolver,
) -> dict[str, Any]:
    parent_node_id = int(selected["from_node_id"])
    encountered_ids = story.list_lineage_entity_ids(parent_node_id, "location")
    encountered_locations: list[dict[str, Any]] = []
    for location_id in sorted(encountered_ids):
        location = canon.get_location(location_id)
        if location is None:
            continue
        encountered_locations.append(
            {
                "id": int(location["id"]),
                "name": location.get("name"),
                "canonical_summary": location.get("canonical_summary"),
            }
        )
    return {
        "rule": (
            "Only return to canonical locations that have already appeared on this path. "
            "Use these encountered locations as the safe set for RETURN_LOCATION."
        ),
        "encountered_locations": encountered_locations,
    }


def build_validation_checklist(*, branch_shape: dict[str, Any] | None = None) -> list[str]:
    checklist = [
        "Return valid GenerationCandidate JSON only; do not wrap it in prose.",
        "If validation fails, fix the listed issues and retry until it passes. Do not stop at the first invalid draft.",
        "Include at least one choice.",
        "Usually return 2 or 3 choices; 1 is okay for a forced beat; 4+ should be rare.",
        "Do not create more than 5 choices on any scene node.",
        "Every choice must include planning notes in the form `NEXT_NODE: ... FURTHER_GOALS: ...`.",
        "Use choice_class when helpful: inspection, progress, commitment, location_transition, or ending.",
        "For brief inspection elaboration that should loop back to the same menu node, you may use TARGETED_NODE: this_node and then write a hidden transition bridge. Do not make every choice a self-merge.",
        "Inspection choices should usually reconverge quickly instead of creating a durable new frontier leaf.",
        "When location_stall_pressure is active, satisfy it in the menu by including at least one `location_transition` choice rather than teleporting the current scene.",
        "Ending choices are allowed. Death, capture, transformation, quiet failure, and hub-return closures are all valid when they fit.",
        "If you need to use a recurring canonical character who has not been met on this path yet, use floating_character_introductions with their existing character_id and a short first-meeting beat.",
        "In choice notes, NEXT_NODE should state the specific immediate result or situation the next scene should actually deliver. FURTHER_GOALS should state the broader direction, later pressure, or branch shape that should continue beyond that.",
        "Use NEXT_NODE as a base for your scene, but expand and elaborate on it. Do not simply repeat it.",
        "If you introduce a brand-new canonical location, character, or object, declare it in new_locations, new_characters, or new_objects with a short readable description.",
        "Do not put existing canon entities into new_locations, new_characters, or new_objects. Existing canon should use entity_references and scene_present_entities.",
        "Persistent objects are exceptional. Ordinary props, vehicles, and local scenery should usually stay in scene text instead of becoming new_objects.",
        "entity_references entries should be shaped like {entity_type, entity_id, role}. They do not use slot.",
        "scene_present_entities entries should be shaped like {entity_type, entity_id, slot, ...}. They use slot, not role.",
        "Locations usually belong in entity_references with role current_scene, not in scene_present_entities.",
        "If you introduce a new unresolved mystery or unanswered question, create or extend a hook.",
        "Use a mix of lyrical narration and clearer spoken dialogue; the player voice should usually be one of the clearest in the scene.",
        "Feel free to act creatively. Make bold choices as long as they fit in the story.",
        "Introduce or reintroduce characters frequently. Characters make a story. Characters may be human, talking/anthropomorphic animals, mythical creatures, fantasy species, golems, dragons, vampires, trolls, ghosts, witches, or anything whimsical, magical, or mythical as long as it fits the setting and/or context.",
        "Introduce new locations frequently when appropriate, or deliberately route the story back to existing locations when the branch is naturally leading there. Places make motion, contrast, and consequence visible.",
        "This world is fantasy first. Outside the king's brass enumerators and their closely related royal systems, ordinary people, places, tools, and problems should feel magical, folkloric, handmade, organic, and mostly preindustrial rather than high-tech, industrial, or sci-fi.",
        "Treat advanced machinery, metallic infrastructure, survey engines, and technical bureaucracy as exceptional pressure textures, not the default texture of the world.",
        "For fit only, not as automatic permission to use these exact canon elements in the current run, think of whimsical-fantasy textures like Madam Bei the frog tram conductor, Pipkin the elf magic librarian, mushroom fields, and glass villages.",
        "Always evaluate whether the player is actually familiar with a character, object, location, title, faction, or system before simply naming it. Hooks, worldbuilding notes, and other behind-the-scenes trackers often name things the player has not learned yet.",
        "If someone besides the protagonist speaks on-screen, use a real character name and make sure that visible speaker can receive portrait/cutout art. Generic labels like 'Guard' or 'Patrol Member' should be reserved for unseen/offscreen voices or kept in narration until the character has a true name.",
        "Frequently use ideas from IDEAS.md when the current branch genuinely supports them. Planning runs happen specifically to make idea usage easier during normal runs like this one.",
        "Prefer clear weird over murky weird. If a line sounds evocative but cannot be paraphrased plainly, rewrite it.",
        "Be especially clear when a line introduces a clue, a rule, a system behavior, or a consequence.",
        "Do not simply restate the just-taken choice as another option in the next scene. Advance the situation first.",
        "Do not repeat the parent scene summary with only cosmetic wording changes.",
        "If an inspection choice names a local prop, marker, knot, placard, seam, or similar focal object, establish it clearly in the scene text first instead of inventing it only in the menu.",
        "Most multi-choice scenes should include at least one consequential option that is not pure inspection.",
        "Use reveal_guardrails when present. Early local pressure and partial strange sightings are okay, but do not dump deferred rulers, hidden powers, or deep backstory too early.",
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
        "Use available_affordance_names as read-only context for what is already unlocked on this branch.",
        "Leave affordance_changes empty unless you are intentionally changing branch affordances in this scene.",
        "Do not copy available_affordance_names into affordance_changes. Each affordance_changes item must be a real change with fields like action and name.",
        "Use target_node_id only for valid same-branch merge candidates.",
        "Do not invent new locked facts.",
        "new_hooks are for brand-new hook proposals. Do not include existing hook ids there; include hook_type and summary instead.",
        "If the scene introduces a new recurring character, new linked location, or reusable visually important object, plan the post-apply asset generation.",
        "Generate art on demand. If a place, character, or object is only future-facing and not yet on-screen or immediately reachable, defer its art until a later scene truly needs it.",
        "If the scene sparks a medium- or long-range idea you want future workers to remember, add a global_direction_note instead of assuming the idea will survive implicitly.",
        "Only name canonical characters that have already appeared on this path unless you are explicitly introducing them in-scene now.",
        "If hooks, notes, or merge summaries mention a canonical character marked `[not yet introduced on this path]`, treat that as planning memory only, not as permission to use them in playable narration or choices yet.",
        "If a choice clearly means travel, arrival, boarding, departure, or being sent somewhere else, strongly prefer a new linked location unless it is truly the same place from nearly the same visual framing.",
        "If the player has clearly arrived somewhere new, `no new art required` is usually the wrong conclusion.",
        "If a location has not already been visually defined, give it a distinct whimsical-fantasy identity that stays readable and not overly complicated for image generation.",
    ]
    if branch_shape and branch_shape.get("should_prefer_divergence"):
        checklist.append(
            "This branch is currently over-merged. Open at least one fresh path this run instead of only reconverging into existing scenes."
        )
    if branch_shape and (branch_shape.get("single_actor_scene_streak") or 0) >= ISOLATION_PRESSURE_THRESHOLD:
        checklist.append(
            "This branch has stayed protagonist-only too long. Reintroduce or introduce a character, or put clear faction/social pressure onstage."
        )
    if branch_shape and (branch_shape.get("new_character_gap_streak") or 0) >= NEW_CHARACTER_PRESSURE_THRESHOLD:
        checklist.append(
            "This branch has gone too long without a brand-new character. Introduce a new character through NEW_CHARACTERS; reusing existing cast does not satisfy this."
        )
    if branch_shape and (branch_shape.get("same_location_streak") or 0) >= SAME_LOCATION_PRESSURE_THRESHOLD:
        checklist.append(
            "This branch has lingered in one place too long. Move to a new location or route back to an existing one soon."
        )
    if branch_shape and branch_shape.get("repeated_action_family") in {"inspect", "follow", "touch", "step_back"}:
        checklist.append(
            f"This branch has been repeating the '{branch_shape.get('repeated_action_family')}' action family. Break the pattern with a social turn, location shift, merge, closure, or immediate external pressure."
        )
    return checklist


def build_author_warnings(
    *,
    frontier_budget_state: dict[str, Any],
    frontier_choice_constraints: dict[str, Any],
    isolation_pressure: dict[str, Any],
    new_character_pressure: dict[str, Any],
    location_stall_pressure: dict[str, Any],
    location_transition_obligation: dict[str, Any],
    recent_action_family_summary: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    pressure_level = str(frontier_budget_state.get("pressure_level") or "normal").strip()
    if pressure_level in {"soft", "hard"}:
        warnings.append(
            f"Frontier pressure is {pressure_level}. Treat merge/closure and fresh-branch limits as hard constraints for this run."
        )
    if frontier_choice_constraints.get("must_include_merge_or_closure"):
        warnings.append("This scene must include at least one merge or closure path.")
        warnings.append(
            "This run will ONLY validate if at least one choice uses TARGETED_NODE with an existing node id to merge into an existing node or uses a non-NONE ENDING_CATEGORY for a real closure."
        )
        warnings.append(
            "You will be able to apply that merge/closure requirement during the choice creation phase."
        )
    max_fresh_choices = frontier_choice_constraints.get("max_fresh_choices_under_pressure")
    if pressure_level in {"soft", "hard"} and max_fresh_choices is not None:
        warnings.append(f"This scene may open at most {int(max_fresh_choices)} fresh branch choice(s) under current pressure.")
    if frontier_choice_constraints.get("inspection_choices_should_reconverge_under_pressure"):
        warnings.append("Inspection choices should reconverge quickly here instead of creating durable fresh leaves.")
    if isolation_pressure.get("active"):
        warnings.append(
            "Isolation pressure is active: this branch has stayed protagonist-only too long. Fix it with another named character, a reintroduced character, or clear faction/social pressure onstage. A new location alone will NOT satisfy this."
        )
    if new_character_pressure.get("active"):
        warnings.append(
            "New-character pressure is active: this branch has gone too long without a brand-new character. Fix it with NEW_CHARACTERS and a real first-meeting beat. Reusing only existing characters will NOT satisfy this."
        )
    if location_stall_pressure.get("active"):
        warnings.append(
            "Location-stall pressure is active: this menu must include at least one CHOICE_CLASS: location_transition option. That choice will promise a move to a different location when it is expanded later. A new character alone will NOT satisfy this."
        )
    if location_transition_obligation.get("active"):
        warnings.append(
            "This selected frontier choice already promised a location transition. THIS RUN WILL ONLY VALIDATE if the child scene changes location now with LOCATION_STATUS: new_location or LOCATION_STATUS: return_location."
        )
    warnings.append(
        "Tone reminder: this world is fantasy first. Outside the king's brass enumerators and their closely related royal systems, keep ordinary scenes magical, folkloric, handmade, organic, and mostly preindustrial rather than broadly high-tech."
    )
    warnings.append(
        "Examples of fit only, not automatic canon for this run: Madam Bei the frog tram conductor, Pipkin the elf magic librarian, mushroom fields, and glass villages."
    )
    repeated_action_family = recent_action_family_summary.get("repeated_action_family")
    if repeated_action_family:
        warnings.append(
            f"Recent scenes have overused the '{repeated_action_family}' action family. Break that pattern in this run."
        )
    return warnings


def build_author_warning_banner(author_warnings: list[str]) -> str:
    if not author_warnings:
        return ""
    return (
        "WARNING: YOUR RUN WILL FAIL if you do not follow these warning constraints as you create your scene. "
        + " ".join(author_warnings)
    )


def build_reveal_guardrails(*, act_phase: str | None = None) -> dict[str, Any]:
    early_phase = act_phase in {None, "", "early"}
    allowed_now = [
        "Immediate local danger, patrol pressure, inspections, rumors, and ordinary present-tense trouble.",
        "One small unnerving mechanism, creature, sighting, or isolated anomaly that hints at larger forces without fully explaining them.",
        "A first personal breadcrumb from an important object, scar, curse, keepsake, missing memory, or other intimate mystery thread.",
        "Suspicious reactions from the environment, bystanders, or institutions that make the mystery feel real without solving it.",
        "Unrelated local arcs, new characters, new locations, and present-tense conflict that do not force the deeper answers yet.",
    ]
    avoid_for_now = [
        "Do not name deferred rulers, secret masters, or the full regime behind broad external pressure too early.",
        "Do not fully explain who commands the strange forces, what they are called officially, or how large they are yet.",
        "Do not fully explain the cause, culprit, or purpose behind the protagonist's biggest personal mysteries yet.",
        "Do not fully explain the origin or purpose of an emotionally important object or keepsake too early.",
        "Do not let one local mechanism or one nearby NPC dump the full backstory, true conspiracy, or final answer all at once.",
    ]
    if not early_phase:
        avoid_for_now = [
            item for item in avoid_for_now
            if "Do not name deferred rulers" not in item
        ]
    return {
        "rule": (
            "You may hint at larger forces early, but keep major hidden-power and backstory revelations slow. "
            "Use pressure, rumors, and partial sightings before direct explanation."
        ),
        "allowed_now": allowed_now,
        "avoid_for_now": avoid_for_now,
    }


def build_candidate_template(branch_key: str, *, context: dict[str, Any]) -> dict[str, Any]:
    current_node = context.get("current_node") or {}
    current_entities = current_node.get("entities") or []
    current_location = next(
        (
            entity for entity in current_entities
            if entity.get("entity_type") == "location" and entity.get("entity_id")
        ),
        None,
    )
    player_character = next(
        (
            entity for entity in current_entities
            if entity.get("entity_type") == "character"
            and entity.get("entity_id")
            and (entity.get("role") in {"player", "hero", "protagonist"})
        ),
        None,
    )
    entity_references = []
    if current_location and current_location.get("entity_id"):
        entity_references.append(
            {"entity_type": "location", "entity_id": int(current_location["entity_id"]), "role": "current_scene"}
        )
    scene_present_entities = []
    if player_character and player_character.get("entity_id"):
        scene_present_entities.append(
            {
                "entity_type": "character",
                "entity_id": int(player_character["entity_id"]),
                "slot": "hero-center",
                "focus": True,
            }
        )
    return {
        "branch_key": branch_key,
        "scene_title": "",
        "scene_summary": "",
        "scene_text": "",
        "dialogue_lines": [
            {"speaker": "Narrator", "text": ""},
        ],
        "choices": [
            {"choice_text": "", "notes": "NEXT_NODE:  FURTHER_GOALS: ", "choice_class": "progress"},
            {"choice_text": "", "notes": "NEXT_NODE:  FURTHER_GOALS: ", "choice_class": "commitment"},
        ],
        "new_locations": [],
        "new_characters": [],
        "new_objects": [],
        "floating_character_introductions": [],
        "entity_references": entity_references,
        "scene_present_entities": scene_present_entities,
        "fact_updates": [],
        "relation_updates": [],
        "new_hooks": [
            {
                "hook_type": "minor_mystery",
                "summary": "",
                "payoff_concept": "",
            }
        ],
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
        "chance": 0.125,
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
    open_ideas: list[dict[str, str]] = []
    for line in content.splitlines():
        match = re.match(r"^- \[(?P<category>[^\]]+)\] (?P<title>[^:]+): (?P<note_text>.+)$", line.strip())
        if match:
            open_ideas.append(
                {
                    "category": match.group("category").strip(),
                    "title": match.group("title").strip(),
                    "note_text": match.group("note_text").strip(),
                }
            )
    return {
        "path": str(ideas_path),
        "current_content": content,
        "open_ideas": open_ideas,
    }


def build_planning_target_packet(
    *,
    frontier_item: dict[str, Any],
    args: argparse.Namespace,
    canon: CanonResolver,
    llm_generation: LLMGenerationService,
    branch_state: BranchStateService,
    story_notes: StoryDirectionService,
    worldbuilding: WorldbuildingService,
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
        worldbuilding=worldbuilding,
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
        "existing_choice_notes": (
            choice.get("notes_data", {}).get("notes")
            if isinstance(choice.get("notes_data"), dict)
            else choice.get("notes")
        ),
        "existing_choice_planning": choice.get("planning"),
        "existing_choice_idea_binding": choice.get("idea_binding"),
        "frontier_item": build_compact_selected_frontier_item(frontier_item, choice=choice),
        "context_summary": summarize_context(context),
        "focus_canon_slice": build_focus_canon_slice(context, canon),
    }


def build_normal_packet(
    *,
    args: argparse.Namespace,
    selected: dict[str, Any],
    context: dict[str, Any],
    canon: CanonResolver,
    story: StoryGraphService,
    ideas_file: dict[str, Any],
    asset_availability: list[dict[str, Any]],
    branching_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path_character_continuity = build_path_character_continuity_summary(
        selected=selected,
        story=story,
        canon=canon,
    )
    path_location_continuity = build_path_location_continuity_summary(
        selected=selected,
        story=story,
        canon=canon,
    )
    parent_node_id = int(selected["from_node_id"])
    allowed_entity_ids_by_type = {
        "character": story.list_lineage_entity_ids(parent_node_id, "character"),
        "location": story.list_lineage_entity_ids(parent_node_id, "location"),
        "object": story.list_lineage_entity_ids(parent_node_id, "object"),
    }
    allowed_character_names = {
        (entry.get("name") or "").strip().lower()
        for entry in path_character_continuity.get("encountered_characters", [])
        if (entry.get("name") or "").strip()
    }
    off_path_character_labels = {
        lowered_name: f"{character.get('name')} [not yet introduced on this path]"
        for character in canon.list_characters()
        if (lowered_name := (character.get("name") or "").strip().lower()) and lowered_name not in allowed_character_names
    }
    selected_choice = story.get_choice(int(selected["choice_id"])) or {}
    parent_current_location_ref = next(
        (
            entity for entity in ((context.get("current_node") or {}).get("entities") or [])
            if entity.get("entity_type") == "location" and entity.get("role") == "current_scene" and entity.get("entity_id") is not None
        ),
        None,
    )
    parent_current_location = None
    if parent_current_location_ref is not None:
        parent_current_location = canon.get_location(int(parent_current_location_ref["entity_id"]))
    frontier_budget_state = context.get("frontier_budget_state") or {}
    budget_config = (branching_policy or {}).get("frontier_budget") or {}
    branch_shape = context.get("branch_shape") or {}
    frontier_choice_constraints = {
        "pressure_level": frontier_budget_state.get("pressure_level"),
        "must_include_merge_or_closure": bool(frontier_budget_state.get("pressure_level") in {"soft", "hard"}),
        "max_fresh_choices_under_pressure": int(budget_config.get("default_max_fresh_choices_per_scene", 1)),
        "allow_second_fresh_choice_only_for_bloom_scenes": bool(
            budget_config.get("allow_second_fresh_choice_only_for_bloom_scenes", True)
        ),
        "inspection_choices_should_reconverge_under_pressure": bool(
            frontier_budget_state.get("pressure_level") in {"soft", "hard"}
        ),
        "guidance": (
            "Under soft or hard frontier pressure, include at least one merge or closure path, keep fresh branching narrow, and do not use inspection choices to open durable new leaves."
            if frontier_budget_state.get("pressure_level") in {"soft", "hard"}
            else "Frontier pressure is normal; ordinary branching rules apply."
        ),
    }
    required_scene_delta = {
        "rule": "The next accepted scene must materially change something important instead of merely inspecting the same object again.",
        "allowed_axes": [
            "danger or time pressure",
            "social/cast situation",
            "location motion or access",
            "hook pressure",
            "merge or closure state",
            "worldbuilding pressure becoming immediate",
        ],
    }
    isolation_pressure = {
        "streak": branch_shape.get("single_actor_scene_streak", 0),
        "threshold": ISOLATION_PRESSURE_THRESHOLD,
        "active": bool((branch_shape.get("single_actor_scene_streak") or 0) >= ISOLATION_PRESSURE_THRESHOLD),
        "guidance": (
            "This branch has stayed protagonist-only too long. Fix that with another named character, a reintroduced character, or clear faction/social pressure onstage. A new location alone does not satisfy this."
            if (branch_shape.get("single_actor_scene_streak") or 0) >= ISOLATION_PRESSURE_THRESHOLD
            else "Solo scenes are still allowed right now, but another person or faction is welcome."
        ),
    }
    new_character_pressure = {
        "streak": branch_shape.get("new_character_gap_streak", 0),
        "threshold": NEW_CHARACTER_PRESSURE_THRESHOLD,
        "active": bool((branch_shape.get("new_character_gap_streak") or 0) >= NEW_CHARACTER_PRESSURE_THRESHOLD),
        "guidance": (
            "This branch has gone too long without a brand-new character. Fix that by introducing someone through NEW_CHARACTERS with a real first-meeting beat. Reusing existing characters alone does not satisfy this."
            if (branch_shape.get("new_character_gap_streak") or 0) >= NEW_CHARACTER_PRESSURE_THRESHOLD
            else "A brand-new character is optional right now, but fresh faces are welcome."
        ),
    }
    location_stall_pressure = {
        "streak": branch_shape.get("same_location_streak", 0),
        "threshold": SAME_LOCATION_PRESSURE_THRESHOLD,
        "active": bool((branch_shape.get("same_location_streak") or 0) >= SAME_LOCATION_PRESSURE_THRESHOLD),
        "guidance": (
            "This branch has stayed in the same place too long. Fix that in the menu by including at least one CHOICE_CLASS: location_transition option. That choice should later move to a genuinely new location or a meaningful return to an already encountered place. A new character alone does not satisfy this."
            if (branch_shape.get("same_location_streak") or 0) >= SAME_LOCATION_PRESSURE_THRESHOLD
            else "Location motion is optional right now, but new places or meaningful returns are welcome."
        ),
    }
    selected_choice_class = (selected_choice.get("choice_class") or "").strip()
    location_transition_obligation = {
        "active": selected_choice_class == "location_transition",
        "rule": (
            "The selected frontier choice already promised a location transition. This child scene will only validate if it changes current_scene now with LOCATION_STATUS: new_location or LOCATION_STATUS: return_location."
            if selected_choice_class == "location_transition"
            else "No expansion-time location-transition obligation is active for this run."
        ),
        "valid_fixes": [
            "LOCATION_STATUS: new_location with NEW_LOCATION",
            "LOCATION_STATUS: return_location with RETURN_LOCATION pointing at an already encountered different location",
        ],
    }
    recent_action_family_summary = {
        "repeated_action_family": branch_shape.get("repeated_action_family"),
        "recent_action_family_counts": branch_shape.get("recent_action_family_counts", {}),
        "guidance": "If one action family dominates recent scenes, break the pattern with a social turn, location shift, merge, closure, or immediate external pressure.",
    }
    author_warnings = build_author_warnings(
        frontier_budget_state=frontier_budget_state,
        frontier_choice_constraints=frontier_choice_constraints,
        isolation_pressure=isolation_pressure,
        new_character_pressure=new_character_pressure,
        location_stall_pressure=location_stall_pressure,
        location_transition_obligation=location_transition_obligation,
        recent_action_family_summary=recent_action_family_summary,
    )
    author_warning_banner = build_author_warning_banner(author_warnings)
    consequential_choice_requirement = {
        "required": bool(
            (frontier_budget_state.get("pressure_level") in {"soft", "hard"})
            or (branch_shape.get("same_location_streak") or 0) >= SAME_LOCATION_PRESSURE_THRESHOLD
            or (branch_shape.get("single_actor_scene_streak") or 0) >= ISOLATION_PRESSURE_THRESHOLD
            or (branch_shape.get("new_character_gap_streak") or 0) >= NEW_CHARACTER_PRESSURE_THRESHOLD
            or branch_shape.get("repeated_action_family") in {"inspect", "follow", "touch", "step_back"}
        ),
        "rule": (
            "If you return 2 or more choices, at least one should be a commitment, social move, location shift, merge, closure, or immediate-pressure response."
        ),
    }
    reveal_guardrails = build_reveal_guardrails(act_phase=context.get("act_phase"))
    choice_handoff = build_choice_handoff(selected_choice)
    return {
        "run_mode": "normal",
        "message": (
            ((author_warning_banner + " ") if author_warning_banner else "")
            +
            "Everything is already wired through. Your job is to continue the story, not inspect the repo. "
            "Use this packet and continue the worker loop immediately through the conversational scene-builder steps. "
            "Fill the requested forms step by step, let the local worker assemble and validate the final candidate, apply it if valid, generate any required art, and stop. "
            "Do not summarize this packet and wait for permission unless the human explicitly asked for discussion only."
        ),
        "author_warnings": author_warnings,
        "author_warning_banner": author_warning_banner,
        "pre_change_url": (
            f"{args.play_base_url.rstrip('/')}/play?branch_key={selected['branch_key']}"
            f"&scene={selected['from_node_id']}"
        ),
        "path_character_continuity": path_character_continuity,
        "path_location_continuity": path_location_continuity,
        "parent_current_location": (
            {
                "id": int(parent_current_location["id"]),
                "name": parent_current_location.get("name"),
                "canonical_summary": parent_current_location.get("canonical_summary"),
            }
            if parent_current_location is not None
            else None
        ),
        "global_open_choice_count": frontier_budget_state.get("open_choice_count"),
        "frontier_budget_state": frontier_budget_state,
        "frontier_choice_constraints": frontier_choice_constraints,
        "required_scene_delta": required_scene_delta,
        "reveal_guardrails": reveal_guardrails,
        "isolation_pressure": isolation_pressure,
        "new_character_pressure": new_character_pressure,
        "location_stall_pressure": location_stall_pressure,
        "location_transition_obligation": location_transition_obligation,
        "recent_action_family_summary": recent_action_family_summary,
        "consequential_choice_requirement": consequential_choice_requirement,
        "from_node_total_choice_count": selected.get("from_node_total_choice_count"),
        "from_node_open_choice_count": selected.get("from_node_open_choice_count"),
        "is_bloom_scene_candidate": bool(
            context.get("eligible_major_hooks")
            or context.get("arc_exit_candidate", {}).get("eligible")
            or context.get("current_node", {}).get("id") is None
        ),
        "arc_exit_candidate": context.get("arc_exit_candidate"),
        "selected_frontier_item": build_compact_selected_frontier_item(selected, choice=selected_choice),
        "choice_handoff": choice_handoff,
        "preview_payload": {
            "branch_key": selected["branch_key"],
            "choice_id": selected["choice_id"],
            "current_node_id": selected["from_node_id"],
            "branch_summary": selected["branch_summary"],
            "requested_choice_count": args.requested_choice_count,
        },
        "context_summary": summarize_context(
            context,
            off_path_character_labels=off_path_character_labels,
        ),
        "focus_canon_slice": build_focus_canon_slice(
            context,
            canon,
            allowed_entity_ids_by_type=allowed_entity_ids_by_type,
        ),
        "asset_availability": asset_availability,
        "ideas_file_summary": {
            "path": ideas_file["path"],
            "open_ideas": ideas_file.get("open_ideas", [])[:5],
        },
        "validation_checklist": build_validation_checklist(branch_shape=context.get("branch_shape")),
        "manual_commands": {
            "prepare": "python -m app.tools.prepare_story_run",
            "validate": "POST /jobs/validate-generation",
            "apply": "POST /jobs/apply-generation",
        },
        "next_action": (
            "Run now. Do not ask the human for permission. Use the conversational scene-builder steps and answer only the requested form at each step. "
            "You may steer the current leaf toward one of the active IDEAS.md ideas when it genuinely fits the branch, hooks, and current scene, "
            "but do not force a mismatch just to use an idea. "
            "If selected_frontier_item.bound_idea is present, treat it as the strongest current medium-range steering signal for this leaf unless continuity strongly argues otherwise. "
            "Use frontier_budget_state to understand current branch pressure. If pressure is soft or hard, prefer merges, closures, and narrow continuation over multiple fresh leaves. "
            "Treat frontier_choice_constraints as hard validation rules for this run, not just soft advice. "
            "Feel free to act creatively. Make bold choices as long as they fit in the story. "
            "Introduce or reintroduce characters frequently. Characters make a story. Characters may be human, talking/anthropomorphic animals, mythical creatures, fantasy species, golems, dragons, vampires, trolls, ghosts, witches, or anything whimsical, magical, or mythical as long as it fits the setting and/or context. "
            "Introduce new locations frequently when appropriate, or deliberately route the story back to existing locations when the branch is naturally leading there. Places make motion, contrast, and consequence visible. "
            "Always evaluate whether the player is actually familiar with a character, object, location, title, faction, or system before simply naming it. Worldbuilding files, hooks, and other behind-the-scenes coherence trackers often name things the player is not aware of yet. "
            "Frequently use ideas from IDEAS.md when the current branch genuinely supports them. Treat IDEAS.md as a main source of fresh people, places, and whimsical turns. Planning runs occur specifically to make idea usage easier during normal worker runs like this one. "
            "Use required_scene_delta, isolation_pressure, new_character_pressure, location_stall_pressure, and recent_action_family_summary to avoid another tiny inspect/follow/press loop. "
            "If isolation_pressure is active, fix it with another named character, a reintroduced character, or clear faction/social pressure onstage; a new location alone does not satisfy it. "
            "If new_character_pressure is active, fix it with NEW_CHARACTERS and a real first-meeting beat; reusing only existing characters does not satisfy it. "
            "If location_stall_pressure is active, satisfy it in the choice-writing phase by including at least one CHOICE_CLASS: location_transition option. That choice should promise a move to a different location when it is expanded later. "
            "If location_transition_obligation.active is true, this child scene must change current_scene now with LOCATION_STATUS: new_location or LOCATION_STATUS: return_location. RETURN_LOCATION must come from path_location_continuity and must be different from parent_current_location. "
            "When either pressure is active, prefer whimsical, readable, unexpected developments over another direct derivative of the current patrol/vault/seam beat. "
            "If choice_handoff is present, follow its NEXT_NODE as the direct immediate handoff unless continuity now clearly demands a pivot. Use NEXT_NODE as a base for your scene, but expand and elaborate on it. Do not simply repeat it. "
            "Answer only the requested form for the current step. Do not emit JSON in normal mode. "
            "If consequential_choice_requirement.required is true and you return multiple choices, make sure at least one option is a commitment, social move, location shift, merge, closure, or immediate-pressure response. "
            "For brief local inspection elaboration, you may use TARGETED_NODE: this_node so a hidden transition beat can loop back into the same menu node. Use that for inspection only, not for every choice, and keep at least one outward option. "
            "When frontier_choice_constraints requires a merge or closure path, this run will only validate if at least one choice uses TARGETED_NODE with an existing node id to merge into an existing node or uses a non-NONE ENDING_CATEGORY for a real closure. "
            "You will be able to satisfy that requirement during the choice creation phase. "
            "Follow reveal_guardrails strictly: early pressure, partial strange sightings, and first personal breadcrumbs are okay, but delayed ruler/backstory revelations are not. "
            "Use path_character_continuity.encountered_characters as the safe set of already-met canonical names for this branch path. "
            "Use path_location_continuity.encountered_locations as the safe set of already encountered canonical places for RETURN_LOCATION on this path. "
            "If a hook, note, or merge summary names someone with the label `[not yet introduced on this path]`, treat that as future-facing planning memory only. "
            "Do not casually name a canonical character from some other leaf unless you are explicitly introducing them now. "
            "If you need that kind of recurring cross-arc appearance, use floating_character_introductions so the branch gains a short reusable first meeting instead of pretending prior familiarity. "
            "Check asset_availability before requesting art. If usable art already exists for a location background, character portrait/cutout, or object render/cutout, reuse it and do not request duplicate generation. "
            "Background prompts must stay static-environment-only and must not name separately rendered characters or reusable props. "
            "validate it, apply it if valid, generate any required art, report the pre-change URL, "
            "report the concrete choice id(s) a human should click from that state to reach the new content, "
            "and explicitly say whether you added any hooks, global direction notes, or IDEAS.md entries. "
            "Then stop. "
            "Do not browse the repo unless the loop is blocked."
        ),
        "final_warning": author_warning_banner,
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
    worldbuilding_notes: list[dict[str, Any]],
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
        "worldbuilding_notes": worldbuilding_notes,
        "planning_targets": targets,
        "endpoint_contract": {
            "update_choice_notes": "POST /choices/{choice_id} with JSON {'notes': 'NEXT_NODE: ... FURTHER_GOALS: ...', 'idea_binding': {...}} so the next normal run gets a concrete immediate handoff plus medium-range steering.",
            "story_notes": "POST /story-notes to add structured global planning memory when a medium- or long-range direction deserves to persist across workers.",
            "worldbuilding": "POST /worldbuilding to add or update ambient world-pressure memory such as patrols, rumors, or automata movements.",
            "ideas_file": "Append directly to IDEAS.md using categorized ideas for characters, locations, objects, or events when you have fun future-facing possibilities worth preserving.",
        },
        "manual_commands": {
            "prepare_normal": "python -m app.tools.prepare_story_run",
            "prepare_plan": "python -m app.tools.prepare_story_run --plan",
            "update_choice_notes": "POST /choices/{choice_id}",
            "create_story_note": "POST /story-notes",
            "create_worldbuilding_note": "POST /worldbuilding",
        },
        "next_action": (
            "Run now. Do not ask the human for permission. This is planning mode. "
            "Append exactly "
            f"{planning_policy['ideas_per_run']} new categorized ideas to IDEAS.md. Each idea must be a concrete character, location, object, or event seed, "
            "and together they must cover at least 2 of those categories with at least one event idea. Read the current IDEAS.md content first and add only genuinely new ideas, "
            "not duplicates and not recycled example seeds from the docs or existing ideas file. Then update the notes on each planning target "
            "with clearer NEXT_NODE/FURTHER_GOALS direction for future workers, and bind at least one planning target to a specific fresh or existing idea so later normal runs have a concrete direction signal. Decide whether any existing hook or global idea is worth steering toward "
            "from those targets, even if it will take several later scenes to matter. Prefer steering that creates concrete short-horizon behavior such as introduce a character soon, move to a new or known location soon, escalate patrol pressure, or set up a merge or closure within 1-2 scenes. If useful, add one or two structured story direction notes. "
            "Do not generate, validate, or apply a new story scene in this run. "
            "If the world needs more offscreen motion, you may add one or two worldbuilding notes about patrols, rumors, factions, automata, danger escalation, or other ambient pressures. "
            "At the end, report the exact categorized ideas you appended, the exact choice notes you updated, any story notes you added, and whether you appended IDEAS.md."
        ),
    }


def select_revival_candidate(
    story: StoryGraphService,
    *,
    branch_key: str | None,
) -> dict[str, Any]:
    candidates = story.list_closed_leaf_candidates(branch_key=branch_key, limit=200)
    if not candidates:
        raise ValueError("No open frontier items are available, and no closed leaf can be revived.")
    return random.choice(candidates)


def build_revival_packet(
    *,
    args: argparse.Namespace,
    selected: dict[str, Any],
    context: dict[str, Any],
    canon: CanonResolver,
    story: StoryGraphService,
    ideas_file: dict[str, Any],
    asset_availability: list[dict[str, Any]],
    revival_target: dict[str, Any],
    max_choices_per_node: int,
) -> dict[str, Any]:
    packet = build_normal_packet(
        args=args,
        selected=selected,
        context=context,
        canon=canon,
        story=story,
        ideas_file=ideas_file,
        asset_availability=asset_availability,
        branching_policy=None,
    )
    packet["run_mode"] = "revival"
    packet["message"] = (
        "The active frontier is empty. Reopen continuity from an earlier closed parent instead of creating a disconnected new cycle. "
        "Use this packet to create one new open choice on the parent scene so the loop can continue from existing continuity."
    )
    packet["revival_context"] = {
        "leaf_node_id": revival_target["leaf_node_id"],
        "leaf_title": revival_target.get("leaf_title"),
        "parent_node_id": revival_target["parent_node_id"],
        "parent_title": revival_target.get("parent_title"),
        "traversed_choice_id": revival_target["traversed_choice_id"],
        "traversed_choice_text": revival_target.get("traversed_choice_text"),
        "parent_total_choice_count": story.count_total_choices_for_node(int(revival_target["parent_node_id"])),
        "parent_open_choice_count": story.count_open_choices_for_node(int(revival_target["parent_node_id"])),
        "max_choices_per_node": max_choices_per_node,
        "revival_rule": (
            "If the parent has fewer than max_choices_per_node total choices, append one new open choice there. "
            "If the parent already has max_choices_per_node total choices, replace the traversed closing choice with a new open choice."
        ),
    }
    packet["next_action"] = (
        "Run now. Do not ask the human for permission. This is revival mode. "
        "Do not create a new scene node immediately. Instead, produce one new choice that fits the parent scene's continuity. "
        "If the parent has fewer than the max number of choices, append the new choice. If it is already full, replace the traversed closing choice. "
        "The new choice may still lead to another plausible closure if the situation is dire, but it should also make survival or continued play possible."
    )
    return packet


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    settings = Settings.from_env()
    llm_generation = LLMGenerationService(project_root)
    ideas_file = read_ideas_file(project_root)

    connection = connect(settings.database_path)
    try:
        canon = CanonResolver(connection)
        assets = AssetService(connection, project_root)
        story = StoryGraphService(connection)
        branch_state = BranchStateService(connection, llm_generation.story_bible["acts"])
        story_notes = StoryDirectionService(connection)
        worldbuilding = WorldbuildingService(connection)
        branching_policy = llm_generation.story_bible.get("branching_policy")
        frontier_items = list_frontier_items(
            story,
            branch_state,
            branch_key=args.branch_key,
            mode=args.mode,
            branching_policy=branching_policy,
        )

        planning_policy = build_planning_policy(llm_generation.story_bible)
        runtime_before = get_loop_runtime_state(connection)
        run_mode, planning_reason = decide_run_mode(
            force_plan=args.plan,
            runtime_state=runtime_before,
            planning_policy=planning_policy,
        )
        runtime_after = record_run_mode(connection, run_mode)

        if not frontier_items:
            revival_target = select_revival_candidate(story, branch_key=args.branch_key)
            selected = {
                "branch_key": args.branch_key or "default",
                "from_node_id": revival_target["parent_node_id"],
                "choice_id": revival_target["traversed_choice_id"],
                "choice_text": revival_target.get("traversed_choice_text"),
                "branch_summary": revival_target.get("parent_title") or revival_target.get("leaf_title"),
                "from_node_total_choice_count": story.count_total_choices_for_node(int(revival_target["parent_node_id"])),
                "from_node_open_choice_count": story.count_open_choices_for_node(int(revival_target["parent_node_id"])),
            }
            context = llm_generation.build_context(
                branch_key=selected["branch_key"],
                canon=canon,
                branch_state=branch_state,
                story_notes=story_notes,
                worldbuilding=worldbuilding,
                story_graph=story,
                focus_entity_ids=[],
                current_node_id=int(selected["from_node_id"]),
                branch_summary=selected["branch_summary"],
                requested_choice_count=args.requested_choice_count,
            )
            packet = build_revival_packet(
                args=args,
                selected=selected,
                context=context,
                canon=canon,
                story=story,
                ideas_file=ideas_file,
                asset_availability=build_asset_availability_summary(
                    context=context,
                    canon=canon,
                    assets=assets,
                ),
                revival_target=revival_target,
                max_choices_per_node=int(((branching_policy or {}).get("frontier_budget") or {}).get("max_choices_per_node", 5)),
            )
            packet["planning_policy"] = planning_policy
            packet["runtime_state_before"] = runtime_before
            packet["runtime_state_after"] = runtime_after
            packet["selection_reason"] = "frontier empty; reopening continuity from a random closed leaf parent"
        elif run_mode == "planning":
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
                worldbuilding_notes=worldbuilding.list_notes(statuses=["active", "parked"], limit=8),
                targets=[
                    build_planning_target_packet(
                        frontier_item=item,
                        args=args,
                        canon=canon,
                        llm_generation=llm_generation,
                        branch_state=branch_state,
                        story_notes=story_notes,
                        worldbuilding=worldbuilding,
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
                worldbuilding=worldbuilding,
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
                story=story,
                ideas_file=ideas_file,
                asset_availability=build_asset_availability_summary(
                    context=context,
                    canon=canon,
                    assets=assets,
                ),
                branching_policy=branching_policy,
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
                packet["full_context"] = {
                    "generation_context": context,
                    "ideas_file": ideas_file,
                }
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        connection.close()

    print(json.dumps(packet, indent=2))


if __name__ == "__main__":
    main()
