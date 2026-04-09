from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import connect
from app.services.branch_state import BranchStateService
from app.services.generation import LLMGenerationService
from app.services.story_graph import StoryGraphService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Park low-priority frontier choices to keep branch growth manageable.")
    parser.add_argument("--branch-key")
    parser.add_argument("--soft-limit", type=int)
    parser.add_argument("--keep-recent-parents", type=int)
    parser.add_argument("--unpark-choice-id", type=int)
    parser.add_argument("--apply", action="store_true", help="Apply the frontier rebalance instead of reporting a dry run.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    project_root = Path(__file__).resolve().parents[2]
    llm_generation = LLMGenerationService(project_root)
    branching_policy = llm_generation.story_bible.get("branching_policy") or {}
    budget = branching_policy.get("frontier_budget") or {}
    soft_limit = args.soft_limit or int(budget.get("soft_open_choice_limit", 48))
    keep_recent_parents = args.keep_recent_parents or int(budget.get("keep_recent_parent_count", 12))

    with connect(settings.database_path) as connection:
        story = StoryGraphService(connection)
        branch_state = BranchStateService(connection, llm_generation.story_bible["acts"])

        if args.unpark_choice_id is not None:
            choice = story.set_choice_status(args.unpark_choice_id, "open")
            print(json.dumps({"unparked_choice_id": args.unpark_choice_id, "choice": choice}, indent=2))
            return

        frontier = story.list_frontier(
            branch_state_service=branch_state,
            branch_key=args.branch_key,
            limit=1000,
            mode="auto",
            branching_policy=branching_policy,
        )
        keep_ids: set[int] = set()
        for item in frontier[:soft_limit]:
            keep_ids.add(int(item["choice_id"]))

        seen_recent_parents: set[int] = set()
        for item in frontier:
            parent_id = int(item["from_node_id"])
            if parent_id in seen_recent_parents:
                continue
            keep_ids.add(int(item["choice_id"]))
            seen_recent_parents.add(parent_id)
            if len(seen_recent_parents) >= keep_recent_parents:
                break

        park_ids = [int(item["choice_id"]) for item in frontier if int(item["choice_id"]) not in keep_ids]
        parked_count = story.park_choices(park_ids) if args.apply else 0
        result: dict[str, Any] = {
            "dry_run": not args.apply,
            "branch_key": args.branch_key,
            "soft_limit": soft_limit,
            "keep_recent_parents": keep_recent_parents,
            "open_frontier_count": len(frontier),
            "kept_choice_ids": sorted(keep_ids),
            "park_choice_ids": park_ids,
        }
        if args.apply:
            result["parked_count"] = parked_count
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
