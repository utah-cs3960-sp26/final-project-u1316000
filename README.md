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
- ComfyUI-backed image generation plus a local Hugging Face background-removal path.

## What Is Not Built Yet
- No SQLite-backed player progression or scene rendering pipeline yet.
- No autonomous story expansion loop.
- No dialogue playback UI beyond raw story node text inspection.
- No full SQLite-backed story playback loop or authored scene-layout system beyond the opening demo.

## Quick Start
1. Create a virtual environment:
   - `python -m venv .venv`
   - `.venv\Scripts\Activate.ps1`
2. Install dependencies:
   - `python -m pip install -r requirements.txt`
3. Preview the console:
   - `python -m uvicorn app.main:app --reload --port 8001`
4. Open:
   - `http://127.0.0.1:8001`

## Useful Pages
- `/` overview dashboard
- `/play` player-view prototype
- `/ui/seed` manual world seeding
- `/ui/locations` canonical locations
- `/ui/characters` canonical characters
- `/ui/objects` canonical objects
- `/ui/story` story nodes and choices
- `/ui/assets` assets and image-job schema examples
- `/ui/jobs` generation job placeholders

## Current Player Demo
- `/play` still uses hardcoded opening dialogue/choices, but it now resolves its background and actor art from SQLite-backed assets.
- It remains separate from the SQLite-backed story graph for playback/navigation purposes.

## Repo Layout
- `app/main.py` FastAPI app factory, HTML routes, and JSON API routes
- `app/database.py` SQLite bootstrap and connection helpers
- `app/models.py` request payload models
- `app/services/canon.py` canonical entity lookup, dedupe, facts, and relations
- `app/services/story_graph.py` story nodes, choices, node-entity links, and jobs
- `app/services/generation.py` future LLM generation interface
- `app/services/assets.py` asset metadata, job queueing, Hugging Face model download, and background removal
- `workflows/comfyui/` ComfyUI workflow templates for editor use and API submission
- `app/templates/` console UI templates
- `app/static/styles.css` console styling
- `docs/llm_operations.md` primary onboarding guide for future AIs and humans
- `app/tools/remove_background.py` CLI tool for local background removal
- `app/tools/download_hf_model.py` CLI tool for downloading model repos into the local cache
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

## Asset Pipeline Notes
- `POST /assets/generate` runs a local ComfyUI workflow and registers the finished file as an asset record.
- Generation prompts are policy-enforced in code:
  - all assets get the same fixed cinematic fantasy style prefix
  - `portrait` and `object_render` assets always add a plain white background plus centered full-body subject rules
  - `portrait` and `object_render` assets also automatically run through background removal and store a `cutout` asset record
  - LLMs should describe content, mood, lighting, physical details, and hooks, not art style
- `POST /assets/request` stores an image-job payload in `generation_jobs`.
- `POST /assets/remove-background` runs local background removal and can store the resulting cutout in `assets`.
- Default ComfyUI settings:
  - base URL: `http://127.0.0.1:8000`
  - workflow dir: `workflows/comfyui`
  - output dir: `data/assets/comfy_output`
- The default background-removal model is `briaai/RMBG-2.0`.
- Important license note: the Hugging Face RMBG-2.0 weights are source-available for non-commercial use, not general commercial use.
- Important access note: `briaai/RMBG-2.0` is gated on Hugging Face. You need to accept the model terms and authenticate first.
- Helpful commands:
  - `python -m app.tools.generate_asset --asset-kind background --entity-type location --entity-id 1 --prompt "A giant mushroom field at dawn with silver dew, towering pale caps, quiet fog, and the unsettling feeling that someone left in a hurry" --width 1600 --height 896`
  - `python -m app.tools.download_hf_model --repo briaai/RMBG-2.0`
  - `hf auth login`
  - `python -m app.tools.remove_background --input path\to\image.png`

## Docs
- Primary onboarding guide: [docs/llm_operations.md](docs/llm_operations.md)

## How to preview:
python -m uvicorn app.main:app --reload --port 8001
