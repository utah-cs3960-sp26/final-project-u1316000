from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.database import connect
from app.main import create_app
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


def test_duplicate_location_is_not_recreated(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    client.post("/seed-world", json={"locations": [{"name": "Cabin"}]})
    client.post("/seed-world", json={"locations": [{"name": "Cabin"}]})
    locations = client.get("/locations").json()
    assert len(locations) == 1


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
    assert "/assets/request" in assets_page.text
    player_page = client.get("/play")
    assert player_page.status_code == 200
    assert "Restart Adventure" in player_page.text
    assert "Mushroom Field" in player_page.text
