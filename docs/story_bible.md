# Story Bible

This file is the human-readable story bible for the current prototype. The machine-readable companion lives at `data/story_bible.json`.

## Tone
- Whimsical, surreal, wacky, slightly unhinged fantasy.
- Sincerity over parody.
- No cringe in pursuit of funny.
- Weirdness must feel consequential, curious, or emotionally textured.
- The world can surprise the player, but continuity must hold once something exists.

## Protagonist Reset
- The protagonist is an abnormally tall gnome.
- The protagonist's left hand has five thumbs.
- The protagonist wears a red-and-white striped bucket hat and does not know how they got it.
- The protagonist begins with significant amnesia.

## Story Shape
- Main mystery beats should be lightly scaffolded, not fully outlined.
- Early story should scatter clues and strange invitations into the world.
- Middle story should connect patterns, recurring entities, and reusable affordances.
- Late story may begin paying off identity and origin mysteries if the groundwork exists.

## Branching Shape
- Default to `2` or `3` meaningful choices, not a rigid `3` every scene.
- A single forced continuation is acceptable when a scene is tight and transition-driven.
- More than `3` choices should be rare and should happen only when the scene truly opens outward.
- Cycles and revisits are desirable when they reinforce continuity.
- Merges are allowed, but only when branch-local consequences still fit the merged scene.
- A **quick merge** is a normal and useful pattern:
  - Example: `A -> examine your five thumbs -> C`
  - and also `A -> follow the silver tracks -> C`
  - where `C` is still the same larger event because the branch difference is only a small informational detour
- Quick merges are especially good for:
  - minor inspection choices
  - small lore reveals
  - brief tone or character beats
  - optional extra information before a larger event that would happen either way
- Use quick merges to prevent meaningless branch explosion when the real dramatic structure is still converging.
- Do not use a quick merge when the detour should create lasting branch-local consequences such as new inventory, new allies, new injuries, new affordances, or materially different knowledge that must change the next scene.
- When testing or reviewing a worker run, the useful "before" URL is the scene URL for the parent choice state:
  - `/play?branch_key=<branch_key>&scene=<from_node_id>`

## Weirdness Rules
- Assumptions should never dominate the setting.
- A Chinese frog in a cabin or a flying rubber duck is acceptable if it has consequences, motives, and continuity.
- Absurdity should be treated as real by the world rather than undercut with constant meta jokes.

## Pacing Rules
- Major mysteries should not pay off immediately after introduction.
- Use delayed payoff gating with both minimum distance and readiness conditions.
- Let small/local stories resolve even when the central mystery stays open.

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
- If a notable prop, vehicle, machine, or keepsake is likely to recur, prefer making it a reusable object.
- Use same-location image variants sparingly until the renderer supports explicit active-asset assignment.
- New recurring characters, new visually distinct linked locations, and reusable visually important objects should get image generation as part of the scene-authoring workflow.
