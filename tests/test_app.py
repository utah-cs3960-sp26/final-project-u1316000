from __future__ import annotations

import contextlib
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image
from fastapi.testclient import TestClient

from app.database import bootstrap_database, connect
from app.main import create_app
from app.services.assets import AssetService
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver
from app.services.generation import LLMGenerationService
from app.services.story_graph import StoryGraphService
from app.services.story_setup import StorySetupService
from app.tools.prepare_story_run import build_validation_checklist
from app.tools.run_story_worker_codex_human import (
    build_codex_step_prompt,
    build_worker_command,
    extract_last_agent_message_from_jsonl,
    extract_thread_id_from_jsonl,
    request_nonempty_codex_step,
    strip_markdown_fences,
)
from app.tools.run_story_worker_local import (
    ScenePlanDraft,
    SceneBodyDraft,
    ChoiceDraft,
    SceneExtrasDraft,
    SceneHooksDraft,
    SceneArtDraft,
    NormalRunConversationState,
    PlanningIdea,
    build_asset_prompt,
    build_form_template,
    build_normal_conversation_system_prompt,
    build_step_prompt,
    build_forced_choice_draft,
    call_lm_studio,
    request_nonempty_ai_step_response,
    request_human_step,
    collect_branch_pressure_issues,
    collect_character_continuity_issues,
    collect_redundant_progression_issues,
    get_planning_idea_issues,
    infer_missing_asset_requests,
    collect_ungrounded_local_prop_issues,
    normalize_generation_candidate_payload,
    parse_choice_form,
    parse_llm_result,
    parse_scene_body_form,
    parse_scene_script_command,
    parse_transition_node_form,
    parse_scene_extras_form,
    parse_scene_hooks_form,
    parse_scene_art_form,
    parse_scene_plan_form,
    compile_scene_body_draft,
    prune_existing_asset_requests,
    normalize_visible_generic_speakers,
    resolve_scene_cast_names,
    validate_scene_plan_draft,
    repair_generation_candidate,
    validate_choice_draft,
    validate_scene_body_draft,
    validate_transition_node_draft,
    validate_scene_hooks_draft,
    scene_body_issues_require_scene_plan_rewind,
    collect_scene_anchor_art_issues,
    load_or_create_normal_session,
    append_validation_attempt_log_record,
    append_validation_attempt_run_separator,
    apply_normal_result,
    is_force_next_override,
    validate_choice_menu,
)


def build_client(tmp_path: Path) -> tuple[TestClient, Path]:
    db_path = tmp_path / "test_world.db"
    app = create_app(db_path)
    client = TestClient(app)
    return client, db_path


def test_startup_creates_required_tables(tmp_path: Path) -> None:
    _, db_path = build_client(tmp_path)
    assert db_path.exists()

    with connect(db_path) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert {
        "locations",
        "characters",
        "objects",
        "relations",
        "facts",
        "story_nodes",
        "story_node_present_entities",
        "choices",
        "node_entities",
        "assets",
        "generation_jobs",
        "branch_state",
        "inventory_entries",
        "unlocked_affordances",
        "relationship_states",
        "branch_tags",
        "story_hooks",
        "story_direction_notes",
        "worldbuilding_notes",
        "worker_choice_failures",
    }.issubset(tables)


def test_seed_world_and_resolve_spatial_relation(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    response = client.post(
        "/seed-world",
        json={
            "premise": "A mystery surrounds a lonely farm.",
            "locations": [
                {"name": "Barn"},
                {"name": "Cabin"},
            ],
            "characters": [
                {"name": "Elias", "home_location_name": "Cabin"},
            ],
            "relations": [
                {
                    "subject_type": "location",
                    "subject_name": "Cabin",
                    "relation_type": "north_of",
                    "object_type": "location",
                    "object_name": "Barn",
                }
            ],
        },
    )

    assert response.status_code == 200

    with connect(db_path) as connection:
        canon = CanonResolver(connection)
        barn = canon.find_location_by_name("Barn")
        assert barn is not None
        cabin = canon.resolve_spatial_relation(anchor_location_id=barn["id"], relation_type="north_of")
        assert cabin is not None
        assert cabin["name"] == "Cabin"


def test_story_nodes_choices_and_entity_reuse(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/seed-world", json={"locations": [{"name": "Barn"}]})
    locations = client.get("/locations").json()
    barn_id = locations[0]["id"]

    first_node = client.post(
        "/story-nodes",
        json={
            "title": "At the Barn",
            "scene_text": "You are standing outside the barn.",
            "referenced_entities": [
                {"entity_type": "location", "entity_id": barn_id, "role": "current_scene"}
            ],
        },
    )
    assert first_node.status_code == 200
    first_node_id = first_node.json()["id"]

    second_node = client.post(
        "/story-nodes",
        json={
            "title": "Behind the Barn",
            "scene_text": "You circle around the rear of the barn.",
            "referenced_entities": [
                {"entity_type": "location", "entity_id": barn_id, "role": "current_scene"}
            ],
        },
    )
    assert second_node.status_code == 200
    second_node_id = second_node.json()["id"]

    choice_response = client.post(
        "/choices",
        json={
            "from_node_id": first_node_id,
            "choice_text": "Walk around back",
            "to_node_id": second_node_id,
        },
    )
    assert choice_response.status_code == 200

    nodes_response = client.get("/story-nodes")
    assert nodes_response.status_code == 200
    nodes = nodes_response.json()
    assert len(nodes) == 2
    assert nodes[0]["entities"][0]["entity_id"] == barn_id
    assert nodes[1]["entities"][0]["entity_id"] == barn_id
    assert nodes[0]["choices"][0]["to_node_id"] == second_node_id


def test_objects_are_persisted_and_listed(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    response = client.post(
        "/seed-world",
        json={
            "locations": [{"name": "Barn"}],
            "objects": [
                {
                    "name": "Brass Compass",
                    "default_location_name": "Barn",
                    "canonical_summary": "A scratched compass with a twitching needle.",
                }
            ],
            "facts": [
                {
                    "entity_type": "object",
                    "entity_name": "Brass Compass",
                    "fact_text": "Its needle points toward unfinished promises.",
                    "is_locked": True,
                }
            ],
        },
    )
    assert response.status_code == 200

    objects_response = client.get("/objects")
    assert objects_response.status_code == 200
    objects = objects_response.json()
    assert len(objects) == 1
    assert objects[0]["name"] == "Brass Compass"

    story_response = client.post(
        "/story-nodes",
        json={
            "title": "Inventory Check",
            "scene_text": "You turn the brass compass over in your hand.",
            "referenced_entities": [
                {"entity_type": "object", "entity_id": objects[0]["id"], "role": "held"}
            ],
        },
    )
    assert story_response.status_code == 200
    assert story_response.json()["entities"][0]["entity_type"] == "object"


def test_asset_request_is_queued(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    response = client.post(
        "/assets/request",
        json={
            "job_type": "generate_portrait",
            "asset_kind": "portrait",
            "entity_type": "character",
            "entity_id": 12,
            "model_repo": "stabilityai/stable-diffusion-xl-base-1.0",
            "prompt": "Tall gnome with a tophat, painted portrait",
            "width": 1024,
            "height": 1024,
        },
    )
    assert response.status_code == 200
    assert response.json()["job_type"] == "asset_request"


def test_background_removal_rejects_missing_input(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    response = client.post(
        "/assets/remove-background",
        json={
            "source_image_path": str(tmp_path / "missing.png"),
            "entity_type": "object",
            "entity_id": 1,
        },
    )
    assert response.status_code == 400


def test_asset_service_prefers_latest_asset_for_entity_kind(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "assets_test.db"
    bootstrap_database(db_path)
    with connect(db_path) as connection:
        service = AssetService(connection, project_root)
        first = service.add_asset(
            entity_type="location",
            entity_id=1,
            asset_kind="background",
            file_path=str(project_root / "data" / "assets" / "generated" / "background" / "one.png"),
        )
        latest = service.add_asset(
            entity_type="location",
            entity_id=1,
            asset_kind="background",
            file_path=str(project_root / "data" / "assets" / "generated" / "background" / "two.png"),
        )

        selected = service.get_latest_asset(entity_type="location", entity_id=1, asset_kind="background")

    assert first["id"] < latest["id"]
    assert selected is not None
    assert selected["id"] == latest["id"]


def test_media_url_for_path_includes_mtime_version(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    asset_dir = project_root / "data" / "assets" / "test-fixtures"
    asset_dir.mkdir(parents=True, exist_ok=True)
    file_path = asset_dir / f"{tmp_path.name}-versioned.png"
    try:
        file_path.write_bytes(b"test")
        with connect(tmp_path / "media_url.db") as connection:
            service = AssetService(connection, project_root)
            media_url = service.media_url_for_path(file_path)

        assert media_url is not None
        assert f"/media/test-fixtures/{file_path.name}?v=" in media_url
    finally:
        if file_path.exists():
            file_path.unlink()
        try:
            asset_dir.rmdir()
        except OSError:
            pass


def test_comfyui_generation_registers_asset(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "comfy_output" / "background"
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_file = output_dir / "z-image_00001_.png"
    Image.new("RGB", (64, 64), color=(120, 80, 200)).save(generated_file)

    monkeypatch.setenv("COMFYUI_OUTPUT_DIR", str(tmp_path / "comfy_output"))

    from app.services import assets as assets_module

    captured_workflow: dict[str, object] = {}

    def fake_submit_workflow(self, workflow):
        captured_workflow.update(workflow)
        return "prompt-123"

    monkeypatch.setattr(assets_module.ComfyUIClient, "submit_workflow", fake_submit_workflow)
    monkeypatch.setattr(
        assets_module.ComfyUIClient,
        "wait_for_history",
        lambda self, prompt_id: {
            "outputs": {
                "9": {
                    "images": [
                        {
                            "filename": "z-image_00001_.png",
                            "subfolder": "background",
                            "type": "output",
                        }
                    ]
                }
            }
        },
    )

    def fake_import_generated_asset(self, *, source_path, entity_type, entity_id, asset_kind, filename_base, generated_root):
        imported = tmp_path / "imported" / asset_kind / f"{entity_type}_{entity_id}_{filename_base}.png"
        imported.parent.mkdir(parents=True, exist_ok=True)
        imported.write_bytes(source_path.read_bytes())
        return imported

    monkeypatch.setattr(assets_module.AssetService, "import_generated_asset", fake_import_generated_asset)

    client, _ = build_client(tmp_path)
    response = client.post(
        "/assets/generate",
        json={
            "asset_kind": "background",
            "entity_type": "location",
            "entity_id": 1,
            "prompt": "A giant mushroom field at dawn.",
            "workflow_name": "text-to-image",
            "filename_base": "mushroom-field-dawn",
            "width": 1600,
            "height": 896,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["prompt_id"] == "prompt-123"
    assert data["asset"]["asset_kind"] == "background"
    assert data["asset"]["entity_type"] == "location"
    assert data["asset"]["entity_id"] == 1
    assert Path(data["output_path"]).exists()
    prompt_text = captured_workflow["76:67"]["inputs"]["text"]  # type: ignore[index]
    assert "Style: Cinematic epic fantasy concept art" in prompt_text
    assert "A giant mushroom field at dawn." in prompt_text
    assert "Do not specify art style directions beyond the content itself." in prompt_text


def test_background_generation_prompt_enforces_environment_only_rules(tmp_path: Path) -> None:
    from app.services.assets import AssetService

    with connect(tmp_path / "background_prompt_test.db") as connection:
        service = AssetService(connection, tmp_path)
        prompt = service.compose_generation_prompt(
            asset_kind="background",
            user_prompt="A vaulted chamber of green glass seams and violet light.",
        )
        negative_prompt = service.compose_negative_prompt(
            asset_kind="background",
            user_negative_prompt=None,
        )

    assert "Environment scene only." in prompt
    assert "No characters, no hands, no bodies, no faces" in prompt
    assert "wide establishing shot" in prompt
    assert negative_prompt is not None
    assert "character" in negative_prompt
    assert "hand" in negative_prompt
    assert "isolated object" in negative_prompt


def test_background_generation_defaults_to_landscape_dimensions(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "comfy_output" / "background"
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_file = output_dir / "bg_00001_.png"
    Image.new("RGB", (64, 64), color=(100, 100, 140)).save(generated_file)

    monkeypatch.setenv("COMFYUI_OUTPUT_DIR", str(tmp_path / "comfy_output"))

    from app.services import assets as assets_module

    captured_workflow: dict[str, object] = {}

    def fake_submit_workflow(self, workflow):
        captured_workflow.update(workflow)
        return "prompt-background-defaults"

    monkeypatch.setattr(assets_module.ComfyUIClient, "submit_workflow", fake_submit_workflow)
    monkeypatch.setattr(
        assets_module.ComfyUIClient,
        "wait_for_history",
        lambda self, prompt_id: {
            "outputs": {
                "9": {
                    "images": [
                        {
                            "filename": "bg_00001_.png",
                            "subfolder": "background",
                            "type": "output",
                        }
                    ]
                }
            }
        },
    )

    def fake_import_generated_asset(self, *, source_path, entity_type, entity_id, asset_kind, filename_base, generated_root):
        imported = tmp_path / "imported" / asset_kind / f"{entity_type}_{entity_id}_{filename_base}.png"
        imported.parent.mkdir(parents=True, exist_ok=True)
        imported.write_bytes(source_path.read_bytes())
        return imported

    monkeypatch.setattr(assets_module.AssetService, "import_generated_asset", fake_import_generated_asset)

    client, _ = build_client(tmp_path)
    response = client.post(
        "/assets/generate",
        json={
            "asset_kind": "background",
            "entity_type": "location",
            "entity_id": 2,
            "prompt": "A vaulted chamber of green glass seams and violet light.",
            "workflow_name": "text-to-image",
        },
    )

    assert response.status_code == 200
    data = response.json()
    metadata = json.loads(data["asset"]["prompt_text"])
    assert metadata["width"] == 1600
    assert metadata["height"] == 896
    assert metadata["negative_prompt"] is not None
    latent_node = captured_workflow["76:68"]["inputs"]  # type: ignore[index]
    assert latent_node["width"] == 1600
    assert latent_node["height"] == 896


def test_character_generation_prompt_enforces_subject_only_rules(tmp_path: Path) -> None:
    from app.services.assets import AssetService

    with connect(tmp_path / "prompt_test.db") as connection:
        service = AssetService(connection, tmp_path)
        prompt = service.compose_generation_prompt(
            asset_kind="portrait",
            user_prompt="A tall gnome with a nervous smile and a velvet tophat, carrying too many keys.",
        )

    assert "Style: Cinematic epic fantasy concept art" in prompt
    assert "Plain white background." in prompt
    assert "subject is centered" in prompt
    assert "full body is in view" in prompt


def test_trim_transparent_canvas_removes_empty_padding(tmp_path: Path) -> None:
    from app.services.assets import AssetService

    with connect(tmp_path / "trim_test.db") as connection:
        service = AssetService(connection, tmp_path)
        image = Image.new("RGBA", (100, 100), color=(0, 0, 0, 0))
        for x in range(30, 70):
            for y in range(20, 80):
                image.putpixel((x, y), (200, 120, 90, 255))

        trimmed = service.trim_transparent_canvas(image)

    assert trimmed.size == (40, 60)


def test_normalize_cutout_frame_uses_standard_character_canvas(tmp_path: Path) -> None:
    from app.services.assets import AssetService

    with connect(tmp_path / "normalize_test.db") as connection:
        service = AssetService(connection, tmp_path)
        image = Image.new("RGBA", (120, 160), color=(0, 0, 0, 0))
        for x in range(20, 100):
            for y in range(20, 150):
                image.putpixel((x, y), (210, 150, 90, 255))

        normalized, metadata = service.normalize_cutout_frame(image, display_class="character-fullbody")

    assert normalized.size == (1024, 1536)
    assert metadata["display_class"] == "character-fullbody"
    assert metadata["content_ratio"]["height"] >= 0.89


def test_portrait_generation_automatically_creates_cutout(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "comfy_output" / "portrait"
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_file = output_dir / "portrait_00001_.png"
    Image.new("RGB", (64, 64), color=(50, 120, 180)).save(generated_file)

    monkeypatch.setenv("COMFYUI_OUTPUT_DIR", str(tmp_path / "comfy_output"))

    from app.services import assets as assets_module

    monkeypatch.setattr(assets_module.ComfyUIClient, "submit_workflow", lambda self, workflow: "prompt-portrait")
    monkeypatch.setattr(
        assets_module.ComfyUIClient,
        "wait_for_history",
        lambda self, prompt_id: {
            "outputs": {
                "9": {
                    "images": [
                        {
                            "filename": "portrait_00001_.png",
                            "subfolder": "portrait",
                            "type": "output",
                        }
                    ]
                }
            }
        },
    )

    def fake_import_generated_asset(self, *, source_path, entity_type, entity_id, asset_kind, filename_base, generated_root):
        imported = tmp_path / "imported" / asset_kind / f"{entity_type}_{entity_id}_{filename_base}.png"
        imported.parent.mkdir(parents=True, exist_ok=True)
        imported.write_bytes(source_path.read_bytes())
        return imported

    monkeypatch.setattr(assets_module.AssetService, "import_generated_asset", fake_import_generated_asset)

    def fake_remove_background(
        self,
        *,
        source_image_path,
        output_name=None,
        model_repo="briaai/RMBG-2.0",
        device="auto",
        entity_type=None,
        asset_kind=None,
    ):
        cutout = tmp_path / "cutouts" / (output_name or "portrait-cutout.png")
        cutout.parent.mkdir(parents=True, exist_ok=True)
        cutout.write_bytes(Path(source_image_path).read_bytes())
        return {
            "output_path": str(cutout),
            "display_class": "character-fullbody",
            "normalization": {"method": "test"},
        }

    monkeypatch.setattr(assets_module.AssetService, "remove_background", fake_remove_background)

    client, _ = build_client(tmp_path)
    response = client.post(
        "/assets/generate",
        json={
            "asset_kind": "portrait",
            "entity_type": "character",
            "entity_id": 7,
            "prompt": "A tall gnome with a worried expression, a velvet tophat, and too many pockets full of keys.",
            "workflow_name": "text-to-image",
            "filename_base": "tall-gnome-portrait",
            "width": 1024,
            "height": 1536,
            "remove_background": False,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["asset"]["asset_kind"] == "portrait"
    assert data["cutout_asset"] is not None
    assert data["cutout_asset"]["asset_kind"] == "cutout"
    assert data["cutout_asset"]["entity_type"] == "character"


def test_duplicate_location_is_not_recreated(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/seed-world", json={"locations": [{"name": "Cabin"}]})
    client.post("/seed-world", json={"locations": [{"name": "Cabin"}]})
    locations = client.get("/locations").json()
    assert len(locations) == 1


def test_story_reset_seeds_bucket_hat_protagonist(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    response = client.post("/story/reset-opening-canon")
    assert response.status_code == 200

    data = response.json()
    assert data["protagonist"]["name"] == "The Tall Gnome"
    assert data["opening_location"]["name"] == "Mushroom Field"
    assert data["default_branch"]["branch_key"] == "default"

    with connect(db_path) as connection:
        canon = CanonResolver(connection)
        facts = canon.list_facts()
        locked_facts = {fact["fact_text"] for fact in facts if fact["is_locked"]}

    assert "The protagonist's left hand has five thumbs." in locked_facts
    assert "The protagonist wears a red-and-white striped bucket hat and does not know how they got it." in locked_facts


def test_branch_state_tracks_affordances_and_tags(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    seed_response = client.post(
        "/seed-world",
        json={
            "objects": [{"name": "Goose Whistle"}],
            "characters": [{"name": "Post Goose"}],
        },
    )
    assert seed_response.status_code == 200

    objects = client.get("/objects").json()
    goose_whistle = next(item for item in objects if item["name"] == "Goose Whistle")

    tag_response = client.post(
        "/branches/default/tags",
        json={"tag": "open-sky", "tag_type": "state", "source": "test"},
    )
    assert tag_response.status_code == 200

    inventory_response = client.post(
        "/branches/default/inventory",
        json={"object_id": goose_whistle["id"], "notes": "Won from a very serious goose."},
    )
    assert inventory_response.status_code == 200

    affordance_response = client.post(
        "/branches/default/affordances",
        json={
            "name": "Call the Goose",
            "description": "Summon the post goose for a short ride or message delivery.",
            "source_object_id": goose_whistle["id"],
            "required_state_tags": ["open-sky"],
        },
    )
    assert affordance_response.status_code == 200

    branch_response = client.get("/branches/default/state")
    assert branch_response.status_code == 200
    branch = branch_response.json()
    assert any(item["object_name"] == "Goose Whistle" for item in branch["inventory"])
    assert any(affordance["name"] == "Call the Goose" for affordance in branch["affordances"])
    assert any(tag["tag"] == "open-sky" for tag in branch["tags"])


def test_seed_opening_story_creates_long_range_major_hooks(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    seed_response = client.post("/story/seed-opening-story")
    assert seed_response.status_code == 200

    hooks_response = client.get("/branches/default/hooks")
    assert hooks_response.status_code == 200
    hooks = hooks_response.json()
    summaries = {hook["summary"]: hook for hook in hooks}

    hat_hook = next(
        hook for hook in hooks
        if "bucket hat" in hook["summary"].lower() and hook["importance"] == "major"
    )
    body_hook = next(
        hook for hook in hooks
        if "five-thumbed left hand" in hook["summary"].lower() and hook["importance"] == "major"
    )

    assert hat_hook["min_distance_to_payoff"] == 20
    assert body_hook["min_distance_to_payoff"] == 20
    assert hat_hook["min_distance_to_next_development"] == 6
    assert body_hook["min_distance_to_next_development"] == 6
    assert hat_hook["introduced_at_depth"] == 0
    assert body_hook["introduced_at_depth"] == 0
    assert "same hidden past event" in (hat_hook["summary"] or "").lower()
    assert "friend, ally, or protector" in (hat_hook["payoff_concept"] or "").lower()
    assert hat_hook["must_not_imply"]
    assert "curse, mutilation, or hostile alteration" in (body_hook["summary"] or "").lower()
    assert "privileged access token" in (body_hook["payoff_concept"] or "").lower()
    assert body_hook["must_not_imply"]


def test_create_story_hook_route_persists_direction_fields(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    response = client.post(
        "/branches/default/hooks",
        json={
            "hook_type": "minor_mystery",
            "importance": "minor",
            "summary": "A brass slot hums your missing name but refuses to print it.",
            "payoff_concept": "The slot is connected to the same paperwork logic that keeps misnaming the protagonist.",
            "min_distance_to_next_development": 2,
            "must_not_imply": [
                "Do not treat the slot as a random one-scene gag.",
            ],
        },
    )

    assert response.status_code == 200
    hook = response.json()
    assert "paperwork logic" in hook["payoff_concept"]
    assert hook["min_distance_to_next_development"] == 2
    assert hook["must_not_imply"] == ["Do not treat the slot as a random one-scene gag."]


def test_seed_opening_story_backfills_existing_major_hook_direction(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    reset_response = client.post("/story/reset-opening-canon")
    assert reset_response.status_code == 200

    hook_response = client.post(
        "/branches/default/hooks",
        json={
            "hook_type": "identity_mystery",
            "importance": "major",
            "summary": (
                "The striped bucket hat, the lost first name, the amnesia, and waking in the Mushroom Field "
                "all point to the same hidden past event."
            ),
            "min_distance_to_payoff": 20,
        },
    )
    assert hook_response.status_code == 200
    assert hook_response.json()["payoff_concept"] is None

    seed_response = client.post("/story/seed-opening-story")
    assert seed_response.status_code == 200

    hooks = client.get("/branches/default/hooks").json()
    hat_hook = next(
        hook for hook in hooks
        if "bucket hat" in hook["summary"].lower() and hook["importance"] == "major"
    )
    assert "friend, ally, or protector" in (hat_hook["payoff_concept"] or "").lower()
    assert any("uniform gear" in guardrail.lower() for guardrail in hat_hook["must_not_imply"])


def test_story_note_creation_reuses_existing_title_and_type(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)

    first = client.post(
        "/story-notes",
        json={
            "note_type": "plotline",
            "title": "Recurring Pressure",
            "note_text": "First version.",
            "status": "active",
            "priority": 3,
        },
    )
    assert first.status_code == 200
    first_id = first.json()["id"]

    second = client.post(
        "/story-notes",
        json={
            "note_type": "plotline",
            "title": "Recurring Pressure",
            "note_text": "Revised version.",
            "status": "active",
            "priority": 5,
        },
    )
    assert second.status_code == 200
    assert second.json()["id"] == first_id
    assert second.json()["note_text"] == "Revised version."
    assert second.json()["priority"] == 5


def test_worldbuilding_creation_reuses_existing_title_and_type(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)

    first = client.post(
        "/worldbuilding",
        json={
            "note_type": "world_pressure",
            "title": "Dawn Sweep",
            "note_text": "First version.",
            "status": "active",
            "priority": 2,
            "pressure": 2,
        },
    )
    assert first.status_code == 200
    first_id = first.json()["id"]

    second = client.post(
        "/worldbuilding",
        json={
            "note_type": "world_pressure",
            "title": "Dawn Sweep",
            "note_text": "Revised version.",
            "status": "active",
            "priority": 4,
            "pressure": 5,
        },
    )
    assert second.status_code == 200
    assert second.json()["id"] == first_id
    assert second.json()["note_text"] == "Revised version."
    assert second.json()["pressure"] == 5


def test_hard_reset_story_reseeds_single_opening_and_clears_old_continuity(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "hard_reset.db"
    bootstrap_database(db_path)
    llm_generation = LLMGenerationService(project_root)

    with connect(db_path) as connection:
        canon = CanonResolver(connection)
        story = StoryGraphService(connection)
        branch_state = BranchStateService(connection, llm_generation.story_bible["acts"])

        old_location = canon.create_or_get_location(name="Bell Orchard", description="Old continuity.")
        old_character = canon.create_or_get_character(name="Clerk Nettle", description="Old continuity.")
        old_object = canon.create_or_get_object(name="Counterfoil Parchment", description="Old continuity.")
        opening = story.create_story_node(
            branch_key="default",
            title="Old Opening",
            scene_text="The previous story starts here.",
            summary="Old continuity.",
            referenced_entities=[{"entity_type": "location", "entity_id": int(old_location["id"]), "role": "current_scene"}],
            present_entities=[{"entity_type": "character", "entity_id": int(old_character["id"]), "slot": "hero-center", "focus": True}],
        )
        story.create_choice(
            from_node_id=int(opening["id"]),
            choice_text="Walk into the old story",
            status="open",
        )
        branch_state.create_hook(
            branch_key="default",
            hook_type="minor_mystery",
            importance="minor",
            summary="An obsolete branch hook.",
            linked_entity_type="object",
            linked_entity_id=int(old_object["id"]),
        )

        ideas_path = tmp_path / "IDEAS.md"
        ideas_path.write_text(
            "\n".join(
                [
                    "## Open Ideas",
                    "- [Character] Clerk Nettle's Rival: Old story specific.",
                    "- [Location] Bell Orchard: Old story specific.",
                    "- [Event] Transit Robbery: Still generic.",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        setup = StorySetupService(
            connection,
            project_root=project_root,
            story_bible=llm_generation.story_bible,
        )
        result = setup.hard_reset_story(
            branch_key="default",
            ideas_path=ideas_path,
            story_specific_idea_terms=["Clerk Nettle", "Bell Orchard", "Counterfoil Parchment"],
        )

        assert result["hook_count"] == 2
        assert result["worldbuilding_note_count"] == 4
        assert result["story_direction_note_count"] == 2

        locations = canon.list_locations()
        characters = canon.list_characters()
        objects = canon.list_objects()
        nodes = story.list_story_nodes()
        choices = story.list_choices()
        hooks = branch_state.list_hooks("default")

    assert [location["name"] for location in locations] == ["Mushroom Field"]
    assert [character["name"] for character in characters] == ["The Tall Gnome"]
    assert objects == []
    assert len(nodes) == 1
    assert result["start_node_id"] == nodes[0]["id"]
    assert nodes[0]["title"] == "Before the Counting Bell"
    assert len(choices) == 3
    assert all(choice["status"] == "open" for choice in choices)
    assert {choice["choice_class"] for choice in choices} == {"inspection", "progress"}
    assert all(choice["planning"] is not None for choice in choices)
    assert any("same hidden past event" in hook["summary"].lower() for hook in hooks)
    assert any("curse, mutilation, or hostile alteration" in hook["summary"].lower() for hook in hooks)

    ideas_text = ideas_path.read_text(encoding="utf-8")
    assert "Clerk Nettle" not in ideas_text
    assert "Bell Orchard" not in ideas_text
    assert "Transit Robbery" in ideas_text


def test_prune_story_specific_ideas_only_removes_matching_open_idea_entries(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "prune_ideas.db"
    bootstrap_database(db_path)
    llm_generation = LLMGenerationService(project_root)
    ideas_path = tmp_path / "IDEAS.md"
    ideas_path.write_text(
        "\n".join(
            [
                "# Ideas Scratchpad",
                "",
                "## Open Ideas",
                "- [Character] Madam Bei's Rival: Remove this one.",
                "- [Event] Tram Strike: Keep this one.",
                "- [Location] Velvet Platform Annex: Remove this one too.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with connect(db_path) as connection:
        setup = StorySetupService(
            connection,
            project_root=project_root,
            story_bible=llm_generation.story_bible,
        )
        result = setup.prune_story_specific_ideas(
            ideas_path=ideas_path,
            story_specific_terms=["Madam Bei", "Velvet Platform"],
        )

    rewritten = ideas_path.read_text(encoding="utf-8")
    assert result["removed_count"] == 2
    assert "Madam Bei's Rival" not in rewritten
    assert "Velvet Platform Annex" not in rewritten
    assert "Tram Strike" in rewritten


def test_generation_validation_blocks_early_major_hook_payoff(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    reset_response = client.post("/story/reset-opening-canon")
    assert reset_response.status_code == 200

    hook_response = client.post(
        "/branches/default/hooks",
        json={
            "hook_type": "identity_mystery",
            "importance": "major",
            "summary": "Where did the bucket hat come from?",
            "min_distance_to_payoff": 3,
            "required_clue_tags": ["hat-origin-clue"],
            "required_state_tags": ["saw-mirror-stitching"],
        },
    )
    assert hook_response.status_code == 200
    hook_id = hook_response.json()["id"]

    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "The hat reveals everything immediately.",
            "scene_text": "The hat tells you the entire truth far too early.",
            "choices": [{"choice_text": "Keep going", "notes": "NEXT_NODE: continue after the reveal. FURTHER_GOALS: move the branch onward after a supposed payoff."}],
            "hook_updates": [
                {
                    "hook_id": hook_id,
                    "status": "resolved",
                    "resolution_text": "The truth is revealed right away.",
                }
            ],
        },
    )

    assert validation_response.status_code == 200
    result = validation_response.json()
    assert result["valid"] is False
    assert any("min_distance_to_payoff" in issue for issue in result["issues"])
    assert any("required clue tags" in issue.lower() for issue in result["issues"])
    assert any("required state tags" in issue.lower() for issue in result["issues"])


def test_generation_validation_blocks_hook_development_during_cooldown(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/reset-opening-canon")

    hook_response = client.post(
        "/branches/default/hooks",
        json={
            "hook_type": "minor_mystery",
            "importance": "minor",
            "summary": "The bucket hat seam twitches when the bell rings.",
            "min_distance_to_next_development": 2,
        },
    )
    assert hook_response.status_code == 200
    hook_id = hook_response.json()["id"]

    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "The seam is interrogated again immediately.",
            "scene_text": "The scene tries to push the same hook again before enough distance has passed.",
            "choices": [
                {
                    "choice_text": "Keep tugging at the seam",
                    "notes": "NEXT_NODE: immediately press the same clue again. FURTHER_GOALS: force another development before the cooldown expires.",
                }
            ],
            "hook_updates": [
                {
                    "hook_id": hook_id,
                    "status": "active",
                    "progress_note": "The seam offers another clue too soon.",
                    "next_min_distance_to_development": 2,
                }
            ],
        },
    )

    assert validation_response.status_code == 200
    result = validation_response.json()
    assert result["valid"] is False
    assert any("development cooldown" in issue.lower() for issue in result["issues"])


def test_generation_validation_requires_hook_for_placeholder_mystery_entity(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/reset-opening-canon")

    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "A mushroom answers back.",
            "scene_text": "An unseen station voice speaks from inside the mushroom stalk.",
            "dialogue_lines": [
                {"speaker": "Narrator", "text": "The stalk knocks from the inside."},
                {"speaker": "Unseen Voice", "text": "The striped hat is expected below."},
            ],
            "entity_references": [
                {"entity_type": "location", "entity_id": 1, "role": "current_scene"},
            ],
            "choices": [{"choice_text": "Step closer", "notes": "NEXT_NODE: approach the speaker. FURTHER_GOALS: deepen the new stalk-side mystery."}],
        },
    )

    assert validation_response.status_code == 200
    result = validation_response.json()
    assert result["valid"] is False
    assert any("unresolved mystery/question" in issue.lower() for issue in result["issues"])
    assert any("current_scene location" in issue for issue in result["issues"])


def test_generation_validation_requires_goal_and_intent_notes_for_choices(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/reset-opening-canon")

    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "A simple branch option with no planning notes.",
            "scene_text": "The scene offers a choice but gives no intent behind it.",
            "choices": [{"choice_text": "Step toward the bell"}],
        },
    )

    assert validation_response.status_code == 422
    detail = validation_response.json().get("detail", [])
    assert any((item.get("loc") or [None])[-1] == "notes" for item in detail if isinstance(item, dict))


def test_generation_validation_allows_placeholder_mystery_when_hook_is_created_and_linked(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/reset-opening-canon")

    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "A mushroom answers back.",
            "scene_text": "An unseen station voice speaks from inside the mushroom stalk.",
            "dialogue_lines": [
                {"speaker": "Narrator", "text": "The stalk knocks from the inside."},
                {"speaker": "Unseen Voice", "text": "The striped hat is expected below."},
            ],
            "entity_references": [
                {"entity_type": "location", "entity_id": 1, "role": "current_scene"},
            ],
            "new_hooks": [
                {
                    "hook_type": "minor_mystery",
                    "importance": "minor",
                    "summary": "An unseen station voice inside the mushroom stalk recognizes the striped hat.",
                    "linked_entity_type": "location",
                    "linked_entity_id": 1,
                    "min_distance_to_payoff": 1,
                }
            ],
            "choices": [{"choice_text": "Step closer", "notes": "NEXT_NODE: approach the speaker. FURTHER_GOALS: keep the voice mystery alive as a true hook."}],
        },
    )

    assert validation_response.status_code == 200
    result = validation_response.json()
    assert result["valid"] is True


def test_generation_validation_rejects_visible_generic_speaker_without_named_character(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/reset-opening-canon")

    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "A patrol member shouts from nearby.",
            "scene_text": "A brass patrol member steps into view and points toward the seam.",
            "dialogue_lines": [
                {"speaker": "Brass Patrol Member", "text": "Hold still and keep your hands where I can count them."},
            ],
            "entity_references": [
                {"entity_type": "location", "entity_id": 1, "role": "current_scene"},
            ],
            "choices": [
                {
                    "choice_text": "Raise your altered hand slowly",
                    "notes": "NEXT_NODE: face the new pressure directly. FURTHER_GOALS: prove visible generic speaker labels are no longer allowed without a real named character behind them.",
                }
            ],
        },
    )

    assert validation_response.status_code == 200
    result = validation_response.json()
    assert result["valid"] is False
    assert any("Brass Patrol Member" in issue and "named existing character" in issue for issue in result["issues"])


def test_generation_validation_rejects_visible_named_existing_speaker_without_art(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    seed_response = client.post(
        "/seed-world",
        json={
            "characters": [
                {
                    "name": "Madam Bei",
                    "description": "A poised stationmaster frog with a brass ticket punch and careful eyes.",
                }
            ]
        },
    )
    assert seed_response.status_code == 200

    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "Madam Bei appears without any art coverage.",
            "scene_text": "Madam Bei steps from behind a mushroom and addresses the gnome directly.",
            "dialogue_lines": [
                {"speaker": "Madam Bei", "text": "You are standing where the dawn auditors will not forgive irregularities."},
            ],
            "entity_references": [
                {"entity_type": "location", "entity_id": 1, "role": "current_scene"},
                {"entity_type": "character", "entity_id": 2, "role": "introduced"},
            ],
            "choices": [
                {
                    "choice_text": "Ask Madam Bei what she means",
                    "notes": "NEXT_NODE: answer the interruption directly. FURTHER_GOALS: prove visible named speakers need portrait or cutout coverage.",
                }
            ],
        },
    )

    assert validation_response.status_code == 200
    result = validation_response.json()
    assert result["valid"] is False
    assert any("Madam Bei" in issue and "character art" in issue for issue in result["issues"])


def test_generation_validation_allows_offscreen_generic_speaker_without_art(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/reset-opening-canon")

    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "An offscreen patrol voice cuts through the mist.",
            "scene_text": "Metal footfalls remain hidden by the fog while a command rings across the field.",
            "dialogue_lines": [
                {"speaker": "Patrol Leader (O.S.)", "text": "Sector Seven sweep. Report all irregularities at once."},
            ],
            "entity_references": [
                {"entity_type": "location", "entity_id": 1, "role": "current_scene"},
            ],
            "choices": [
                {
                    "choice_text": "Drop lower behind the mushroom roots",
                    "notes": "NEXT_NODE: react to the offscreen voice without revealing yourself. FURTHER_GOALS: confirm unseen speakers are still allowed without visible character art.",
                }
            ],
        },
    )

    assert validation_response.status_code == 200
    result = validation_response.json()
    assert result["valid"] is True


def test_generation_validation_allows_unlocked_affordance_choice(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    seed_response = client.post("/seed-world", json={"objects": [{"name": "Goose Whistle"}]})
    assert seed_response.status_code == 200
    goose_whistle = client.get("/objects").json()[0]

    client.post("/branches/default/tags", json={"tag": "open-sky", "tag_type": "state"})
    client.post(
        "/branches/default/affordances",
        json={
            "name": "Call the Goose",
            "description": "Summon the goose for travel.",
            "source_object_id": goose_whistle["id"],
            "required_state_tags": ["open-sky"],
        },
    )

    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "The whistle becomes useful.",
            "scene_text": "Wind combs the mushrooms and the whistle feels warm in your pocket.",
            "choices": [
                {
                    "choice_text": "Blow the goose whistle",
                    "notes": "NEXT_NODE: call an emergency ride. FURTHER_GOALS: use an unlocked affordance to open a traversal branch.",
                    "required_affordances": ["Call the Goose"],
                }
            ],
        },
    )

    assert validation_response.status_code == 200
    result = validation_response.json()
    assert result["valid"] is True


def test_generation_validation_rejects_missing_affordance_choice(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "A goose option appears from nowhere.",
            "scene_text": "A choice references a goose that has never been earned.",
            "choices": [
                {
                    "choice_text": "Blow the goose whistle",
                    "notes": "NEXT_NODE: call a goose from nowhere. FURTHER_GOALS: shortcut the story with an unavailable affordance.",
                    "required_affordances": ["Call the Goose"],
                }
            ],
        },
    )

    assert validation_response.status_code == 200
    result = validation_response.json()
    assert result["valid"] is False
    assert any("unavailable affordances" in issue for issue in result["issues"])


def test_generation_preview_includes_story_bible_and_branch_state(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/reset-opening-canon")
    client.post("/story/seed-opening-story")
    client.post(
        "/story-notes",
        json={
            "note_type": "plotline",
            "title": "Tram escalation seed",
            "note_text": "A later tram ride could erupt into a transit crisis or robbery.",
        },
    )
    preview_response = client.post(
        "/jobs/generation-preview",
        json={
            "branch_key": "default",
            "focus_entity_ids": [1],
            "branch_summary": "The tall gnome has just awakened in the mushroom field.",
        },
    )

    assert preview_response.status_code == 200
    data = preview_response.json()
    assert data["job"]["job_type"] == "llm_generation_preview"
    assert data["context"]["story_bible"]["title"] == "The Tall Gnome's Impossible Hat"
    assert data["context"]["branch_state"]["branch_key"] == "default"
    assert "merge_candidates" in data["context"]
    assert len(data["context"]["merge_candidates"]) >= 1
    assert "branch_shape" in data["context"]
    assert "global_direction_notes" in data["context"]
    assert data["context"]["global_direction_notes"][0]["title"] == "Tram escalation seed"
    assert "Major mysteries must not resolve" in data["prompt"]


def test_frontier_returns_open_branch_ends(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    response = client.get("/frontier")
    assert response.status_code == 200
    items = response.json()
    assert len(items) >= 3
    assert all(item["choice_id"] is not None for item in items)
    assert all("selection_score" in item for item in items)
    assert all("selection_reason" in item for item in items)
    assert all("branch_shape" in item for item in items)
    assert all("merge_pressure_level" in item["branch_shape"] for item in items)


def test_frontier_rebalances_toward_old_shallow_opening_frontier_when_branch_gets_very_deep(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    with connect(db_path) as connection:
        story = StoryGraphService(connection)
        branch_state = BranchStateService(connection, client.app.state.llm_generation.story_bible["acts"])
        connection.execute(
            "UPDATE choices SET created_at = datetime('now', '-10 days') WHERE from_node_id IN (2, 3, 4)"
        )

        current_node_id = 2
        for index in range(12):
            open_choice = story.create_choice(
                from_node_id=current_node_id,
                choice_text=f"Continue deeper {index}",
                to_node_id=None,
                status="open",
                commit=False,
            )
            next_node = story.create_story_node(
                branch_key="default",
                title=f"Deep Node {index}",
                scene_text="The branch keeps descending.",
                summary="A deeper continuation node.",
                parent_node_id=current_node_id,
                commit=False,
            )
            connection.execute(
                "UPDATE choices SET to_node_id = ?, status = 'fulfilled' WHERE id = ?",
                (int(next_node["id"]), int(open_choice["id"])),
            )
            current_node_id = int(next_node["id"])
        story.create_choice(
            from_node_id=current_node_id,
            choice_text="Final deep unresolved choice",
            to_node_id=None,
            status="open",
            commit=False,
        )
        connection.commit()
        branch_state.sync_branch_progress("default")

        frontier = story.list_frontier(
            branch_state_service=branch_state,
            branch_key="default",
            limit=20,
            mode="auto",
            branching_policy=client.app.state.llm_generation.story_bible.get("branching_policy"),
        )

    assert frontier
    assert int(frontier[0]["from_node_id"]) in {2, 3, 4}
    assert int(frontier[0]["depth"]) <= 1


def test_generation_validation_blocks_merge_only_scene_when_branch_needs_divergence(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    with connect(db_path) as connection:
        story = StoryGraphService(connection)
        branch_state = BranchStateService(connection, client.app.state.llm_generation.story_bible["acts"])
        branch_state.sync_branch_progress("default")

        node_one = story.create_story_node(
            branch_key="default",
            title="Quick Merge One",
            scene_text="A tiny detour points back toward the tracks.",
            summary="First merge-only detour.",
            parent_node_id=4,
        )
        story.create_choice(
            from_node_id=int(node_one["id"]),
            choice_text="Rejoin the silver tracks",
            to_node_id=2,
            status="fulfilled",
        )

        node_two = story.create_story_node(
            branch_key="default",
            title="Quick Merge Two",
            scene_text="Another tiny detour still points back to the main line.",
            summary="Second merge-only detour.",
            parent_node_id=int(node_one["id"]),
        )
        story.create_choice(
            from_node_id=int(node_two["id"]),
            choice_text="Rejoin the silver tracks again",
            to_node_id=2,
            status="fulfilled",
        )

    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "Yet another tiny detour that only merges back into the same thing.",
            "scene_text": "The scene exists only to fold back into an existing branch again.",
            "choices": [
                {
                    "choice_text": "Return to the silver tracks",
                    "notes": "NEXT_NODE: fold back into the main clue trail. FURTHER_GOALS: test whether over-merged branches are forced to diverge.",
                    "target_node_id": 2,
                }
            ],
        },
    )

    assert validation_response.status_code == 200
    result = validation_response.json()
    assert result["valid"] is False
    assert any("quick-merged too often recently" in issue for issue in result["issues"])


def test_apply_generation_writes_node_and_branch_state_atomically(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier_item = client.get("/frontier").json()[0]

    response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": frontier_item["from_node_id"],
            "choice_id": frontier_item["choice_id"],
            "candidate": {
                "branch_key": "default",
                "scene_title": "The Velvet Mushroom",
                "scene_summary": "The marked mushroom answers with a weird little secret.",
                "scene_text": "You follow the groove to a mushroom that seems to be waiting for you.",
                "dialogue_lines": [
                    {"speaker": "Narrator", "text": "The velvet-marked mushroom leans closer, although mushrooms should not lean."},
                    {"speaker": "You", "text": "That feels rude, somehow."},
                ],
                "scene_present_entities": [
                    {"entity_type": "character", "entity_id": 1, "slot": "hero-center", "focus": True, "scale": 1.16}
                ],
                "choices": [
                    {
                        "choice_text": "Knock on the mushroom stem",
                        "notes": "NEXT_NODE: test whether the mushroom answers. FURTHER_GOALS: open a fresh mystery path at the marked stem.",
                    },
                    {
                        "choice_text": "Circle around the velvet knot",
                        "notes": "NEXT_NODE: inspect the marker from another angle. FURTHER_GOALS: widen the local branch with a clue-focused alternative.",
                    },
                ],
                "global_direction_notes": [
                    {
                        "note_type": "plotline",
                        "title": "Tram action escalation seed",
                        "note_text": "A later tram ride could tip into a robbery or transit crisis.",
                    }
                ],
                "discovered_clue_tags": ["velvet-mushroom-found"],
            },
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["node"]["title"] == "The Velvet Mushroom"
    assert len(data["created_choices"]) == 2

    nodes = client.get("/story-nodes").json()
    created_node = next(node for node in nodes if node["title"] == "The Velvet Mushroom")
    assert len(created_node["present_entities"]) == 1
    assert created_node["choices"][0]["to_node_id"] is None

    refreshed_frontier = client.get("/frontier").json()
    assert all(item["choice_id"] != frontier_item["choice_id"] for item in refreshed_frontier)

    branch_state = client.get("/branches/default/state").json()
    assert any(tag["tag"] == "velvet-mushroom-found" for tag in branch_state["tags"])
    notes = client.get("/story-notes").json()
    assert any(note["title"] == "Tram action escalation seed" for note in notes)


def test_apply_generation_inherits_scene_location_and_present_entities_when_omitted(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier_item = client.get("/frontier").json()[0]
    parent_story = client.get(f"/play?branch_key=default&scene={frontier_item['from_node_id']}")
    assert parent_story.status_code == 200

    response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": frontier_item["from_node_id"],
            "choice_id": frontier_item["choice_id"],
            "candidate": {
                "branch_key": "default",
                "scene_summary": "A continuation scene with no explicit staging metadata.",
                "scene_text": "First paragraph of narration.\n\nSecond paragraph of narration.",
                "choices": [
                    {
                        "choice_text": "Keep going",
                        "notes": "NEXT_NODE: keep the same scene moving. FURTHER_GOALS: confirm inherited staging keeps the same visual context when metadata is omitted.",
                    }
                ],
            },
        },
    )

    assert response.status_code == 200
    node = response.json()["node"]
    assert any(entity["role"] == "current_scene" for entity in node["entities"])
    assert node["present_entities"]

    branch_nodes = client.get("/story-nodes").json()
    created_node = next(row for row in branch_nodes if row["id"] == node["id"])
    assert len(created_node["dialogue_lines"]) == 2


def test_player_view_prefers_current_scene_location_over_mentioned_locations(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    node_response = client.post(
        "/story-nodes",
        json={
            "branch_key": "default",
            "title": "Location Priority Check",
            "scene_text": "A test scene with a mentioned place and a current place.",
            "summary": "The current scene should win over any merely mentioned location.",
            "referenced_entities": [
                {"entity_type": "location", "entity_id": 2, "role": "mentioned"},
                {"entity_type": "location", "entity_id": 1, "role": "current_scene"},
            ],
            "present_entities": [
                {"entity_type": "character", "entity_id": 1, "slot": "hero-center", "focus": True},
            ],
        },
    )

    assert node_response.status_code == 200
    node = node_response.json()
    page = client.get(f"/play?branch_key=default&scene={node['id']}")
    assert page.status_code == 200
    match = re.search(r'<script id="player-story-data" type="application/json">(.*?)</script>', page.text, re.S)
    assert match is not None
    player_data = json.loads(html.unescape(match.group(1)))
    scene = player_data["scenes"][str(node["id"])]
    assert scene["location_entity_id"] == 1
    assert scene["location"] == "Mushroom Field"


def test_story_node_creation_rejects_zero_entity_ids_in_present_entities(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    node_response = client.post(
        "/story-nodes",
        json={
            "branch_key": "default",
            "title": "Bad Entity Id",
            "scene_text": "This should be rejected.",
            "summary": "Invalid staged entity id.",
            "referenced_entities": [
                {"entity_type": "location", "entity_id": 1, "role": "current_scene"},
            ],
            "present_entities": [
                {"entity_type": "character", "entity_id": 0, "slot": "hero-center", "focus": True},
            ],
        },
    )

    assert node_response.status_code == 422


def test_apply_generation_allows_quick_merge_choice_target(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    seed = client.post("/story/seed-opening-story").json()
    assert seed["start_node_id"] >= 1

    nodes = client.get("/story-nodes").json()
    hand_node = next(node for node in nodes if node["title"] == "Five Thumbs")
    velvet_node = next(node for node in nodes if node["title"] == "Silver Tracks")
    frontier_item = next(item for item in client.get("/frontier").json() if item["from_node_id"] == hand_node["id"])

    response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": frontier_item["from_node_id"],
            "choice_id": frontier_item["choice_id"],
            "candidate": {
                "branch_key": "default",
                "scene_title": "A Small Omen",
                "scene_summary": "The inspection reveals a clue and then narrows back toward an existing branch.",
                "scene_text": "The plate rings softly, pointing your attention back toward the silver grooves.",
                "choices": [
                    {
                        "choice_text": "Follow the grooves after all",
                        "notes": "NEXT_NODE: return to the main clue trail. FURTHER_GOALS: quick-merge this minor omen back into the silver-track line.",
                        "target_node_id": velvet_node["id"],
                    }
                ],
            },
        },
    )

    assert response.status_code == 200
    data = response.json()
    created_choice = data["created_choices"][0]
    assert created_choice["to_node_id"] == velvet_node["id"]
    assert created_choice["status"] == "fulfilled"


def test_apply_generation_wraps_merge_choice_in_transition_node_when_requested(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    seed = client.post("/story/seed-opening-story").json()
    assert seed["start_node_id"] >= 1

    nodes = client.get("/story-nodes").json()
    hand_node = next(node for node in nodes if node["title"] == "Five Thumbs")
    velvet_node = next(node for node in nodes if node["title"] == "Silver Tracks")
    frontier_item = next(item for item in client.get("/frontier").json() if item["from_node_id"] == hand_node["id"])

    response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": frontier_item["from_node_id"],
            "choice_id": frontier_item["choice_id"],
            "candidate": {
                "branch_key": "default",
                "scene_title": "A Higher Detour",
                "scene_summary": "The inspection rises briefly into a higher angle before flowing back into the older trail.",
                "scene_text": "You climb the wet stem and catch the grooves again from above.",
                "choices": [
                    {
                        "choice_text": "Drop back toward the silver tracks",
                        "notes": "NEXT_NODE: you descend and recover the older trail. FURTHER_GOALS: merge this brief detour back into the silver-track line without teleporting.",
                        "target_node_id": velvet_node["id"],
                    }
                ],
                "transition_nodes": [
                    {
                        "choice_list_index": 0,
                        "scene_title": "Back Down the Stem",
                        "scene_summary": "You descend from the mushroom and pick up the older line of grooves again.",
                        "scene_text": "You slide down the slick stem, cross the mossy roots, and recover the silver grooves just as the older trail opens ahead of you again.",
                        "dialogue_lines": [
                            {"speaker": "Narrator", "text": "You slide down the slick stem and recover the silver grooves."}
                        ],
                    }
                ],
            },
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["created_transition_nodes"]) == 1
    transition_node = data["created_transition_nodes"][0]
    assert transition_node["node_kind"] == "transition"
    assert transition_node["auto_continue_to_node_id"] == velvet_node["id"]

    created_choice = data["created_choices"][0]
    assert created_choice["to_node_id"] == transition_node["id"]
    assert created_choice["to_node_id"] != velvet_node["id"]

    page = client.get(f"/play?branch_key=default&scene={transition_node['id']}")
    assert page.status_code == 200
    match = re.search(r'<script id="player-story-data" type="application/json">(.*?)</script>', page.text, re.S)
    assert match is not None
    player_data = json.loads(html.unescape(match.group(1)))
    transition_scene = player_data["scenes"][str(transition_node["id"])]
    assert transition_scene["node_kind"] == "transition"
    assert transition_scene["auto_continue_to_scene"] == str(velvet_node["id"])
    assert transition_scene["choices"] == []


def test_apply_generation_rejects_invalid_candidate_without_partial_write(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier_item = client.get("/frontier").json()[0]
    before_nodes = client.get("/story-nodes").json()

    response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": frontier_item["from_node_id"],
            "choice_id": frontier_item["choice_id"],
            "candidate": {
                "branch_key": "default",
                "scene_summary": "An invalid candidate.",
                "scene_text": "This tries to apply without choices.",
                "choices": [],
            },
        },
    )

    assert response.status_code == 400
    after_nodes = client.get("/story-nodes").json()
    assert len(after_nodes) == len(before_nodes)


def test_apply_generation_rejects_missing_affordance_choice_write(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier_item = client.get("/frontier").json()[0]

    response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": frontier_item["from_node_id"],
            "choice_id": frontier_item["choice_id"],
            "candidate": {
                "branch_key": "default",
                "scene_summary": "A goose appears from nowhere.",
                "scene_text": "An impossible shortcut is offered.",
                "choices": [
                    {
                        "choice_text": "Blow the goose whistle",
                        "notes": "NEXT_NODE: summon a nonexistent goose. FURTHER_GOALS: force a branch through an unavailable affordance.",
                        "required_affordances": ["Call the Goose"],
                    }
                ],
            },
        },
    )

    assert response.status_code == 400


def test_validate_generation_requires_new_entity_descriptions(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier_item = client.get("/frontier").json()[0]

    response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "A new clerk appears.",
            "scene_text": "A fussy little clerk pops out of the wall with a ledger.",
            "choices": [
                {
                    "choice_text": "Hear the clerk out",
                    "notes": "NEXT_NODE: listen to the new clerk. FURTHER_GOALS: open a fresh recurring bureaucratic character thread.",
                }
            ],
            "relation_updates": [
                {
                    "subject_type": "character",
                    "subject_name": "Clerk Sedge",
                    "relation_type": "works_at",
                    "object_type": "location",
                    "object_name": "Mushroom Field",
                }
            ],
        },
    )

    assert response.status_code == 200
    issues = response.json()["issues"]
    assert any("Clerk Sedge" in issue and "new_characters" in issue for issue in issues)


def test_apply_generation_creates_new_entity_with_description(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier_item = client.get("/frontier").json()[0]

    response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": frontier_item["from_node_id"],
            "choice_id": frontier_item["choice_id"],
            "candidate": {
                "branch_key": "default",
                "scene_title": "Clerk Arrival",
                "scene_summary": "A neat clerk appears near the mushroom path.",
                "scene_text": "A neat clerk steps out with a ledger and a worried little bow.",
                "choices": [
                    {
                        "choice_text": "Ask the clerk what he wants",
                        "notes": "NEXT_NODE: meet the new clerk. FURTHER_GOALS: open a fresh recurring character thread with a bureaucratic angle.",
                    }
                ],
                "new_characters": [
                    {
                        "name": "Clerk Sedge",
                        "description": "A tidy field clerk with a ledger, a careful bow, and an anxious respect for procedures.",
                        "canonical_summary": "A recurring mushroom-field clerk who treats strange incidents as paperwork problems.",
                    }
                ],
                "relation_updates": [
                    {
                        "subject_type": "character",
                        "subject_name": "Clerk Sedge",
                        "relation_type": "works_at",
                        "object_type": "location",
                        "object_name": "Mushroom Field",
                    }
                ],
            },
        },
    )

    assert response.status_code == 200

    with connect(db_path) as connection:
        canon = CanonResolver(connection)
        clerk = canon.find_character_by_name("Clerk Sedge")

    assert clerk is not None
    assert clerk["description"] == "A tidy field clerk with a ledger, a careful bow, and an anxious respect for procedures."


def test_apply_generation_auto_stages_new_named_speaking_character_for_inferred_art(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier_item = client.get("/frontier").json()[0]

    response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": frontier_item["from_node_id"],
            "choice_id": frontier_item["choice_id"],
            "candidate": {
                "branch_key": "default",
                "scene_title": "Clerk in the Mist",
                "scene_summary": "A named clerk emerges from the field and speaks before the gnome can retreat.",
                "scene_text": "A careful little clerk steps out of the mist with a waxed ledger tucked under one arm.",
                "dialogue_lines": [
                    {"speaker": "Clerk Sedge", "text": "You are standing exactly where the counting weather said you would."},
                ],
                "choices": [
                    {
                        "choice_text": "Ask Clerk Sedge how he knew that",
                        "notes": "NEXT_NODE: meet the new clerk directly. FURTHER_GOALS: prove newly introduced visible speakers get staged for portrait inference.",
                    }
                ],
                "new_characters": [
                    {
                        "name": "Clerk Sedge",
                        "description": "A neat field clerk with a waxed ledger, mist-beaded lashes, and a voice trained by paperwork.",
                        "canonical_summary": "A recurring clerk who treats strange sightings like administrative weather.",
                    }
                ],
            },
        },
    )

    assert response.status_code == 200
    node = response.json()["node"]

    with connect(db_path) as connection:
        canon = CanonResolver(connection)
        clerk = canon.find_character_by_name("Clerk Sedge")

    assert clerk is not None

    created_node = next(row for row in client.get("/story-nodes").json() if row["id"] == node["id"])
    assert any(
        entity["entity_type"] == "character" and int(entity["entity_id"]) == int(clerk["id"])
        for entity in created_node["present_entities"]
    )

    inferred = infer_missing_asset_requests(
        node=created_node,
        explicit_requests=[],
        project_root=Path(__file__).resolve().parents[1],
        client=client,
    )
    assert any(
        request["asset_kind"] == "portrait"
        and request["entity_type"] == "character"
        and int(request["entity_id"]) == int(clerk["id"])
        for request in inferred
    )


def test_refresh_protagonist_assets_creates_latest_cutout(tmp_path: Path, monkeypatch) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/reset-opening-canon")

    portrait_source = tmp_path / "main-character-no-cutout.png"
    Image.new("RGB", (64, 96), color=(180, 80, 70)).save(portrait_source)

    from app.services import assets as assets_module

    def fake_remove_background(
        self,
        *,
        source_image_path,
        output_name=None,
        model_repo="briaai/RMBG-2.0",
        device="auto",
        entity_type=None,
        asset_kind=None,
    ):
        cutout = tmp_path / (output_name or "cutout.png")
        Image.open(source_image_path).convert("RGBA").save(cutout)
        return {
            "output_path": str(cutout),
            "display_class": "character-fullbody",
            "normalization": {"method": "test"},
        }

    monkeypatch.setattr(assets_module.AssetService, "remove_background", fake_remove_background)

    refresh_response = client.post(
        "/story/refresh-protagonist-assets",
        params={"source_image_path": str(portrait_source)},
    )
    assert refresh_response.status_code == 200
    data = refresh_response.json()
    assert data["portrait_asset"]["asset_kind"] == "portrait"
    assert data["cutout_asset"]["asset_kind"] == "cutout"

    with connect(db_path) as connection:
        service = AssetService(connection, Path(__file__).resolve().parents[1])
        latest_cutout = service.get_latest_asset(entity_type="character", entity_id=1, asset_kind="cutout")

    assert latest_cutout is not None
    assert latest_cutout["id"] == data["cutout_asset"]["id"]


def test_play_resolves_asset_backed_scene_media(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    project_root = Path(__file__).resolve().parents[1]
    fixture_dir = project_root / "data" / "assets" / "test-fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    background_path = fixture_dir / f"{tmp_path.name}-bg.png"
    cutout_path = fixture_dir / f"{tmp_path.name}-cutout.png"
    try:
        Image.new("RGB", (32, 32), color=(90, 110, 160)).save(background_path)
        Image.new("RGBA", (32, 32), color=(200, 160, 90, 255)).save(cutout_path)

        with connect(db_path) as connection:
            service = AssetService(connection, project_root)
            service.add_asset(
                entity_type="location",
                entity_id=1,
                asset_kind="background",
                file_path=str(background_path),
            )
            service.add_asset(
                entity_type="character",
                entity_id=1,
                asset_kind="cutout",
                file_path=str(cutout_path),
            )

        response = client.get("/play")
        assert response.status_code == 200
        assert f"/media/test-fixtures/{background_path.name}" in response.text
        assert f"/media/test-fixtures/{cutout_path.name}" in response.text
        assert "Follow the grooves beneath the velvet-marked mushroom" in response.text
    finally:
        if background_path.exists():
            background_path.unlink()
        if cutout_path.exists():
            cutout_path.unlink()
        try:
            fixture_dir.rmdir()
        except OSError:
            pass


def test_play_scene_query_sets_start_scene_permalink(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    nodes = client.get("/story-nodes").json()
    target_scene_id = str(nodes[1]["id"])

    response = client.get(f"/play?branch_key=default&scene={target_scene_id}")
    assert response.status_code == 200

    match = re.search(
        r'<script id="player-story-data" type="application/json">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    assert match is not None
    story_data = json.loads(match.group(1))
    assert story_data["start_scene"] == target_scene_id


def test_play_story_payload_exposes_choice_planning(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier_item = client.get("/frontier").json()[0]

    apply_response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": frontier_item["from_node_id"],
            "choice_id": frontier_item["choice_id"],
            "candidate": {
                "branch_key": "default",
                "scene_title": "Planning Test Scene",
                "scene_summary": "A scene used to confirm planning text reaches the player payload.",
                "scene_text": "A clear little branch for testing.",
                "choices": [
                    {
                        "choice_text": "Take the careful route",
                        "notes": "NEXT_NODE: choose the safer branch. FURTHER_GOALS: keep the mushroom-field thread alive while opening a cautious follow-up path.",
                    }
                ],
            },
        },
    )
    assert apply_response.status_code == 200
    created_node_id = str(apply_response.json()["node"]["id"])

    response = client.get(f"/play?branch_key=default&scene={created_node_id}")
    assert response.status_code == 200
    match = re.search(
        r'<script id="player-story-data" type="application/json">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    assert match is not None
    story_data = json.loads(match.group(1))
    choice = story_data["scenes"][created_node_id]["choices"][0]
    assert choice["next_node"] == "choose the safer branch."
    assert choice["further_goals"] == "keep the mushroom-field thread alive while opening a cautious follow-up path."


def test_snapshot_db_tool_creates_manual_backup(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    assert client.get("/").status_code == 200

    snapshot_dir = tmp_path / "snapshots"
    command = [
        sys.executable,
        "-m",
        "app.tools.snapshot_db",
        "--name",
        "session-one",
        "--output-dir",
        str(snapshot_dir),
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CYOA_DB_PATH": str(db_path)},
    )

    assert result.returncode == 0
    snapshot_path = Path(result.stdout.strip())
    assert snapshot_path.exists()
    assert snapshot_path.parent == snapshot_dir
    assert snapshot_path.name.endswith("session-one.db")
    assert snapshot_path.read_bytes() == db_path.read_bytes()

    second_result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CYOA_DB_PATH": str(db_path)},
    )

    assert second_result.returncode == 0
    second_snapshot_path = Path(second_result.stdout.strip())
    assert second_snapshot_path.exists()
    assert second_snapshot_path != snapshot_path
    assert second_snapshot_path.name.startswith(f"{db_path.stem}-session-one-")
    assert second_snapshot_path.read_bytes() == db_path.read_bytes()


def test_prepare_story_run_tool_outputs_compact_packet(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    ideas_path = Path(__file__).resolve().parents[1] / "IDEAS.md"
    original_ideas = ideas_path.read_text(encoding="utf-8")
    client.post(
        "/story-notes",
        json={
            "note_type": "plotline",
            "title": "Transit Trouble Seed",
            "note_text": "A later tram route could erupt into a transit crisis.",
        },
    )
    try:
        ideas_path.write_text(
            original_ideas.rstrip()
            + "\n- [Event] Clockseed Stampede: A delayed route could spill sentient stamps across the platform and force a frantic sorting scene.\n",
            encoding="utf-8",
        )

        command = [
            sys.executable,
            "-m",
            "app.tools.prepare_story_run",
            "--play-base-url",
            "http://127.0.0.1:8001",
        ]
        result = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "CYOA_DB_PATH": str(db_path)},
        )

        assert result.returncode == 0
        packet = json.loads(result.stdout)
        assert packet["run_mode"] == "normal"
        assert "Everything is already wired through" in packet["message"]
        assert "continue the worker loop immediately" in packet["message"]
        assert packet["pre_change_url"].startswith("http://127.0.0.1:8001/play?branch_key=default&scene=")
        assert packet["selected_frontier_item"]["choice_id"] is not None
        assert "active_hooks" not in packet["selected_frontier_item"]
        assert "available_affordances" not in packet["selected_frontier_item"]
        assert packet["preview_payload"]["branch_key"] == "default"
        assert packet["context_summary"]["branch_key"] == "default"
        assert "focus_canon_slice" in packet
        assert "asset_availability" in packet
        assert any(item["entity_type"] == "location" for item in packet["asset_availability"])
        assert "ideas_file_summary" in packet
        assert packet["ideas_file_summary"]["path"].endswith("IDEAS.md")
        assert packet["ideas_file_summary"]["open_ideas"]
        assert "reveal_guardrails" in packet
        assert any("strange" in item.lower() or "anomaly" in item.lower() for item in packet["reveal_guardrails"]["allowed_now"])
        assert any("deferred rulers" in item.lower() or "full regime" in item.lower() or "masterminds" in item.lower() for item in packet["reveal_guardrails"]["avoid_for_now"])
        assert "validation_checklist" in packet
        assert any("Use NEXT_NODE as a base for your scene" in item for item in packet["validation_checklist"])
        assert "candidate_template" not in packet
        assert "endpoint_contract" not in packet
        assert "full_context" not in packet
        assert "eligible_major_hooks" in packet["context_summary"]
        assert "blocked_major_hooks" in packet["context_summary"]
        assert packet["context_summary"]["global_direction_notes"][0]["title"] == "Transit Trouble Seed"
        assert "branch_shape" in packet["context_summary"]
        assert "choice_handoff" in packet
        assert "author_warnings" in packet
        assert "author_warning_banner" in packet
        assert "final_warning" in packet
        assert "isolation_pressure" in packet
        assert "new_character_pressure" in packet
        assert "location_stall_pressure" in packet
        assert "path_location_continuity" in packet
        assert "parent_current_location" in packet
        assert "location_transition_obligation" in packet
        assert packet["author_warnings"]
        if packet["isolation_pressure"]["active"]:
            assert any("Isolation pressure is active" in warning for warning in packet["author_warnings"])
        if packet["new_character_pressure"]["active"]:
            assert any("New-character pressure is active" in warning for warning in packet["author_warnings"])
        if packet["location_stall_pressure"]["active"]:
            assert any("Location-stall pressure is active" in warning for warning in packet["author_warnings"])
            assert any("location_transition" in warning for warning in packet["author_warnings"])
        if packet["frontier_budget_state"]["pressure_level"] in {"soft", "hard"}:
            assert any("Frontier pressure is" in warning for warning in packet["author_warnings"])
            assert any("This run will ONLY validate if at least one choice uses TARGET_EXISTING_NODE" in warning for warning in packet["author_warnings"])
            assert packet["message"].startswith("WARNING:")
            assert "YOUR RUN WILL FAIL" in packet["message"]
            assert "TARGET_EXISTING_NODE to merge into an existing node" in packet["message"]
            assert packet["author_warning_banner"].startswith("WARNING: YOUR RUN WILL FAIL")
            assert packet["final_warning"].startswith("WARNING: YOUR RUN WILL FAIL")
        assert "consequential_choice_requirement" in packet
        assert packet["next_action"].startswith("Run now. Do not ask the human for permission.")
        assert "choice id" in packet["next_action"].lower()
        assert "Use NEXT_NODE as a base for your scene" in packet["next_action"]
        assert "Do not emit JSON in normal mode" in packet["next_action"]
        assert "ideas.md" in packet["next_action"].lower()
        assert "main source of fresh people, places, and whimsical turns" in packet["next_action"]
        assert "asset_availability" in packet["next_action"]
        assert "reveal_guardrails" in packet["next_action"]
        assert "frontier_choice_constraints as hard validation rules" in packet["next_action"]
        assert "isolation_pressure" in packet["next_action"]
        assert "new_character_pressure" in packet["next_action"]
        assert "location_stall_pressure" in packet["next_action"]
        assert "path_location_continuity" in packet["next_action"]
        assert "location_transition option" in packet["next_action"]
        assert "this run will only validate if at least one choice uses TARGET_EXISTING_NODE" in packet["next_action"]
        assert "You will be able to satisfy that requirement during the choice creation phase." in packet["next_action"]
        assert isinstance(packet["planning_policy"]["chance"], float)
        assert 0 < packet["planning_policy"]["chance"] <= 1
        assert packet["runtime_state_after"]["normal_runs_since_plan"] == 1
    finally:
        ideas_path.write_text(original_ideas, encoding="utf-8")


def test_prepare_story_run_exposes_choice_handoff_from_next_node_notes(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier_choice_id = client.get("/frontier").json()[0]["choice_id"]

    update_response = client.post(
        f"/choices/{frontier_choice_id}",
        json={
            "notes": (
                "NEXT_NODE: The seam opens and reveals a lift below the roots. "
                "FURTHER_GOALS: Bring in survey pressure and move the branch underground soon."
            )
        },
    )
    assert update_response.status_code == 200

    command = [
        sys.executable,
        "-m",
        "app.tools.prepare_story_run",
        "--choice-id",
        str(frontier_choice_id),
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "CYOA_DB_PATH": str(db_path)},
    )
    packet = json.loads(result.stdout)
    assert packet["choice_handoff"]["next_node"] == "The seam opens and reveals a lift below the roots."
    assert packet["choice_handoff"]["further_goals"] == "Bring in survey pressure and move the branch underground soon."


def test_prepare_story_run_exposes_location_transition_obligation_for_selected_choice(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier_choice_id = client.get("/frontier").json()[0]["choice_id"]

    with connect(db_path) as connection:
        connection.execute(
            "UPDATE choices SET notes = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "notes": "NEXT_NODE: You follow the ladder shaft toward the old orchard route. FURTHER_GOALS: Return to an earlier location through a new scene.",
                        "choice_class": "location_transition",
                    }
                ),
                frontier_choice_id,
            ),
        )
        connection.commit()

    command = [
        sys.executable,
        "-m",
        "app.tools.prepare_story_run",
        "--play-base-url",
        "http://127.0.0.1:8001",
        "--choice-id",
        str(frontier_choice_id),
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CYOA_DB_PATH": str(db_path)},
    )

    assert result.returncode == 0
    packet = json.loads(result.stdout)
    assert packet["selected_frontier_item"]["choice_class"] == "location_transition"
    assert packet["location_transition_obligation"]["active"] is True
    assert "changes current_scene now" in packet["location_transition_obligation"]["rule"]
    assert packet["parent_current_location"] is not None
    assert packet["path_location_continuity"]["encountered_locations"]


def test_prepare_story_run_tool_can_include_full_context_on_request(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    command = [
        sys.executable,
        "-m",
        "app.tools.prepare_story_run",
        "--full-context",
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CYOA_DB_PATH": str(db_path)},
    )

    assert result.returncode == 0
    packet = json.loads(result.stdout)
    assert "full_context" in packet


def test_prepare_story_run_tool_forced_plan_outputs_planning_packet(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    command = [
        sys.executable,
        "-m",
        "app.tools.prepare_story_run",
        "--plan",
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CYOA_DB_PATH": str(db_path)},
    )

    assert result.returncode == 0
    packet = json.loads(result.stdout)
    assert packet["run_mode"] == "planning"
    assert "Do not generate or apply a new story scene" in packet["message"]
    assert packet["planning_reason"] == "forced by --plan"
    assert len(packet["planning_targets"]) >= 3
    assert len(packet["planning_targets"]) <= 4
    assert packet["ideas_file"]["path"].endswith("IDEAS.md")
    assert "Ideas Scratchpad" in packet["ideas_file"]["current_content"]
    assert packet["next_action"].startswith("Run now. Do not ask the human for permission. This is planning mode.")
    assert "update_choice_notes" in packet["endpoint_contract"]
    assert packet["runtime_state_after"]["last_run_mode"] == "planning"
    assert packet["runtime_state_after"]["normal_runs_since_plan"] == 0


def test_prepare_story_run_random_plan_respects_cooldown_and_chance(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    command = [sys.executable, "-m", "app.tools.prepare_story_run"]

    first = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CYOA_DB_PATH": str(db_path), "CYOA_PLANNING_ROLL": "0.0"},
    )
    second = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CYOA_DB_PATH": str(db_path), "CYOA_PLANNING_ROLL": "0.0"},
    )
    third = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CYOA_DB_PATH": str(db_path), "CYOA_PLANNING_ROLL": "0.0"},
    )

    assert first.returncode == 0
    assert second.returncode == 0
    assert third.returncode == 0
    assert json.loads(first.stdout)["run_mode"] == "normal"
    assert json.loads(second.stdout)["run_mode"] == "normal"
    assert json.loads(third.stdout)["run_mode"] == "planning"


def test_prepare_story_run_outputs_revival_packet_when_frontier_empty(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    with connect(db_path) as connection:
        opening_choice = connection.execute(
            "SELECT id, from_node_id FROM choices WHERE to_node_id IS NULL AND status = 'open' ORDER BY id LIMIT 1"
        ).fetchone()
        assert opening_choice is not None
        opening_choice_id = int(opening_choice["id"])
        parent_node_id = int(opening_choice["from_node_id"])

    apply_response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": parent_node_id,
            "choice_id": opening_choice_id,
            "candidate": {
                "branch_key": "default",
                "scene_title": "Short Dead End",
                "scene_summary": "A tiny branch closes immediately.",
                "scene_text": "The path narrows into a polite dead end with nowhere else to go.",
                "entity_references": [
                    {"entity_type": "location", "entity_id": 1, "role": "current_scene"},
                ],
                "choices": [
                    {
                        "choice_text": "Accept the dead end",
                        "notes": "NEXT_NODE: finish this doomed side path cleanly. FURTHER_GOALS: close the branch so revival logic has something to reopen later.",
                        "choice_class": "ending",
                        "ending_category": "dead_end",
                    }
                ],
            },
        },
    )
    assert apply_response.status_code == 200

    with connect(db_path) as connection:
        open_rows = connection.execute(
            "SELECT id FROM choices WHERE status = 'open'"
        ).fetchall()
        for row in open_rows:
            StoryGraphService(connection).set_choice_status(int(row["id"]), "closed")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.tools.prepare_story_run",
            "--play-base-url",
            "http://127.0.0.1:8001",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "CYOA_DB_PATH": str(db_path),
            "CYOA_PLANNING_ROLL": "1.0",
        },
    )

    assert result.returncode == 0
    packet = json.loads(result.stdout)
    assert packet["run_mode"] == "revival"
    assert packet["selection_reason"].startswith("frontier empty; reopening continuity")
    assert packet["revival_context"]["max_choices_per_node"] == 5
    with connect(db_path) as connection:
        candidate_pairs = {
            (int(row["parent_node_id"]), int(row["traversed_choice_id"]))
            for row in StoryGraphService(connection).list_closed_leaf_candidates(branch_key="default", limit=50)
        }
    assert (
        packet["revival_context"]["parent_node_id"],
        packet["revival_context"]["traversed_choice_id"],
    ) in candidate_pairs


def test_prepare_story_run_surfaces_worldbuilding_notes_in_normal_packet(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    create_response = client.post(
        "/worldbuilding",
        json={
            "note_type": "patrol_pressure",
            "title": "Clockwork Patrols",
            "note_text": "Clockwork patrols have begun pacing the outer tram loops at dusk.",
            "priority": 4,
            "pressure": 5,
        },
    )
    assert create_response.status_code == 200
    note = create_response.json()

    update_response = client.post(
        f"/worldbuilding/{note['id']}",
        json={"status": "active", "pressure": 6},
    )
    assert update_response.status_code == 200
    assert update_response.json()["pressure"] == 6

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.tools.prepare_story_run",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "CYOA_DB_PATH": str(db_path),
            "CYOA_PLANNING_ROLL": "1.0",
        },
    )

    assert result.returncode == 0
    packet = json.loads(result.stdout)
    worldbuilding_notes = packet["context_summary"]["worldbuilding_notes"]
    assert any(note["title"] == "Clockwork Patrols" for note in worldbuilding_notes)


def test_generation_prompt_includes_bold_character_and_location_guidance(tmp_path: Path) -> None:
    _, _ = build_client(tmp_path)
    service = LLMGenerationService(Path(__file__).resolve().parents[1])
    prompt = service.build_prompt({"branch_key": "default"})

    assert "Feel free to act creatively. Make bold choices as long as they fit in the story." in prompt
    assert "Introduce or reintroduce characters frequently. Characters make a story." in prompt
    assert "Introduce new locations frequently when appropriate" in prompt
    assert "Always evaluate whether the player is actually familiar" in prompt
    assert "Frequently use ideas from IDEAS.md" in prompt


def test_validation_checklist_includes_boldness_and_dynamic_pressure() -> None:
    checklist = build_validation_checklist(
        branch_shape={
            "should_prefer_divergence": True,
            "single_actor_scene_streak": 6,
            "new_character_gap_streak": 6,
            "same_location_streak": 4,
            "repeated_action_family": "inspect",
        }
    )

    assert any("Feel free to act creatively" in item for item in checklist)
    assert any("Characters make a story" in item for item in checklist)
    assert any("player is actually familiar" in item for item in checklist)
    assert any("Frequently use ideas from IDEAS.md" in item for item in checklist)
    assert any("Introduce new locations frequently" in item for item in checklist)
    assert any("stayed protagonist-only too long" in item for item in checklist)
    assert any("gone too long without a brand-new character" in item for item in checklist)
    assert any("lingered in one place too long" in item for item in checklist)
    assert any("repeating the 'inspect' action family" in item for item in checklist)


def test_branch_shape_tracks_location_actor_and_action_pressure(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/reset-opening-canon")

    with connect(db_path) as connection:
        story = StoryGraphService(connection)
        previous_id = None
        for index in range(4):
            node = story.create_story_node(
                branch_key="default",
                title=f"Static Node {index}",
                scene_text="The same field keeps humming while the gnome remains alone.",
                summary="The branch lingers in the same field with the same lone protagonist.",
                parent_node_id=previous_id,
                referenced_entities=[{"entity_type": "location", "entity_id": 1, "role": "current_scene"}],
                present_entities=[{"entity_type": "character", "entity_id": 1, "slot": "hero-center", "focus": True}],
            )
            story.create_choice(
                from_node_id=int(node["id"]),
                choice_text="Follow the humming seam deeper",
                notes='{"notes":"NEXT_NODE: keep following the seam. FURTHER_GOALS: continue the same investigation.","choice_class":"progress"}',
            )
            previous_id = int(node["id"])

        branch_shape = story.describe_branch_shape("default")

    assert branch_shape["same_location_streak"] >= 3
    assert branch_shape["single_actor_scene_streak"] >= 3
    assert branch_shape["repeated_action_family"] == "follow"


def test_collect_branch_pressure_issues_rejects_static_same_location_all_inspection_scene() -> None:
    candidate = parse_llm_result(
        {"run_mode": "normal"},
        json.dumps(
            {
                "branch_key": "default",
                "scene_summary": "The gnome remains alone in the same field, still inspecting the seam.",
                "scene_text": "Nothing materially changes. The same seam glows and the same gnome watches it.",
                "choices": [
                    {
                        "choice_text": "Inspect the seam more closely",
                        "notes": "NEXT_NODE: inspect the seam more closely. FURTHER_GOALS: continue the same inspection loop.",
                        "choice_class": "inspection",
                    },
                    {
                        "choice_text": "Listen to the seam carefully",
                        "notes": "NEXT_NODE: listen to the seam carefully. FURTHER_GOALS: continue the same inspection loop.",
                        "choice_class": "inspection",
                    },
                ],
                "entity_references": [
                    {"entity_type": "location", "entity_id": 1, "role": "current_scene"},
                ],
            }
        ),
    )

    issues = collect_branch_pressure_issues(
        packet={
            "isolation_pressure": {"active": True},
            "location_stall_pressure": {"active": True},
            "recent_action_family_summary": {
                "repeated_action_family": "inspect",
                "recent_action_family_counts": {"inspect": 4},
            },
            "frontier_budget_state": {"pressure_level": "normal"},
        },
        candidate=candidate,
    )

    assert any("consequential option" in issue for issue in issues)
    assert any("still too solitary" in issue for issue in issues)
    assert any("location_transition option" in issue for issue in issues)


def test_rebalance_frontier_parks_excess_choices_and_can_unpark(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    for index in range(3):
        create_response = client.post(
            "/choices",
            json={
                "from_node_id": 1,
                "choice_text": f"Extra backlog choice {index + 1}",
                "status": "open",
                "notes": json.dumps({"notes": "NEXT_NODE: create backlog. FURTHER_GOALS: stress the frontier rebalance tool."}),
            },
        )
        assert create_response.status_code == 200

    apply_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.tools.rebalance_frontier",
            "--soft-limit",
            "1",
            "--keep-recent-parents",
            "1",
            "--apply",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CYOA_DB_PATH": str(db_path)},
    )

    assert apply_result.returncode == 0
    payload = json.loads(apply_result.stdout)
    assert payload["dry_run"] is False
    assert payload["parked_count"] >= 1
    assert payload["park_choice_ids"]

    with connect(db_path) as connection:
        parked_count = connection.execute(
            "SELECT COUNT(*) AS count FROM choices WHERE status = 'parked'"
        ).fetchone()["count"]
        open_count = connection.execute(
            "SELECT COUNT(*) AS count FROM choices WHERE status = 'open'"
        ).fetchone()["count"]

    assert parked_count >= 1
    assert open_count >= 1

    unpark_choice_id = int(payload["park_choice_ids"][0])
    unpark_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.tools.rebalance_frontier",
            "--unpark-choice-id",
            str(unpark_choice_id),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CYOA_DB_PATH": str(db_path)},
    )
    assert unpark_result.returncode == 0
    unpark_payload = json.loads(unpark_result.stdout)
    assert unpark_payload["choice"]["status"] == "open"


def test_choice_failure_tracking_only_parks_at_five_and_resets_when_reopened(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    choice_id = int(client.get("/frontier").json()[0]["choice_id"])

    with connect(db_path) as connection:
        story = StoryGraphService(connection)
        for index in range(4):
            record = story.record_choice_worker_failure(choice_id=choice_id, error=f"failure {index + 1}")
            assert int(record["failed_run_count"]) == index + 1
            choice = story.get_choice(choice_id)
            assert choice is not None
            assert choice["status"] == "open"

        record = story.record_choice_worker_failure(choice_id=choice_id, error="failure 5")
        assert int(record["failed_run_count"]) == 5
        choice = story.get_choice(choice_id)
        assert choice is not None
        assert choice["status"] == "parked"

        reopened = story.set_choice_status(choice_id, "open")
        assert reopened["status"] == "open"
        assert story.get_choice_failure(choice_id) is None


def test_choices_with_four_failures_remain_in_frontier_until_threshold(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    choice_id = int(client.get("/frontier").json()[0]["choice_id"])

    with connect(db_path) as connection:
        story = StoryGraphService(connection)
        for index in range(4):
            story.record_choice_worker_failure(choice_id=choice_id, error=f"failure {index + 1}")

    frontier = client.get("/frontier").json()
    assert any(int(item["choice_id"]) == choice_id for item in frontier)

    with connect(db_path) as connection:
        story = StoryGraphService(connection)
        story.record_choice_worker_failure(choice_id=choice_id, error="failure 5")

    frontier_after = client.get("/frontier").json()
    assert all(int(item["choice_id"]) != choice_id for item in frontier_after)


def test_run_story_worker_failure_counts_once_per_failed_run_not_retry_attempt(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    choice_id = int(client.get("/frontier").json()[0]["choice_id"])
    log_file = tmp_path / "worker_log.ndjson"

    response_file = tmp_path / "invalid_responses.json"
    response_file.write_text(
        json.dumps(
            [
                {
                    "branch_key": "default",
                    "scene_summary": "A repetitive invalid scene.",
                    "scene_text": "This scene intentionally repeats the same move without advancing anything.",
                    "choices": [
                        {
                            "choice_text": "Follow the grooves beneath the velvet-marked mushroom",
                            "notes": "NEXT_NODE: follow the grooves beneath the velvet-marked mushroom. FURTHER_GOALS: follow the grooves beneath the velvet-marked mushroom.",
                        }
                    ],
                },
                {
                    "branch_key": "default",
                    "scene_summary": "A repetitive invalid scene.",
                    "scene_text": "This scene intentionally repeats the same move without advancing anything.",
                    "choices": [
                        {
                            "choice_text": "Follow the grooves beneath the velvet-marked mushroom",
                            "notes": "NEXT_NODE: follow the grooves beneath the velvet-marked mushroom. FURTHER_GOALS: follow the grooves beneath the velvet-marked mushroom.",
                        }
                    ],
                },
            ]
        ),
        encoding="utf-8",
    )

    command = [
        sys.executable,
        "-m",
        "app.tools.run_story_worker_local",
        "--model",
        "mock-model",
        "--max-retries",
        "2",
        "--log-file",
        str(log_file),
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "CYOA_DB_PATH": str(db_path),
            "CYOA_LOCAL_WORKER_RESPONSE_FILE": str(response_file),
            "CYOA_PLANNING_ROLL": "1.0",
        },
    )

    assert result.returncode != 0

    with connect(db_path) as connection:
        story = StoryGraphService(connection)
        failure = story.get_choice_failure(choice_id)

    assert failure is not None
    assert int(failure["failed_run_count"]) == 1


def test_generation_validation_rejects_inspection_fresh_branching_under_frontier_pressure(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    with connect(db_path) as connection:
        story = StoryGraphService(connection)
        for index in range(50):
            story.create_choice(
                from_node_id=1,
                choice_text=f"Pressure seed {index + 1}",
                status="open",
                notes=json.dumps({"notes": "NEXT_NODE: widen the frontier. FURTHER_GOALS: trigger soft frontier pressure."}),
            )

    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "Two inspection options try to widen the frontier again.",
            "scene_text": "The platform offers small sensory checks instead of a meaningful commitment.",
            "entity_references": [
                {"entity_type": "location", "entity_id": 1, "role": "current_scene"},
            ],
            "choices": [
                {
                    "choice_text": "Inspect the placard lettering",
                    "notes": "NEXT_NODE: inspect a tiny local detail for flavor. FURTHER_GOALS: open a minor side look that should not become a durable branch under pressure.",
                    "choice_class": "inspection",
                },
                {
                    "choice_text": "Listen at the bell housing",
                    "notes": "NEXT_NODE: inspect another tiny local detail for flavor. FURTHER_GOALS: open a second minor side look that should reconverge quickly instead of widening the frontier.",
                    "choice_class": "inspection",
                },
            ],
        },
    )

    assert validation_response.status_code == 200
    payload = validation_response.json()
    assert payload["valid"] is False
    assert any("Frontier pressure is high" in issue for issue in payload["issues"])
    assert any("Inspection choices should reconverge quickly" in issue for issue in payload["issues"])


def test_floating_character_introduction_allows_recurring_character_and_marks_path(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    with connect(db_path) as connection:
        opening_choice = connection.execute(
            "SELECT id, from_node_id FROM choices WHERE to_node_id IS NULL AND status = 'open' ORDER BY id LIMIT 1"
        ).fetchone()
        assert opening_choice is not None
        opening_choice_id = int(opening_choice["id"])
        parent_node_id = int(opening_choice["from_node_id"])

    seed_response = client.post(
        "/seed-world",
        json={
            "characters": [
                {
                    "name": "Madam Bei",
                    "description": "A poised frog conductor with a patient stare.",
                }
            ]
        },
    )
    assert seed_response.status_code == 200
    madam_bei = next(
        character for character in client.get("/characters").json()
        if character["name"] == "Madam Bei"
    )

    candidate = parse_llm_result(
        {"run_mode": "normal"},
        json.dumps(
            {
                "branch_key": "default",
                "scene_summary": "A recurring character appears through a floating first meeting.",
                "scene_text": "Madam Bei lifts one hand toward the witness bell and waits for your answer.",
                "choices": [
                    {
                        "choice_text": "Ask Madam Bei what she heard",
                        "notes": "NEXT_NODE: respond to the new arrival directly. FURTHER_GOALS: unlock a reusable recurring character on this branch without pretending an earlier meeting happened.",
                    }
                ],
                "floating_character_introductions": [
                    {
                        "character_id": madam_bei["id"],
                        "intro_text": "A frog conductor steps from the side window, straightens her vest, and introduces herself as Madam Bei.",
                    }
                ],
                "entity_references": [
                    {"entity_type": "location", "entity_id": 1, "role": "current_scene"},
                    {"entity_type": "character", "entity_id": madam_bei["id"], "role": "introduced"},
                ],
                "scene_present_entities": [
                    {"entity_type": "character", "entity_id": 1, "slot": "hero-center", "focus": True},
                    {"entity_type": "character", "entity_id": madam_bei["id"], "slot": "left-support"},
                ],
            }
        ),
    )

    continuity_issues = collect_character_continuity_issues(
        packet={"selected_frontier_item": {"from_node_id": parent_node_id}},
        candidate=candidate,
    )
    assert continuity_issues == []

    apply_response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": parent_node_id,
            "choice_id": opening_choice_id,
            "candidate": candidate.model_dump(mode="json"),
        },
    )
    assert apply_response.status_code == 200
    applied = apply_response.json()

    with connect(db_path) as connection:
        story = StoryGraphService(connection)
        lineage_ids = story.list_lineage_entity_ids(int(applied["node"]["id"]), "character")
        node = story.get_story_node(int(applied["node"]["id"]))

    assert madam_bei["id"] in lineage_ids
    assert node is not None
    assert node["scene_text"].startswith(
        "A frog conductor steps from the side window, straightens her vest, and introduces herself as Madam Bei."
    )


def test_run_story_worker_local_normal_dry_run_with_mock_response(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    with connect(db_path) as connection:
        frontier_choice = connection.execute(
            "SELECT id FROM choices WHERE status = 'open' AND to_node_id IS NULL ORDER BY from_node_id ASC, id ASC LIMIT 1"
        ).fetchone()
    assert frontier_choice is not None
    frontier_choice_id = int(frontier_choice["id"])
    log_file = tmp_path / "worker_log.ndjson"
    session_file = tmp_path / "normal_session.json"

    response_file = tmp_path / "normal_response.json"
    response_file.write_text(
        json.dumps(
            [
                "\n".join(
                        [
                            "SCENE_TITLE: Mock Loop Scene",
                            "SCENE_SUMMARY: A valid mocked scene candidate with immediate patrol pressure as the lantern gate route suddenly matters.",
                            "MATERIAL_CHANGE: The gnome is no longer just inspecting the seam; the patrol arrives close enough to force a decision about whether to stay or head for the lantern gate.",
                        "OPENING_BEAT: pressure_escalation",
                        "LOCATION_STATUS: same_location",
                        "SCENE_CAST: Brass Patrol Member",
                        "NEW_CHARACTERS: Brass Patrol Member",
                        "NEW_LOCATION: NONE",
                        "NEW_CHARACTER_INTRO: He is no longer just a rumor in the mist; he steps into full view with brass gear rattling at his belt.",
                        "NEW_LOCATION_INTRO: NONE",
                    ]
                ),
                "\n".join(
                        [
                            "SCENE_SETTINGS:",
                            "visible_when_speaking: true",
                            "start_show_all_from_last_node: true",
                            "SCENE_BODY:",
                            "0: The mocked local worker produces a fuller dry-run scene in the Mushroom Field. Dew slides from the mushroom caps as the seam hums underfoot, but the important change is no longer the seam itself.",
                            "@show 1n",
                            "1n: A brass patrol member steps through the mist and raises a dented lantern toward the gnome, forcing the moment out of quiet inspection and into open pressure while the lantern gate at the field's edge stops feeling hypothetical and starts feeling like the next real route out.",
                            "Hold where you are and show me your hands.",
                    ]
                ),
                "\n".join(
                    [
                        "CHOICE_TEXT: Answer the patrol member and keep the scene in the open",
                        "CHOICE_CLASS: commitment",
                        "NEXT_NODE: The patrol member demands an explanation and watches for any sign that the hat or hand means trouble.",
                        "FURTHER_GOALS: Prove the conversational worker can produce a consequential social move instead of another inspection loop.",
                        "ENDING_CATEGORY: NONE",
                        "TARGET_EXISTING_NODE: NONE",
                    ]
                ),
                "\n".join(
                        [
                            "CHOICE_TEXT: Break for the lantern gate before the patrol seals the route",
                            "CHOICE_CLASS: progress",
                            "NEXT_NODE: The gnome reaches the lantern gate path and forces the patrol member to choose between pursuit and holding the field.",
                            "FURTHER_GOALS: Prove the runner accepts a distinct pressure-response choice with explicit location motion instead of another local inspection beat.",
                        "ENDING_CATEGORY: NONE",
                        "TARGET_EXISTING_NODE: NONE",
                    ]
                ),
                "END",
                "END",
                "\n".join(
                    [
                        "CHARACTER_DETAILS: Brass Patrol Member | A brass-armored survey runner with a dented lantern and a voice trained for public orders.",
                        "CHARACTER_ART_HINTS: Brass Patrol Member | Narrow brass-armored patrol runner with a dented lantern, soot-dark gloves, a long field coat, and a watchful, suspicious expression.",
                    ]
                ),
            ]
        ),
        encoding="utf-8",
    )

    command = [
        sys.executable,
        "-m",
        "app.tools.run_story_worker_local",
        "--model",
        "mock-model",
        "--choice-id",
        str(frontier_choice_id),
        "--dry-run",
        "--log-file",
        str(log_file),
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "CYOA_DB_PATH": str(db_path),
            "CYOA_LOCAL_WORKER_RESPONSE_FILE": str(response_file),
            "CYOA_LOCAL_WORKER_SESSION_FILE": str(session_file),
            "CYOA_PLANNING_ROLL": "1.0",
        },
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["run_mode"] == "normal"
    assert payload["dry_run"] is True
    assert payload["expanded_choice_id"] if "expanded_choice_id" in payload else payload["choice_id"]
    assert payload["validation"]["valid"] is True
    session_payload = json.loads(session_file.read_text(encoding="utf-8"))
    assert session_payload["run_count"] == 1
    assert len(session_payload["messages"]) >= 6


def test_conversational_scene_builder_form_parsers() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Lantern Stop",
                "SCENE_SUMMARY: A patrol finally steps into view and forces the issue.",
                "MATERIAL_CHANGE: The branch shifts from private inspection to open social pressure in the field.",
                "OPENING_BEAT: pressure_escalation",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: Brass Patrol Member",
                "NEW_CHARACTERS: Brass Patrol Member",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: He emerges from the mist with a rattling lantern.",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    assert isinstance(scene_plan, ScenePlanDraft)
    assert scene_plan.scene_cast_mode == "explicit"
    assert scene_plan.scene_cast_entries == ["Brass Patrol Member"]
    assert scene_plan.new_character_names == ["Brass Patrol Member"]
    assert scene_plan.new_location_name is None
    assert resolve_scene_cast_names(
        draft=scene_plan,
        resolution={
            "protagonist_name": "The Tall Gnome",
            "character_name_map": {"brass patrol member": {"name": "Brass Patrol Member"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}, 2: {"id": 2, "name": "Brass Patrol Member"}},
            "current_visible_cast_names": [],
        },
    ) == ["The Tall Gnome", "Brass Patrol Member"]

    scene_plan_with_ids = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Mirror Junction",
                "SCENE_SUMMARY: The protagonist spots two familiar figures reflected in the hall glass.",
                "MATERIAL_CHANGE: The scene turns from solitary exploration into a cast-aware encounter setup.",
                "OPENING_BEAT: discovery",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    assert scene_plan_with_ids.scene_cast_mode == "mc_only"
    assert scene_plan_with_ids.scene_cast_entries == []
    assert scene_plan_with_ids.new_character_names == []

    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS:",
                "visible_when_speaking: true",
                "start_show_all_from_last_node: false",
                "mc_always_visible: true",
                "SCENE_BODY:",
                "The field stops feeling private the moment the lantern breaks through the mist.",
                "@show 1",
                "1: Hold where you are.",
                "@show 1",
                "@show 2",
                "0: The lantern glare hardens.",
            ]
        )
    )
    assert isinstance(scene_body, SceneBodyDraft)
    assert scene_body.settings.visible_when_speaking is True
    assert scene_body.settings.mc_always_visible is True
    assert scene_body.textboxes[0].speaker_ref == "0"
    assert scene_body.textboxes[1].pending_commands[0].action == "show"
    assert scene_body.textboxes[2].pending_commands[1].targets == ["2"]

    equals_style_scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: visible_when_speaking=true, start_show_all_from_last_node=false, mc_always_visible=true",
                "SCENE_BODY:",
                "0: The mirrored hall shivers awake.",
                "2: Keep moving.",
            ]
        )
    )
    assert equals_style_scene_body.settings.visible_when_speaking is True
    assert equals_style_scene_body.settings.start_show_all_from_last_node is False


def test_validate_scene_plan_draft_rejects_unknown_scene_cast_numeric_id_early() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Bad Cast Id",
                "SCENE_SUMMARY: A bad cast id should fail immediately.",
                "MATERIAL_CHANGE: The scene plan should reject invalid cast ids during setup.",
                "OPENING_BEAT: discovery",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: 99",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )

    issues = validate_scene_plan_draft(
        packet={},
        draft=scene_plan,
        resolution={
            "protagonist_name": "The Tall Gnome",
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "encountered_names": {"the tall gnome"},
        },
    )

    assert any("SCENE_CAST uses numeric id '99'" in issue for issue in issues)


def test_validate_scene_plan_draft_rejects_unknown_scene_cast_name_unless_declared_new() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Bad Cast Name",
                "SCENE_SUMMARY: An undeclared unknown cast name should fail immediately.",
                "MATERIAL_CHANGE: The scene plan should reject stray cast names during setup.",
                "OPENING_BEAT: discovery",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: New Witness",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )

    issues = validate_scene_plan_draft(
        packet={},
        draft=scene_plan,
        resolution={
            "protagonist_name": "The Tall Gnome",
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "encountered_names": {"the tall gnome"},
        },
    )

    assert any("SCENE_CAST includes 'New Witness'" in issue for issue in issues)


def test_validate_scene_plan_draft_allows_larger_cast_when_not_all_visible_at_once() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Crowded Circuit",
                "SCENE_SUMMARY: A larger cast is declared, but the body can stage them selectively.",
                "MATERIAL_CHANGE: The scene may rotate who is present without showing everyone at once.",
                "OPENING_BEAT: transition",
                "LOCATION_STATUS: new_location",
                "SCENE_CAST: 1, 2",
                "NEW_CHARACTERS: Gearwick, Clerk Sedge",
                "NEW_LOCATION: The Junction Mechanism Chamber",
                "NEW_CHARACTER_INTRO: New arrivals spill in from different conduits as the chamber wakes.",
                "NEW_LOCATION_INTRO: The passage opens into a louder chamber full of moving brass.",
            ]
        )
    )

    issues = validate_scene_plan_draft(
        packet={},
        draft=scene_plan,
        resolution={
            "protagonist_name": "The Tall Gnome",
            "character_name_map": {
                "the tall gnome": {"id": 1, "name": "The Tall Gnome"},
                "brass patrol member": {"id": 2, "name": "Brass Patrol Member"},
            },
            "character_id_map": {
                1: {"id": 1, "name": "The Tall Gnome"},
                2: {"id": 2, "name": "Brass Patrol Member"},
            },
            "current_visible_cast_names": [],
            "encountered_names": {"the tall gnome", "brass patrol member"},
        },
    )

    assert not any("side slots" in issue for issue in issues)
    assert not any("at most three characters total" in issue for issue in issues)


def test_validate_scene_plan_draft_rejects_solitary_setup_under_isolation_pressure() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Alone Again",
                "SCENE_SUMMARY: You study the vault seam in private again.",
                "MATERIAL_CHANGE: The same solitary investigation continues in place.",
                "OPENING_BEAT: discovery",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )

    issues = validate_scene_plan_draft(
        packet={"isolation_pressure": {"active": True}},
        draft=scene_plan,
        resolution={
            "protagonist_name": "The Tall Gnome",
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "encountered_names": {"the tall gnome"},
        },
    )

    assert any("Isolation pressure is active" in issue for issue in issues)


def test_validate_scene_plan_draft_rejects_missing_new_character_under_new_character_pressure() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Familiar Faces",
                "SCENE_SUMMARY: You rely on familiar company instead of anyone new.",
                "MATERIAL_CHANGE: Existing cast pressure increases, but no brand-new person appears.",
                "OPENING_BEAT: confrontation",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: 1, 2",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )

    issues = validate_scene_plan_draft(
        packet={"new_character_pressure": {"active": True}},
        draft=scene_plan,
        resolution={
            "protagonist_name": "The Tall Gnome",
            "character_name_map": {
                "the tall gnome": {"id": 1, "name": "The Tall Gnome"},
                "brass patrol member": {"id": 2, "name": "Brass Patrol Member"},
            },
            "character_id_map": {
                1: {"id": 1, "name": "The Tall Gnome"},
                2: {"id": 2, "name": "Brass Patrol Member"},
            },
            "current_visible_cast_names": [],
            "encountered_names": {"the tall gnome", "brass patrol member"},
        },
    )

    assert any("New-character pressure is active" in issue for issue in issues)


def test_validate_scene_plan_draft_allows_same_location_setup_under_location_stall_pressure_until_choice_phase() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Same Hallway",
                "SCENE_SUMMARY: You remain in the same hall and inspect the same trouble again.",
                "MATERIAL_CHANGE: The branch stays in place without changing location framing.",
                "OPENING_BEAT: discovery",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )

    issues = validate_scene_plan_draft(
        packet={"location_stall_pressure": {"active": True}},
        draft=scene_plan,
        resolution={
            "protagonist_name": "The Tall Gnome",
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "encountered_names": {"the tall gnome"},
        },
    )

    assert not any("Location-stall pressure is active" in issue for issue in issues)


def test_validate_scene_plan_draft_rejects_same_location_under_location_transition_obligation() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Same Hallway",
                "SCENE_SUMMARY: You keep talking in the same hall.",
                "MATERIAL_CHANGE: The conversation continues without actually moving.",
                "OPENING_BEAT: dialogue_turn",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )

    issues = validate_scene_plan_draft(
        packet={"location_transition_obligation": {"active": True}},
        draft=scene_plan,
        resolution={
            "protagonist_name": "The Tall Gnome",
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "encountered_names": {"the tall gnome"},
            "current_location_id": 2,
            "path_location_name_map": {"echoing orchard": {"id": 3, "name": "Echoing Orchard"}},
            "path_location_id_map": {3: {"id": 3, "name": "Echoing Orchard"}},
        },
    )

    assert any("promised a location transition" in issue for issue in issues)


def test_validate_scene_plan_draft_allows_path_safe_return_location_under_location_transition_obligation() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Back To The Orchard",
                "SCENE_SUMMARY: You leave the archive and head back to the orchard with the fragment.",
                "MATERIAL_CHANGE: The branch returns to a known place as a brand-new scene.",
                "OPENING_BEAT: transition",
                "LOCATION_STATUS: return_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "RETURN_LOCATION: Echoing Orchard",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: The corridor tips you back toward the orchard's replaying fruit.",
            ]
        )
    )

    issues = validate_scene_plan_draft(
        packet={"location_transition_obligation": {"active": True}},
        draft=scene_plan,
        resolution={
            "protagonist_name": "The Tall Gnome",
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "encountered_names": {"the tall gnome"},
            "current_location_id": 2,
            "path_location_name_map": {"echoing orchard": {"id": 3, "name": "Echoing Orchard"}},
            "path_location_id_map": {3: {"id": 3, "name": "Echoing Orchard"}},
        },
    )

    assert not any("promised a location transition" in issue for issue in issues)

    none_settings_scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY:",
                "0: The mirrored hall shivers awake.",
            ]
        )
    )
    assert none_settings_scene_body.settings.visible_when_speaking is True
    assert none_settings_scene_body.settings.start_show_all_from_last_node is True
    assert none_settings_scene_body.settings.mc_always_visible is True

    raw_scene_body = parse_scene_body_form(
        "\n".join(
            [
                "You find a sandwich on the ground. Tentatively, you pick it up.",
                "1n: go ahead brah, eat up",
                "1: Who are you????",
                "1n: I just told you man, name's Spike Jonathan.",
                "I think you're gonna want to eat that sandwich",
                "for a craaaaazy trip",
                "1: is it safe",
                "1n: only one way to find out",
            ]
        )
    )
    assert raw_scene_body.settings.visible_when_speaking is True
    assert raw_scene_body.settings.start_show_all_from_last_node is True
    assert raw_scene_body.settings.mc_always_visible is True
    assert raw_scene_body.textboxes[0].speaker_ref == "0"
    assert raw_scene_body.textboxes[0].text.startswith("You find a sandwich on the ground.")
    assert raw_scene_body.textboxes[2].speaker_ref == "1"
    assert "for a craaaaazy trip" in raw_scene_body.textboxes[3].text

    narrator_alias_body = parse_scene_body_form(
        "\n".join(
            [
                "@show narrator",
                "@show 0",
                "@show n",
                "n: this should still be narrator text",
            ]
        )
    )
    assert narrator_alias_body.textboxes[0].speaker_ref == "n"

    narrator_without_colon_body = parse_scene_body_form(
        "\n".join(
            [
                "Narrator",
                "This should still be narrator text without requiring a colon.",
            ]
        )
    )
    assert narrator_without_colon_body.textboxes[0].speaker_ref == "0"
    assert "without requiring a colon" in narrator_without_colon_body.textboxes[0].text

    scene_body_template = build_form_template("scene_body")
    assert "This is NOT JSON. Use a simple newline-based labeled input format." in scene_body_template
    assert "Put each field on its own new line. Newlines are the only valid separator between fields." in scene_body_template
    assert "Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between top-level fields." in scene_body_template
    assert "Optional. Omit SCENE_SETTINGS entirely to use the default settings, or write SCENE_SETTINGS: NONE for the same effect." in scene_body_template
    assert "or write SCENE_SETTINGS: NONE for the same effect." in scene_body_template
    assert "Optional label. If you omit the words SCENE_BODY and just enter the script text" in scene_body_template
    assert "You may use either one-per-line 'key: value' syntax or comma-separated 'key=value' syntax." in scene_body_template
    assert "newlines are the ONLY valid separator for switching speakers, starting a new textbox, or applying visibility commands." in scene_body_template
    assert "Do not use pipes '|', commas, semicolons, arrows, or any other separator characters" in scene_body_template
    assert "A numbered speaker line like '1:' means that character is SPEAKING out loud." in scene_body_template
    assert "Do not put any choices, option lists, or menu text inside SCENE_BODY." in scene_body_template
    assert "Do not write attribution like 'says the Tall Gnome' inside the dialogue text" in scene_body_template

    scene_plan_with_ids = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Mirror Junction",
                "SCENE_SUMMARY: The protagonist spots two familiar figures reflected in the hall glass.",
                "MATERIAL_CHANGE: The scene turns from solitary exploration into a cast-aware encounter setup.",
                "OPENING_BEAT: discovery",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )

    scene_body_prompt = build_step_prompt(
        step_name="scene_body",
        packet={"branch_key": "default"},
        state=NormalRunConversationState(scene_plan=scene_plan_with_ids),
        requested_choice_count=2,
    )
    assert "You may omit SCENE_SETTINGS entirely to use the default settings shown above, or write SCENE_SETTINGS: NONE for the same effect." in scene_body_prompt
    assert "You may also omit the SCENE_BODY label entirely and just write the script itself" in scene_body_prompt
    assert "When the narrator is speaking about the protagonist, refer to the protagonist as 'you' and 'your', not by name." in scene_body_prompt
    assert "If you want the protagonist visibly on-screen, the protagonist must be included in SCENE_CAST." in scene_body_prompt
    assert "Newlines are the ONLY valid separator for switching speakers, starting a new textbox, or applying visibility commands." in scene_body_prompt
    assert "Do not use pipes '|', commas, semicolons, arrows, or any other separator characters" in scene_body_prompt
    assert "A numbered speaker line like '1:' means that character is SPEAKING out loud." in scene_body_prompt
    assert "Do not put any choices, option lists, or menu text inside SCENE_BODY." in scene_body_prompt
    assert "Visibility commands must each be isolated on their own line, with a newline before and after the command." in scene_body_prompt
    assert "Dialogue text should be only what the character says." in scene_body_prompt
    assert "Respond with ONLY the provided fields and corresponding values. Do not add any other fields not present at this step of the run." in scene_body_prompt

    scene_plan_template = build_form_template("scene_plan")
    assert "SCENE_CAST: <write one actual value only: MC_ONLY, SAME, NONE, or comma-separated canonical character ids/names like 1, 2>" in scene_plan_template
    assert "RETURN_LOCATION: <existing canonical location id or name, or NONE>" in scene_plan_template

    scene_plan_prompt = build_step_prompt(
        step_name="scene_plan",
        packet={"branch_key": "default"},
        state=NormalRunConversationState(),
        requested_choice_count=2,
    )
    assert "For SCENE_CAST, write one actual value only, such as MC_ONLY or 1, 2. Do not repeat the syntax guide or the option list." in scene_plan_prompt

    pressured_scene_plan_prompt = build_step_prompt(
        step_name="scene_plan",
        packet={
            "branch_key": "default",
            "isolation_pressure": {"active": True},
            "new_character_pressure": {"active": True},
            "location_stall_pressure": {"active": True},
        },
        state=NormalRunConversationState(),
        requested_choice_count=2,
    )
    assert "Current isolation pressure rules" in pressured_scene_plan_prompt
    assert "a new location alone does NOT satisfy isolation pressure" in pressured_scene_plan_prompt
    assert "Current new-character pressure rules" in pressured_scene_plan_prompt
    assert "reusing only existing characters does NOT satisfy new-character pressure" in pressured_scene_plan_prompt
    assert "Current location-stall pressure rules" in pressured_scene_plan_prompt
    assert "CHOICE_CLASS: location_transition option in the menu" in pressured_scene_plan_prompt
    assert "a new character alone does NOT satisfy location-stall pressure" in pressured_scene_plan_prompt
    assert "IDEAS.md as a main source of fresh people, places, and whimsical turns" in pressured_scene_plan_prompt

    link_nodes_template = build_form_template("link_nodes")
    assert "TRANSITION_TITLE" in link_nodes_template
    assert "TRANSITION_SUMMARY" in link_nodes_template
    assert "This transition node must have no choices" in link_nodes_template

    resolution = {
        "character_name_map": {},
        "character_id_map": {},
        "current_visible_cast_names": [],
        "protagonist_name": "The Tall Gnome",
        "encountered_names": {"the tall gnome"},
    }
    state_for_compile = type("State", (), {})()
    state_for_compile.scene_plan = scene_plan_with_ids
    state_for_compile.scene_body = narrator_alias_body
    compiled_body, compile_issues = compile_scene_body_draft(
        state=state_for_compile,
        draft=narrator_alias_body,
        resolution=resolution,
    )
    assert compile_issues == []
    assert compiled_body is not None
    assert compiled_body.dialogue_lines[0].speaker == "Narrator"

    mc_visibility_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Shared Frame",
                "SCENE_SUMMARY: The protagonist stays on-screen while another voice takes over the beat.",
                "MATERIAL_CHANGE: The scene proves the main character can remain visible even when another speaker becomes active.",
                "OPENING_BEAT: dialogue_turn",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY, Brass Patrol Member",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    mc_visibility_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS:",
                "visible_when_speaking: true",
                "start_show_all_from_last_node: false",
                "mc_always_visible: true",
                "SCENE_BODY:",
                "1: I don't like this.",
                "2: Then you should leave.",
            ]
        )
    )
    state_for_mc_visibility = type("State", (), {})()
    state_for_mc_visibility.scene_plan = mc_visibility_plan
    state_for_mc_visibility.scene_body = mc_visibility_body
    compiled_mc_body, mc_compile_issues = compile_scene_body_draft(
        state=state_for_mc_visibility,
        draft=mc_visibility_body,
        resolution={
            "character_name_map": {"brass patrol member": {"id": 2, "name": "Brass Patrol Member"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}, 2: {"id": 2, "name": "Brass Patrol Member"}},
            "current_visible_cast_names": [],
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome", "brass patrol member"},
        },
    )
    assert mc_compile_issues == []
    assert compiled_mc_body is not None

    body_choice_issues = validate_scene_body_draft(
        packet={},
        state=NormalRunConversationState(scene_plan=scene_plan_with_ids),
        draft=parse_scene_body_form(
            "\n".join(
                [
                    "0: The vault presses in around you.",
                    "You can choose how to react.",
                ]
            )
        ),
        resolution={
            "character_name_map": {},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome"},
        },
    )
    assert any("must not contain menu text" in issue for issue in body_choice_issues)
    assert compiled_mc_body.hidden_lines_by_character["the tall gnome"] == []

    scene_plan_with_new_character = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: New Arrival",
                "SCENE_SUMMARY: A stranger steps into the hall.",
                "MATERIAL_CHANGE: The scene stops being solitary because a new stranger appears and can now speak.",
                "OPENING_BEAT: arrival",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: Spike Jonathan",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: Spike Jonathan ducks through the doorway with a guilty look.",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    assert scene_plan_with_new_character.new_character_names == ["Spike Jonathan"]

    choice = parse_choice_form(
        "\n".join(
            [
                "CHOICE_TEXT: Answer the patrol member directly",
                "CHOICE_CLASS: commitment",
                "NEXT_NODE: The questioning begins in earnest.",
                "FURTHER_GOALS: Turn this branch into a social confrontation instead of another inspection beat.",
                "ENDING_CATEGORY: NONE",
                "TARGET_EXISTING_NODE: NONE",
            ]
        )
    )
    assert isinstance(choice, ChoiceDraft)
    assert choice.choice_class == "commitment"

    extras = parse_scene_extras_form(
        "\n".join(
            [
                "CHARACTER_DETAILS: Brass Patrol Member | He emerges from the mist with a rattling lantern.",
                "CHARACTER_ART_HINTS: Brass Patrol Member | Narrow brass-armored patrol runner with a dented lantern.",
            ]
        ),
        expected_labels=["CHARACTER_DETAILS", "CHARACTER_ART_HINTS"],
    )
    assert isinstance(extras, SceneExtrasDraft)
    assert extras.new_characters[0].name == "Brass Patrol Member"

    hooks = parse_scene_hooks_form(
        "\n".join(
            [
                "HOOK_ACTION: NEW_HOOK",
                "HOOK_IMPORTANCE: minor",
                "HOOK_TYPE: patrol-pressure",
                "HOOK_SUMMARY: Patrol attention now lingers on the branch.",
                "HOOK_PAYOFF_CONCEPT: The patrol keeps recognizing irregular signs around the protagonist.",
                "HOOK_ID: NONE",
                "HOOK_STATUS: NONE",
                "HOOK_PROGRESS_NOTE: NONE",
                "CLUE_TAGS: lantern-sighting",
                "STATE_TAGS: patrol-pressure",
                "GLOBAL_DIRECTION_NOTES: plotline | Lantern Trouble | Patrol attention now lingers on the branch. | 3",
            ]
        )
    )
    assert isinstance(hooks, SceneHooksDraft)
    assert hooks.clue_tags == ["lantern-sighting"]

    hooks_with_annotated_id = parse_scene_hooks_form(
        "\n".join(
            [
                "HOOK_ACTION: UPDATE_HOOK",
                "HOOK_IMPORTANCE: NONE",
                "HOOK_TYPE: NONE",
                "HOOK_SUMMARY: NONE",
                "HOOK_PAYOFF_CONCEPT: NONE",
                "HOOK_ID: 1 (The original hook about the hidden past event)",
                "HOOK_STATUS: active",
                "HOOK_PROGRESS_NOTE: The hook advances.",
                "CLUE_TAGS: NONE",
                "STATE_TAGS: NONE",
                "GLOBAL_DIRECTION_NOTES: NONE",
            ]
        )
    )
    assert hooks_with_annotated_id.hook_id == 1

    art = parse_scene_art_form(
        "CHARACTER_ART_HINTS: Brass Patrol Member | Narrow brass-armored patrol runner with a dented lantern.\n"
        "LOCATION_ART_HINTS: NONE",
        expected_labels=["CHARACTER_ART_HINTS", "LOCATION_ART_HINTS"],
    )
    assert isinstance(art, SceneArtDraft)
    assert art.character_art_hints["Brass Patrol Member"].startswith("Narrow brass-armored")

    choice_with_annotated_target = parse_choice_form(
        "\n".join(
            [
                "CHOICE_TEXT: Return to the prior hub",
                "CHOICE_CLASS: progress",
                "NEXT_NODE: You retrace your steps toward the familiar junction.",
                "FURTHER_GOALS: Use a deliberate merge to keep the frontier narrow under pressure.",
                "ENDING_CATEGORY: NONE",
                "TARGET_EXISTING_NODE: 7 (Hub Return)",
            ]
        )
    )
    assert choice_with_annotated_target.target_existing_node == 7


def test_normal_worker_session_resets_on_limit_or_model_change(tmp_path: Path) -> None:
    session_path = tmp_path / "session.json"
    system_prompt = build_normal_conversation_system_prompt("guide text")
    assert "Place the final answer in normal assistant content. Do not put the real answer only in hidden reasoning_content." in system_prompt

    first = load_or_create_normal_session(
        session_path=session_path,
        model="model-a",
        system_prompt=system_prompt,
        context_run_limit=3,
        reset_context=False,
    )
    assert first.run_count == 0
    assert first.messages[0].role == "system"
    session_path.write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "normal",
                "model": "model-a",
                "run_count": 3,
                "messages": [{"role": "system", "content": system_prompt}],
            }
        ),
        encoding="utf-8",
    )
    reset_for_limit = load_or_create_normal_session(
        session_path=session_path,
        model="model-a",
        system_prompt=system_prompt,
        context_run_limit=3,
        reset_context=False,
    )
    assert reset_for_limit.run_count == 0

    session_path.write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "normal",
                "model": "model-a",
                "run_count": 1,
                "messages": [{"role": "system", "content": "old"}],
            }
        ),
        encoding="utf-8",
    )
    reset_for_model = load_or_create_normal_session(
        session_path=session_path,
        model="model-b",
        system_prompt=system_prompt,
        context_run_limit=3,
        reset_context=False,
    )
    assert reset_for_model.model == "model-b"
    assert reset_for_model.run_count == 0
    assert reset_for_model.messages[0].content == system_prompt


def test_call_lm_studio_returns_blank_when_model_emits_no_content(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": "",
                        },
                        "finish_reason": "stop",
                    }
                ]
            }

    def fake_post(*args, **kwargs):
        return FakeResponse()

    monkeypatch.setattr("app.tools.run_story_worker_local.httpx.post", fake_post)

    assert call_lm_studio(
        api_base="http://127.0.0.1:1234/v1",
        model="test-model",
        system_prompt="system",
        user_prompt="user",
        messages=None,
        response_format=None,
        temperature=0.2,
        max_tokens=256,
        request_timeout=30.0,
    ) == ""


def test_request_nonempty_ai_step_response_retries_blank_outputs(monkeypatch) -> None:
    responses = ["", "", "SCENE_TITLE: Recovered"]

    def fake_call_lm_studio(**kwargs) -> str:
        return responses.pop(0)

    monkeypatch.setattr("app.tools.run_story_worker_local.call_lm_studio", fake_call_lm_studio)

    assert request_nonempty_ai_step_response(
        api_base="http://127.0.0.1:1234/v1",
        model="test-model",
        messages=[{"role": "user", "content": "prompt"}],
        temperature=0.2,
        max_tokens=256,
        request_timeout=30.0,
        empty_retry_limit=3,
    ) == "SCENE_TITLE: Recovered"


def test_validation_attempt_log_rolls_over_after_1000_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "validation_attempts.md"
    for index in range(1000):
        append_validation_attempt_log_record(
            log_path=log_path,
            record={
                "timestamp": "2026-04-15 03:00:00 AM",
                "model": "test-model",
                "step": "scene_body",
                "issues": [f"issue {index}"],
                "attempted_output": f"line {index}",
            },
        )
    append_validation_attempt_log_record(
        log_path=log_path,
        record={
            "timestamp": "2026-04-15 03:00:01 AM",
            "model": "test-model",
            "step": "scene_body",
            "issues": ["fresh issue"],
            "attempted_output": "first line\\nsecond line",
        },
    )

    text = log_path.read_text(encoding="utf-8")
    assert "fresh issue" in text
    assert "first line\nsecond line" in text
    assert "```text" in text
    assert "issue 999" not in text


def test_validation_attempt_log_includes_retry_index(tmp_path: Path) -> None:
    log_path = tmp_path / "validation_attempts.md"
    append_validation_attempt_log_record(
        log_path=log_path,
        record={
            "timestamp": "2026-04-15 03:00:01 AM",
            "model": "test-model",
            "step": "scene_body",
            "retry_index": 2,
            "issues": ["still bad"],
            "attempted_output": "SCENE_BODY: NONE",
        },
    )

    text = log_path.read_text(encoding="utf-8")
    assert "retry=2" in text


def test_validation_attempt_log_includes_run_separator(tmp_path: Path) -> None:
    log_path = tmp_path / "validation_attempts.md"
    append_validation_attempt_run_separator(
        log_path=log_path,
        record={
            "timestamp": "2026-04-15 07:30:00 PM",
            "model": "test-model",
            "run_mode": "normal",
            "choice_id": 7,
        },
    )
    append_validation_attempt_log_record(
        log_path=log_path,
        record={
            "timestamp": "2026-04-15 07:30:01 PM",
            "model": "test-model",
            "step": "scene_body",
            "issues": ["still bad"],
            "attempted_output": "SCENE_BODY: NONE",
        },
    )

    text = log_path.read_text(encoding="utf-8")
    assert "-" * 72 in text
    assert "run_mode=normal" in text
    assert "choice_id=7" in text


def test_build_step_prompt_includes_retry_line_on_retry() -> None:
    prompt = build_step_prompt(
        step_name="scene_plan",
        packet={},
        state=NormalRunConversationState(),
        requested_choice_count=2,
        issues=["Missing required labels: SCENE_TITLE"],
        retry_index=2,
    )

    assert "Step: scene_plan" in prompt
    assert "Retry: 2" in prompt


def test_build_step_prompt_surfaces_frontier_pressure_constraints_up_front() -> None:
    prompt = build_step_prompt(
        step_name="choice",
        packet={
            "frontier_choice_constraints": {
                "pressure_level": "soft",
                "must_include_merge_or_closure": True,
                "max_fresh_choices_under_pressure": 1,
                "allow_second_fresh_choice_only_for_bloom_scenes": True,
                "inspection_choices_should_reconverge_under_pressure": True,
                "guidance": "Under soft frontier pressure, keep branching narrow.",
            },
            "consequential_choice_requirement": {"required": True},
        },
        state=NormalRunConversationState(),
        requested_choice_count=2,
        choice_index=0,
    )

    assert "Current frontier pressure rules:" in prompt
    assert "include at least one merge or closure path in this scene" in prompt
    assert "THIS RUN WILL ONLY VALIDATE if at least one choice uses TARGET_EXISTING_NODE" in prompt
    assert "allow at most 1 fresh branch choice(s)" in prompt
    assert "inspection choices should reconverge quickly" in prompt
    assert "MAKE this choice either a merge or a closure." in prompt
    assert "Fix that here in this choice-writing phase" in prompt


def test_scene_plan_prompt_clarifies_new_character_names_only_and_auto_append() -> None:
    prompt = build_step_prompt(
        step_name="scene_plan",
        packet={},
        state=NormalRunConversationState(),
        requested_choice_count=2,
    )

    assert "For NEW_CHARACTERS, write only the new characters' names" in prompt
    assert "Do not invent ids, slot numbers, or parenthetical numbers there." in prompt
    assert "they will be added to the accepted scene cast automatically" in prompt


def test_build_step_prompt_surfaces_location_transition_requirement_when_location_pressure_is_active() -> None:
    prompt = build_step_prompt(
        step_name="choice",
        packet={
            "location_stall_pressure": {"active": True},
        },
        state=NormalRunConversationState(),
        requested_choice_count=2,
        choice_index=0,
    )

    assert "This menu must include at least one CHOICE_CLASS: location_transition option." in prompt
    assert "future expansion will move to a different location than the current one" in prompt
    assert "Allowed CHOICE_CLASS values are only: inspection, progress, commitment, location_transition, ending." in prompt


def test_build_step_prompt_repeats_merge_closure_requirement_in_hooks_step() -> None:
    prompt = build_step_prompt(
        step_name="hooks",
        packet={
            "frontier_choice_constraints": {
                "pressure_level": "soft",
                "must_include_merge_or_closure": True,
                "max_fresh_choices_under_pressure": 1,
                "inspection_choices_should_reconverge_under_pressure": True,
            },
        },
        state=NormalRunConversationState(),
        requested_choice_count=2,
    )

    assert "Current frontier pressure rules:" in prompt
    assert "Reminder: if frontier pressure required a merge or closure path" in prompt
    assert "TARGET_EXISTING_NODE for a deliberate merge" in prompt
    assert "non-NONE ENDING_CATEGORY for a real closure" in prompt


def test_validate_choice_draft_requires_choice_one_to_handle_merge_or_closure_under_pressure() -> None:
    issues = validate_choice_draft(
        packet={
            "frontier_choice_constraints": {
                "must_include_merge_or_closure": True,
            }
        },
        state=NormalRunConversationState(),
        draft=ChoiceDraft(
            choice_text="Ask what they want",
            choice_class="commitment",
            next_node="The patrol answers cautiously while keeping the vault sealed.",
            further_goals="Learn more about the patrol and the vault pressure.",
            ending_category=None,
            target_existing_node=None,
        ),
        resolution={},
        choice_index=0,
    )

    assert any("choice_1 must either use TARGET_EXISTING_NODE" in issue for issue in issues)


def test_validate_choice_draft_rejects_ungrounded_local_prop_early() -> None:
    state = NormalRunConversationState(
        scene_plan=ScenePlanDraft(
            scene_title="The Seam's Whisper",
            scene_summary="You watch the humming seam in the vault as the air tightens around it.",
            material_change="The vault pulse becomes immediate and threatening.",
            opening_beat="discovery",
            location_status="same_location",
            scene_cast_mode="mc_only",
        ),
        scene_body=SceneBodyDraft(
            settings={},
            raw_body="You steady yourself beside the humming seam while the vault light stutters around your hand.",
            textboxes=[],
        ),
    )
    issues = validate_choice_draft(
        packet={
            "selected_frontier_item": {
                "choice_text": "Reach for the humming hat to feel its pulse",
                "existing_choice_notes": "NEXT_NODE: The hat's pulse syncs with your hand. FURTHER_GOALS: Learn why the vault hums.",
            },
            "context_summary": {
                "current_node": {
                    "title": "The Seam's Whisper",
                    "summary": "A sealed glass vault pulses with memory-light while a seam hums in the wall.",
                }
            },
        },
        state=state,
        draft=ChoiceDraft(
            choice_text="Examine the stitch and its resonance more closely",
            choice_class="inspection",
            next_node="You study the humming wall more carefully and search for a clue in the vibration.",
            further_goals="Stay with the local mystery and deepen the vault clue trail.",
            ending_category=None,
            target_existing_node=None,
        ),
        resolution={},
        choice_index=0,
    )

    assert any("introduces a new focal prop or marker" in issue for issue in issues)


def test_validate_choice_draft_rejects_invalid_merge_target_early() -> None:
    issues = validate_choice_draft(
        packet={
            "context_summary": {
                "merge_candidates": [
                    {"node_id": 7, "title": "Before the Counting Bell"},
                ]
            }
        },
        state=NormalRunConversationState(),
        draft=ChoiceDraft(
            choice_text="Merge back into an older thread",
            choice_class="progress",
            next_node="The branch reconnects with an earlier scene that still fits the current pressure.",
            further_goals="Compress the frontier and continue from a stronger shared lane.",
            ending_category=None,
            target_existing_node=999,
        ),
        resolution={},
        choice_index=0,
    )

    assert any("not a valid merge candidate" in issue for issue in issues)


def test_is_force_next_override_accepts_secret_spellings() -> None:
    assert is_force_next_override("FORCE NEXT")
    assert is_force_next_override("force_next")
    assert is_force_next_override("FORce NXT")
    assert not is_force_next_override("END")


def test_request_human_step_returns_immediately_for_force_next(monkeypatch, capsys) -> None:
    inputs = iter(["FORCE NEXT"])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))

    response = request_human_step("Step: scene_body")

    assert response == "FORCE NEXT"


def test_build_forced_choice_draft_prefers_merge_under_frontier_pressure() -> None:
    draft = build_forced_choice_draft(
        packet={
            "frontier_choice_constraints": {
                "must_include_merge_or_closure": True,
            },
            "context_summary": {
                "merge_candidates": [
                    {"node_id": 7, "title": "Before the Counting Bell"},
                ]
            },
        },
        choice_index=0,
    )

    assert draft.target_existing_node == 7
    assert draft.ending_category is None
    assert "Merge back into Before the Counting Bell" == draft.choice_text


def test_apply_normal_result_returns_preview_only_when_force_next_used(tmp_path: Path) -> None:
    client, _db_path = build_client(tmp_path)
    candidate = parse_llm_result(
        {"run_mode": "normal"},
        json.dumps(
            {
                "branch_key": "default",
                "scene_title": "Preview",
                "scene_summary": "Preview only.",
                "scene_text": "You pause here.",
                "dialogue_lines": [],
                "choices": [
                    {
                        "choice_text": "Merge back into Before the Counting Bell",
                        "notes": "NEXT_NODE: The branch reconverges. FURTHER_GOALS: Keep frontier pressure down.",
                        "choice_class": "progress",
                        "target_node_id": 1,
                    }
                ],
            }
        ),
    )

    result = apply_normal_result(
        packet={
            "pre_change_url": "http://127.0.0.1:8001/play?branch_key=default&scene=1",
            "selected_frontier_item": {"choice_id": 42},
            "preview_payload": {"branch_key": "default", "current_node_id": 1, "choice_id": 42},
        },
        candidate=candidate,
        client=client,
        dry_run=False,
        project_root=tmp_path,
        validation_payload={
            "valid": True,
            "forced_preview_only": True,
            "issues": ["FORCE NEXT was used in human mode."],
            "force_next_steps": ["scene_plan"],
        },
    )

    assert result["dry_run"] is True
    assert result["forced_preview_only"] is True
    assert "nothing was validated or applied" in result["message"].lower()


def test_compile_scene_body_splits_long_same_speaker_block_into_multiple_textboxes() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Split Test",
                "SCENE_SUMMARY: You enter the passage and keep moving.",
                "MATERIAL_CHANGE: The passage becomes an active route and the pressure keeps building.",
                "OPENING_BEAT: transition",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "1: You step into the narrow passage and the damp stone closes around you. The lantern glow flickers against the wall.",
                "The air smells like wet metal. Somewhere below, a door shuts hard.",
            ]
        )
    )
    state = NormalRunConversationState(scene_plan=scene_plan, scene_body=scene_body)
    compiled, issues = compile_scene_body_draft(
        state=state,
        draft=scene_body,
        resolution={
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome"},
        },
    )

    assert issues == []
    assert compiled is not None
    assert [line.speaker for line in compiled.dialogue_lines] == ["You", "You"]
    assert len(compiled.dialogue_lines) == 2
    assert compiled.dialogue_lines[0].text.endswith("The lantern glow flickers against the wall.")
    assert compiled.dialogue_lines[1].text == "The air smells like wet metal. Somewhere below, a door shuts hard."


def test_parse_scene_body_does_not_treat_mid_sentence_colon_as_speaker_switch() -> None:
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "You instinctively flatten yourself against the fungi, using the shadows for cover.",
                "The patrol sweeps across the field, their attention focused on measuring and recording everything: the precise spacing of the fungi, the discoloration in the soil, and the subtle shifts in light.",
            ]
        )
    )

    assert len(scene_body.textboxes) == 1
    assert scene_body.textboxes[0].speaker_ref == "0"
    assert "everything: the precise spacing" in scene_body.textboxes[0].text


def test_compile_scene_body_allows_show_1_for_mc_only_even_without_protagonist_name() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: MC Only Command",
                "SCENE_SUMMARY: The protagonist reacts as patrol pressure closes in.",
                "MATERIAL_CHANGE: The protagonist becomes explicitly visible before the patrol arrives.",
                "OPENING_BEAT: consequence",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "@show 1",
                "You snap your head up as the brass patrol rounds the mushroom cluster.",
            ]
        )
    )
    state = NormalRunConversationState(scene_plan=scene_plan, scene_body=scene_body)

    compiled, issues = compile_scene_body_draft(
        state=state,
        draft=scene_body,
        resolution={
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "protagonist_id": 1,
            "protagonist_name": None,
            "encountered_names": {"the tall gnome"},
        },
    )

    assert issues == []
    assert compiled is not None


def test_validate_scene_body_draft_flags_narrator_owned_in_world_dialogue() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Brass Arrival",
                "SCENE_SUMMARY: A brass patrol corners you near the vault seam.",
                "MATERIAL_CHANGE: Patrol pressure becomes immediate and verbal.",
                "OPENING_BEAT: consequence",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY, Brass Patrol Member",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "The brass patrol halts around you.",
                "Brass Patrol Member clears his throat and says, \"Hold where you are.\"",
            ]
        )
    )
    issues = validate_scene_body_draft(
        packet={},
        state=NormalRunConversationState(scene_plan=scene_plan),
        draft=scene_body,
        resolution={
            "character_name_map": {"brass patrol member": {"id": 2, "name": "Brass Patrol Member"}},
            "character_id_map": {
                1: {"id": 1, "name": "The Tall Gnome"},
                2: {"id": 2, "name": "Brass Patrol Member"},
            },
            "current_visible_cast_names": [],
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome", "brass patrol member"},
        },
    )

    assert any("gives spoken dialogue to an in-world character through Narrator text" in issue for issue in issues)
    assert not scene_body_issues_require_scene_plan_rewind(issues)


def test_validate_scene_body_draft_requests_scene_plan_rewind_for_uncast_speaking_character() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Unexpected Enumerator",
                "SCENE_SUMMARY: An enumerator appears and addresses you near the vault.",
                "MATERIAL_CHANGE: A new in-world speaker forces the cast to expand.",
                "OPENING_BEAT: consequence",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "The lead enumerator studies your hand.",
                "He clears his throat and calls out, \"Sir, we detect an anomaly here.\"",
            ]
        )
    )
    issues = validate_scene_body_draft(
        packet={},
        state=NormalRunConversationState(scene_plan=scene_plan),
        draft=scene_body,
        resolution={
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome"},
        },
    )

    assert any("Return to scene_plan and either add the proper casting or declare a new character" in issue for issue in issues)
    assert scene_body_issues_require_scene_plan_rewind(issues)


def test_validate_scene_body_draft_rejects_raw_quotes_in_narrator_text() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Raw Quotes",
                "SCENE_SUMMARY: The narrator should not carry raw quoted speech.",
                "MATERIAL_CHANGE: The formatting rule becomes explicit before choices.",
                "OPENING_BEAT: consequence",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "\"Anomaly detected. Source: Unregistered temporal signature.\"",
            ]
        )
    )
    issues = validate_scene_body_draft(
        packet={},
        state=NormalRunConversationState(scene_plan=scene_plan),
        draft=scene_body,
        resolution={
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome"},
        },
    )

    assert any("uses raw quotes inside Narrator text" in issue for issue in issues)
    assert not scene_body_issues_require_scene_plan_rewind(issues)


def test_validate_scene_body_draft_allows_escaped_quotes_in_narrator_text() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Escaped Quotes",
                "SCENE_SUMMARY: Escaped quote marks can remain in narration prose.",
                "MATERIAL_CHANGE: The narrator can still mention quoted words as prose when escaped.",
                "OPENING_BEAT: quiet_aftermath",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "The word \\\"anomaly\\\" hangs in your mind long after the patrol passes.",
            ]
        )
    )
    issues = validate_scene_body_draft(
        packet={},
        state=NormalRunConversationState(scene_plan=scene_plan),
        draft=scene_body,
        resolution={
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome"},
        },
    )

    assert not any("uses raw quotes inside Narrator text" in issue for issue in issues)


def test_validate_scene_body_draft_requests_scene_plan_rewind_for_isolation_pressure_failure() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Thin Company",
                "SCENE_SUMMARY: The scene promises pressure but still leaves you alone.",
                "MATERIAL_CHANGE: The branch tries to continue in solitude again.",
                "OPENING_BEAT: pressure_escalation",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "You remain alone with the humming seam and listen to your own breathing.",
                "Nothing else arrives and nobody addresses you.",
            ]
        )
    )
    issues = validate_scene_body_draft(
        packet={"isolation_pressure": {"active": True}},
        state=NormalRunConversationState(scene_plan=scene_plan),
        draft=scene_body,
        resolution={
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome"},
        },
    )

    assert any("Isolation pressure is active" in issue for issue in issues)
    assert scene_body_issues_require_scene_plan_rewind(issues)


def test_validate_scene_body_draft_requests_scene_plan_rewind_for_new_character_pressure_without_setup() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Old Company",
                "SCENE_SUMMARY: You stay with familiar people only.",
                "MATERIAL_CHANGE: The branch keeps moving without introducing anyone new.",
                "OPENING_BEAT: pressure_escalation",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: 1, 2",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "The Brass Patrol Member keeps talking while no new person enters the scene.",
            ]
        )
    )
    issues = validate_scene_body_draft(
        packet={"new_character_pressure": {"active": True}},
        state=NormalRunConversationState(scene_plan=scene_plan),
        draft=scene_body,
        resolution={
            "character_name_map": {
                "the tall gnome": {"id": 1, "name": "The Tall Gnome"},
                "brass patrol member": {"id": 2, "name": "Brass Patrol Member"},
            },
            "character_id_map": {
                1: {"id": 1, "name": "The Tall Gnome"},
                2: {"id": 2, "name": "Brass Patrol Member"},
            },
            "current_visible_cast_names": [],
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome", "brass patrol member"},
        },
    )

    assert any("New-character pressure is active" in issue for issue in issues)
    assert scene_body_issues_require_scene_plan_rewind(issues)


def test_validate_scene_body_draft_retries_when_declared_new_character_never_appears() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Missing Arrival",
                "SCENE_SUMMARY: The plan declares someone new, but the body forgets them.",
                "MATERIAL_CHANGE: A fresh face should appear here.",
                "OPENING_BEAT: transition",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: Clerk Sedge",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: A tidy clerk arrives balancing a waxed ledger.",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "You wait by the seam and nothing new enters before the choices arrive.",
            ]
        )
    )
    issues = validate_scene_body_draft(
        packet={"new_character_pressure": {"active": True}},
        state=NormalRunConversationState(scene_plan=scene_plan),
        draft=scene_body,
        resolution={
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome"},
        },
    )

    assert any("SCENE_BODY still does not actually introduce the declared NEW_CHARACTERS" in issue for issue in issues)
    assert not scene_body_issues_require_scene_plan_rewind(issues)


def test_validate_scene_body_draft_requests_scene_plan_rewind_for_location_transition_obligation_failure() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Supposed Move",
                "SCENE_SUMMARY: The scene claims a return but the body never actually moves.",
                "MATERIAL_CHANGE: The plan says the branch shifts place.",
                "OPENING_BEAT: transition",
                "LOCATION_STATUS: return_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "RETURN_LOCATION: Echoing Orchard",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "You stand exactly where you were, watching the same seam breathe in the same wall.",
                "The place does not change and the moment does not shift.",
            ]
        )
    )
    issues = validate_scene_body_draft(
        packet={"location_transition_obligation": {"active": True}},
        state=NormalRunConversationState(scene_plan=scene_plan),
        draft=scene_body,
        resolution={
            "character_name_map": {"the tall gnome": {"id": 1, "name": "The Tall Gnome"}},
            "character_id_map": {1: {"id": 1, "name": "The Tall Gnome"}},
            "current_visible_cast_names": [],
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome"},
            "current_location_id": 2,
            "path_location_name_map": {"echoing orchard": {"id": 3, "name": "Echoing Orchard"}},
            "path_location_id_map": {3: {"id": 3, "name": "Echoing Orchard"}},
        },
    )

    assert any("promised a location transition" in issue for issue in issues)
    assert scene_body_issues_require_scene_plan_rewind(issues)


def test_compile_scene_body_rotates_out_oldest_non_protagonist_when_side_slots_fill() -> None:
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Junction Company",
                "SCENE_SUMMARY: Several characters are available, but only some should be visible at once.",
                "MATERIAL_CHANGE: The chamber fills with people in rotating beats.",
                "OPENING_BEAT: confrontation",
                "LOCATION_STATUS: new_location",
                "SCENE_CAST: 1, 2",
                "NEW_CHARACTERS: Gearwick, Clerk Sedge",
                "NEW_LOCATION: The Junction Mechanism Chamber",
                "NEW_CHARACTER_INTRO: New arrivals appear from different maintenance conduits.",
                "NEW_LOCATION_INTRO: The seam opens into a mechanical chamber.",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY:",
                "@show 2",
                "@show Gearwick",
                "@show Clerk Sedge",
                "Narrator: The chamber fills with anxious voices.",
            ]
        )
    )

    compiled, issues = compile_scene_body_draft(
        state=NormalRunConversationState(scene_plan=scene_plan),
        draft=scene_body,
        resolution={
            "character_name_map": {
                "the tall gnome": {"id": 1, "name": "The Tall Gnome"},
                "brass patrol member": {"id": 2, "name": "Brass Patrol Member"},
            },
            "character_id_map": {
                1: {"id": 1, "name": "The Tall Gnome"},
                2: {"id": 2, "name": "Brass Patrol Member"},
            },
            "current_visible_cast_names": [],
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome", "brass patrol member"},
        },
    )

    assert compiled is not None
    assert not issues
    assert compiled.hidden_lines_by_character["brass patrol member"] == [0]
    assert compiled.hidden_lines_by_character["gearwick"] == []
    assert compiled.hidden_lines_by_character["clerk sedge"] == []


def test_parse_scene_script_command_accepts_show_all_and_hide_all_variants() -> None:
    command_lines = [
        ("@show_all", "show_all"),
        ("@show all", "show_all"),
        ("@hide_all", "hide_all"),
        ("@hide all", "hide_all"),
    ]

    for raw_line, expected_action in command_lines:
        command = parse_scene_script_command(raw_line)
        assert command is not None
        assert command.action == expected_action
        assert command.targets == []


def test_validate_choice_menu_allows_distinct_progress_choices_when_one_has_real_motion() -> None:
    packet = {
        "consequential_choice_requirement": {"required": True},
    }
    choices = [
        ChoiceDraft(
            choice_text="Study the mirrored tram door for a route marker",
            choice_class="progress",
            next_node="The reflected door shows a route sigil and a safer angle of approach.",
            further_goals="Keep the branch moving while preserving the eerie transit setup.",
        ),
        ChoiceDraft(
            choice_text="Break for the side passage before the brass patrol closes it",
            choice_class="progress",
            next_node="You reach the side passage and force the patrol to choose whether to follow.",
            further_goals="Create real location motion and immediate pressure instead of another static inspection beat.",
        ),
    ]

    assert validate_choice_menu(packet=packet, choices=choices) == []


def test_validate_choice_menu_does_not_block_two_inspection_choices_when_strength_rule_is_off() -> None:
    packet = {
        "consequential_choice_requirement": {"required": False},
    }
    choices = [
        ChoiceDraft(
            choice_text="Inspect the frost on the wall",
            choice_class="inspection",
            next_node="The frost reveals a faint mark when you breathe on it.",
            further_goals="Gather a clue without forcing the branch onward yet.",
        ),
        ChoiceDraft(
            choice_text="Listen at the tram door for a hidden rhythm",
            choice_class="inspection",
            next_node="A second hum answers from the far side of the door.",
            further_goals="Let the scene stay investigatory without collapsing into duplicate wording.",
        ),
    ]

    assert validate_choice_menu(packet=packet, choices=choices) == []


def test_validate_choice_menu_rejects_repeated_touch_family_with_specific_fix() -> None:
    packet = {
        "recent_action_family_summary": {
            "repeated_action_family": "touch",
            "recent_action_family_counts": {"touch": 3},
        },
        "consequential_choice_requirement": {"required": False},
    }
    choices = [
        ChoiceDraft(
            choice_text="Touch the humming hat again",
            choice_class="inspection",
            next_node="Your fingers brush the hat and the pulse returns in a stronger rhythm.",
            further_goals="Probe the same local mystery for another immediate clue.",
        ),
        ChoiceDraft(
            choice_text="Touch the seam to compare its vibration",
            choice_class="inspection",
            next_node="The seam answers your hand with a second, sharper pulse of light.",
            further_goals="Keep testing the same touch-based route through the vault mystery.",
        ),
    ]

    issues = validate_choice_menu(packet=packet, choices=choices)

    assert any("overusing the 'touch' action family" in issue for issue in issues)
    assert any("merge using TARGET_EXISTING_NODE" in issue for issue in issues)


def test_validate_choice_menu_allows_merge_to_break_repeated_follow_pattern() -> None:
    packet = {
        "recent_action_family_summary": {
            "repeated_action_family": "follow",
            "recent_action_family_counts": {"follow": 4},
        },
        "consequential_choice_requirement": {"required": False},
    }
    choices = [
        ChoiceDraft(
            choice_text="Attempt to diffuse the tension by addressing the patrol's perceived anomaly directly.",
            choice_class="commitment",
            next_node="You speak up and try to redirect the patrol toward a bureaucratic explanation instead of panic.",
            further_goals="Turn the pressure into negotiation and learn more about the patrol's procedure.",
            ending_category=None,
            target_existing_node=4,
        ),
        ChoiceDraft(
            choice_text="Attempt to ignore the patrol and focus on the seam of green glass in the ground instead.",
            choice_class="inspection",
            next_node="You crouch by the seam and force yourself to focus on its vibration despite the patrol pressure.",
            further_goals="Stay with the local vault mystery a little longer before the scene reconverges.",
            ending_category=None,
            target_existing_node=None,
        ),
    ]

    assert validate_choice_menu(packet=packet, choices=choices) == []


def test_validate_choice_menu_allows_travel_or_escape_to_break_repeated_follow_pattern() -> None:
    packet = {
        "recent_action_family_summary": {
            "repeated_action_family": "follow",
            "recent_action_family_counts": {"follow": 4},
        },
        "consequential_choice_requirement": {"required": False},
    }
    choices = [
        ChoiceDraft(
            choice_text="Attempt to diffuse the tension by addressing the patrol's perceived anomaly directly.",
            choice_class="commitment",
            next_node="You try to keep the patrol talking instead of letting them tighten the circle around you.",
            further_goals="Use social maneuvering to survive the scrutiny and gather procedural clues.",
        ),
        ChoiceDraft(
            choice_text="Turn and run, putting distance between yourself and the patrol's scrutiny.",
            choice_class="progress",
            next_node="You break into a desperate run through the damp field before the patrol can close the gap.",
            further_goals="Gain distance, force location motion, and find temporary cover away from the immediate pressure.",
        ),
    ]

    assert validate_choice_menu(packet=packet, choices=choices) == []


def test_validate_choice_menu_does_not_raise_new_character_pressure_when_scene_plan_already_declared_one() -> None:
    packet = {
        "new_character_pressure": {"active": True},
    }
    state = NormalRunConversationState(
        scene_plan=ScenePlanDraft(
            scene_title="Registry Arrival",
            scene_summary="A new official is about to appear.",
            material_change="The branch reaches a new annex and introduces a fresh face.",
            opening_beat="transition",
            location_status="new_location",
            scene_cast_mode="explicit",
            scene_cast_entries=["1", "2"],
            new_character_names=["Chrono-Curator"],
            new_location_name="The Memory Registry Annex",
            new_character_intro="The Chrono-Curator steps out to address the disturbance.",
            new_location_intro="You enter the annex under a wash of registry light.",
        ),
        scene_body=SceneBodyDraft(
            raw_body="The annex hums while the Brass Patrol Member gestures toward the deeper shelves."
        ),
    )
    choices = [
        ChoiceDraft(
            choice_text="Follow the curator deeper into the annex",
            choice_class="commitment",
            next_node="The curator leads you past the first shelves toward a private registry alcove.",
            further_goals="Learn why the annex tracks mnemonic energy and what the curator wants from you.",
        ),
        ChoiceDraft(
            choice_text="Stay near the entrance and question the patrol member",
            choice_class="progress",
            next_node="You hold at the threshold and force the patrol member to explain why the annex reacted to you.",
            further_goals="Keep the curator at a distance while testing the patrol's knowledge of the system.",
        ),
    ]
    resolution = {
        "protagonist_name": "The Tall Gnome",
        "protagonist_id": 1,
        "current_visible_cast_names": [],
        "character_name_map": {
            "the tall gnome": {"id": 1, "name": "The Tall Gnome"},
            "brass patrol member": {"id": 2, "name": "Brass Patrol Member"},
        },
        "character_id_map": {
            1: {"id": 1, "name": "The Tall Gnome"},
            2: {"id": 2, "name": "Brass Patrol Member"},
        },
        "encountered_names": {"the tall gnome", "brass patrol member"},
    }

    issues = validate_choice_menu(packet=packet, choices=choices, state=state, resolution=resolution)

    assert not any("New-character pressure is active" in issue for issue in issues)


def test_validate_choice_menu_requires_location_transition_under_location_stall_pressure() -> None:
    packet = {
        "location_stall_pressure": {"active": True},
    }
    choices = [
        ChoiceDraft(
            choice_text="Follow the conduit line toward the inner registry",
            choice_class="commitment",
            next_node="You move deeper into the junction toward a brighter cluster of memory conduits.",
            further_goals="Learn what the chamber regulates and how it reacts to your altered hand.",
        ),
        ChoiceDraft(
            choice_text="Stay near the threshold and observe the chamber's rhythm",
            choice_class="progress",
            next_node="You hold at the entrance long enough to study how the chamber cycles its light and pressure.",
            further_goals="Gather enough information to decide whether the junction is safe to traverse further.",
        ),
    ]
    issues = validate_choice_menu(packet=packet, choices=choices)

    assert any("CHOICE_CLASS: location_transition" in issue for issue in issues)


def test_validate_choice_menu_allows_location_transition_under_location_stall_pressure() -> None:
    packet = {
        "location_stall_pressure": {"active": True},
    }
    choices = [
        ChoiceDraft(
            choice_text="Follow Elara through the orchard tram gate",
            choice_class="location_transition",
            next_node="Elara leads you out of the archive and back toward the Echoing Orchard through a hidden gate.",
            further_goals="Return to a known place with new context and compare the fragment against the orchard's replaying sounds.",
        ),
        ChoiceDraft(
            choice_text="Stay in the archive and question Elara first",
            choice_class="commitment",
            next_node="You hold Elara in place for one direct answer before anyone moves.",
            further_goals="Clarify what the map fragment means before committing to the route she suggests.",
        ),
    ]

    assert validate_choice_menu(packet=packet, choices=choices) == []


def test_validate_choice_menu_rejects_extra_fresh_branches_under_frontier_pressure_early() -> None:
    packet = {
        "frontier_budget_state": {"pressure_level": "soft"},
        "frontier_choice_constraints": {
            "max_fresh_choices_under_pressure": 1,
            "allow_second_fresh_choice_only_for_bloom_scenes": False,
        },
        "consequential_choice_requirement": {"required": False},
    }
    choices = [
        ChoiceDraft(
            choice_text="Appeal to the patrol leader",
            choice_class="commitment",
            next_node="The patrol leader pauses and demands a direct explanation for your presence.",
            further_goals="Turn the pressure into a social lane without opening more stray leaves.",
        ),
        ChoiceDraft(
            choice_text="Slip deeper into the side passage",
            choice_class="progress",
            next_node="You dart for the side passage before the patrol can fully close around you.",
            further_goals="Open another path through the vault infrastructure.",
        ),
    ]

    issues = validate_choice_menu(packet=packet, choices=choices)

    assert any("only allows 1 fresh branch" in issue for issue in issues)


def test_validate_scene_hooks_draft_rejects_unhooked_new_mystery_early(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "test_world.db"
    bootstrap_database(db_path)
    monkeypatch.setenv("CYOA_DB_PATH", str(db_path))

    state = NormalRunConversationState(
        scene_plan=ScenePlanDraft(
            scene_title="A Voice in the Vault",
            scene_summary="You hear an unseen voice answer from inside the sealed chamber.",
            material_change="A new mystery enters the scene through a disembodied reply.",
            opening_beat="discovery",
            location_status="same_location",
            scene_cast_mode="mc_only",
        ),
        scene_body=parse_scene_body_form(
            "\n".join(
                [
                    "SCENE_SETTINGS: NONE",
                    "SCENE_BODY: Narrator",
                    "You hold still at the seam.",
                    "An unseen voice answers from inside the sealed chamber and speaks your missing name.",
                ]
            )
        ),
        choices=[
            ChoiceDraft(
                choice_text="Call back to the unseen voice",
                choice_class="commitment",
                next_node="You answer the voice and force the hidden speaker to respond again.",
                further_goals="Turn the mystery into an immediate social pressure instead of leaving it ambient.",
            )
        ],
    )
    issues = validate_scene_hooks_draft(
        packet={
            "branch_key": "default",
            "context_summary": {"branch_depth": 3},
        },
        state=state,
        draft=SceneHooksDraft(
            hook_action="none",
            clue_tags=[],
            state_tags=[],
            global_direction_notes=[],
        ),
        resolution={
            "current_location_id": 1,
            "character_name_map": {},
            "protagonist_name": "The Tall Gnome",
            "protagonist_id": 1,
            "encountered_names": {"the tall gnome"},
        },
    )

    assert any("introduces an unresolved mystery/question without creating or extending a hook" in issue for issue in issues)


def test_run_story_worker_local_planning_mode_updates_notes_and_ideas(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier = client.get("/frontier").json()
    target_choice_id = frontier[0]["choice_id"]
    ideas_file = tmp_path / "IDEAS.md"
    log_file = tmp_path / "worker_log.ndjson"
    ideas_file.write_text("# Ideas Scratchpad\n\n## Open Ideas\n", encoding="utf-8")

    response_file = tmp_path / "planning_response.json"
    response_file.write_text(
        json.dumps(
            [
                {
                    "ideas_to_append": [
                        {
                            "category": "object",
                            "title": "Humming Receipt",
                            "note_text": "A tram receipt that hums when danger is imminent.",
                        },
                        {
                            "category": "location",
                            "title": "Needle Marsh Depot",
                            "note_text": "A reed-thick marsh depot where arrivals are logged on dragonfly wings.",
                        },
                        {
                            "category": "event",
                            "title": "Dragonfly Ledger Spill",
                            "note_text": "A route audit could go sideways when dragonfly-borne ledgers burst open over a crowded platform.",
                        },
                    ]
                },
                {
                    "choice_note_updates": [
                        {
                            "choice_id": target_choice_id,
                            "notes": "NEXT_NODE: push the branch toward a reusable tram-side mystery. FURTHER_GOALS: set up a later reed-marsh detour or a careful merge back if the beat stays small.",
                            "bound_idea": {
                                "title": "Needle Marsh Depot",
                                "category": "location",
                                "source": "fresh",
                                "steering_note": "This leaf can plausibly widen into the depot once the current tram-side mystery develops.",
                            },
                        }
                    ],
                    "story_direction_notes": [
                        {
                            "note_type": "plotline",
                            "title": "Needle Marsh Seed",
                            "note_text": "One tram branch could later open into Needle Marsh Depot where dragonflies carry route ledgers between platforms.",
                            "status": "active",
                            "priority": 2,
                        }
                    ],
                    "summary": "Planning pass completed.",
                },
            ]
        ),
        encoding="utf-8",
    )

    command = [
        sys.executable,
        "-m",
        "app.tools.run_story_worker_local",
        "--model",
        "mock-model",
        "--plan",
        "--ideas-file",
        str(ideas_file),
        "--log-file",
        str(log_file),
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "CYOA_DB_PATH": str(db_path),
            "CYOA_LOCAL_WORKER_RESPONSE_FILE": str(response_file),
        },
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["run_mode"] == "planning"
    assert payload["updated_choice_ids"] == [target_choice_id]
    assert payload["ideas_added"] == 3
    assert payload["story_notes_added"] == 1
    assert payload["ideas_appended"][0]["category"] == "object"
    assert payload["choice_note_updates"][0]["choice_id"] == target_choice_id
    assert "NEXT_NODE:" in payload["choice_note_updates"][0]["notes"]
    assert payload["choice_note_updates"][0]["bound_idea"]["title"] == "Needle Marsh Depot"
    assert payload["story_notes_created"][0]["title"] == "Needle Marsh Seed"

    updated_choice = client.get("/choices").json()
    matching = next(choice for choice in updated_choice if choice["id"] == target_choice_id)
    assert "NEXT_NODE:" in matching["notes"]
    assert matching["idea_binding"]["title"] == "Needle Marsh Depot"
    ideas_text = ideas_file.read_text(encoding="utf-8")
    assert "[Location] Needle Marsh Depot" in ideas_text
    story_notes = client.get("/story-notes").json()
    assert any(note["title"] == "Needle Marsh Seed" for note in story_notes)


def test_run_story_worker_local_planning_mode_rejects_duplicate_ideas(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    ideas_file = tmp_path / "IDEAS.md"
    log_file = tmp_path / "worker_log.ndjson"
    ideas_file.write_text(
        "# Ideas Scratchpad\n\n## Open Ideas\n\n- [Location] Bell Orchard: A branch could later open into a bell orchard where departures grow like fruit.\n",
        encoding="utf-8",
    )

    response_file = tmp_path / "planning_duplicate_response.json"
    response_file.write_text(
        json.dumps(
            [
                {
                    "ideas_to_append": [
                        {
                            "category": "location",
                            "title": "Bell Orchard",
                            "note_text": "A branch could later open into a bell orchard where departures grow like fruit.",
                        },
                        {
                            "category": "event",
                            "title": "Moss Toll",
                            "note_text": "A living toll gate could demand memories instead of coins.",
                        },
                        {
                            "category": "character",
                            "title": "Ledger Wren",
                            "note_text": "A meticulous bird clerk could follow the player across routes with increasingly personal paperwork.",
                        },
                    ]
                }
            ]
        ),
        encoding="utf-8",
    )

    command = [
        sys.executable,
        "-m",
        "app.tools.run_story_worker_local",
        "--model",
        "mock-model",
        "--plan",
        "--ideas-file",
        str(ideas_file),
        "--log-file",
        str(log_file),
        "--max-retries",
        "1",
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "CYOA_DB_PATH": str(db_path),
            "CYOA_LOCAL_WORKER_RESPONSE_FILE": str(response_file),
        },
    )

    assert result.returncode != 0
    assert "reuses a built-in example seed" in result.stderr or "reuses a built-in example seed" in result.stdout


def test_planning_idea_validation_rejects_same_motif_reskins() -> None:
    packet = {
        "planning_policy": {"ideas_per_run": 3},
        "ideas_file": {
            "current_content": "\n".join(
                [
                    "- [Location] The Bell Orchard: Bells grow like fruit in a hidden grove.",
                    "- [Character] Clerk Nettle's Archivist: A meticulous transit archivist.",
                ]
            ),
            "open_ideas": [
                {"category": "Location", "title": "The Bell Orchard", "note_text": "Bells grow like fruit in a hidden grove."},
                {"category": "Character", "title": "Clerk Nettle's Archivist", "note_text": "A meticulous transit archivist."},
            ],
        },
    }
    ideas = [
        PlanningIdea(
            category="location",
            title="The Bell Orchard Keeper",
            note_text="A hidden orchard keeper tends bells that grow like fruit and hum at anomaly frequencies.",
        ),
        PlanningIdea(
            category="character",
            title="The Silent Bellkeeper",
            note_text="A mute bellkeeper tends the orchard bells and listens for identity-frequency shifts.",
        ),
        PlanningIdea(
            category="event",
            title="Bell Frequency Sync",
            note_text="When an orchard bell rings at the right frequency, nearby records and memories briefly align.",
        ),
    ]

    issues = get_planning_idea_issues(packet, ideas)
    assert any("too close to existing idea" in issue or "too similar to existing idea" in issue for issue in issues)
    assert any("clustering around the same motif words" in issue for issue in issues)


def test_planning_idea_validation_requires_event() -> None:
    packet = {
        "planning_policy": {"ideas_per_run": 3},
        "ideas_file": {"current_content": "", "open_ideas": []},
    }
    ideas = [
        PlanningIdea(category="character", title="Parade Matron", note_text="A matron who measures routes by confetti weight."),
        PlanningIdea(category="location", title="Porcelain Switchyard", note_text="A switchyard of teacup rails under cracked chandeliers."),
        PlanningIdea(category="object", title="Receipt Kite", note_text="A stamped kite that only flies toward disputed arrivals."),
    ]

    issues = get_planning_idea_issues(packet, ideas)
    assert any("at least one event idea" in issue for issue in issues)


def test_planning_idea_validation_allows_distinct_poetic_titles() -> None:
    packet = {
        "planning_policy": {"ideas_per_run": 3},
        "ideas_file": {
            "current_content": """
## Open Ideas
- [Location] The City That Breathes in Reverse Humid Air: A metropolis where humidity rises at night.
- [Event] The Day the Stars Forgot How to Shine: A celestial event of stalled light.
""",
            "open_ideas": [
                {
                    "category": "Location",
                    "title": "The City That Breathes in Reverse Humid Air",
                    "note_text": "A metropolis where humidity rises at night.",
                },
                {
                    "category": "Event",
                    "title": "The Day the Stars Forgot How to Shine",
                    "note_text": "A celestial event of stalled light.",
                },
            ],
        },
    }
    ideas = [
        PlanningIdea(
            category="character",
            title="The Luminous Cartographer of Unwritten Horizons",
            note_text="A mapmaker who inks roads onto moth wings so travelers can only follow them at dusk.",
        ),
        PlanningIdea(
            category="location",
            title="The Library That Breathes With Forgotten Birthdays",
            note_text="A library whose shelves exhale cake-sweet dust whenever a lost anniversary nears.",
        ),
        PlanningIdea(
            category="event",
            title="The Day the Stars Forgot Their Names",
            note_text="For one night, constellations trade identities and every route-chart points somewhere slightly wrong.",
        ),
    ]

    issues = get_planning_idea_issues(packet, ideas)
    assert all("too close to existing idea" not in issue for issue in issues)
    assert all("too similar to existing idea" not in issue for issue in issues)


def test_prepare_story_run_surfaces_bound_idea_on_selected_frontier_item(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier = client.get("/frontier").json()
    choice_id = frontier[0]["choice_id"]
    update = client.post(
        f"/choices/{choice_id}",
        json={
            "notes": "NEXT_NODE: inspect the route marker. FURTHER_GOALS: widen this branch toward a strange depot encounter.",
            "idea_binding": {
                "title": "Porcelain Switchyard",
                "category": "location",
                "source": "existing",
                "steering_note": "This branch should eventually drift toward a fragile rail-yard reveal.",
            },
        },
    )
    assert update.status_code == 200

    command = [
        sys.executable,
        "-m",
        "app.tools.prepare_story_run",
        "--choice-id",
        str(choice_id),
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CYOA_DB_PATH": str(db_path), "CYOA_PLANNING_ROLL": "1.0"},
    )

    assert result.returncode == 0
    packet = json.loads(result.stdout)
    assert packet["run_mode"] == "normal"
    assert packet["selected_frontier_item"]["bound_idea"]["title"] == "Porcelain Switchyard"
    assert "strongest current medium-range steering signal" in packet["next_action"]


def test_normalize_generation_candidate_payload_repairs_common_local_model_shape_confusions() -> None:
    raw_payload = {
        "branch_key": "default",
        "scene_summary": "Summary",
        "scene_text": "Text",
        "choices": [
            {
                "choice_text": "Continue",
                "notes": "NEXT_NODE: continue the scene. FURTHER_GOALS: keep the branch moving.",
            }
        ],
        "entity_references": [
            {"entity_type": "character", "entity_id": 1, "role": "hero-center"}
        ],
        "scene_present_entities": [
            {"entity_type": "location", "entity_id": 3, "role": "current_scene"}
        ],
        "new_hooks": [
            {"hook_id": 12, "summary": "A recurring bell seems to know more than it should."}
        ],
        "asset_requests": [
            {
                "entity_type": "character",
                "entity_id": 2,
                "requested_asset_kinds": ["portrait", "cutout"],
            }
        ],
    }

    normalized = normalize_generation_candidate_payload(raw_payload)

    assert normalized["entity_references"] == [
        {"entity_type": "location", "entity_id": 3, "role": "current_scene"}
    ]
    assert normalized["scene_present_entities"] == [
        {"entity_type": "character", "entity_id": 1, "slot": "hero-center"}
    ]
    assert normalized["new_hooks"] == []
    assert normalized["hook_updates"] == [
        {"hook_id": 12, "status": "active", "progress_note": "A recurring bell seems to know more than it should."}
    ]
    assert normalized["asset_requests"] == [
        {
            "job_type": "generate_portrait",
            "asset_kind": "portrait",
            "entity_type": "character",
            "entity_id": 2,
        }
    ]


def test_parse_llm_result_uses_generation_candidate_normalizer_for_common_shape_errors() -> None:
    packet = {"run_mode": "normal"}
    raw_text = json.dumps(
        {
            "branch_key": "default",
            "scene_summary": "Summary",
            "scene_text": "Text",
            "choices": [
                {
                    "choice_text": "Continue",
                    "notes": "NEXT_NODE: continue the scene. FURTHER_GOALS: keep the branch moving.",
                }
            ],
            "entity_references": [
                {"entity_type": "character", "entity_id": 1, "role": "hero-center"}
            ],
            "scene_present_entities": [
                {"entity_type": "location", "entity_id": 3, "role": "current_scene"}
            ],
            "new_hooks": [
                {"hook_id": 12, "summary": "A recurring bell seems to know more than it should."}
            ],
            "asset_requests": [
                {
                    "entity_type": "character",
                    "entity_id": 2,
                    "requested_asset_kinds": ["portrait", "cutout"],
                }
            ],
        }
    )

    candidate = parse_llm_result(packet, raw_text)
    assert candidate.entity_references[0].entity_type == "location"
    assert candidate.entity_references[0].entity_id == 3
    assert candidate.entity_references[0].role == "current_scene"
    assert candidate.scene_present_entities[0].entity_type == "character"
    assert candidate.scene_present_entities[0].slot == "hero-center"
    assert candidate.hook_updates[0].hook_id == 12
    assert candidate.hook_updates[0].status == "active"
    assert candidate.asset_requests[0].job_type == "generate_portrait"
    assert candidate.asset_requests[0].asset_kind == "portrait"


def test_normalize_visible_generic_speakers_maps_to_single_obvious_named_character(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    with connect(db_path) as connection:
        canon = CanonResolver(connection)
        character = canon.create_or_get_character(
            name="Brass Patrol Member",
            description="A brass-armored survey officer with a severe stare.",
        )
        character_id = int(character["id"])

    candidate = parse_llm_result(
        {"run_mode": "normal"},
        json.dumps(
            {
                "branch_key": "default",
                "scene_summary": "A patrol voice cuts across the field.",
                "scene_text": "A brass patrol officer steps into the dew with a metallic creak.",
                "dialogue_lines": [
                    {"speaker": "Patrol Member", "text": "Hold still and keep your cursed hand where I can see it."}
                ],
                "entity_references": [
                    {"entity_type": "location", "entity_id": 1, "role": "current_scene"},
                    {"entity_type": "character", "entity_id": character_id, "role": "introduced"},
                ],
                "choices": [
                    {
                        "choice_text": "Raise your hand slowly",
                        "notes": "NEXT_NODE: answer the interruption directly. FURTHER_GOALS: prove obvious generic speaker labels can be normalized safely.",
                    }
                ],
            }
        ),
    )

    with contextlib.ExitStack() as stack:
        previous_db = os.environ.get("CYOA_DB_PATH")
        os.environ["CYOA_DB_PATH"] = str(db_path)
        stack.callback(lambda: os.environ.__setitem__("CYOA_DB_PATH", previous_db) if previous_db is not None else os.environ.pop("CYOA_DB_PATH", None))
        normalized = normalize_visible_generic_speakers(
            packet={"selected_frontier_item": {"from_node_id": 1}},
            candidate=candidate,
        )

    assert normalized.dialogue_lines[0].speaker == "Brass Patrol Member"


def test_repair_generation_candidate_prunes_safe_near_miss_shapes(tmp_path: Path) -> None:
    candidate = parse_llm_result(
        {"run_mode": "normal"},
        json.dumps(
            {
                "branch_key": "default",
                "scene_summary": "The patrol closes in at the lip of the vault.",
                "scene_text": "Metal steps echo nearby while the vault air tightens around the protagonist.",
                "choices": [
                    {
                        "choice_text": "Inspect the closing patrol shadow",
                        "notes": "NEXT_NODE: inspect the pressure more closely. FURTHER_GOALS: keep the pressure visible without changing course.",
                    },
                    {
                        "choice_text": "Inspect the closing patrol shadow",
                        "notes": "NEXT_NODE: inspect the pressure more closely. FURTHER_GOALS: keep the pressure visible without changing course.",
                    },
                ],
                "floating_character_introductions": [
                    {"character_id": 1, "intro_text": "The Tall Gnome steps forward."}
                ],
                "hook_updates": [
                    {"hook_id": 11, "status": "active", "progress_note": "The blocked mystery stirs again."}
                ],
            }
        ),
    )

    with contextlib.ExitStack() as stack:
        previous_db = os.environ.get("CYOA_DB_PATH")
        os.environ["CYOA_DB_PATH"] = str(tmp_path / "repair_test.db")
        stack.callback(lambda: os.environ.__setitem__("CYOA_DB_PATH", previous_db) if previous_db is not None else os.environ.pop("CYOA_DB_PATH", None))
        bootstrap_database(tmp_path / "repair_test.db")
        repaired = repair_generation_candidate(
            packet={
                "context_summary": {
                    "blocked_major_hooks": [{"id": 11}],
                    "blocked_major_developments": [],
                }
            },
            candidate=candidate,
        )

    assert repaired.floating_character_introductions == []
    assert repaired.hook_updates == []
    assert len(repaired.choices) == 1


def test_infer_missing_asset_requests_detects_current_scene_background(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/reset-opening-canon")
    seed = client.post("/story/seed-opening-story").json()
    node = next(row for row in client.get("/story-nodes").json() if row["id"] == seed["start_node_id"])

    inferred = infer_missing_asset_requests(
        node=node,
        explicit_requests=[],
        project_root=Path(__file__).resolve().parents[1],
        client=client,
    )

    background_request = next(
        request for request in inferred if request["asset_kind"] == "background" and request["entity_type"] == "location"
    )
    assert background_request["entity_id"] == 1
    assert "Mushroom Field" in background_request["prompt"]


def test_apply_generation_new_location_becomes_current_scene_for_inferred_background(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/reset-opening-canon")
    client.post("/story/seed-opening-story")
    frontier_item = client.get("/frontier").json()[0]

    response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": frontier_item["from_node_id"],
            "choice_id": frontier_item["choice_id"],
            "candidate": {
                "branch_key": "default",
                "scene_title": "Velvet Platform Arrival",
                "scene_summary": "You step down into a velvet-edged platform hidden beneath the field.",
                "scene_text": "You arrive at the velvet platform beneath the field and the air tastes like brass tea.",
                "choices": [
                    {
                        "choice_text": "Keep going",
                        "notes": "NEXT_NODE: continue along the velvet platform. FURTHER_GOALS: confirm the new location becomes the current scene for inferred art generation.",
                    }
                ],
                "new_locations": [
                    {
                        "name": "Velvet Platform",
                        "description": "A velvet-edged tram platform hidden beneath the mushroom field.",
                        "canonical_summary": "Warm lantern haze, stitched rail markings, and brass bells hanging above the tracks.",
                    }
                ],
            },
        },
    )

    assert response.status_code == 200
    created_node = next(row for row in client.get("/story-nodes").json() if row["id"] == response.json()["node"]["id"])
    current_scene = next(
        entity for entity in created_node["entities"]
        if entity["entity_type"] == "location" and entity["role"] == "current_scene"
    )

    inferred = infer_missing_asset_requests(
        node=created_node,
        explicit_requests=[],
        project_root=Path(__file__).resolve().parents[1],
        client=client,
    )

    background_request = next(
        request for request in inferred if request["asset_kind"] == "background" and request["entity_type"] == "location"
    )
    assert background_request["entity_id"] == int(current_scene["entity_id"])
    assert "Velvet Platform" in background_request["prompt"]
    assert "velvet-edged tram platform hidden beneath the mushroom field" in background_request["prompt"]


def test_infer_missing_asset_requests_skips_explicit_requests(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/reset-opening-canon")
    seed = client.post("/story/seed-opening-story").json()
    node = next(row for row in client.get("/story-nodes").json() if row["id"] == seed["start_node_id"])

    inferred = infer_missing_asset_requests(
        node=node,
        explicit_requests=[
            {"asset_kind": "background", "entity_type": "location", "entity_id": 1},
            {"asset_kind": "portrait", "entity_type": "character", "entity_id": 1},
        ],
        project_root=Path(__file__).resolve().parents[1],
        client=client,
    )

    assert all(request["asset_kind"] != "background" for request in inferred)
    assert all(not (request["asset_kind"] == "portrait" and request["entity_id"] == 1) for request in inferred)


def test_build_asset_prompt_for_location_ignores_scene_actor_text() -> None:
    prompt = build_asset_prompt(
        entity_type="location",
        entity={
            "name": "Velvet Platform",
            "description": "A velvet-edged tram platform hidden beneath the mushroom field.",
            "canonical_summary": "Brass bells, stitched rail markings, and warm lantern haze.",
        },
        scene_summary="Madam Bei points at the tram while the Tall Gnome hesitates.",
        scene_text="Madam Bei waves from beside the teacup tram.",
    )

    assert "Velvet Platform" in prompt
    assert "Madam Bei" not in prompt
    assert "Teacup Tram" not in prompt
    assert "No characters" in prompt


def test_validate_generation_rejects_duplicate_existing_asset_request(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/reset-opening-canon")
    client.post("/story/seed-opening-story")

    with connect(db_path) as connection:
        assets = AssetService(connection, Path(__file__).resolve().parents[1])
        background_path = tmp_path / "velvet-platform-bg.png"
        Image.new("RGB", (32, 32), color=(90, 110, 160)).save(background_path)
        assets.add_asset(
            entity_type="location",
            entity_id=1,
            asset_kind="background",
            file_path=str(background_path),
        )

    response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "A duplicate-art request test.",
            "scene_text": "The field waits for no replacement background.",
            "choices": [
                {
                    "choice_text": "Keep walking",
                    "notes": "NEXT_NODE: keep the scene valid. FURTHER_GOALS: test duplicate asset rejection without changing branch shape.",
                }
            ],
            "asset_requests": [
                {
                    "job_type": "generate_background",
                    "asset_kind": "background",
                    "entity_type": "location",
                    "entity_id": 1,
                    "prompt": "Mushroom Field at dawn.",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is False
    assert any("already exists" in issue for issue in payload["issues"])


def test_validate_generation_rejects_background_prompt_with_character_or_object_names(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    seed_response = client.post(
        "/seed-world",
        json={
            "locations": [{"name": "Velvet Platform"}],
            "characters": [{"name": "Madam Bei", "description": "A poised frog stationmaster."}],
            "objects": [{"name": "Teacup Tram", "description": "A jewel-bright tram shaped like a teacup."}],
        },
    )
    assert seed_response.status_code == 200

    response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "A new platform background is requested badly.",
            "scene_text": "The platform should be static scenery only.",
            "choices": [
                {
                    "choice_text": "Wait for a better prompt",
                    "notes": "NEXT_NODE: keep the scene valid. FURTHER_GOALS: prove background prompts cannot absorb separate actor and object assets.",
                }
            ],
            "new_characters": [
                {"name": "Madam Bei", "description": "A poised frog stationmaster."}
            ],
            "new_objects": [
                {"name": "Teacup Tram", "description": "A jewel-bright tram shaped like a teacup."}
            ],
            "asset_requests": [
                {
                    "job_type": "generate_background",
                    "asset_kind": "background",
                    "entity_type": "location",
                    "entity_id": 1,
                    "prompt": "Velvet Platform with Madam Bei standing by the Teacup Tram under lantern light.",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is False
    assert any("Background prompts must describe the static environment only" in issue for issue in payload["issues"])


def test_player_script_renders_choice_id_badge() -> None:
    player_js = (Path(__file__).resolve().parents[1] / "app" / "static" / "player.js").read_text(encoding="utf-8")
    assert "choice-id" in player_js
    assert 'id ${choice.id}' in player_js
    assert "choice-intent-debug-visible" in player_js
    assert "choice-intent-overlay" in player_js


def test_branch_hooks_endpoint_and_ui_show_readiness(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/reset-opening-canon")
    hook_response = client.post(
        "/branches/default/hooks",
        json={
            "hook_type": "minor_mystery",
            "importance": "minor",
            "summary": "A hidden bell under the mushroom seems to know your hat.",
            "linked_entity_type": "location",
            "linked_entity_id": 1,
            "min_distance_to_payoff": 2,
            "min_distance_to_next_development": 1,
            "required_clue_tags": ["bell-heard"],
        },
    )
    assert hook_response.status_code == 200

    hooks_response = client.get("/branches/default/hooks")
    assert hooks_response.status_code == 200
    hooks = hooks_response.json()
    assert len(hooks) >= 1
    assert "readiness" in hooks[0]
    assert "development_required_depth" in hooks[0]["readiness"]
    assert "remaining_development_distance" in hooks[0]["readiness"]

    ui_response = client.get("/ui/hooks")
    assert ui_response.status_code == 200
    assert "Branch hook pacing and payoff readiness." in ui_response.text
    assert "Min payoff distance" in ui_response.text
    assert "Min development distance" in ui_response.text


def test_ui_pages_render(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert "CYOA World Console" in response.text
    assert "Stuck Frontier Choices" in response.text
    seed_page = client.get("/ui/seed")
    assert seed_page.status_code == 200
    assert "Manual World Seeding" in seed_page.text
    objects_page = client.get("/ui/objects")
    assert objects_page.status_code == 200
    assert "Objects" in objects_page.text
    assets_page = client.get("/ui/assets")
    assert assets_page.status_code == 200
    assert "/assets/generate" in assets_page.text
    hooks_page = client.get("/ui/hooks")
    assert hooks_page.status_code == 200
    assert "Hooks" in hooks_page.text


def test_story_notes_endpoints_and_ui(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    create_response = client.post(
        "/story-notes",
        json={
            "note_type": "future_character",
            "title": "Future Goose Bandit",
            "note_text": "A goose-back bandit could later interrupt a tram route and become a recurring rival.",
            "source_branch_key": "default",
        },
    )
    assert create_response.status_code == 200
    note = create_response.json()
    assert note["title"] == "Future Goose Bandit"

    update_response = client.post(
        f"/story-notes/{note['id']}",
        json={"status": "parked"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["status"] == "parked"

    notes_response = client.get("/story-notes")
    assert notes_response.status_code == 200
    assert any(item["title"] == "Future Goose Bandit" for item in notes_response.json())

    ui_response = client.get("/ui/story-notes")
    assert ui_response.status_code == 200
    assert "Story Notes" in ui_response.text
    assert "Future Goose Bandit" in ui_response.text
    player_page = client.get("/play")
    assert player_page.status_code == 200
    assert "Restart Adventure" in player_page.text
    assert "Mushroom Field" in player_page.text
    assert "actors-layer" in player_page.text
    death_page = client.get("/play/death")
    assert death_page.status_code == 200
    assert "You Died" in death_page.text
    assert "Restart Adventure" in death_page.text


def test_home_console_renders_stuck_choice_details(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    choice_id = int(client.get("/frontier").json()[0]["choice_id"])

    with connect(db_path) as connection:
        story = StoryGraphService(connection)
        story.record_choice_worker_failure(choice_id=choice_id, error="Validation failed: repeated seam follow loop.")

    response = client.get("/")
    assert response.status_code == 200
    assert "Stuck Frontier Choices" in response.text
    assert "repeated seam follow loop" in response.text


def test_collect_scene_anchor_art_issues_flags_object_art_for_travel_scene() -> None:
    candidate = parse_llm_result(
        {"run_mode": "normal"},
        json.dumps(
            {
                "branch_key": "default",
                "scene_summary": "A portal opens toward Lantern Siding.",
                "scene_text": "The mirror spills light like a doorway into somewhere else.",
                "choices": [
                    {
                        "choice_text": "Keep going",
                        "notes": "NEXT_NODE: continue through the portal. FURTHER_GOALS: reach the next place cleanly.",
                    }
                ],
                "asset_requests": [
                    {
                        "job_type": "generate_object",
                        "asset_kind": "object_render",
                        "entity_type": "object",
                        "entity_id": 1,
                        "prompt": "A mirror portal object.",
                    }
                ],
            }
        ),
    )

    issues = collect_scene_anchor_art_issues(
        packet={
            "selected_frontier_item": {
                "choice_text": "Step through the mirror portal into Lantern Siding"
            }
        },
        candidate=candidate,
    )

    assert any("Do not use object_render as a stand-in for a scene background" in issue for issue in issues)


def test_collect_redundant_progression_issues_flags_repeated_parent_choice_and_summary() -> None:
    candidate = parse_llm_result(
        {"run_mode": "normal"},
        json.dumps(
            {
                "branch_key": "default",
                "scene_summary": "The tall gnome wakes in the Mushroom Field just before a brass survey patrol crosses it.",
                "scene_text": "The next scene should have advanced, but this one stalls.",
                "choices": [
                    {
                        "choice_text": "Trace the counting-wires to the green glass seam",
                        "notes": "NEXT_NODE: find where the wires lead before patrol arrives. FURTHER_GOALS: open buried-glass registry storyline and test altered hand.",
                    }
                ],
            }
        ),
    )

    issues = collect_redundant_progression_issues(
        packet={
            "selected_frontier_item": {
                "choice_text": "Trace the counting-wires to the green glass seam",
                "existing_choice_notes": "NEXT_NODE: find where the wires lead before patrol arrives. FURTHER_GOALS: open buried-glass registry storyline and test altered hand.",
            },
            "context_summary": {
                "current_node": {
                    "summary": "The tall gnome wakes in the Mushroom Field just before a brass survey patrol crosses it."
                }
            },
        },
        candidate=candidate,
    )

    assert any("too closely repeats the just-taken choice" in issue for issue in issues)
    assert any("too closely repeats the parent scene summary" in issue for issue in issues)


def test_collect_ungrounded_local_prop_issues_flags_menu_only_prop() -> None:
    candidate = parse_llm_result(
        {"run_mode": "normal"},
        json.dumps(
            {
                "branch_key": "default",
                "scene_summary": "The seam glows brighter under the altered hand.",
                "scene_text": "The green glass seam pulses when touched and the wires hum beneath the roots.",
                "choices": [
                    {
                        "choice_text": "Step around the velvet knot",
                        "notes": "NEXT_NODE: inspect the marker from another angle. FURTHER_GOALS: widen the local branch with a clue-first alternative.",
                    }
                ],
            }
        ),
    )

    issues = collect_ungrounded_local_prop_issues(
        packet={
            "selected_frontier_item": {
                "choice_text": "Trace the counting-wires to the green glass seam",
                "existing_choice_notes": "NEXT_NODE: find where the wires lead before patrol arrives. FURTHER_GOALS: open buried-glass registry storyline and test altered hand.",
            },
            "context_summary": {
                "current_node": {
                    "title": "The Counting Bell",
                    "summary": "The wires twitch beneath the field and a green seam glows in the soil.",
                }
            },
        },
        candidate=candidate,
    )

    assert any("introduces a new focal prop or marker" in issue for issue in issues)


def test_collect_ungrounded_local_prop_issues_allows_grounded_seam_phrase() -> None:
    candidate = parse_llm_result(
        {"run_mode": "normal"},
        json.dumps(
            {
                "branch_key": "default",
                "scene_summary": "The cracked seam glows brighter under the altered hand.",
                "scene_text": "A cracked seam of green glass runs beneath the roots while the counting wires hum beside it.",
                "choices": [
                    {
                        "choice_text": "Examine the cracked seam for clues.",
                        "notes": "NEXT_NODE: inspect the damaged seam closely. FURTHER_GOALS: gather a clue without changing location or inventing a new prop.",
                    }
                ],
            }
        ),
    )

    issues = collect_ungrounded_local_prop_issues(
        packet={
            "selected_frontier_item": {
                "choice_text": "Follow the pulse toward the humming wires",
                "existing_choice_notes": "NEXT_NODE: trace the glowing seam to understand its pattern. FURTHER_GOALS: open a route-discovery path that may lead to hidden glyphs.",
            },
            "context_summary": {
                "current_node": {
                    "title": "The Brass Toll Echoes",
                    "summary": "A distant brass toll echoes across the field as dawn lifts the dew, brightening the cracked seam that pulses when touched.",
                }
            },
        },
        candidate=candidate,
    )

    assert issues == []


def test_parse_and_validate_transition_node_form() -> None:
    packet = {
        "context_summary": {
            "merge_candidates": [
                {"node_id": 7, "title": "Silver Tracks", "summary": "The silver grooves lead toward the marked mushroom."}
            ]
        }
    }
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: Bridge Setup",
                "SCENE_SUMMARY: A merge choice needs a bridge beat.",
                "MATERIAL_CHANGE: The scene now routes back into an older lane through a connective passage.",
                "OPENING_BEAT: transition",
                "LOCATION_STATUS: same_location",
                "SCENE_CAST: MC_ONLY",
                "NEW_CHARACTERS: NONE",
                "NEW_LOCATION: NONE",
                "NEW_CHARACTER_INTRO: NONE",
                "NEW_LOCATION_INTRO: NONE",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "Narrator",
                "You find the turn that will let this branch fold back into the older trail.",
            ]
        )
    )
    state = NormalRunConversationState(
        scene_plan=scene_plan,
        scene_body=scene_body,
        choices=[
            ChoiceDraft(
                choice_text="Rejoin the silver tracks",
                choice_class="progress",
                next_node="You slip back toward the marked trail and pick up its older momentum.",
                further_goals="Reconnect to the established investigation lane without a jarring jump.",
                target_existing_node=7,
            )
        ],
    )
    transition = parse_transition_node_form(
        raw_text="\n".join(
            [
                "TRANSITION_TITLE: Back to the Grooves",
                "TRANSITION_SUMMARY: You climb down and thread through the roots until the old silver grooves come back into view.",
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "You climb carefully down the damp stalk, keeping the patrol's attention above you until the ground rises to meet your boots again.",
                "The silver grooves reappear between the mossy roots, and you angle toward them just as the older trail's pressure closes around you once more.",
            ]
        ),
        choice_index=0,
        target_existing_node=7,
    )
    resolution = {
        "protagonist_id": 1,
        "protagonist_name": "The Tall Gnome",
        "character_name_map": {},
        "character_id_map": {},
        "encountered_names": {"the tall gnome"},
    }

    issues = validate_transition_node_draft(
        packet=packet,
        state=state,
        draft=transition,
        resolution=resolution,
    )

    assert issues == []


def test_validate_transition_node_draft_does_not_inherit_scene_level_new_character_pressure() -> None:
    packet = {
        "new_character_pressure": {"active": True},
        "context_summary": {
            "merge_candidates": [
                {
                    "node_id": 7,
                    "title": "Sealed Vault",
                    "summary": "The old vault waits with its humming wall intact.",
                }
            ]
        },
    }
    scene_plan = parse_scene_plan_form(
        "\n".join(
            [
                "SCENE_TITLE: The Resonance Junction",
                "SCENE_SUMMARY: A new authority figure appears in a new chamber.",
                "MATERIAL_CHANGE: The branch reaches a volatile junction and declares a new technician.",
                "OPENING_BEAT: discovery",
                "LOCATION_STATUS: new_location",
                "SCENE_CAST: 1, 2",
                "NEW_CHARACTERS: The Chrono-Technician",
                "NEW_LOCATION: Resonance Junction",
                "NEW_CHARACTER_INTRO: The Chrono-Technician emerges from a maintenance conduit.",
                "NEW_LOCATION_INTRO: You step through the seam into a humming chamber of brass and memory light.",
            ]
        )
    )
    scene_body = parse_scene_body_form(
        "\n".join(
            [
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "You enter the junction while the patrol watches the glowing vault wall.",
            ]
        )
    )
    state = NormalRunConversationState(
        scene_plan=scene_plan,
        scene_body=scene_body,
        choices=[
            ChoiceDraft(
                choice_text="Question the patrol and move back toward the vault",
                choice_class="progress",
                next_node="The patrol reacts badly to your question, forcing a tense return toward the vault wall.",
                further_goals="Reconnect to the established vault lane without an abrupt teleport.",
                target_existing_node=7,
            )
        ],
    )
    transition = parse_transition_node_form(
        raw_text="\n".join(
            [
                "TRANSITION_TITLE: Back to the Vault Wall",
                "TRANSITION_SUMMARY: The patrol's alarm drives you back across the junction until the sealed vault takes over the whole field of action again.",
                "SCENE_SETTINGS: NONE",
                "SCENE_BODY: Narrator",
                "The patrol's warning cuts through the chamber and forces you back along the live conduits toward the sealed wall.",
                "The deeper machinery falls away behind you until the older vault pressure closes around the scene once more.",
            ]
        ),
        choice_index=0,
        target_existing_node=7,
    )
    resolution = {
        "protagonist_id": 1,
        "protagonist_name": "The Tall Gnome",
        "character_name_map": {
            "the tall gnome": {"id": 1, "name": "The Tall Gnome"},
            "brass patrol member": {"id": 2, "name": "Brass Patrol Member"},
        },
        "character_id_map": {
            1: {"id": 1, "name": "The Tall Gnome"},
            2: {"id": 2, "name": "Brass Patrol Member"},
        },
        "encountered_names": {"the tall gnome", "brass patrol member"},
    }

    issues = validate_transition_node_draft(
        packet=packet,
        state=state,
        draft=transition,
        resolution=resolution,
    )

    assert not any("New-character pressure is active" in issue for issue in issues)


def test_prune_existing_asset_requests_drops_already_available_art() -> None:
    candidate = parse_llm_result(
        {"run_mode": "normal"},
        json.dumps(
            {
                "branch_key": "default",
                "scene_summary": "The protagonist remains on screen.",
                "scene_text": "The scene does not need new portrait art.",
                "choices": [
                    {
                        "choice_text": "Keep going",
                        "notes": "NEXT_NODE: continue the branch. FURTHER_GOALS: prove duplicate art requests can be ignored safely.",
                    }
                ],
                "asset_requests": [
                    {
                        "job_type": "generate_portrait",
                        "asset_kind": "portrait",
                        "entity_type": "character",
                        "entity_id": 1,
                    }
                ],
            }
        ),
    )

    pruned = prune_existing_asset_requests(
        packet={
            "asset_availability": [
                {
                    "entity_type": "character",
                    "entity_id": 1,
                    "name": "The Tall Gnome",
                    "available_asset_kinds": ["portrait", "cutout"],
                }
            ]
        },
        candidate=candidate,
    )

    assert pruned.asset_requests == []


def test_generation_validation_rejects_non_location_current_scene(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/reset-opening-canon")

    validation_response = client.post(
        "/jobs/validate-generation",
        json={
            "branch_key": "default",
            "scene_summary": "An object is incorrectly used as the scene anchor.",
            "scene_text": "The teacup tram is treated like the whole backdrop instead of a staged object.",
            "entity_references": [
                {"entity_type": "object", "entity_id": 1, "role": "current_scene"},
            ],
            "choices": [
                {
                    "choice_text": "Step around the tram",
                    "notes": "NEXT_NODE: inspect the mistaken scene anchor. FURTHER_GOALS: prove object assets must not drive the background layer.",
                }
            ],
        },
    )

    assert validation_response.status_code == 200
    result = validation_response.json()
    assert result["valid"] is False
    assert any("current_scene must always reference a location" in issue for issue in result["issues"])


def test_apply_generation_rejects_non_location_current_scene(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    apply_response = client.post(
        "/jobs/apply-generation",
        json={
            "branch_key": "default",
            "parent_node_id": 1,
            "choice_id": 1,
            "candidate": {
                "branch_key": "default",
                "scene_title": "Bad Scene Anchor Fallback",
                "scene_summary": "A malformed current_scene reference should not suppress parent location inheritance.",
                "scene_text": "The tram looms large, but the mushroom field is still the actual place around it.",
                "entity_references": [
                    {"entity_type": "object", "entity_id": 1, "role": "current_scene"}
                ],
                "choices": [
                    {
                        "choice_text": "Keep walking",
                        "notes": "NEXT_NODE: continue through the malformed scene. FURTHER_GOALS: confirm parent location inheritance stays safe.",
                    }
                ],
            },
        },
    )

    assert apply_response.status_code == 400
    detail = apply_response.json().get("detail", {})
    validation = detail.get("validation", {}) if isinstance(detail, dict) else {}
    issues = validation.get("issues", []) if isinstance(validation, dict) else []
    assert any("current_scene must always reference a location" in issue for issue in issues)


def test_codex_human_strip_markdown_fences_handles_code_block() -> None:
    assert strip_markdown_fences("```text\nSCENE_TITLE: Hello\n```") == "SCENE_TITLE: Hello"


def test_codex_human_extract_thread_id_from_jsonl_finds_started_thread() -> None:
    stdout_text = '\n'.join(
        [
            '{"type":"thread.started","thread_id":"thread_abc123"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"SCENE_TITLE: Hello"}}',
        ]
    )

    assert extract_thread_id_from_jsonl(stdout_text) == "thread_abc123"


def test_codex_human_extract_last_agent_message_from_jsonl_reads_latest_agent_message() -> None:
    stdout_text = '\n'.join(
        [
            '{"type":"thread.started","thread_id":"thread_abc123"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"SCENE_TITLE: First"}}',
            '{"type":"item.completed","item":{"type":"tool_result","text":"ignore me"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"SCENE_TITLE: Final"}}',
        ]
    )

    assert extract_last_agent_message_from_jsonl(stdout_text) == "SCENE_TITLE: Final"


def test_codex_human_build_prompt_warns_not_json() -> None:
    prompt = build_codex_step_prompt("SCENE_TITLE: <string>")

    assert "This is NOT JSON." in prompt
    assert "Return only the exact fields" in prompt


def test_codex_human_build_worker_command_forces_human_mode() -> None:
    command = build_worker_command(worker_model="placeholder-model", passthrough_args=["--dry-run"])

    assert command[:7] == [
        sys.executable,
        "-m",
        "app.tools.run_story_worker_local",
        "--author-mode",
        "human",
        "--model",
        "placeholder-model",
    ]
    assert command[-1] == "--dry-run"


def test_codex_human_request_nonempty_step_retries_blank_outputs(monkeypatch, tmp_path: Path) -> None:
    responses = [("", "thread_1"), ("", "thread_1"), ("SCENE_TITLE: Recovered", "thread_1")]

    def fake_run_codex_step(**kwargs):
        return responses.pop(0)

    monkeypatch.setattr("app.tools.run_story_worker_codex_human.run_codex_step", fake_run_codex_step)

    response_text, thread_id = request_nonempty_codex_step(
        codex_command="codex.cmd",
        codex_model=None,
        project_root=tmp_path,
        prompt_text="SCENE_TITLE: <string>",
        thread_id=None,
        empty_retry_limit=3,
    )

    assert response_text == "SCENE_TITLE: Recovered"
    assert thread_id == "thread_1"

