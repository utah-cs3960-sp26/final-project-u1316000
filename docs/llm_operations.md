# LLM Operations Guide

This is the first file an LLM should read when working in this repository. It explains the project goal, the data model, and the safe workflow for extending the codebase without breaking canon continuity.

## Project Goal
- Build a local-first prototype for a text-based choose-your-own-adventure system.
- Do not start the story automatically.
- Preserve continuity across branches by storing canonical world state in SQLite.
- Treat the story as two connected graphs:
  - a **world graph** of locations, characters, facts, and relations
  - a **choice graph** of story nodes and player-facing choice edges

## Current Milestone
- SQLite persistence is set up.
- FastAPI JSON endpoints are available.
- A minimal browser UI exists for inspection and manual seeding.
- LLM generation is stubbed only. There is no autonomous expansion loop yet.
- Image generation is not implemented yet beyond asset/job placeholders.

## Repo Layout
- `app/main.py`: FastAPI app factory, JSON API routes, and HTML routes
- `app/database.py`: SQLite bootstrap and connection helpers
- `app/models.py`: Pydantic request models
- `app/services/canon.py`: canonical entity lookup, dedupe, facts, and relations
- `app/services/story_graph.py`: story nodes, choices, node-entity links, and jobs
- `app/services/generation.py`: LLM generation stub for later structured prompting
- `app/templates/`: browser UI templates
- `app/static/styles.css`: UI styling
- `tests/test_app.py`: integration tests for bootstrap, canon continuity, and choice graph storage

## Core Data Model
### Canonical world tables
- `locations`: canonical places such as barns, cabins, roads, forests
- `characters`: recurring people or creatures
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
- Before creating a location or character, check whether it already exists by normalized name.
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
   - `python -m uvicorn app.main:app --reload`
   - open the UI at `http://127.0.0.1:8000`
   - inspect `/ui/seed`, `/ui/story`, and the JSON endpoints
3. Before adding new canon:
   - look for an existing location or character by normalized name
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
- `GET /story-nodes`
- `GET /choices`
- `POST /story-nodes`
- `POST /choices`
- `GET /jobs`
- `POST /jobs/generation-stub`

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

