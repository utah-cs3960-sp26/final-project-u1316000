from __future__ import annotations

import contextlib
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
from app.services.story_graph import StoryGraphService


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
    assert hat_hook["min_distance_to_next_development"] == 4
    assert body_hook["min_distance_to_next_development"] == 4
    assert hat_hook["introduced_at_depth"] == 0
    assert body_hook["introduced_at_depth"] == 0
    assert "missing past" in (hat_hook["payoff_concept"] or "").lower()
    assert hat_hook["must_not_imply"]
    assert "deliberate intervention" in (body_hook["payoff_concept"] or "").lower()
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
    assert "missing past" in (hat_hook["payoff_concept"] or "").lower()
    assert any("tram uniform gear" in guardrail.lower() for guardrail in hat_hook["must_not_imply"])


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
            "choices": [{"choice_text": "Keep going", "notes": "Goal: continue after the reveal. Intent: move the branch onward after a supposed payoff."}],
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
                    "notes": "Goal: immediately press the same clue again. Intent: force another development before the cooldown expires.",
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
            "choices": [{"choice_text": "Step closer", "notes": "Goal: approach the speaker. Intent: deepen the new stalk-side mystery."}],
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

    assert validation_response.status_code == 200
    result = validation_response.json()
    assert result["valid"] is False
    assert any("Goal and Intent" in issue for issue in result["issues"])


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
            "choices": [{"choice_text": "Step closer", "notes": "Goal: approach the speaker. Intent: keep the voice mystery alive as a true hook."}],
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
                    "notes": "Goal: call an emergency ride. Intent: use an unlocked affordance to open a traversal branch.",
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
                    "notes": "Goal: call a goose from nowhere. Intent: shortcut the story with an unavailable affordance.",
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
                    "notes": "Goal: fold back into the main clue trail. Intent: test whether over-merged branches are forced to diverge.",
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
                        "notes": "Goal: test whether the mushroom answers. Intent: open a fresh mystery path at the marked stem.",
                    },
                    {
                        "choice_text": "Circle around the velvet knot",
                        "notes": "Goal: inspect the marker from another angle. Intent: widen the local branch with a clue-focused alternative.",
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
                        "notes": "Goal: keep the same scene moving. Intent: confirm inherited staging keeps the same visual context when metadata is omitted.",
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
                        "notes": "Goal: return to the main clue trail. Intent: quick-merge this minor omen back into the silver-track line.",
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
                        "notes": "Goal: summon a nonexistent goose. Intent: force a branch through an unavailable affordance.",
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
                    "notes": "Goal: listen to the new clerk. Intent: open a fresh recurring bureaucratic character thread.",
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
                        "notes": "Goal: meet the new clerk. Intent: open a fresh recurring character thread with a bureaucratic angle.",
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


def test_play_story_payload_exposes_choice_intent(tmp_path: Path) -> None:
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
                "scene_title": "Intent Test Scene",
                "scene_summary": "A scene used to confirm intent text reaches the player payload.",
                "scene_text": "A clear little branch for testing.",
                "choices": [
                    {
                        "choice_text": "Take the careful route",
                        "notes": "Goal: choose the safer branch. Intent: keep the mushroom-field thread alive while opening a cautious follow-up path.",
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
    assert choice["intent"] == "keep the mushroom-field thread alive while opening a cautious follow-up path."


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
    client.post(
        "/story-notes",
        json={
            "note_type": "plotline",
            "title": "Transit Trouble Seed",
            "note_text": "A later tram route could erupt into a transit crisis.",
        },
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
    assert packet["preview_payload"]["branch_key"] == "default"
    assert packet["context_summary"]["branch_key"] == "default"
    assert "focus_canon_slice" in packet
    assert "validation_checklist" in packet
    assert "candidate_template" in packet
    assert "endpoint_contract" in packet
    assert "full_context" not in packet
    assert "eligible_major_hooks" in packet["context_summary"]
    assert "blocked_major_hooks" in packet["context_summary"]
    assert packet["context_summary"]["global_direction_notes"][0]["title"] == "Transit Trouble Seed"
    assert "branch_shape" in packet["context_summary"]
    assert "global_direction_notes" in packet["candidate_template"]
    assert packet["next_action"].startswith("Run now. Do not ask the human for permission.")
    assert "choice id" in packet["next_action"].lower()
    assert packet["planning_policy"]["chance"] == 0.25
    assert packet["runtime_state_after"]["normal_runs_since_plan"] == 1


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


def test_run_story_worker_local_normal_dry_run_with_mock_response(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")

    response_file = tmp_path / "normal_response.json"
    response_file.write_text(
        json.dumps(
            {
                "branch_key": "default",
                "scene_title": "Mock Loop Scene",
                "scene_summary": "A valid mocked scene candidate.",
                "scene_text": "The mocked local worker produces a scene without touching the database in dry-run mode.",
                "choices": [
                    {
                        "choice_text": "Take the mocked path",
                        "notes": "Goal: confirm the local runner can validate a mocked scene. Intent: prove the CLI works before real model calls.",
                    },
                    {
                        "choice_text": "Stay put and inspect the result",
                        "notes": "Goal: keep the branch stable while testing. Intent: preserve a second valid option for schema coverage.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    command = [
        sys.executable,
        "-m",
        "app.tools.run_story_worker_local",
        "--model",
        "mock-model",
        "--dry-run",
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

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["run_mode"] == "normal"
    assert payload["dry_run"] is True
    assert payload["expanded_choice_id"] if "expanded_choice_id" in payload else payload["choice_id"]
    assert payload["validation"]["valid"] is True


def test_run_story_worker_local_planning_mode_updates_notes_and_ideas(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post("/story/seed-opening-story")
    frontier = client.get("/frontier").json()
    target_choice_id = frontier[0]["choice_id"]
    ideas_file = tmp_path / "IDEAS.md"
    ideas_file.write_text("# Ideas Scratchpad\n\n## Open Ideas\n", encoding="utf-8")

    response_file = tmp_path / "planning_response.json"
    response_file.write_text(
        json.dumps(
            {
                "ideas_to_append": [
                    "A tram receipt that hums when danger is imminent.",
                    "A bell orchard where departures ripen on trees.",
                    "A duck inspector who only trusts counterfeit maps.",
                ],
                "choice_note_updates": [
                    {
                        "choice_id": target_choice_id,
                        "notes": "Goal: push the branch toward a reusable tram-side mystery. Intent: set up a later bell-orchard detour or a careful merge back if the beat stays small.",
                    }
                ],
                "story_direction_notes": [
                    {
                        "note_type": "plotline",
                        "title": "Bell Orchard Seed",
                        "note_text": "One tram branch could later open into a bell orchard where departures grow like fruit.",
                        "status": "active",
                        "priority": 2,
                    }
                ],
                "summary": "Planning pass completed.",
            }
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

    updated_choice = client.get("/choices").json()
    matching = next(choice for choice in updated_choice if choice["id"] == target_choice_id)
    assert "Goal:" in matching["notes"]
    ideas_text = ideas_file.read_text(encoding="utf-8")
    assert "bell orchard" in ideas_text.lower()
    story_notes = client.get("/story-notes").json()
    assert any(note["title"] == "Bell Orchard Seed" for note in story_notes)


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
