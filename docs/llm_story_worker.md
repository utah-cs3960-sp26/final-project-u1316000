# LLM Story Worker

This is the single file the story-expansion loop should point the LLM at every run.

## Project Idea
- This project is a local-first prototype for an expansive AI-grown choose-your-own-adventure story.
- The long-term idea is that the world keeps expanding through repeated LLM runs instead of being fully authored up front.
- The story should feel whimsical, surreal, and adventurous, with recurring places, characters, items, and mysteries that stay consistent across many branches.
- Different branches should be able to rediscover the same cabin, frog, goose route, whistle, or mystery clue instead of inventing disconnected replacements.

## Specific Goal Of This Repo
- This repository is not trying to be the LLM itself. Its job is to be the continuity engine and authoring runtime around the LLM.
- It stores global canon in SQLite.
- It stores branch-specific player consequences separately from global world truth.
- It lets a worker LLM expand the story one scene at a time in a way that can be validated before becoming canon in that branch.
- It also stores and reuses visual assets so backgrounds, characters, and important objects can recur instead of being one-off images.

## Your Job In Plain English
- Most of the time, your job is not to change the code.
- Your normal job is to act like a story worker: look at one open branch end, understand the current canon and branch state, and write the next scene in structured JSON.
- You are mainly helping the story grow while preserving continuity, pacing, tone, hooks, and persistent consequences.
- Only touch code or docs if the human explicitly asks for codebase changes or if the loop is clearly blocked by a missing tool, schema, or bug.

## Goal
- Expand the story one scene at a time.
- Preserve continuity.
- Keep the tone whimsical, surreal, sincere, and slightly unhinged.
- Delay major payoffs until the branch state and hook gating actually allow them.
- Carry the visual side forward too when the scene genuinely needs new art.
- Stakes, danger, and even death are allowed, but they are not required for a good scene or arc.

## Source Of Truth
- Global canon:
  - `locations`
  - `characters`
  - `objects`
  - `relations`
  - locked `facts`
  - reusable `assets`
- Branch-local state:
  - `branch_state`
  - `inventory_entries`
  - `unlocked_affordances`
  - `relationship_states`
  - `branch_tags`
  - `story_hooks`
- Story graph:
  - `story_nodes`
  - `choices`
  - `node_entities`
  - `story_node_present_entities`

## Hard Rules
- Do not resolve major hooks early.
- Do not forget branch-local affordances, inventory, or relationship changes.
- Do not casually duplicate world entities.
- Keep notes, clues, unlocked capabilities, and player consequences branch-local unless they become true world canon.
- Strange things should matter. Do not add randomness with no continuity or consequence.
- Death is available as a consequence, but do not treat lethality as the default way to make scenes meaningful.
- Usually return `2` or `3` player choices. `1` is acceptable for a forced transition or tightly framed beat. More than `3` should be uncommon and justified.
- Cycles are allowed. Merges are allowed only when the resulting scene still fits the relevant branch-local consequences.
- If you introduce a new recurring character, a new visually distinct linked location, or a reusable visually important object, make sure the visual follow-through happens after apply.

## Per-Run Workflow
1. Read this file.
2. Ask the backend which branch end to work on:
   - `GET /frontier`
3. Pick one frontier item.
4. Record the pre-change test URL for the exact state you are expanding:
   - `/play?branch_key=<branch_key>&scene=<from_node_id>`
   - when you finish the run, always print or report that URL so a human can quickly reopen the state before your change
5. Build the generation context:
   - `POST /jobs/generation-preview`
6. Return one structured `GenerationCandidate`.
7. Validate it:
   - `POST /jobs/validate-generation`
8. Only if valid, apply it:
   - `POST /jobs/apply-generation`
9. After apply, generate required missing visuals:
   - new recurring character -> `portrait`
   - new visually distinct linked location -> `background`
   - new reusable visually important object -> `object_render`

## Recommended Loop Contract
1. `GET /frontier?limit=1&mode=auto`
2. Use the returned `choice_id`, `from_node_id`, `branch_key`, and branch summary.
3. `POST /jobs/generation-preview`
4. Produce one `GenerationCandidate`
5. `POST /jobs/validate-generation`
6. If valid:
   - `POST /jobs/apply-generation`
   - inspect newly created canon entities
   - call `POST /assets/generate` for any required missing visuals
7. Repeat

## Expected Output Shape
Return JSON that fits `GenerationCandidate`.

Required fields:
- `branch_key`
- `scene_summary`
- `scene_text`
- `choices`

Strongly recommended fields:
- `dialogue_lines`
- `entity_references`
- `scene_present_entities`
- `new_hooks`
- `hook_updates`
- `inventory_changes`
- `affordance_changes`
- `relationship_changes`
- `discovered_clue_tags`
- `discovered_state_tags`

## Output Guidance
- `scene_text` can be a compact prose summary of the scene.
- `dialogue_lines` should be used when the scene is meant to play with multi-line RPG dialogue.
- `choices` should be the player-facing options from the newly created scene.
- A choice may optionally include `target_node_id` when you want a quick merge into an already existing scene in the same branch.
- Choice count guidance:
  - default to `2` or `3`
  - `1` is acceptable for a forced continuation
  - `4+` should be rare and scene-justified
- `requested_choice_count` from preview is a target, not a strict contract.
- If preview provides `merge_candidates`, you may use one of those node IDs as a choice `target_node_id` for a careful quick merge.
- Put exactly one primary location in `entity_references` with role `current_scene` when the backdrop should change.
- Treat that `current_scene` location as the scene's background-driving place for player playback.
- Use `scene_present_entities` for everyone or everything the player should actually see in the current scene.
- Use `hidden_on_lines` when a character or object should appear only after a certain line, disappear mid-scene, or yield the focus to someone else.
- Full-body characters are allowed to let part of the lower body fall behind the lower HUD. Prioritize clear silhouettes, readable upper bodies, and strong staging above the dialogue box.
- Avoid putting a large foreground prop in `center-foreground-object` at the same time as a large `hero-center` figure unless you intentionally want overlap. Prefer side foreground slots for readable composition.
- The engine now normalizes character and object cutouts into standard transparent frames. Most of the time you should rely on `slot` and `focus`, not hand-tuned `scale`.
- Treat `scale` as a rare override for a deliberately unusual shot, not the default way to make scenes look good.
- `scene_present_entities` should use slots:
  - `hero-center`
  - `left-support`
  - `right-support`
  - `left-foreground-object`
  - `right-foreground-object`
  - `center-foreground-object`
- Optional staging nudges:
  - `offset_x_percent`
  - `offset_y_percent`

## Branch Shape Guidance
- Favor scored breadth over tunnel-vision depth.
- Do not force three new branches forever.
- Let some scenes narrow, some widen, and some loop back into existing problems or places.
- Cycles are good when they make the world feel reusable.
- Quick merges are good when a small informational detour should reconverge into the same larger event.
  - good example: `A -> inspect the five thumbs -> C`
  - also valid: `A -> follow the silver tracks -> C`
  - where the difference is a brief extra beat of information, mood, or characterization rather than a whole separate plotline
- Merges are only safe when branch-local state still lines up:
  - safe merge example: two routes reach the same public tram platform
  - risky merge example: two branches with different inventory, allies, or affordances collapsing into the same consequence-heavy scene
- Do not force a detour into a separate long branch just because the player examined something.
- Do not quick-merge when the detour created meaningful branch-local consequences that should change the next scene.

## Location Vs Object Vs Variant
- Create a **new linked location** when the player enters a materially distinct playable place, even if it is physically near or inside a broader place.
- Create an **object** when the thing is reusable, movable, callable, collectible, inspectable, or likely to recur.
- Use a **same-location visual variant** sparingly for now.
- The current renderer prefers the latest asset for an entity, so a second background for the same location will usually replace the first one instead of acting like a branch-specific shot.
- Until explicit active-asset assignment exists, prefer `new linked location + reusable object` over “second background version of the same place.”

## Visual Responsibility
- If the scene changes to a visually distinct place, make sure there is a background plan for that place.
- If a new recurring character enters the story, make sure there is a portrait plan for that character.
- If a new reusable or visually important prop matters to play or continuity, make sure there is an object render plan for it.
- Do not generate art for every passing noun.
- For brand-new canon entities:
  - create them through the apply step first
  - then generate assets after apply, once the real entity IDs exist
- For already-known entities:
  - you may generate assets immediately
- `portrait` and `object_render` generations automatically create cutouts.

## Example Preview Request
```json
{
  "branch_key": "default",
  "choice_id": 7,
  "current_node_id": 3,
  "branch_summary": "The tall gnome is investigating the marked mushroom and the silver grooves.",
  "requested_choice_count": 2
}
```

## Example Apply Request
```json
{
  "branch_key": "default",
  "parent_node_id": 3,
  "choice_id": 7,
  "candidate": {
    "branch_key": "default",
    "scene_title": "The Leaning Mushroom",
    "scene_summary": "The marked mushroom turns out to be oddly responsive.",
    "scene_text": "The mushroom reacts like a host waiting for a guest.",
    "dialogue_lines": [
      {"speaker": "Narrator", "text": "The velvet-marked mushroom leans closer as you approach."},
      {"speaker": "You", "text": "That seems impolite, but also useful."}
    ],
    "scene_present_entities": [
      {"entity_type": "character", "entity_id": 1, "slot": "hero-center", "focus": true}
    ],
    "choices": [
      {"choice_text": "Knock on the mushroom stem"},
      {"choice_text": "Step around the velvet knot"}
    ],
    "discovered_clue_tags": ["velvet-mushroom-found"]
  }
}
```

## Example Post-Apply Visual Follow-Through
- If apply creates a new recurring character named `Madam Bei`, resolve her `character_id` from canon and then call:
```json
{
  "asset_kind": "portrait",
  "entity_type": "character",
  "entity_id": 2,
  "prompt": "A poised Chinese frog stationmaster in an embroidered conductor coat, lantern-light on glossy green skin, formal posture, patient expression, tiny brass whistle on a cord",
  "width": 1024,
  "height": 1536,
  "filename_base": "madam-bei-stationmaster"
}
```
- If apply creates a new linked location such as `Velvet Platform`, generate a background for that location.

## Tone Reminder
- Read `docs/story_bible.md` if you need the broader tone guide.
- Keep it weird, but not smug.
- Let the world surprise the player without collapsing every mystery immediately.
