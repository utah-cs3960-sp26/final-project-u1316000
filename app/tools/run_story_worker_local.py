from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, ValidationError

from app.main import create_app
from app.models import DirectionNoteProposal, GenerationCandidate


class PlanningChoiceUpdate(BaseModel):
    choice_id: int
    notes: str


class PlanningResult(BaseModel):
    ideas_to_append: list[str] = Field(default_factory=list)
    choice_note_updates: list[PlanningChoiceUpdate] = Field(default_factory=list)
    story_direction_notes: list[DirectionNoteProposal] = Field(default_factory=list)
    summary: str | None = None


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
        response_schema = {
            "ideas_to_append": ["idea one", "idea two", "idea three"],
            "choice_note_updates": [
                {"choice_id": 0, "notes": "Goal: ... Intent: ..."}
            ],
            "story_direction_notes": [
                {
                    "note_type": "plotline",
                    "title": "Optional",
                    "note_text": "Optional",
                    "status": "active",
                    "priority": 2,
                }
            ],
            "summary": "Optional planning summary.",
        }
        return (
            "This is a planning-mode packet.\n"
            "Do not generate or apply a scene.\n"
            "Return only JSON matching this shape:\n"
            f"{json.dumps(response_schema, indent=2)}\n\n"
            "Packet:\n"
            f"{json.dumps(packet, indent=2)}"
        )

    response_schema = GenerationCandidate.model_json_schema()
    return (
        "This is a normal story-worker packet.\n"
        "Return only a valid GenerationCandidate JSON object.\n"
        "JSON schema:\n"
        f"{json.dumps(response_schema, indent=2)}\n\n"
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
    response = httpx.post(url, json=payload, timeout=300.0)
    response.raise_for_status()
    data = response.json()
    message = data["choices"][0]["message"]
    content = message.get("content") or ""
    if content.strip():
        return content
    reasoning_content = message.get("reasoning_content") or ""
    if reasoning_content.strip():
        return reasoning_content
    raise RuntimeError("LM Studio returned neither content nor reasoning_content.")


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


def get_test_client() -> TestClient:
    return TestClient(create_app())


def append_ideas(ideas_path: Path, ideas: list[str]) -> None:
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
        cleaned = idea.strip()
        if not cleaned:
            continue
        output += f"- {cleaned}\n"
    ideas_path.write_text(output.rstrip() + "\n", encoding="utf-8")


def apply_planning_result(
    *,
    packet: dict[str, Any],
    result: PlanningResult,
    client: TestClient,
    ideas_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    choice_updates: list[int] = []
    note_ids: list[int] = []
    if not dry_run:
        append_ideas(ideas_path, result.ideas_to_append)
        for update in result.choice_note_updates:
            response = client.post(f"/choices/{update.choice_id}", json={"notes": update.notes})
            response.raise_for_status()
            choice_updates.append(update.choice_id)
        for note in result.story_direction_notes:
            response = client.post("/story-notes", json=note.model_dump())
            response.raise_for_status()
            note_ids.append(int(response.json()["id"]))
    else:
        choice_updates = [update.choice_id for update in result.choice_note_updates]

    return {
        "run_mode": "planning",
        "planning_reason": packet.get("planning_reason"),
        "dry_run": dry_run,
        "updated_choice_ids": choice_updates,
        "ideas_added": len(result.ideas_to_append),
        "story_notes_added": len(note_ids) if not dry_run else len(result.story_direction_notes),
        "summary": result.summary,
    }


def apply_normal_result(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
    client: TestClient,
    dry_run: bool,
) -> dict[str, Any]:
    validation = client.post("/jobs/validate-generation", json=candidate.model_dump(mode="json"))
    validation.raise_for_status()
    validation_payload = validation.json()
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

    return {
        "run_mode": "normal",
        "dry_run": False,
        "pre_change_url": packet["pre_change_url"],
        "expanded_choice_id": packet["selected_frontier_item"]["choice_id"],
        "expanded_choice_text": packet["selected_frontier_item"]["choice_text"],
        "new_node_id": applied["node"]["id"],
        "new_node_title": applied["node"]["title"],
        "created_choice_ids": [choice["id"] for choice in applied["created_choices"]],
        "generated_asset_count": len(generated_assets),
        "hooks_added": len(candidate.new_hooks),
        "global_direction_notes_added": len(candidate.global_direction_notes),
    }


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
    for _ in range(max(args.max_retries, 1)):
        try:
            raw = call_lm_studio(
                api_base=args.api_base,
                model=args.model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_format=response_format,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
            )
            parsed = parse_llm_result(packet, raw)
            break
        except (httpx.HTTPError, json.JSONDecodeError, ValidationError, RuntimeError) as exc:
            last_error = exc
    if parsed is None:
        raise SystemExit(f"Local worker failed: {last_error}")

    with get_test_client() as client:
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
            result = apply_normal_result(
                packet=packet,
                candidate=parsed,
                client=client,
                dry_run=args.dry_run,
            )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
