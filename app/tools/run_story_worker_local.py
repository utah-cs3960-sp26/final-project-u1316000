from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
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


class PlanningChoiceUpdate(BaseModel):
    choice_id: int
    notes: str


PlanningIdeaCategory = Literal["character", "location", "object", "event"]


class PlanningIdea(BaseModel):
    category: PlanningIdeaCategory
    title: str
    note_text: str


class PlanningResult(BaseModel):
    ideas_to_append: list[PlanningIdea] = Field(default_factory=list)
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
            "Use the existing IDEAS.md content in the packet as shared memory.\n"
            "Add only unique ideas that are not already present there.\n"
            "Do not reuse or lightly remix example seeds from the docs or packet.\n"
            "The API already supplied the required structured-output schema. Return only JSON.\n\n"
            "Packet:\n"
            f"{json.dumps(packet, indent=2)}"
        )

    return (
        "This is a normal story-worker packet.\n"
        "The API already supplied the required GenerationCandidate structured-output schema.\n"
        "If validation issues are returned later, revise the JSON and try again until it passes validation.\n"
        "Return only the JSON object, with no markdown fences or extra commentary.\n\n"
        "Packet:\n"
        f"{json.dumps(packet, indent=2)}"
    )


def build_response_format(packet: dict[str, Any]) -> dict[str, Any]:
    if packet["run_mode"] == "planning":
        schema = PlanningResult.model_json_schema()
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "planning_result",
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
        return Path(mock_response_path).read_text(encoding="utf-8")

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


def parse_llm_result(packet: dict[str, Any], raw_text: str) -> GenerationCandidate | PlanningResult:
    json_text = extract_json_text(raw_text)
    if packet["run_mode"] == "planning":
        return PlanningResult.model_validate_json(json_text)
    return GenerationCandidate.model_validate_json(json_text)


def build_validation_retry_user_prompt(
    *,
    packet: dict[str, Any],
    previous_candidate: GenerationCandidate,
    issues: list[str],
) -> str:
    return (
        "Your previous GenerationCandidate failed validation.\n"
        "Fix the listed issues and return a corrected GenerationCandidate JSON object only.\n"
        "Do not explain the changes. Do not stop. Keep trying until validation passes.\n\n"
        "Important reminders:\n"
        "- Every choice.notes value must literally include both 'Goal:' and 'Intent:' with meaningful content.\n"
        "- Set target_node_id to null unless you are intentionally quick-merging into one of the listed merge_candidates.\n"
        "- Reuse existing art instead of requesting duplicate generation.\n\n"
        "- Do not name a canonical character in scene text or choice text unless they have already appeared on this path or you are explicitly introducing them now.\n\n"
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
        "- Every choice.notes value must literally include both 'Goal:' and 'Intent:' with meaningful content.\n"
        "- Set target_node_id to null unless you are intentionally quick-merging into one of the listed merge_candidates.\n"
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
    previous_result: PlanningResult,
    issues: list[str],
) -> str:
    return (
        "Your previous planning result failed validation.\n"
        "Fix the listed planning issues and return a corrected planning JSON object only.\n"
        "Do not explain the changes. Do not stop. Keep trying until the planning result passes validation.\n\n"
        "Important reminders:\n"
        "- Read the current IDEAS.md content in the packet before proposing new ideas.\n"
        "- Every new planning idea must be genuinely new, not already present in IDEAS.md.\n"
        "- Do not reuse built-in example seeds from the docs or prior examples.\n"
        "- Across the run, your ideas must span at least 2 categories across character/location/object/event.\n"
        "- Update at least one frontier choice note.\n\n"
        f"Planning issues:\n{json.dumps(issues, indent=2)}\n\n"
        "Previous invalid planning result:\n"
        f"{json.dumps(previous_result.model_dump(mode='json'), indent=2)}\n\n"
        "Original packet:\n"
        f"{json.dumps(packet, indent=2)}"
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
        allowed_names = {
            (character.get("name") or "").strip().lower()
            for character_id in encountered_ids
            if (character := canon.get_character(character_id)) is not None and (character.get("name") or "").strip()
        }
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


def get_planning_validation_issues(packet: dict[str, Any], result: PlanningResult) -> list[str]:
    issues: list[str] = []
    required_count = int((packet.get("planning_policy") or {}).get("ideas_per_run") or 0)
    if len(result.ideas_to_append) < max(required_count, 2):
        issues.append(
            f"Planning mode requires at least {max(required_count, 2)} categorized ideas, but only {len(result.ideas_to_append)} were returned."
        )

    categories = {idea.category for idea in result.ideas_to_append}
    if len(categories) < 2:
        issues.append(
            "Planning mode ideas must span at least 2 categories across character/location/object/event."
        )

    if not result.choice_note_updates:
        issues.append("Planning mode must update at least one frontier choice note.")

    existing_content = normalize_idea_text(((packet.get("ideas_file") or {}).get("current_content") or ""))
    seen_titles: set[str] = set()
    seen_signatures: set[str] = set()
    for idea in result.ideas_to_append:
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
    return issues


def validate_planning_result(packet: dict[str, Any], result: PlanningResult) -> None:
    issues = get_planning_validation_issues(packet, result)
    if issues:
        raise RuntimeError("\n".join(issues))


def apply_planning_result(
    *,
    packet: dict[str, Any],
    result: PlanningResult,
    client: TestClient,
    ideas_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    validate_planning_result(packet, result)
    choice_updates: list[int] = []
    note_records: list[dict[str, Any]] = []
    if not dry_run:
        append_ideas(ideas_path, result.ideas_to_append)
        for update in result.choice_note_updates:
            response = client.post(f"/choices/{update.choice_id}", json={"notes": update.notes})
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
            }
            for update in result.choice_note_updates
        ],
        "ideas_added": len(result.ideas_to_append),
        "ideas_appended": [idea.model_dump() for idea in result.ideas_to_append],
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
        "hooks_added": len(candidate.new_hooks),
        "global_direction_notes_added": len(candidate.global_direction_notes),
    }


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
    packet = run_prepare_story_run(args, project_root)
    worker_guide = load_worker_guide(project_root)
    system_prompt = build_system_prompt(worker_guide)
    user_prompt = build_user_prompt(packet)
    response_format = build_response_format(packet)

    last_error: Exception | None = None
    parsed: GenerationCandidate | PlanningResult | None = None
    validated_payload: dict[str, Any] | None = None

    with get_test_client() as client:
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
                if packet["run_mode"] == "planning":
                    assert isinstance(parsed, PlanningResult)
                    planning_issues = get_planning_validation_issues(packet, parsed)
                    if not planning_issues:
                        break
                    last_error = RuntimeError("Planning validation failed: " + json.dumps(planning_issues, indent=2))
                    user_prompt = build_planning_retry_user_prompt(
                        packet=packet,
                        previous_result=parsed,
                        issues=planning_issues,
                    )
                    continue
                assert isinstance(parsed, GenerationCandidate)
                validated_payload = validate_candidate(client, parsed)
                continuity_issues = collect_character_continuity_issues(
                    packet=packet,
                    candidate=parsed,
                )
                if continuity_issues:
                    validated_payload["valid"] = False
                    validated_payload["issues"] = list(validated_payload.get("issues", [])) + continuity_issues
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

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
