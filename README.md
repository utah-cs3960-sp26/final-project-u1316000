# CYOA Prototype

Local-first prototype for a branching choose-your-own-adventure system backed by SQLite and a FastAPI inspection console.

## What This Repo Is
- A persistence and tooling layer for an AI-assisted branching story project.
- A canonical world database plus a story-choice graph.
- An operator console for seeding canon, inspecting branches, and debugging continuity.
- Not yet a player-facing game client.

## Current Capabilities
- SQLite schema for locations, characters, objects, relations, facts, story nodes, choices, assets, and generation jobs.
- Story bible plus per-branch state for inventory, affordances, relationship shifts, branch tags, and delayed-payoff hooks.
- FastAPI JSON endpoints for seeding and inspecting world/story data.
- Browser-based console for manual world setup and story graph inspection.
- Structured LLM generation preview and validation workflow with hook pacing guardrails.
- One-scene story expansion loop support with a frontier endpoint and apply-generation writeback.
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
- `/play/death` death-screen prototype
- `/ui/seed` manual world seeding
- `/ui/locations` canonical locations
- `/ui/characters` canonical characters
- `/ui/objects` canonical objects
- `/ui/story` story nodes and choices
- `/ui/assets` assets and image-job schema examples
- `/ui/jobs` generation job placeholders
- `/story-bible` story bible JSON
- `/branches/default/state` aggregated branch state snapshot
- `/frontier` scored open branch ends for the next LLM run

## Current Player Demo
- `/play` still uses hardcoded opening dialogue/choices, but it now resolves its background and actor art from SQLite-backed assets.
- It remains separate from the SQLite-backed story graph for playback/navigation purposes.

## Repo Layout
- `app/main.py` FastAPI app factory, HTML routes, and JSON API routes
- `app/database.py` SQLite bootstrap and connection helpers
- `app/models.py` request payload models
- `app/services/canon.py` canonical entity lookup, dedupe, facts, and relations
- `app/services/story_graph.py` story nodes, choices, node-entity links, and jobs
- `app/services/branch_state.py` per-branch inventory, affordances, tags, relationships, and hooks
- `app/services/generation.py` story bible loading, context building, and generation validation
- `app/services/story_graph.py` now also handles frontier listing, scene presentation metadata, and apply-generation writeback
- `app/services/assets.py` asset metadata, job queueing, Hugging Face model download, and background removal
- `app/services/story_setup.py` soft-reset opening canon and protagonist asset refresh helpers
- `workflows/comfyui/` ComfyUI workflow templates for editor use and API submission
- `app/templates/` console UI templates
- `app/static/styles.css` console styling
- `docs/llm_operations.md` primary onboarding guide for future AIs and humans
- `docs/story_bible.md` human-readable tone and pacing guide
- `app/tools/snapshot_db.py` CLI tool for taking a manual SQLite snapshot before a story session
- `app/tools/prepare_story_run.py` CLI tool for preparing one compact story-worker packet for the next scene expansion
- `app/tools/run_story_worker_local.py` CLI tool for running one local LM Studio-backed worker loop
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
- Track player consequences per branch, not in global canon prose.
- Treat major mysteries as delayed-payoff hooks with both minimum distance and readiness conditions.
- Keep operator-facing tools separate from any eventual player-facing UI.

## Story Workflow Notes
- Global canon stays in `locations`, `characters`, `objects`, `relations`, `facts`, and reusable `assets`.
- Branch-specific consequences live in:
  - `branch_state`
  - `inventory_entries`
  - `unlocked_affordances`
  - `relationship_states`
  - `branch_tags`
  - `story_hooks`
- Use `POST /jobs/generation-preview` to build a structured LLM prompt context from the story bible, canon, and branch state.
- Use `POST /jobs/validate-generation` to check a candidate scene against hook pacing and affordance continuity rules before writing it into the graph.
- Use `GET /frontier` to pick the next unresolved branch end.
- Use `POST /jobs/apply-generation` to atomically write a validated scene into the story graph.
- Use `story_direction_notes` for global planning memory:
  - future plotline ideas
  - future character introductions
  - escalation plans
  - reminders about where a currently small system could lead later
- Use [IDEAS.md](D:/Documents/CS/CS%203960/adventure-test/IDEAS.md) as the loose shared scratchpad for fun future ideas that you or a worker want to jot down quickly.
- Hooks are branch-local in-world unresolved threads. Story direction notes are out-of-world planning memory.
- Default worker behavior should be `2` or `3` choices per scene, not a rigid `3` forever.
- Cycles are fine. Merges should be used carefully when branch-local state still lines up.
- Use `POST /story/reset-opening-canon` to seed the updated bucket-hat protagonist and opening canon.
- Use `POST /story/seed-opening-story` to seed the SQLite-backed opening branch.
- Use `POST /story/refresh-protagonist-assets` to register a fresh protagonist portrait and cutout after a design update.
- Stakes, danger, failure, and death are allowed in story generation, but they should be used deliberately rather than as a default.
- If you want one rollback point before a session, create it manually with:
  - `python -m app.tools.snapshot_db --name before-session`
- If you want to hand a fresh thread one compact story-run packet instead of making it inspect the repo first, use:
  - `python -m app.tools.prepare_story_run`
- If you want to run one local worker loop against LM Studio instead of copy-pasting packets by hand, use:
  - `python -m app.tools.run_story_worker_local --model <loaded-model-id>`
  - add `--plan` to force planning mode
  - add `--dry-run` to validate or plan without writing changes

## Asset Pipeline Notes
- `POST /assets/generate` runs a local ComfyUI workflow and registers the finished file as an asset record.
- Generation prompts are policy-enforced in code:
  - all assets get the same fixed cinematic fantasy style prefix
  - `portrait` and `object_render` assets always add a plain white background plus centered full-body subject rules
  - `portrait` and `object_render` assets also automatically run through background removal and store a `cutout` asset record
  - LLMs should describe content, mood, lighting, physical details, and hooks, not art style
- `POST /assets/request` stores an image-job payload in `generation_jobs`.
- `POST /assets/remove-background` runs local background removal and can store the resulting cutout in `assets`.
- When a story worker introduces:
  - a new recurring character, generate a `portrait`
  - a new visually distinct linked location, generate a `background`
  - a new reusable visually important object, generate an `object_render`
- For brand-new canon entities, apply the scene first so IDs exist, then generate the assets.
- Prefer `new linked location + reusable object` over a same-location visual variant for materially distinct sub-scenes until explicit active-asset assignment exists.
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
- Tone and pacing guide: [docs/story_bible.md](docs/story_bible.md)
- Loop worker guide: [docs/llm_story_worker.md](docs/llm_story_worker.md)

## How to preview:
python -m uvicorn app.main:app --reload --port 8001
