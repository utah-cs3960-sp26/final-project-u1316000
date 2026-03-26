from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.database import bootstrap_database, connect
from app.models import ChoiceCreate, GenerationPayload, StoryNodeCreate, WorldSeed
from app.services.canon import CanonResolver
from app.services.generation import LLMGenerationService
from app.services.story_graph import StoryGraphService


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

    app = FastAPI(title="CYOA Prototype")
    app.state.settings = settings
    app.state.llm_generation = LLMGenerationService()

    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, db: sqlite3.Connection = Depends(get_db)) -> HTMLResponse:
        canon = CanonResolver(db)
        story = StoryGraphService(db)
        context = {
            "request": request,
            "counts": story.counts(),
            "locations": canon.list_locations()[:5],
            "characters": canon.list_characters()[:5],
            "nodes": story.list_story_nodes()[:5],
            "jobs": story.list_jobs()[:5],
            "db_path": str(settings.database_path),
        }
        return templates.TemplateResponse(request, "index.html", context)

    @app.get("/ui/seed", response_class=HTMLResponse)
    def seed_page(request: Request, db: sqlite3.Connection = Depends(get_db)) -> HTMLResponse:
        canon = CanonResolver(db)
        context = {
            "request": request,
            "locations": canon.list_locations(),
            "characters": canon.list_characters(),
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
            "created_relations": created_relations,
            "created_facts": created_facts,
        }

    @app.get("/locations")
    def get_locations(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
        return CanonResolver(db).list_locations()

    @app.get("/characters")
    def get_characters(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
        return CanonResolver(db).list_characters()

    @app.get("/story-nodes")
    def get_story_nodes(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
        return StoryGraphService(db).list_story_nodes()

    @app.get("/choices")
    def get_choices(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
        return StoryGraphService(db).list_choices()

    @app.get("/jobs")
    def get_jobs(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
        return StoryGraphService(db).list_jobs()

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

    return app


app = create_app()
