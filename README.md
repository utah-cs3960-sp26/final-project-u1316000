# CYOA Prototype

Local-first prototype for a branching choose-your-own-adventure system backed by SQLite and a FastAPI inspection console.

## What This Repo Is
- A persistence and tooling layer for an AI-assisted branching story project.
- A canonical world database plus a story-choice graph.
- An operator console for seeding canon, inspecting branches, and debugging continuity.
- Not yet a player-facing game client.

## Current Capabilities
- SQLite schema for locations, characters, objects, relations, facts, story nodes, choices, assets, and generation jobs.
- FastAPI JSON endpoints for seeding and inspecting world/story data.
- Browser-based console for manual world setup and story graph inspection.
- LLM generation stub for future structured scene expansion.

## What Is Not Built Yet
- No SQLite-backed player progression or scene rendering pipeline yet.
- No autonomous story expansion loop.
- No dialogue playback UI beyond raw story node text inspection.
- No image generation or scene compositing workflow yet.

## Quick Start
1. Create a virtual environment:
   - `python -m venv .venv`
   - `.venv\Scripts\Activate.ps1`
2. Install dependencies:
   - `python -m pip install -r requirements.txt`
3. Preview the console:
   - `python -m uvicorn app.main:app --reload`
4. Open:
   - `http://127.0.0.1:8000`

## Useful Pages
- `/` overview dashboard
- `/play` player-view prototype
- `/ui/seed` manual world seeding
- `/ui/locations` canonical locations
- `/ui/characters` canonical characters
- `/ui/objects` canonical objects
- `/ui/story` story nodes and choices
- `/ui/jobs` generation job placeholders

## Current Player Demo
- `/play` is a hardcoded prototype scene for testing the player-facing layout.
- It is separate from the SQLite-backed story graph and exists only as a front-end demo for now.

## Repo Layout
- `app/main.py` FastAPI app factory, HTML routes, and JSON API routes
- `app/database.py` SQLite bootstrap and connection helpers
- `app/models.py` request payload models
- `app/services/canon.py` canonical entity lookup, dedupe, facts, and relations
- `app/services/story_graph.py` story nodes, choices, node-entity links, and jobs
- `app/services/generation.py` future LLM generation interface
- `app/templates/` console UI templates
- `app/static/styles.css` console styling
- `docs/llm_operations.md` primary onboarding guide for future AIs and humans
- `tests/test_app.py` integration tests

## Mental Model
- The **world graph** stores reusable canon such as locations, characters, relations, and facts.
- Objects are first-class canon so persistent items can be reused across branches and asset generation.
- The **choice graph** stores scenes and the choices that connect them.
- A story node should reference canonical entity IDs rather than duplicating world data in prose.
- Continuity comes from reusing canonical entities instead of recreating them in each branch.

## Working Conventions
- Use SQLite as the source of truth.
- Treat locked facts as hard canon and avoid contradicting them automatically.
- Reuse existing locations, characters, and objects whenever possible.
- Keep operator-facing tools separate from any eventual player-facing UI.

## Docs
- Primary onboarding guide: [docs/llm_operations.md](docs/llm_operations.md)
