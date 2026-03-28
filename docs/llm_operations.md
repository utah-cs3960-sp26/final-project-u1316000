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
- Story generation now has a structured preview/validation workflow driven by the story bible and branch state.
- The loop can now choose a real branch end from `/frontier` and apply a validated scene with `/jobs/apply-generation`.
- There is still no autonomous expansion loop yet.
- ComfyUI-backed image generation is available through a workflow template and asset API/tooling.
- Local background removal is wired through a Hugging Face-compatible tool path.

## Repo Layout
- `app/main.py`: FastAPI app factory, JSON API routes, and HTML routes
- `app/database.py`: SQLite bootstrap and connection helpers
- `app/models.py`: Pydantic request models
- `app/services/canon.py`: canonical entity lookup, dedupe, facts, and relations
- `app/services/story_graph.py`: story nodes, choices, node-entity links, and jobs
- `app/services/branch_state.py`: per-branch inventory, affordances, tags, relationships, and hooks
- `app/services/generation.py`: story bible loading, LLM context building, and validation
- `app/services/assets.py`: asset job schema, Hugging Face model download helper, and background removal
- `app/services/story_setup.py`: bucket-hat opening reset helpers
- `workflows/comfyui/`: ComfyUI workflow templates; keep editor and API variants here
- `app/templates/`: browser UI templates
- `app/static/styles.css`: UI styling
- `app/tools/generate_asset.py`: command-line helper for ComfyUI-backed image generation
- `app/tools/remove_background.py`: command-line helper for RMBG background removal
- `app/tools/download_hf_model.py`: command-line helper for prefetching Hugging Face model repos
- `tests/test_app.py`: integration tests for bootstrap, canon continuity, and choice graph storage
- `docs/llm_story_worker.md`: the single file a story loop should point the LLM at every run

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

### Branch state tables
- `branch_state`: current act phase, branch depth, and latest node
- `inventory_entries`: branch-specific persistent items
- `unlocked_affordances`: reusable capabilities such as summoning a goose ride
- `relationship_states`: branch-specific character stance and relationship tags
- `branch_tags`: clue/state/travel/quest tags used for pacing and gating
- `story_hooks`: delayed-payoff setups with importance, minimum distance, and readiness tags

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
- Persistent branch consequences should not be buried only in prose:
  - put reusable items in `inventory_entries`
  - put reusable powers/travel options/social permissions in `unlocked_affordances`
  - put mystery pacing in `story_hooks`

## Story Bible Rules
- Read [docs/story_bible.md](docs/story_bible.md) before generating story content.
- The current protagonist reset is authoritative:
  - abnormally tall gnome
  - five thumbs on the left hand
  - red-and-white striped bucket hat of unknown origin
  - amnesia remains a major mystery
- The world is whimsical and surreal, but it should not feel like parody.
- Strange things should matter. Avoid randomness that exists only to be quirky.
- Major hooks require both:
  - minimum narrative distance before payoff
  - readiness via clue/state tags
- Side quests are welcome. Immediate collapse of every mystery is not.
- Default to `2` or `3` choices rather than forcing `3` every time.
- Cycles are welcome. Merges require extra care because branch-local consequences can diverge.
- Prefer `new linked location + reusable object` over a same-location visual variant when the player enters a materially distinct sub-scene.

## Safe Workflow For An LLM
1. Read this file first.
2. Read `docs/story_bible.md`.
3. For story-expansion loop work, prefer `docs/llm_story_worker.md` as the per-run entrypoint.
4. If a human wants a rollback point before a session, create a manual snapshot first:
   - `python -m app.tools.snapshot_db --name before-session`
5. Prefer the one-command worker prep path:
   - `python -m app.tools.prepare_story_run`
   - this prepares one compact packet with the selected frontier item, pre-change URL, preview payload, and branch context
   - use it instead of rediscovering the repo structure in a new thread
6. Inspect current data only if needed:
   - `python -m pytest`
   - `python -m uvicorn app.main:app --reload --port 8001`
   - open the UI at `http://127.0.0.1:8001`
   - inspect `/ui/seed`, `/ui/story`, `/story-bible`, and `/branches/default/state`
7. If the opening canon needs to be reset to the current protagonist design:
   - call `POST /story/reset-opening-canon`
   - call `POST /story/seed-opening-story`
   - optionally call `POST /story/refresh-protagonist-assets`
8. Before adding new canon:
   - look for an existing location, character, or object by normalized name
   - inspect nearby relations and existing facts
9. When adding story content later:
   - create or reuse canonical entities first
   - update branch state when the player gains a persistent item, relationship change, clue, or affordance
   - create a `story_node`
   - attach canonical entity references in `node_entities`
   - add `choices` as outgoing edges
   - store any new world truths in `facts` and `relations`
10. Before treating generated content as valid:
   - get a target from `GET /frontier`
   - build context with `POST /jobs/generation-preview`
   - validate the candidate with `POST /jobs/validate-generation`
   - apply it with `POST /jobs/apply-generation`
11. After applying a scene, generate any required missing visuals:
   - new recurring character -> `portrait`
   - new linked visually distinct location -> `background`
   - new reusable visually important object -> `object_render`
12. Do not duplicate entities just because a branch rediscovers them.

## JSON API Overview
- `POST /seed-world`
- `GET /locations`
- `GET /characters`
- `GET /objects`
- `GET /story-nodes`
- `GET /choices`
- `GET /assets`
- `GET /story-bible`
- `GET /frontier`
- `GET /branches/{branch_key}/state`
- `POST /branches/{branch_key}/tags`
- `POST /branches/{branch_key}/inventory`
- `POST /branches/{branch_key}/affordances`
- `POST /branches/{branch_key}/relationships`
- `POST /branches/{branch_key}/hooks`
- `POST /story-nodes`
- `POST /choices`
- `GET /jobs`
- `POST /jobs/generation-preview`
- `POST /jobs/validate-generation`
- `POST /jobs/apply-generation`
- `POST /story/reset-opening-canon`
- `POST /story/seed-opening-story`
- `POST /story/refresh-protagonist-assets`
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
- Scene-authoring policy:
  - if a scene introduces a new recurring character, a new visually distinct linked location, or a reusable visually important object, treat image generation as part of finishing that scene
  - for brand-new canon entities, apply the scene first so SQLite assigns real IDs, then call `/assets/generate`
  - use same-location variants sparingly for now because the renderer currently prefers the latest asset per entity and does not yet support explicit active-asset assignment
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
- Keep SQLite as the source of truth for continuity.
- Prefer exact entity reuse over fuzzy reinvention.
- Preserve branch continuity for unlocked affordances and major hooks; do not “forget” a goose whistle once a branch has earned it.
- Do not resolve a major mystery simply because it was introduced recently or because a detail looks tempting.
- Treat `/frontier` items as the canonical units of work for the one-scene loop.
- Do not hand-edit workflow node IDs for routine asset generation. Use the Python tool/API and treat the workflow JSON as a template.
- Do not let the LLM override the house art style or the portrait/object white-background rules through prompt wording. Those are fixed generator policies.
