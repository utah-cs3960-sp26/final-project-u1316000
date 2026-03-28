from __future__ import annotations

from pathlib import Path

from PIL import Image
from fastapi.testclient import TestClient

from app.database import bootstrap_database, connect
from app.main import create_app
from app.services.assets import AssetService
from app.services.canon import CanonResolver


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
        "choices",
        "node_entities",
        "assets",
        "generation_jobs",
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

    def fake_remove_background(self, *, source_image_path, output_name=None, model_repo="briaai/RMBG-2.0", device="auto"):
        cutout = tmp_path / "cutouts" / (output_name or "portrait-cutout.png")
        cutout.parent.mkdir(parents=True, exist_ok=True)
        cutout.write_bytes(Path(source_image_path).read_bytes())
        return str(cutout)

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


def test_play_resolves_asset_backed_scene_media(tmp_path: Path) -> None:
    client, db_path = build_client(tmp_path)
    client.post(
        "/seed-world",
        json={
            "locations": [{"name": "Mushroom Field"}],
            "characters": [{"name": "The Tall Gnome"}],
        },
    )

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
    finally:
        if background_path.exists():
            background_path.unlink()
        if cutout_path.exists():
            cutout_path.unlink()
        try:
            fixture_dir.rmdir()
        except OSError:
            pass


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
    player_page = client.get("/play")
    assert player_page.status_code == 200
    assert "Restart Adventure" in player_page.text
    assert "Mushroom Field" in player_page.text
    assert "actors-layer" in player_page.text
