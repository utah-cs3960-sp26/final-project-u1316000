from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models import GenerationCandidate
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver
from app.services.story_graph import StoryGraphService


class LLMGenerationService:
    """Builds story-generation context, prompt scaffolding, and validation rules."""

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
        story_graph: StoryGraphService,
        focus_entity_ids: list[int],
        current_node_id: int | None,
        branch_summary: str | None,
        requested_choice_count: int,
    ) -> dict[str, Any]:
        premise_facts = [fact for fact in canon.list_facts() if fact["entity_type"] == "world"]
        recurring_entities = branch_state.list_recurring_entities(branch_key)
        active_hooks = branch_state.list_hooks(branch_key, statuses=["active", "payoff_ready", "blocked"])
        eligible_major_hooks = branch_state.list_eligible_hooks(branch_key, importance="major")
        blocked_major_hooks = branch_state.list_ineligible_hooks(branch_key, importance="major")
        branch = branch_state.sync_branch_progress(branch_key)
        current_node = story_graph.get_story_node(current_node_id) if current_node_id is not None else None

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
            "active_hooks": active_hooks,
            "eligible_major_hooks": eligible_major_hooks,
            "blocked_major_hooks": blocked_major_hooks,
            "recurring_entities": recurring_entities,
            "requested_choice_count": requested_choice_count,
        }

    def build_prompt(self, context: dict[str, Any]) -> str:
        return (
            "You are extending a branching whimsical fantasy world while preserving continuity.\n"
            "Follow the story bible, respect hook pacing, and keep the tone sincere rather than jokey.\n"
            "Major mysteries must not resolve before their minimum distance and readiness conditions.\n"
            "Persistent affordances and inventory items remain available in the branch unless explicitly changed.\n"
            "Treat requested_choice_count as a target, not a rigid quota. Usually return 2 or 3 choices, sometimes 1 for a forced beat, and only occasionally 4 or more when the scene genuinely blooms.\n"
            "Cycles are allowed. Careful merges are allowed when branch-local consequences still make sense; do not collapse branches that now depend on different local state.\n"
            "Use scene_present_entities and hidden_on_lines when actors or objects should appear, disappear, or swap focus during the same scene.\n"
            "If a scene introduces a new recurring character, a new visually distinct linked location, or a reusable visually important object, make the need for art obvious so the post-apply asset pass can generate it once real IDs exist.\n"
            "Return structured JSON only with these top-level keys:\n"
            "scene_summary, scene_text, dialogue_lines, choices, entity_references, scene_present_entities, fact_updates, relation_updates, "
            "new_hooks, hook_updates, inventory_changes, affordance_changes, relationship_changes, "
            "asset_requests, discovered_clue_tags, discovered_state_tags.\n"
            f"Context:\n{json.dumps(context, indent=2)}"
        )

    def validate_candidate(
        self,
        *,
        candidate: GenerationCandidate,
        branch_state_service: BranchStateService,
        canon: CanonResolver,
    ) -> dict[str, Any]:
        branch = branch_state_service.sync_branch_progress(candidate.branch_key)
        current_depth = int(branch["branch_depth"])
        state_tags = {row["tag"] for row in branch_state_service.list_branch_tags(candidate.branch_key, tag_type="state")}
        clue_tags = {row["tag"] for row in branch_state_service.list_branch_tags(candidate.branch_key, tag_type="clue")}
        discovered_state_tags = state_tags | set(candidate.discovered_state_tags)
        discovered_clue_tags = clue_tags | set(candidate.discovered_clue_tags)
        issues: list[str] = []

        if len(candidate.choices) == 0:
            issues.append("Generation candidate must include at least one choice.")

        beat_budget = self.story_bible["beat_budget"]
        major_hook_updates = 0
        for hook_update in candidate.hook_updates:
            hook = branch_state_service.get_hook(hook_update.hook_id)
            if hook is None:
                issues.append(f"Unknown hook id referenced in hook_updates: {hook_update.hook_id}")
                continue
            if hook["importance"] == "major" and hook_update.status in {"payoff_ready", "resolved"}:
                major_hook_updates += 1
            if hook_update.status == "resolved":
                if current_depth < int(hook["introduced_at_depth"]) + int(hook["min_distance_to_payoff"]):
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

        locked_world_facts = [fact["fact_text"].strip().lower() for fact in canon.list_facts() if fact["is_locked"]]
        for fact in candidate.fact_updates:
            normalized_fact = fact.fact_text.strip().lower()
            if normalized_fact in locked_world_facts:
                continue
            if fact.is_locked and fact.source != "locked_rule":
                issues.append("LLM-generated fact updates must not introduce new locked facts directly.")

        return {
            "valid": not issues,
            "issues": issues,
            "current_depth": current_depth,
            "act_phase": branch["act_phase"],
        }
