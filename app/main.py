from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.database import bootstrap_database, connect
from app.models import (
    AssetGenerateRequest,
    AssetRequest,
    BackgroundRemovalRequest,
    ChoiceCreate,
    GenerationPayload,
    StoryNodeCreate,
    WorldSeed,
)
from app.services.assets import AssetService
from app.services.canon import CanonResolver
from app.services.generation import LLMGenerationService
from app.services.story_graph import StoryGraphService


PLAYER_DEMO_STORY: dict[str, Any] = {
    "title": "The Tall Gnome Awakens",
    "start_scene": "opening",
    "scenes": {
        "opening": {
            "location": "Mushroom Field",
            "location_entity_id": 1,
            "present_entities": [
                {
                    "entity_type": "character",
                    "entity_id": 1,
                    "slot": "hero-center",
                    "focus": True,
                    "scale": 1.16,
                    "use_player_fallback": True,
                }
            ],
            "lines": [
                {
                    "speaker": "Narrator",
                    "text": "Cold dew clings to your coat as you wake in a field of larger-than-life mushrooms, their pale caps towering overhead like quiet moons hung on crooked stems.",
                },
                {
                    "speaker": "You",
                    "text": "You push yourself upright and discover your body is wrong in a very specific way: you are still unmistakably a gnome, but stretched to the size of a human.",
                },
                {
                    "speaker": "Narrator",
                    "text": "The memory of how you arrived here refuses to surface. There is only a raw blankness behind your eyes, like a torn page where a name should be.",
                },
                {
                    "speaker": "Narrator",
                    "text": "Your normal hat is gone. In its place sits a black tophat beaded with mist, wrapped in a silver ribbon stitched with symbols that feel insultingly familiar.",
                },
                {
                    "speaker": "Narrator",
                    "text": "You lift your left hand toward the dawn and freeze. Five thumbs stare back at you, flexing with eerie coordination, as if they have always belonged there.",
                },
                {
                    "speaker": "Narrator",
                    "text": "Beyond the mushroom trunks, something metallic taps three careful beats. In the grass nearby, silver tracks, your strange hat, and your impossible hand all seem to demand attention at once.",
                },
            ],
            "choices": [
                {
                    "label": "Examine the silver tracks in the grass",
                    "target": "tracks",
                },
                {
                    "label": "Inspect the tophat and its silver ribbon",
                    "target": "hat",
                },
                {
                    "label": "Study your five-thumbed left hand",
                    "target": "hand",
                },
            ],
        },
        "tracks": {
            "location": "Mushroom Field",
            "location_entity_id": 1,
            "present_entities": [
                {
                    "entity_type": "character",
                    "entity_id": 1,
                    "slot": "hero-center",
                    "focus": True,
                    "scale": 1.16,
                    "use_player_fallback": True,
                }
            ],
            "lines": [
                {
                    "speaker": "Narrator",
                    "text": "You kneel beside the silver tracks and find they are not footprints at all, but two narrow grooves pressed into the earth as though a tiny carriage had rolled through the field with no horse to pull it.",
                },
                {
                    "speaker": "Narrator",
                    "text": "The grooves stop beneath the largest mushroom in sight, where someone has tied a strip of velvet to the stem at exactly your eye level.",
                },
                {
                    "speaker": "You",
                    "text": "You do not remember leaving it there, but the knot is one your hands know how to tie.",
                },
            ],
        },
        "hat": {
            "location": "Mushroom Field",
            "location_entity_id": 1,
            "present_entities": [
                {
                    "entity_type": "character",
                    "entity_id": 1,
                    "slot": "hero-center",
                    "focus": True,
                    "scale": 1.16,
                    "use_player_fallback": True,
                }
            ],
            "lines": [
                {
                    "speaker": "Narrator",
                    "text": "The tophat fits too well. Inside the brim, the silver ribbon has been stitched through with tiny letters: NOT YOUR FIRST NAME.",
                },
                {
                    "speaker": "Narrator",
                    "text": "Tucked in the band is a pressed violet and a sliver of mirror. When you angle the mirror just right, a second version of your face seems to blink a fraction too late.",
                },
                {
                    "speaker": "You",
                    "text": "Whoever replaced your old hat knew exactly what would frighten you and exactly what would make you keep walking.",
                },
            ],
        },
        "hand": {
            "location": "Mushroom Field",
            "location_entity_id": 1,
            "present_entities": [
                {
                    "entity_type": "character",
                    "entity_id": 1,
                    "slot": "hero-center",
                    "focus": True,
                    "scale": 1.16,
                    "use_player_fallback": True,
                }
            ],
            "lines": [
                {
                    "speaker": "Narrator",
                    "text": "You spread your left hand and watch the five thumbs curl inward in sequence, each one stopping as if it were matching a rhythm your head can almost hear.",
                },
                {
                    "speaker": "Narrator",
                    "text": "In the wet soil, you find a half-buried stone plate shaped with five thumb-sized hollows. It looks less like a warning than a lock waiting for you to remember the key.",
                },
                {
                    "speaker": "You",
                    "text": "Whatever happened to you, it did not happen by accident.",
                },
            ],
        },
    },
}


def get_db(request: Request) -> sqlite3.Connection:
    database_path = request.app.state.settings.database_path
    connection = connect(database_path)
    try:
        yield connection
    finally:
        connection.close()


def create_app(database_path: str | Path | None = None) -> FastAPI:
    settings = Settings.from_env(database_path)
    bootstrap_database(settings.database_path)
    project_root = Path(__file__).resolve().parent.parent
    asset_root = project_root / "data" / "assets"

    app = FastAPI(title="CYOA Prototype")
    app.state.settings = settings
    app.state.project_root = project_root
    app.state.llm_generation = LLMGenerationService()

    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
    app.mount("/media", StaticFiles(directory=str(asset_root)), name="media")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, db: sqlite3.Connection = Depends(get_db)) -> HTMLResponse:
        canon = CanonResolver(db)
        story = StoryGraphService(db)
        assets = AssetService(db, project_root)
        context = {
            "request": request,
            "counts": story.counts(),
            "locations": canon.list_locations()[:5],
            "characters": canon.list_characters()[:5],
            "objects": canon.list_objects()[:5],
            "assets": assets.list_assets()[:5],
            "nodes": story.list_story_nodes()[:5],
            "jobs": story.list_jobs()[:5],
            "db_path": str(settings.database_path),
        }
        return templates.TemplateResponse(request, "index.html", context)

    @app.get("/play", response_class=HTMLResponse)
    def player_view(request: Request, db: sqlite3.Connection = Depends(get_db)) -> HTMLResponse:
        assets = AssetService(db, project_root)
        resolved_story = {
            **PLAYER_DEMO_STORY,
            "scenes": {
                scene_key: assets.resolve_scene_assets(scene_definition)
                for scene_key, scene_definition in PLAYER_DEMO_STORY["scenes"].items()
            },
        }
        return templates.TemplateResponse(
            request,
            "player.html",
            {
                "request": request,
                "story_data": resolved_story,
                "title": PLAYER_DEMO_STORY["title"],
            },
        )

    @app.get("/ui/seed", response_class=HTMLResponse)
    def seed_page(request: Request, db: sqlite3.Connection = Depends(get_db)) -> HTMLResponse:
        canon = CanonResolver(db)
        context = {
            "request": request,
            "locations": canon.list_locations(),
            "characters": canon.list_characters(),
            "objects": canon.list_objects(),
            "relations": canon.list_relations(),
            "facts": canon.list_facts(),
        }
        return templates.TemplateResponse(request, "seed.html", context)

    @app.post("/ui/seed/location")
    def seed_location(
        name: str = Form(...),
        description: str = Form(""),
        canonical_summary: str = Form(""),
        db: sqlite3.Connection = Depends(get_db),
    ) -> RedirectResponse:
        canon = CanonResolver(db)
        canon.create_or_get_location(
            name=name,
            description=description or None,
            canonical_summary=canonical_summary or None,
        )
        return RedirectResponse("/ui/seed", status_code=303)

    @app.post("/ui/seed/character")
    def seed_character(
        name: str = Form(...),
        description: str = Form(""),
        canonical_summary: str = Form(""),
        home_location_name: str = Form(""),
        db: sqlite3.Connection = Depends(get_db),
    ) -> RedirectResponse:
        canon = CanonResolver(db)
        home_location_id = None
        if home_location_name.strip():
            home_location = canon.create_or_get_location(name=home_location_name.strip())
            home_location_id = int(home_location["id"])
        canon.create_or_get_character(
            name=name,
            description=description or None,
            canonical_summary=canonical_summary or None,
            home_location_id=home_location_id,
        )
        return RedirectResponse("/ui/seed", status_code=303)

    @app.post("/ui/seed/object")
    def seed_object(
        name: str = Form(...),
        description: str = Form(""),
        canonical_summary: str = Form(""),
        default_location_name: str = Form(""),
        db: sqlite3.Connection = Depends(get_db),
    ) -> RedirectResponse:
        canon = CanonResolver(db)
        default_location_id = None
        if default_location_name.strip():
            default_location = canon.create_or_get_location(name=default_location_name.strip())
            default_location_id = int(default_location["id"])
        canon.create_or_get_object(
            name=name,
            description=description or None,
            canonical_summary=canonical_summary or None,
            default_location_id=default_location_id,
        )
        return RedirectResponse("/ui/seed", status_code=303)

    @app.post("/ui/seed/relation")
    def seed_relation(
        subject_type: str = Form(...),
        subject_name: str = Form(...),
        relation_type: str = Form(...),
        object_type: str = Form(...),
        object_name: str = Form(...),
        notes: str = Form(""),
        db: sqlite3.Connection = Depends(get_db),
    ) -> RedirectResponse:
        canon = CanonResolver(db)
        subject_id = canon.resolve_entity_id(subject_type, subject_name)
        object_id = canon.resolve_entity_id(object_type, object_name)
        canon.add_relation(
            subject_type=subject_type,
            subject_id=subject_id,
            relation_type=relation_type,
            object_type=object_type,
            object_id=object_id,
            notes=notes or None,
        )
        return RedirectResponse("/ui/seed", status_code=303)

    @app.post("/ui/seed/fact")
    def seed_fact(
        entity_type: str = Form(...),
        entity_name: str = Form(""),
        fact_text: str = Form(...),
        is_locked: bool = Form(False),
        source: str = Form("manual"),
        db: sqlite3.Connection = Depends(get_db),
    ) -> RedirectResponse:
        canon = CanonResolver(db)
        if entity_type == "world":
            entity_id = 0
        else:
            entity_id = canon.resolve_entity_id(entity_type, entity_name)
        canon.add_fact(
            entity_type=entity_type,
            entity_id=entity_id,
            fact_text=fact_text,
            is_locked=is_locked,
            source=source,
        )
        return RedirectResponse("/ui/seed", status_code=303)

    @app.get("/ui/locations", response_class=HTMLResponse)
    def locations_page(request: Request, db: sqlite3.Connection = Depends(get_db)) -> HTMLResponse:
        canon = CanonResolver(db)
        return templates.TemplateResponse(
            request,
            "locations.html",
            {"request": request, "locations": canon.list_locations()},
        )

    @app.get("/ui/characters", response_class=HTMLResponse)
    def characters_page(request: Request, db: sqlite3.Connection = Depends(get_db)) -> HTMLResponse:
        canon = CanonResolver(db)
        return templates.TemplateResponse(
            request,
            "characters.html",
            {"request": request, "characters": canon.list_characters()},
        )

    @app.get("/ui/objects", response_class=HTMLResponse)
    def objects_page(request: Request, db: sqlite3.Connection = Depends(get_db)) -> HTMLResponse:
        canon = CanonResolver(db)
        return templates.TemplateResponse(
            request,
            "objects.html",
            {"request": request, "objects": canon.list_objects()},
        )

    @app.get("/ui/assets", response_class=HTMLResponse)
    def assets_page(request: Request, db: sqlite3.Connection = Depends(get_db)) -> HTMLResponse:
        assets = AssetService(db, project_root)
        directories = assets.ensure_asset_directories()
        return templates.TemplateResponse(
            request,
            "assets.html",
            {
                "request": request,
                "assets": assets.list_assets(),
                "asset_generate_example": {
                    "asset_kind": "background",
                    "entity_type": "location",
                    "entity_id": 1,
                    "prompt": "A dawn-lit field of enormous mushrooms with silver dew on the grass, mist pooled between the stalks, distant metallic tracks half-hidden in the soil, and a hushed uncanny mood that suggests someone left only moments ago",
                    "workflow_name": "text-to-image",
                    "filename_base": "mushroom-field-dawn",
                    "width": 1600,
                    "height": 896,
                    "steps": 25,
                    "guidance_scale": 4.0,
                    "seed": 42,
                    "remove_background": False,
                    "metadata": {"style": "storybook"},
                },
                "asset_request_example": {
                    "job_type": "generate_portrait",
                    "asset_kind": "portrait",
                    "entity_type": "character",
                    "entity_id": 1,
                    "model_repo": "stabilityai/stable-diffusion-xl-base-1.0",
                    "prompt": "A mysterious gnome in a black tophat, storybook portrait, transparent background",
                    "negative_prompt": "blurry, extra fingers, watermark",
                    "width": 1024,
                    "height": 1024,
                    "steps": 28,
                    "guidance_scale": 6.5,
                    "seed": 7,
                    "metadata": {"style": "storybook", "reuse": True},
                },
                "background_removal_example": {
                    "source_image_path": str(directories["source"] / "example.png"),
                    "output_name": "example-cutout.png",
                    "entity_type": "character",
                    "entity_id": 1,
                    "model_repo": "briaai/RMBG-2.0",
                    "device": "auto",
                },
                "comfyui_base_url": settings.comfyui_base_url,
                "comfyui_workflow_dir": str(settings.comfyui_workflow_dir),
                "comfyui_output_dir": str(settings.comfyui_output_dir),
            },
        )

    @app.get("/ui/story", response_class=HTMLResponse)
    def story_page(request: Request, db: sqlite3.Connection = Depends(get_db)) -> HTMLResponse:
        story = StoryGraphService(db)
        canon = CanonResolver(db)
        return templates.TemplateResponse(
            request,
            "story.html",
            {
                "request": request,
                "nodes": story.list_story_nodes(),
                "locations": canon.list_locations(),
                "characters": canon.list_characters(),
                "objects": canon.list_objects(),
            },
        )

    @app.post("/ui/story/node")
    def create_story_node_form(
        branch_key: str = Form("default"),
        title: str = Form(""),
        scene_text: str = Form(...),
        summary: str = Form(""),
        entity_refs: str = Form(""),
        db: sqlite3.Connection = Depends(get_db),
    ) -> RedirectResponse:
        story = StoryGraphService(db)
        references: list[dict[str, Any]] = []
        for raw_ref in [part.strip() for part in entity_refs.split(",") if part.strip()]:
            try:
                entity_type, entity_id = raw_ref.split(":")
                references.append({"entity_type": entity_type, "entity_id": int(entity_id), "role": "mentioned"})
            except ValueError:
                continue
        story.create_story_node(
            branch_key=branch_key,
            title=title or None,
            scene_text=scene_text,
            summary=summary or None,
            referenced_entities=references,
        )
        return RedirectResponse("/ui/story", status_code=303)

    @app.post("/ui/story/choice")
    def create_story_choice_form(
        from_node_id: int = Form(...),
        choice_text: str = Form(...),
        to_node_id: int = Form(0),
        status: str = Form("open"),
        notes: str = Form(""),
        db: sqlite3.Connection = Depends(get_db),
    ) -> RedirectResponse:
        story = StoryGraphService(db)
        story.create_choice(
            from_node_id=from_node_id,
            choice_text=choice_text,
            to_node_id=to_node_id or None,
            status=status,
            notes=notes or None,
        )
        return RedirectResponse("/ui/story", status_code=303)

    @app.get("/ui/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, db: sqlite3.Connection = Depends(get_db)) -> HTMLResponse:
        story = StoryGraphService(db)
        return templates.TemplateResponse(
            request,
            "jobs.html",
            {"request": request, "jobs": story.list_jobs()},
        )

    @app.post("/seed-world")
    def seed_world(payload: WorldSeed, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
        canon = CanonResolver(db)
        created_locations: list[dict[str, Any]] = []
        created_characters: list[dict[str, Any]] = []
        created_objects: list[dict[str, Any]] = []
        created_relations: list[dict[str, Any]] = []
        created_facts: list[dict[str, Any]] = []

        for location in payload.locations:
            created_locations.append(
                canon.create_or_get_location(
                    name=location.name,
                    description=location.description,
                    canonical_summary=location.canonical_summary,
                )
            )

        for character in payload.characters:
            home_location_id = None
            if character.home_location_name:
                home = canon.create_or_get_location(name=character.home_location_name)
                home_location_id = int(home["id"])
            created_characters.append(
                canon.create_or_get_character(
                    name=character.name,
                    description=character.description,
                    canonical_summary=character.canonical_summary,
                    home_location_id=home_location_id,
                )
            )

        for object_record in payload.objects:
            default_location_id = None
            if object_record.default_location_name:
                location = canon.create_or_get_location(name=object_record.default_location_name)
                default_location_id = int(location["id"])
            created_objects.append(
                canon.create_or_get_object(
                    name=object_record.name,
                    description=object_record.description,
                    canonical_summary=object_record.canonical_summary,
                    default_location_id=default_location_id,
                )
            )

        for relation in payload.relations:
            created_relations.append(
                canon.add_relation(
                    subject_type=relation.subject_type,
                    subject_id=canon.resolve_entity_id(relation.subject_type, relation.subject_name),
                    relation_type=relation.relation_type,
                    object_type=relation.object_type,
                    object_id=canon.resolve_entity_id(relation.object_type, relation.object_name),
                    notes=relation.notes,
                )
            )

        if payload.premise:
            created_facts.append(
                canon.add_fact(
                    entity_type="world",
                    entity_id=0,
                    fact_text=payload.premise,
                    is_locked=True,
                    source="premise",
                )
            )

        for rule in payload.locked_rules:
            created_facts.append(
                canon.add_fact(
                    entity_type="world",
                    entity_id=0,
                    fact_text=rule,
                    is_locked=True,
                    source="locked_rule",
                )
            )

        for fact in payload.facts:
            if fact.entity_id is not None:
                entity_id = fact.entity_id
            elif fact.entity_type == "world":
                entity_id = 0
            elif fact.entity_name:
                entity_id = canon.resolve_entity_id(fact.entity_type, fact.entity_name)
            else:
                raise HTTPException(status_code=400, detail="Facts require entity_id or entity_name.")
            created_facts.append(
                canon.add_fact(
                    entity_type=fact.entity_type,
                    entity_id=entity_id,
                    fact_text=fact.fact_text,
                    is_locked=fact.is_locked,
                    source=fact.source,
                )
            )

        return {
            "created_locations": created_locations,
            "created_characters": created_characters,
            "created_objects": created_objects,
            "created_relations": created_relations,
            "created_facts": created_facts,
        }

    @app.get("/locations")
    def get_locations(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
        return CanonResolver(db).list_locations()

    @app.get("/characters")
    def get_characters(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
        return CanonResolver(db).list_characters()

    @app.get("/objects")
    def get_objects(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
        return CanonResolver(db).list_objects()

    @app.get("/story-nodes")
    def get_story_nodes(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
        return StoryGraphService(db).list_story_nodes()

    @app.get("/choices")
    def get_choices(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
        return StoryGraphService(db).list_choices()

    @app.get("/jobs")
    def get_jobs(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
        return StoryGraphService(db).list_jobs()

    @app.get("/assets")
    def get_assets(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
        return AssetService(db, project_root).list_assets()

    @app.post("/story-nodes")
    def create_story_node(payload: StoryNodeCreate, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
        story = StoryGraphService(db)
        return story.create_story_node(
            branch_key=payload.branch_key,
            title=payload.title,
            scene_text=payload.scene_text,
            summary=payload.summary,
            parent_node_id=payload.parent_node_id,
            referenced_entities=[reference.model_dump() for reference in payload.referenced_entities],
        )

    @app.post("/choices")
    def create_choice(payload: ChoiceCreate, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
        story = StoryGraphService(db)
        return story.create_choice(
            from_node_id=payload.from_node_id,
            choice_text=payload.choice_text,
            to_node_id=payload.to_node_id,
            status=payload.status,
            notes=payload.notes,
        )

    @app.post("/jobs/generation-stub")
    def create_generation_stub(payload: GenerationPayload, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
        canon = CanonResolver(db)
        story = StoryGraphService(db)
        premise_facts = [fact for fact in canon.list_facts() if fact["entity_type"] == "world"]
        relevant_entities = [
            location
            for location in canon.list_locations()
            if location["id"] in payload.focus_entity_ids
        ]
        context = app.state.llm_generation.build_context(
            branch_key=payload.branch_key,
            premise_facts=premise_facts,
            relevant_entities=relevant_entities,
            open_hooks=payload.open_hooks,
        )
        prompt = app.state.llm_generation.build_prompt(context)
        return story.create_job(job_type="llm_stub", payload_json=json.dumps({"context": context, "prompt": prompt}))

    @app.post("/assets/request")
    def create_asset_request(payload: AssetRequest, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
        assets = AssetService(db, project_root)
        assets.ensure_asset_directories()
        normalized_payload = payload.model_dump()
        if not normalized_payload.get("model_repo"):
            if payload.job_type == "remove_background":
                normalized_payload["model_repo"] = "briaai/RMBG-2.0"
        return assets.enqueue_asset_job(normalized_payload)

    @app.post("/assets/generate")
    def generate_asset(payload: AssetGenerateRequest, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
        assets = AssetService(db, project_root)
        assets.ensure_asset_directories()
        workflow_path = settings.comfyui_workflow_dir / f"{payload.workflow_name}.api.json"
        try:
            return assets.generate_with_comfyui(
                workflow_path=workflow_path,
                comfyui_base_url=settings.comfyui_base_url,
                comfyui_output_dir=settings.comfyui_output_dir,
                entity_type=payload.entity_type,
                entity_id=payload.entity_id,
                asset_kind=payload.asset_kind,
                prompt=payload.prompt,
                width=payload.width,
                height=payload.height,
                steps=payload.steps,
                guidance_scale=payload.guidance_scale,
                seed=payload.seed,
                negative_prompt=payload.negative_prompt,
                filename_base=payload.filename_base,
                metadata=payload.metadata,
                remove_background=payload.remove_background,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"ComfyUI request failed: {exc}. "
                    f"Confirm ComfyUI is running at {settings.comfyui_base_url} and the workflow is valid."
                ),
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"ComfyUI generation failed: {exc}") from exc

    @app.post("/assets/remove-background")
    def remove_background(payload: BackgroundRemovalRequest, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
        assets = AssetService(db, project_root)
        try:
            output_path = assets.remove_background(
                source_image_path=payload.source_image_path,
                output_name=payload.output_name,
                model_repo=payload.model_repo,
                device=payload.device,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Background removal failed: {exc}. "
                    "If this is a gated Hugging Face repo such as briaai/RMBG-2.0, "
                    "log in with `hf auth login` and accept the model terms first."
                ),
            ) from exc
        if payload.entity_type is not None and payload.entity_id is not None:
            asset_record = assets.add_asset(
                entity_type=payload.entity_type,
                entity_id=payload.entity_id,
                asset_kind="cutout",
                file_path=output_path,
                prompt_text=json.dumps(payload.model_dump()),
            )
        else:
            asset_record = {
                "entity_type": payload.entity_type,
                "entity_id": payload.entity_id,
                "asset_kind": "cutout",
                "file_path": output_path,
            }
        return {"output_path": output_path, "asset": asset_record}

    return app


app = create_app()
