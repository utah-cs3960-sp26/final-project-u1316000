from __future__ import annotations

import json
from typing import Any


class LLMGenerationService:
    """Stub for the later LLM-driven generation loop."""

    def build_context(
        self,
        *,
        branch_key: str,
        premise_facts: list[dict[str, Any]],
        relevant_entities: list[dict[str, Any]],
        open_hooks: list[str],
    ) -> dict[str, Any]:
        return {
            "branch_key": branch_key,
            "premise_facts": premise_facts,
            "relevant_entities": relevant_entities,
            "open_hooks": open_hooks,
        }

    def build_prompt(self, context: dict[str, Any]) -> str:
        return (
            "You are extending a branching world while preserving canon.\n"
            "Return structured JSON only.\n"
            f"Context:\n{json.dumps(context, indent=2)}"
        )

    def validate_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        required_keys = {"scene_summary", "events", "choices", "fact_updates"}
        missing = sorted(required_keys - payload.keys())
        if missing:
            raise ValueError(f"Missing required generation keys: {', '.join(missing)}")
        return payload
