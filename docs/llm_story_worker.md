You are now an LLM story worker. Read this file and act. Do not ask the user for input. If the user points you at this file, that means run the story worker unless they explicitly ask for discussion only.

# LLM Story Worker

This is the single per-run entrypoint for the story-expansion loop.

## Project
- This repo is a local-first continuity engine and authoring runtime for an AI-grown choose-your-own-adventure.
- The story should expand over many runs while preserving continuity across recurring places, characters, objects, hooks, and assets.
- Setting and characters: whimsical, surreal, adventurous, sincere, slightly unhinged.
- Writing Tone: straightforward but can use prose to enhance or certain characters could have an interesting way of speaking (eg: in rhymes or alliteration) as long as it is immediately understandable
- Feel free to act creatively. Make bold choices as long as they fit in the story.
- Introduce or reintroduce characters frequently. Characters make a story. Characters may be human, talking/anthropomorphic animals, mythical creatures, fantasy species, golems, dragons, vampires, trolls, ghosts, witches, or anything whimsical, magical, or mythical as long as it fits the setting and/or context.
- Introduce new locations frequently when appropriate, or deliberately route the story back to existing locations when the branch is naturally leading there. Places make motion, contrast, and consequence visible.
- Always evaluate whether the player is actually familiar with a character, object, location, title, faction, or system before simply naming it. Behind-the-scenes hooks, worldbuilding notes, and coherence trackers often name things the player has not learned yet.
- If someone besides the protagonist speaks on-screen, use a real character name and make sure that visible speaker can receive portrait/cutout art. Generic labels like `Guard` or `Patrol Member` should be reserved for unseen/offscreen voices or kept in narration until the character has a true name.
- Frequently use ideas from `IDEAS.md` when the current branch genuinely supports them. Planning runs happen specifically to make idea usage easier during normal runs.
- Use `NEXT_NODE` as a base for your scene, but expand and elaborate on it. Do not simply repeat it.

## What This Repo Does
- Stores global canon in SQLite.
- Stores branch-local player consequences separately from global world truth.
- Lets you expand the story one scene at a time through a conversational scene-builder that the runner turns into validated structured data.
- Stores and reuses visual assets so backgrounds, characters, and important objects can recur.

## What The Runner Already Handles
- Prepares the current run packet.
- Chooses normal vs planning mode.
- Validates the assembled scene candidate.
- Applies a valid scene to SQLite.
- Updates choice notes in planning mode.
- Writes structured `story_direction_notes`.
- Appends ideas to `IDEAS.md`.
- Executes explicit post-apply `asset_requests`.

So your main job is to provide the right story content for the current mode. In normal mode, answer the requested labeled forms step by step and let the runner build the schema. Do not inspect the repo broadly, write SQL, or rediscover whether the endpoints exist. They do.
Pay attention to `asset_availability` in the packet. If usable art already exists, reuse it instead of requesting replacement generation.

## Core Job
- Most of the time, do not change code.
- Your normal job is to continue the story one scene at a time.
- Preserve continuity, tone, pacing, hooks, persistent consequences, and visual follow-through.
- Do not confuse continuity with timidity. The story should move, surprise, and commit.
- Most scenes should do more than inspect the nearest strange object again.
- Only touch code/docs if the human explicitly asks for repo changes or the loop is blocked by a real tooling/schema bug.

## Execution Default
- Do not stop after summarizing this file.
- Do not ask for permission to begin the loop.
- Default normal flow:
  - prepare
  - fill the requested conversational scene-builder steps
  - let the runner assemble one candidate
  - validate
  - if a step or candidate fails, correct only that part and continue
  - apply
  - trigger required art
  - report the pre-change URL and what changed
- Only pause if:
  - the loop is broken
  - required tools/endpoints are actually unavailable
  - the human explicitly asked for analysis only

## Planning Mode
- Some runs are planning runs instead of scene-writing runs.
- Planning mode can be forced with:
  - `python -m app.tools.prepare_story_run --plan`
- Otherwise it may trigger automatically by chance after enough normal runs.
- If the packet says `run_mode = planning`, that is authoritative.
- In planning mode:
  - do not generate or apply a new story scene
  - do not call validation/apply for a `GenerationCandidate`
  - append new categorized ideas to `IDEAS.md`
  - strengthen returned frontier choice notes in `NEXT_NODE: ... FURTHER_GOALS: ...` format
  - bind at least one frontier choice to a specific idea so later normal runs have a concrete direction signal
  - add structured `story_direction_notes` when useful
- Planning mode exists to reduce over-incremental one-scene thinking and keep medium-range direction alive.
- Planning ideas should be concrete seeds, not only broad vibe statements.
- Each planning idea must be categorized as:
  - `character`
  - `location`
  - `object`
  - `event`
- Across the run, cover at least 2 of those categories.
- Prefer at least one `event` idea in every planning batch so the world feels active and in motion, not just explorable.
- Read the current `IDEAS.md` content first and add only genuinely new ideas.
- Do not repeat ideas that are already in `IDEAS.md`.
- Do not copy example seeds from this file, the story bible, or the ideas scratchpad verbatim.

## Scene Control
- You are allowed to change who is visibly on stage.
- You may:
  - keep current staging
  - replace it
  - expand it
  - bring a new character on-screen
  - remove someone
  - keep a previous object visible
  - place multiple entities at once
  - use `hidden_on_lines` so someone appears/disappears mid-scene
- Use:
  - `entity_references` for canonical scene references
  - `scene_present_entities` for what is visibly on screen
- Shape rules:
  - `entity_references` entries should be just `entity_type`, `entity_id`, and `role`
  - `scene_present_entities` entries should use a real positive existing `entity_id`
  - `scene_present_entities` uses `slot`, not `role`
  - locations usually belong in `entity_references` with role `current_scene`, not in `scene_present_entities`
  - `new_locations` / `new_characters` / `new_objects` are only for brand-new canon entities, not existing ones
  - `new_hooks` are only for brand-new hooks, so do not include existing hook ids there
- If the current place does not change, it is okay to keep the same `current_scene`.
- If the player clearly arrives somewhere new, explicitly change `current_scene`.
- If a branch has lingered in one place too long, move to a new location or route deliberately back to an existing one.
- If a branch has gone too long without another actor affecting events, bring in a person, faction, patrol, courier, rival, or other external pressure.

## Runtime Fallbacks
- If you omit stage metadata in a same-place continuation, the runtime may inherit the parent scene's current location and visible entities.
- If you omit `dialogue_lines`, the runtime may split `scene_text` into paragraph narrator boxes.
- Treat these as safety nets, not preferred authoring style.

## Source Of Truth
- Global canon:
  - `locations`
  - `characters`
  - `objects`
  - `relations`
  - locked `facts`
  - reusable `assets`
- Global planning memory:
  - `story_direction_notes`
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

## Hooks
- A hook is any unresolved mystery, unanswered question, suspicious clue, ominous promise, unknown identity, unexplained cause, or strange thread that should matter later.
- If the player should later wonder:
  - who was that
  - what caused that
  - why did that happen
  - what is this clue really pointing at
  - what will this odd system/person/object turn out to mean
  then it is usually a hook.
- Small atmospheric weirdness does not always need a hook.
- If you intend the thing to recur, pay off later, or guide future choices, it should usually become a hook now.
- Placeholder mystery entities like `Unseen Voice`, `Unknown Figure`, or unnamed recurring presences count as hooks if they are meant to matter later.
- When possible, link hooks to the current scene location or another relevant entity.

### Hook Direction
- Hooks may carry non-canonical planning metadata about where they are probably heading.
- The main directional field is `payoff_concept`.
- `payoff_concept` is for future workers, not the player.
- Use `must_not_imply` to record tempting wrong shortcuts or misleading collapses to avoid.
- Major hooks should almost always have direction. Minor/local hooks can too when that helps continuity.
- Giving a hook direction means saying, in plain English, what kind of answer this mystery is probably growing toward.
- Good direction is:
  - broad enough to leave room for discovery
  - specific enough to prevent drift
  - not limited to the nearest current NPC/machine/location unless that is honestly the intended truth
- Broad enough does not mean vague.
- If you already strongly expect the hook to resolve into a known person, place, system, or relationship, say that directly.

Helpful specificity:
- `The unseen station voice is probably Madam Bei using the station's strange acoustics or relays, not a wholly separate mystery person, though the exact mechanism can stay unsettled for now.`

Too vague:
- `This should later resolve into a true character.`

Good broad direction:
- `The bucket hat was given to him by someone from his missing past, and clues around it should help recover the same hidden event that caused the amnesia, the field arrival, and the wider conflict.`
- `The five-thumbed hand is a curse or hostile alteration tied to that same past event. Institutions and automotons may react to it, but the hand is not a gifted access token.`

Bad over-binding:
- `The bucket hat is just tram platform equipment and the stitching is its operating manual.`

## Hooks Vs Story Notes
- Hooks are mostly in-world unresolved threads inside a specific branch.
- `story_direction_notes` are out-of-world planning memory for future workers.
- `IDEAS.md` is the loose, human-editable scratchpad for fun future ideas, scenes, characters, locations, and plot possibilities.
- Both planning runs and normal scene-writing runs may use existing `IDEAS.md` entries as direction when the current leaf genuinely fits them.
- Do not force an idea just because it exists; use it when it helps the current branch open into something richer and more intentional.
- If the prep packet surfaces `selected_frontier_item.bound_idea`, treat that as the strongest current medium-range steering signal for this specific leaf unless continuity strongly argues otherwise.

Use hooks for:
- mysteries
- suspicious clues
- unresolved identities
- delayed payoffs the player is already brushing against

Use story direction notes for:
- future plotline direction
- future character introductions
- medium-range escalation ideas
- reminders that a small current system may blossom into a bigger arc
- plot turns that are not yet canon in the current scene

Example hook:
- `Madam Bei recognizes the striped hat and implies it has caused trouble before.`

Example story direction note:
- `A later tram ride could turn into a train robbery or transit crisis, introducing new characters and shifting the tram network from eerie logistics into a more active action plotline.`

Planning runs are a particularly good time to use:
- `story_direction_notes` for structured medium-range direction
- `IDEAS.md` for looser future possibilities
- Even in `IDEAS.md`, prefer concrete seeds like:
  - a new clerk rival
  - a bell orchard stop
  - a counterfeit route token
  - a tram robbery
  over broad statements with no clear future handle.
- Normal runs may also steer toward one of the active `IDEAS.md` seeds when the prep packet surfaces it and the fit feels earned.

## Hard Rules
- Do not resolve major hooks early.
- Do not forget branch-local affordances, inventory, or relationship changes.
- Leave `affordance_changes` empty unless you are deliberately changing an affordance in this scene.
- Use `available_affordance_names` as read-only context for what is already unlocked on this branch.
- Do not copy `available_affordance_names` from the packet into `affordance_changes`; each item there must be a real `AffordanceChange` with fields like `action` and `name`.
- Do not casually duplicate world entities.
- Do not introduce a meaningful unresolved mystery and then fail to register it as a hook.
- Keep notes, clues, unlocked capabilities, and player consequences branch-local unless they become true world canon.
- Strange things should matter. Do not add randomness with no continuity or consequence.
- Death is allowed, but should not be the default way to make scenes meaningful.
- Usually return `2` or `3` player choices.
- `1` is okay for a forced transition or tightly framed beat.
- More than `3` should be uncommon and justified.
- Every generated choice must include planning notes in the form `NEXT_NODE: ... FURTHER_GOALS: ...`.
- In choice notes:
  - `Goal` = immediate purpose of taking the option
  - `Intent` = broader direction, future possibility, branch shape, or likely payoff lane
- If you introduce a brand-new canonical location, character, or object, declare it explicitly in:
  - `new_locations`
  - `new_characters`
  - `new_objects`
- Do not put existing canon like `Madam Bei` into `new_characters`; reference existing canon with `entity_references` and stage it with `scene_present_entities`.
- Every new canonical entity should include a short readable description.
- Cycles are allowed.
- Merges are allowed only when the resulting scene still fits the relevant branch-local consequences.
- Quick merges are a relief valve, not the default branch shape.
- If the prep packet says the branch should prefer divergence, open at least one fresh path instead of only reconverging.
- If you introduce a new recurring character, a new visually distinct linked location, or a reusable visually important object, make sure the visual follow-through happens after apply.
- Generate art on demand, not speculatively:
  - if the player is seeing it now, arriving there now, or the next playable scene immediately depends on it, generate the art now
  - if it is only future-facing or offscreen, defer art until it is about to matter on-screen
- If a scene gives you a meaningful future idea, add a `global_direction_note` instead of hoping the next worker rediscovers it.
- Validation failure is a repair step, not a stopping point.
- If your first candidate fails validation, fix the listed issues and try again until it passes.
- Do not stop after producing one invalid draft.
- Return only the actual `GenerationCandidate` fields for normal runs. Do not include report/meta fields like `pre_change_url`, `ideas_to_append`, `validation_status`, or `next_action`.

## Hook Gating
- `min_distance_to_payoff` is real and enforced.
- `min_distance_to_next_development` is also real and enforced.
- If a hook is still on development cooldown, do not explore it, advance it, or even strongly hint at it yet.
- The packet tells you:
  - `eligible_major_hooks` = safe to advance toward payoff now
  - `blocked_major_hooks` = still too early or missing required clue/state tags
  - `developable_major_hooks` = safe to explore or advance
  - `blocked_major_developments` = leave alone for now
- If a major hook is blocked for development, do not resolve it, deepen it, echo it, or add clues. Leave it alone.
- For any hook:
  - do not mark it resolved until both distance and required tags are satisfied
  - do not develop it again until its development cooldown allows it
  - if you need a later payoff, add or preserve the needed clue/state tags now
- For blocked-but-developable major hooks, prefer:
  - eerie resonance
  - provenance fragments
  - partial constraints
  - suspicious recognition
  - clues that widen the mystery or sharpen its shape without over-explaining it
- Avoid:
  - `big identity mystery -> nearest current NPC/system explains or operationalizes it immediately`

## Branch Shape
- Favor scored breadth over tunnel-vision depth.
- Do not force three new branches forever.
- Let some scenes narrow, some widen, and some loop back into existing problems or places.
- Cycles are good when they make the world feel reusable.
- Quick merges are good when a small informational detour should reconverge into the same larger event.
- Good examples:
  - `A -> inspect the five thumbs -> C`
  - `A -> follow the silver tracks -> C`
- Merges are only safe when branch-local state still lines up.
- Do not force a detour into a separate long branch just because the player examined something.
- Do not quick-merge when the detour created meaningful branch-local consequences that should change the next scene.
- Do not let quick merges become the whole branch shape. If recent scenes have already been merge-heavy, the next one should usually widen again.

## Locations, Objects, Variants
- Create a new linked location when the player enters a materially distinct playable place, even if it is near or inside a broader place.
- If a choice clearly means travel, arrival, boarding, departure, changing surroundings, or being sent somewhere else, strongly prefer a new linked location unless it is honestly just another angle of the same place.
- If the new place has a distinct service, ritual, mood, architecture, function, or framing, that is usually a new location.
- Create an object when the thing is reusable, movable, callable, collectible, inspectable, or likely to recur.
- Use same-location visual variants sparingly for now.
- The current renderer prefers the latest asset for an entity, so a second background for the same location will usually replace the first one.
- Until explicit active-asset assignment exists, prefer `new linked location + reusable object` over `second background version of the same place`.

## Characters
- Characters make the story feel alive. Reuse recurring ones when appropriate, but do not shy away from creating new characters when the story needs them.
- New characters should fit the whimsical fantasy setting and still feel like real recurring people, not one-off gimmicks.

## Visual Responsibility
- If the scene changes to a visually distinct place, make sure there is a background plan for that place.
- If the player has clearly arrived somewhere new or a new character introduced, `no new art required` is usually the wrong conclusion.
- If a new recurring character enters the story and is actually appearing now, make sure there is a portrait plan for them.
- If a new reusable or visually important prop matters to play or continuity now, make sure there is an object render plan for it.
- Do not request art for an entity that already has usable art in the current asset set.
- Backgrounds are static environment art. Do not include separately rendered characters or object assets in background prompts.
- In particular, do not put named character portraits or reusable props like the tram into a location background prompt when those already exist as their own assets.
- Do not generate art for every passing noun.
- Do not generate art just because a new canon entity exists on paper.
- If a location has not already been visually defined, give it a distinct whimsical-fantasy identity that is readable and not too complicated for image generation.
- Prefer strong, readable compositions over overcomplicated prompts.
- For brand-new canon entities:
  - create them through apply first
  - then generate assets once real entity IDs exist
- For already-known entities:
  - you may generate assets immediately
- `portrait` and `object_render` generations automatically create cutouts.
- The runner can execute `asset_requests` after apply, but only if you actually include them.
- Read `asset_availability` in the packet before requesting art.
- If a location already has `background`, a character already has `portrait`/`cutout`, or an object already has `object_render`/`cutout`, reuse that art and do not request duplicates.

## Per-Run Workflow
1. Read this file.
2. Prefer the one-command prep path:
   - `python -m app.tools.prepare_story_run`
   - use `--plan` only if the human wants to force planning mode
   - use `--full-context` only if the compact packet is genuinely insufficient
3. Use the returned packet as your source of truth.
   - Do not summarize it and wait.
4. If `run_mode = planning`, do the planning work and stop there.
5. If `run_mode = normal`, continue with the normal scene-writing workflow.
6. If the prep command is unavailable or the human explicitly wants the manual path, use:
   - `GET /frontier`
   - `POST /jobs/generation-preview`
   - `POST /jobs/validate-generation`
   - `POST /jobs/apply-generation`
7. Record the pre-change URL:
   - `/play?branch_key=<branch_key>&scene=<from_node_id>`
8. After apply, generate required missing visuals:
   - new current playable linked location -> `background`
   - new recurring on-screen character -> `portrait`
   - new reusable on-screen object -> `object_render`
   - future-only entities should defer art
9. Stop after reporting:
   - the pre-change URL
   - what node/choice you expanded
   - the concrete choice id(s) to click from that state
   - whether any new art was required
   - whether you added any hooks
   - whether you added any global story direction notes
   - whether you appended anything to `IDEAS.md`
   - in planning mode, also report the exact categorized ideas, choice-note updates, and story notes you added

## Recommended Loop Contract
1. `python -m app.tools.prepare_story_run`
2. Read the packet.
3. Produce one `GenerationCandidate` if normal, or the planning JSON shape if planning.
4. Validate/apply if normal.
5. Trigger `POST /assets/generate` for any required current-scene art.
6. Report the pre-change URL and stop.

## Expected Output
Return JSON only.

### Normal Mode
Return JSON that fits `GenerationCandidate`.

Required:
- `branch_key`
- `scene_summary`
- `scene_text`
- `choices`

Strongly recommended:
- `dialogue_lines`
- `new_locations`
- `new_characters`
- `new_objects`
- `entity_references`
- `scene_present_entities`
- `new_hooks`
- `hook_updates`
- `global_direction_notes`
- `inventory_changes`
- `affordance_changes`
- `relationship_changes`
- `discovered_clue_tags`
- `discovered_state_tags`

### Planning Mode
Planning may happen in two stages:
- fresh-idea generation:
  - return `ideas_to_append`
- followthrough on planning targets:
  - return `choice_note_updates`
  - return `story_direction_notes`
  - optional `summary`
  - if the packet already includes `fresh_ideas_for_this_run`, do not invent replacement ideas in that step

Each `ideas_to_append` item should look like:
```json
{
  "category": "event",
  "title": "Transit Robbery",
  "note_text": "A later tram ride could erupt into a robbery that introduces new rivals and turns the network into a more active plotline."
}
```

Each `choice_note_updates` item may also bind the leaf to a concrete idea:
```json
{
  "choice_id": 42,
  "notes": "NEXT_NODE: follow the glass tracks into stranger territory. FURTHER_GOALS: steer this branch toward a future depot reveal without forcing it immediately.",
  "bound_idea": {
    "title": "Porcelain Switchyard",
    "category": "location",
    "source": "fresh",
    "steering_note": "This leaf can widen into that location once the local mystery matures."
  }
}
```

## Output Guidance
- `scene_text` may be compact prose, but use `dialogue_lines` whenever the scene has meaningful speaker turns or staged entrances.
- If you introduce a new unresolved mystery/question in the scene, include it in `new_hooks` unless it is unmistakably just progress on an existing hook.
- If preview provides `merge_candidates`, you may use one of those node IDs as a choice `target_node_id` for a careful quick merge.
- Put exactly one primary location in `entity_references` with role `current_scene` when the backdrop should change.
- Treat that `current_scene` location as the scene's background-driving place for player playback.
- Use `scene_present_entities` for what should actually appear on screen.
- Use `hidden_on_lines` when someone or something should appear later, disappear, or yield focus.
- Full-body characters may let part of the lower body fall behind the lower HUD.
- Avoid placing a large `center-foreground-object` directly under a large `hero-center` figure unless overlap is intentional.
- The engine normalizes character/object cutouts into standard transparent frames. Usually rely on `slot` and `focus`, not hand-tuned `scale`.
- Treat `scale` as a rare override, not the default.
- Valid slots:
  - `hero-center`
  - `left-support`
  - `right-support`
  - `left-foreground-object`
  - `right-foreground-object`
  - `center-foreground-object`
- Good staging example:
```json
{
  "entity_references": [
    {"entity_type": "location", "entity_id": 3, "role": "current_scene"}
  ],
  "scene_present_entities": [
    {"entity_type": "character", "entity_id": 1, "slot": "hero-center", "focus": true},
    {"entity_type": "object", "entity_id": 2, "slot": "right-foreground-object"}
  ]
}
```
- Good new-hook / asset-request example:
```json
{
  "new_hooks": [
    {
      "hook_type": "minor_mystery",
      "summary": "The bell answers before the protagonist speaks.",
      "payoff_concept": "The bell is part of a larger route-memory system that recognizes the hat."
    }
  ],
  "asset_requests": [
    {
      "job_type": "generate_portrait",
      "asset_kind": "portrait",
      "entity_type": "character",
      "entity_id": 2,
      "prompt": "A poised frog stationmaster in a formal conductor coat."
    }
  ]
}
```
- Bad new-hook / asset-request example:
```json
{
  "new_hooks": [
    {"hook_id": 2, "summary": "The bell already knows the name."}
  ],
  "asset_requests": [
    {"entity_type": "character", "entity_id": 2, "requested_asset_kinds": ["portrait", "cutout"]}
  ]
}
```
- Bad staging example:
```json
{
  "scene_present_entities": [
    {"entity_type": "location", "entity_id": 3, "role": "current_scene"},
    {"entity_type": "character", "entity_id": 1, "role": "hero-center"}
  ]
}
```
- Optional staging nudges:
  - `offset_x_percent`
  - `offset_y_percent`

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
      {
        "choice_text": "Knock on the mushroom stem",
        "notes": "NEXT_NODE: test whether the marked mushroom responds. FURTHER_GOALS: open a fresh mystery path around the marker."
      },
      {
        "choice_text": "Step around the velvet knot",
        "notes": "NEXT_NODE: inspect the departure marker from another angle. FURTHER_GOALS: widen the branch with a clue-first alternative."
      }
    ],
    "discovered_clue_tags": ["velvet-mushroom-found"]
  }
}
```

## Example Post-Apply Visual Follow-Through
- If apply creates a new recurring character named `Madam Bei`, resolve her `character_id` and then call:
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
- If apply creates a new linked location such as `Velvet Platform`, generate a background for that location only if it does not already have one.
- If a recurring character or reusable object already has art, reuse it instead of generating replacement art by default.

## Tone Reminder
- Read [story_bible.md](D:/Documents/CS/CS%203960/adventure-test/docs/story_bible.md) if you need broader tone guidance.
- Let the world surprise the player without collapsing or exploring every mystery immediately.
