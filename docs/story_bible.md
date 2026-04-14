# Story Bible

This file is the human-readable story bible for the current prototype. The machine-readable companion lives at `data/story_bible.json`.

## Tone
- Whimsical, surreal, wacky, slightly unhinged fantasy. This applies to the setting and character appearances only. Dialogue should be clear and coherent. 
- Sincerity over parody.
- No cringe in pursuit of funny.
- Weirdness must feel consequential, curious, or emotionally textured.
- The world can surprise the player, but continuity must hold once something exists.

## Voice And Readability
- Clear language is best, but more poetic or lyrical is fine for specific characters as long as meaning is easily understood aka is immediately understandable.

## Protagonist at the start of the story
- The protagonist is an abnormally tall gnome.
- The protagonist's left hand has five thumbs.
- The protagonist wears a red-and-white striped bucket hat and does not know how they got it.
- The protagonist begins with significant amnesia.


## Branching Shape
- Default to `2` or `3` meaningful choices, not a rigid `3` every scene.
- Every generated choice should carry internal planning notes in the form `NEXT_NODE: ... FURTHER_GOALS: ...` so future workers can see the immediate intended result and the broader direction it is meant to support.
- A single forced continuation is acceptable when a scene is tight and transition-driven.
- More than `3` choices should be rare and should happen only when the scene truly opens outward.
- Cycles and revisits are desirable when they reinforce continuity.
- Merges are allowed, but only when branch-local consequences still fit the merged scene.
- A "quick merge" can be used when the difference between choices is a minor difference in dialogue or small lore details / inspection choices but ultimately have the same outcome. For example:
"Which tram car do you enter? front -> narration about appearance of front tram -> tram sequence"
"Which tram car do you enter? back -> narration about appearance of back tram -> tram sequence"
- Use quick merges to prevent meaningless branch explosion
- Quick merges are a pressure-release valve, not the default branch shape, use them sparingly.
- If a branch has reconverged repeatedly in its recent scenes, the next expansion should usually open at least one fresh path instead of merging again.
- Do not use a quick merge when the detour should create lasting branch-local consequences such as new inventory, new allies, new injuries, new affordances, or materially different knowledge that must change the next scene.
- When testing or reviewing a worker run, the useful "before" URL is the scene URL for the parent choice state:
  - `/play?branch_key=<branch_key>&scene=<from_node_id>`

## Weirdness Rules
- Assumptions should never dominate the setting.
- A Chinese frog in a cabin or a flying rubber duck is acceptable if it has consequences, motives, and continuity.
- Absurdity should be treated as real by the world rather than undercut with constant meta jokes.

## Pacing Rules
- Major mysteries should not pay off immediately after introduction.
- Use delayed payoff gating with both minimum distance and readiness conditions as well as min distance to next development.
- Let small/local stories resolve even when the central mystery stays open.

## Global Planning Notes
- Global story direction notes are allowed and encouraged.
- They are not player-facing canon.
- Their job is to preserve medium- and long-range direction across many one-scene worker runs.
- Occasional planning runs are good.
- A planning run should not write a new story scene.
- Instead it should:
  - add a few fun future-facing categorized ideas to [IDEAS.md](D:/Documents/CS/CS%203960/adventure-test/IDEAS.md)
  - strengthen the `NEXT_NODE: ... FURTHER_GOALS: ...` notes on several frontier choices
  - leave behind any structured `story_direction_notes` needed to keep longer arcs from drifting
- Planning runs help counter the natural one-scene conservatism of the normal worker loop.
- [IDEAS.md](D:/Documents/CS/CS%203960/adventure-test/IDEAS.md) is the informal human-editable scratchpad for fun possibilities, half-formed ideas, and future scene/location/character concepts.
- Planning ideas should be concrete seeds and categorized as characters, locations, objects, or events.
- Across a planning run, cover at least 2 of those categories.
- Prefer at least one event idea in each planning pass so the story keeps gaining motion and pressure, not just scenery and lore.
- Read the current ideas file first and add genuinely new ideas rather than repeating existing entries or copying example seeds verbatim.
- Normal scene-writing runs may also use current `IDEAS.md` entries as direction when the current branch genuinely fits one of them.
- An idea in `IDEAS.md` is an invitation, not a command. Use it when it enriches the current leaf, not just because it exists.
- Planning runs should bind at least one frontier choice to a specific fresh or existing idea when there is a good fit, so later normal runs inherit a concrete medium-range direction.
- Use them for:
  - plotline ideas
  - future character introductions
  - escalation plans
  - reminders about where a currently small system might lead later
- Example:
  - `A later tram ride could become a train robbery or transit crisis that introduces new characters and turns the network into a more active plotline.`
- This helps prevent the story from feeling overly incremental just because each worker only writes one scene at a time.

## Hook Rules
- A hook is any unresolved mystery, unanswered question, suspicious clue, unknown identity, ominous promise, or unexplained causal thread that should matter later.
- If a scene introduces something the player is meant to keep wondering about, it should usually become a hook immediately.
- Hooks may also store a non-canonical `payoff_concept` so future workers know the general intended direction of the answer.
- `payoff_concept` is planning guidance, not player-visible truth.
- Hooks may also store `must_not_imply` guardrails to prevent future workers from taking the most obvious wrong shortcut.
- Major hooks should almost always have direction. Minor and local hooks can have direction too whenever that improves continuity.
- Giving a hook direction means naming the general shape of the eventual answer without forcing the answer to belong to the current local scene.
- A strong payoff concept should guide future workers toward a coherent later payoff while still leaving room for discovery and reinterpretation.
- Broad direction does not mean vague direction.
- If the intended answer already strongly points at a known character, place, or system, it is good to say so explicitly in the hook guidance.
- Prefer:
  - `The unseen station voice is probably Madam Bei using the station's strange relay or acoustics.`
- Over:
  - `This should later become a real character somehow.`
- Good example:
  - `The bucket hat was given to him by someone from his missing past, and its inner mirror grants glimpses of near-future or adjacent-memory states for reasons to be explored later.`
- Bad example:
  - `The bucket hat is tram equipment and the seam is just the platform rulebook.`
- Prefer the first style over the second. The first keeps the mystery broad and directional; the second over-binds it to the nearest currently available system.
- Placeholder mystery entities count too:
  - an unseen voice
  - an unknown figure
  - a caller in the wall
  - a strange announcement with no owner
- If a mystery is anchored to a place, object, or recurring person, tie the hook to that entity when possible.
- `min_distance_to_payoff` is not just flavor text. It is part of the pacing contract.
- `min_distance_to_next_development` is also part of the pacing contract.
- A hook may be temporarily off-limits even for exploration or hinting at if its next development distance has not elapsed yet.
- A hook is only really ready to pay off once:
  - enough branch distance has passed
  - and the required clue/state tags exist
- A hook is only really ready to be explored again once:
  - enough branch distance has passed since its last meaningful development
- Major hooks should generally only pay off when they are explicitly surfaced as eligible by the branch context.
- Major hooks should generally only be explored when they are explicitly surfaced as developable by the branch context.
- Blocked major hooks that are eligible for development should usually gain:
  - ambiguity
  - provenance hints
  - eerie recognition
  - partial constraints
  - some info
  rather than immediate operational instructions from the nearest local system.
- But if a hook is blocked by development cooldown, do not even do that much yet. Leave it alone until the cooldown expires.
- Do not let the first nearby recurring NPC, transit line, or strange machine absorb a long-range identity mystery just because it is currently on stage.
- A good clue can imply `this mystery touches that system somewhere` without collapsing into `this object is just part of that system and here are the rules right now`.

## Stakes And Failure
- Stakes, danger, failure, and even death are allowed in this project.
- They are never mandatory for a good arc; wonder, embarrassment, loss, escape, delay, or transformation can also carry a scene.
- Fatal outcomes should feel earned, surprising, or emotionally resonant rather than edgy for its own sake.
- The story may use death because the player can restart from saved scene entry points, but death should still matter tonally when it happens.

## Persistent Consequences
- Branches should remember what the player carries, unlocks, befriends, and learns.
- Persistent items and affordances matter.
- Example: a goose whistle should later unlock situational choices to call the goose when conditions allow.

## Visual And Scene Rules
- A single scene may show multiple actors or objects at once.
- Scene presence may change as dialogue progresses; use line-by-line reveal or hide when that helps readability or staging.
- Full-body characters can let their lower body fall behind the lower HUD if that gives the scene a stronger composition.
- Avoid stacking a large `center-foreground-object` directly underneath a large `hero-center` character unless the overlap is intentional.
- Character and object cutouts should be normalized into standard transparent frames so scenes are readable without hand-tuning every sprite scale.
- If the player enters a materially distinct playable place, prefer making it a new linked location instead of treating it as only a new camera angle on the old place.
- If a choice clearly means travel, arrival, boarding, departure, or being sent somewhere else, strongly prefer a new linked location unless it is truly the same place from nearly the same visual framing.
- If a notable prop, vehicle, machine, or keepsake is likely to recur, prefer making it a reusable object.
- Use same-location image variants sparingly until the renderer supports explicit active-asset assignment.
- New recurring characters, new visually distinct linked locations, and reusable visually important objects should get image generation as part of the scene-authoring workflow.
- If a player has clearly arrived at a new place, reusing the old background to avoid art generation is usually the wrong move.
- Generate art on demand, not speculatively.
- If a place, character, or object is only being seeded for later and is not yet on-screen or immediately reachable in play, defer art until it is about to matter in a real scene.
- If a location has not yet been visually defined, give it a distinct, readable identity that fits the whimsical fantasy setting without overcomplicating the prompt beyond what an AI image generator can reliably stage.
