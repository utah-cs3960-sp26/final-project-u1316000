# LLM Operations Guide

This is the first file an LLM should read when working in this repository. It explains the project goal, the data model, and the safe workflow for extending the codebase without breaking canon continuity.

## Project Goal
- Build a local-first prototype for a text-based choose-your-own-adventure system.
- Do not start the story automatically.
- Preserve continuity across branches by storing canonical world state in SQLite.
- Treat the story as two connected graphs:
  - a **world graph** of locations, characters, objects, facts, and relations
  - a **choice graph** of story nodes and player-facing choice edges

## Current Milestone
- SQLite persistence is set up.
- FastAPI JSON endpoints are available.
- A minimal browser UI exists for inspection and manual seeding.
- LLM generation is stubbed only. There is no autonomous expansion loop yet.
- ComfyUI-backed image generation is available through a workflow template and asset API/tooling.
- Local background removal is wired through a Hugging Face-compatible tool path.

## Repo Layout
- `app/main.py`: FastAPI app factory, JSON API routes, and HTML routes
- `app/database.py`: SQLite bootstrap and connection helpers
- `app/models.py`: Pydantic request models
- `app/services/canon.py`: canonical entity lookup, dedupe, facts, and relations
- `app/services/story_graph.py`: story nodes, choices, node-entity links, and jobs
- `app/services/generation.py`: LLM generation stub for later structured prompting
- `app/services/assets.py`: asset job schema, Hugging Face model download helper, and background removal
- `workflows/comfyui/`: ComfyUI workflow templates; keep editor and API variants here
- `app/templates/`: browser UI templates
- `app/static/styles.css`: UI styling
- `app/tools/generate_asset.py`: command-line helper for ComfyUI-backed image generation
- `app/tools/remove_background.py`: command-line helper for RMBG background removal
- `app/tools/download_hf_model.py`: command-line helper for prefetching Hugging Face model repos
- `tests/test_app.py`: integration tests for bootstrap, canon continuity, and choice graph storage

## Core Data Model
### Canonical world tables
- `locations`: canonical places such as barns, cabins, roads, forests
- `characters`: recurring people or creatures
- `objects`: recurring items, props, tools, keepsakes, keys, and other persistent things
- `relations`: directed links such as `north_of`, `lives_in`, `knows`
- `facts`: statements about entities or world rules
- `assets`: reusable portrait/background/cutout metadata

### Story graph tables
- `story_nodes`: scene records
- `choices`: directed edges from one node to another
- `node_entities`: links between a scene and canonical entities it references
- `generation_jobs`: placeholder records for later LLM expansion jobs

## Hard Canon vs Soft Canon
- **Hard canon** means facts that must not be contradicted automatically.
- In this prototype, hard canon is represented by `facts.is_locked = 1`.
- World premise statements and locked rules should be stored as `facts` with:
  - `entity_type = "world"`
  - `entity_id = 0`
- **Soft canon** is any non-locked generated or manual fact that can later be revised or promoted.

## Continuity Rules
- Reuse canonical entities whenever possible.
- Before creating a location, character, or object, check whether it already exists by normalized name.
- Spatial continuity is explicit. Example:
  - if the cabin is north of the barn, store one `relations` row where:
    - subject = cabin
    - relation_type = `north_of`
    - object = barn
- A later branch going north from the barn should resolve the same cabin row instead of creating another one.

## Safe Workflow For An LLM
1. Read this file first.
2. Inspect current data:
   - `python -m pytest`
   - `python -m uvicorn app.main:app --reload --port 8001`
   - open the UI at `http://127.0.0.1:8001`
   - inspect `/ui/seed`, `/ui/story`, and the JSON endpoints
3. Before adding new canon:
   - look for an existing location, character, or object by normalized name
   - inspect nearby relations and existing facts
4. When adding story content later:
   - create or reuse canonical entities first
   - create a `story_node`
   - attach canonical entity references in `node_entities`
   - add `choices` as outgoing edges
   - store any new world truths in `facts` and `relations`
5. Do not duplicate entities just because a branch rediscovers them.

## JSON API Overview
- `POST /seed-world`
- `GET /locations`
- `GET /characters`
- `GET /objects`
- `GET /story-nodes`
- `GET /choices`
- `GET /assets`
- `POST /story-nodes`
- `POST /choices`
- `GET /jobs`
- `POST /jobs/generation-stub`
- `POST /assets/request`
- `POST /assets/generate`
- `POST /assets/remove-background`

## Asset Workflow
- Preferred image-generation path:
  - Use `POST /assets/generate` or `python -m app.tools.generate_asset ...`
  - The repo submits `workflows/comfyui/<workflow>.api.json` to the local ComfyUI server
  - Generated image files are stored on disk, then registered in `assets`
- Prompt policy is enforced in code:
  - all generated assets get a fixed house style prefix
  - `portrait` and `object_render` prompts always append a plain white background plus centered full-body subject rules
  - `portrait` and `object_render` generations automatically run through background removal and create a linked `cutout` asset record
  - for the user-provided prompt text, focus on content: mood, scale, lighting, texture, physical details, and story hooks
  - do not spend prompt budget restating art style instructions unless a content detail truly matters
- Keep workflow templates in `workflows/comfyui/`.
- Treat `text-to-image.json` as the editor workflow and `text-to-image.api.json` as the machine-submittable version.
- Use `POST /assets/request` to store a structured asset-generation request even before a full image generator is wired in.
- Use `POST /assets/remove-background` or `python -m app.tools.remove_background --input path\to\image.png` to create transparent cutouts from source images.
- `/play` currently uses hardcoded opening dialogue, but it resolves location backgrounds and actor cutouts from SQLite assets.
- Default background-removal model: `briaai/RMBG-2.0`.
- Licensing warning: the Hugging Face RMBG-2.0 weights are non-commercial under the model card license, so keep that constraint in mind.
- Access warning: `briaai/RMBG-2.0` is gated on Hugging Face, so accept the model terms and run `hf auth login` before trying to download or infer with it.

## Example Usage
### Seed a barn and cabin
```json
{
  "premise": "A rural mystery unfolds around an isolated farm.",
  "locked_rules": [
    "The world should remain grounded and coherent."
  ],
  "locations": [
    {"name": "Barn", "canonical_summary": "A weathered red barn."},
    {"name": "Cabin", "canonical_summary": "A small cabin north of the barn."}
  ],
  "relations": [
    {
      "subject_type": "location",
      "subject_name": "Cabin",
      "relation_type": "north_of",
      "object_type": "location",
      "object_name": "Barn"
    }
  ]
}
```

### Create a story node later
```json
{
  "branch_key": "default",
  "title": "At the Barn",
  "scene_text": "You stand outside the barn at dusk.",
  "summary": "Opening node near the farm.",
  "referenced_entities": [
    {"entity_type": "location", "entity_id": 1, "role": "current_scene"}
  ]
}
```

## Guardrails
- No autonomous story generation should be activated in this milestone.
- No story should be pre-seeded unless explicitly requested later.
- Keep SQLite as the source of truth for continuity.
- Prefer exact entity reuse over fuzzy reinvention.
- Do not hand-edit workflow node IDs for routine asset generation. Use the Python tool/API and treat the workflow JSON as a template.
- Do not let the LLM override the house art style or the portrait/object white-background rules through prompt wording. Those are fixed generator policies.
