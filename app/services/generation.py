from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.models import GenerationCandidate
from app.services.assets import AssetService
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver
from app.services.story_notes import StoryDirectionService
from app.services.story_graph import StoryGraphService
from app.services.worldbuilding import WorldbuildingService


class LLMGenerationService:
    """Builds story-generation context, prompt scaffolding, and validation rules."""

    MYSTERY_MARKER_PATTERNS = [
        re.compile(r"\bunseen voice\b", re.IGNORECASE),
        re.compile(r"\bunknown voice\b", re.IGNORECASE),
        re.compile(r"\bmysterious voice\b", re.IGNORECASE),
        re.compile(r"\bunseen figure\b", re.IGNORECASE),
        re.compile(r"\bunknown figure\b", re.IGNORECASE),
        re.compile(r"\bunknown speaker\b", re.IGNORECASE),
        re.compile(r"\bsomeone\b.*\b(from inside|in the dark|behind|inside the stalk)\b", re.IGNORECASE),
    ]
    PLACEHOLDER_SPEAKER_PATTERNS = [
        re.compile(r"\bunseen\b", re.IGNORECASE),
        re.compile(r"\bunknown\b", re.IGNORECASE),
        re.compile(r"\bmysterious\b", re.IGNORECASE),
    ]
    NONVISUAL_SPEAKER_PATTERNS = [
        re.compile(r"\(o\.s\.\)", re.IGNORECASE),
        re.compile(r"\boffscreen\b", re.IGNORECASE),
        re.compile(r"\bover (the )?(radio|speaker|intercom)\b", re.IGNORECASE),
    ]
    CHOICE_NOTES_PATTERN = re.compile(
        r"next_node\s*:\s*(?P<next_node>.+?)\s+further_goals\s*:\s*(?P<further_goals>.+)",
        re.IGNORECASE | re.DOTALL,
    )
    MIN_ENTITY_DESCRIPTION_LENGTH = 12
    PROMPT_ENTITY_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
    INSPECTION_CHOICE_PATTERNS = [
        re.compile(r"\b(look|listen|inspect|examine|touch|read|ask|knock|press|study|feel)\b", re.IGNORECASE),
    ]
    COMMITMENT_CHOICE_PATTERNS = [
        re.compile(r"\b(board|ride|follow|enter|accept|choose|commit|jump|descend|climb|go with)\b", re.IGNORECASE),
    ]
    ENDING_CHOICE_PATTERNS = [
        re.compile(r"\b(die|death|surrender|give up|stay behind|accept capture|let it take you)\b", re.IGNORECASE),
    ]

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.story_bible_path = project_root / "data" / "story_bible.json"
        self.story_bible = json.loads(self.story_bible_path.read_text(encoding="utf-8"))

    def build_context(
        self,
        *,
        branch_key: str,
        canon: CanonResolver,
        branch_state: BranchStateService,
        story_notes: StoryDirectionService,
        worldbuilding: WorldbuildingService,
        story_graph: StoryGraphService,
        focus_entity_ids: list[int],
        current_node_id: int | None,
        branch_summary: str | None,
        requested_choice_count: int,
    ) -> dict[str, Any]:
        premise_facts = [fact for fact in canon.list_facts() if fact["entity_type"] == "world"]
        recurring_entities = branch_state.list_recurring_entities(branch_key)
        branch = branch_state.sync_branch_progress(branch_key)
        projected_depth = int(branch["branch_depth"]) + 1
        active_hooks = branch_state.list_hooks_with_readiness(
            branch_key,
            statuses=["active", "payoff_ready", "blocked"],
            depth_override=projected_depth,
        )
        eligible_major_hooks = branch_state.list_eligible_hooks(
            branch_key,
            importance="major",
            depth_override=projected_depth,
        )
        blocked_major_hooks = branch_state.list_ineligible_hooks(
            branch_key,
            importance="major",
            depth_override=projected_depth,
        )
        developable_major_hooks = branch_state.list_development_eligible_hooks(
            branch_key,
            importance="major",
            depth_override=projected_depth,
        )
        blocked_major_developments = branch_state.list_development_ineligible_hooks(
            branch_key,
            importance="major",
            depth_override=projected_depth,
        )
        current_node = story_graph.get_story_node(current_node_id) if current_node_id is not None else None
        branch_shape = story_graph.describe_branch_shape(
            branch_key,
            branching_policy=self.story_bible.get("branching_policy"),
        )
        merge_candidates = story_graph.list_merge_candidates(
            branch_key,
            exclude_node_ids=[current_node_id] if current_node_id is not None else None,
        )
        global_direction_notes = story_notes.list_notes(statuses=["active", "parked"], limit=12)
        worldbuilding_notes = worldbuilding.list_notes(statuses=["active", "parked"], limit=10)
        frontier_budget_state = story_graph.build_frontier_budget_state(
            branch_key=branch_key,
            branching_policy=self.story_bible.get("branching_policy"),
        )
        arc_exit_candidate = self._compute_arc_exit_candidate(branch=branch, merge_candidates=merge_candidates)

        relevant_locations = [location for location in canon.list_locations() if location["id"] in focus_entity_ids]
        relevant_characters = [character for character in canon.list_characters() if character["id"] in focus_entity_ids]
        relevant_objects = [obj for obj in canon.list_objects() if obj["id"] in focus_entity_ids]
        current_act = self.story_bible["acts"][branch["act_phase"]]

        return {
            "branch_key": branch_key,
            "branch_state": branch,
            "branch_summary": branch_summary,
            "current_node": current_node,
            "story_bible": {
                "title": self.story_bible["title"],
                "tone": self.story_bible["tone"],
                "world_rules": self.story_bible["world_rules"],
                "protagonist": self.story_bible["protagonist"],
                "beat_budget": self.story_bible["beat_budget"],
                "current_act": {
                    "phase": branch["act_phase"],
                    "goal": current_act["goal"],
                    "guidance": current_act["guidance"],
                },
            },
            "premise_facts": premise_facts,
            "focus_entities": {
                "locations": relevant_locations,
                "characters": relevant_characters,
                "objects": relevant_objects,
            },
            "inventory": branch["inventory"],
            "available_affordances": branch_state.list_available_affordances(branch_key),
            "relationship_states": branch["relationships"],
            "branch_tags": branch["tags"],
            "branch_shape": branch_shape,
            "frontier_budget_state": frontier_budget_state,
            "active_hooks": active_hooks,
            "eligible_major_hooks": eligible_major_hooks,
            "blocked_major_hooks": blocked_major_hooks,
            "developable_major_hooks": developable_major_hooks,
            "blocked_major_developments": blocked_major_developments,
            "global_direction_notes": global_direction_notes,
            "worldbuilding_notes": worldbuilding_notes,
            "recurring_entities": recurring_entities,
            "merge_candidates": merge_candidates,
            "arc_exit_candidate": arc_exit_candidate,
            "requested_choice_count": requested_choice_count,
        }

    def build_prompt(self, context: dict[str, Any]) -> str:
        return (
            "You are extending a branching whimsical fantasy world while preserving continuity.\n"
            "Follow the story bible, respect hook pacing, and keep the tone sincere rather than jokey.\n"
            "Use a mix of lyrical narration and clearer spoken dialogue.\n"
            "Narration may be more poetic or uncanny, but spoken dialogue should usually be more grounded and immediately understandable.\n"
            "The player character should usually be one of the clearest voices in the scene.\n"
            "Feel free to act creatively. Make bold choices as long as they fit in the story.\n"
            "Introduce or reintroduce characters frequently. Characters make a story.\n"
            "Introduce new locations frequently when appropriate, or deliberately route the story back to existing locations when the branch is naturally leading there. Places make motion, contrast, and consequence visible.\n"
            "Always evaluate whether the player is actually familiar with a character, object, location, title, faction, or system before simply naming it in playable text. Behind-the-scenes hooks, notes, worldbuilding files, and coherence trackers often name things the player has not learned yet.\n"
            "If someone besides the protagonist speaks on-screen, use a real character name and make sure that visible speaker can receive portrait/cutout art. Generic labels like 'Guard' or 'Patrol Member' should be reserved for unseen or offscreen voices, or kept in narration until the character has a true name.\n"
            "Frequently use ideas from IDEAS.md when the current branch genuinely supports them. Planning runs exist specifically to make idea usage easier during normal runs like this one.\n"
            "This world is fantasy first. Outside the king's brass enumerators and their closely related royal systems, ordinary people, places, tools, and problems should feel magical, folkloric, handmade, organic, and mostly preindustrial rather than high-tech, industrial, or sci-fi.\n"
            "Treat advanced machinery, metallic infrastructure, survey engines, and technical bureaucracy as exceptional pressure textures, not the baseline look of the world.\n"
            "For fit only, not as automatic canon for the current scene, think of whimsical-fantasy textures like Madam Bei the frog tram conductor, Pipkin the elf magic librarian, mushroom fields, and glass villages. Use examples like that as tone guidance, not as permission to insert those exact people or places unless the packet and path support them.\n"
            "Prefer clear weird over murky weird. If a line sounds evocative but you cannot paraphrase it plainly, rewrite it.\n"
            "Be especially clear when a line introduces a clue, a rule, a system behavior, or a consequence.\n"
            "A hook is any unresolved mystery, unanswered question, ominous promise, unknown identity, suspicious clue, or strange causal thread that should matter later.\n"
            "If you introduce a new unresolved mystery or question, create a new_hook immediately unless it is clearly just advancing an already existing hook.\n"
            "If you introduce a placeholder mystery entity such as an unseen voice, unknown figure, or unnamed presence, create a hook for it immediately and link that hook to the current_scene location or another relevant entity whenever possible.\n"
            "If you introduce a brand-new canonical location, character, or object, declare it explicitly in new_locations, new_characters, or new_objects with a short readable description.\n"
            "Do not rely on name-only auto-creation for new canon entities.\n"
            "For major hooks, include a payoff_concept that voices the intended direction of the eventual answer without fully locking every detail. Minor and local hooks may include payoff_concept too when that helps continuity.\n"
            "Use must_not_imply on hooks when there are tempting wrong shortcuts future workers should avoid.\n"
            "A good payoff_concept should describe the general shape of the later answer, not just bind the mystery to whatever system or NPC is immediately available in the current scene.\n"
            "Broad direction does not mean vague direction: if a hook likely resolves into a known character, place, or system, say that directly instead of writing mushy placeholder notes.\n"
            "Major mysteries must not resolve before their minimum distance and readiness conditions.\n"
            "Hooks may also have a cooldown before they are allowed to be developed again. If a hook is blocked for development, do not explore it, advance it, or even strongly hint at it in the new scene.\n"
            "Major hook payoffs are only safe when the relevant hook appears in eligible_major_hooks. If it is still blocked, deepen it without resolving it.\n"
            "Only develop a major hook when it appears in developable_major_hooks. If it appears in blocked_major_developments, leave it alone for now.\n"
            "When a major hook is still blocked, prefer ambiguous clues, provenance fragments, eerie recognition, or partial constraints over explicit procedural instructions, ownership reveals, or local-system explanations unless several prior clues already support that connection.\n"
            "Do not let the first nearby recurring NPC, transit system, or local mechanic swallow a long-range mystery just because it is available now.\n"
            "For any hook, min_distance_to_payoff and required clue/state tags determine whether payoff is allowed. Validation will reject early resolution.\n"
            "Persistent affordances and inventory items remain available in the branch unless explicitly changed.\n"
            "Global direction notes are out-of-world planning memory, not player-facing canon. Use them to keep longer arcs, future characters, and stronger plot direction alive across runs.\n"
            "When you start a medium- or long-range plotline, or you realize a future beat, character, or escalation should probably happen later, add a global_direction_note so future workers do not have to rediscover the idea from scratch.\n"
            "Treat requested_choice_count as a target, not a rigid quota. Usually return 2 or 3 choices, sometimes 1 for a forced beat, and only occasionally 4 or more when the scene genuinely blooms.\n"
            "Cycles are allowed. Careful merges are allowed when branch-local consequences still make sense; do not collapse branches that now depend on different local state.\n"
            "Quick merges are a relief valve, not the default branch shape. If branch_shape.should_prefer_divergence is true, open at least one fresh path instead of only merging into existing scenes.\n"
            "Minor inspection choices should usually reconverge quickly instead of creating a durable new frontier leaf.\n"
            "Do not simply restate the just-taken choice as another choice in the child scene. Advance the situation materially first.\n"
            "Do not repeat the parent summary with only cosmetic wording changes.\n"
            "If an inspection choice names a local prop, marker, knot, placard, seam, or similar focal object, establish it clearly in the scene text first instead of inventing it only in the choice menu.\n"
            "Most multi-choice scenes should include at least one consequential option that is not pure inspection, such as a commitment, location_transition, merge, ending, social move, location change, or response to immediate pressure.\n"
            "If the branch has gone too long without another actor affecting events, bring in a person, faction, patrol, courier, rival, or other external pressure.\n"
            "If the branch has lingered too long in one place, satisfy that in the menu by including at least one choice_class `location_transition` option that promises a later move to a different place when expanded.\n"
            "If frontier_budget_state.pressure_level is soft or hard, prefer merges, closures, and narrow continuation over spawning multiple fresh leaves.\n"
            "When a local beat feels like it is winding down and arc_exit_candidate says a larger merge is plausible, you may use 1-2 transition scenes to close the local arc and route into another compatible storyline.\n"
            "Every choice must include internal planning notes in this form: 'NEXT_NODE: ... FURTHER_GOALS: ...'. NEXT_NODE should state a specific immediate result or situation the next scene should actually deliver. FURTHER_GOALS should state the broader follow-through, medium-range aim, or later pressure the branch should keep in motion.\n"
            "Use NEXT_NODE as a base for your scene, but expand and elaborate on it. Do not simply repeat it.\n"
            "Choices may optionally include choice_class values inspection, progress, commitment, location_transition, or ending.\n"
            "Ending choices are allowed. Death, capture, transformation, dead ends, and hub-return closures are all valid if they fit the scene.\n"
            "If you need to use a recurring canonical character who has not been met on this specific path yet, add a floating_character_introduction with that existing character_id and a short reusable first-meeting beat. Floating introductions are for recurring characters only, not locations or objects.\n"
            "If a quick merge is appropriate, a generated choice may include target_node_id pointing at one of the provided merge_candidates.\n"
            "Use scene_present_entities and hidden_on_lines when actors or objects should appear, disappear, or swap focus during the same scene.\n"
            "If a scene introduces a new recurring character, a new visually distinct linked location, or a reusable visually important object, make the need for art obvious so the post-apply asset pass can generate it once real IDs exist.\n"
            "Generate art on demand, not speculatively. If a place, character, or object is only being set up for later and is not yet on-screen or immediately reachable in play, defer its art until a later scene actually needs it.\n"
            "If usable art already exists for a location background, character portrait/cutout, or object render/cutout, reuse it and do not request duplicate generation.\n"
            "Background prompts must stay static-environment-only and must not name separately rendered characters or reusable props.\n"
            "If a choice clearly means travel, arrival, boarding, departure, or being sent somewhere else, strongly prefer a new linked location unless it is truly the same place from nearly the same visual framing.\n"
            "If the player has clearly arrived somewhere new, reusing the old background just to avoid art generation is usually the wrong choice.\n"
            "Persistent objects are exceptional. Do not create new persistent objects for ordinary props, vehicles, or local scenery unless they are truly gameplay-critical, reusable, or inventory-relevant.\n"
            "Worldbuilding notes describe offscreen pressures like patrols, factions, rumors, automata, or danger escalation. You may use them as a grounded source of surprise and conflict.\n"
            "If a location does not yet have art, give it a distinct whimsical-fantasy identity that stays readable and not overly complicated for image generation.\n"
            "Return structured JSON only with these top-level keys:\n"
            "scene_summary, scene_text, dialogue_lines, choices, transition_nodes, new_locations, new_characters, new_objects, floating_character_introductions, entity_references, scene_present_entities, fact_updates, relation_updates, "
            "new_hooks, hook_updates, global_direction_notes, inventory_changes, affordance_changes, relationship_changes, "
            "asset_requests, discovered_clue_tags, discovered_state_tags.\n"
            f"Context:\n{json.dumps(context, indent=2)}"
        )

    def validate_candidate(
        self,
        *,
        candidate: GenerationCandidate,
        branch_state_service: BranchStateService,
        canon: CanonResolver,
        story_graph: StoryGraphService,
    ) -> dict[str, Any]:
        branch = branch_state_service.sync_branch_progress(candidate.branch_key)
        branch_shape = story_graph.describe_branch_shape(
            candidate.branch_key,
            branching_policy=self.story_bible.get("branching_policy"),
        )
        frontier_budget = story_graph.build_frontier_budget_state(
            branch_key=candidate.branch_key,
            branching_policy=self.story_bible.get("branching_policy"),
        )
        budget_config = ((self.story_bible.get("branching_policy") or {}).get("frontier_budget") or {})
        current_depth = int(branch["branch_depth"])
        projected_depth = current_depth + 1
        state_tags = {row["tag"] for row in branch_state_service.list_branch_tags(candidate.branch_key, tag_type="state")}
        clue_tags = {row["tag"] for row in branch_state_service.list_branch_tags(candidate.branch_key, tag_type="clue")}
        discovered_state_tags = state_tags | set(candidate.discovered_state_tags)
        discovered_clue_tags = clue_tags | set(candidate.discovered_clue_tags)
        issues: list[str] = []
        assets = AssetService(branch_state_service.connection, self.project_root)

        if len(candidate.choices) == 0:
            issues.append("Generation candidate must include at least one choice.")

        current_scene_references = [
            reference for reference in candidate.entity_references
            if reference.role == "current_scene"
        ]
        if len(current_scene_references) > 1:
            issues.append("Generation candidate must declare at most one current_scene location.")
        for reference in current_scene_references:
            if reference.entity_type != "location":
                issues.append("current_scene must always reference a location, never a character or object.")

        entity_proposals = {
            "location": {proposal.name.strip().lower(): proposal for proposal in candidate.new_locations},
            "character": {proposal.name.strip().lower(): proposal for proposal in candidate.new_characters},
            "object": {proposal.name.strip().lower(): proposal for proposal in candidate.new_objects},
        }
        for entity_type, proposals in entity_proposals.items():
            for proposal in proposals.values():
                description = (proposal.description or "").strip()
                if len(description) < self.MIN_ENTITY_DESCRIPTION_LENGTH:
                    issues.append(
                        f"New {entity_type} '{proposal.name}' must include a short readable description."
                    )

        for entity_type, entity_name in self._collect_unknown_named_entities(candidate=candidate, canon=canon):
            if entity_name.strip().lower() not in entity_proposals[entity_type]:
                issues.append(
                    f"New {entity_type} '{entity_name}' must be declared in new_{entity_type}s with a short description."
                )

        new_character_names = {proposal.name.strip().lower() for proposal in candidate.new_characters}
        for intro in candidate.floating_character_introductions:
            character = canon.get_character(int(intro.character_id))
            if character is None:
                issues.append(
                    f"Floating character introduction references unknown character id {intro.character_id}."
                )
                continue
            character_name = (character.get("name") or "").strip().lower()
            if character_name and character_name in new_character_names:
                issues.append(
                    f"Floating character introduction for '{character.get('name')}' conflicts with new_characters. Use one or the other."
                )

        seen_transition_choice_indexes: set[int] = set()
        for transition in candidate.transition_nodes:
            if transition.choice_list_index >= len(candidate.choices):
                issues.append(
                    f"Transition node references choice_list_index {transition.choice_list_index}, but this candidate only has {len(candidate.choices)} choice(s)."
                )
                continue
            if transition.choice_list_index in seen_transition_choice_indexes:
                issues.append(
                    f"Only one transition node may be attached to choice_list_index {transition.choice_list_index}."
                )
                continue
            seen_transition_choice_indexes.add(transition.choice_list_index)
            attached_choice = candidate.choices[transition.choice_list_index]
            if attached_choice.target_node_id is None:
                issues.append(
                    f"Transition node for choice '{attached_choice.choice_text}' is invalid because that choice does not merge into an existing node."
                )
            if len((transition.scene_text or "").split()) < 12:
                issues.append(
                    f"Transition node for choice '{attached_choice.choice_text}' is too thin. Write a real bridging beat, not a fragment."
                )

        merge_choice_count = sum(1 for choice in candidate.choices if choice.target_node_id is not None)
        fresh_choice_count = sum(1 for choice in candidate.choices if choice.target_node_id is None)
        closure_choice_count = 0
        inspection_fresh_count = 0
        inferred_choice_classes: list[str] = []
        consequential_choice_count = 0
        if branch_shape["should_prefer_divergence"] and merge_choice_count > 0 and fresh_choice_count == 0:
            issues.append(
                "This branch has quick-merged too often recently. Open at least one fresh path instead of only merging into existing scenes."
            )
        max_choices_per_node = int(budget_config.get("max_choices_per_node", 5))
        if len(candidate.choices) > max_choices_per_node:
            issues.append(f"Candidate creates {len(candidate.choices)} choices, which exceeds the max of {max_choices_per_node}.")
        for choice in candidate.choices:
            notes = (choice.notes or "").strip()
            if not notes:
                issues.append(
                    f"Choice '{choice.choice_text}' must include planning notes describing NEXT_NODE and FURTHER_GOALS."
                )
                continue
            match = self.CHOICE_NOTES_PATTERN.search(notes)
            next_node = (match.group("next_node") or "").strip() if match else ""
            further_goals = (match.group("further_goals") or "").strip() if match else ""
            if match is None or len(next_node) < 12 or len(further_goals) < 12:
                issues.append(
                    f"Choice '{choice.choice_text}' must use notes in the form 'NEXT_NODE: ... FURTHER_GOALS: ...' with meaningful content."
                )
            choice_class = self._resolve_choice_class(choice)
            inferred_choice_classes.append(choice_class)
            if choice_class == "ending":
                closure_choice_count += 1
                if choice.ending_category is None:
                    issues.append(
                        f"Ending choice '{choice.choice_text}' must include ending_category."
                    )
            if choice_class == "inspection" and choice.target_node_id is None:
                inspection_fresh_count += 1
            if choice_class in {"commitment", "location_transition", "ending"} or choice.target_node_id is not None:
                consequential_choice_count += 1
            elif choice_class == "progress" and self._choice_text_implies_consequence(choice.choice_text):
                consequential_choice_count += 1

        pressure_level = frontier_budget.get("pressure_level")
        allow_second_fresh = bool(budget_config.get("allow_second_fresh_choice_only_for_bloom_scenes", True))
        default_max_fresh = int(budget_config.get("default_max_fresh_choices_per_scene", 1))
        bloom_scene_candidate = self._is_bloom_scene_candidate(
            branch=branch,
            candidate=candidate,
            merge_choice_count=merge_choice_count,
        )
        if pressure_level in {"soft", "hard"} and merge_choice_count + closure_choice_count == 0:
            issues.append("Frontier pressure is high; include at least one merge or closure path in this scene.")
        if pressure_level in {"soft", "hard"} and fresh_choice_count > default_max_fresh and not (
            allow_second_fresh and bloom_scene_candidate and fresh_choice_count == default_max_fresh + 1
        ):
            issues.append(
                f"Fresh branching exceeds the configured limit of {default_max_fresh} for this scene under frontier pressure."
            )
        if pressure_level == "hard" and fresh_choice_count > 0 and not bloom_scene_candidate:
            issues.append("Hard frontier pressure only allows fresh branching for bloom scenes with strong justification.")
        if pressure_level in {"soft", "hard"} and inspection_fresh_count > 0:
            issues.append("Inspection choices should reconverge quickly under frontier pressure instead of opening new durable leaves.")
        _ = consequential_choice_count

        if branch_shape.get("same_location_streak", 0) >= story_graph.SAME_LOCATION_PRESSURE_THRESHOLD:
            if not self._candidate_has_location_transition_choice(candidate):
                issues.append(
                    "This branch has stayed in one place too long. Include at least one choice_class `location_transition` option in the menu so a later expansion can move to a different location."
                )

        beat_budget = self.story_bible["beat_budget"]
        major_hook_updates = 0
        for hook_update in candidate.hook_updates:
            hook = branch_state_service.get_hook(hook_update.hook_id)
            if hook is None:
                issues.append(f"Unknown hook id referenced in hook_updates: {hook_update.hook_id}")
                continue
            readiness = branch_state_service._hook_readiness(hook, projected_depth, state_tags, clue_tags)
            if not readiness["development_eligible"]:
                issues.append(
                    f"Hook {hook_update.hook_id} is still on development cooldown and cannot be explored yet."
                )
            if hook["importance"] == "major" and hook_update.status in {"payoff_ready", "resolved"}:
                major_hook_updates += 1
            if hook_update.status == "resolved":
                if projected_depth < int(hook["introduced_at_depth"]) + int(hook["min_distance_to_payoff"]):
                    issues.append(
                        f"Hook {hook_update.hook_id} resolves before min_distance_to_payoff allows."
                    )
                required_clues = set(hook["required_clue_tags"]) | set(hook_update.add_required_clue_tags)
                required_states = set(hook["required_state_tags"]) | set(hook_update.add_required_state_tags)
                if not required_clues.issubset(discovered_clue_tags):
                    issues.append(f"Hook {hook_update.hook_id} resolves without all required clue tags.")
                if not required_states.issubset(discovered_state_tags):
                    issues.append(f"Hook {hook_update.hook_id} resolves without all required state tags.")

        if major_hook_updates > int(beat_budget["max_major_hook_advances_per_scene"]):
            issues.append("Candidate advances too many major hooks in one scene.")

        proposed_major_hooks = sum(1 for hook in candidate.new_hooks if hook.importance == "major")
        if proposed_major_hooks > int(beat_budget["max_new_major_hooks_per_scene"]):
            issues.append("Candidate introduces too many major hooks in one scene.")
        for hook in candidate.new_hooks:
            if hook.importance == "major" and not (hook.payoff_concept or "").strip():
                issues.append(f"Major hook '{hook.summary}' must include a payoff_concept.")

        proposed_minor_hooks = sum(1 for hook in candidate.new_hooks if hook.importance in {"minor", "local"})
        if proposed_minor_hooks > int(beat_budget["max_minor_hooks_per_scene"]):
                issues.append("Candidate introduces too many minor/local hooks in one scene.")

        if candidate.new_objects:
            inventory_object_names = {
                (change.object_name or "").strip().lower()
                for change in candidate.inventory_changes
                if change.object_name
            }
            relation_object_names = {
                relation.object_name.strip().lower()
                for relation in candidate.relation_updates
                if relation.object_type == "object" and relation.object_name
            }
            for obj in candidate.new_objects:
                object_name = obj.name.strip().lower()
                if object_name not in inventory_object_names and object_name not in relation_object_names:
                    issues.append(
                        f"Persistent object '{obj.name}' needs stronger justification. Keep ordinary props in scene text unless they are inventory-relevant, reusable, or mechanically important."
                    )

        prompt_blocked_names = self._collect_prompt_blocked_entity_names(candidate=candidate, canon=canon)
        for asset_request in candidate.asset_requests:
            if asset_request.entity_type is None or asset_request.entity_id is None:
                issues.append("Asset requests must include entity_type and entity_id.")
                continue
            if asset_request.asset_kind == "background" and asset_request.entity_type != "location":
                issues.append("Background asset requests must target a location entity.")
            if asset_request.asset_kind == "portrait" and asset_request.entity_type != "character":
                issues.append("Portrait asset requests must target a character entity.")
            if asset_request.asset_kind == "object_render" and asset_request.entity_type != "object":
                issues.append("Object render requests must target an object entity.")

            existing_asset = assets.get_latest_asset(
                entity_type=asset_request.entity_type,
                entity_id=asset_request.entity_id,
                asset_kind=asset_request.asset_kind,
            )
            if existing_asset is not None:
                issues.append(
                    f"{asset_request.asset_kind} art already exists for {asset_request.entity_type}:{asset_request.entity_id}; do not request duplicate generation."
                )

            prompt_text = (asset_request.prompt or "").strip()
            if asset_request.asset_kind == "background" and prompt_text:
                normalized_prompt_tokens = set(self.PROMPT_ENTITY_TOKEN_PATTERN.findall(prompt_text.lower()))
                mentioned_blocked_names = [
                    name for name, name_tokens in prompt_blocked_names
                    if name_tokens and name_tokens.issubset(normalized_prompt_tokens)
                ]
                if mentioned_blocked_names:
                    issues.append(
                        "Background prompts must describe the static environment only and must not include on-screen or separately-rendered character/object names: "
                        + ", ".join(sorted(mentioned_blocked_names))
                        + "."
                    )
        issues.extend(
            self._collect_visible_speaker_issues(
                candidate=candidate,
                canon=canon,
                assets=assets,
            )
        )

        available_affordances = {row["name"] for row in branch_state_service.list_available_affordances(candidate.branch_key)}
        unlocked_affordances = available_affordances | {
            change.name for change in candidate.affordance_changes if change.action in {"unlock", "restore"}
        }
        for choice in candidate.choices:
            missing_affordances = [name for name in choice.required_affordances if name not in unlocked_affordances]
            if missing_affordances:
                issues.append(
                    f"Choice '{choice.choice_text}' requires unavailable affordances: {', '.join(missing_affordances)}."
                )
            if choice.target_node_id is not None:
                target_node = story_graph.get_story_node(choice.target_node_id)
                if target_node is None:
                    issues.append(
                        f"Choice '{choice.choice_text}' points to unknown target_node_id {choice.target_node_id}."
                    )
                elif target_node["branch_key"] != candidate.branch_key:
                    issues.append(
                        f"Choice '{choice.choice_text}' points to target_node_id {choice.target_node_id} in a different branch."
                    )

        locked_world_facts = [fact["fact_text"].strip().lower() for fact in canon.list_facts() if fact["is_locked"]]
        for fact in candidate.fact_updates:
            normalized_fact = fact.fact_text.strip().lower()
            if normalized_fact in locked_world_facts:
                continue
            if fact.is_locked and fact.source != "locked_rule":
                issues.append("LLM-generated fact updates must not introduce new locked facts directly.")

        mystery_markers = self._detect_unresolved_mystery_markers(candidate)
        if mystery_markers:
            active_hooks = branch_state_service.list_hooks(
                candidate.branch_key,
                statuses=["active", "payoff_ready", "blocked"],
            )
            uncovered_markers = [
                marker
                for marker in mystery_markers
                if not self._mystery_marker_is_covered(
                    marker=marker,
                    active_hooks=active_hooks,
                    new_hooks=candidate.new_hooks,
                )
            ]
            if uncovered_markers:
                marker_text = ", ".join(sorted(uncovered_markers))
                issues.append(
                    "Candidate introduces an unresolved mystery/question without creating or extending a hook: "
                    f"{marker_text}."
                )
                current_scene = next(
                    (reference for reference in candidate.entity_references if reference.role == "current_scene"),
                    None,
                )
                if current_scene is not None:
                    linked_new_hooks = [
                        hook for hook in candidate.new_hooks if hook.linked_entity_type and hook.linked_entity_id is not None
                    ]
                    if not linked_new_hooks:
                        issues.append(
                            "A new scene-anchored mystery should link its hook to the current_scene location or another relevant entity."
                        )

        return {
            "valid": not issues,
            "issues": issues,
            "current_depth": current_depth,
            "projected_depth": projected_depth,
            "act_phase": branch["act_phase"],
            "frontier_budget_state": frontier_budget,
            "arc_exit_candidate": self._compute_arc_exit_candidate(branch=branch, merge_candidates=story_graph.list_merge_candidates(candidate.branch_key)),
            "inferred_choice_classes": inferred_choice_classes,
        }

    def _resolve_choice_class(self, choice: Any) -> str:
        if choice.choice_class is not None:
            return choice.choice_class
        text = " ".join(filter(None, [choice.choice_text, choice.notes])).lower()
        if any(pattern.search(text) for pattern in self.ENDING_CHOICE_PATTERNS):
            return "ending"
        if any(pattern.search(text) for pattern in self.COMMITMENT_CHOICE_PATTERNS):
            return "commitment"
        if any(pattern.search(text) for pattern in self.INSPECTION_CHOICE_PATTERNS):
            return "inspection"
        return "progress"

    def _choice_text_implies_consequence(self, value: str) -> bool:
        text = (value or "").lower()
        return any(
            marker in text
            for marker in (
                "follow ",
                "board",
                "ride",
                "enter",
                "descend",
                "climb",
                "hide",
                "surrender",
                "run",
                "return",
                "call out",
                "warn",
                "speak",
                "ask ",
                "approach",
            )
        )

    def _candidate_has_location_transition_choice(self, candidate: GenerationCandidate) -> bool:
        return any(self._resolve_choice_class(choice) == "location_transition" for choice in candidate.choices)

    def _candidate_adds_actor_pressure(self, candidate: GenerationCandidate) -> bool:
        if candidate.new_characters or candidate.floating_character_introductions:
            return True
        character_refs = [
            reference for reference in candidate.entity_references
            if reference.entity_type == "character"
        ]
        character_present = [
            present for present in candidate.scene_present_entities
            if present.entity_type == "character" and present.slot != "hero-center"
        ]
        if character_present:
            return True
        if len({reference.entity_id for reference in character_refs}) >= 2:
            return True
        texts = " ".join(
            filter(
                None,
                [
                    candidate.scene_summary,
                    candidate.scene_text,
                    *(line.text for line in candidate.dialogue_lines),
                    *(choice.choice_text for choice in candidate.choices),
                ],
            )
        ).lower()
        return any(
            marker in texts
            for marker in ("patrol", "enumerator", "courier", "clerk", "rival", "auditor", "guard", "they arrive", "someone")
        )

    def _candidate_adds_location_motion(self, candidate: GenerationCandidate) -> bool:
        if candidate.new_locations:
            return True
        texts = " ".join(
            filter(
                None,
                [
                    candidate.scene_summary,
                    candidate.scene_text,
                    *(choice.choice_text for choice in candidate.choices),
                ],
            )
        ).lower()
        return any(
            marker in texts
            for marker in ("arrive", "arrival", "board", "ride", "descend", "climb", "return", "reach", "enter", "cross into", "tunnel", "station", "depot", "gate")
        )

    def _candidate_repeats_action_family(self, candidate: GenerationCandidate, family: str) -> bool:
        families = [self._classify_choice_action_family(choice.choice_text) for choice in candidate.choices]
        return bool(families) and all(choice_family in {family, "other"} for choice_family in families)

    def _classify_choice_action_family(self, choice_text: str) -> str:
        text = (choice_text or "").lower()
        if any(token in text for token in ("ask", "speak", "call", "answer", "warn", "bargain")):
            return "social"
        if any(token in text for token in ("board", "ride", "enter", "arrive", "return", "climb", "descend", "cross")):
            return "travel"
        if any(token in text for token in ("follow", "trace", "deeper")):
            return "follow"
        if any(token in text for token in ("touch", "press", "grip", "hold")):
            return "touch"
        if any(token in text for token in ("step back", "turn back", "observe", "watch", "wait")):
            return "step_back"
        if any(token in text for token in ("look", "listen", "inspect", "examine", "read", "study", "judge")):
            return "inspect"
        return "other"

    def _candidate_has_material_delta(self, candidate: GenerationCandidate) -> bool:
        if (
            candidate.new_locations
            or candidate.new_characters
            or candidate.floating_character_introductions
            or candidate.new_hooks
            or candidate.hook_updates
            or candidate.global_direction_notes
            or candidate.inventory_changes
            or candidate.affordance_changes
            or candidate.relationship_changes
            or candidate.discovered_clue_tags
            or candidate.discovered_state_tags
        ):
            return True
        if any(
            choice.target_node_id is not None or self._resolve_choice_class(choice) in {"location_transition", "ending"}
            for choice in candidate.choices
        ):
            return True
        if any(choice.required_affordances for choice in candidate.choices):
            return True
        if self._candidate_adds_actor_pressure(candidate) or self._candidate_adds_location_motion(candidate):
            return True
        texts = " ".join(filter(None, [candidate.scene_summary, candidate.scene_text])).lower()
        return any(
            marker in texts
            for marker in (
                "arrive",
                "arrival",
                "patrol",
                "seize",
                "close",
                "collapse",
                "alarm",
                "bell",
                "courier",
                "capture",
                "faction",
                "route closes",
            )
        )

    def _is_bloom_scene_candidate(
        self,
        *,
        branch: dict[str, Any],
        candidate: GenerationCandidate,
        merge_choice_count: int,
    ) -> bool:
        if branch.get("eligible_major_hooks"):
            return True
        if candidate.new_locations:
            return True
        if any(choice.target_node_id is not None for choice in candidate.choices) and merge_choice_count > 0:
            return False
        if candidate.new_hooks:
            return True
        return False

    def _compute_arc_exit_candidate(
        self,
        *,
        branch: dict[str, Any],
        merge_candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        low_local_pressure = (
            len(branch.get("active_hooks", [])) <= 2
            and len(branch.get("affordances", [])) <= 1
            and len(branch.get("inventory", [])) <= 1
        )
        return {
            "eligible": bool(low_local_pressure and merge_candidates),
            "reason": (
                "Local branch pressure is low enough that a transition merge is plausible."
                if low_local_pressure and merge_candidates
                else "Keep developing local consequences before attempting a larger arc-exit merge."
            ),
        }

    def _collect_prompt_blocked_entity_names(
        self,
        *,
        candidate: GenerationCandidate,
        canon: CanonResolver,
    ) -> list[tuple[str, set[str]]]:
        names: set[str] = set()

        for reference in candidate.entity_references:
            if reference.entity_type == "character":
                character = canon.get_character(reference.entity_id)
                if character and character.get("name"):
                    names.add(str(character["name"]))
            elif reference.entity_type == "object":
                obj = canon.get_object(reference.entity_id)
                if obj and obj.get("name"):
                    names.add(str(obj["name"]))

        for present in candidate.scene_present_entities:
            if present.entity_type == "character":
                character = canon.get_character(present.entity_id)
                if character and character.get("name"):
                    names.add(str(character["name"]))
            elif present.entity_type == "object":
                obj = canon.get_object(present.entity_id)
                if obj and obj.get("name"):
                    names.add(str(obj["name"]))

        for character in candidate.new_characters:
            if character.name.strip():
                names.add(character.name.strip())
        for obj in candidate.new_objects:
            if obj.name.strip():
                names.add(obj.name.strip())

        blocked_names: list[tuple[str, set[str]]] = []
        for name in sorted(names):
            tokens = {
                token
                for token in self.PROMPT_ENTITY_TOKEN_PATTERN.findall(name.lower())
                if token not in {"the"}
            }
            if tokens:
                blocked_names.append((name, tokens))
        return blocked_names

    def _collect_unknown_named_entities(
        self,
        *,
        candidate: GenerationCandidate,
        canon: CanonResolver,
    ) -> list[tuple[str, str]]:
        unknown_entities: set[tuple[str, str]] = set()

        def add_if_unknown(entity_type: str, entity_name: str | None) -> None:
            if entity_type == "world" or not entity_name or not entity_name.strip():
                return
            try:
                canon.resolve_entity_id(entity_type, entity_name)
            except ValueError:
                unknown_entities.add((entity_type, entity_name.strip()))

        for fact in candidate.fact_updates:
            add_if_unknown(fact.entity_type, fact.entity_name)
        for relation in candidate.relation_updates:
            add_if_unknown(relation.subject_type, relation.subject_name)
            add_if_unknown(relation.object_type, relation.object_name)
        for change in candidate.inventory_changes:
            add_if_unknown("object", change.object_name)
        for character in candidate.new_characters:
            add_if_unknown("location", character.home_location_name)
        for obj in candidate.new_objects:
            add_if_unknown("location", obj.default_location_name)

        return sorted(unknown_entities)

    def _detect_unresolved_mystery_markers(self, candidate: GenerationCandidate) -> list[str]:
        markers: set[str] = set()
        texts = [
            candidate.scene_summary,
            candidate.scene_text,
            *(line.text for line in candidate.dialogue_lines),
        ]
        for text in texts:
            lower_text = text.lower()
            for pattern in self.MYSTERY_MARKER_PATTERNS:
                if pattern.search(lower_text):
                    markers.add(pattern.pattern.replace("\\b", "").replace("\\", ""))

        for line in candidate.dialogue_lines:
            speaker = line.speaker.strip().lower()
            if speaker in {"narrator", "you"}:
                continue
            if any(pattern.search(speaker) for pattern in self.PLACEHOLDER_SPEAKER_PATTERNS):
                markers.add(speaker)
        return sorted(markers)

    def _speaker_is_nonvisual(self, speaker: str) -> bool:
        normalized = (speaker or "").strip()
        return any(pattern.search(normalized) for pattern in (*self.PLACEHOLDER_SPEAKER_PATTERNS, *self.NONVISUAL_SPEAKER_PATTERNS))

    def _collect_visible_speaker_issues(
        self,
        *,
        candidate: GenerationCandidate,
        canon: CanonResolver,
        assets: AssetService,
    ) -> list[str]:
        issues: list[str] = []
        explicit_portrait_targets = {
            int(request.entity_id)
            for request in candidate.asset_requests
            if request.entity_type == "character"
            and request.entity_id is not None
            and request.asset_kind in {"portrait", "cutout"}
        }
        existing_characters_by_name = {
            (character.get("name") or "").strip().lower(): character
            for character in canon.list_characters()
            if (character.get("name") or "").strip()
        }
        new_character_names = {
            (character.name or "").strip().lower()
            for character in candidate.new_characters
            if (character.name or "").strip()
        }
        seen_speakers: set[str] = set()

        for line in candidate.dialogue_lines:
            speaker = (line.speaker or "").strip()
            lowered = speaker.lower()
            if not speaker or lowered in {"narrator", "you"} or self._speaker_is_nonvisual(speaker):
                continue
            if lowered in seen_speakers:
                continue
            seen_speakers.add(lowered)
            if lowered in new_character_names:
                continue

            character = existing_characters_by_name.get(lowered)
            if character is None:
                issues.append(
                    f"Visible dialogue speaker '{speaker}' must correspond to a named existing character or a named new_characters entry. "
                    "If the speaker should stay generic for now, make them explicitly unseen/offscreen instead of showing them on-screen."
                )
                continue

            character_id = int(character["id"])
            preferred_asset = assets.get_preferred_asset(
                entity_type="character",
                entity_id=character_id,
                preferred_kinds=["cutout", "portrait"],
            )
            if preferred_asset is None and character_id not in explicit_portrait_targets:
                issues.append(
                    f"Visible dialogue speaker '{speaker}' does not have character art yet. "
                    "Reuse existing portrait/cutout art, add a portrait request, or keep the speaker explicitly unseen/offscreen."
                )

        return issues

    def _mystery_marker_is_covered(
        self,
        *,
        marker: str,
        active_hooks: list[dict[str, Any]],
        new_hooks: list[Any],
    ) -> bool:
        marker_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", marker.lower())
            if token not in {"a", "an", "the", "of", "from", "inside"}
        }
        for hook in active_hooks:
            hook_text = " ".join(
                [
                    str(hook.get("summary") or ""),
                    str(hook.get("notes") or ""),
                    str(hook.get("resolution_text") or ""),
                ]
            ).lower()
            hook_tokens = set(re.findall(r"[a-z0-9]+", hook_text))
            if marker_tokens and marker_tokens.issubset(hook_tokens):
                return True
        for hook in new_hooks:
            hook_text = " ".join(
                [
                    str(hook.summary or ""),
                    str(hook.notes or ""),
                    str(hook.hook_type or ""),
                ]
            ).lower()
            hook_tokens = set(re.findall(r"[a-z0-9]+", hook_text))
            if marker_tokens and marker_tokens.issubset(hook_tokens):
                return True
        return False
