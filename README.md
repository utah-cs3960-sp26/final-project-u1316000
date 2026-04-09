# Proposal
This project will be a web based choose your own adventure game prototype that uses AI to generate both the story and the images for the game. With a looping python script, I can allow locally run AI models to run for very long periods of time and generate a large amount of content for the game, creating an ever expanding world where the choices and assets are all done autonomously. However unlike a simple AI generated story, this project has a large system of guardrails, world bible, story hooks, lore, and backing database to ensure long lasting continuity and allow for recurring characters, locations, and items. The webapp also has a console view that displays all of the current characters, locations, and more as well as an interactive branching graph view to see the progression of the story. The story itself is very barebones at the moment and has gone through several story quality control changes, but the framework is in place to allow for a large and complex story to be generated. 

The core of this project is the "story worker" loop. It is a python script that will run in a terminal in a "while true" loop and will use a locally run AI model to generate new content for the story. It starts by running a "prepare story run" script that fetches relevant story nodes and choices to give each fresh AI context of recent choices, larger worldbuilding and branch directions, etc. It then runs the LLM with the worker guide and the prepared story run context to generate new story content. This content is then validated by the worker script to ensure it meets the standards of the world bible and story hooks. If it is valid, any requested art is generated through a locally downloaded image generator through comfyui and the choices and new dialogue is then written to the database and the loop repeats. This allows for a continuous stream of new content to be generated for the story. There is also a 15% chance for a "planning run" to occur, in which the AI writes no new content but instead analyzes larger story directions and creates new ideas. In addition to all of the quality control for the agentic loop, the codebase also has a test suite that verifies the SQL tables are initialized correctly and that the story worker can generate valid story content.



## CYOA Prototype

Local-first prototype for a branching choose-your-own-adventure system backed by SQLite and a FastAPI inspection console.

## What This Repo Is
- A persistence and tooling layer for an AI-assisted branching story project.
- A canonical world database plus a story-choice graph.
- An operator console for seeding canon, inspecting branches, and debugging continuity.
- Not yet a player-facing game client.

## Current Capabilities
- SQLite schema for locations, characters, objects, relations, facts, story nodes, choices, assets, and generation jobs.
- Story bible plus per-branch state for inventory, affordances, relationship shifts, branch tags, and delayed-payoff hooks.
- Worldbuilding memory lane for ambient pressure such as rumors, patrols, automata, and broader offscreen motion.
- FastAPI JSON endpoints for seeding and inspecting world/story data.
- Browser-based console for manual world setup and story graph inspection.
- Structured LLM generation preview and validation workflow with hook pacing, frontier-budget, merge, and closure guardrails.
- Local autonomous story-worker loop support through LM Studio-backed CLI tooling.
- Frontier budget enforcement, inspection-choice reconvergence pressure, larger arc-exit merge support, and branch-ending support.
- Empty-frontier revival flow that reopens continuity from an earlier closed parent instead of starting a disconnected cycle.
- Parked-choice maintenance tooling for reversible frontier cleanup.
- ComfyUI-backed image generation plus a local Hugging Face background-removal path.

## What Is Not Built Yet
- No fully polished player-facing game loop yet; `/play` is still a prototype on top of the evolving story graph.
- No sophisticated authored scene-layout editor beyond the current console and presentation metadata.
- No fully automatic branch cleanup strategy beyond parking/revival; larger long-range merge behavior still depends on the generation loop and validators rather than a dedicated planner daemon.

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
- `app/services/worldbuilding.py` ambient world-pressure memory and update helpers
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
- `app/tools/rebalance_frontier.py` CLI tool for parking low-priority frontier choices or unparking them later
- `app/tools/remove_background.py` CLI tool for local background removal
- `app/tools/download_hf_model.py` CLI tool for downloading model repos into the local cache
- `tests/test_app.py` integration tests

## Mental Model
- The **world graph** stores reusable canon such as locations, characters, relations, and facts.
- Objects still exist as canon, but persistent objects are now treated as exceptional rather than the default answer for every notable prop.
- The **choice graph** stores scenes and the choices that connect them.
- A story node should reference canonical entity IDs rather than duplicating world data in prose.
- Continuity comes from reusing canonical entities instead of recreating them in each branch.
- Not every choice deserves a permanent frontier leaf. Inspection beats should usually reconverge, winding-down arcs should deliberately merge, and some branches should truly end.
- When a recurring character needs to appear on a path where they have not been met yet, the system can use a floating character introduction instead of pretending prior familiarity.

## Working Conventions
- Use SQLite as the source of truth.
- Treat locked facts as hard canon and avoid contradicting them automatically.
- Reuse existing locations, characters, and objects whenever possible.
- Track player consequences per branch, not in global canon prose.
- Treat major mysteries as delayed-payoff hooks with both minimum distance and readiness conditions.
- Keep operator-facing tools separate from any eventual player-facing UI.

## Branch Control and Continuity
- Branch growth is now actively constrained in code, not just suggested in prompts.
- `branching_policy.frontier_budget` controls the default branch budget:
  - `soft_open_choice_limit: 48`
  - `hard_open_choice_limit: 72`
  - `max_choices_per_node: 5`
  - `keep_recent_parent_count: 12`
  - `default_max_fresh_choices_per_scene: 1`
  - `allow_second_fresh_choice_only_for_bloom_scenes: true`
- When frontier pressure is `soft` or `hard`, workers should prefer merges, closures, and narrow continuation over spawning multiple fresh leaves.
- Choice classes are supported:
  - `inspection`
  - `progress`
  - `commitment`
  - `ending`
- Ending choices are allowed and validated. Supported ending categories include:
  - `death`
  - `dead_end`
  - `capture`
  - `transformation`
  - `hub_return`
- Inspection choices are expected to reconverge quickly, especially under frontier pressure.
- Arc-exit merges are allowed when a local storyline is winding down and the branch state is compatible with a larger reconvergence target.
- If the active frontier becomes empty, the worker does not just stop. `prepare_story_run` now selects a random closed leaf, walks back to its parent, and enters `revival` mode:
  - if that parent has fewer than 5 total choices, a new choice is appended
  - if it already has 5 choices, the traversed closing choice is replaced
- Choice statuses now include `parked` and `closed` in addition to the earlier open/fulfilled flow. Parked choices stay in history but are hidden from normal frontier selection until unparked.

## Story Workflow Notes
- Global canon stays in `locations`, `characters`, `objects`, `relations`, `facts`, and reusable `assets`.
- Branch-specific consequences live in:
  - `branch_state`
  - `inventory_entries`
  - `unlocked_affordances`
  - `relationship_states`
  - `branch_tags`
  - `story_hooks`
- Use `POST /jobs/generation-preview` to build a structured LLM prompt context from the story bible, canon, branch state, frontier pressure, and worldbuilding notes.
- Use `POST /jobs/validate-generation` to check a candidate scene against hook pacing, affordance continuity, frontier-budget, merge, closure, and object-demotion rules before writing it into the graph.
- Use `GET /frontier` to pick the next unresolved branch end.
- Use `POST /jobs/apply-generation` to atomically write a validated scene into the story graph.
- Use `story_direction_notes` for global planning memory:
  - future plotline ideas
  - future character introductions
  - escalation plans
  - reminders about where a currently small system could lead later
- Use `worldbuilding_notes` for ambient offscreen pressure:
  - patrols
  - rumors
  - automata activity
  - faction motion
  - danger escalation
  - political pressure
- Use [IDEAS.md](D:/Documents/CS/CS%203960/adventure-test/IDEAS.md) as the loose shared scratchpad for fun future ideas that you or a worker want to jot down quickly.
- Hooks are branch-local in-world unresolved threads. Story direction notes are out-of-world planning memory. Worldbuilding notes are world-level ambient pressure memory.
- Default worker behavior should be `2` or `3` choices per scene, not a rigid `3` forever.
- Cycles are fine. Quick merges are one of the main branch-count control tools, and larger merges should be used when branch-local state still lines up.
- Persistent objects are exceptional. Ordinary props, vehicles, scenery details, and one-off devices should usually stay in scene text, hooks, or worldbuilding rather than becoming reusable canon objects.
- Stakes, danger, failure, and death are allowed in story generation and are part of the supported closure toolkit.
- Use `POST /story/reset-opening-canon` to seed the updated bucket-hat protagonist and opening canon.
- Use `POST /story/seed-opening-story` to seed the SQLite-backed opening branch.
- Use `POST /story/refresh-protagonist-assets` to register a fresh protagonist portrait and cutout after a design update.
- If you want one rollback point before a session, create it manually with:
  - `python -m app.tools.snapshot_db --name before-session`
- If you want to hand a fresh thread one compact story-run packet instead of making it inspect the repo first, use:
  - `python -m app.tools.prepare_story_run`
- If you want to run one local worker loop against LM Studio instead of copy-pasting packets by hand, use:
  - `python -m app.tools.run_story_worker_local --model <loaded-model-id>`
  - add `--plan` to force planning mode
  - add `--dry-run` to validate or plan without writing changes
- If you want to reduce frontier sprawl without deleting history, use:
  - `python -m app.tools.rebalance_frontier`
  - add `--apply` to actually park choices instead of previewing
  - add `--unpark-choice-id <id>` to restore a parked choice

## Asset Pipeline Notes
- `POST /assets/generate` runs a local ComfyUI workflow and registers the finished file as an asset record.
- Generation prompts are policy-enforced in code:
  - all assets get the same fixed cinematic fantasy style prefix
  - `portrait` assets always add plain-white-background, single-character, full-body-with-margins rules
  - `object_render` assets always add plain-white-background, single-object-only, no-extra-props rules
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

## Worker Loop Notes
- `prepare_story_run` now emits three kinds of packets:
  - `normal`
  - `planning`
  - `revival`
- Normal packets include:
  - frontier budget state
  - current branch pressure
  - compact worldbuilding notes
  - path character continuity
  - arc-exit eligibility hints
  - bound-idea steering when planning has attached a concrete idea to a frontier choice
- Planning packets:
  - do not apply a new story scene
  - generate fresh ideas
  - update frontier choice notes
  - can bind a choice to a specific idea
  - can create worldbuilding notes for offscreen pressure
- Revival packets:
  - happen only when no open frontier items remain
  - reopen continuity from an earlier closed parent
  - ask the model for one new choice rather than a full new scene
- The local runner logs started/succeeded/failed runs to:
  - `data/worker_logs/local_worker_runs.ndjson`
- The runner now validates and retries not only for schema problems, but also for branch continuity, art-anchor mistakes, and other guardrail failures surfaced by the validator.

## Docs
- Primary onboarding guide: [docs/llm_operations.md](docs/llm_operations.md)
- Tone and pacing guide: [docs/story_bible.md](docs/story_bible.md)
- Loop worker guide: [docs/llm_story_worker.md](docs/llm_story_worker.md)

## How to preview:
python -m uvicorn app.main:app --reload --port 8001
