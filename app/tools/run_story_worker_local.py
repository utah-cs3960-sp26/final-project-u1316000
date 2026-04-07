from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.database import connect
from app.main import create_app
from app.models import DirectionNoteProposal, GenerationCandidate
from app.services.assets import AssetService
from app.services.canon import CanonResolver
from app.services.story_graph import StoryGraphService


PlanningIdeaCategory = Literal["character", "location", "object", "event"]


class PlanningIdeaBinding(BaseModel):
    title: str
    category: PlanningIdeaCategory
    source: Literal["fresh", "existing"] = "fresh"
    steering_note: str | None = None


class PlanningChoiceUpdate(BaseModel):
    choice_id: int
    notes: str
    bound_idea: PlanningIdeaBinding | None = None


class PlanningIdea(BaseModel):
    category: PlanningIdeaCategory
    title: str
    note_text: str


class PlanningResult(BaseModel):
    ideas_to_append: list[PlanningIdea] = Field(default_factory=list)
    choice_note_updates: list[PlanningChoiceUpdate] = Field(default_factory=list)
    story_direction_notes: list[DirectionNoteProposal] = Field(default_factory=list)
    summary: str | None = None


class PlanningIdeasResult(BaseModel):
    ideas_to_append: list[PlanningIdea] = Field(default_factory=list)


class PlanningFollowthroughResult(BaseModel):
    choice_note_updates: list[PlanningChoiceUpdate] = Field(default_factory=list)
    story_direction_notes: list[DirectionNoteProposal] = Field(default_factory=list)
    summary: str | None = None


DISALLOWED_PLANNING_IDEA_SEEDS = {
    "transit robbery",
    "clerk nettle s rival",
    "bell orchard",
    "goose back courier route",
    "name bureaucracy",
    "yesterday s orchard",
    "the apology bridge",
}

IDEA_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "under",
    "with",
}

GENERIC_IDEA_TOKENS = {
    "character",
    "location",
    "object",
    "event",
    "tram",
    "trams",
    "route",
    "routes",
    "station",
    "stations",
    "platform",
    "platforms",
    "field",
    "mushroom",
    "mushrooms",
    "hat",
    "mirror",
    "identity",
    "identities",
    "correction",
    "corrections",
    "departure",
    "departures",
    "ticket",
    "tickets",
    "memory",
    "memories",
    "name",
    "names",
    "record",
    "records",
    "body",
    "protagonist",
}

VALID_PRESENT_ENTITY_SLOTS = {
    "hero-center",
    "left-support",
    "right-support",
    "left-foreground-object",
    "right-foreground-object",
    "center-foreground-object",
}


SCENE_TRANSITION_CUE_PATTERNS = [
    re.compile(r"\b(board|boarding|ride|travel|arrive|arrival|depart|departure|enter|entered|reach|reached)\b", re.IGNORECASE),
    re.compile(r"\bstep\s+(into|through)\b", re.IGNORECASE),
    re.compile(r"\b(head|go|went)\s+to\b", re.IGNORECASE),
    re.compile(r"\bportal\b", re.IGNORECASE),
]

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one local story-worker loop against LM Studio."
    )
    parser.add_argument("--model", required=True, help="Loaded LM Studio model identifier.")
    parser.add_argument("--api-base", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--branch-key")
    parser.add_argument("--choice-id", type=int)
    parser.add_argument("--mode", default="auto", choices=["auto", "manual"])
    parser.add_argument("--requested-choice-count", type=int, default=2)
    parser.add_argument("--play-base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--full-context", action="store_true")
    parser.add_argument("--plan", action="store_true", help="Force planning mode.")
    parser.add_argument("--dry-run", action="store_true", help="Do everything except write changes.")
    parser.add_argument("--temperature", type=float, default=0.4)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=1800.0,
        help="HTTP timeout in seconds for the LM Studio chat completion request.",
    )
    parser.add_argument(
        "--ideas-file",
        help="Optional override for IDEAS.md path, useful for testing or alternate notebooks.",
    )
    parser.add_argument(
        "--log-file",
        help="Optional path to append timestamped run summaries. Defaults to data/worker_logs/local_worker_runs.ndjson",
    )
    return parser


def run_prepare_story_run(args: argparse.Namespace, project_root: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "app.tools.prepare_story_run",
        "--mode",
        args.mode,
        "--requested-choice-count",
        str(args.requested_choice_count),
        "--play-base-url",
        args.play_base_url,
    ]
    if args.branch_key:
        command.extend(["--branch-key", args.branch_key])
    if args.choice_id is not None:
        command.extend(["--choice-id", str(args.choice_id)])
    if args.full_context:
        command.append("--full-context")
    if args.plan:
        command.append("--plan")

    completed = subprocess.run(
        command,
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "prepare_story_run failed"
        raise RuntimeError(stderr)
    return json.loads(completed.stdout)


def load_worker_guide(project_root: Path) -> str:
    return (project_root / "docs" / "llm_story_worker.md").read_text(encoding="utf-8")


def build_system_prompt(worker_guide: str) -> str:
    return (
        "You are the local story worker for this repository.\n"
        "Follow the guide below exactly.\n"
        "Return JSON only. Do not wrap it in markdown fences.\n\n"
        f"{worker_guide}"
    )


def build_user_prompt(packet: dict[str, Any]) -> str:
    if packet["run_mode"] == "planning":
        return (
            "This is a planning-mode packet.\n"
            "Do not generate or apply a scene.\n"
            "Use the planning targets and shared notes in the packet to strengthen future direction.\n"
            "The fresh ideas for this planning run have already been generated separately.\n"
            "The API already supplied the required structured-output schema. Return only JSON.\n\n"
            "Packet:\n"
            f"{json.dumps(packet, indent=2)}"
        )

    return (
        "This is a normal story-worker packet.\n"
        "The API already supplied the required GenerationCandidate structured-output schema.\n"
        "If validation issues are returned later, revise the JSON and try again until it passes validation.\n"
        "Return only GenerationCandidate fields. Do not include report/meta fields like pre_change_url, ideas_to_append, validation_status, or next_action.\n"
        "Use new_locations/new_characters/new_objects only for brand-new canon entities; those seed objects should not carry existing ids.\n"
        "Do not put existing canon like Madam Bei into new_characters. Existing canon belongs in entity_references and scene_present_entities.\n"
        "entity_references entries are only for existing canon references and should be shaped like {entity_type, entity_id, role}.\n"
        "scene_present_entities entries are for visible on-screen staging and should be shaped like {entity_type, entity_id, slot, ...}; they use slot, not role.\n"
        "Locations usually belong in entity_references with role current_scene, not in scene_present_entities.\n"
        "new_hooks entries are only for brand-new hooks; do not include hook ids there, and always include at least hook_type and summary.\n"
        "Leave optional arrays empty unless you are intentionally changing something. "
        "In particular, do not copy read-only branch context into affordance_changes; if you are not unlocking/suspending/restoring/retiring an affordance now, return affordance_changes as [].\n"
        "Return only the JSON object, with no markdown fences or extra commentary.\n\n"
        "Packet:\n"
        f"{json.dumps(packet, indent=2)}"
    )


def build_response_format(packet: dict[str, Any]) -> dict[str, Any]:
    if packet["run_mode"] == "planning":
        schema = PlanningFollowthroughResult.model_json_schema()
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "planning_followthrough_result",
                "schema": schema,
            },
        }
    schema = GenerationCandidate.model_json_schema()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "generation_candidate",
            "schema": schema,
        },
    }


def build_planning_ideas_system_prompt() -> str:
    return (
        "You invent fresh story seeds for a whimsical, surreal fantasy world.\n"
        "Return JSON only. Do not wrap it in markdown fences.\n"
        "Be genuinely original rather than producing small variations on the same motif.\n"
    )


def build_planning_ideas_user_prompt(packet: dict[str, Any]) -> str:
    required_count = int((packet.get("planning_policy") or {}).get("ideas_per_run") or 3)
    return (
        "I'm coming up with ideas for a fantasy whimsical and surreal setting. "
        "In this world talking tram-operating frogs are normal and underground glass villages are possible, "
        "the sky is the limit.\n"
        f"Give me {required_count} new categorized ideas for this world. "
        "Spread them across at least 3 categories chosen from character, location, object, and event.\n"
        "At least one idea must be an event, because the world should feel alive and in motion, not just explorable.\n"
        "The ideas must be concrete and vivid.\n"
        "Do not lightly remix the same motif across multiple ideas.\n"
        "Do not reuse bells, orchards, rival clerks, archivists, reversed announcements, whispering gutters, "
        "or identity-correction paperwork as your central gimmick.\n"
        "DO NOT use anything I said in your response; come up with completely original ideas.\n"
        "Return only JSON matching the supplied schema."
    )


def build_planning_ideas_response_format() -> dict[str, Any]:
    schema = PlanningIdeasResult.model_json_schema()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "planning_ideas_result",
            "schema": schema,
        },
    }


def build_planning_followthrough_user_prompt(
    *,
    packet: dict[str, Any],
    ideas: list[PlanningIdea],
) -> str:
    packet_for_prompt = dict(packet)
    packet_for_prompt["fresh_ideas_for_this_run"] = [idea.model_dump() for idea in ideas]
    return (
        "This is a planning-mode packet.\n"
        "Do not generate or apply a scene.\n"
        "Use the already-generated fresh ideas in fresh_ideas_for_this_run exactly as the ideas to append for this planning pass.\n"
        "Do not replace them with different ideas and do not return ideas_to_append in this step.\n"
        "Use the planning targets and shared notes in the packet to update choice notes and add any useful structured story notes.\n"
        "Bind at least one planning target to a specific idea using bound_idea so later normal runs have a concrete direction signal.\n"
        "If a target fits one of the fresh ideas, prefer binding it. If an existing IDEAS.md idea fits better, you may bind that instead.\n"
        "Return only JSON.\n\n"
        "Packet:\n"
        f"{json.dumps(packet_for_prompt, indent=2)}"
    )


def call_lm_studio(
    *,
    api_base: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any],
    temperature: float,
    max_tokens: int,
    request_timeout: float,
) -> str:
    mock_response_path = os.environ.get("CYOA_LOCAL_WORKER_RESPONSE_FILE")
    if mock_response_path:
        raw_mock = Path(mock_response_path).read_text(encoding="utf-8")
        try:
            parsed_mock = json.loads(raw_mock)
        except json.JSONDecodeError:
            return raw_mock
        if isinstance(parsed_mock, list):
            if not parsed_mock:
                raise RuntimeError("Mock response queue is empty.")
            next_item = parsed_mock.pop(0)
            Path(mock_response_path).write_text(json.dumps(parsed_mock), encoding="utf-8")
            if isinstance(next_item, str):
                return next_item
            return json.dumps(next_item)
        if isinstance(parsed_mock, str):
            return parsed_mock
        return json.dumps(parsed_mock)

    url = f"{api_base.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": response_format,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    response = httpx.post(url, json=payload, timeout=request_timeout)
    response.raise_for_status()
    data = response.json()
    message = data["choices"][0]["message"]
    content = message.get("content") or ""
    if content.strip():
        return content
    reasoning_content = message.get("reasoning_content") or ""
    if reasoning_content.strip():
        return reasoning_content
    finish_reason = data["choices"][0].get("finish_reason")
    raise RuntimeError(
        "LM Studio returned neither content nor reasoning_content. "
        f"finish_reason={finish_reason!r}. This often means the request disconnected or timed out "
        "during prompt processing."
    )


def extract_json_text(raw_text: str) -> str:
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        possible = stripped[start : end + 1]
        try:
            json.loads(possible)
            return possible
        except json.JSONDecodeError:
            pass
    json.loads(stripped)
    return stripped


def normalize_generation_candidate_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(raw_payload)

    seed_fields = {
        "new_locations": ("name", "description", "canonical_summary"),
        "new_characters": ("name", "description", "canonical_summary", "home_location_name"),
        "new_objects": ("name", "description", "canonical_summary", "default_location_name"),
    }
    entity_references = list(payload.get("entity_references") or [])
    scene_present_entities = list(payload.get("scene_present_entities") or [])
    hook_updates = list(payload.get("hook_updates") or [])
    existing_story_notes = list(payload.get("global_direction_notes") or [])

    normalized_entity_references: list[dict[str, Any]] = []
    normalized_scene_present_entities: list[dict[str, Any]] = []

    for reference in entity_references:
        if not isinstance(reference, dict):
            normalized_entity_references.append(reference)
            continue
        role = reference.get("role")
        if role in VALID_PRESENT_ENTITY_SLOTS:
            moved = {
                "entity_type": reference.get("entity_type"),
                "entity_id": reference.get("entity_id"),
                "slot": role,
            }
            for key in ("scale", "offset_x_percent", "offset_y_percent", "focus", "hidden_on_lines", "use_player_fallback"):
                if key in reference:
                    moved[key] = reference[key]
            normalized_scene_present_entities.append(moved)
            continue
        normalized_entity_references.append(reference)

    for present in scene_present_entities:
        if not isinstance(present, dict):
            normalized_scene_present_entities.append(present)
            continue
        role = present.get("role")
        if "slot" not in present and role in VALID_PRESENT_ENTITY_SLOTS:
            moved = dict(present)
            moved["slot"] = role
            moved.pop("role", None)
            normalized_scene_present_entities.append(moved)
            continue
        if present.get("entity_type") == "location" and role == "current_scene" and "slot" not in present:
            normalized_entity_references.append(
                {
                    "entity_type": present.get("entity_type"),
                    "entity_id": present.get("entity_id"),
                    "role": "current_scene",
                }
            )
            continue
        normalized_scene_present_entities.append(present)

    normalized_new_hooks: list[dict[str, Any]] = []
    hooks_source = list(payload.get("new_hooks") or [])
    if payload.get("hooks") and not hooks_source:
        hooks_source = list(payload.get("hooks") or [])
    for hook in hooks_source:
        if isinstance(hook, dict) and hook.get("hook_id") is not None and not hook.get("hook_type"):
            hook_update = {
                "hook_id": hook.get("hook_id"),
                "status": "active",
            }
            if hook.get("summary"):
                hook_update["progress_note"] = hook.get("summary")
            hook_updates.append(hook_update)
            continue
            normalized_new_hooks.append(hook)

    normalized_asset_requests: list[dict[str, Any]] = []
    for request in list(payload.get("asset_requests") or []):
        if (
            isinstance(request, dict)
            and request.get("requested_asset_kinds")
            and not request.get("job_type")
            and not request.get("asset_kind")
        ):
            entity_type = request.get("entity_type")
            entity_id = request.get("entity_id")
            for requested_kind in request.get("requested_asset_kinds") or []:
                if requested_kind == "cutout":
                    continue
                if requested_kind == "background":
                    job_type = "generate_background"
                elif requested_kind == "portrait":
                    job_type = "generate_portrait"
                elif requested_kind == "object_render":
                    job_type = "generate_object"
                else:
                    continue
                normalized_asset_requests.append(
                    {
                        "job_type": job_type,
                        "asset_kind": requested_kind,
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                    }
                )
            continue
        normalized_asset_requests.append(request)

    for field_name, allowed_keys in seed_fields.items():
        normalized_seeds: list[dict[str, Any]] = []
        for item in list(payload.get(field_name) or []):
            if isinstance(item, dict):
                normalized_seeds.append({key: item[key] for key in allowed_keys if key in item})
            else:
                normalized_seeds.append(item)
        payload[field_name] = normalized_seeds

    payload["entity_references"] = normalized_entity_references
    payload["scene_present_entities"] = normalized_scene_present_entities
    payload["new_hooks"] = normalized_new_hooks
    payload["hook_updates"] = hook_updates
    payload["asset_requests"] = normalized_asset_requests
    payload["global_direction_notes"] = existing_story_notes or list(payload.get("story_direction_notes") or [])
    return payload


def parse_llm_result(packet: dict[str, Any], raw_text: str) -> GenerationCandidate | PlanningResult:
    json_text = extract_json_text(raw_text)
    if packet["run_mode"] == "planning":
        return PlanningFollowthroughResult.model_validate_json(json_text)
    raw_payload = json.loads(json_text)
    normalized_payload = normalize_generation_candidate_payload(raw_payload)
    return GenerationCandidate.model_validate(normalized_payload)


def parse_planning_ideas_result(raw_text: str) -> PlanningIdeasResult:
    json_text = extract_json_text(raw_text)
    return PlanningIdeasResult.model_validate_json(json_text)


def build_validation_retry_user_prompt(
    *,
    packet: dict[str, Any],
    previous_candidate: GenerationCandidate,
    issues: list[str],
) -> str:
    encountered_characters = ((packet.get("path_character_continuity") or {}).get("encountered_characters") or [])
    allowed_names = [entry.get("name") for entry in encountered_characters if entry.get("name")]
    return (
        "Your previous GenerationCandidate failed validation.\n"
        "Fix the listed issues and return a corrected GenerationCandidate JSON object only.\n"
        "Do not explain the changes. Do not stop. Keep trying until validation passes.\n\n"
        "Important reminders:\n"
        "- Every choice.notes value must literally include both 'Goal:' and 'Intent:' with meaningful content.\n"
        "- Set target_node_id to null unless you are intentionally quick-merging into one of the listed merge_candidates.\n"
        "- Reuse existing art instead of requesting duplicate generation.\n\n"
        "- Return only GenerationCandidate fields. Do not include pre_change_url, ideas_to_append, validation_status, or next_action.\n"
        "- new_locations/new_characters/new_objects are only for brand-new canon entities and should not carry existing ids.\n"
        "- Existing canon like Madam Bei belongs in entity_references and scene_present_entities, not new_characters.\n"
        "- entity_references entries must use existing canon ids and should only contain entity_type, entity_id, and role.\n"
        "- scene_present_entities entries are for visible on-screen staging and must use slot, not role.\n"
        "- Locations usually belong in entity_references with role current_scene, not in scene_present_entities.\n"
        "- new_hooks are new hook proposals, so do not include hook ids there; include hook_type and summary instead.\n\n"
        "- Do not name a canonical character in scene text or choice text unless they have already appeared on this path or you are explicitly introducing them now.\n"
        f"- Already-met canonical characters on this path: {', '.join(allowed_names) if allowed_names else '(none yet besides implicit player context)'}.\n\n"
        "- If you are not deliberately changing affordances in this scene, return affordance_changes as [].\n"
        "- Do not copy available_affordance_names into affordance_changes. Each affordance_changes item must use the AffordanceChange shape with fields like action and name.\n\n"
        f"Validation issues:\n{json.dumps(issues, indent=2)}\n\n"
        "Previous invalid candidate:\n"
        f"{json.dumps(previous_candidate.model_dump(mode='json'), indent=2)}\n\n"
        "Original packet:\n"
        f"{json.dumps(packet, indent=2)}"
    )


def build_schema_retry_user_prompt(
    *,
    packet: dict[str, Any],
    raw_text: str,
    issues: list[str],
) -> str:
    return (
        "Your previous response did not match the required JSON/schema.\n"
        "Fix the listed schema issues and return a corrected JSON object only.\n"
        "Do not explain the changes. Do not stop. Keep trying until the JSON parses and validates.\n\n"
        "Important reminders:\n"
        "- Use only allowed slot values in scene_present_entities: "
        "'hero-center', 'left-support', 'right-support', 'left-foreground-object', "
        "'right-foreground-object', or 'center-foreground-object'.\n"
        "- scene_present_entities and entity_references must use positive existing entity_id values; never omit entity_id and never use 0.\n"
        "- Every choice.notes value must literally include both 'Goal:' and 'Intent:' with meaningful content.\n"
        "- Set target_node_id to null unless you are intentionally quick-merging into one of the listed merge_candidates.\n"
        "- Return only GenerationCandidate fields. Do not include pre_change_url, ideas_to_append, validation_status, or next_action.\n"
        "- new_locations/new_characters/new_objects are only for brand-new canon entities. Existing canon belongs in entity_references or scene_present_entities instead.\n"
        "- entity_references entries should only contain entity_type, entity_id, and role.\n"
        "- scene_present_entities entries should contain entity_type, entity_id, slot, and optional staging fields; they do not use role.\n"
        "- Locations usually belong in entity_references with role current_scene, not in scene_present_entities.\n"
        "- new_hooks entries are new hook proposals and require hook_type and summary; do not include hook ids there.\n"
        "- If you are not deliberately changing affordances in this scene, return affordance_changes as [].\n"
        "- Do not copy available_affordance_names into affordance_changes.\n"
        "- Return JSON only, with no markdown fences or commentary.\n\n"
        f"Schema issues:\n{json.dumps(issues, indent=2)}\n\n"
        "Previous invalid response:\n"
        f"{raw_text.strip()}\n\n"
        "Original packet:\n"
        f"{json.dumps(packet, indent=2)}"
    )


def build_planning_retry_user_prompt(
    *,
    packet: dict[str, Any],
    ideas: list[PlanningIdea],
    previous_result: PlanningFollowthroughResult,
    issues: list[str],
) -> str:
    packet_for_prompt = dict(packet)
    packet_for_prompt["fresh_ideas_for_this_run"] = [idea.model_dump() for idea in ideas]
    return (
        "Your previous planning result failed validation.\n"
        "Fix the listed planning issues and return a corrected planning JSON object only.\n"
        "Do not explain the changes. Do not stop. Keep trying until the planning result passes validation.\n\n"
        "Important reminders:\n"
        "- Update at least one frontier choice note.\n"
        "- Bind at least one updated choice to a specific idea using bound_idea.\n\n"
        f"Planning issues:\n{json.dumps(issues, indent=2)}\n\n"
        "Previous invalid planning result:\n"
        f"{json.dumps(previous_result.model_dump(mode='json'), indent=2)}\n\n"
        "Original packet:\n"
        f"{json.dumps(packet_for_prompt, indent=2)}"
    )


def build_planning_ideas_retry_user_prompt(
    *,
    packet: dict[str, Any],
    previous_result: PlanningIdeasResult,
    issues: list[str],
) -> str:
    required_count = int((packet.get("planning_policy") or {}).get("ideas_per_run") or 3)
    return (
        "Your previous fresh-idea batch failed validation.\n"
        "Fix the listed originality issues and return a corrected JSON object only.\n"
        "Do not explain the changes. Do not stop. Keep trying until the idea batch passes validation.\n\n"
        "Important reminders:\n"
        f"- Return exactly {required_count} ideas.\n"
        "- Cover at least 3 categories across character/location/object/event.\n"
        "- Include at least one event idea.\n"
        "- Do not reuse or lightly mutate the same motif across multiple ideas.\n"
        "- Do not repeat or closely remix existing repo ideas.\n"
        "- Avoid bells, orchards, rival clerks, archivists, reversed announcements, whispering gutters, or identity-correction paperwork as your central gimmick.\n\n"
        f"Idea issues:\n{json.dumps(issues, indent=2)}\n\n"
        "Previous invalid idea batch:\n"
        f"{json.dumps(previous_result.model_dump(mode='json'), indent=2)}"
    )


def get_test_client() -> TestClient:
    return TestClient(create_app())


def validate_candidate(client: TestClient, candidate: GenerationCandidate) -> dict[str, Any]:
    validation = client.post("/jobs/validate-generation", json=candidate.model_dump(mode="json"))
    validation.raise_for_status()
    return validation.json()


def collect_character_continuity_issues(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
) -> list[str]:
    selected = packet.get("selected_frontier_item") or {}
    parent_node_id = selected.get("from_node_id")
    if parent_node_id is None:
        return []

    settings = Settings.from_env()
    connection = connect(settings.database_path)
    try:
        canon = CanonResolver(connection)
        story = StoryGraphService(connection)
        encountered_ids = story.list_lineage_entity_ids(int(parent_node_id), "character")
        introduced_names = {
            (character.name or "").strip().lower()
            for character in candidate.new_characters
            if (character.name or "").strip()
        }
        explicitly_introduced_existing_ids = {
            int(reference.entity_id)
            for reference in candidate.entity_references
            if reference.entity_type == "character"
        } | {
            int(present.entity_id)
            for present in candidate.scene_present_entities
            if present.entity_type == "character"
        }
        allowed_names = {
            (character.get("name") or "").strip().lower()
            for character_id in encountered_ids
            if (character := canon.get_character(character_id)) is not None and (character.get("name") or "").strip()
        }
        allowed_names.update(
            (character.get("name") or "").strip().lower()
            for character_id in explicitly_introduced_existing_ids
            if (character := canon.get_character(character_id)) is not None and (character.get("name") or "").strip()
        )
        allowed_names.update(introduced_names)

        texts = [
            candidate.scene_summary,
            candidate.scene_text,
            *(line.text for line in candidate.dialogue_lines),
            *(choice.choice_text for choice in candidate.choices),
        ]

        issues: list[str] = []
        seen_names: set[str] = set()
        for character in canon.list_characters():
            name = (character.get("name") or "").strip()
            if not name:
                continue
            lowered = name.lower()
            if lowered in allowed_names or lowered in seen_names:
                continue
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", re.IGNORECASE)
            if any(pattern.search(text or "") for text in texts):
                issues.append(
                    f"Character '{name}' is named in the new scene/choices but has not appeared on this path yet. "
                    "Either introduce them explicitly in-scene first or remove the reference."
                )
                seen_names.add(lowered)
        return issues
    finally:
        connection.close()


def collect_scene_anchor_art_issues(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
) -> list[str]:
    texts = [
        (packet.get("selected_frontier_item") or {}).get("choice_text") or "",
        candidate.scene_summary or "",
        candidate.scene_text or "",
    ]
    scene_transition_expected = any(
        pattern.search(text)
        for pattern in SCENE_TRANSITION_CUE_PATTERNS
        for text in texts
        if text
    )
    if not scene_transition_expected:
        return []

    has_location_current_scene = any(
        reference.role == "current_scene" and reference.entity_type == "location"
        for reference in candidate.entity_references
    )
    if has_location_current_scene:
        return []

    object_render_requests = [
        request for request in candidate.asset_requests
        if request.asset_kind == "object_render"
    ]
    if not object_render_requests:
        return []

    return [
        "This draft looks like a travel/arrival scene, but it does not anchor the scene to a location current_scene and instead requests object art. Do not use object_render as a stand-in for a scene background; create or reference the location and request or reuse its background instead."
    ]


def append_ideas(ideas_path: Path, ideas: list[PlanningIdea]) -> None:
    if not ideas:
        return
    existing = ideas_path.read_text(encoding="utf-8") if ideas_path.exists() else ""
    lines = existing.splitlines()
    if "## Open Ideas" not in existing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["## Open Ideas", ""])
    output = "\n".join(lines).rstrip() + "\n\n"
    for idea in ideas:
        category = idea.category.strip().title()
        title = idea.title.strip()
        note_text = idea.note_text.strip()
        if not category or not title or not note_text:
            continue
        output += f"- [{category}] {title}: {note_text}\n"
    ideas_path.write_text(output.rstrip() + "\n", encoding="utf-8")


def normalize_idea_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def idea_tokens(value: str, *, exclude_generic: bool = False) -> set[str]:
    tokens = {
        token[:-1] if token.endswith("s") and len(token) > 4 else token
        for token in re.findall(r"[a-z0-9]+", normalize_idea_text(value))
        if len(token) >= 4 and token not in IDEA_STOPWORDS
    }
    if exclude_generic:
        return {token for token in tokens if token not in GENERIC_IDEA_TOKENS}
    return tokens


def idea_similarity_tokens(idea: PlanningIdea) -> set[str]:
    return idea_tokens(f"{idea.title} {idea.note_text}")


def jaccard_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def get_planning_idea_issues(packet: dict[str, Any], ideas: list[PlanningIdea]) -> list[str]:
    issues: list[str] = []
    required_count = int((packet.get("planning_policy") or {}).get("ideas_per_run") or 0)
    if len(ideas) < max(required_count, 2):
        issues.append(
            f"Planning mode requires at least {max(required_count, 2)} categorized ideas, but only {len(ideas)} were returned."
        )

    categories = {idea.category for idea in ideas}
    if len(categories) < 2:
        issues.append(
            "Planning mode ideas must span at least 2 categories across character/location/object/event."
        )
    if "event" not in categories:
        issues.append("Planning mode idea batches must include at least one event idea.")

    existing_ideas = ((packet.get("ideas_file") or {}).get("open_ideas") or [])
    existing_content = normalize_idea_text(((packet.get("ideas_file") or {}).get("current_content") or ""))
    existing_title_tokens = {
        item.get("title", ""): idea_tokens(item.get("title", ""), exclude_generic=True)
        for item in existing_ideas
    }
    existing_similarity_tokens = {
        item.get("title", ""): idea_tokens(
            f"{item.get('title', '')} {item.get('note_text', '')}",
            exclude_generic=False,
        )
        for item in existing_ideas
    }

    seen_titles: set[str] = set()
    seen_signatures: set[str] = set()
    title_token_occurrences: dict[str, int] = {}

    for idea in ideas:
        normalized_title = normalize_idea_text(idea.title)
        normalized_text = normalize_idea_text(idea.note_text)
        if normalized_title in DISALLOWED_PLANNING_IDEA_SEEDS:
            issues.append(
                f"Planning idea '{idea.title}' reuses a built-in example seed. Add a different unique idea."
            )
        if normalized_title in seen_titles:
            issues.append(f"Planning idea '{idea.title}' duplicates another new idea title in this run.")
        seen_titles.add(normalized_title)
        signature = f"{idea.category}:{normalized_title}:{normalized_text}"
        if signature in seen_signatures:
            issues.append(f"Planning idea '{idea.title}' duplicates another new idea in this run.")
        seen_signatures.add(signature)
        if normalized_title and normalized_title in existing_content:
            issues.append(
                f"Planning idea '{idea.title}' already appears in IDEAS.md. Add a genuinely new idea."
            )
        if normalized_text and normalized_text in existing_content:
            issues.append(
                f"Planning idea '{idea.title}' repeats wording already present in IDEAS.md. Add a genuinely new idea."
            )

        title_tokens = idea_tokens(idea.title, exclude_generic=True)
        full_tokens = idea_similarity_tokens(idea)
        for token in title_tokens:
            title_token_occurrences[token] = title_token_occurrences.get(token, 0) + 1

        for existing_title, tokens in existing_title_tokens.items():
            if len(title_tokens & tokens) >= 2:
                issues.append(
                    f"Planning idea '{idea.title}' is too close to existing idea '{existing_title}'. Pick a more distinct concept."
                )
                break
        for existing_title, tokens in existing_similarity_tokens.items():
            if jaccard_similarity(full_tokens, tokens) >= 0.4:
                issues.append(
                    f"Planning idea '{idea.title}' is too similar to existing idea '{existing_title}'. Pick a more original concept."
                )
                break

    repeated_motif_tokens = [
        token
        for token, count in sorted(title_token_occurrences.items())
        if count > 1
    ]
    if repeated_motif_tokens:
        issues.append(
            "Planning ideas are clustering around the same motif words in their titles: "
            + ", ".join(repeated_motif_tokens[:6])
            + ". Spread the ideas farther apart."
        )

    for index, idea in enumerate(ideas):
        left_tokens = idea_similarity_tokens(idea)
        for other in ideas[index + 1 :]:
            right_tokens = idea_similarity_tokens(other)
            if jaccard_similarity(left_tokens, right_tokens) >= 0.4:
                issues.append(
                    f"Planning ideas '{idea.title}' and '{other.title}' are too similar to each other. Spread the concepts farther apart."
                )

    return issues


def get_planning_validation_issues(
    packet: dict[str, Any],
    result: PlanningFollowthroughResult,
    ideas: list[PlanningIdea],
) -> list[str]:
    issues: list[str] = []
    if not result.choice_note_updates:
        issues.append("Planning mode must update at least one frontier choice note.")
        return issues

    fresh_idea_lookup = {
        (idea.title.strip().lower(), idea.category): idea
        for idea in ideas
    }
    existing_ideas = ((packet.get("ideas_file") or {}).get("open_ideas") or [])
    existing_idea_lookup = {
        ((item.get("title") or "").strip().lower(), (item.get("category") or "").strip().lower()): item
        for item in existing_ideas
        if (item.get("title") or "").strip() and (item.get("category") or "").strip()
    }
    if not any(update.bound_idea is not None for update in result.choice_note_updates):
        issues.append("Planning mode must bind at least one updated choice to a concrete idea using bound_idea.")
    for update in result.choice_note_updates:
        if update.bound_idea is None:
            continue
        key = (update.bound_idea.title.strip().lower(), update.bound_idea.category)
        if update.bound_idea.source == "fresh":
            if key not in fresh_idea_lookup:
                issues.append(
                    f"Choice update {update.choice_id} binds fresh idea '{update.bound_idea.title}', but that idea is not one of fresh_ideas_for_this_run."
                )
        elif key not in existing_idea_lookup:
            issues.append(
                f"Choice update {update.choice_id} binds existing idea '{update.bound_idea.title}', but that idea is not present in IDEAS.md."
            )
    return issues


def validate_planning_result(packet: dict[str, Any], ideas: list[PlanningIdea], result: PlanningFollowthroughResult) -> None:
    issues = get_planning_validation_issues(packet, result, ideas)
    if issues:
        raise RuntimeError("\n".join(issues))


def apply_planning_result(
    *,
    packet: dict[str, Any],
    ideas: list[PlanningIdea],
    result: PlanningFollowthroughResult,
    client: TestClient,
    ideas_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    validate_planning_result(packet, ideas, result)
    choice_updates: list[int] = []
    note_records: list[dict[str, Any]] = []
    if not dry_run:
        append_ideas(ideas_path, ideas)
        for update in result.choice_note_updates:
            payload: dict[str, Any] = {"notes": update.notes}
            if update.bound_idea is not None:
                payload["idea_binding"] = update.bound_idea.model_dump(mode="json")
            response = client.post(f"/choices/{update.choice_id}", json=payload)
            response.raise_for_status()
            choice_updates.append(update.choice_id)
        for note in result.story_direction_notes:
            response = client.post("/story-notes", json=note.model_dump())
            response.raise_for_status()
            note_records.append(response.json())
    else:
        choice_updates = [update.choice_id for update in result.choice_note_updates]

    target_labels = {
        int(target["choice_id"]): target["choice_text"]
        for target in packet.get("planning_targets", [])
    }

    return {
        "run_mode": "planning",
        "planning_reason": packet.get("planning_reason"),
        "dry_run": dry_run,
        "updated_choice_ids": choice_updates,
        "choice_note_updates": [
            {
                "choice_id": update.choice_id,
                "choice_text": target_labels.get(update.choice_id),
                "notes": update.notes,
                "bound_idea": update.bound_idea.model_dump(mode="json") if update.bound_idea is not None else None,
            }
            for update in result.choice_note_updates
        ],
        "ideas_added": len(ideas),
        "ideas_appended": [idea.model_dump() for idea in ideas],
        "story_notes_added": len(note_records) if not dry_run else len(result.story_direction_notes),
        "story_notes_created": note_records if not dry_run else [note.model_dump() for note in result.story_direction_notes],
        "summary": result.summary,
    }


def apply_normal_result(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
    client: TestClient,
    dry_run: bool,
    project_root: Path,
    validation_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation_payload = validation_payload or validate_candidate(client, candidate)
    if not validation_payload["valid"]:
        raise RuntimeError(f"Validation failed: {json.dumps(validation_payload['issues'], indent=2)}")

    if dry_run:
        return {
            "run_mode": "normal",
            "dry_run": True,
            "pre_change_url": packet["pre_change_url"],
            "choice_id": packet["selected_frontier_item"]["choice_id"],
            "validation": validation_payload,
        }

    apply_payload = {
        "branch_key": packet["preview_payload"]["branch_key"],
        "parent_node_id": packet["preview_payload"]["current_node_id"],
        "choice_id": packet["preview_payload"]["choice_id"],
        "candidate": candidate.model_dump(mode="json"),
    }
    apply_response = client.post("/jobs/apply-generation", json=apply_payload)
    apply_response.raise_for_status()
    applied = apply_response.json()

    generated_assets: list[dict[str, Any]] = []
    inferred_assets: list[dict[str, Any]] = []
    for asset_request in candidate.asset_requests:
        if (
            asset_request.asset_kind == "cutout"
            or asset_request.entity_type is None
            or asset_request.entity_id is None
            or asset_request.prompt is None
        ):
            continue
        payload = {
            "asset_kind": asset_request.asset_kind,
            "entity_type": asset_request.entity_type,
            "entity_id": asset_request.entity_id,
            "prompt": asset_request.prompt,
            "width": asset_request.width or 1024,
            "height": asset_request.height or 1024,
            "steps": asset_request.steps or 25,
            "guidance_scale": asset_request.guidance_scale or 4.0,
            "seed": asset_request.seed,
            "negative_prompt": asset_request.negative_prompt,
            "metadata": asset_request.metadata,
        }
        generated = client.post("/assets/generate", json=payload)
        generated.raise_for_status()
        generated_assets.append(generated.json())

    inferred_requests = infer_missing_asset_requests(
        node=applied["node"],
        explicit_requests=[request.model_dump(mode="json") for request in candidate.asset_requests],
        project_root=project_root,
        client=client,
    )
    for payload in inferred_requests:
        generated = client.post("/assets/generate", json=payload)
        generated.raise_for_status()
        inferred_assets.append(generated.json())

    asset_outputs = [
        {
            "source": "explicit",
            "asset_kind": asset.get("asset_kind"),
            "entity_type": asset.get("entity_type"),
            "entity_id": asset.get("entity_id"),
            "status": asset.get("status"),
            "file_path": asset.get("file_path"),
            "public_url": asset.get("public_url"),
        }
        for asset in generated_assets
    ] + [
        {
            "source": "inferred",
            "asset_kind": asset.get("asset_kind"),
            "entity_type": asset.get("entity_type"),
            "entity_id": asset.get("entity_id"),
            "status": asset.get("status"),
            "file_path": asset.get("file_path"),
            "public_url": asset.get("public_url"),
        }
        for asset in inferred_assets
    ]

    return {
        "run_mode": "normal",
        "dry_run": False,
        "pre_change_url": packet["pre_change_url"],
        "expanded_choice_id": packet["selected_frontier_item"]["choice_id"],
        "expanded_choice_text": packet["selected_frontier_item"]["choice_text"],
        "new_node_id": applied["node"]["id"],
        "new_node_title": applied["node"]["title"],
        "created_choice_ids": [choice["id"] for choice in applied["created_choices"]],
        "generated_asset_count": len(generated_assets) + len(inferred_assets),
        "explicit_asset_count": len(generated_assets),
        "inferred_asset_count": len(inferred_assets),
        "generated_assets": asset_outputs,
        "hooks_added": len(candidate.new_hooks),
        "global_direction_notes_added": len(candidate.global_direction_notes),
    }


def get_default_log_path(project_root: Path) -> Path:
    return project_root / "data" / "worker_logs" / "local_worker_runs.ndjson"


def append_run_log_record(*, log_path: Path, record: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_log_timestamps() -> dict[str, str]:
    now = datetime.now().astimezone()
    return {
        "timestamp": now.strftime("%Y-%m-%d %I:%M:%S %p"),
    }


def append_run_started_log(
    *,
    log_path: Path,
    args: argparse.Namespace,
    packet: dict[str, Any],
) -> None:
    timestamps = build_log_timestamps()
    append_run_log_record(
        log_path=log_path,
        record={
            **timestamps,
            "status": "started",
            "model": args.model,
            "run_mode": packet.get("run_mode"),
            "dry_run": args.dry_run,
            "pre_change_url": packet.get("pre_change_url"),
            "choice_id": (packet.get("selected_frontier_item") or {}).get("choice_id"),
            "planning_reason": packet.get("planning_reason"),
        },
    )


def append_run_finished_log(
    *,
    log_path: Path,
    args: argparse.Namespace,
    result: dict[str, Any],
) -> None:
    timestamps = build_log_timestamps()
    append_run_log_record(
        log_path=log_path,
        record={
            **timestamps,
            "status": "succeeded",
            "model": args.model,
            "run_mode": result.get("run_mode"),
            "dry_run": result.get("dry_run"),
            "result": result,
        },
    )


def append_run_failed_log(
    *,
    log_path: Path,
    args: argparse.Namespace,
    packet: dict[str, Any] | None,
    error: Exception | str,
) -> None:
    timestamps = build_log_timestamps()
    append_run_log_record(
        log_path=log_path,
        record={
            **timestamps,
            "status": "failed",
            "model": args.model,
            "run_mode": packet.get("run_mode") if packet else None,
            "dry_run": args.dry_run,
            "pre_change_url": packet.get("pre_change_url") if packet else None,
            "choice_id": ((packet or {}).get("selected_frontier_item") or {}).get("choice_id"),
            "planning_reason": (packet or {}).get("planning_reason"),
            "error": str(error),
        },
    )


def infer_missing_asset_requests(
    *,
    node: dict[str, Any],
    explicit_requests: list[dict[str, Any]],
    project_root: Path,
    client: TestClient,
) -> list[dict[str, Any]]:
    explicit_pairs = {
        (
            request.get("asset_kind"),
            request.get("entity_type"),
            int(request["entity_id"]),
        )
        for request in explicit_requests
        if request.get("asset_kind") and request.get("entity_type") and request.get("entity_id") is not None
    }

    settings = client.app.state.settings
    from app.database import connect

    connection = connect(settings.database_path)
    try:
        assets = AssetService(connection, project_root)
        canon = CanonResolver(connection)
        inferred: list[dict[str, Any]] = []

        current_scene = next(
            (entity for entity in node.get("entities", []) if entity.get("role") == "current_scene" and entity.get("entity_type") == "location"),
            None,
        )
        if current_scene is not None:
            location_id = int(current_scene["entity_id"])
            if ("background", "location", location_id) not in explicit_pairs:
                background = assets.get_latest_asset(
                    entity_type="location",
                    entity_id=location_id,
                    asset_kind="background",
                )
                if background is None:
                    location = canon.get_location(location_id)
                    if location is not None:
                        inferred.append(
                            {
                                "asset_kind": "background",
                                "entity_type": "location",
                                "entity_id": location_id,
                                "prompt": build_asset_prompt(
                                    entity_type="location",
                                    entity=location,
                                    scene_summary=node.get("summary"),
                                    scene_text=node.get("scene_text"),
                                ),
                                "width": 1600,
                                "height": 896,
                                "metadata": {"source": "inferred_post_apply"},
                            }
                        )

        for present in node.get("present_entities", []):
            entity_type = present.get("entity_type")
            if entity_type not in {"character", "object"}:
                continue
            entity_id = int(present["entity_id"])
            preferred_kind = "portrait" if entity_type == "character" else "object_render"
            if (preferred_kind, entity_type, entity_id) in explicit_pairs:
                continue
            preferred_asset = assets.get_preferred_asset(
                entity_type=entity_type,
                entity_id=entity_id,
                preferred_kinds=["cutout", preferred_kind],
            )
            if preferred_asset is not None:
                continue
            if entity_type == "character":
                record = canon.get_character(entity_id)
                width, height = 1024, 1536
            else:
                record = canon.get_object(entity_id)
                width, height = 1024, 1024
            if record is None:
                continue
            inferred.append(
                {
                    "asset_kind": preferred_kind,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "prompt": build_asset_prompt(
                        entity_type=entity_type,
                        entity=record,
                        scene_summary=node.get("summary"),
                        scene_text=node.get("scene_text"),
                    ),
                    "width": width,
                    "height": height,
                    "metadata": {"source": "inferred_post_apply"},
                }
            )

        return inferred
    finally:
        connection.close()


def build_asset_prompt(
    *,
    entity_type: str,
    entity: dict[str, Any],
    scene_summary: str | None,
    scene_text: str | None,
) -> str:
    name = (entity.get("name") or "").strip()
    description = (entity.get("description") or "").strip()
    canonical_summary = (entity.get("canonical_summary") or "").strip()
    if entity_type == "location":
        fragments = [fragment for fragment in [description, canonical_summary] if fragment]
        joined = ". ".join(fragments[:2]).strip()
        suffix = "Static environment only. No characters. No separately rendered props or vehicles."
        return " ".join(part for part in [name + ".", joined, suffix] if part).strip()
    scene_hint = (scene_summary or scene_text or "").strip()
    fragments = [fragment for fragment in [description, canonical_summary, scene_hint] if fragment]
    joined = ". ".join(fragments[:3]).strip()
    if entity_type == "character":
        return f"Full-body portrait of {name}. {joined}".strip()
    return f"{name}. {joined}".strip()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    log_path = Path(args.log_file) if args.log_file else get_default_log_path(project_root)
    packet: dict[str, Any] | None = None
    try:
        packet = run_prepare_story_run(args, project_root)
        append_run_started_log(log_path=log_path, args=args, packet=packet)
        worker_guide = load_worker_guide(project_root)
        system_prompt = build_system_prompt(worker_guide)
        last_error: Exception | None = None
        parsed: GenerationCandidate | PlanningFollowthroughResult | None = None
        planning_ideas: list[PlanningIdea] = []
        validated_payload: dict[str, Any] | None = None

        with get_test_client() as client:
            if packet["run_mode"] == "planning":
                idea_system_prompt = build_planning_ideas_system_prompt()
                idea_user_prompt = build_planning_ideas_user_prompt(packet)
                idea_response_format = build_planning_ideas_response_format()
                idea_batch: PlanningIdeasResult | None = None

                for _ in range(max(args.max_retries, 1)):
                    raw = ""
                    try:
                        raw = call_lm_studio(
                            api_base=args.api_base,
                            model=args.model,
                            system_prompt=idea_system_prompt,
                            user_prompt=idea_user_prompt,
                            response_format=idea_response_format,
                            temperature=args.temperature,
                            max_tokens=args.max_tokens,
                            request_timeout=args.request_timeout,
                        )
                        idea_batch = parse_planning_ideas_result(raw)
                        idea_issues = get_planning_idea_issues(packet, idea_batch.ideas_to_append)
                        if not idea_issues:
                            planning_ideas = idea_batch.ideas_to_append
                            break
                        last_error = RuntimeError("Planning idea validation failed: " + json.dumps(idea_issues, indent=2))
                        idea_user_prompt = build_planning_ideas_retry_user_prompt(
                            packet=packet,
                            previous_result=idea_batch,
                            issues=idea_issues,
                        )
                    except ValidationError as exc:
                        last_error = exc
                        issue_lines = [
                            " -> ".join(str(part) for part in error.get("loc", [])) + f": {error.get('msg')}"
                            for error in exc.errors()
                        ] or [str(exc)]
                        if raw.strip():
                            idea_user_prompt = (
                                "Your previous fresh-idea response did not match the required JSON/schema.\n"
                                "Fix the listed schema issues and return a corrected JSON object only.\n\n"
                                f"Schema issues:\n{json.dumps(issue_lines, indent=2)}\n\n"
                                f"Previous invalid response:\n{raw.strip()}"
                            )
                    except json.JSONDecodeError as exc:
                        last_error = exc
                        if raw.strip():
                            idea_user_prompt = (
                                "Your previous fresh-idea response did not parse as JSON.\n"
                                "Fix it and return only valid JSON.\n\n"
                                f"JSON issue:\n{json.dumps([str(exc)], indent=2)}\n\n"
                                f"Previous invalid response:\n{raw.strip()}"
                            )
                    except (httpx.HTTPError, RuntimeError) as exc:
                        last_error = exc

                if not planning_ideas:
                    raise SystemExit(f"Local worker failed: {last_error}")

                user_prompt = build_planning_followthrough_user_prompt(
                    packet=packet,
                    ideas=planning_ideas,
                )
                response_format = build_response_format(packet)

                for _ in range(max(args.max_retries, 1)):
                    raw = ""
                    try:
                        raw = call_lm_studio(
                            api_base=args.api_base,
                            model=args.model,
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            response_format=response_format,
                            temperature=args.temperature,
                            max_tokens=args.max_tokens,
                            request_timeout=args.request_timeout,
                        )
                        parsed = parse_llm_result(packet, raw)
                        assert isinstance(parsed, PlanningFollowthroughResult)
                        planning_issues = get_planning_validation_issues(packet, parsed, planning_ideas)
                        if not planning_issues:
                            break
                        last_error = RuntimeError("Planning validation failed: " + json.dumps(planning_issues, indent=2))
                        user_prompt = build_planning_retry_user_prompt(
                            packet=packet,
                            ideas=planning_ideas,
                            previous_result=parsed,
                            issues=planning_issues,
                        )
                    except ValidationError as exc:
                        last_error = exc
                        issue_lines = [
                            " -> ".join(str(part) for part in error.get("loc", [])) + f": {error.get('msg')}"
                            for error in exc.errors()
                        ] or [str(exc)]
                        if raw.strip():
                            user_prompt = build_schema_retry_user_prompt(
                                packet=packet,
                                raw_text=raw,
                                issues=issue_lines,
                            )
                    except json.JSONDecodeError as exc:
                        last_error = exc
                        if raw.strip():
                            user_prompt = build_schema_retry_user_prompt(
                                packet=packet,
                                raw_text=raw,
                                issues=[str(exc)],
                            )
                    except (httpx.HTTPError, RuntimeError) as exc:
                        last_error = exc
            else:
                user_prompt = build_user_prompt(packet)
                response_format = build_response_format(packet)
                for _ in range(max(args.max_retries, 1)):
                    raw = ""
                    try:
                        raw = call_lm_studio(
                            api_base=args.api_base,
                            model=args.model,
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            response_format=response_format,
                            temperature=args.temperature,
                            max_tokens=args.max_tokens,
                            request_timeout=args.request_timeout,
                        )
                        parsed = parse_llm_result(packet, raw)
                        assert isinstance(parsed, GenerationCandidate)
                        validated_payload = validate_candidate(client, parsed)
                        continuity_issues = collect_character_continuity_issues(
                            packet=packet,
                            candidate=parsed,
                        )
                        scene_anchor_issues = collect_scene_anchor_art_issues(
                            packet=packet,
                            candidate=parsed,
                        )
                        combined_extra_issues = continuity_issues + scene_anchor_issues
                        if combined_extra_issues:
                            validated_payload["valid"] = False
                            validated_payload["issues"] = list(validated_payload.get("issues", [])) + combined_extra_issues
                        if validated_payload["valid"]:
                            break
                        last_error = RuntimeError(
                            f"Validation failed: {json.dumps(validated_payload['issues'], indent=2)}"
                        )
                        user_prompt = build_validation_retry_user_prompt(
                            packet=packet,
                            previous_candidate=parsed,
                            issues=validated_payload["issues"],
                        )
                    except ValidationError as exc:
                        last_error = exc
                        issue_lines = [
                            " -> ".join(str(part) for part in error.get("loc", [])) + f": {error.get('msg')}"
                            for error in exc.errors()
                        ] or [str(exc)]
                        if raw.strip():
                            user_prompt = build_schema_retry_user_prompt(
                                packet=packet,
                                raw_text=raw,
                                issues=issue_lines,
                            )
                    except json.JSONDecodeError as exc:
                        last_error = exc
                        if raw.strip():
                            user_prompt = build_schema_retry_user_prompt(
                                packet=packet,
                                raw_text=raw,
                                issues=[str(exc)],
                            )
                    except (httpx.HTTPError, RuntimeError) as exc:
                        last_error = exc
            if parsed is None:
                raise SystemExit(f"Local worker failed: {last_error}")

            if packet["run_mode"] == "planning":
                ideas_path = Path(args.ideas_file) if args.ideas_file else project_root / "IDEAS.md"
                result = apply_planning_result(
                    packet=packet,
                    ideas=planning_ideas,
                    result=parsed,
                    client=client,
                    ideas_path=ideas_path,
                    dry_run=args.dry_run,
                )
            else:
                assert isinstance(parsed, GenerationCandidate)
                result = apply_normal_result(
                    packet=packet,
                    candidate=parsed,
                    client=client,
                    dry_run=args.dry_run,
                    project_root=project_root,
                    validation_payload=validated_payload,
                )

        append_run_finished_log(log_path=log_path, args=args, result=result)
        print(json.dumps(result, indent=2))
    except BaseException as exc:
        append_run_failed_log(log_path=log_path, args=args, packet=packet, error=exc)
        raise


if __name__ == "__main__":
    main()
