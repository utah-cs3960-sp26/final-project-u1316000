from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.models import GenerationCandidate
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver
from app.services.story_notes import StoryDirectionService
from app.services.story_graph import StoryGraphService


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
    CHOICE_NOTES_PATTERN = re.compile(
        r"goal\s*:\s*(?P<goal>.+?)\s+intent\s*:\s*(?P<intent>.+)",
        re.IGNORECASE | re.DOTALL,
    )
    MIN_ENTITY_DESCRIPTION_LENGTH = 12

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
            "active_hooks": active_hooks,
            "eligible_major_hooks": eligible_major_hooks,
            "blocked_major_hooks": blocked_major_hooks,
            "developable_major_hooks": developable_major_hooks,
            "blocked_major_developments": blocked_major_developments,
            "global_direction_notes": global_direction_notes,
            "recurring_entities": recurring_entities,
            "merge_candidates": merge_candidates,
            "requested_choice_count": requested_choice_count,
        }

    def build_prompt(self, context: dict[str, Any]) -> str:
        return (
            "You are extending a branching whimsical fantasy world while preserving continuity.\n"
            "Follow the story bible, respect hook pacing, and keep the tone sincere rather than jokey.\n"
            "Use a mix of lyrical narration and clearer spoken dialogue.\n"
            "Narration may be more poetic or uncanny, but spoken dialogue should usually be more grounded and immediately understandable.\n"
            "The player character should usually be one of the clearest voices in the scene.\n"
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
            "Every choice must include internal planning notes in this form: 'Goal: ... Intent: ...'. Goal is the immediate purpose of taking the option. Intent is what broader direction, branch shape, or future possibility the option is meant to open, reinforce, or revisit.\n"
            "If a quick merge is appropriate, a generated choice may include target_node_id pointing at one of the provided merge_candidates.\n"
            "Use scene_present_entities and hidden_on_lines when actors or objects should appear, disappear, or swap focus during the same scene.\n"
            "If a scene introduces a new recurring character, a new visually distinct linked location, or a reusable visually important object, make the need for art obvious so the post-apply asset pass can generate it once real IDs exist.\n"
            "Generate art on demand, not speculatively. If a place, character, or object is only being set up for later and is not yet on-screen or immediately reachable in play, defer its art until a later scene actually needs it.\n"
            "If a choice clearly means travel, arrival, boarding, departure, or being sent somewhere else, strongly prefer a new linked location unless it is truly the same place from nearly the same visual framing.\n"
            "If the player has clearly arrived somewhere new, reusing the old background just to avoid art generation is usually the wrong choice.\n"
            "If a location does not yet have art, give it a distinct whimsical-fantasy identity that stays readable and not overly complicated for image generation.\n"
            "Return structured JSON only with these top-level keys:\n"
            "scene_summary, scene_text, dialogue_lines, choices, new_locations, new_characters, new_objects, entity_references, scene_present_entities, fact_updates, relation_updates, "
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
        current_depth = int(branch["branch_depth"])
        projected_depth = current_depth + 1
        state_tags = {row["tag"] for row in branch_state_service.list_branch_tags(candidate.branch_key, tag_type="state")}
        clue_tags = {row["tag"] for row in branch_state_service.list_branch_tags(candidate.branch_key, tag_type="clue")}
        discovered_state_tags = state_tags | set(candidate.discovered_state_tags)
        discovered_clue_tags = clue_tags | set(candidate.discovered_clue_tags)
        issues: list[str] = []

        if len(candidate.choices) == 0:
            issues.append("Generation candidate must include at least one choice.")

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

        merge_choice_count = sum(1 for choice in candidate.choices if choice.target_node_id is not None)
        fresh_choice_count = sum(1 for choice in candidate.choices if choice.target_node_id is None)
        if branch_shape["should_prefer_divergence"] and merge_choice_count > 0 and fresh_choice_count == 0:
            issues.append(
                "This branch has quick-merged too often recently. Open at least one fresh path instead of only merging into existing scenes."
            )
        for choice in candidate.choices:
            notes = (choice.notes or "").strip()
            if not notes:
                issues.append(
                    f"Choice '{choice.choice_text}' must include notes describing Goal and Intent."
                )
                continue
            match = self.CHOICE_NOTES_PATTERN.search(notes)
            if match is None or len(match.group("goal").strip()) < 12 or len(match.group("intent").strip()) < 12:
                issues.append(
                    f"Choice '{choice.choice_text}' must use notes in the form 'Goal: ... Intent: ...' with meaningful content."
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
        }

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
