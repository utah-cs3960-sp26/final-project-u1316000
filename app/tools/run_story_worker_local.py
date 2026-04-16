from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.database import connect
from app.main import create_app
from app.models import (
    CharacterSeed,
    ChoiceClass,
    DialogueLine,
    DirectionNoteProposal,
    EndingCategory,
    EntityReference,
    FloatingCharacterIntroduction,
    GeneratedChoice,
    GenerationCandidate,
    HookProposal,
    HookUpdate,
    LocationSeed,
    ScenePresentEntity,
    TransitionNodeSpec,
    WorldbuildingNoteProposal,
)
from app.services.assets import AssetService, default_dimensions_for_asset_kind
from app.services.branch_state import BranchStateService
from app.services.canon import CanonResolver
from app.services.story_graph import StoryGraphService


PlanningIdeaCategory = Literal["character", "location", "object", "event"]


class PlanningIdeaBinding(BaseModel):
    title: str
    category: PlanningIdeaCategory
    source: Literal["fresh", "existing"] = "fresh"
    steering_note: str | None = None


class PlanningChoiceUpdate(BaseModel):
    choice_id: int
    notes: str = Field(
        min_length=20,
        pattern=r"^NEXT_NODE:\s*\S[\s\S]*FURTHER_GOALS:\s*\S[\s\S]*$",
    )
    bound_idea: PlanningIdeaBinding | None = None


class PlanningIdea(BaseModel):
    category: PlanningIdeaCategory
    title: str
    note_text: str


class PlanningResult(BaseModel):
    ideas_to_append: list[PlanningIdea] = Field(default_factory=list)
    choice_note_updates: list[PlanningChoiceUpdate] = Field(default_factory=list)
    story_direction_notes: list[DirectionNoteProposal] = Field(default_factory=list)
    summary: str | None = None


class PlanningIdeasResult(BaseModel):
    ideas_to_append: list[PlanningIdea] = Field(default_factory=list)


class PlanningFollowthroughResult(BaseModel):
    choice_note_updates: list[PlanningChoiceUpdate] = Field(default_factory=list)
    story_direction_notes: list[DirectionNoteProposal] = Field(default_factory=list)
    worldbuilding_notes: list[WorldbuildingNoteProposal] = Field(default_factory=list)
    summary: str | None = None


class RevivalChoiceResult(BaseModel):
    choice_text: str
    notes: str = Field(
        min_length=20,
        pattern=r"^NEXT_NODE:\s*\S[\s\S]*FURTHER_GOALS:\s*\S[\s\S]*$",
    )
    choice_class: Literal["inspection", "progress", "commitment", "location_transition", "ending"] | None = None
    ending_category: Literal["death", "dead_end", "capture", "transformation", "hub_return"] | None = None


class SessionMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class WorkerSessionState(BaseModel):
    version: int = 1
    mode: Literal["normal"] = "normal"
    model: str
    run_count: int = 0
    messages: list[SessionMessage] = Field(default_factory=list)
    last_run_outcome: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ScenePlanDraft(BaseModel):
    scene_title: str
    scene_summary: str
    material_change: str
    opening_beat: str
    location_status: Literal["same_location", "new_location", "return_location"]
    scene_cast_mode: Literal["none", "mc_only", "same", "explicit"] = "explicit"
    scene_cast_entries: list[str] = Field(default_factory=list)
    new_character_names: list[str] = Field(default_factory=list)
    new_location_name: str | None = None
    return_location_ref: str | None = None
    new_character_intro: str | None = None
    new_location_intro: str | None = None


class SceneSettingsDraft(BaseModel):
    visible_when_speaking: bool = True
    start_show_all_from_last_node: bool = True
    mc_always_visible: bool = True


class SceneScriptCommand(BaseModel):
    action: Literal["show", "hide", "show_only", "show_all", "hide_all"]
    targets: list[str] = Field(default_factory=list)


class SceneScriptTextbox(BaseModel):
    speaker_ref: str = "0"
    text: str
    pending_commands: list[SceneScriptCommand] = Field(default_factory=list)


class CompiledSceneBody(BaseModel):
    scene_text: str
    dialogue_lines: list[DialogueLine] = Field(default_factory=list)
    hidden_lines_by_character: dict[str, list[int]] = Field(default_factory=dict)


class SceneBodyDraft(BaseModel):
    settings: SceneSettingsDraft = Field(default_factory=SceneSettingsDraft)
    raw_body: str
    textboxes: list[SceneScriptTextbox] = Field(default_factory=list)


class ChoiceDraft(BaseModel):
    choice_text: str
    choice_class: ChoiceClass
    next_node: str
    further_goals: str
    ending_category: EndingCategory | None = None
    target_existing_node: int | None = None


class SceneExtrasDraft(BaseModel):
    new_characters: list[CharacterSeed] = Field(default_factory=list)
    new_locations: list[LocationSeed] = Field(default_factory=list)


class SceneHooksDraft(BaseModel):
    hook_action: Literal["none", "new_hook", "update_hook"] = "none"
    hook_importance: Literal["major", "minor", "local"] | None = None
    hook_type: str | None = None
    hook_summary: str | None = None
    hook_payoff_concept: str | None = None
    hook_id: int | None = None
    hook_status: Literal["active", "payoff_ready", "resolved", "blocked"] | None = None
    hook_progress_note: str | None = None
    clue_tags: list[str] = Field(default_factory=list)
    state_tags: list[str] = Field(default_factory=list)
    global_direction_notes: list[DirectionNoteProposal] = Field(default_factory=list)


class SceneArtDraft(BaseModel):
    character_art_hints: dict[str, str] = Field(default_factory=dict)
    location_art_hints: dict[str, str] = Field(default_factory=dict)


class TransitionNodeDraft(BaseModel):
    choice_index: int = Field(ge=0)
    target_existing_node: int = Field(ge=1)
    scene_title: str | None = None
    scene_summary: str
    body: SceneBodyDraft


class NormalRunConversationState(BaseModel):
    scene_plan: ScenePlanDraft | None = None
    scene_body: SceneBodyDraft | None = None
    choices: list[ChoiceDraft] = Field(default_factory=list)
    transition_nodes: list[TransitionNodeDraft] = Field(default_factory=list)
    hooks: SceneHooksDraft | None = None
    extras: SceneExtrasDraft | None = None
    art: SceneArtDraft | None = None
    force_next_steps: list[str] = Field(default_factory=list)


DISALLOWED_PLANNING_IDEA_SEEDS = {
    "transit robbery",
    "clerk nettle s rival",
    "bell orchard",
    "goose back courier route",
    "name bureaucracy",
    "yesterday s orchard",
    "the apology bridge",
}

IDEA_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "under",
    "with",
}

GENERIC_IDEA_TOKENS = {
    "character",
    "location",
    "object",
    "event",
    "tram",
    "trams",
    "route",
    "routes",
    "station",
    "stations",
    "platform",
    "platforms",
    "field",
    "mushroom",
    "mushrooms",
    "hat",
    "mirror",
    "identity",
    "identities",
    "correction",
    "corrections",
    "departure",
    "departures",
    "ticket",
    "tickets",
    "memory",
    "memories",
    "name",
    "names",
    "record",
    "records",
    "body",
    "protagonist",
}

VALID_PRESENT_ENTITY_SLOTS = {
    "hero-center",
    "left-support",
    "right-support",
    "left-foreground-object",
    "right-foreground-object",
    "center-foreground-object",
}

NORMAL_STEP_LABELS: dict[str, list[str]] = {
    "scene_plan": [
        "SCENE_TITLE",
        "SCENE_SUMMARY",
        "MATERIAL_CHANGE",
        "OPENING_BEAT",
        "LOCATION_STATUS",
        "SCENE_CAST",
        "NEW_CHARACTERS",
        "NEW_LOCATION",
        "NEW_CHARACTER_INTRO",
        "NEW_LOCATION_INTRO",
    ],
    "scene_body": [
        "SCENE_SETTINGS",
        "SCENE_BODY",
    ],
    "choice": [
        "CHOICE_TEXT",
        "CHOICE_CLASS",
        "NEXT_NODE",
        "FURTHER_GOALS",
        "ENDING_CATEGORY",
        "TARGET_EXISTING_NODE",
    ],
    "link_nodes": [
        "TRANSITION_TITLE",
        "TRANSITION_SUMMARY",
        "SCENE_SETTINGS",
        "SCENE_BODY",
    ],
    "hooks": [
        "HOOK_ACTION",
        "HOOK_IMPORTANCE",
        "HOOK_TYPE",
        "HOOK_SUMMARY",
        "HOOK_PAYOFF_CONCEPT",
        "HOOK_ID",
        "HOOK_STATUS",
        "HOOK_PROGRESS_NOTE",
        "CLUE_TAGS",
        "STATE_TAGS",
        "GLOBAL_DIRECTION_NOTES",
    ],
    "details": [
        "CHARACTER_DETAILS",
        "LOCATION_DETAILS",
    ],
    "art": [
        "CHARACTER_ART_HINTS",
        "LOCATION_ART_HINTS",
    ],
}


OPTIONAL_CHOICE_SKIP_MARKERS = {
    "",
    "END",
    "NONE",
    "SKIP",
}
EMPTY_STEP_RESPONSE_RETRY_LIMIT = 3

SAME_LOCATION_PRESSURE_THRESHOLD = 6


SCENE_TRANSITION_CUE_PATTERNS = [
    re.compile(r"\b(board|boarding|ride|travel|arrive|arrival|depart|departure|enter|entered|reach|reached)\b", re.IGNORECASE),
    re.compile(r"\bstep\s+(into|through)\b", re.IGNORECASE),
    re.compile(r"\b(head|go|went)\s+to\b", re.IGNORECASE),
    re.compile(r"\bportal\b", re.IGNORECASE),
]

TEXT_SIMILARITY_STOPWORDS = {
    "a",
    "an",
    "and",
    "around",
    "as",
    "at",
    "before",
    "by",
    "deeper",
    "for",
    "from",
    "go",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "through",
    "to",
    "under",
    "up",
    "with",
}

CHOICE_GENERIC_TOKENS = {
    "ask",
    "choice",
    "clue",
    "clues",
    "climb",
    "counting",
    "deeper",
    "examine",
    "faint",
    "field",
    "follow",
    "glass",
    "goal",
    "green",
    "hand",
    "hat",
    "hidden",
    "inspect",
    "intent",
    "look",
    "marker",
    "mushroom",
    "read",
    "route",
    "routes",
    "seam",
    "step",
    "study",
    "symbol",
    "symbols",
    "touch",
    "trace",
    "watch",
    "wire",
    "wires",
}

MYSTERY_MARKER_PATTERNS = [
    re.compile(r"\bunseen voice\b", re.IGNORECASE),
    re.compile(r"\bunknown voice\b", re.IGNORECASE),
    re.compile(r"\bmysterious voice\b", re.IGNORECASE),
    re.compile(r"\bunseen figure\b", re.IGNORECASE),
    re.compile(r"\bunknown figure\b", re.IGNORECASE),
    re.compile(r"\bunknown speaker\b", re.IGNORECASE),
    re.compile(r"\bsomeone\b.*\b(from inside|in the dark|behind|inside the stalk)\b", re.IGNORECASE),
]
CONSEQUENCE_TEXT_PATTERNS = [
    re.compile(r"\bfollow\b", re.IGNORECASE),
    re.compile(r"\bboard\b", re.IGNORECASE),
    re.compile(r"\bride\b", re.IGNORECASE),
    re.compile(r"\benter\b", re.IGNORECASE),
    re.compile(r"\bdescend\b", re.IGNORECASE),
    re.compile(r"\bclimb\b", re.IGNORECASE),
    re.compile(r"\bhide\b", re.IGNORECASE),
    re.compile(r"\bsurrender\b", re.IGNORECASE),
    re.compile(r"\brun\b", re.IGNORECASE),
    re.compile(r"\breturn\b", re.IGNORECASE),
    re.compile(r"\bcall out\b", re.IGNORECASE),
    re.compile(r"\bwarn\b", re.IGNORECASE),
    re.compile(r"\bspeak\b", re.IGNORECASE),
    re.compile(r"\bask\b", re.IGNORECASE),
    re.compile(r"\bapproach\b", re.IGNORECASE),
    re.compile(r"\bhead for\b", re.IGNORECASE),
    re.compile(r"\bescape\b", re.IGNORECASE),
    re.compile(r"\bflee\b", re.IGNORECASE),
    re.compile(r"\bnegotiate\b", re.IGNORECASE),
    re.compile(r"\baddress(?:ing)?\b", re.IGNORECASE),
    re.compile(r"\bdiffuse\b", re.IGNORECASE),
]
SPEECH_VERB_PATTERN = re.compile(
    r"\b(says|said|asks|asked|replies|replied|calls|called|shouts|shouted|whispers|whispered|mutters|muttered|barks|barked|speaks|spoke|tells|told|cries|cried|answers|answered|demands|demanded|calls out|called out|clears his throat)\b",
    re.IGNORECASE,
)
IN_WORLD_ROLE_PATTERN = re.compile(
    r"\b(enumerator|surveyors?|patrol(?: member)?|guard|officer|courier|clerk|rival|auditor|witness|stranger|figure)\b",
    re.IGNORECASE,
)

LOCAL_PROP_CHOICE_PATTERNS = [
    re.compile(r"\b(?:inspect|examine|read|touch|press|study|step around|circle around|go around|look at|pick up|lift)\s+(?:the|a|an)\s+([a-z][a-z0-9' -]{2,60})", re.IGNORECASE),
]
NONVISUAL_SPEAKER_PATTERNS = [
    re.compile(r"\bunseen\b", re.IGNORECASE),
    re.compile(r"\bunknown\b", re.IGNORECASE),
    re.compile(r"\bmysterious\b", re.IGNORECASE),
    re.compile(r"\(o\.s\.\)", re.IGNORECASE),
    re.compile(r"\boffscreen\b", re.IGNORECASE),
    re.compile(r"\bover (the )?(radio|speaker|intercom)\b", re.IGNORECASE),
]


def normalize_text_for_similarity(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def similarity_tokens(value: str, *, extra_stopwords: set[str] | None = None) -> set[str]:
    stopwords = TEXT_SIMILARITY_STOPWORDS | (extra_stopwords or set())
    return {
        token[:-1] if token.endswith("s") and len(token) > 4 else token
        for token in re.findall(r"[a-z0-9]+", normalize_text_for_similarity(value))
        if len(token) >= 4 and token not in stopwords
    }


def texts_are_near_duplicates(left: str, right: str) -> bool:
    normalized_left = normalize_text_for_similarity(left)
    normalized_right = normalize_text_for_similarity(right)
    if not normalized_left or not normalized_right:
        return False
    if normalized_left == normalized_right:
        return True
    left_tokens = similarity_tokens(left)
    right_tokens = similarity_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = left_tokens & right_tokens
    if not overlap:
        return False
    smaller = min(len(left_tokens), len(right_tokens))
    if len(overlap) >= 3 and (len(overlap) / max(smaller, 1)) >= 0.75:
        return True
    return len(left_tokens.symmetric_difference(right_tokens)) <= 1 and len(overlap) >= 2


def extract_grounding_phrase(value: str) -> str:
    phrase = (value or "").strip()
    phrase = re.split(r"\b(?:for|with|that|where|while|before|after|beneath|under|near|beside)\b", phrase, maxsplit=1, flags=re.IGNORECASE)[0]
    return phrase.strip(" .,!?:;\"'")


def classify_choice_action_family(choice_text: str) -> str:
    text = (choice_text or "").lower()
    if any(token in text for token in ("ask", "speak", "call", "answer", "warn", "bargain", "address", "negotiate", "diffuse", "appeal", "explain", "bluff", "persuade")):
        return "social"
    if any(token in text for token in ("board", "ride", "enter", "arrive", "return", "climb", "descend", "cross", "head for", "go to", "run", "escape", "flee", "hide", "slip past", "dash", "retreat")):
        return "travel"
    if any(token in text for token in ("follow", "trace", "deeper")):
        return "follow"
    if any(token in text for token in ("touch", "press", "grip", "hold")):
        return "touch"
    if any(token in text for token in ("step back", "turn back", "observe", "watch", "wait", "step aside")):
        return "step_back"
    if any(token in text for token in ("look", "listen", "inspect", "examine", "read", "study", "judge", "kneel beside")):
        return "inspect"
    return "other"


def infer_choice_class_from_text(choice_text: str, notes: str | None) -> str:
    text = " ".join(filter(None, [choice_text, notes or ""])).lower()
    if any(pattern.search(text) for pattern in SCENE_TRANSITION_CUE_PATTERNS):
        return "commitment"
    if any(token in text for token in ("die", "death", "surrender", "give up", "accept capture", "let it take you")):
        return "ending"
    if any(token in text for token in ("look", "listen", "inspect", "examine", "read", "study", "judge")):
        return "inspection"
    if any(token in text for token in ("follow", "enter", "board", "ride", "climb", "descend", "return", "ask", "warn")):
        return "commitment"
    return "progress"


def choice_is_location_transition(choice: ChoiceDraft) -> bool:
    return choice.choice_class == "location_transition"


def choice_text_implies_consequence(value: str) -> bool:
    text = (value or "").lower()
    return any(pattern.search(text) for pattern in CONSEQUENCE_TEXT_PATTERNS)


def choice_breaks_repeated_action_pattern(choice: ChoiceDraft) -> bool:
    if choice.target_existing_node is not None or choice.ending_category is not None or choice_is_location_transition(choice):
        return True
    action_family = classify_choice_action_family(choice.choice_text)
    if action_family in {"social", "travel"}:
        return True
    combined_text = " ".join(
        filter(None, [choice.choice_text, choice.next_node, choice.further_goals])
    )
    return choice_text_implies_consequence(combined_text)


def candidate_adds_actor_pressure(candidate: GenerationCandidate) -> bool:
    if candidate.new_characters or candidate.floating_character_introductions:
        return True
    character_refs = [
        reference for reference in candidate.entity_references
        if reference.entity_type == "character"
    ]
    if any(
        present.entity_type == "character" and present.slot != "hero-center"
        for present in candidate.scene_present_entities
    ):
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


def candidate_adds_new_character(candidate: GenerationCandidate) -> bool:
    return bool(candidate.new_characters)


def candidate_adds_location_motion(candidate: GenerationCandidate) -> bool:
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


def candidate_has_location_transition_choice(candidate: GenerationCandidate) -> bool:
    return any(choice.choice_class == "location_transition" for choice in candidate.choices)


def candidate_satisfies_location_transition_obligation(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
) -> bool:
    parent_current_location = packet.get("parent_current_location") or {}
    parent_location_id = parent_current_location.get("id")
    current_scene_reference = next(
        (
            reference for reference in candidate.entity_references
            if reference.entity_type == "location" and reference.role == "current_scene"
        ),
        None,
    )
    if current_scene_reference is not None and current_scene_reference.entity_id is not None and parent_location_id is not None:
        if int(current_scene_reference.entity_id) != int(parent_location_id):
            return True
    return candidate_adds_location_motion(candidate)


def build_staleness_pressure_block(packet: dict[str, Any]) -> str:
    isolation_pressure = packet.get("isolation_pressure") or {}
    new_character_pressure = packet.get("new_character_pressure") or {}
    location_stall_pressure = packet.get("location_stall_pressure") or {}
    location_transition_obligation = packet.get("location_transition_obligation") or {}
    lines: list[str] = []
    if isolation_pressure.get("active"):
        lines.append("Current isolation pressure rules:")
        lines.append(
            "- this branch has stayed protagonist-only too long and must stop being effectively solitary"
        )
        lines.append(
            "- satisfy this with another named character, a reintroduced character, or clear faction/social pressure onstage"
        )
        lines.append("- a new location alone does NOT satisfy isolation pressure")
    if new_character_pressure.get("active"):
        lines.append("Current new-character pressure rules:")
        lines.append(
            "- this branch has gone too long without introducing a brand-new character"
        )
        lines.append(
            "- satisfy this with NEW_CHARACTERS and a real first-meeting beat"
        )
        lines.append("- reusing only existing characters does NOT satisfy new-character pressure")
    if location_stall_pressure.get("active"):
        lines.append("Current location-stall pressure rules:")
        lines.append(
            "- this branch has stayed in the same place too long"
        )
        lines.append(
            "- satisfy this in the choice-writing phase by including at least one CHOICE_CLASS: location_transition option in the menu"
        )
        lines.append("- that location_transition choice promises that its future expansion will move to a different location")
        lines.append("- a new character alone does NOT satisfy location-stall pressure")
    if location_transition_obligation.get("active"):
        lines.append("Current location-transition expansion rules:")
        lines.append(
            "- the selected frontier choice already promised a move to a different location"
        )
        lines.append(
            "- this child scene will only validate if LOCATION_STATUS changes location now with either new_location or return_location"
        )
        lines.append(
            "- return_location must target a path-safe existing location different from the parent current location"
        )
    if lines:
        lines.append(
            "- use IDEAS.md as a main source of fresh people, places, and whimsical turns when pressure is active"
        )
        lines.append(
            "- prefer whimsical, readable, unexpected developments over another direct derivative of the current patrol/vault/seam beat"
        )
        return "\n".join(lines) + "\n\n"
    return ""


def scene_plan_satisfies_isolation_pressure(*, draft: ScenePlanDraft, resolution: dict[str, Any]) -> bool:
    protagonist_name = (resolution.get("protagonist_name") or "").strip().lower()
    resolved_cast = resolve_scene_cast_names(draft=draft, resolution=resolution)
    visible_non_protagonists = any(
        name.strip() and name.strip().lower() != protagonist_name
        for name in resolved_cast
    )
    text = " ".join(
        filter(
            None,
            [
                draft.scene_summary,
                draft.material_change,
                draft.new_character_intro or "",
                draft.new_location_intro or "",
            ],
        )
    ).lower()
    has_social_or_faction_pressure = any(
        marker in text
        for marker in (
            "patrol",
            "enumerator",
            "courier",
            "guard",
            "clerk",
            "auditor",
            "question",
            "interrogate",
            "confront",
            "called out",
            "someone arrives",
            "they arrive",
        )
    )
    return visible_non_protagonists or has_social_or_faction_pressure


def scene_plan_satisfies_new_character_pressure(*, draft: ScenePlanDraft) -> bool:
    return bool([name for name in draft.new_character_names if name.strip()])


def resolve_return_location_target(
    *,
    draft: ScenePlanDraft,
    resolution: dict[str, Any],
) -> dict[str, Any] | None:
    raw_target = (draft.return_location_ref or "").strip()
    if not raw_target:
        return None
    path_location_name_map = resolution.get("path_location_name_map") or {}
    path_location_id_map = resolution.get("path_location_id_map") or {}
    lowered_target = raw_target.lower()
    if lowered_target in path_location_name_map:
        return path_location_name_map[lowered_target]
    if lowered_target.isdigit():
        return path_location_id_map.get(int(lowered_target))
    return None


def scene_plan_satisfies_location_transition_obligation(
    *,
    draft: ScenePlanDraft,
    resolution: dict[str, Any],
) -> bool:
    if draft.location_status == "new_location":
        return bool((draft.new_location_name or "").strip())
    if draft.location_status != "return_location":
        return False
    target_location = resolve_return_location_target(draft=draft, resolution=resolution)
    if target_location is None or target_location.get("id") is None:
        return False
    current_location_id = resolution.get("current_location_id")
    return int(target_location["id"]) != int(current_location_id) if current_location_id is not None else True


def scene_body_mentions_declared_new_character(*, state: NormalRunConversationState, draft: SceneBodyDraft) -> bool:
    if state.scene_plan is None:
        return False
    declared_new_names = {
        name.strip().lower()
        for name in state.scene_plan.new_character_names
        if name.strip()
    }
    if not declared_new_names:
        return False
    lowered_text = " ".join(
        filter(
            None,
            [
                draft.raw_body,
                *(textbox.text for textbox in draft.textboxes),
            ],
        )
    ).lower()
    if any(name in lowered_text for name in declared_new_names):
        return True
    for textbox in draft.textboxes:
        if textbox.speaker_ref.strip().lower() in declared_new_names:
            return True
        for command in textbox.pending_commands:
            if any(target.strip().lower() in declared_new_names for target in command.targets):
                return True
    return False


def candidate_has_material_delta(candidate: GenerationCandidate) -> bool:
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
        choice.target_node_id is not None
        or (choice.choice_class or infer_choice_class_from_text(choice.choice_text, choice.notes)) in {"location_transition", "ending"}
        for choice in candidate.choices
    ):
        return True
    if any(choice.required_affordances for choice in candidate.choices):
        return True
    if candidate_adds_actor_pressure(candidate) or candidate_adds_location_motion(candidate):
        return True
    texts = " ".join(filter(None, [candidate.scene_summary, candidate.scene_text])).lower()
    return any(
        marker in texts
        for marker in ("arrive", "arrival", "patrol", "seize", "close", "collapse", "alarm", "bell", "courier", "capture", "faction", "route closes")
    )


def prune_existing_asset_requests(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
) -> GenerationCandidate:
    availability = packet.get("asset_availability") or []
    existing_pairs = {
        (asset_kind, entry.get("entity_type"), int(entry["entity_id"]))
        for entry in availability
        for asset_kind in (entry.get("available_asset_kinds") or [])
        if entry.get("entity_type") and entry.get("entity_id") is not None
    }
    filtered_requests = []
    seen_pairs: set[tuple[str | None, str | None, int | None]] = set()
    for request in candidate.asset_requests:
        pair = (
            request.asset_kind,
            request.entity_type,
            int(request.entity_id) if request.entity_id is not None else None,
        )
        if pair in existing_pairs or pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        filtered_requests.append(request)
    if len(filtered_requests) == len(candidate.asset_requests):
        return candidate
    return candidate.model_copy(update={"asset_requests": filtered_requests})


def is_nonvisual_speaker_label(value: str) -> bool:
    normalized = (value or "").strip()
    return any(pattern.search(normalized) for pattern in NONVISUAL_SPEAKER_PATTERNS)


def speaker_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (value or "").lower())
        if token not in {"a", "an", "the"}
    }


def normalize_visible_generic_speakers(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
) -> GenerationCandidate:
    selected = packet.get("selected_frontier_item") or {}
    parent_node_id = selected.get("from_node_id")
    if parent_node_id is None or not candidate.dialogue_lines:
        return candidate

    settings = Settings.from_env()
    connection = connect(settings.database_path)
    try:
        canon = CanonResolver(connection)
        story = StoryGraphService(connection)
        character_ids = set(story.list_lineage_entity_ids(int(parent_node_id), "character"))
        parent_node = story.get_story_node(int(parent_node_id)) or {}
        for entity in (parent_node.get("entities") or []):
            if entity.get("entity_type") == "character" and entity.get("entity_id") is not None:
                character_ids.add(int(entity["entity_id"]))
        for entity in (parent_node.get("present_entities") or []):
            if entity.get("entity_type") == "character" and entity.get("entity_id") is not None:
                character_ids.add(int(entity["entity_id"]))
        for entity in candidate.entity_references:
            if entity.entity_type == "character":
                character_ids.add(int(entity.entity_id))
        for entity in candidate.scene_present_entities:
            if entity.entity_type == "character":
                character_ids.add(int(entity.entity_id))

        known_names = {
            str(character.get("name") or "").strip(): speaker_tokens(str(character.get("name") or ""))
            for character_id in sorted(character_ids)
            if (character := canon.get_character(character_id)) is not None and str(character.get("name") or "").strip()
        }
        new_names = {
            character.name.strip(): speaker_tokens(character.name.strip())
            for character in candidate.new_characters
            if character.name.strip()
        }
        name_tokens = {**known_names, **new_names}

        changed = False
        normalized_lines = []
        for line in candidate.dialogue_lines:
            speaker = (line.speaker or "").strip()
            lowered = speaker.lower()
            if not speaker or lowered in {"narrator", "you"} or is_nonvisual_speaker_label(speaker):
                normalized_lines.append(line)
                continue
            if any(speaker.casefold() == name.casefold() for name in name_tokens):
                normalized_lines.append(line)
                continue
            tokens = speaker_tokens(speaker)
            if not tokens:
                normalized_lines.append(line)
                continue
            matches = [
                name
                for name, candidate_tokens in name_tokens.items()
                if (
                    tokens == candidate_tokens
                    or (len(tokens) >= 2 and tokens.issubset(candidate_tokens))
                    or (len(tokens) == 1 and name in new_names and tokens.issubset(candidate_tokens))
                )
            ]
            if len(matches) == 1:
                normalized_lines.append(line.model_copy(update={"speaker": matches[0]}))
                changed = True
                continue
            normalized_lines.append(line)

        if not changed:
            return candidate
        return candidate.model_copy(update={"dialogue_lines": normalized_lines})
    finally:
        connection.close()


def repair_generation_candidate(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
) -> GenerationCandidate:
    updated_candidate = normalize_visible_generic_speakers(packet=packet, candidate=candidate)

    blocked_hook_ids = {
        int(hook["id"])
        for hook in (
            ((packet.get("context_summary") or {}).get("blocked_major_hooks") or [])
            + (((packet.get("context_summary") or {}).get("blocked_major_developments") or []))
        )
        if hook.get("id") is not None
    }
    if blocked_hook_ids:
        filtered_hook_updates = [
            hook_update
            for hook_update in updated_candidate.hook_updates
            if int(hook_update.hook_id) not in blocked_hook_ids
        ]
        if len(filtered_hook_updates) != len(updated_candidate.hook_updates):
            updated_candidate = updated_candidate.model_copy(update={"hook_updates": filtered_hook_updates})

    filtered_floating_intros = [
        intro for intro in updated_candidate.floating_character_introductions
        if int(intro.character_id) != 1
    ]
    if len(filtered_floating_intros) != len(updated_candidate.floating_character_introductions):
        updated_candidate = updated_candidate.model_copy(update={"floating_character_introductions": filtered_floating_intros})

    deduped_choices = []
    for choice in updated_candidate.choices:
        if any(
            texts_are_near_duplicates(choice.choice_text, existing.choice_text)
            or texts_are_near_duplicates(choice.notes or "", existing.notes or "")
            for existing in deduped_choices
        ):
            continue
        deduped_choices.append(choice)
    if len(deduped_choices) != len(updated_candidate.choices) and deduped_choices:
        updated_candidate = updated_candidate.model_copy(update={"choices": deduped_choices})

    if len(updated_candidate.choices) >= 2:
        inferred_classes = [
            choice.choice_class or infer_choice_class_from_text(choice.choice_text, choice.notes)
            for choice in updated_candidate.choices
        ]
        if all(choice_class == "inspection" for choice_class in inferred_classes):
            upgraded_choices = list(updated_candidate.choices)
            for index, choice in enumerate(upgraded_choices):
                if choice.choice_class is not None:
                    continue
                combined_text = " ".join(filter(None, [choice.choice_text, choice.notes or ""])).lower()
                if (
                    choice_text_implies_consequence(choice.choice_text)
                    or any(token in combined_text for token in ("patrol", "hide", "run", "surrender", "approach", "speak", "ask ", "return", "enter", "climb", "descend"))
                ):
                    upgraded_choices[index] = choice.model_copy(update={"choice_class": "commitment"})
                    updated_candidate = updated_candidate.model_copy(update={"choices": upgraded_choices})
                    break

    return updated_candidate

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one local story-worker loop against LM Studio."
    )
    parser.add_argument("--model", required=True, help="Loaded LM Studio model identifier.")
    parser.add_argument("--api-base", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--branch-key")
    parser.add_argument("--choice-id", type=int)
    parser.add_argument("--mode", default="auto", choices=["auto", "manual"])
    parser.add_argument("--requested-choice-count", type=int, default=2)
    parser.add_argument("--play-base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--full-context", action="store_true")
    parser.add_argument("--plan", action="store_true", help="Force planning mode.")
    parser.add_argument("--dry-run", action="store_true", help="Do everything except write changes.")
    parser.add_argument("--temperature", type=float, default=0.4)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--context-run-limit", type=int, default=3)
    parser.add_argument("--reset-context", action="store_true")
    parser.add_argument("--author-mode", choices=["ai", "human"], default="ai")
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=1800.0,
        help="HTTP timeout in seconds for the LM Studio chat completion request.",
    )
    parser.add_argument(
        "--ideas-file",
        help="Optional override for IDEAS.md path, useful for testing or alternate notebooks.",
    )
    parser.add_argument(
        "--log-file",
        help="Optional path to append timestamped run summaries. Defaults to data/worker_logs/local_worker_runs.ndjson",
    )
    return parser


def run_prepare_story_run(args: argparse.Namespace, project_root: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "app.tools.prepare_story_run",
        "--mode",
        args.mode,
        "--requested-choice-count",
        str(args.requested_choice_count),
        "--play-base-url",
        args.play_base_url,
    ]
    if args.branch_key:
        command.extend(["--branch-key", args.branch_key])
    if args.choice_id is not None:
        command.extend(["--choice-id", str(args.choice_id)])
    if args.full_context:
        command.append("--full-context")
    if args.plan:
        command.append("--plan")

    completed = subprocess.run(
        command,
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "prepare_story_run failed"
        raise RuntimeError(stderr)
    return json.loads(completed.stdout)


def load_worker_guide(project_root: Path) -> str:
    return (project_root / "docs" / "llm_story_worker.md").read_text(encoding="utf-8")


def build_system_prompt(worker_guide: str) -> str:
    return (
        "You are the local story worker for this repository.\n"
        "Follow the guide below exactly.\n"
        "Return JSON only. Do not wrap it in markdown fences.\n\n"
        f"{worker_guide}"
    )


def build_normal_conversation_system_prompt(worker_guide: str) -> str:
    return (
        "You are the local story worker for this repository.\n"
        "You are in normal conversational scene-builder mode.\n"
        "Keep continuity across this chat. Treat prior accepted steps and prior accepted runs as real context.\n"
        "Answer only the requested labeled form for the current step. Do not emit JSON in normal mode.\n"
        "Place the final answer in normal assistant content. Do not put the real answer only in hidden reasoning_content.\n"
        "Use NEXT_NODE as a base for your scene, but expand and elaborate on it. Do not simply repeat it.\n\n"
        f"{worker_guide}"
    )


def get_default_session_path(project_root: Path) -> Path:
    override = os.environ.get("CYOA_LOCAL_WORKER_SESSION_FILE")
    if override:
        return Path(override)
    return project_root / "data" / "worker_logs" / "normal_worker_session.json"


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_or_create_normal_session(
    *,
    session_path: Path,
    model: str,
    system_prompt: str,
    context_run_limit: int,
    reset_context: bool,
) -> WorkerSessionState:
    if reset_context or context_run_limit <= 0:
        return WorkerSessionState(
            model=model,
            run_count=0,
            messages=[SessionMessage(role="system", content=system_prompt)],
            created_at=utc_timestamp(),
            updated_at=utc_timestamp(),
        )

    try:
        raw = json.loads(session_path.read_text(encoding="utf-8"))
        session = WorkerSessionState.model_validate(raw)
    except Exception:
        session = WorkerSessionState(
            model=model,
            run_count=0,
            messages=[SessionMessage(role="system", content=system_prompt)],
            created_at=utc_timestamp(),
            updated_at=utc_timestamp(),
        )
        return session

    if session.model != model or session.run_count >= context_run_limit or not session.messages:
        return WorkerSessionState(
            model=model,
            run_count=0,
            messages=[SessionMessage(role="system", content=system_prompt)],
            created_at=utc_timestamp(),
            updated_at=utc_timestamp(),
        )

    first_message = session.messages[0] if session.messages else None
    if first_message is None or first_message.role != "system":
        session.messages.insert(0, SessionMessage(role="system", content=system_prompt))
    else:
        session.messages[0] = SessionMessage(role="system", content=system_prompt)
    session.updated_at = utc_timestamp()
    return session


def save_normal_session(session_path: Path, session: WorkerSessionState) -> None:
    session.updated_at = utc_timestamp()
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(session.model_dump_json(indent=2), encoding="utf-8")


def read_normal_session(session_path: Path) -> WorkerSessionState | None:
    try:
        return WorkerSessionState.model_validate_json(session_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def append_session_message(session: WorkerSessionState, *, role: Literal["system", "user", "assistant"], content: str) -> None:
    session.messages.append(SessionMessage(role=role, content=content))
    session.updated_at = utc_timestamp()


def finalize_normal_session(
    *,
    session: WorkerSessionState,
    session_path: Path,
    outcome_summary: str,
) -> None:
    session.run_count += 1
    session.last_run_outcome = outcome_summary
    save_normal_session(session_path, session)


def session_messages_for_api(session: WorkerSessionState) -> list[dict[str, str]]:
    return [message.model_dump() for message in session.messages]


def strip_markdown_fences(raw_text: str) -> str:
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def build_form_template(step: str) -> str:
    if step == "scene_plan":
        return (
            "This is NOT JSON. Use a simple newline-based labeled input format.\n"
            "Put each field on its own new line. Newlines are the only valid separator between fields. Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between top-level fields.\n"
            "Fill this exact label form:\n"
            "SCENE_TITLE: <string> | Short required scene title.\n"
            "SCENE_SUMMARY: <string> | 1-2 sentence summary of what happens in this scene.\n"
            "MATERIAL_CHANGE: <string> | Say what this scene accomplishes. What is newly true, newly dangerous, newly revealed, newly introduced, or newly decided by the end?\n"
            "OPENING_BEAT: <string; examples: pressure_escalation, discovery, arrival, confrontation, consequence, transition, dialogue_turn, quiet_aftermath> | What kind of beat opens the scene?\n"
            "LOCATION_STATUS: <one of: same_location, new_location, return_location>\n"
            "SCENE_CAST: <write one actual value only: MC_ONLY, SAME, NONE, or comma-separated canonical character ids/names like 1, 2> | Choose one syntax and write only that value. Do not copy the option list itself.\n"
            "NEW_CHARACTERS: <comma-separated character NAMES only, or NONE> | Brand-new characters introduced by this scene. Write names only, not ids, not parentheses, and not name-plus-number formats like 'Bob (2)'. New characters do not need to be listed in SCENE_CAST; they will be appended automatically.\n"
            "NEW_LOCATION: <string or NONE> | Brand-new location name introduced by this scene.\n"
            "RETURN_LOCATION: <existing canonical location id or name, or NONE> | Required when LOCATION_STATUS is return_location.\n"
            "NEW_CHARACTER_INTRO: <string or NONE> | Use for a first-meeting beat if a newly visible person matters now.\n"
            "NEW_LOCATION_INTRO: <string or NONE> | Use for an arrival/transition beat if the location changes."
        )
    if step == "scene_body":
        return (
            "This is NOT JSON. Use a simple newline-based labeled input format.\n"
            "Put each field on its own new line. Newlines are the only valid separator between fields. Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between top-level fields.\n"
            "Fill this exact label form:\n"
            "SCENE_SETTINGS: <setting lines or NONE> | Optional. Omit SCENE_SETTINGS entirely to use the default settings, or write SCENE_SETTINGS: NONE for the same effect. Supported settings are visible_when_speaking, start_show_all_from_last_node, and mc_always_visible. You may use either one-per-line 'key: value' syntax or comma-separated 'key=value' syntax.\n"
            "SCENE_BODY: <script> | Optional label. If you omit the words SCENE_BODY and just enter the script text, it will still be treated as the scene body. Use speaker lines like '0: text', '7: text', or '1n: text', visibility commands like '@show 1' and '@show_only 1n', or plain text which defaults to Narrator.\n"
            "Important: newlines are the ONLY valid separator for switching speakers, starting a new textbox, or applying visibility commands.\n"
            "Put every @show/@hide command on its own line. Put every new speaker on its own line. Do not cram multiple speakers or commands onto one line.\n"
            "Do not use pipes '|', commas, semicolons, arrows, or any other separator characters to separate speakers, textboxes, or visibility changes. Use only newlines.\n"
            "Speaker lines and plain text create textboxes. Lines continue the current textbox until a new speaker line or command line starts.\n"
            "A numbered speaker line like '1:' means that character is SPEAKING out loud. Do not use '1:' or '2:' for narration, stage directions, internal thoughts, or menu options.\n"
            "It is okay if The Tall Gnome talks to himself, but a '1:' line must still be spoken dialogue.\n"
            "When a character speaks, write only the words they say. Do not write attribution like 'says the Tall Gnome' inside the dialogue text; the speaker label already provides that.\n"
            "Think of this like a movie script, where there is narration by a narrator, and anybody else who talks is a speaker. This is like that.\n"
            "Your scene body should usually represent about a page of a movie script overall ~300 words between total narration and character dialogue.\n"
            "Do not put any choices, option lists, or menu text inside SCENE_BODY. SCENE_BODY is only what happens in the scene; the choices will be written in a later step.\n"
            "Visibility commands must each be on their own line and apply at the same moment the next textbox begins.\n"
            "1n refers to a NEW CHARACTER. If you are referring about the protagonist use 1: NOT 1n:."
            "Examples: @show 1 | @show: 1 | @show1 | @show 1,2 | @hide 2 | @show_only 1n | @show_all | @hide_all"
        )
    if step == "choice":
        return (
            "This is NOT JSON. Use a simple newline-based labeled input format.\n"
            "Put each field on its own new line. Newlines are the only valid separator between fields. Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between top-level fields.\n"
            "Fill this exact label form:\n"
            "CHOICE_TEXT: <string> | Player-facing choice text.\n"
            "CHOICE_CLASS: <one of: inspection, progress, commitment, location_transition, ending>\n"
            "NEXT_NODE: <string> | Specific immediate result the next scene should deliver.\n"
            "FURTHER_GOALS: <string> | Broader medium-range direction for the branch.\n"
            "ENDING_CATEGORY: <one of: death, dead_end, capture, transformation, hub_return, NONE> | Use a non-NONE ending category when this choice is a real closure path.\n"
            "TARGET_EXISTING_NODE: <existing node id for a deliberate merge/hub return, or NONE> | Use this when the choice is intentionally merging or routing back into an existing node."
        )
    if step == "link_nodes":
        return (
            "This is NOT JSON. Use a simple newline-based labeled input format.\n"
            "Put each field on its own new line. Newlines are the only valid separator between fields. Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between top-level fields.\n"
            "SCENE_BODY reminders: Use speaker lines like '0: text', '7: text', or '1n: text', visibility commands like '@show 1' and '@show_only 1n', or plain text which defaults to Narrator.\n"
            "Important: newlines are the ONLY valid separator for switching speakers, starting a new textbox, or applying visibility commands.\n"
            "Put every @show/@hide command on its own line. Put every new speaker on its own line. Do not cram multiple speakers or commands onto one line.\n"
            "Do not use pipes '|', commas, semicolons, arrows, or any other separator characters to separate speakers, textboxes, or visibility changes. Use only newlines.\n"
            "Speaker lines and plain text create textboxes. Lines continue the current textbox until a new speaker line or command line starts.\n"
            "A numbered speaker line like '1:' means that character is SPEAKING out loud. Do not use '1:' or '2:' for narration, stage directions, internal thoughts, or menu options.\n"
            "It is okay if The Tall Gnome talks to himself, but a '1:' line must still be spoken dialogue.\n"
            "When a character speaks, write only the words they say. Do not write attribution like 'says the Tall Gnome' inside the dialogue text; the speaker label already provides that.\n"
            "Think of this like a movie script, where there is narration by a narrator, and anybody else who talks is a speaker. This is like that.\n"
            "Your scene body should usually represent about a page of a movie script overall ~300 words between total narration and character dialogue.\n"
            "Do not put any choices, option lists, or menu text inside SCENE_BODY. SCENE_BODY is only what happens in the scene; the choices will be written in a later step.\n"
            "Visibility commands must each be on their own line and apply at the same moment the next textbox begins.\n"
            "1n refers to a NEW CHARACTER. If you are referring about the protagonist use 1: NOT 1n:."
            "Examples: @show 1 | @show: 1 | @show1 | @show 1,2 | @hide 2 | @show_only 1n | @show_all | @hide_all"
            "Fill this exact label form:\n"
            "TRANSITION_TITLE: <string or NONE> | Optional short title for the hidden bridge node.\n"
            "TRANSITION_SUMMARY: <string> | 1-2 sentence summary of how this merge bridge connects the current choice into the target node.\n"
            "SCENE_SETTINGS: <setting lines or NONE> | Optional. Omit SCENE_SETTINGS entirely to use default scene-body settings, or write SCENE_SETTINGS: NONE.\n"
            "SCENE_BODY: <script> | Write the hidden bridge scene here using the same newline-based scene-body format as normal scenes. This transition node must have no choices; it should simply lead smoothly into the target node."
        )
    if step == "hooks":
        return (
            "This is NOT JSON. Use a simple newline-based labeled input format.\n"
            "Put each field on its own new line. Newlines are the only valid separator between fields. Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between top-level fields.\n"
            "Fill this exact label form:\n"
            "HOOK_ACTION: <one of: NONE, NEW_HOOK, UPDATE_HOOK> | END skips this whole step.\n"
            "HOOK_IMPORTANCE: <major, minor, local, or NONE> | Only needed for NEW_HOOK.\n"
            "HOOK_TYPE: <string or NONE> | Short behind-the-scenes hook category.\n"
            "HOOK_SUMMARY: <string or NONE> | What thread now exists or sharpens.\n"
            "HOOK_PAYOFF_CONCEPT: <string or NONE> | How this could matter later.\n"
            "HOOK_ID: <existing hook id or NONE> | Only needed for UPDATE_HOOK.\n"
            "HOOK_STATUS: <active, payoff_ready, resolved, blocked, or NONE> | Only needed for UPDATE_HOOK.\n"
            "HOOK_PROGRESS_NOTE: <string or NONE> | What changed on the existing hook.\n"
            "CLUE_TAGS: <comma-separated tags, or NONE> | Small-scale discoveries newly made true in this scene.\n"
            "STATE_TAGS: <comma-separated tags, or NONE> | Small-scale branch state changes newly made true in this scene.\n"
            "GLOBAL_DIRECTION_NOTES: <NONE, or one or more lines: note_type | title | note_text | priority> | Behind-the-scenes steering for future scenes."
        )
    if step == "details":
        return (
            "This is NOT JSON. Use a simple newline-based labeled input format.\n"
            "Put each field on its own new line. Newlines are the only valid separator between fields. Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between top-level fields.\n"
            "Fill this exact label form:\n"
            "CHARACTER_DETAILS: <one or more lines: Name | Description> | Required when NEW_CHARACTERS was declared above.\n"
            "CHARACTER_ART_HINTS: <one or more lines: Name | Visual hint> | Required when NEW_CHARACTERS was declared above.\n"
            "LOCATION_DETAILS: <one or more lines: Name | Description> | Required when NEW_LOCATION was declared above.\n"
            "LOCATION_ART_HINTS: <one or more lines: Name | Visual hint> | Required when NEW_LOCATION was declared above."
        )
    return build_form_template("details")


def build_details_form_template(*, state: NormalRunConversationState) -> str:
    if state.scene_plan is None:
        return build_form_template("details")
    lines = ["Fill this exact label form:"]
    if state.scene_plan.new_character_names:
        lines.append("CHARACTER_DETAILS: <one or more lines: Name | Description> | Required. Only define the NEW_CHARACTERS declared above.")
        lines.append(
            "CHARACTER_ART_HINTS: <one or more lines: Name | Visual hint> | Required. Focus on stable visual traits like silhouette, materials, colors, props, age/read, and standout features. Avoid plot events or vague praise."
        )
    if state.scene_plan.new_location_name:
        lines.append("LOCATION_DETAILS: <one or more lines: Name | Description> | Required. Only define the NEW_LOCATION declared above.")
        lines.append(
            "LOCATION_ART_HINTS: <one or more lines: Name | Visual hint> | Required. Focus on structure, materials, lighting, atmosphere, and scale. Avoid camera jargon unless truly necessary."
        )
    return "\n".join(lines)


def build_detail_target_form_template(*, target_type: Literal["character", "location"], target_name: str) -> str:
    lines = ["Fill this exact label form:"]
    if target_type == "character":
        lines.append(
            f'CHARACTER_DETAILS: <string> Write a short canonical description of {target_name} for continuity.'
        )
        lines.append(
            f'CHARACTER_ART_HINTS: <string> Write a short visual description of {target_name} for image generation. Focus on stable visual traits like silhouette, materials, colors, props, age/read, and standout features. Avoid plot events or vague praise.'
        )
    else:
        lines.append(
            f'LOCATION_DETAILS: <string> Write a short canonical description of {target_name} for continuity.'
        )
        lines.append(
            f'LOCATION_ART_HINTS: <string> Write a short visual description of {target_name} for image generation. Focus on structure, materials, lighting, atmosphere, and scale. Avoid camera jargon unless truly necessary.'
        )
    return "\n".join(lines)


def get_expected_detail_labels(*, state: NormalRunConversationState) -> list[str]:
    labels: list[str] = []
    if state.scene_plan and state.scene_plan.new_character_names:
        labels.append("CHARACTER_DETAILS")
        labels.append("CHARACTER_ART_HINTS")
    if state.scene_plan and state.scene_plan.new_location_name:
        labels.append("LOCATION_DETAILS")
        labels.append("LOCATION_ART_HINTS")
    return labels


def get_detail_targets(*, state: NormalRunConversationState) -> list[tuple[Literal["character", "location"], str]]:
    if state.scene_plan is None:
        return []
    targets: list[tuple[Literal["character", "location"], str]] = []
    for name in state.scene_plan.new_character_names:
        cleaned = name.strip()
        if cleaned:
            targets.append(("character", cleaned))
    if state.scene_plan.new_location_name and state.scene_plan.new_location_name.strip():
        targets.append(("location", state.scene_plan.new_location_name.strip()))
    return targets


def parse_labeled_sections(raw_text: str, labels: list[str]) -> dict[str, str]:
    stripped = strip_markdown_fences(raw_text)
    sections: dict[str, list[str]] = {}
    current_label: str | None = None
    label_set = set(labels)

    for line in stripped.splitlines():
        match = re.match(r"^([A-Z0-9_]+):\s*(.*)$", line.rstrip())
        if match and match.group(1) in label_set:
            current_label = match.group(1)
            sections[current_label] = [match.group(2).strip()] if match.group(2).strip() else []
            continue
        if current_label is not None:
            sections[current_label].append(line.rstrip())

    missing = [label for label in labels if label not in sections]
    if missing:
        raise ValueError(f"Missing required labels: {', '.join(missing)}")

    return {
        label: "\n".join(line for line in sections[label] if line is not None).strip()
        for label in labels
    }


def parse_none_text(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned or cleaned.upper() == "NONE":
        return None
    return cleaned


def parse_optional_leading_int(value: str) -> int | None:
    cleaned = parse_none_text(value or "")
    if cleaned is None:
        return None
    match = re.match(r"^\s*(\d+)", cleaned)
    if match is None:
        raise ValueError(f"Expected an integer or NONE, got: {value!r}")
    return int(match.group(1))


def parse_comma_list(value: str) -> list[str]:
    cleaned = parse_none_text(value)
    if cleaned is None:
        return []
    return [item.strip() for item in cleaned.split(",") if item.strip()]


def should_skip_optional_choice(raw_text: str) -> bool:
    cleaned = strip_markdown_fences(raw_text).strip().upper()
    return cleaned in OPTIONAL_CHOICE_SKIP_MARKERS


def is_force_next_override(raw_text: str) -> bool:
    cleaned = strip_markdown_fences(raw_text).strip()
    normalized = re.sub(r"[^a-z]", "", cleaned.lower())
    return normalized in {"forcenext", "forcenxt"}


def mark_force_next_step(state: NormalRunConversationState, step_key: str) -> None:
    if step_key not in state.force_next_steps:
        state.force_next_steps.append(step_key)


def build_forced_scene_plan_draft(*, packet: dict[str, Any], resolution: dict[str, Any]) -> ScenePlanDraft:
    selected = packet.get("selected_frontier_item") or {}
    protagonist_name = (resolution.get("protagonist_name") or "the protagonist").strip()
    choice_id = selected.get("choice_id")
    title_suffix = f" {choice_id}" if choice_id is not None else ""
    return ScenePlanDraft(
        scene_title=f"Forced Preview Scene{title_suffix}",
        scene_summary=f"This scene plan was force-advanced in human mode so you could continue previewing later steps for {protagonist_name}.",
        material_change="FORCE NEXT advanced past scene planning without requiring complete authored content.",
        opening_beat="transition",
        location_status="same_location",
        scene_cast_mode="mc_only",
        scene_cast_entries=[],
        new_character_names=[],
        new_location_name=None,
        new_character_intro=None,
        new_location_intro=None,
    )


def build_forced_scene_body_draft() -> SceneBodyDraft:
    text = "You pause here. FORCE NEXT skipped the full scene body so you could keep moving through the authoring flow."
    return SceneBodyDraft(
        settings=SceneSettingsDraft(),
        raw_body=text,
        textboxes=[SceneScriptTextbox(speaker_ref="0", text=text)],
    )


def build_forced_choice_draft(
    *,
    packet: dict[str, Any],
    choice_index: int,
) -> ChoiceDraft:
    constraints = packet.get("frontier_choice_constraints") or {}
    merge_candidates = ((packet.get("context_summary") or {}).get("merge_candidates") or [])
    if constraints.get("must_include_merge_or_closure"):
        if merge_candidates:
            merge_target = merge_candidates[0]
            target_node_id = int(merge_target.get("node_id") or 0)
            target_title = str(merge_target.get("title") or f"node {target_node_id}").strip()
            return ChoiceDraft(
                choice_text=f"Merge back into {target_title}",
                choice_class="progress",
                next_node=f"The branch deliberately reconverges at {target_title}.",
                further_goals="Relieve frontier pressure by merging into an existing node during preview navigation.",
                ending_category=None,
                target_existing_node=target_node_id,
            )
        return ChoiceDraft(
            choice_text="Close this thread here",
            choice_class="ending",
            next_node="This preview-only path closes here instead of opening another fresh branch.",
            further_goals="Relieve frontier pressure with a closure path during preview navigation.",
            ending_category="dead_end",
            target_existing_node=None,
        )
    return ChoiceDraft(
        choice_text=f"Forced preview choice {choice_index + 1}",
        choice_class="progress",
        next_node="The run advances with a placeholder choice because FORCE NEXT was used in human mode.",
        further_goals="Continue previewing later authoring steps without committing to final content yet.",
        ending_category=None,
        target_existing_node=None,
    )


def build_forced_transition_node_draft(
    *,
    choice_index: int,
    target_existing_node: int,
) -> TransitionNodeDraft:
    body_text = (
        "You move through the missing connective beat here. FORCE NEXT inserted a hidden bridge so this merge can still preview cleanly."
    )
    return TransitionNodeDraft(
        choice_index=choice_index,
        target_existing_node=target_existing_node,
        scene_title="Forced Preview Bridge",
        scene_summary="A hidden transition node carries the player smoothly into the merge target during preview navigation.",
        body=SceneBodyDraft(
            settings=SceneSettingsDraft(),
            raw_body=body_text,
            textboxes=[SceneScriptTextbox(speaker_ref="0", text=body_text)],
        ),
    )


def build_forced_detail_target_response(
    *,
    target_type: Literal["character", "location"],
    target_name: str,
) -> tuple[SceneExtrasDraft, SceneArtDraft]:
    if target_type == "character":
        return (
            SceneExtrasDraft(
                new_characters=[
                    CharacterSeed(
                        name=target_name,
                        description="Placeholder character details inserted by FORCE NEXT for preview navigation.",
                    )
                ]
            ),
            SceneArtDraft(
                character_art_hints={
                    target_name: "Placeholder visual hint inserted by FORCE NEXT for preview navigation."
                }
            ),
        )
    return (
        SceneExtrasDraft(
            new_locations=[
                LocationSeed(
                    name=target_name,
                    description="Placeholder location details inserted by FORCE NEXT for preview navigation.",
                )
            ]
        ),
        SceneArtDraft(
            location_art_hints={
                target_name: "Placeholder visual hint inserted by FORCE NEXT for preview navigation."
            }
        ),
    )


def parse_scene_cast_value(value: str) -> tuple[Literal["none", "mc_only", "same", "explicit"], list[str]]:
    cleaned = (value or "").strip()
    normalized = cleaned.upper()
    if not cleaned or normalized == "NONE":
        return "none", []
    if normalized == "MC_ONLY":
        return "mc_only", []
    if normalized == "SAME":
        return "same", []
    return "explicit", [item.strip() for item in cleaned.split(",") if item.strip()]


def parse_line_entries(value: str) -> list[str]:
    cleaned = parse_none_text(value)
    if cleaned is None:
        return []
    entries: list[str] = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        entries.append(line)
    return entries


def parse_bool_setting(value: str) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean setting value: {value!r}")


def split_text_into_textbox_chunks(text: str, *, sentences_per_chunk: int = 2) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    if sentences_per_chunk <= 0:
        return [cleaned]
    sentence_parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    if len(sentence_parts) <= sentences_per_chunk:
        return [cleaned]
    chunks: list[str] = []
    for index in range(0, len(sentence_parts), sentences_per_chunk):
        chunk = " ".join(sentence_parts[index : index + sentences_per_chunk]).strip()
        if chunk:
            chunks.append(chunk)
    return chunks or [cleaned]


def parse_scene_settings(value: str) -> SceneSettingsDraft:
    cleaned = parse_none_text(value)
    if cleaned is None:
        return SceneSettingsDraft()
    settings = SceneSettingsDraft()
    allowed_keys = {"visible_when_speaking", "start_show_all_from_last_node", "mc_always_visible"}
    entries: list[str] = []
    for raw_line in parse_line_entries(cleaned):
        parts = [part.strip() for part in raw_line.split(",") if part.strip()]
        if parts:
            entries.extend(parts)
    for line in entries:
        if ":" in line:
            key, raw_value = line.split(":", 1)
        elif "=" in line:
            key, raw_value = line.split("=", 1)
        else:
            raise ValueError("SCENE_SETTINGS lines must use either 'setting_name: value' or 'setting_name=value'.")
        key = key.strip()
        if key not in allowed_keys:
            raise ValueError(f"Unknown SCENE_SETTINGS key: {key}")
        setattr(settings, key, parse_bool_setting(raw_value))
    return settings


def split_command_targets(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def is_narrator_ref(value: str) -> bool:
    return (value or "").strip().lower() in {"0", "n", "narrator"}


def parse_scene_script_command(line: str) -> SceneScriptCommand | None:
    stripped = line.strip()
    if not stripped.startswith("@"):
        return None
    lowered = stripped.lower()
    if lowered in {"@show_all", "@showall", "@show all"}:
        return SceneScriptCommand(action="show_all")
    if lowered in {"@hide_all", "@hideall", "@hide all"}:
        return SceneScriptCommand(action="hide_all")
    for action in ("show_only", "show", "hide"):
        prefix = f"@{action}"
        if lowered.startswith(prefix):
            rest = stripped[len(prefix):]
            if rest.startswith(":"):
                rest = rest[1:]
            rest = rest.strip()
            if not rest:
                raise ValueError(f"@{action} requires at least one target.")
            return SceneScriptCommand(action=cast(Any, action), targets=split_command_targets(rest))
    raise ValueError(f"Unknown scene body command: {line}")


def parse_scene_speaker_line(line: str) -> tuple[str, str] | None:
    if ":" not in line:
        return None
    prefix, remainder = line.split(":", 1)
    speaker_ref = prefix.strip()
    if not speaker_ref:
        return None
    if len(speaker_ref) > 40:
        return None
    if any(mark in speaker_ref for mark in ".!?"):
        return None
    word_count = len(speaker_ref.split())
    lowered_ref = speaker_ref.lower()
    if re.fullmatch(r"\d+n?|\d+", lowered_ref) or lowered_ref in {"n", "narrator"}:
        return speaker_ref, remainder.strip()
    if word_count > 4:
        return None
    if re.fullmatch(r"[A-Za-z][A-Za-z'\\-]*(?:\s+[A-Za-z][A-Za-z'\\-]*){0,3}", speaker_ref):
        return speaker_ref, remainder.strip()
    return None


def parse_scene_script_textboxes(value: str) -> list[SceneScriptTextbox]:
    raw_body = (value or "").strip()
    if not raw_body:
        raise ValueError("SCENE_BODY cannot be empty.")

    textboxes: list[SceneScriptTextbox] = []
    pending_commands: list[SceneScriptCommand] = []
    current_speaker = "0"
    current_lines: list[str] = []
    has_current = False

    def flush_current() -> None:
        nonlocal current_lines, has_current
        if not has_current:
            return
        text = "\n".join(line.rstrip() for line in current_lines).strip()
        if text:
            textboxes.append(
                SceneScriptTextbox(
                    speaker_ref=current_speaker,
                    text=text,
                    pending_commands=list(pending_commands_snapshot),
                )
            )
        current_lines = []
        has_current = False

    pending_commands_snapshot: list[SceneScriptCommand] = []
    for raw_line in raw_body.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            if has_current:
                current_lines.append("")
            continue
        command = parse_scene_script_command(line)
        if command is not None:
            flush_current()
            pending_commands.append(command)
            continue
        stripped_line = line.strip()
        if stripped_line.lower() in {"narrator", "n"}:
            flush_current()
            current_speaker = "0"
            current_lines = []
            pending_commands_snapshot = list(pending_commands)
            pending_commands = []
            has_current = True
            continue
        speaker_match = parse_scene_speaker_line(line)
        if speaker_match:
            flush_current()
            current_speaker = speaker_match[0].strip()
            current_lines = [speaker_match[1]]
            pending_commands_snapshot = list(pending_commands)
            pending_commands = []
            has_current = True
            continue
        if not has_current:
            current_speaker = "0"
            current_lines = [line]
            pending_commands_snapshot = list(pending_commands)
            pending_commands = []
            has_current = True
            continue
        current_lines.append(line)

    flush_current()
    if pending_commands:
        raise ValueError("Visibility commands must be followed by a textbox.")
    if not textboxes:
        raise ValueError("SCENE_BODY must contain at least one textbox.")
    return textboxes


def parse_scene_plan_form(raw_text: str) -> ScenePlanDraft:
    sections = parse_labeled_sections(raw_text, NORMAL_STEP_LABELS["scene_plan"])
    scene_cast_mode, scene_cast_entries = parse_scene_cast_value(sections["SCENE_CAST"])
    return_location_match = re.search(r"(?m)^RETURN_LOCATION:\s*(.*)$", strip_markdown_fences(raw_text).strip())
    return ScenePlanDraft(
        scene_title=sections["SCENE_TITLE"].strip(),
        scene_summary=sections["SCENE_SUMMARY"].strip(),
        material_change=sections["MATERIAL_CHANGE"].strip(),
        opening_beat=sections["OPENING_BEAT"].strip(),
        location_status=cast(
            Literal["same_location", "new_location", "return_location"],
            sections["LOCATION_STATUS"].strip(),
        ),
        scene_cast_mode=scene_cast_mode,
        scene_cast_entries=scene_cast_entries,
        new_character_names=parse_comma_list(sections["NEW_CHARACTERS"]),
        new_location_name=parse_none_text(sections["NEW_LOCATION"]),
        return_location_ref=parse_none_text(return_location_match.group(1)) if return_location_match else None,
        new_character_intro=parse_none_text(sections["NEW_CHARACTER_INTRO"]),
        new_location_intro=parse_none_text(sections["NEW_LOCATION_INTRO"]),
    )


def parse_scene_body_form(raw_text: str) -> SceneBodyDraft:
    stripped = strip_markdown_fences(raw_text).strip()
    has_settings_label = bool(re.search(r"(?m)^SCENE_SETTINGS:\s*", stripped))
    has_body_label = bool(re.search(r"(?m)^SCENE_BODY:\s*", stripped))

    if not has_settings_label and not has_body_label:
        scene_body = stripped
        return SceneBodyDraft(
            settings=SceneSettingsDraft(),
            raw_body=scene_body,
            textboxes=parse_scene_script_textboxes(scene_body),
        )

    if has_body_label and not has_settings_label:
        sections = parse_labeled_sections(
            f"SCENE_SETTINGS: NONE\n{stripped}",
            NORMAL_STEP_LABELS["scene_body"],
        )
    elif has_settings_label and not has_body_label:
        settings_match = re.search(r"(?ms)^SCENE_SETTINGS:\s*(.*)$", stripped)
        settings_text = settings_match.group(1).strip() if settings_match else stripped
        raise ValueError(
            "SCENE_BODY is missing. If you only want to write the scene script, omit SCENE_SETTINGS entirely and just enter the body text."
            if settings_text
            else "SCENE_BODY is missing."
        )
    else:
        sections = parse_labeled_sections(stripped, NORMAL_STEP_LABELS["scene_body"])

    return SceneBodyDraft(
        settings=parse_scene_settings(sections["SCENE_SETTINGS"]),
        raw_body=sections["SCENE_BODY"].strip(),
        textboxes=parse_scene_script_textboxes(sections["SCENE_BODY"]),
    )


def parse_transition_node_form(*, raw_text: str, choice_index: int, target_existing_node: int) -> TransitionNodeDraft:
    sections = parse_labeled_sections(strip_markdown_fences(raw_text).strip(), NORMAL_STEP_LABELS["link_nodes"])
    body = SceneBodyDraft(
        settings=parse_scene_settings(sections["SCENE_SETTINGS"]),
        raw_body=sections["SCENE_BODY"].strip(),
        textboxes=parse_scene_script_textboxes(sections["SCENE_BODY"]),
    )
    return TransitionNodeDraft(
        choice_index=choice_index,
        target_existing_node=target_existing_node,
        scene_title=parse_none_text(sections["TRANSITION_TITLE"]),
        scene_summary=sections["TRANSITION_SUMMARY"].strip(),
        body=body,
    )


def parse_choice_form(raw_text: str) -> ChoiceDraft:
    sections = parse_labeled_sections(raw_text, NORMAL_STEP_LABELS["choice"])
    return ChoiceDraft(
        choice_text=sections["CHOICE_TEXT"].strip(),
        choice_class=cast(ChoiceClass, sections["CHOICE_CLASS"].strip()),
        next_node=sections["NEXT_NODE"].strip(),
        further_goals=sections["FURTHER_GOALS"].strip(),
        ending_category=cast(EndingCategory | None, parse_none_text(sections["ENDING_CATEGORY"])),
        target_existing_node=parse_optional_leading_int(sections["TARGET_EXISTING_NODE"]),
    )


def parse_scene_extras_form(raw_text: str, *, expected_labels: list[str] | None = None) -> SceneExtrasDraft:
    sections = parse_labeled_sections(raw_text, expected_labels or NORMAL_STEP_LABELS["details"])

    new_characters = []
    for entry in parse_line_entries(sections.get("CHARACTER_DETAILS", "NONE")):
        parts = [part.strip() for part in entry.split("|", 1)]
        if len(parts) != 2:
            raise ValueError("CHARACTER_DETAILS entries must use 'Name | Description'.")
        new_characters.append(CharacterSeed(name=parts[0], description=parts[1]))

    new_locations = []
    for entry in parse_line_entries(sections.get("LOCATION_DETAILS", "NONE")):
        parts = [part.strip() for part in entry.split("|", 1)]
        if len(parts) != 2:
            raise ValueError("LOCATION_DETAILS entries must use 'Name | Description'.")
        new_locations.append(LocationSeed(name=parts[0], description=parts[1]))

    return SceneExtrasDraft(
        new_characters=new_characters,
        new_locations=new_locations,
    )


def parse_scene_hooks_form(raw_text: str) -> SceneHooksDraft:
    sections = parse_labeled_sections(raw_text, NORMAL_STEP_LABELS["hooks"])

    global_notes = []
    for entry in parse_line_entries(sections["GLOBAL_DIRECTION_NOTES"]):
        parts = [part.strip() for part in entry.split("|", 3)]
        if len(parts) != 4:
            raise ValueError("GLOBAL_DIRECTION_NOTES entries must use 'note_type | title | note_text | priority'.")
        global_notes.append(
            DirectionNoteProposal(
                note_type=parts[0],
                title=parts[1],
                note_text=parts[2],
                priority=int(parts[3]),
            )
        )

    hook_action = (parse_none_text(sections["HOOK_ACTION"]) or "NONE").strip().upper()
    action_map = {
        "NONE": "none",
        "NEW_HOOK": "new_hook",
        "UPDATE_HOOK": "update_hook",
    }
    if hook_action not in action_map:
        raise ValueError("HOOK_ACTION must be one of NONE, NEW_HOOK, UPDATE_HOOK.")

    hook_importance = parse_none_text(sections["HOOK_IMPORTANCE"])
    hook_status = parse_none_text(sections["HOOK_STATUS"])
    return SceneHooksDraft(
        hook_action=cast(Any, action_map[hook_action]),
        hook_importance=cast(Any, hook_importance.lower()) if hook_importance else None,
        hook_type=parse_none_text(sections["HOOK_TYPE"]),
        hook_summary=parse_none_text(sections["HOOK_SUMMARY"]),
        hook_payoff_concept=parse_none_text(sections["HOOK_PAYOFF_CONCEPT"]),
        hook_id=parse_optional_leading_int(sections["HOOK_ID"]),
        hook_status=cast(Any, hook_status.lower()) if hook_status else None,
        hook_progress_note=parse_none_text(sections["HOOK_PROGRESS_NOTE"]),
        clue_tags=parse_comma_list(sections["CLUE_TAGS"]),
        state_tags=parse_comma_list(sections["STATE_TAGS"]),
        global_direction_notes=global_notes,
    )


def parse_scene_art_form(raw_text: str, *, expected_labels: list[str] | None = None) -> SceneArtDraft:
    sections = parse_labeled_sections(raw_text, expected_labels or NORMAL_STEP_LABELS["art"])

    character_hints: dict[str, str] = {}
    for entry in parse_line_entries(sections.get("CHARACTER_ART_HINTS", "NONE")):
        parts = [part.strip() for part in entry.split("|", 1)]
        if len(parts) != 2:
            raise ValueError("CHARACTER_ART_HINTS entries must use 'Name | Visual hint'.")
        character_hints[parts[0]] = parts[1]

    location_hints: dict[str, str] = {}
    for entry in parse_line_entries(sections.get("LOCATION_ART_HINTS", "NONE")):
        parts = [part.strip() for part in entry.split("|", 1)]
        if len(parts) != 2:
            raise ValueError("LOCATION_ART_HINTS entries must use 'Name | Visual hint'.")
        location_hints[parts[0]] = parts[1]

    return SceneArtDraft(
        character_art_hints=character_hints,
        location_art_hints=location_hints,
    )


def parse_detail_target_response(
    raw_text: str,
    *,
    target_type: Literal["character", "location"],
    target_name: str,
) -> tuple[SceneExtrasDraft, SceneArtDraft]:
    expected_labels = (
        ["CHARACTER_DETAILS", "CHARACTER_ART_HINTS"]
        if target_type == "character"
        else ["LOCATION_DETAILS", "LOCATION_ART_HINTS"]
    )
    sections = parse_labeled_sections(raw_text, expected_labels)

    if target_type == "character":
        description = parse_none_text(sections["CHARACTER_DETAILS"])
        art_hint = parse_none_text(sections["CHARACTER_ART_HINTS"])
        if description is None:
            raise ValueError("CHARACTER_DETAILS must be a non-empty string.")
        if art_hint is None:
            raise ValueError("CHARACTER_ART_HINTS must be a non-empty string.")
        return (
            SceneExtrasDraft(new_characters=[CharacterSeed(name=target_name, description=description)]),
            SceneArtDraft(character_art_hints={target_name: art_hint}),
        )

    description = parse_none_text(sections["LOCATION_DETAILS"])
    art_hint = parse_none_text(sections["LOCATION_ART_HINTS"])
    if description is None:
        raise ValueError("LOCATION_DETAILS must be a non-empty string.")
    if art_hint is None:
        raise ValueError("LOCATION_ART_HINTS must be a non-empty string.")
    return (
        SceneExtrasDraft(new_locations=[LocationSeed(name=target_name, description=description)]),
        SceneArtDraft(location_art_hints={target_name: art_hint}),
    )


def merge_scene_extras(base: SceneExtrasDraft | None, addition: SceneExtrasDraft) -> SceneExtrasDraft:
    base = base or SceneExtrasDraft()
    return SceneExtrasDraft(
        new_characters=[*base.new_characters, *addition.new_characters],
        new_locations=[*base.new_locations, *addition.new_locations],
    )


def merge_scene_art(base: SceneArtDraft | None, addition: SceneArtDraft) -> SceneArtDraft:
    base = base or SceneArtDraft()
    merged_character_hints = dict(base.character_art_hints)
    merged_character_hints.update(addition.character_art_hints)
    merged_location_hints = dict(base.location_art_hints)
    merged_location_hints.update(addition.location_art_hints)
    return SceneArtDraft(
        character_art_hints=merged_character_hints,
        location_art_hints=merged_location_hints,
    )


def build_normal_run_intro_prompt(
    *,
    packet: dict[str, Any],
    previous_outcome: str | None,
) -> str:
    outcome_note = ""
    if previous_outcome:
        outcome_note = (
            "Previous run outcome note:\n"
            f"{previous_outcome}\n\n"
        )
    return (
        "This is a new normal story-worker run.\n"
        "The packet below is authoritative. Use it instead of guessing.\n"
        "Treat earlier rejected drafts as non-canon unless they were explicitly accepted.\n"
        "Use NEXT_NODE as a base for your scene, but expand and elaborate on it. Do not simply repeat it.\n"
        "Answer only the requested labeled form for each step. Do not emit JSON in normal mode.\n\n"
        f"{outcome_note}"
        "Packet:\n"
        f"{json.dumps(packet, indent=2)}"
    )


def summarize_recent_cast_references(packet: dict[str, Any]) -> str:
    current_node = ((packet.get("context_summary") or {}).get("current_node") or {})
    current_present_entities = current_node.get("present_entities") or []
    encountered_characters = ((packet.get("path_character_continuity") or {}).get("encountered_characters") or [])

    protagonist_refs: list[str] = []
    recent_refs: list[str] = []
    for entry in encountered_characters[:6]:
        name = (entry.get("name") or "").strip()
        character_id = entry.get("id")
        if not name or character_id is None:
            continue
        label = f"{name} ({character_id})"
        if (entry.get("role") or "").lower() in {"player", "hero", "protagonist"} or not protagonist_refs:
            if label not in protagonist_refs:
                protagonist_refs.append(label)
        else:
            recent_refs.append(label)

    if not protagonist_refs:
        for entity in current_present_entities:
            if entity.get("entity_type") == "character" and entity.get("entity_id"):
                protagonist_refs.append(f"Character {entity['entity_id']} ({entity['entity_id']})")
                break

    protagonist_text = protagonist_refs[0] if protagonist_refs else "unknown protagonist"
    recent_text = ", ".join(recent_refs[:5]) if recent_refs else "no recent non-protagonist characters"
    return (
        f"  Current cast snippet: MC_ONLY={protagonist_text} | recent: {recent_text}\n"
        "  Examples: MC_ONLY | NONE | SAME | 3, 7 | Bob, Joe | 3, New Character Guy\n"
        "  New scene refs: existing canon keeps its real id, new characters are assigned 1n, 2n, 3n in the accepted scene cast."
    )


def resolve_scene_cast_names(
    *,
    draft: ScenePlanDraft,
    resolution: dict[str, Any],
) -> list[str]:
    protagonist_name = (resolution.get("protagonist_name") or "").strip()
    resolved_names: list[str] = []
    if draft.scene_cast_mode == "mc_only":
        if protagonist_name:
            resolved_names.append(protagonist_name)
    elif draft.scene_cast_mode == "same":
        resolved_names.extend(list(resolution.get("current_visible_cast_names") or []))
    elif draft.scene_cast_mode == "explicit":
        pass
    elif draft.scene_cast_mode == "none":
        pass

    character_name_map = resolution.get("character_name_map") or {}
    character_id_map = resolution.get("character_id_map") or {}
    if draft.scene_cast_mode == "explicit":
        for entry in draft.scene_cast_entries:
            token = entry.strip()
            if not token:
                continue
            if token.upper() == "MC_ONLY":
                if protagonist_name:
                    resolved_names.append(protagonist_name)
                continue
            if token.isdigit():
                character = character_id_map.get(int(token))
                if character and (character.get("name") or "").strip():
                    resolved_names.append((character.get("name") or "").strip())
                else:
                    resolved_names.append(token)
                continue
            resolved_names.append((character_name_map.get(token.lower()) or {}).get("name") or token)
    for name in draft.new_character_names:
        cleaned = name.strip()
        if cleaned:
            resolved_names.append(cleaned)
    if draft.scene_cast_mode != "none" and protagonist_name:
        protagonist_lowered = protagonist_name.strip().lower()
        if protagonist_lowered not in {name.strip().lower() for name in resolved_names if name.strip()}:
            resolved_names.insert(0, protagonist_name)
    deduped: list[str] = []
    seen: set[str] = set()
    for name in resolved_names:
        lowered = name.strip().lower()
        if not lowered or lowered in seen:
            continue
        deduped.append(name.strip())
        seen.add(lowered)
    return deduped


def validate_scene_cast_entries(
    *,
    draft: ScenePlanDraft,
    resolution: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    if draft.scene_cast_mode != "explicit":
        return issues

    character_name_map = resolution.get("character_name_map") or {}
    character_id_map = resolution.get("character_id_map") or {}
    declared_new_names = {
        name.strip().lower()
        for name in draft.new_character_names
        if name.strip()
    }

    for entry in draft.scene_cast_entries:
        token = (entry or "").strip()
        if not token or token.upper() == "MC_ONLY":
            continue
        if token.isdigit():
            if int(token) not in character_id_map:
                issues.append(
                    f"SCENE_CAST uses numeric id '{token}', but no existing canonical character has that id."
                )
            continue
        lowered = token.lower()
        if lowered in character_name_map:
            continue
        if lowered in declared_new_names:
            continue
        issues.append(
            f"SCENE_CAST includes '{token}', but that name is neither an existing canonical character nor a declared NEW_CHARACTER."
        )
    return issues


def format_scene_cast_summary(
    *,
    draft: ScenePlanDraft,
    resolution: dict[str, Any],
) -> str:
    resolved = resolve_scene_cast_names(draft=draft, resolution=resolution)
    if resolved:
        return ", ".join(resolved)
    if draft.scene_cast_mode == "none":
        return "NONE"
    if draft.scene_cast_mode == "mc_only":
        return "MC_ONLY"
    if draft.scene_cast_mode == "same":
        return "SAME"
    return ", ".join(draft.scene_cast_entries) if draft.scene_cast_entries else "NONE"


def build_scene_cast_index_map(
    *,
    draft: ScenePlanDraft,
    resolution: dict[str, Any],
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    cast_names = resolve_scene_cast_names(draft=draft, resolution=resolution)
    ref_to_name = {"0": "Narrator"}
    exact_name_lookup = {"narrator": "Narrator", "n": "Narrator"}
    character_name_map = resolution.get("character_name_map") or {}
    character_id_map = resolution.get("character_id_map") or {}
    protagonist_name = (resolution.get("protagonist_name") or "").strip()
    protagonist_id = resolution.get("protagonist_id")
    for index, name in enumerate(cast_names, start=1):
        cleaned = name.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered == protagonist_name.strip().lower() and protagonist_name:
            ref = str(resolution.get("protagonist_id") or index)
        elif lowered in character_name_map and character_name_map[lowered].get("id") is not None:
            ref = str(int(character_name_map[lowered]["id"]))
        elif lowered in {entry.strip().lower() for entry in draft.new_character_names if entry.strip()}:
            new_index = list(
                entry.strip().lower() for entry in draft.new_character_names if entry.strip()
            ).index(lowered) + 1
            ref = f"{new_index}n"
        else:
            ref = str(index)
        ref_to_name[ref] = cleaned
        exact_name_lookup[cleaned.strip().lower()] = cleaned
    if protagonist_name:
        exact_name_lookup[protagonist_name.strip().lower()] = protagonist_name
    if draft.scene_cast_mode == "mc_only":
        protagonist_label = protagonist_name
        if not protagonist_label and protagonist_id is not None:
            protagonist_label = str((character_id_map.get(int(protagonist_id)) or {}).get("name") or "").strip()
        if protagonist_label:
            ref_to_name.setdefault("1", protagonist_label)
            exact_name_lookup[protagonist_label.strip().lower()] = protagonist_label
            if protagonist_id is not None:
                ref_to_name.setdefault(str(int(protagonist_id)), protagonist_label)
    return cast_names, ref_to_name, exact_name_lookup


def format_scene_cast_block(
    *,
    draft: ScenePlanDraft,
    resolution: dict[str, Any],
) -> str:
    cast_names, ref_to_name, _ = build_scene_cast_index_map(draft=draft, resolution=resolution)
    lines = ["Accepted scene cast:", "0 | Narrator"]
    for ref, name in ref_to_name.items():
        if ref == "0":
            continue
        suffix = " (new)" if ref.endswith("n") else ""
        lines.append(f"{ref} | {name}{suffix}")
    if len(lines) == 2 and not cast_names:
        lines.append("(No visible character participants declared for this scene.)")
    return "\n".join(lines)


def build_frontier_constraint_block(packet: dict[str, Any]) -> str:
    constraints = packet.get("frontier_choice_constraints") or {}
    pressure_level = str(constraints.get("pressure_level") or (packet.get("frontier_budget_state") or {}).get("pressure_level") or "normal").strip()
    if pressure_level not in {"soft", "hard"}:
        return ""
    max_fresh_choices = int(constraints.get("max_fresh_choices_under_pressure") or 1)
    lines = [
        "Current frontier pressure rules:",
        f"- pressure level: {pressure_level}",
    ]
    if constraints.get("must_include_merge_or_closure"):
        lines.append("- include at least one merge or closure path in this scene")
        lines.append("- THIS RUN WILL ONLY VALIDATE if at least one choice uses TARGET_EXISTING_NODE to merge into an existing node or uses a non-NONE ENDING_CATEGORY for a real closure")
        lines.append("- you will apply that merge/closure requirement during the choice creation steps")
    lines.append(f"- allow at most {max_fresh_choices} fresh branch choice(s) in this scene under pressure")
    if constraints.get("inspection_choices_should_reconverge_under_pressure"):
        lines.append("- inspection choices should reconverge quickly and should not open durable fresh leaves")
    if constraints.get("allow_second_fresh_choice_only_for_bloom_scenes"):
        lines.append("- a second fresh branch is only acceptable for a genuine bloom scene")
    guidance = str(constraints.get("guidance") or "").strip()
    if guidance:
        lines.append(f"- guidance: {guidance}")
    return "\n".join(lines) + "\n\n"


def build_step_prompt(
    *,
    step_name: str,
    packet: dict[str, Any],
    state: NormalRunConversationState,
    requested_choice_count: int,
    issues: list[str] | None = None,
    choice_index: int | None = None,
    optional_choice: bool = False,
    detail_target: tuple[Literal["character", "location"], str] | None = None,
    transition_target: tuple[int, int] | None = None,
    retry_index: int = 0,
) -> str:
    issue_block = ""
    if issues:
        issue_block = (
            "Your previous response for this step failed.\n"
            f"Issues:\n{json.dumps(issues, indent=2)}\n\n"
        )
    retry_block = f"Retry: {retry_index}\n" if retry_index > 0 else ""

    scene_plan_summary = ""
    if state.scene_plan is not None:
        scene_cast_summary = format_scene_cast_summary(draft=state.scene_plan, resolution=resolve_normal_context(packet))
        scene_plan_summary = (
            "Accepted scene plan so far:\n"
            f"- title: {state.scene_plan.scene_title}\n"
            f"- summary: {state.scene_plan.scene_summary}\n"
            f"- material change: {state.scene_plan.material_change}\n"
            f"- location_status: {state.scene_plan.location_status}\n"
            f"- return_location: {state.scene_plan.return_location_ref or 'NONE'}\n"
            f"- scene_cast: {scene_cast_summary}\n\n"
        )

    scene_body_summary = ""
    if state.scene_body is not None:
        scene_body_summary = (
            "Accepted scene body so far:\n"
            f"- textboxes: {len(state.scene_body.textboxes)}\n"
            f"- raw body words: {len(state.scene_body.raw_body.split())} words\n\n"
        )

    accepted_choices_summary = ""
    if state.choices:
        accepted_choices_summary = "Accepted choices so far:\n" + "\n".join(
            f"- {index + 1}: {choice.choice_text} [{choice.choice_class}]"
            for index, choice in enumerate(state.choices)
        ) + "\n\n"

    frontier_constraint_block = build_frontier_constraint_block(packet)
    staleness_pressure_block = build_staleness_pressure_block(packet)

    declared_new_characters = ", ".join(state.scene_plan.new_character_names) if state.scene_plan and state.scene_plan.new_character_names else "NONE"
    declared_new_location = state.scene_plan.new_location_name if state.scene_plan and state.scene_plan.new_location_name else "NONE"
    declared_return_location = state.scene_plan.return_location_ref if state.scene_plan and state.scene_plan.return_location_ref else "NONE"

    if step_name == "scene_plan":
        cast_refs = summarize_recent_cast_references(packet)
        scene_plan_template = build_form_template("scene_plan").replace(
            "SCENE_CAST: <write one actual value only: MC_ONLY, SAME, NONE, or comma-separated canonical character ids/names like 1, 2> | Choose one syntax and write only that value. Do not copy the option list itself.\n",
            "SCENE_CAST: <write one actual value only: MC_ONLY, SAME, NONE, or comma-separated canonical character ids/names like 1, 2> | Choose one syntax and write only that value. Do not copy the option list itself.\n"
            f"{cast_refs}\n",
        )
        return (
            f"{issue_block}"
            "Step: scene_plan\n"
            f"{retry_block}"
            f"{frontier_constraint_block}"
            f"{staleness_pressure_block}"
            "Newlines are the ONLY valid separator between top-level fields in this step. Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between fields.\n"
            "Decide the immediate shape of the next scene.\n"
            "The scene plan must materially advance the branch, not restate the parent beat.\n"
            "SCENE_CAST lists the characters available to appear in this scene. It does not force immediate visibility.\n"
            "When pressure is active, use IDEAS.md as a main source of fresh people, places, and whimsical turns.\n"
            "Prefer whimsical, readable, unexpected developments over another direct derivative of the current patrol/vault/seam beat.\n"
            "For SCENE_CAST, write one actual value only, such as MC_ONLY or 1, 2. Do not repeat the syntax guide or the option list.\n"
            "For NEW_CHARACTERS, write only the new characters' names, comma-separated if needed. Do not invent ids, slot numbers, or parenthetical numbers there. Any actual ids are assigned later by the system at apply time.\n"
            "New characters do not need to be repeated in SCENE_CAST. If you declare them in NEW_CHARACTERS, they will be added to the accepted scene cast automatically.\n"
            f"Already encountered path-safe locations for RETURN_LOCATION: {', '.join(entry.get('name') for entry in ((packet.get('path_location_continuity') or {}).get('encountered_locations') or []) if entry.get('name')) or 'NONE'}.\n"
            "If LOCATION_STATUS is return_location, use RETURN_LOCATION to name a path-safe existing location from that list. Do not guess an unseen canonical place.\n"
            "Respond with ONLY the provided fields and corresponding values. Do not add any other fields not present at this step of the run.\n"
            f"{scene_plan_template}"
        )
    if step_name == "scene_body":
        scene_cast_block = ""
        if state.scene_plan is not None:
            scene_cast_block = (
                f"{format_scene_cast_block(draft=state.scene_plan, resolution=resolve_normal_context(packet))}\n"
                f"Scene settings defaults:\n"
                f"visible_when_speaking: {str(state.scene_body.settings.visible_when_speaking).lower() if state.scene_body else 'true'}\n"
                f"start_show_all_from_last_node: {str(state.scene_body.settings.start_show_all_from_last_node).lower() if state.scene_body else 'true'}\n"
                f"mc_always_visible: {str(state.scene_body.settings.mc_always_visible).lower() if state.scene_body else 'true'}\n\n"
            )
        return (
            f"{issue_block}"
            f"{scene_plan_summary}"
            "Step: scene_body\n"
            f"{retry_block}"
            f"{frontier_constraint_block}"
            f"{staleness_pressure_block}"
            f"{scene_cast_block}"
            "Newlines are the ONLY valid separator between top-level fields in this step. Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between fields.\n"
            "Write the scripted scene body.\n"
            "Make this feel like a real story passage, not a stub. Usually write multiple beats before choices appear.\n"
            "When pressure is active, use IDEAS.md as a main source of fresh people, places, and whimsical turns.\n"
            "Prefer whimsical, readable, unexpected developments over another direct derivative of the current patrol/vault/seam beat.\n"
            f"Declared location setup so far: NEW_LOCATION={declared_new_location}; RETURN_LOCATION={declared_return_location}.\n"
            "Use the accepted scene cast refs when possible. Existing canon keeps its real id; brand-new characters use refs like 1n, 2n. Exact speaker names also work.\n"
            "When the narrator is speaking about the protagonist, refer to the protagonist as 'you' and 'your', not by name. The narrator should NEVER say \"The Tall Gnome\"\n"
            "If you want the protagonist visibly on-screen, the protagonist must be included in SCENE_CAST. mc_always_visible keeps the protagonist visible only if the protagonist is already in the accepted scene cast.\n"
            "You may omit SCENE_SETTINGS entirely to use the default settings shown above, or write SCENE_SETTINGS: NONE for the same effect.\n"
            "You may also omit the SCENE_BODY label entirely and just write the script itself; the tool will treat it as SCENE_BODY.\n"
            "Newlines are the ONLY valid separator for switching speakers, starting a new textbox, or applying visibility commands.\n"
            "Every new speaker and every visibility command must start on its own new line. Do not put multiple speakers or commands on the same line.\n"
            "Do not use pipes '|', commas, semicolons, arrows, or any other separator characters to separate speakers, textboxes, or visibility changes. Use only newlines.\n"
            "Plain text with no prefix becomes Narrator text. Lines continue the current textbox until a new speaker line or command line starts.\n"
            "A numbered speaker line like '1:' means that character is SPEAKING out loud. Do not use '1:' or '2:' for narration, stage directions, internal thoughts, or menu options.\n"
            "It is okay if The Tall Gnome talks to himself, but a '1:' line must still be spoken dialogue.\n"
            "Dialogue text should be only what the character says. Do not add attribution like 'says the Tall Gnome' inside the spoken line; the speaker label already handles that.\n"
            "If someone visible speaks on-screen, they must be a named existing character in SCENE_CAST or a named NEW_CHARACTER you declared for this scene. Do not invent fresh visible generic labels like 'Surveyor' or 'Guard' for on-screen dialogue.\n"
            "If the person should stay generic for now, keep them in Narrator text or make them explicitly offscreen instead of giving them a visible speaker line.\n"
            "Do not put any choices, option lists, or menu text inside SCENE_BODY. SCENE_BODY is only what happens in the scene; the choices will be written in a later step.\n"
            "Visibility commands must each be isolated on their own line, with a newline before and after the command. They apply at the same moment the next textbox begins.\n"
            "DO NOT USE SHOW OR HIDE WITHOUT A NEWLINE BEFORE AND AFTER THE COMMAND.\n"
            "Respond with ONLY the provided fields and corresponding values. Do not add any other fields not present at this step of the run.\n"
            f"{build_form_template('scene_body')}"
        )
    if step_name == "choice":
        assert choice_index is not None
        consequence_note = ""
        merge_or_closure_required_now = bool(
            choice_index == 0 and ((packet.get("frontier_choice_constraints") or {}).get("must_include_merge_or_closure"))
        )
        if choice_index == 0 and ((packet.get("consequential_choice_requirement") or {}).get("required")):
            consequence_note = (
                "At least one menu choice this scene must be a commitment, social move, location shift, merge, closure, or immediate-pressure response. Prefer making this choice satisfy that requirement unless a later choice clearly will.\n"
            )
        location_transition_pressure = packet.get("location_stall_pressure") or {}
        location_transition_note = ""
        if location_transition_pressure.get("active") and not any(choice.choice_class == "location_transition" for choice in state.choices):
            location_transition_note = (
                "Location-stall pressure is active. This menu must include at least one CHOICE_CLASS: location_transition option.\n"
                "A location_transition choice promises that its future expansion will move to a different location than the current one.\n"
                "Use path_location_continuity when returning to an existing place, or point toward a brand-new place if that fits better.\n"
            )
        choice_header = f"Write choice {choice_index + 1} of {requested_choice_count}.\n"
        if optional_choice:
            prior_count_text = "1 choice" if choice_index == 1 else f"{choice_index} choices"
            choice_header = (
                f"Write an optional choice {choice_index + 1}.\n"
                f"If the scene should stay at {prior_count_text}, reply with only END.\n"
            )
        merge_or_closure_instruction = (
            "MAKE this choice either a merge or a closure. THIS RUN WILL ONLY VALIDATE if this choice uses TARGET_EXISTING_NODE for a deliberate merge into an existing node or uses a non-NONE ENDING_CATEGORY for a true closure.\n"
            "Fix that here in this choice-writing phase, not earlier and not later.\n"
            if merge_or_closure_required_now
            else "TARGET_EXISTING_NODE should be NONE unless this is a deliberate merge or hub return.\n"
        )
        return (
            f"{issue_block}"
            f"{scene_plan_summary}"
            f"{scene_body_summary}"
            f"{accepted_choices_summary}"
            f"Step: choice_{choice_index + 1}\n"
            f"{retry_block}"
            f"{frontier_constraint_block}"
            f"{staleness_pressure_block}"
            "Newlines are the ONLY valid separator between top-level fields in this step. Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between fields.\n"
            f"{choice_header}"
            "This choice must open a genuinely distinct lane from the others. Do not repeat the just-taken choice or the parent's NEXT_NODE.\n"
            f"{consequence_note}"
            f"{location_transition_note}"
            "Allowed CHOICE_CLASS values are only: inspection, progress, commitment, location_transition, ending.\n"
            "If the move is socially bold, urgent, or otherwise strongly consequential, usually use CHOICE_CLASS: commitment rather than inventing a new class label.\n"
            f"{merge_or_closure_instruction}"
            "Respond with ONLY the provided fields and corresponding values. Do not add any other fields not present at this step of the run.\n"
            f"{build_form_template('choice')}"
        )
    if step_name == "hooks":
        return (
            f"{issue_block}"
            f"{scene_plan_summary}"
            f"{scene_body_summary}"
            f"{accepted_choices_summary}"
            "Step: hooks\n"
            f"{retry_block}"
            f"{frontier_constraint_block}"
            "Newlines are the ONLY valid separator between top-level fields in this step. Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between fields.\n"
            "Reminder: if frontier pressure required a merge or closure path, this run only validates if one of your choices already used TARGET_EXISTING_NODE for a deliberate merge or a non-NONE ENDING_CATEGORY for a real closure.\n"
            "Has your scene text introduced any new unknowns, mysteries, tensions, or behind-the-scenes threads that should matter later?\n"
            "If yes, fill out the hook or clue fields below. If not, reply with only END to skip this step entirely.\n"
            "Hooks are the larger story threads that should pay off later. Clue tags and state tags are the smaller-scale things this scene newly reveals or makes true.\n"
            "Respond with ONLY the provided fields and corresponding values. Do not add any other fields not present at this step of the run.\n"
            f"{build_form_template('hooks')}"
        )
    if step_name == "link_nodes":
        assert transition_target is not None
        choice_list_index, target_node_id = transition_target
        linked_choice = state.choices[choice_list_index]
        target_node = next(
            (
                candidate
                for candidate in ((packet.get("context_summary") or {}).get("merge_candidates") or [])
                if int(candidate.get("node_id") or 0) == target_node_id
            ),
            None,
        )
        target_title = str((target_node or {}).get("title") or f"Node {target_node_id}").strip()
        target_summary = str((target_node or {}).get("summary") or "No target summary available.").strip()
        return (
            f"{issue_block}"
            f"{scene_plan_summary}"
            f"{scene_body_summary}"
            f"{accepted_choices_summary}"
            f"Step: link_nodes for choice_{choice_list_index + 1}\n"
            f"{retry_block}"
            "Newlines are the ONLY valid separator between top-level fields in this step. Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between fields.\n"
            "Write a hidden transition node that bridges this merge choice into its target existing node.\n"
            "This bridge node has no choices. The player should experience it as a seamless connective beat and then continue directly into the target node.\n"
            f"Choice to bridge: {linked_choice.choice_text}\n"
            f"Choice NEXT_NODE: {linked_choice.next_node}\n"
            f"Target existing node id: {target_node_id}\n"
            f"Target title: {target_title}\n"
            f"Target summary: {target_summary}\n"
            "Do not teleport abruptly. Show the connective motion, exchange, realization, or arrival that makes the merge feel earned.\n"
            "Do not add choices here. End at the natural handoff into the target node.\n"
            "You may use the same scene-body scripting format here, including Narrator, speaker lines, and visibility commands.\n"
            "Respond with ONLY the provided fields and corresponding values. Do not add any other fields not present at this step of the run.\n"
            f"{build_form_template('link_nodes')}"
        )
    if step_name == "details":
        target_type, target_name = detail_target if detail_target is not None else ("character", "Unknown")
        target_label = "character" if target_type == "character" else "location"
        return (
            f"{issue_block}"
            f"{scene_plan_summary}"
            f"{scene_body_summary}"
            "Step: details\n"
            f"{retry_block}"
            "Newlines are the ONLY valid separator between top-level fields in this step. Do not use pipes '|', slashes '/', backslashes '\\', commas, or other inline separators between fields.\n"
            f'Define the {target_label} "{target_name}" you declared in the first phase.\n'
            "These descriptions become the canonical seed details used for continuity and later asset generation.\n"
            "Put the visual generation hint here too so the same step covers both canon description and art guidance.\n"
            "Respond with ONLY the provided fields and corresponding values. Do not add any other fields not present at this step of the run.\n"
            f"{build_detail_target_form_template(target_type=target_type, target_name=target_name)}"
        )
    raise ValueError(f"Unsupported step_name for conversational builder: {step_name}")


def request_human_step(prompt_text: str) -> str:
    print(prompt_text)
    print("\nEnter response. Finish with a line containing only END.\n")
    lines: list[str] = []
    while True:
        line = input()
        if is_force_next_override(line):
            return line.strip()
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def resolve_normal_context(packet: dict[str, Any]) -> dict[str, Any]:
    selected = packet.get("selected_frontier_item") or {}
    current_node = (packet.get("context_summary") or {}).get("current_node") or {}
    current_entities = current_node.get("entities") or []
    encountered = ((packet.get("path_character_continuity") or {}).get("encountered_characters") or [])
    encountered_locations = ((packet.get("path_location_continuity") or {}).get("encountered_locations") or [])

    settings = Settings.from_env()
    connection = connect(settings.database_path)
    try:
        canon = CanonResolver(connection)
        characters = canon.list_characters()
        locations = canon.list_locations()
    finally:
        connection.close()

    character_name_map = {
        (character.get("name") or "").strip().lower(): character
        for character in characters
        if (character.get("name") or "").strip()
    }
    character_id_map = {
        int(character["id"]): character
        for character in characters
        if character.get("id") is not None
    }
    location_name_map = {
        (location.get("name") or "").strip().lower(): location
        for location in locations
        if (location.get("name") or "").strip()
    }
    encountered_names = {
        (entry.get("name") or "").strip().lower()
        for entry in encountered
        if (entry.get("name") or "").strip()
    }
    path_location_name_map = {
        (entry.get("name") or "").strip().lower(): entry
        for entry in encountered_locations
        if (entry.get("name") or "").strip()
    }
    path_location_id_map = {
        int(entry["id"]): entry
        for entry in encountered_locations
        if entry.get("id") is not None
    }
    encountered_location_names = {
        (entry.get("name") or "").strip().lower()
        for entry in encountered_locations
        if (entry.get("name") or "").strip()
    }
    protagonist = next(
        (
            entity for entity in current_entities
            if entity.get("entity_type") == "character"
            and entity.get("entity_id")
            and entity.get("role") in {"player", "hero", "protagonist", "introduced"}
        ),
        None,
    )
    current_location = next(
        (
            entity for entity in current_entities
            if entity.get("entity_type") == "location"
            and entity.get("entity_id")
            and entity.get("role") == "current_scene"
        ),
        None,
    )
    protagonist_name = None
    protagonist_id = None
    if protagonist and protagonist.get("entity_id"):
        protagonist_id = int(protagonist["entity_id"])
        for known_character in characters:
            if int(known_character.get("id") or 0) == protagonist_id:
                protagonist_name = known_character.get("name")
                break
    if protagonist_name:
        encountered_names.add(protagonist_name.strip().lower())
    current_visible_cast_names: list[str] = []
    for entity in current_node.get("present_entities") or []:
        if entity.get("entity_type") != "character" or entity.get("entity_id") is None:
            continue
        character = character_id_map.get(int(entity["entity_id"]))
        if character is None or not (character.get("name") or "").strip():
            continue
        current_visible_cast_names.append((character.get("name") or "").strip())
    current_location_id = int(current_location["entity_id"]) if current_location and current_location.get("entity_id") else None
    current_location_record = (
        path_location_id_map.get(current_location_id)
        or (location_name_map.get((packet.get("parent_current_location") or {}).get("name", "").strip().lower()) if packet.get("parent_current_location") else None)
        or None
    )
    return {
        "selected_choice_text": selected.get("choice_text") or "",
        "selected_choice_notes": selected.get("existing_choice_notes") or "",
        "current_summary": current_node.get("summary") or "",
        "current_scene_text": current_node.get("scene_text") or "",
        "character_name_map": character_name_map,
        "character_id_map": character_id_map,
        "location_name_map": location_name_map,
        "path_location_name_map": path_location_name_map,
        "path_location_id_map": path_location_id_map,
        "encountered_names": encountered_names,
        "encountered_location_names": encountered_location_names,
        "current_visible_cast_names": current_visible_cast_names,
        "protagonist_id": protagonist_id,
        "protagonist_name": protagonist_name,
        "current_location_id": current_location_id,
        "current_location_name": (
            current_location_record.get("name")
            if isinstance(current_location_record, dict)
            else (packet.get("parent_current_location") or {}).get("name")
        ),
    }


def collect_plan_names(texts: list[str], resolution: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    encountered_names = set(resolution.get("encountered_names") or set())
    for lowered_name, character in (resolution.get("character_name_map") or {}).items():
        if not lowered_name or lowered_name in encountered_names:
            continue
        name = character.get("name") or ""
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", re.IGNORECASE)
        if any(pattern.search(text or "") for text in texts):
            issues.append(
                f"'{name}' is not yet a safe known character on this path. Either keep them unnamed for now or explicitly introduce them."
            )
    return issues


def resolve_scene_reference_name(
    *,
    raw_ref: str,
    ref_to_name: dict[str, str],
    exact_name_lookup: dict[str, str],
    allow_narrator: bool,
) -> str | None:
    token = (raw_ref or "").strip()
    if not token:
        return None
    lowered_token = token.lower()
    if lowered_token in ref_to_name:
        if lowered_token == "0" and not allow_narrator:
            return None
        return ref_to_name[lowered_token]
    lowered = token.lower()
    if allow_narrator and lowered in {"narrator", "n"}:
        return "Narrator"
    return exact_name_lookup.get(lowered)


def compile_scene_body_draft(
    *,
    state: NormalRunConversationState,
    draft: SceneBodyDraft,
    resolution: dict[str, Any],
) -> tuple[CompiledSceneBody | None, list[str]]:
    if state.scene_plan is None:
        return None, ["Cannot compile SCENE_BODY before SCENE_CAST is accepted."]

    cast_names, ref_to_name, exact_name_lookup = build_scene_cast_index_map(
        draft=state.scene_plan,
        resolution=resolution,
    )
    all_character_names = [name.strip() for name in cast_names if name.strip()]
    all_character_lookup = {name.strip().lower(): name.strip() for name in all_character_names}
    protagonist_name = (resolution.get("protagonist_name") or "").strip()
    new_character_names = {
        name.strip().lower(): name.strip()
        for name in all_character_names
        if name.strip().lower() not in (resolution.get("character_name_map") or {})
        and name.strip().lower() != protagonist_name.strip().lower()
    }

    issues: list[str] = []
    protagonist_lowered = protagonist_name.strip().lower()
    manual_visible_order: list[str] = []
    if draft.settings.start_show_all_from_last_node:
        for name in (resolution.get("current_visible_cast_names") or []):
            lowered_name = name.strip().lower()
            if lowered_name in all_character_lookup and lowered_name not in manual_visible_order:
                manual_visible_order.append(lowered_name)
    auto_visible: set[str] = set()
    dialogue_lines: list[DialogueLine] = []
    scene_paragraphs: list[str] = []
    hidden_lines_by_character = {
        name.strip().lower(): []
        for name in all_character_names
    }

    def limit_visible_order(order: list[str], *, prioritized: list[str] | None = None) -> list[str]:
        prioritized = [name for name in (prioritized or []) if name and name != protagonist_lowered]
        non_protagonists = [name for name in order if name != protagonist_lowered]
        while len(non_protagonists) > 2:
            removable_index = next(
                (index for index, name in enumerate(non_protagonists) if name not in prioritized),
                0,
            )
            del non_protagonists[removable_index]
        rebuilt: list[str] = []
        if protagonist_lowered and protagonist_lowered in order:
            rebuilt.append(protagonist_lowered)
        for name in non_protagonists:
            if name not in rebuilt:
                rebuilt.append(name)
        return rebuilt

    def show_character(lowered_name: str) -> None:
        nonlocal manual_visible_order
        if not lowered_name:
            return
        manual_visible_order = [name for name in manual_visible_order if name != lowered_name]
        manual_visible_order.append(lowered_name)
        manual_visible_order = limit_visible_order(manual_visible_order)

    def hide_character(lowered_name: str) -> None:
        nonlocal manual_visible_order
        manual_visible_order = [name for name in manual_visible_order if name != lowered_name]

    manual_visible_order = limit_visible_order(manual_visible_order)

    def resolve_command_targets(command: SceneScriptCommand) -> list[str]:
        resolved_targets: list[str] = []
        for target in command.targets:
            if is_narrator_ref(target):
                continue
            resolved_name = resolve_scene_reference_name(
                raw_ref=target,
                ref_to_name=ref_to_name,
                exact_name_lookup=exact_name_lookup,
                allow_narrator=False,
            )
            if resolved_name is None:
                if re.fullmatch(r"\d+n?|\d+", target.strip().lower()):
                    issues.append(f"SCENE_BODY command target '{target}' does not match a valid accepted scene cast ref.")
                elif target.strip().lower() in new_character_names:
                    resolved_name = new_character_names[target.strip().lower()]
                else:
                    issues.append(f"SCENE_BODY command target '{target}' does not match SCENE_CAST or a declared new character name.")
            if resolved_name is not None and resolved_name.strip().lower() != "narrator":
                resolved_targets.append(resolved_name.strip())
        return resolved_targets

    compiled_line_index = 0
    for textbox in draft.textboxes:
        for command in textbox.pending_commands:
            resolved_targets = resolve_command_targets(command)
            lowered_targets = [name.strip().lower() for name in resolved_targets]
            if command.action == "show":
                for lowered_target in lowered_targets:
                    show_character(lowered_target)
            elif command.action == "hide":
                for lowered_target in lowered_targets:
                    hide_character(lowered_target)
            elif command.action == "show_only":
                manual_visible_order = []
                for lowered_target in lowered_targets:
                    show_character(lowered_target)
            elif command.action == "show_all":
                manual_visible_order = []
                for character_name in all_character_names:
                    show_character(character_name.strip().lower())
            elif command.action == "hide_all":
                manual_visible_order = []

        resolved_speaker = resolve_scene_reference_name(
            raw_ref=textbox.speaker_ref,
            ref_to_name=ref_to_name,
            exact_name_lookup=exact_name_lookup,
            allow_narrator=True,
        )
        if resolved_speaker is None:
            if re.fullmatch(r"\d+n?|\d+", textbox.speaker_ref.strip().lower()):
                issues.append(f"SCENE_BODY speaker ref '{textbox.speaker_ref}' does not match the accepted scene cast.")
            elif textbox.speaker_ref.strip().lower() in new_character_names:
                resolved_speaker = new_character_names[textbox.speaker_ref.strip().lower()]
            else:
                issues.append(f"SCENE_BODY speaker '{textbox.speaker_ref}' does not match SCENE_CAST.")
                resolved_speaker = textbox.speaker_ref.strip()

        if draft.settings.visible_when_speaking and resolved_speaker not in {"Narrator", protagonist_name}:
            auto_visible = {resolved_speaker.strip().lower()} if resolved_speaker.strip() else set()
        elif draft.settings.visible_when_speaking and resolved_speaker == protagonist_name:
            auto_visible = {protagonist_name.strip().lower()} if protagonist_name.strip() else set()
        else:
            auto_visible = set()

        effective_visible_order = list(manual_visible_order)
        for lowered_name in auto_visible:
            if lowered_name and lowered_name != protagonist_lowered:
                effective_visible_order = [name for name in effective_visible_order if name != lowered_name]
                effective_visible_order.append(lowered_name)
        effective_visible_order = limit_visible_order(
            effective_visible_order,
            prioritized=[name for name in auto_visible if name != protagonist_lowered],
        )
        effective_visible = set(effective_visible_order) | {name for name in auto_visible if name == protagonist_lowered}
        if draft.settings.mc_always_visible and protagonist_name.strip():
            if protagonist_lowered in all_character_lookup:
                effective_visible.add(protagonist_lowered)

        speaker_label = "Narrator"
        if resolved_speaker == protagonist_name and protagonist_name.strip():
            speaker_label = "You"
        elif resolved_speaker and resolved_speaker.strip():
            speaker_label = resolved_speaker.strip()

        text = textbox.text.strip()
        if text:
            text_chunks = split_text_into_textbox_chunks(text, sentences_per_chunk=2)
            for chunk in text_chunks:
                for character_name in all_character_names:
                    lowered_name = character_name.strip().lower()
                    if lowered_name not in effective_visible:
                        hidden_lines_by_character[lowered_name].append(compiled_line_index)
                dialogue_lines.append(DialogueLine(speaker=speaker_label, text=chunk))
                scene_paragraphs.append(chunk)
                compiled_line_index += 1

    return (
        CompiledSceneBody(
            scene_text="\n\n".join(scene_paragraphs).strip(),
            dialogue_lines=dialogue_lines,
            hidden_lines_by_character=hidden_lines_by_character,
        ),
        issues,
    )


def validate_scene_plan_draft(
    *,
    packet: dict[str, Any],
    draft: ScenePlanDraft,
    resolution: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    issues.extend(validate_scene_cast_entries(draft=draft, resolution=resolution))
    if not draft.scene_title.strip() or draft.scene_title.strip().upper() == "NONE":
        issues.append("SCENE_TITLE is required and cannot be NONE.")
    if not draft.scene_summary.strip():
        issues.append("SCENE_SUMMARY cannot be empty.")
    if not draft.opening_beat.strip():
        issues.append("OPENING_BEAT cannot be empty.")
    parent_summary = resolution.get("current_summary") or ""
    if parent_summary and texts_are_near_duplicates(draft.scene_summary, parent_summary):
        issues.append("SCENE_SUMMARY is too close to the parent summary. Advance the situation materially.")
    if len((draft.material_change or "").split()) < 4:
        issues.append("MATERIAL_CHANGE should state a concrete change, not a fragment.")
    declared_new_names = {name.strip().lower() for name in draft.new_character_names if name.strip()}
    resolved_visible_names = resolve_scene_cast_names(draft=draft, resolution=resolution)
    protagonist_name = (resolution.get("protagonist_name") or "").strip().lower()
    non_protagonist_visible = [name for name in resolved_visible_names if name.strip().lower() != protagonist_name]
    if draft.location_status == "new_location" and not draft.new_location_name:
        issues.append("NEW_LOCATION is required when LOCATION_STATUS is new_location.")
    if draft.location_status == "new_location" and not draft.new_location_intro:
        issues.append("NEW_LOCATION_INTRO is required when LOCATION_STATUS is new_location.")
    if draft.location_status == "return_location" and not (draft.return_location_ref or "").strip():
        issues.append("RETURN_LOCATION is required when LOCATION_STATUS is return_location.")
    if draft.location_status == "return_location":
        target_location = resolve_return_location_target(draft=draft, resolution=resolution)
        if target_location is None:
            issues.append(
                "RETURN_LOCATION must name an already encountered path-safe location by id or name. Use path_location_continuity as the safe set."
            )
        else:
            current_location_id = resolution.get("current_location_id")
            if current_location_id is not None and int(target_location["id"]) == int(current_location_id):
                issues.append("RETURN_LOCATION must be a different location from the parent current location.")
    unknown_or_off_path_visible = []
    for name in non_protagonist_visible:
        lowered = name.strip().lower()
        if not lowered:
            continue
        if lowered not in (resolution.get("character_name_map") or {}) and lowered not in declared_new_names:
            unknown_or_off_path_visible.append(name)
        elif lowered not in (resolution.get("encountered_names") or set()) and not draft.new_character_intro:
            issues.append(f"SCENE_CAST includes '{name}' before a first-meeting beat. Provide NEW_CHARACTER_INTRO or keep them offstage for now.")
    if unknown_or_off_path_visible and not draft.new_character_intro:
        issues.append("A newly visible character needs NEW_CHARACTER_INTRO.")
    texts_to_scan = [
        draft.scene_summary,
        draft.material_change,
        ", ".join(draft.new_character_names),
        draft.new_location_name or "",
        draft.return_location_ref or "",
        draft.new_character_intro or "",
        draft.new_location_intro or "",
    ]
    issues.extend(collect_plan_names(texts_to_scan, resolution))
    isolation_pressure = packet.get("isolation_pressure") or {}
    if isolation_pressure.get("active") and not scene_plan_satisfies_isolation_pressure(draft=draft, resolution=resolution):
        issues.append(
            "Isolation pressure is active and this scene setup still looks effectively solitary. Fix SCENE_CAST and/or NEW_CHARACTERS, or make the plan explicitly bring clear faction/social pressure onstage. A new location alone does not satisfy this."
        )
    new_character_pressure = packet.get("new_character_pressure") or {}
    if new_character_pressure.get("active") and not scene_plan_satisfies_new_character_pressure(draft=draft):
        issues.append(
            "New-character pressure is active and this scene setup still does not introduce a brand-new character. Add NEW_CHARACTERS and NEW_CHARACTER_INTRO. Reusing only existing characters does not satisfy this."
        )
    location_transition_obligation = packet.get("location_transition_obligation") or {}
    if location_transition_obligation.get("active") and not scene_plan_satisfies_location_transition_obligation(
        draft=draft,
        resolution=resolution,
    ):
        issues.append(
            "This selected choice promised a location transition, so this scene setup must change location now. Use LOCATION_STATUS: new_location with NEW_LOCATION or LOCATION_STATUS: return_location with RETURN_LOCATION pointing at a different path-safe existing location."
        )
    lowered_text = " ".join(texts_to_scan).lower()
    if any(token in lowered_text for token in ("full backstory", "true mastermind", "secret ruler", "the king")):
        issues.append("This scene plan reveals too much too early. Keep large hidden powers and full backstory deferred.")
    return issues


def validate_scene_body_draft(
    *,
    packet: dict[str, Any],
    state: NormalRunConversationState,
    draft: SceneBodyDraft,
    resolution: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    compiled, compile_issues = compile_scene_body_draft(state=state, draft=draft, resolution=resolution)
    issues.extend(compile_issues)
    scene_text = compiled.scene_text if compiled is not None else draft.raw_body
    dialogue_lines = compiled.dialogue_lines if compiled is not None else []
    word_count = len(scene_text.split()) + sum(len(line.text.split()) for line in dialogue_lines)
    if word_count < 45:
        issues.append("SCENE_BODY is too short. Write a fuller scene passage before presenting choices.")
    next_node = ((packet.get("choice_handoff") or {}).get("next_node") or "").strip()
    if next_node and texts_are_near_duplicates(scene_text, next_node):
        issues.append("SCENE_BODY too closely repeats NEXT_NODE. Expand it into a real scene instead of restating the handoff.")
    if state.scene_plan and texts_are_near_duplicates(scene_text, state.scene_plan.scene_summary):
        issues.append("SCENE_BODY too closely repeats SCENE_SUMMARY. Add actual development, description, and consequence.")
    issues.extend(collect_plan_names([scene_text, *(line.text for line in dialogue_lines)], resolution))
    lowered_body_text = " ".join([draft.raw_body, scene_text, *(line.text for line in dialogue_lines)]).lower()
    if any(
        phrase in lowered_body_text
        for phrase in (
            "you can choose",
            "choose how to react",
            "choose how you react",
            "option 1",
            "option 2",
            "choice 1",
            "choice 2",
        )
    ):
        issues.append("SCENE_BODY must not contain menu text or explicit choice prompts. Only write what happens in the scene; choices come later.")
    issues.extend(collect_narrator_owned_dialogue_issues(state=state, draft=draft, resolution=resolution))
    issues.extend(collect_scene_body_pressure_issues(packet=packet, state=state, draft=draft, scene_text=scene_text, resolution=resolution))
    return issues


def collect_scene_body_pressure_issues(
    *,
    packet: dict[str, Any],
    state: NormalRunConversationState,
    draft: SceneBodyDraft,
    scene_text: str,
    resolution: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    if state.scene_plan is None:
        return issues
    lowered_scene_text = " ".join(filter(None, [draft.raw_body, scene_text])).lower()
    isolation_pressure = packet.get("isolation_pressure") or {}
    protagonist_name = (resolution.get("protagonist_name") or "").strip().lower()
    _cast_names, ref_to_name, exact_name_lookup = build_scene_cast_index_map(
        draft=state.scene_plan,
        resolution=resolution,
    )
    non_protagonist_present_onstage = False
    for textbox in draft.textboxes:
        resolved_speaker = resolve_scene_reference_name(
            raw_ref=textbox.speaker_ref,
            ref_to_name=ref_to_name,
            exact_name_lookup=exact_name_lookup,
            allow_narrator=False,
        )
        if resolved_speaker and resolved_speaker.strip().lower() != protagonist_name:
            non_protagonist_present_onstage = True
            break
        for command in textbox.pending_commands:
            for target in command.targets:
                resolved_target = resolve_scene_reference_name(
                    raw_ref=target,
                    ref_to_name=ref_to_name,
                    exact_name_lookup=exact_name_lookup,
                    allow_narrator=False,
                )
                if resolved_target and resolved_target.strip().lower() != protagonist_name:
                    non_protagonist_present_onstage = True
                    break
            if non_protagonist_present_onstage:
                break
        if non_protagonist_present_onstage:
            break
    social_or_faction_pressure_present = any(
        marker in lowered_scene_text
        for marker in (
            "patrol",
            "enumerator",
            "courier",
            "guard",
            "clerk",
            "auditor",
            "question",
            "interrogate",
            "confront",
            "calls out",
            "voice",
            "they arrive",
            "someone arrives",
        )
    )
    if isolation_pressure.get("active") and not (non_protagonist_present_onstage or social_or_faction_pressure_present):
        issues.append(
            "Isolation pressure is active and this scene still cannot be fixed inside SCENE_BODY alone. Return to scene_plan and add another named character, reintroduced character, or clear faction/social pressure onstage. A new location alone does not satisfy this."
        )
    new_character_pressure = packet.get("new_character_pressure") or {}
    if new_character_pressure.get("active"):
        if not state.scene_plan.new_character_names:
            issues.append(
                "New-character pressure is active and this scene still cannot be fixed inside SCENE_BODY alone. Return to scene_plan and add NEW_CHARACTERS plus NEW_CHARACTER_INTRO. Reusing only existing characters does not satisfy this."
            )
        elif not scene_body_mentions_declared_new_character(state=state, draft=draft):
            issues.append(
                "New-character pressure is active and SCENE_BODY still does not actually introduce the declared NEW_CHARACTERS onstage. Make the new character appear clearly in the scene."
            )
    location_transition_obligation = packet.get("location_transition_obligation") or {}
    if location_transition_obligation.get("active"):
        if state.scene_plan.location_status == "same_location":
            issues.append(
                "This selected choice promised a location transition and this scene is still declared as same_location. Return to scene_plan and change LOCATION_STATUS so the child scene actually moves now."
            )
        elif state.scene_plan.location_status in {"new_location", "return_location"}:
            has_motion_cue = any(pattern.search(lowered_scene_text) for pattern in SCENE_TRANSITION_CUE_PATTERNS)
            if not has_motion_cue and not (state.scene_plan.new_location_intro or "").strip():
                issues.append(
                    "This selected choice promised a location transition, but this scene body does not actually show the move, arrival, or re-entry yet. Return to scene_plan and strengthen LOCATION_STATUS/RETURN_LOCATION setup before retrying SCENE_BODY."
                )
    return issues


def validate_transition_node_draft(
    *,
    packet: dict[str, Any],
    state: NormalRunConversationState,
    draft: TransitionNodeDraft,
    resolution: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    if draft.choice_index >= len(state.choices):
        issues.append(f"Transition bridge references unknown choice index {draft.choice_index + 1}.")
        return issues
    attached_choice = state.choices[draft.choice_index]
    if attached_choice.target_existing_node is None:
        issues.append("Transition bridges can only be written for choices that already merge into an existing node.")
        return issues
    if attached_choice.target_existing_node != draft.target_existing_node:
        issues.append("Transition bridge target does not match the accepted merge target for this choice.")
    if not draft.scene_summary.strip():
        issues.append("TRANSITION_SUMMARY cannot be empty.")
    if len((draft.scene_summary or "").split()) < 8:
        issues.append("TRANSITION_SUMMARY should explain how this bridge reaches the target node, not just name it.")

    transition_packet = dict(packet)
    transition_packet["isolation_pressure"] = {"active": False}
    transition_packet["new_character_pressure"] = {"active": False}
    transition_packet["location_stall_pressure"] = {"active": False}
    body_issues = validate_scene_body_draft(
        packet=transition_packet,
        state=state,
        draft=draft.body,
        resolution=resolution,
    )
    issues.extend(
        issue.replace("SCENE_BODY", "Transition bridge SCENE_BODY")
        for issue in body_issues
    )

    target_summary = ""
    for candidate in ((packet.get("context_summary") or {}).get("merge_candidates") or []):
        if int(candidate.get("node_id") or 0) == draft.target_existing_node:
            target_summary = str(candidate.get("summary") or "").strip()
            break
    compiled, _ = compile_scene_body_draft(state=state, draft=draft.body, resolution=resolution)
    transition_text = compiled.scene_text if compiled is not None else draft.body.raw_body
    if target_summary and texts_are_near_duplicates(transition_text, target_summary):
        issues.append("Transition bridge SCENE_BODY is too close to the merge target summary. Show the connective movement into that node instead of restating it.")
    if attached_choice.next_node and texts_are_near_duplicates(transition_text, attached_choice.next_node):
        issues.append("Transition bridge SCENE_BODY is too close to this choice's NEXT_NODE. Dramatize the connective beat instead of paraphrasing the handoff.")
    return issues


def collect_narrator_owned_dialogue_issues(
    *,
    state: NormalRunConversationState,
    draft: SceneBodyDraft,
    resolution: dict[str, Any],
) -> list[str]:
    if state.scene_plan is None:
        return []
    cast_names = {
        name.strip().lower()
        for name in resolve_scene_cast_names(draft=state.scene_plan, resolution=resolution)
        if name.strip()
    }
    issues: list[str] = []
    for textbox in draft.textboxes:
        speaker_ref = textbox.speaker_ref.strip().lower()
        if speaker_ref not in {"0", "n", "narrator"}:
            continue
        text = textbox.text or ""
        lowered_text = text.lower()
        has_raw_straight_quotes = bool(re.search(r'(?<!\\)"', text))
        has_curly_quotes = "“" in text or "”" in text
        if not has_raw_straight_quotes and not has_curly_quotes:
            continue
        if not SPEECH_VERB_PATTERN.search(text):
            issues.append(
                'SCENE_BODY uses raw quotes inside Narrator text. If an in-world character is speaking, write them as a speaker line like \'<id>: ...\' or \'<name>: ...\'. If you need literal quote marks in narration prose, escape them as \\"like this\\".'
            )
            continue

        mentions_cast_name = any(name in lowered_text for name in cast_names if name and name != "narrator")
        mentions_generic_in_world_role = IN_WORLD_ROLE_PATTERN.search(lowered_text) is not None
        base_issue = (
            "SCENE_BODY gives spoken dialogue to an in-world character through Narrator text rather than the correct format of the character speaking directly like '<id>: ...' or '<name>: ...'."
        )
        if mentions_cast_name:
            issues.append(base_issue + " Rewrite that line so the character themselves speaks the dialogue.")
        elif mentions_generic_in_world_role:
            issues.append(
                base_issue
                + " This appears to use a character who is not currently in SCENE_CAST. Return to scene_plan and either add the proper casting or declare a new character before retrying SCENE_BODY."
            )
        else:
            issues.append(
                base_issue
                + ' If this is truly narration prose rather than speech, remove the raw quotes or escape them as \\"like this\\".'
            )
    return issues


def scene_body_issues_require_scene_plan_rewind(issues: list[str] | None) -> bool:
    return any(
        "Return to scene_plan and either add the proper casting or declare a new character" in issue
        or "Return to scene_plan and add another named character" in issue
        or "Return to scene_plan and add NEW_CHARACTERS plus NEW_CHARACTER_INTRO" in issue
        or "Return to scene_plan and change LOCATION_STATUS/NEW_LOCATION" in issue
        or "Return to scene_plan and strengthen LOCATION_STATUS/NEW_LOCATION" in issue
        or "Return to scene_plan and change LOCATION_STATUS so the child scene actually moves now" in issue
        or "Return to scene_plan and strengthen LOCATION_STATUS/RETURN_LOCATION setup" in issue
        for issue in (issues or [])
    )


def collect_choice_grounding_issues(
    *,
    packet: dict[str, Any],
    state: NormalRunConversationState,
    draft: ChoiceDraft,
) -> list[str]:
    support_text = "\n".join(
        filter(
            None,
            [
                ((packet.get("selected_frontier_item") or {}).get("choice_text") or "").strip(),
                ((packet.get("selected_frontier_item") or {}).get("existing_choice_notes") or "").strip(),
                (((packet.get("context_summary") or {}).get("current_node") or {}).get("title") or "").strip(),
                (((packet.get("context_summary") or {}).get("current_node") or {}).get("summary") or "").strip(),
                state.scene_plan.scene_summary if state.scene_plan else "",
                state.scene_body.raw_body if state.scene_body else "",
            ],
        )
    )
    support_tokens = similarity_tokens(support_text, extra_stopwords=CHOICE_GENERIC_TOKENS)
    issues: list[str] = []
    for pattern in LOCAL_PROP_CHOICE_PATTERNS:
        match = pattern.search(draft.choice_text or "")
        if match is None:
            continue
        phrase = extract_grounding_phrase(match.group(1))
        phrase_tokens = similarity_tokens(phrase, extra_stopwords=CHOICE_GENERIC_TOKENS)
        if len(phrase_tokens) < 2:
            continue
        if len(phrase_tokens - support_tokens) >= 2:
            issues.append(
                f"Choice '{draft.choice_text}' introduces a new focal prop or marker ('{phrase}') that the scene does not establish. "
                "If that object matters, establish it clearly in the scene text first or rename the choice to match grounded scene details."
            )
            break
    return issues


def collect_choice_target_issues(
    *,
    packet: dict[str, Any],
    draft: ChoiceDraft,
) -> list[str]:
    issues: list[str] = []
    if draft.target_existing_node is None:
        return issues
    merge_candidates = ((packet.get("context_summary") or {}).get("merge_candidates") or [])
    allowed_target_ids = {
        int(candidate["node_id"])
        for candidate in merge_candidates
        if candidate.get("node_id") is not None
    }
    if int(draft.target_existing_node) not in allowed_target_ids:
        issues.append(
            f"TARGET_EXISTING_NODE {draft.target_existing_node} is not a valid merge candidate for this branch right now. "
            "Use one of the listed merge candidate node ids or set TARGET_EXISTING_NODE to NONE."
        )
    return issues


def collect_frontier_choice_shape_issues(
    *,
    packet: dict[str, Any],
    choices: list[ChoiceDraft],
) -> list[str]:
    issues: list[str] = []
    frontier_budget = packet.get("frontier_budget_state") or {}
    pressure_level = frontier_budget.get("pressure_level")
    if pressure_level not in {"soft", "hard"}:
        return issues

    constraints = packet.get("frontier_choice_constraints") or {}
    default_max_fresh = int(constraints.get("max_fresh_choices_under_pressure") or 1)
    allow_second_fresh = bool(constraints.get("allow_second_fresh_choice_only_for_bloom_scenes"))
    bloom_scene_candidate = bool(packet.get("is_bloom_scene_candidate"))

    fresh_choices = [choice for choice in choices if choice.target_existing_node is None and choice.ending_category is None]
    if len(fresh_choices) > default_max_fresh and not (
        allow_second_fresh and bloom_scene_candidate and len(fresh_choices) == default_max_fresh + 1
    ):
        issues.append(
            f"Frontier pressure only allows {default_max_fresh} fresh branch choice(s) in this run. "
            "Use TARGET_EXISTING_NODE for a merge or a non-NONE ENDING_CATEGORY for a closure instead of opening another fresh leaf."
        )

    inspection_fresh_choices = []
    for choice in choices:
        inferred_class = choice.choice_class or infer_choice_class_from_text(
            choice.choice_text,
            f"NEXT_NODE: {choice.next_node} FURTHER_GOALS: {choice.further_goals}",
        )
        if inferred_class == "inspection" and choice.target_existing_node is None and choice.ending_category is None:
            inspection_fresh_choices.append(choice)
    if inspection_fresh_choices:
        choice_labels = ", ".join(f"'{choice.choice_text}'" for choice in inspection_fresh_choices[:3])
        issues.append(
            "Inspection choices cannot open durable fresh leaves under frontier pressure. "
            f"Make {choice_labels} reconverge with TARGET_EXISTING_NODE, turn it into a closure, or rewrite it as a materially different move."
        )
    return issues


def build_partial_candidate_for_stage_validation(
    *,
    packet: dict[str, Any],
    state: NormalRunConversationState,
    resolution: dict[str, Any],
    hooks_override: SceneHooksDraft | None = None,
) -> GenerationCandidate | None:
    if state.scene_plan is None or state.scene_body is None or not state.choices:
        return None
    temp_state = state.model_copy(deep=True)
    if hooks_override is not None:
        temp_state.hooks = hooks_override
    candidate_packet = dict(packet)
    if "preview_payload" not in candidate_packet:
        candidate_packet["preview_payload"] = {"branch_key": packet.get("branch_key") or "default"}
    try:
        return assemble_generation_candidate_from_state(packet=candidate_packet, state=temp_state, resolution=resolution)
    except ValueError:
        return None


def collect_partial_branch_pressure_issues(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
    state: NormalRunConversationState | None = None,
) -> list[str]:
    isolation_pressure = packet.get("isolation_pressure") or {}
    new_character_pressure = packet.get("new_character_pressure") or {}
    location_stall_pressure = packet.get("location_stall_pressure") or {}
    action_summary = packet.get("recent_action_family_summary") or {}
    repeated_action_family = action_summary.get("repeated_action_family")
    repeated_action_count = int((action_summary.get("recent_action_family_counts") or {}).get(repeated_action_family or "", 0))
    issues: list[str] = []

    if isolation_pressure.get("active") and not candidate_adds_actor_pressure(candidate):
        issues.append(
            "Isolation pressure is active and this branch is still too solitary. Reintroduce or introduce a character, or bring clear faction/social pressure onstage."
        )

    declared_new_character_names = (
        [name for name in ((state.scene_plan.new_character_names if state and state.scene_plan else []) or []) if name.strip()]
    )
    if (
        new_character_pressure.get("active")
        and not candidate_adds_new_character(candidate)
        and not declared_new_character_names
    ):
        issues.append(
            "New-character pressure is active and this branch still does not introduce a brand-new character. Use NEW_CHARACTERS; reusing only existing characters does not satisfy this."
        )

    if (
        isolation_pressure.get("active")
        or new_character_pressure.get("active")
        or location_stall_pressure.get("active")
        or repeated_action_count >= 3
        or (packet.get("frontier_budget_state") or {}).get("pressure_level") in {"soft", "hard"}
    ) and not candidate_has_material_delta(candidate):
        issues.append(
            "The scene does not appear to create a material delta. Advance danger, cast, location access, hook pressure, merge/closure state, or world pressure becoming immediate."
        )
    return issues


def detect_unresolved_mystery_markers(candidate: GenerationCandidate) -> list[str]:
    markers: set[str] = set()
    texts = [
        candidate.scene_summary,
        candidate.scene_text,
        *(line.text for line in candidate.dialogue_lines),
    ]
    for text in texts:
        lower_text = (text or "").lower()
        for pattern in MYSTERY_MARKER_PATTERNS:
            if pattern.search(lower_text):
                markers.add(pattern.pattern.replace("\\b", "").replace("\\", ""))
    return sorted(markers)


def mystery_marker_is_covered(
    *,
    marker: str,
    active_hooks: list[dict[str, Any]],
    draft: SceneHooksDraft,
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
    draft_hook_text = " ".join(
        filter(
            None,
            [
                draft.hook_summary or "",
                draft.hook_progress_note or "",
                draft.hook_type or "",
            ],
        )
    ).lower()
    if draft_hook_text:
        draft_hook_tokens = set(re.findall(r"[a-z0-9]+", draft_hook_text))
        if marker_tokens and marker_tokens.issubset(draft_hook_tokens):
            return True
    return False


def validate_choice_draft(
    *,
    packet: dict[str, Any],
    state: NormalRunConversationState,
    draft: ChoiceDraft,
    resolution: dict[str, Any],
    choice_index: int | None = None,
) -> list[str]:
    issues: list[str] = []
    if not draft.choice_text.strip():
        issues.append("CHOICE_TEXT cannot be empty.")
    if len(draft.next_node.split()) < 5:
        issues.append("NEXT_NODE should state a concrete immediate result, not a fragment.")
    if len(draft.further_goals.split()) < 4:
        issues.append("FURTHER_GOALS should state medium-range direction, not a fragment.")
    parent_choice_text = resolution.get("selected_choice_text") or ""
    parent_choice_notes = resolution.get("selected_choice_notes") or ""
    if parent_choice_text and texts_are_near_duplicates(draft.choice_text, parent_choice_text):
        issues.append("This choice too closely repeats the just-taken choice.")
    combined_notes = f"NEXT_NODE: {draft.next_node} FURTHER_GOALS: {draft.further_goals}"
    if parent_choice_notes and texts_are_near_duplicates(combined_notes, parent_choice_notes):
        issues.append("This choice too closely repeats the just-taken NEXT_NODE/FURTHER_GOALS lane.")
    for prior_choice in state.choices:
        if texts_are_near_duplicates(draft.choice_text, prior_choice.choice_text):
            issues.append("This choice duplicates an already accepted choice in the same scene.")
        prior_notes = f"NEXT_NODE: {prior_choice.next_node} FURTHER_GOALS: {prior_choice.further_goals}"
        if texts_are_near_duplicates(combined_notes, prior_notes):
            issues.append("This choice duplicates another accepted choice's NEXT_NODE/FURTHER_GOALS lane.")
    issues.extend(collect_plan_names([draft.choice_text, draft.next_node, draft.further_goals], resolution))
    if draft.choice_class == "ending" and draft.ending_category is None:
        issues.append("ENDING_CATEGORY is required when CHOICE_CLASS is ending.")
    if draft.choice_class != "ending" and draft.ending_category is not None:
        issues.append("ENDING_CATEGORY should be NONE unless CHOICE_CLASS is ending.")
    issues.extend(collect_choice_grounding_issues(packet=packet, state=state, draft=draft))
    issues.extend(collect_choice_target_issues(packet=packet, draft=draft))
    merge_or_closure_required_now = bool(
        choice_index == 0 and ((packet.get("frontier_choice_constraints") or {}).get("must_include_merge_or_closure"))
    )
    if merge_or_closure_required_now and draft.target_existing_node is None and draft.ending_category is None:
        issues.append(
            "Frontier pressure is active. choice_1 must either use TARGET_EXISTING_NODE for a deliberate merge into an existing node or use a non-NONE ENDING_CATEGORY for a real closure."
        )
    inferred_choice_class = draft.choice_class or infer_choice_class_from_text(
        draft.choice_text,
        f"NEXT_NODE: {draft.next_node} FURTHER_GOALS: {draft.further_goals}",
    )
    if (
        (packet.get("frontier_budget_state") or {}).get("pressure_level") in {"soft", "hard"}
        and inferred_choice_class == "inspection"
        and draft.target_existing_node is None
        and draft.ending_category is None
    ):
        issues.append(
            "Under frontier pressure, an inspection choice cannot open a durable fresh leaf. "
            "Use TARGET_EXISTING_NODE to merge it, give it a real ENDING_CATEGORY closure, or rewrite it as a materially different move."
        )
    return issues


def choice_draft_counts_as_consequential(choice: ChoiceDraft) -> bool:
    if choice.choice_class in {"commitment", "location_transition", "ending"} or choice.target_existing_node is not None:
        return True
    combined_text = " ".join(filter(None, [choice.choice_text, choice.next_node, choice.further_goals]))
    action_family = classify_choice_action_family(choice.choice_text)
    if action_family in {"social", "travel", "follow"}:
        return True
    return choice_text_implies_consequence(combined_text)


def validate_choice_menu(
    *,
    packet: dict[str, Any],
    choices: list[ChoiceDraft],
    state: NormalRunConversationState | None = None,
    resolution: dict[str, Any] | None = None,
) -> list[str]:
    issues: list[str] = []
    action_summary = packet.get("recent_action_family_summary") or {}
    repeated_action_family = action_summary.get("repeated_action_family")
    repeated_action_count = int((action_summary.get("recent_action_family_counts") or {}).get(repeated_action_family or "", 0))

    if repeated_action_family in {"inspect", "follow", "touch", "step_back"} and repeated_action_count >= 3:
        if choices and not any(choice_breaks_repeated_action_pattern(choice) for choice in choices):
            issues.append(
                f"This branch has been overusing the '{repeated_action_family}' action family. At least one choice in this menu must break that pattern with a social turn, location shift, merge using TARGET_EXISTING_NODE, closure using ENDING_CATEGORY, or another materially different move."
            )

    if len(choices) >= 2 and ((packet.get("consequential_choice_requirement") or {}).get("required")):
        if not any(choice_draft_counts_as_consequential(choice) for choice in choices):
            issues.append(
                "At least one choice in this menu must be a commitment, social move, location shift, merge, closure, or immediate-pressure response."
            )

    location_stall_pressure = packet.get("location_stall_pressure") or {}
    if location_stall_pressure.get("active") and not any(choice.choice_class == "location_transition" for choice in choices):
        issues.append(
            "Location-stall pressure is active. This menu must include at least one CHOICE_CLASS: location_transition option. Satisfy that here in the choice-writing phase; that choice will promise a move to a different location when it is expanded later."
        )

    issues.extend(collect_frontier_choice_shape_issues(packet=packet, choices=choices))

    if state is not None and resolution is not None:
        temp_state = state.model_copy(deep=True)
        temp_state.choices = list(choices)
        candidate = build_partial_candidate_for_stage_validation(packet=packet, state=temp_state, resolution=resolution)
        if candidate is not None:
            issues.extend(collect_redundant_progression_issues(packet=packet, candidate=candidate))
            issues.extend(collect_partial_branch_pressure_issues(packet=packet, candidate=candidate, state=temp_state))
    return issues


def should_request_hooks(*, packet: dict[str, Any], state: NormalRunConversationState) -> bool:
    return True


def should_request_details(*, packet: dict[str, Any], state: NormalRunConversationState) -> bool:
    if state.scene_plan is None:
        return False
    return bool(state.scene_plan.new_character_names or state.scene_plan.new_location_name)


def validate_scene_hooks_draft(
    *,
    packet: dict[str, Any],
    state: NormalRunConversationState,
    draft: SceneHooksDraft,
    resolution: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    if draft.hook_action == "new_hook":
        if draft.hook_importance is None:
            issues.append("HOOK_IMPORTANCE is required when HOOK_ACTION is NEW_HOOK.")
        if not draft.hook_type:
            issues.append("HOOK_TYPE is required when HOOK_ACTION is NEW_HOOK.")
        if not draft.hook_summary:
            issues.append("HOOK_SUMMARY is required when HOOK_ACTION is NEW_HOOK.")
        if not draft.hook_payoff_concept:
            issues.append("HOOK_PAYOFF_CONCEPT is required when HOOK_ACTION is NEW_HOOK.")
    elif draft.hook_action == "update_hook":
        if draft.hook_id is None:
            issues.append("HOOK_ID is required when HOOK_ACTION is UPDATE_HOOK.")
        if draft.hook_status is None:
            issues.append("HOOK_STATUS is required when HOOK_ACTION is UPDATE_HOOK.")
        if not draft.hook_progress_note:
            issues.append("HOOK_PROGRESS_NOTE is required when HOOK_ACTION is UPDATE_HOOK.")
    else:
        if any([draft.hook_importance, draft.hook_type, draft.hook_summary, draft.hook_payoff_concept, draft.hook_id, draft.hook_status, draft.hook_progress_note]):
            issues.append("When HOOK_ACTION is NONE, leave the hook-specific fields as NONE.")

    candidate = build_partial_candidate_for_stage_validation(
        packet=packet,
        state=state,
        resolution=resolution,
        hooks_override=draft,
    )
    if candidate is not None:
        markers = detect_unresolved_mystery_markers(candidate)
        if markers:
            settings = Settings.from_env()
            connection = connect(settings.database_path)
            try:
                branch_state = BranchStateService(
                    connection,
                    act_phase_ranges={"early": {"start": 0, "end": 999999}},
                )
                active_hooks = branch_state.list_hooks(
                    candidate.branch_key,
                    statuses=["active", "payoff_ready", "blocked"],
                )
                uncovered_markers = [
                    marker
                    for marker in markers
                    if not mystery_marker_is_covered(marker=marker, active_hooks=active_hooks, draft=draft)
                ]
                if uncovered_markers:
                    issues.append(
                        "This scene introduces an unresolved mystery/question without creating or extending a hook: "
                        + ", ".join(sorted(uncovered_markers))
                        + ". Use the hooks step to create or update the relevant hook now."
                    )

                if draft.hook_action == "update_hook" and draft.hook_id is not None:
                    hook = branch_state.get_hook(draft.hook_id)
                    if hook is None:
                        issues.append(f"HOOK_ID {draft.hook_id} does not exist.")
                    else:
                        projected_depth = int(((packet.get("context_summary") or {}).get("branch_depth")) or 0) + 1
                        state_tags = {row["tag"] for row in branch_state.list_branch_tags(candidate.branch_key, tag_type="state")}
                        clue_tags = {row["tag"] for row in branch_state.list_branch_tags(candidate.branch_key, tag_type="clue")}
                        readiness = branch_state._hook_readiness(hook, projected_depth, state_tags, clue_tags)
                        if not readiness["development_eligible"]:
                            issues.append(
                                f"HOOK_ID {draft.hook_id} is still on development cooldown and cannot be advanced yet."
                            )
                        if draft.hook_status == "resolved":
                            min_payoff_depth = int(hook["introduced_at_depth"]) + int(hook["min_distance_to_payoff"])
                            if projected_depth < min_payoff_depth:
                                issues.append(
                                    f"HOOK_ID {draft.hook_id} cannot resolve yet because it has not reached min_distance_to_payoff."
                                )
                            required_clues = set(hook["required_clue_tags"])
                            required_states = set(hook["required_state_tags"])
                            if not required_clues.issubset(set(candidate.discovered_clue_tags)):
                                issues.append(f"HOOK_ID {draft.hook_id} cannot resolve yet because required clue tags are still missing.")
                            if not required_states.issubset(set(candidate.discovered_state_tags)):
                                issues.append(f"HOOK_ID {draft.hook_id} cannot resolve yet because required state tags are still missing.")
            finally:
                connection.close()
    return issues


def validate_scene_extras_draft(
    *,
    packet: dict[str, Any],
    state: NormalRunConversationState,
    draft: SceneExtrasDraft,
    resolution: dict[str, Any],
    art: SceneArtDraft | None = None,
) -> list[str]:
    issues: list[str] = []
    if state.scene_plan and state.scene_plan.new_location_name:
        if not draft.new_locations:
            issues.append("NEW_LOCATION was declared above, so LOCATION_DETAILS must define it.")
        else:
            expected = state.scene_plan.new_location_name.strip().lower()
            defined = {location.name.strip().lower() for location in draft.new_locations if location.name.strip()}
            if expected not in defined:
                issues.append(f"NEW_LOCATION declares '{state.scene_plan.new_location_name}', but LOCATION_DETAILS does not define that exact location name.")
    if state.scene_plan and state.scene_plan.new_character_names:
        if not draft.new_characters:
            issues.append("NEW_CHARACTERS were declared above, so CHARACTER_DETAILS must define them.")
        defined = {character.name.strip().lower() for character in draft.new_characters if character.name.strip()}
        for name in state.scene_plan.new_character_names:
            lowered = name.strip().lower()
            if lowered and lowered not in defined:
                issues.append(f"NEW_CHARACTERS declares '{name}', but CHARACTER_DETAILS does not define that exact name.")
    if state.scene_plan is not None:
        scene_cast_names = resolve_scene_cast_names(draft=state.scene_plan, resolution=resolution)
        known_names = set(resolution.get("character_name_map") or {})
        extras_new_names = {character.name.strip().lower() for character in draft.new_characters if character.name.strip()}
        protagonist_name = (resolution.get("protagonist_name") or "").strip().lower()
        for name in scene_cast_names:
            lowered = name.strip().lower()
            if not lowered or lowered == protagonist_name or lowered in known_names:
                continue
            if lowered not in extras_new_names:
                issues.append(f"SCENE_CAST includes new character '{name}' but CHARACTER_DETAILS does not define them.")
    art = art or SceneArtDraft()
    if state.scene_plan and state.scene_plan.new_character_names:
        if not art.character_art_hints:
            issues.append("NEW_CHARACTERS were declared above, so CHARACTER_ART_HINTS must define them.")
        lowered_hints = {name.strip().lower() for name in art.character_art_hints if name.strip()}
        for name in state.scene_plan.new_character_names:
            if name.strip().lower() not in lowered_hints:
                issues.append(f"NEW_CHARACTERS declares '{name}', but CHARACTER_ART_HINTS does not define that exact name.")
    if state.scene_plan and state.scene_plan.new_location_name:
        if not art.location_art_hints:
            issues.append("NEW_LOCATION was declared above, so LOCATION_ART_HINTS must define it.")
        elif state.scene_plan.new_location_name.strip().lower() not in {name.strip().lower() for name in art.location_art_hints if name.strip()}:
            issues.append(f"NEW_LOCATION declares '{state.scene_plan.new_location_name}', but LOCATION_ART_HINTS does not define that exact name.")
    return issues


def validate_detail_target_draft(
    *,
    target_type: Literal["character", "location"],
    target_name: str,
    extras: SceneExtrasDraft,
    art: SceneArtDraft,
) -> list[str]:
    lowered_target = target_name.strip().lower()
    issues: list[str] = []
    if target_type == "character":
        defined = {character.name.strip().lower() for character in extras.new_characters if character.name.strip()}
        if lowered_target not in defined:
            issues.append(f'CHARACTER_DETAILS must define "{target_name}" exactly.')
        hinted = {name.strip().lower() for name in art.character_art_hints if name.strip()}
        if lowered_target not in hinted:
            issues.append(f'CHARACTER_ART_HINTS must define "{target_name}" exactly.')
    else:
        defined = {location.name.strip().lower() for location in extras.new_locations if location.name.strip()}
        if lowered_target not in defined:
            issues.append(f'LOCATION_DETAILS must define "{target_name}" exactly.')
        hinted = {name.strip().lower() for name in art.location_art_hints if name.strip()}
        if lowered_target not in hinted:
            issues.append(f'LOCATION_ART_HINTS must define "{target_name}" exactly.')
    return issues




def infer_current_location_reference(
    *,
    state: NormalRunConversationState,
    extras: SceneExtrasDraft | None,
    resolution: dict[str, Any],
) -> tuple[list[LocationSeed], EntityReference | None]:
    if state.scene_plan is None:
        return [], None
    if state.scene_plan.location_status == "same_location":
        current_location_id = resolution.get("current_location_id")
        if current_location_id is None:
            return [], None
        return [], EntityReference(entity_type="location", entity_id=int(current_location_id), role="current_scene")
    if state.scene_plan.location_status == "new_location":
        new_locations = list((extras.new_locations if extras else []) or [])
        return new_locations, None

    target_location = resolve_return_location_target(draft=state.scene_plan, resolution=resolution)
    if target_location is not None and target_location.get("id") is not None:
        return [], EntityReference(entity_type="location", entity_id=int(target_location["id"]), role="current_scene")
    raise ValueError("Could not resolve return_location from encountered canonical locations.")


def assemble_generation_candidate_from_state(
    *,
    packet: dict[str, Any],
    state: NormalRunConversationState,
    resolution: dict[str, Any],
) -> GenerationCandidate:
    if state.scene_plan is None or state.scene_body is None or not state.choices:
        raise ValueError("Cannot assemble candidate before scene_plan, scene_body, and choices are accepted.")

    extras = state.extras or SceneExtrasDraft()
    hooks = state.hooks or SceneHooksDraft()
    art = state.art or SceneArtDraft()

    if art.character_art_hints:
        for character in extras.new_characters:
            hint = art.character_art_hints.get(character.name)
            if hint:
                character.canonical_summary = hint
    if art.location_art_hints:
        for location in extras.new_locations:
            hint = art.location_art_hints.get(location.name)
            if hint:
                location.canonical_summary = hint
    new_locations, current_scene_reference = infer_current_location_reference(
        state=state,
        extras=extras,
        resolution=resolution,
    )

    compiled_scene_body, compile_issues = compile_scene_body_draft(state=state, draft=state.scene_body, resolution=resolution)
    if compile_issues or compiled_scene_body is None:
        raise ValueError(f"Cannot assemble candidate from invalid SCENE_BODY: {compile_issues}")

    character_name_map = resolution.get("character_name_map") or {}
    protagonist_id = resolution.get("protagonist_id")
    protagonist_name = (resolution.get("protagonist_name") or "").strip()
    scene_cast_names = resolve_scene_cast_names(draft=state.scene_plan, resolution=resolution)
    scene_cast_lookup = {name.strip().lower() for name in scene_cast_names if name.strip()}

    scene_present_entities: list[ScenePresentEntity] = []
    entity_references: list[EntityReference] = []
    floating_introductions: list[FloatingCharacterIntroduction] = []

    if current_scene_reference is not None:
        entity_references.append(current_scene_reference)

    if protagonist_id is not None and protagonist_name.strip().lower() in scene_cast_lookup:
        scene_present_entities.append(
            ScenePresentEntity(entity_type="character", entity_id=int(protagonist_id), slot="hero-center", focus=True)
        )

    support_slots = ["left-support", "right-support"]
    visible_existing_names: list[str] = []
    for name in scene_cast_names:
        lowered = name.strip().lower()
        if protagonist_id is not None and lowered == protagonist_name.strip().lower():
            continue
        if lowered in character_name_map:
            visible_existing_names.append(lowered)

    for index, lowered in enumerate(visible_existing_names[: len(support_slots)]):
        character = character_name_map[lowered]
        character_id = int(character["id"])
        scene_present_entities.append(
            ScenePresentEntity(entity_type="character", entity_id=character_id, slot=cast(Any, support_slots[index]), focus=False)
        )
        if lowered not in (resolution.get("encountered_names") or set()) and state.scene_plan.new_character_intro:
            floating_introductions.append(
                FloatingCharacterIntroduction(character_id=character_id, intro_text=state.scene_plan.new_character_intro)
            )

    generated_choices = [
        GeneratedChoice(
            choice_text=choice.choice_text,
            notes=f"NEXT_NODE: {choice.next_node} FURTHER_GOALS: {choice.further_goals}",
            choice_class=choice.choice_class,
            ending_category=choice.ending_category,
            target_node_id=choice.target_existing_node,
        )
        for choice in state.choices
    ]
    compiled_transition_nodes: list[TransitionNodeSpec] = []
    for transition in state.transition_nodes:
        compiled_transition_body, transition_compile_issues = compile_scene_body_draft(
            state=state,
            draft=transition.body,
            resolution=resolution,
        )
        if transition_compile_issues or compiled_transition_body is None:
            raise ValueError(f"Cannot assemble candidate from invalid transition bridge: {transition_compile_issues}")
        compiled_transition_nodes.append(
            TransitionNodeSpec(
                choice_list_index=transition.choice_index,
                scene_title=transition.scene_title,
                scene_summary=transition.scene_summary,
                scene_text=compiled_transition_body.scene_text,
                dialogue_lines=compiled_transition_body.dialogue_lines,
            )
        )

    new_hooks: list[HookProposal] = []
    hook_updates: list[HookUpdate] = []
    if hooks.hook_action == "new_hook":
        new_hooks.append(
            HookProposal(
                importance=cast(Any, hooks.hook_importance),
                hook_type=hooks.hook_type or "",
                summary=hooks.hook_summary or "",
                payoff_concept=hooks.hook_payoff_concept or "",
            )
        )
    elif hooks.hook_action == "update_hook":
        hook_updates.append(
            HookUpdate(
                hook_id=int(hooks.hook_id or 0),
                status=cast(Any, hooks.hook_status),
                progress_note=hooks.hook_progress_note or "",
            )
        )

    return GenerationCandidate(
        branch_key=packet["preview_payload"]["branch_key"],
        scene_title=state.scene_plan.scene_title,
        scene_summary=state.scene_plan.scene_summary,
        scene_text=compiled_scene_body.scene_text,
        dialogue_lines=compiled_scene_body.dialogue_lines,
        choices=generated_choices,
        transition_nodes=compiled_transition_nodes,
        new_locations=new_locations,
        new_characters=extras.new_characters,
        new_objects=[],
        floating_character_introductions=floating_introductions,
        entity_references=entity_references,
        scene_present_entities=scene_present_entities,
        new_hooks=new_hooks,
        hook_updates=hook_updates,
        global_direction_notes=hooks.global_direction_notes,
        discovered_clue_tags=hooks.clue_tags,
        discovered_state_tags=hooks.state_tags,
    )


def build_user_prompt(packet: dict[str, Any]) -> str:
    if packet["run_mode"] == "planning":
        return (
            "This is a planning-mode packet.\n"
            "Do not generate or apply a scene.\n"
            "Use the planning targets and shared notes in the packet to strengthen future direction.\n"
            "The fresh ideas for this planning run have already been generated separately.\n"
            "The API already supplied the required structured-output schema. Return only JSON.\n\n"
            "Packet:\n"
            f"{json.dumps(packet, indent=2)}"
        )
    if packet["run_mode"] == "revival":
        return (
            "This is a frontier-revival packet.\n"
            "Do not generate a new scene node yet.\n"
            "Return one new choice only for the parent scene so continuity can reopen from an earlier closed branch point.\n"
            "Use the packet's revival_context exactly.\n"
            "If the parent scene is already at max choice capacity, the system will replace the traversed closing choice with your new one.\n"
            "Return only JSON with the revival choice fields.\n\n"
            "Packet:\n"
            f"{json.dumps(packet, indent=2)}"
        )

    return (
        "This is a normal story-worker packet.\n"
        "The API already supplied the required GenerationCandidate structured-output schema.\n"
        "If validation issues are returned later, revise the JSON and try again until it passes validation.\n"
        "Return only GenerationCandidate fields. Do not include report/meta fields like pre_change_url, ideas_to_append, validation_status, or next_action.\n"
        "Use new_locations/new_characters/new_objects only for brand-new canon entities; those seed objects should not carry existing ids.\n"
        "Do not put existing canon like Madam Bei into new_characters. Existing canon belongs in entity_references and scene_present_entities.\n"
        "entity_references entries are only for existing canon references and should be shaped like {entity_type, entity_id, role}.\n"
        "scene_present_entities entries are for visible on-screen staging and should be shaped like {entity_type, entity_id, slot, ...}; they use slot, not role.\n"
        "Locations usually belong in entity_references with role current_scene, not in scene_present_entities.\n"
        "new_hooks entries are only for brand-new hooks; do not include hook ids there, and always include at least hook_type and summary.\n"
        "Leave optional arrays empty unless you are intentionally changing something. "
        "In particular, do not copy read-only branch context into affordance_changes; if you are not unlocking/suspending/restoring/retiring an affordance now, return affordance_changes as [].\n"
        "If reveal_guardrails are present in the packet, obey them strictly. Early local pressure, partial strange sightings, and first personal breadcrumbs are okay. Dumping the hidden regime, true masterminds, or full backstory too early is not okay yet.\n"
        "If choice_handoff is present in the packet, treat NEXT_NODE as the immediate scene result you should actually deliver or clearly pivot from, and treat FURTHER_GOALS as medium-range steering pressure.\n"
        "Use NEXT_NODE as a base for your scene, but expand and elaborate on it. Do not simply repeat it.\n"
        "Always evaluate whether the player is actually familiar with a character, object, location, title, faction, or system before simply naming it in playable text. Hooks, worldbuilding notes, and other behind-the-scenes coherence trackers often name things the player has not learned yet.\n"
        "Frequently use ideas from IDEAS.md when the current branch genuinely supports them. Planning runs exist specifically to make idea usage easier during normal runs like this one.\n"
        "Treat IDEAS.md as a main source of fresh people, places, and whimsical turns when the branch is getting stale.\n"
        "Prefer whimsical, readable, unexpected developments over another direct derivative of the current patrol/vault/seam beat.\n"
        "Return only the JSON object, with no markdown fences or extra commentary.\n\n"
        "Frequently introduce new characters or bring in old ones if the context makes sense. New character ideas in IDEAS.md. \n"
        "Continually introduce new locations or revisit old ones as choices progress and the location changes.\n"
        "Packet:\n"
        f"{json.dumps(packet, indent=2)}"
    )


def build_response_format(packet: dict[str, Any]) -> dict[str, Any]:
    if packet["run_mode"] == "planning":
        schema = PlanningFollowthroughResult.model_json_schema()
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "planning_followthrough_result",
                "schema": schema,
            },
        }
    if packet["run_mode"] == "revival":
        schema = RevivalChoiceResult.model_json_schema()
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "revival_choice_result",
                "schema": schema,
            },
        }
    schema = GenerationCandidate.model_json_schema()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "generation_candidate",
            "schema": schema,
        },
    }


def build_planning_ideas_system_prompt() -> str:
    return (
        "You invent fresh story seeds for a whimsical, surreal fantasy world.\n"
        "Return JSON only. Do not wrap it in markdown fences.\n"
        "Be genuinely original rather than producing small variations on the same motif.\n"
    )


def build_planning_ideas_user_prompt(packet: dict[str, Any]) -> str:
    required_count = int((packet.get("planning_policy") or {}).get("ideas_per_run") or 3)
    return (
        "I'm coming up with ideas for a fantasy whimsical and surreal setting. "
        "In this world talking tram-operating frogs are normal and underground glass villages are possible, "
        "the sky is the limit.\n"
        f"Give me {required_count} new categorized ideas for this world. "
        "Spread them across at least 3 categories chosen from character, location, object, and event.\n"
        "At least one idea must be an event, because the world should feel alive and in motion, not just explorable.\n"
        "The ideas must be concrete and vivid.\n"
        "Do not lightly remix the same motif across multiple ideas.\n"
        "Do not reuse bells, orchards, rival clerks, archivists, reversed announcements, whispering gutters, "
        "or identity-correction paperwork as your central gimmick.\n"
        "DO NOT use anything I said in your response; come up with completely original ideas.\n"
        "Return only JSON matching the supplied schema."
    )


def build_planning_ideas_response_format() -> dict[str, Any]:
    schema = PlanningIdeasResult.model_json_schema()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "planning_ideas_result",
            "schema": schema,
        },
    }


def build_planning_followthrough_user_prompt(
    *,
    packet: dict[str, Any],
    ideas: list[PlanningIdea],
) -> str:
    packet_for_prompt = dict(packet)
    packet_for_prompt["fresh_ideas_for_this_run"] = [idea.model_dump() for idea in ideas]
    return (
        "This is a planning-mode packet.\n"
        "Do not generate or apply a scene.\n"
        "Use the already-generated fresh ideas in fresh_ideas_for_this_run exactly as the ideas to append for this planning pass.\n"
        "Do not replace them with different ideas and do not return ideas_to_append in this step.\n"
        "Use the planning targets and shared notes in the packet to update choice notes and add any useful structured story notes.\n"
        "If the setting needs more ambient motion or conflict pressure, you may also add worldbuilding_notes about patrols, rumors, factions, automata, or danger escalation.\n"
        "Bind at least one planning target to a specific idea using bound_idea so later normal runs have a concrete direction signal.\n"
        "If a target fits one of the fresh ideas, prefer binding it. If an existing IDEAS.md idea fits better, you may bind that instead.\n"
        "Return only JSON.\n\n"
        "Packet:\n"
        f"{json.dumps(packet_for_prompt, indent=2)}"
    )


def call_lm_studio(
    *,
    api_base: str,
    model: str,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    messages: list[dict[str, str]] | None = None,
    response_format: dict[str, Any] | None = None,
    temperature: float,
    max_tokens: int,
    request_timeout: float,
) -> str:
    mock_response_path = os.environ.get("CYOA_LOCAL_WORKER_RESPONSE_FILE")
    if mock_response_path:
        raw_mock = Path(mock_response_path).read_text(encoding="utf-8")
        try:
            parsed_mock = json.loads(raw_mock)
        except json.JSONDecodeError:
            return raw_mock
        if isinstance(parsed_mock, list):
            if not parsed_mock:
                raise RuntimeError("Mock response queue is empty.")
            next_item = parsed_mock.pop(0)
            Path(mock_response_path).write_text(json.dumps(parsed_mock), encoding="utf-8")
            if isinstance(next_item, str):
                return next_item
            return json.dumps(next_item)
        if isinstance(parsed_mock, str):
            return parsed_mock
        return json.dumps(parsed_mock)

    url = f"{api_base.rstrip('/')}/chat/completions"
    request_messages = messages or [
        {"role": "system", "content": system_prompt or ""},
        {"role": "user", "content": user_prompt or ""},
    ]
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": request_messages,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    response = httpx.post(url, json=payload, timeout=request_timeout)
    response.raise_for_status()
    data = response.json()
    message = data["choices"][0]["message"]
    content = message.get("content") or ""
    if content.strip():
        return content
    reasoning_content = message.get("reasoning_content") or ""
    if reasoning_content.strip():
        return reasoning_content
    return ""


def request_nonempty_ai_step_response(
    *,
    api_base: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    request_timeout: float,
    empty_retry_limit: int = EMPTY_STEP_RESPONSE_RETRY_LIMIT,
) -> str:
    response_text = ""
    for _ in range(max(empty_retry_limit, 1)):
        response_text = call_lm_studio(
            api_base=api_base,
            model=model,
            messages=messages,
            response_format=None,
            temperature=temperature,
            max_tokens=max_tokens,
            request_timeout=request_timeout,
        )
        if response_text.strip():
            return response_text
    return ""


def extract_json_text(raw_text: str) -> str:
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        possible = stripped[start : end + 1]
        try:
            json.loads(possible)
            return possible
        except json.JSONDecodeError:
            pass
    json.loads(stripped)
    return stripped


def normalize_generation_candidate_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(raw_payload)

    seed_fields = {
        "new_locations": ("name", "description", "canonical_summary"),
        "new_characters": ("name", "description", "canonical_summary", "home_location_name"),
        "new_objects": ("name", "description", "canonical_summary", "default_location_name"),
    }
    entity_references = list(payload.get("entity_references") or [])
    scene_present_entities = list(payload.get("scene_present_entities") or [])
    hook_updates = list(payload.get("hook_updates") or [])
    existing_story_notes = list(payload.get("global_direction_notes") or [])

    normalized_entity_references: list[dict[str, Any]] = []
    normalized_scene_present_entities: list[dict[str, Any]] = []

    for reference in entity_references:
        if not isinstance(reference, dict):
            normalized_entity_references.append(reference)
            continue
        role = reference.get("role")
        if role in VALID_PRESENT_ENTITY_SLOTS:
            moved = {
                "entity_type": reference.get("entity_type"),
                "entity_id": reference.get("entity_id"),
                "slot": role,
            }
            for key in ("scale", "offset_x_percent", "offset_y_percent", "focus", "hidden_on_lines", "use_player_fallback"):
                if key in reference:
                    moved[key] = reference[key]
            normalized_scene_present_entities.append(moved)
            continue
        normalized_entity_references.append(reference)

    for present in scene_present_entities:
        if not isinstance(present, dict):
            normalized_scene_present_entities.append(present)
            continue
        role = present.get("role")
        if "slot" not in present and role in VALID_PRESENT_ENTITY_SLOTS:
            moved = dict(present)
            moved["slot"] = role
            moved.pop("role", None)
            normalized_scene_present_entities.append(moved)
            continue
        if present.get("entity_type") == "location" and role == "current_scene" and "slot" not in present:
            normalized_entity_references.append(
                {
                    "entity_type": present.get("entity_type"),
                    "entity_id": present.get("entity_id"),
                    "role": "current_scene",
                }
            )
            continue
        normalized_scene_present_entities.append(present)

    normalized_new_hooks: list[dict[str, Any]] = []
    hooks_source = list(payload.get("new_hooks") or [])
    if payload.get("hooks") and not hooks_source:
        hooks_source = list(payload.get("hooks") or [])
    for hook in hooks_source:
        if isinstance(hook, dict) and hook.get("hook_id") is not None and not hook.get("hook_type"):
            hook_update = {
                "hook_id": hook.get("hook_id"),
                "status": "active",
            }
            if hook.get("summary"):
                hook_update["progress_note"] = hook.get("summary")
            hook_updates.append(hook_update)
            continue
            normalized_new_hooks.append(hook)

    normalized_asset_requests: list[dict[str, Any]] = []
    for request in list(payload.get("asset_requests") or []):
        if (
            isinstance(request, dict)
            and request.get("requested_asset_kinds")
            and not request.get("job_type")
            and not request.get("asset_kind")
        ):
            entity_type = request.get("entity_type")
            entity_id = request.get("entity_id")
            for requested_kind in request.get("requested_asset_kinds") or []:
                if requested_kind == "cutout":
                    continue
                if requested_kind == "background":
                    job_type = "generate_background"
                elif requested_kind == "portrait":
                    job_type = "generate_portrait"
                elif requested_kind == "object_render":
                    job_type = "generate_object"
                else:
                    continue
                normalized_asset_requests.append(
                    {
                        "job_type": job_type,
                        "asset_kind": requested_kind,
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                    }
                )
            continue
        normalized_asset_requests.append(request)

    for field_name, allowed_keys in seed_fields.items():
        normalized_seeds: list[dict[str, Any]] = []
        for item in list(payload.get(field_name) or []):
            if isinstance(item, dict):
                normalized_seeds.append({key: item[key] for key in allowed_keys if key in item})
            else:
                normalized_seeds.append(item)
        payload[field_name] = normalized_seeds

    payload["entity_references"] = normalized_entity_references
    payload["scene_present_entities"] = normalized_scene_present_entities
    payload["new_hooks"] = normalized_new_hooks
    payload["hook_updates"] = hook_updates
    payload["asset_requests"] = normalized_asset_requests
    payload["global_direction_notes"] = existing_story_notes or list(payload.get("story_direction_notes") or [])
    return payload


def parse_llm_result(
    packet: dict[str, Any],
    raw_text: str,
) -> GenerationCandidate | PlanningFollowthroughResult | RevivalChoiceResult:
    json_text = extract_json_text(raw_text)
    if packet["run_mode"] == "planning":
        return PlanningFollowthroughResult.model_validate_json(json_text)
    if packet["run_mode"] == "revival":
        return RevivalChoiceResult.model_validate_json(json_text)
    raw_payload = json.loads(json_text)
    normalized_payload = normalize_generation_candidate_payload(raw_payload)
    return GenerationCandidate.model_validate(normalized_payload)


def parse_planning_ideas_result(raw_text: str) -> PlanningIdeasResult:
    json_text = extract_json_text(raw_text)
    return PlanningIdeasResult.model_validate_json(json_text)


def get_revival_validation_issues(packet: dict[str, Any], result: RevivalChoiceResult) -> list[str]:
    issues: list[str] = []
    if result.choice_class == "ending" and result.ending_category is None:
        issues.append("Revival ending choices must include ending_category.")
    parent_total = int(((packet.get("revival_context") or {}).get("parent_total_choice_count")) or 0)
    max_choices = int(((packet.get("revival_context") or {}).get("max_choices_per_node")) or 5)
    if parent_total > max_choices:
        issues.append("Revival target parent already exceeds max choice count; clean that parent before reviving it.")
    return issues


def build_validation_retry_user_prompt(
    *,
    packet: dict[str, Any],
    previous_candidate: GenerationCandidate,
    issues: list[str],
) -> str:
    encountered_characters = ((packet.get("path_character_continuity") or {}).get("encountered_characters") or [])
    allowed_names = [entry.get("name") for entry in encountered_characters if entry.get("name")]
    return (
        "Your previous GenerationCandidate failed validation.\n"
        "Fix the listed issues and return a corrected GenerationCandidate JSON object only.\n"
        "Do not explain the changes. Do not stop. Keep trying until validation passes.\n\n"
        "Important reminders:\n"
        "- Every choice.notes value must use meaningful planning notes in the form 'NEXT_NODE: ... FURTHER_GOALS: ...'.\n"
        "- Use NEXT_NODE as a base for your scene, but expand and elaborate on it. Do not simply repeat it.\n"
        "- Set target_node_id to null unless you are intentionally quick-merging into one of the listed merge_candidates.\n"
        "- Reuse existing art instead of requesting duplicate generation.\n\n"
        "- Return only GenerationCandidate fields. Do not include pre_change_url, ideas_to_append, validation_status, or next_action.\n"
        "- new_locations/new_characters/new_objects are only for brand-new canon entities and should not carry existing ids.\n"
        "- Existing canon like Madam Bei belongs in entity_references and scene_present_entities, not new_characters.\n"
        "- If an existing recurring character has not been met on this path yet, use floating_character_introductions with their existing character_id and a short first-meeting beat.\n"
        "- entity_references entries must use existing canon ids and should only contain entity_type, entity_id, and role.\n"
        "- scene_present_entities entries are for visible on-screen staging and must use slot, not role.\n"
        "- Locations usually belong in entity_references with role current_scene, not in scene_present_entities.\n"
        "- new_hooks are new hook proposals, so do not include hook ids there; include hook_type and summary instead.\n\n"
        "- Do not simply restate the just-taken choice as another option. The new scene must materially advance before offering its next menu.\n"
        "- If choice_handoff is present in the packet, deliver or clearly pivot from its NEXT_NODE instead of ignoring it.\n"
        "- Use NEXT_NODE as a base for your scene, but expand and elaborate on it. Do not simply repeat it.\n"
        "- Do not name a character, object, location, title, faction, or system in playable text just because it appears in hooks, worldbuilding, or other behind-the-scenes packet memory. Check whether the player actually knows it yet.\n"
        "- Do not repeat the parent scene summary with only cosmetic wording changes.\n"
        "- If an inspection choice names a local prop, marker, or knot, establish that thing clearly in the scene text first. Do not invent unsupported focal objects in the menu.\n\n"
        "- Feel free to act creatively. Make bold choices as long as they fit in the story.\n"
        "- Introduce or reintroduce characters frequently. Characters make a story.\n"
        "- If someone besides the protagonist speaks on-screen, use a real character name and make sure that visible speaker can receive portrait/cutout art. Generic labels like 'Guard' or 'Patrol Member' should be reserved for unseen/offscreen voices or kept in narration until the character has a true name.\n"
        "- Frequently use ideas from IDEAS.md when the branch genuinely supports them. Planning runs exist to make this easier.\n"
        "- Introduce new locations frequently when appropriate, or deliberately route the story back to existing locations when the branch is naturally leading there. Places make motion, contrast, and consequence visible.\n"
        "- Most multi-choice scenes should include at least one consequential option that is not pure inspection.\n\n"
        "- Obey reveal_guardrails when present in the packet. Early local pressure, partial strange sightings, and first personal breadcrumbs are okay. Dumping the hidden regime, true masterminds, or full backstory too early is not okay yet.\n\n"
        "- Do not name a canonical character in scene text or choice text unless they have already appeared on this path or you are explicitly introducing them now.\n"
        f"- Already-met canonical characters on this path: {', '.join(allowed_names) if allowed_names else '(none yet besides implicit player context)'}.\n\n"
        "- If you are not deliberately changing affordances in this scene, return affordance_changes as [].\n"
        "- Do not copy available_affordance_names into affordance_changes. Each affordance_changes item must use the AffordanceChange shape with fields like action and name.\n\n"
        f"Validation issues:\n{json.dumps(issues, indent=2)}\n\n"
        "Previous invalid candidate:\n"
        f"{json.dumps(previous_candidate.model_dump(mode='json'), indent=2)}\n\n"
        "Original packet:\n"
        f"{json.dumps(packet, indent=2)}"
    )


def build_schema_retry_user_prompt(
    *,
    packet: dict[str, Any],
    raw_text: str,
    issues: list[str],
) -> str:
    if packet.get("run_mode") == "revival":
        return (
            "Your previous revival-choice response did not match the required JSON/schema.\n"
            "Fix the listed schema issues and return one corrected revival-choice JSON object only.\n"
            "Do not explain the changes. Do not stop. Keep trying until the JSON parses and validates.\n\n"
            "Important reminders:\n"
            "- choice_text is required.\n"
            "- notes must use meaningful planning notes in the form 'NEXT_NODE: ... FURTHER_GOALS: ...'.\n"
            "- Use NEXT_NODE as a base for your scene, but expand and elaborate on it. Do not simply repeat it.\n"
            "- If choice_class is 'ending', include ending_category.\n"
            "- Return JSON only, with no markdown fences or commentary.\n\n"
            f"Schema issues:\n{json.dumps(issues, indent=2)}\n\n"
            "Previous invalid response:\n"
            f"{raw_text.strip()}\n\n"
            "Original packet:\n"
            f"{json.dumps(packet, indent=2)}"
        )
    return (
        "Your previous response did not match the required JSON/schema.\n"
        "Fix the listed schema issues and return a corrected JSON object only.\n"
        "Do not explain the changes. Do not stop. Keep trying until the JSON parses and validates.\n\n"
        "Important reminders:\n"
        "- Use only allowed slot values in scene_present_entities: "
        "'hero-center', 'left-support', 'right-support', 'left-foreground-object', "
        "'right-foreground-object', or 'center-foreground-object'.\n"
        "- scene_present_entities and entity_references must use positive existing entity_id values; never omit entity_id and never use 0.\n"
        "- Every choice.notes value must use meaningful planning notes in the form 'NEXT_NODE: ... FURTHER_GOALS: ...'.\n"
        "- Use NEXT_NODE as a base for your scene, but expand and elaborate on it. Do not simply repeat it.\n"
        "- Set target_node_id to null unless you are intentionally quick-merging into one of the listed merge_candidates.\n"
        "- Return only GenerationCandidate fields. Do not include pre_change_url, ideas_to_append, validation_status, or next_action.\n"
        "- new_locations/new_characters/new_objects are only for brand-new canon entities. Existing canon belongs in entity_references or scene_present_entities instead.\n"
        "- If an existing recurring character has not been met on this path yet, use floating_character_introductions with their existing character_id and a short first-meeting beat.\n"
        "- entity_references entries should only contain entity_type, entity_id, and role.\n"
        "- scene_present_entities entries should contain entity_type, entity_id, slot, and optional staging fields; they do not use role.\n"
        "- Locations usually belong in entity_references with role current_scene, not in scene_present_entities.\n"
        "- new_hooks entries are new hook proposals and require hook_type and summary; do not include hook ids there.\n"
        "- Do not simply restate the just-taken choice as another option. Advance the scene before offering the next menu.\n"
        "- If choice_handoff is present in the packet, deliver or clearly pivot from its NEXT_NODE instead of ignoring it.\n"
        "- Use NEXT_NODE as a base for your scene, but expand and elaborate on it. Do not simply repeat it.\n"
        "- Do not name a character, object, location, title, faction, or system in playable text just because it appears in hooks, worldbuilding, or other behind-the-scenes packet memory. Check whether the player actually knows it yet.\n"
        "- Do not repeat the parent scene summary with only cosmetic changes.\n"
        "- If a choice names a local prop or marker, establish it in the scene text first instead of inventing it only in the menu.\n"
        "- Feel free to act creatively. Make bold choices as long as they fit in the story.\n"
        "- Introduce or reintroduce characters frequently. Characters make a story.\n"
        "- If someone besides the protagonist speaks on-screen, use a real character name and make sure that visible speaker can receive portrait/cutout art. Generic labels like 'Guard' or 'Patrol Member' should be reserved for unseen/offscreen voices or kept in narration until the character has a true name.\n"
        "- Frequently use ideas from IDEAS.md when the branch genuinely supports them. Planning runs exist to make this easier.\n"
        "- Introduce new locations frequently when appropriate, or deliberately route the story back to existing locations when the branch is naturally leading there. Places make motion, contrast, and consequence visible.\n"
        "- Most multi-choice scenes should include at least one consequential option that is not pure inspection.\n"
        "- Obey reveal_guardrails when present in the packet. Early local pressure, partial strange sightings, and first personal breadcrumbs are okay. Dumping the hidden regime, true masterminds, or full backstory too early is not okay yet.\n"
        "- If you are not deliberately changing affordances in this scene, return affordance_changes as [].\n"
        "- Do not copy available_affordance_names into affordance_changes.\n"
        "- Return JSON only, with no markdown fences or commentary.\n\n"
        f"Schema issues:\n{json.dumps(issues, indent=2)}\n\n"
        "Previous invalid response:\n"
        f"{raw_text.strip()}\n\n"
        "Original packet:\n"
        f"{json.dumps(packet, indent=2)}"
    )


def build_planning_retry_user_prompt(
    *,
    packet: dict[str, Any],
    ideas: list[PlanningIdea],
    previous_result: PlanningFollowthroughResult,
    issues: list[str],
) -> str:
    packet_for_prompt = dict(packet)
    packet_for_prompt["fresh_ideas_for_this_run"] = [idea.model_dump() for idea in ideas]
    return (
        "Your previous planning result failed validation.\n"
        "Fix the listed planning issues and return a corrected planning JSON object only.\n"
        "Do not explain the changes. Do not stop. Keep trying until the planning result passes validation.\n\n"
        "Important reminders:\n"
        "- Update at least one frontier choice note.\n"
        "- Bind at least one updated choice to a specific idea using bound_idea.\n"
        "- Prefer choice notes in the form 'NEXT_NODE: ... FURTHER_GOALS: ...' so later normal runs get a concrete handoff.\n"
        "- Prefer planning notes that create short-horizon behavior such as introduce a character soon, move to a new or known location soon, escalate patrol pressure, or set up a merge/closure.\n"
        "- worldbuilding_notes are optional but allowed when the world needs more ambient pressure or conflict.\n\n"
        f"Planning issues:\n{json.dumps(issues, indent=2)}\n\n"
        "Previous invalid planning result:\n"
        f"{json.dumps(previous_result.model_dump(mode='json'), indent=2)}\n\n"
        "Original packet:\n"
        f"{json.dumps(packet_for_prompt, indent=2)}"
    )


def build_planning_ideas_retry_user_prompt(
    *,
    packet: dict[str, Any],
    previous_result: PlanningIdeasResult,
    issues: list[str],
) -> str:
    required_count = int((packet.get("planning_policy") or {}).get("ideas_per_run") or 3)
    return (
        "Your previous fresh-idea batch failed validation.\n"
        "Fix the listed originality issues and return a corrected JSON object only.\n"
        "Do not explain the changes. Do not stop. Keep trying until the idea batch passes validation.\n\n"
        "Important reminders:\n"
        f"- Return exactly {required_count} ideas.\n"
        "- Cover at least 3 categories across character/location/object/event.\n"
        "- Include at least one event idea.\n"
        "- Do not reuse or lightly mutate the same motif across multiple ideas.\n"
        "- Do not repeat or closely remix existing repo ideas.\n"
        "- Avoid bells, orchards, rival clerks, archivists, reversed announcements, whispering gutters, or identity-correction paperwork as your central gimmick.\n\n"
        f"Idea issues:\n{json.dumps(issues, indent=2)}\n\n"
        "Previous invalid idea batch:\n"
        f"{json.dumps(previous_result.model_dump(mode='json'), indent=2)}"
    )


def get_test_client() -> TestClient:
    return TestClient(create_app())


def validate_candidate(client: TestClient, candidate: GenerationCandidate) -> dict[str, Any]:
    validation = client.post("/jobs/validate-generation", json=candidate.model_dump(mode="json"))
    validation.raise_for_status()
    return validation.json()


def run_normal_conversational_builder(
    *,
    packet: dict[str, Any],
    args: argparse.Namespace,
    worker_guide: str,
    project_root: Path,
    client: TestClient,
) -> tuple[GenerationCandidate, dict[str, Any], Path, NormalRunConversationState, dict[str, Any]]:
    if args.author_mode not in {"ai", "human"}:
        raise RuntimeError(f"Unsupported author mode: {args.author_mode}")

    session_path = get_default_session_path(project_root)
    validation_attempt_log_path = get_default_validation_attempt_log_path(project_root)
    session = load_or_create_normal_session(
        session_path=session_path,
        model=args.model,
        system_prompt=build_normal_conversation_system_prompt(worker_guide),
        context_run_limit=max(args.context_run_limit, 1),
        reset_context=args.reset_context,
    )
    resolution = resolve_normal_context(packet)
    state = NormalRunConversationState()

    append_session_message(
        session,
        role="user",
        content=build_normal_run_intro_prompt(
            packet=packet,
            previous_outcome=session.last_run_outcome,
        ),
    )

    def get_step_response(*, prompt_text: str) -> str:
        append_session_message(session, role="user", content=prompt_text)
        save_normal_session(session_path, session)
        if args.author_mode == "human":
            response_text = request_human_step(prompt_text)
        else:
            response_text = request_nonempty_ai_step_response(
                api_base=args.api_base,
                model=args.model,
                messages=session_messages_for_api(session),
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                request_timeout=args.request_timeout,
            )
        if response_text.strip():
            append_session_message(session, role="assistant", content=response_text)
            save_normal_session(session_path, session)
        return response_text

    def log_validation_attempt(
        *,
        step_name: str,
        attempted_output: str,
        issues: list[str],
        choice_index: int | None = None,
        detail_target: tuple[str, str] | None = None,
        retry_index: int | None = None,
    ) -> None:
        append_validation_attempt_log_record(
            log_path=validation_attempt_log_path,
            record={
                **build_log_timestamps(),
                "model": args.model,
                "run_mode": "normal",
                "author_mode": args.author_mode,
                "choice_id": (packet.get("selected_frontier_item") or {}).get("choice_id"),
                "pre_change_url": packet.get("pre_change_url"),
                "step": step_name,
                "retry_index": retry_index,
                "choice_index": (choice_index + 1) if choice_index is not None else None,
                "detail_target": {
                    "type": detail_target[0],
                    "name": detail_target[1],
                }
                if detail_target is not None
                else None,
                "issues": list(issues),
                "attempted_output": attempted_output,
            },
        )

    step_retry_limit = max(args.max_retries, 1)
    raw_scene_plan = ""
    raw_scene_body = ""

    scene_plan_issues: list[str] | None = None
    scene_body_issues: list[str] | None = None
    while True:
        if state.scene_plan is None:
            for attempt in range(step_retry_limit):
                raw_scene_plan = get_step_response(
                    prompt_text=build_step_prompt(
                        step_name="scene_plan",
                        packet=packet,
                        state=state,
                        requested_choice_count=args.requested_choice_count,
                        issues=scene_plan_issues,
                        retry_index=attempt,
                    )
                )
                if args.author_mode == "human" and is_force_next_override(raw_scene_plan):
                    state.scene_plan = build_forced_scene_plan_draft(packet=packet, resolution=resolution)
                    mark_force_next_step(state, "scene_plan")
                    break
                try:
                    draft = parse_scene_plan_form(raw_scene_plan)
                    scene_plan_issues = validate_scene_plan_draft(packet=packet, draft=draft, resolution=resolution)
                    if not scene_plan_issues:
                        state.scene_plan = draft
                        break
                    log_validation_attempt(
                        step_name="scene_plan",
                        attempted_output=raw_scene_plan,
                        issues=scene_plan_issues,
                        retry_index=attempt + 1,
                    )
                except (ValidationError, ValueError) as exc:
                    scene_plan_issues = [str(exc)]
                    log_validation_attempt(
                        step_name="scene_plan",
                        attempted_output=raw_scene_plan,
                        issues=scene_plan_issues,
                        retry_index=attempt + 1,
                    )
            if state.scene_plan is None:
                raise RuntimeError("Failed to produce a valid scene_plan form.")

        rewind_to_scene_plan = False
        rewind_scene_plan_issues: list[str] | None = None
        if state.scene_body is None:
            for attempt in range(step_retry_limit):
                raw_scene_body = get_step_response(
                    prompt_text=build_step_prompt(
                        step_name="scene_body",
                        packet=packet,
                        state=state,
                        requested_choice_count=args.requested_choice_count,
                        issues=scene_body_issues,
                        retry_index=attempt,
                    )
                )
                if args.author_mode == "human" and is_force_next_override(raw_scene_body):
                    state.scene_body = build_forced_scene_body_draft()
                    mark_force_next_step(state, "scene_body")
                    break
                try:
                    draft = parse_scene_body_form(raw_scene_body)
                    scene_body_issues = validate_scene_body_draft(packet=packet, state=state, draft=draft, resolution=resolution)
                    if not scene_body_issues:
                        state.scene_body = draft
                        break
                    log_validation_attempt(
                        step_name="scene_body",
                        attempted_output=raw_scene_body,
                        issues=scene_body_issues,
                        retry_index=attempt + 1,
                    )
                    if scene_body_issues_require_scene_plan_rewind(scene_body_issues):
                        rewind_to_scene_plan = True
                        if any("Isolation pressure is active" in issue or "Location-stall pressure is active" in issue for issue in scene_body_issues):
                            rewind_scene_plan_issues = list(scene_body_issues)
                        else:
                            rewind_scene_plan_issues = [
                                "The previous SCENE_BODY introduced or used an in-world speaking character through Narrator text without proper casting. "
                                "Fix SCENE_CAST and/or NEW_CHARACTERS now, add NEW_CHARACTER_INTRO if needed, then retry SCENE_BODY."
                            ]
                        break
                except (ValidationError, ValueError) as exc:
                    scene_body_issues = [str(exc)]
                    log_validation_attempt(
                        step_name="scene_body",
                        attempted_output=raw_scene_body,
                        issues=scene_body_issues,
                        retry_index=attempt + 1,
                    )
            if rewind_to_scene_plan:
                state.scene_plan = None
                state.scene_body = None
                scene_plan_issues = rewind_scene_plan_issues
                scene_body_issues = None
                continue
            if state.scene_body is None:
                raise RuntimeError("Failed to produce a valid scene_body form.")
        break

    def collect_choice(
        *,
        choice_index: int,
        requested_count: int,
        optional: bool = False,
        prompt_issues: list[str] | None = None,
    ) -> ChoiceDraft | None:
        issues: list[str] | None = list(prompt_issues) if prompt_issues else None
        for attempt in range(step_retry_limit):
            raw_choice = get_step_response(
                prompt_text=build_step_prompt(
                    step_name="choice",
                    packet=packet,
                    state=state,
                    requested_choice_count=requested_count,
                    issues=issues,
                    choice_index=choice_index,
                    optional_choice=optional,
                    retry_index=attempt,
                )
            )
            if args.author_mode == "human" and is_force_next_override(raw_choice):
                mark_force_next_step(state, f"choice_{choice_index + 1}")
                if optional:
                    return None
                return build_forced_choice_draft(packet=packet, choice_index=choice_index)
            if optional and should_skip_optional_choice(raw_choice):
                return None
            try:
                draft = parse_choice_form(raw_choice)
                issues = validate_choice_draft(
                    packet=packet,
                    state=state,
                    draft=draft,
                    resolution=resolution,
                    choice_index=choice_index,
                )
                if not issues:
                    return draft
                log_validation_attempt(
                    step_name="choice",
                    attempted_output=raw_choice,
                    issues=issues,
                    choice_index=choice_index,
                    retry_index=attempt + 1,
                )
            except (ValidationError, ValueError) as exc:
                issues = [str(exc)]
                log_validation_attempt(
                    step_name="choice",
                    attempted_output=raw_choice,
                    issues=issues,
                    choice_index=choice_index,
                    retry_index=attempt + 1,
                )
        if optional:
            return None
        raise RuntimeError(f"Failed to produce a valid choice_{choice_index + 1} form.")

    first_choice = collect_choice(choice_index=0, requested_count=max(args.requested_choice_count, 1))
    if first_choice is None:
        raise RuntimeError("Failed to produce a valid choice_1 form.")
    state.choices.append(first_choice)

    if args.requested_choice_count == 2:
        second_choice = collect_choice(choice_index=1, requested_count=2, optional=True)
        if second_choice is not None:
            state.choices.append(second_choice)

        menu_issues = validate_choice_menu(packet=packet, choices=state.choices, state=state, resolution=resolution)
        menu_retry_count = 0
        while menu_issues and menu_retry_count < step_retry_limit:
            menu_retry_count += 1
            log_validation_attempt(
                step_name="choice_menu",
                attempted_output=json.dumps([choice.model_dump(mode="json") for choice in state.choices], ensure_ascii=False),
                issues=menu_issues,
                retry_index=menu_retry_count,
            )
            replacement = collect_choice(
                choice_index=1,
                requested_count=2,
                optional=False,
                prompt_issues=menu_issues,
            )
            if replacement is None:
                raise RuntimeError("Failed to produce a strong enough choice menu.")
            if len(state.choices) == 1:
                state.choices.append(replacement)
            else:
                state.choices[-1] = replacement
            menu_issues = validate_choice_menu(packet=packet, choices=state.choices, state=state, resolution=resolution)
        if menu_issues:
            log_validation_attempt(
                step_name="choice_menu",
                attempted_output=json.dumps([choice.model_dump(mode="json") for choice in state.choices], ensure_ascii=False),
                issues=menu_issues,
                retry_index=menu_retry_count + 1,
            )
            raise RuntimeError("Failed to produce a strong enough choice menu.")

        if len(state.choices) >= 2:
            third_choice = collect_choice(choice_index=2, requested_count=3, optional=True)
            if third_choice is not None:
                state.choices.append(third_choice)
                menu_issues = validate_choice_menu(packet=packet, choices=state.choices, state=state, resolution=resolution)
                if menu_issues:
                    state.choices.pop()
    else:
        for choice_index in range(1, args.requested_choice_count):
            accepted_choice = collect_choice(choice_index=choice_index, requested_count=args.requested_choice_count, optional=False)
            if accepted_choice is None:
                raise RuntimeError(f"Failed to produce a valid choice_{choice_index + 1} form.")
            state.choices.append(accepted_choice)
        menu_issues = validate_choice_menu(packet=packet, choices=state.choices, state=state, resolution=resolution)
        if menu_issues:
            log_validation_attempt(
                step_name="choice_menu",
                attempted_output=json.dumps([choice.model_dump(mode="json") for choice in state.choices], ensure_ascii=False),
                issues=menu_issues,
                retry_index=1,
            )
            raise RuntimeError("Failed to produce a strong enough choice menu.")

    state.transition_nodes = []
    for merge_choice_index, merge_choice in enumerate(state.choices):
        if merge_choice.target_existing_node is None:
            continue
        transition_issues: list[str] | None = None
        accepted_transition: TransitionNodeDraft | None = None
        for attempt in range(step_retry_limit):
            raw_transition = get_step_response(
                prompt_text=build_step_prompt(
                    step_name="link_nodes",
                    packet=packet,
                    state=state,
                    requested_choice_count=args.requested_choice_count,
                    issues=transition_issues,
                    transition_target=(merge_choice_index, int(merge_choice.target_existing_node)),
                    retry_index=attempt,
                )
            )
            if args.author_mode == "human" and is_force_next_override(raw_transition):
                accepted_transition = build_forced_transition_node_draft(
                    choice_index=merge_choice_index,
                    target_existing_node=int(merge_choice.target_existing_node),
                )
                mark_force_next_step(state, f"link_nodes:{merge_choice_index + 1}")
                break
            try:
                draft = parse_transition_node_form(
                    raw_text=raw_transition,
                    choice_index=merge_choice_index,
                    target_existing_node=int(merge_choice.target_existing_node),
                )
                transition_issues = validate_transition_node_draft(
                    packet=packet,
                    state=state,
                    draft=draft,
                    resolution=resolution,
                )
                if not transition_issues:
                    accepted_transition = draft
                    break
                log_validation_attempt(
                    step_name="link_nodes",
                    attempted_output=raw_transition,
                    issues=transition_issues,
                    choice_index=merge_choice_index,
                    retry_index=attempt + 1,
                )
            except (ValidationError, ValueError) as exc:
                transition_issues = [str(exc)]
                log_validation_attempt(
                    step_name="link_nodes",
                    attempted_output=raw_transition,
                    issues=transition_issues,
                    choice_index=merge_choice_index,
                    retry_index=attempt + 1,
                )
        if accepted_transition is None:
            raise RuntimeError(f"Failed to produce a valid transition bridge for choice_{merge_choice_index + 1}.")
        state.transition_nodes.append(accepted_transition)

    if should_request_hooks(packet=packet, state=state):
        raw_hooks = get_step_response(
            prompt_text=build_step_prompt(
                step_name="hooks",
                packet=packet,
                state=state,
                requested_choice_count=args.requested_choice_count,
            )
        )
        if not should_skip_optional_choice(raw_hooks):
            hooks_accepted = False
            for attempt in range(step_retry_limit):
                if args.author_mode == "human" and is_force_next_override(raw_hooks):
                    mark_force_next_step(state, "hooks")
                    hooks_accepted = False
                    break
                try:
                    draft = parse_scene_hooks_form(raw_hooks)
                    issues = validate_scene_hooks_draft(packet=packet, state=state, draft=draft, resolution=resolution)
                    if not issues:
                        state.hooks = draft
                        hooks_accepted = True
                        break
                    log_validation_attempt(
                        step_name="hooks",
                        attempted_output=raw_hooks,
                        issues=issues,
                        retry_index=attempt + 1,
                    )
                except (ValidationError, ValueError) as exc:
                    issues = [str(exc)]
                    log_validation_attempt(
                        step_name="hooks",
                        attempted_output=raw_hooks,
                        issues=issues,
                        retry_index=attempt + 1,
                    )
                if attempt == step_retry_limit - 1:
                    break
                raw_hooks = get_step_response(
                    prompt_text=build_step_prompt(
                        step_name="hooks",
                        packet=packet,
                        state=state,
                        requested_choice_count=args.requested_choice_count,
                        issues=issues,
                        retry_index=attempt,
                    )
                )
                if should_skip_optional_choice(raw_hooks):
                    break
            if not hooks_accepted:
                state.hooks = None

    if should_request_details(packet=packet, state=state):
        for detail_target in get_detail_targets(state=state):
            target_type, _target_name = detail_target
            expected_detail_labels = (
                ["CHARACTER_DETAILS", "CHARACTER_ART_HINTS"]
                if target_type == "character"
                else ["LOCATION_DETAILS", "LOCATION_ART_HINTS"]
            )
            issues = None
            for attempt in range(step_retry_limit):
                raw_details = get_step_response(
                    prompt_text=build_step_prompt(
                        step_name="details",
                        packet=packet,
                        state=state,
                        requested_choice_count=args.requested_choice_count,
                        issues=issues,
                        detail_target=detail_target,
                        retry_index=attempt,
                    )
                )
                if args.author_mode == "human" and is_force_next_override(raw_details):
                    draft, art_draft = build_forced_detail_target_response(
                        target_type=detail_target[0],
                        target_name=detail_target[1],
                    )
                    state.extras = merge_scene_extras(state.extras, draft)
                    state.art = merge_scene_art(state.art, art_draft)
                    mark_force_next_step(state, f'details:{detail_target[0]}:{detail_target[1]}')
                    break
                try:
                    draft, art_draft = parse_detail_target_response(
                        raw_details,
                        target_type=detail_target[0],
                        target_name=detail_target[1],
                    )
                    issues = validate_detail_target_draft(
                        target_type=detail_target[0],
                        target_name=detail_target[1],
                        extras=draft,
                        art=art_draft,
                    )
                    if not issues:
                        state.extras = merge_scene_extras(state.extras, draft)
                        state.art = merge_scene_art(state.art, art_draft)
                        break
                    log_validation_attempt(
                        step_name="details",
                        attempted_output=raw_details,
                        issues=issues,
                        detail_target=detail_target,
                        retry_index=attempt + 1,
                    )
                except (ValidationError, ValueError) as exc:
                    issues = [str(exc)]
                    log_validation_attempt(
                        step_name="details",
                        attempted_output=raw_details,
                        issues=issues,
                        detail_target=detail_target,
                        retry_index=attempt + 1,
                    )
                if attempt == step_retry_limit - 1:
                    raise RuntimeError(f'Failed to produce a valid details form for {detail_target[0]} "{detail_target[1]}".')

    candidate = assemble_generation_candidate_from_state(packet=packet, state=state, resolution=resolution)
    candidate = prune_existing_asset_requests(packet=packet, candidate=candidate)
    candidate = repair_generation_candidate(packet=packet, candidate=candidate)
    if state.force_next_steps:
        validated_payload = {
            "valid": True,
            "issues": [
                "FORCE NEXT was used in human mode. This run is preview-only and will not be validated or applied."
            ],
            "forced_preview_only": True,
            "force_next_steps": list(state.force_next_steps),
        }
        return candidate, validated_payload, session_path, state, resolution

    validated_payload = validate_candidate(client, candidate)
    continuity_issues = collect_character_continuity_issues(packet=packet, candidate=candidate)
    scene_anchor_issues = collect_scene_anchor_art_issues(packet=packet, candidate=candidate)
    redundant_progression_issues = collect_redundant_progression_issues(packet=packet, candidate=candidate)
    ungrounded_prop_issues = collect_ungrounded_local_prop_issues(packet=packet, candidate=candidate)
    branch_pressure_issues = collect_branch_pressure_issues(packet=packet, candidate=candidate)
    combined_extra_issues = (
        continuity_issues
        + scene_anchor_issues
        + redundant_progression_issues
        + ungrounded_prop_issues
        + branch_pressure_issues
    )
    if combined_extra_issues:
        validated_payload["valid"] = False
        validated_payload["issues"] = list(validated_payload.get("issues", [])) + combined_extra_issues
        log_validation_attempt(
            step_name="final_validation",
            attempted_output=json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False),
            issues=list(validated_payload["issues"]),
        )
        raise RuntimeError(f"Validation failed: {json.dumps(validated_payload['issues'], indent=2)}")
    if not validated_payload["valid"]:
        log_validation_attempt(
            step_name="final_validation",
            attempted_output=json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False),
            issues=list(validated_payload.get("issues", [])),
        )
        raise RuntimeError(f"Validation failed: {json.dumps(validated_payload['issues'], indent=2)}")
    return candidate, validated_payload, session_path, state, resolution


def collect_character_continuity_issues(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
) -> list[str]:
    selected = packet.get("selected_frontier_item") or {}
    parent_node_id = selected.get("from_node_id")
    if parent_node_id is None:
        return []

    settings = Settings.from_env()
    connection = connect(settings.database_path)
    try:
        canon = CanonResolver(connection)
        story = StoryGraphService(connection)
        encountered_ids = story.list_lineage_entity_ids(int(parent_node_id), "character")
        introduced_names = {
            (character.name or "").strip().lower()
            for character in candidate.new_characters
            if (character.name or "").strip()
        }
        floating_intro_character_ids = {
            int(intro.character_id)
            for intro in candidate.floating_character_introductions
        }
        explicitly_introduced_existing_ids = {
            int(reference.entity_id)
            for reference in candidate.entity_references
            if reference.entity_type == "character"
        } | {
            int(present.entity_id)
            for present in candidate.scene_present_entities
            if present.entity_type == "character"
        } | floating_intro_character_ids
        allowed_names = {
            (character.get("name") or "").strip().lower()
            for character_id in encountered_ids
            if (character := canon.get_character(character_id)) is not None and (character.get("name") or "").strip()
        }
        allowed_names.update(
            (character.get("name") or "").strip().lower()
            for character_id in explicitly_introduced_existing_ids
            if (character := canon.get_character(character_id)) is not None and (character.get("name") or "").strip()
        )
        allowed_names.update(introduced_names)

        texts = [
            candidate.scene_summary,
            candidate.scene_text,
            *(line.text for line in candidate.dialogue_lines),
            *(choice.choice_text for choice in candidate.choices),
        ]

        issues: list[str] = []
        seen_names: set[str] = set()
        for character in canon.list_characters():
            name = (character.get("name") or "").strip()
            if not name:
                continue
            lowered = name.lower()
            if lowered in allowed_names or lowered in seen_names:
                continue
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", re.IGNORECASE)
            if any(pattern.search(text or "") for text in texts):
                issues.append(
                    f"Character '{name}' is named in the new scene/choices but has not appeared on this path yet. "
                    "Either introduce them explicitly in-scene first, use floating_character_introductions, or remove the reference."
                )
                seen_names.add(lowered)
        return issues
    finally:
        connection.close()


def collect_scene_anchor_art_issues(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
) -> list[str]:
    texts = [
        (packet.get("selected_frontier_item") or {}).get("choice_text") or "",
        candidate.scene_summary or "",
        candidate.scene_text or "",
    ]
    scene_transition_expected = any(
        pattern.search(text)
        for pattern in SCENE_TRANSITION_CUE_PATTERNS
        for text in texts
        if text
    )
    if not scene_transition_expected:
        return []

    has_location_current_scene = any(
        reference.role == "current_scene" and reference.entity_type == "location"
        for reference in candidate.entity_references
    )
    if has_location_current_scene:
        return []

    object_render_requests = [
        request for request in candidate.asset_requests
        if request.asset_kind == "object_render"
    ]
    if not object_render_requests:
        return []

    return [
        "This draft looks like a travel/arrival scene, but it does not anchor the scene to a location current_scene and instead requests object art. Do not use object_render as a stand-in for a scene background; create or reference the location and request or reuse its background instead."
    ]


def collect_redundant_progression_issues(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
) -> list[str]:
    selected = packet.get("selected_frontier_item") or {}
    context_summary = packet.get("context_summary") or {}
    current_node = context_summary.get("current_node") or {}
    parent_choice_text = (selected.get("choice_text") or "").strip()
    parent_choice_notes = (selected.get("existing_choice_notes") or "").strip()
    parent_summary = (current_node.get("summary") or "").strip()

    issues: list[str] = []
    if parent_summary and texts_are_near_duplicates(candidate.scene_summary or "", parent_summary):
        issues.append(
            "The new scene_summary too closely repeats the parent scene summary. Advance the situation materially instead of restating the same beat."
        )

    seen_choice_texts: list[str] = []
    seen_choice_notes: list[str] = []
    for choice in candidate.choices:
        if parent_choice_text and texts_are_near_duplicates(choice.choice_text, parent_choice_text):
            issues.append(
                f"Choice '{choice.choice_text}' too closely repeats the just-taken choice. The next scene should offer a materially new follow-up, merge, or closure instead of re-offering the same action."
            )
        if parent_choice_notes and texts_are_near_duplicates(choice.notes or "", parent_choice_notes):
            issues.append(
                f"Choice '{choice.choice_text}' reuses the just-taken NEXT_NODE/FURTHER_GOALS almost verbatim. Give the new scene a distinct next-step purpose instead of repeating the same plan."
            )
        if any(texts_are_near_duplicates(choice.choice_text, prior_text) for prior_text in seen_choice_texts):
            issues.append(
                f"Choice '{choice.choice_text}' duplicates another choice in the same scene. Each option should represent a genuinely different next step."
            )
        if any(texts_are_near_duplicates(choice.notes or "", prior_notes) for prior_notes in seen_choice_notes):
            issues.append(
                f"Choice '{choice.choice_text}' duplicates another choice's NEXT_NODE/FURTHER_GOALS lane too closely. Separate the options more clearly."
            )
        seen_choice_texts.append(choice.choice_text)
        seen_choice_notes.append(choice.notes or "")
    return issues


def collect_ungrounded_local_prop_issues(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
) -> list[str]:
    selected = packet.get("selected_frontier_item") or {}
    context_summary = packet.get("context_summary") or {}
    current_node = context_summary.get("current_node") or {}
    support_text = "\n".join(
        filter(
            None,
            [
                selected.get("choice_text") or "",
                selected.get("existing_choice_notes") or "",
                current_node.get("title") or "",
                current_node.get("summary") or "",
                candidate.scene_summary or "",
                candidate.scene_text or "",
                *(line.text for line in candidate.dialogue_lines),
            ],
        )
    )
    support_tokens = similarity_tokens(support_text, extra_stopwords=CHOICE_GENERIC_TOKENS)

    issues: list[str] = []
    for choice in candidate.choices:
        for pattern in LOCAL_PROP_CHOICE_PATTERNS:
            match = pattern.search(choice.choice_text or "")
            if match is None:
                continue
            phrase = extract_grounding_phrase(match.group(1))
            phrase_tokens = similarity_tokens(phrase, extra_stopwords=CHOICE_GENERIC_TOKENS)
            if len(phrase_tokens) < 2:
                continue
            if len(phrase_tokens - support_tokens) >= 2:
                issues.append(
                    f"Choice '{choice.choice_text}' introduces a new focal prop or marker ('{phrase}') that the scene does not establish. If that object matters, establish it clearly in the scene text first or rename the choice to match grounded scene details."
                )
                break
    return issues


def collect_branch_pressure_issues(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
) -> list[str]:
    isolation_pressure = packet.get("isolation_pressure") or {}
    new_character_pressure = packet.get("new_character_pressure") or {}
    location_stall_pressure = packet.get("location_stall_pressure") or {}
    location_transition_obligation = packet.get("location_transition_obligation") or {}
    action_summary = packet.get("recent_action_family_summary") or {}
    issues: list[str] = []

    repeated_action_family = action_summary.get("repeated_action_family")
    repeated_action_count = int((action_summary.get("recent_action_family_counts") or {}).get(repeated_action_family or "", 0))

    choice_classes: list[str] = []
    consequential_choice_count = 0
    for choice in candidate.choices:
        choice_class = choice.choice_class or infer_choice_class_from_text(choice.choice_text, choice.notes)
        choice_classes.append(choice_class)
        if choice_class in {"commitment", "location_transition", "ending"} or choice.target_node_id is not None:
            consequential_choice_count += 1
        elif choice_class == "progress" and choice_text_implies_consequence(choice.choice_text):
            consequential_choice_count += 1

    if len(candidate.choices) >= 2 and consequential_choice_count == 0:
        issues.append(
            "Multi-choice scenes should include at least one consequential option, such as a commitment, location_transition, merge, ending, social move, location change, or response to immediate pressure."
        )

    if isolation_pressure.get("active") and not candidate_adds_actor_pressure(candidate):
        issues.append(
            "Isolation pressure is active and this branch is still too solitary. Reintroduce or introduce a character, or bring clear faction/social pressure onstage."
        )

    if new_character_pressure.get("active") and not candidate_adds_new_character(candidate):
        issues.append(
            "New-character pressure is active and this branch still does not introduce a brand-new character. Use NEW_CHARACTERS; reusing only existing characters does not satisfy this."
        )

    if location_transition_obligation.get("active") and not candidate_satisfies_location_transition_obligation(
        packet=packet,
        candidate=candidate,
    ):
        issues.append(
            "This selected choice promised a location transition, but the resulting scene still does not actually change location. Use new_location or return_location so the child scene arrives somewhere different from the parent current location."
        )

    if location_stall_pressure.get("active") and not candidate_has_location_transition_choice(candidate):
        issues.append(
            "Location-stall pressure is active and this menu still does not include a CHOICE_CLASS: location_transition option. Satisfy that in the choice-writing phase instead of forcing this scene to teleport."
        )

    if repeated_action_family in {"inspect", "follow", "touch", "step_back"} and repeated_action_count >= 3:
        candidate_families = [classify_choice_action_family(choice.choice_text) for choice in candidate.choices]
        if candidate_families and all(family in {repeated_action_family, "other"} for family in candidate_families):
            issues.append(
                f"This branch has been overusing the '{repeated_action_family}' action family. Break the pattern with a social turn, location shift, merge, closure, or another materially different move."
            )

    if (
        isolation_pressure.get("active")
        or new_character_pressure.get("active")
        or location_stall_pressure.get("active")
        or repeated_action_count >= 3
        or (packet.get("frontier_budget_state") or {}).get("pressure_level") in {"soft", "hard"}
    ) and not candidate_has_material_delta(candidate):
        issues.append(
            "The scene does not appear to create a material delta. Advance danger, cast, location access, hook pressure, merge/closure state, or world pressure becoming immediate."
        )

    return issues


def append_ideas(ideas_path: Path, ideas: list[PlanningIdea]) -> None:
    if not ideas:
        return
    existing = ideas_path.read_text(encoding="utf-8") if ideas_path.exists() else ""
    lines = existing.splitlines()
    if "## Open Ideas" not in existing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["## Open Ideas", ""])
    output = "\n".join(lines).rstrip() + "\n\n"
    for idea in ideas:
        category = idea.category.strip().title()
        title = idea.title.strip()
        note_text = idea.note_text.strip()
        if not category or not title or not note_text:
            continue
        output += f"- [{category}] {title}: {note_text}\n"
    ideas_path.write_text(output.rstrip() + "\n", encoding="utf-8")


def normalize_idea_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def idea_tokens(value: str, *, exclude_generic: bool = False) -> set[str]:
    tokens = {
        token[:-1] if token.endswith("s") and len(token) > 4 else token
        for token in re.findall(r"[a-z0-9]+", normalize_idea_text(value))
        if len(token) >= 4 and token not in IDEA_STOPWORDS
    }
    if exclude_generic:
        return {token for token in tokens if token not in GENERIC_IDEA_TOKENS}
    return tokens


def jaccard_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def significant_idea_overlap(left: set[str], right: set[str]) -> int:
    return len(left & right)


def get_planning_idea_issues(packet: dict[str, Any], ideas: list[PlanningIdea]) -> list[str]:
    issues: list[str] = []
    required_count = int((packet.get("planning_policy") or {}).get("ideas_per_run") or 0)
    if len(ideas) < max(required_count, 2):
        issues.append(
            f"Planning mode requires at least {max(required_count, 2)} categorized ideas, but only {len(ideas)} were returned."
        )

    categories = {idea.category for idea in ideas}
    if len(categories) < 2:
        issues.append(
            "Planning mode ideas must span at least 2 categories across character/location/object/event."
        )
    if "event" not in categories:
        issues.append("Planning mode idea batches must include at least one event idea.")

    existing_ideas = ((packet.get("ideas_file") or {}).get("open_ideas") or [])
    existing_content = normalize_idea_text(((packet.get("ideas_file") or {}).get("current_content") or ""))
    existing_title_tokens = {
        item.get("title", ""): idea_tokens(item.get("title", ""), exclude_generic=True)
        for item in existing_ideas
    }
    existing_distinctive_similarity_tokens = {
        item.get("title", ""): idea_tokens(
            f"{item.get('title', '')} {item.get('note_text', '')}",
            exclude_generic=True,
        )
        for item in existing_ideas
    }

    seen_titles: set[str] = set()
    seen_signatures: set[str] = set()
    title_token_occurrences: dict[str, int] = {}

    for idea in ideas:
        normalized_title = normalize_idea_text(idea.title)
        normalized_text = normalize_idea_text(idea.note_text)
        if normalized_title in DISALLOWED_PLANNING_IDEA_SEEDS:
            issues.append(
                f"Planning idea '{idea.title}' reuses a built-in example seed. Add a different unique idea."
            )
        if normalized_title in seen_titles:
            issues.append(f"Planning idea '{idea.title}' duplicates another new idea title in this run.")
        seen_titles.add(normalized_title)
        signature = f"{idea.category}:{normalized_title}:{normalized_text}"
        if signature in seen_signatures:
            issues.append(f"Planning idea '{idea.title}' duplicates another new idea in this run.")
        seen_signatures.add(signature)
        if normalized_title and normalized_title in existing_content:
            issues.append(
                f"Planning idea '{idea.title}' already appears in IDEAS.md. Add a genuinely new idea."
            )
        if normalized_text and normalized_text in existing_content:
            issues.append(
                f"Planning idea '{idea.title}' repeats wording already present in IDEAS.md. Add a genuinely new idea."
            )

        title_tokens = idea_tokens(idea.title, exclude_generic=True)
        distinctive_full_tokens = idea_tokens(
            f"{idea.title} {idea.note_text}",
            exclude_generic=True,
        )
        for token in title_tokens:
            title_token_occurrences[token] = title_token_occurrences.get(token, 0) + 1

        for existing_title, tokens in existing_title_tokens.items():
            if len(title_tokens & tokens) >= 3:
                issues.append(
                    f"Planning idea '{idea.title}' is too close to existing idea '{existing_title}'. Pick a more distinct concept."
                )
                break
        for existing_title, tokens in existing_distinctive_similarity_tokens.items():
            if (
                significant_idea_overlap(distinctive_full_tokens, tokens) >= 2
                and jaccard_similarity(distinctive_full_tokens, tokens) >= 0.35
            ):
                issues.append(
                    f"Planning idea '{idea.title}' is too similar to existing idea '{existing_title}'. Pick a more original concept."
                )
                break

    repeated_motif_tokens = [
        token
        for token, count in sorted(title_token_occurrences.items())
        if count > 1
    ]
    if repeated_motif_tokens:
        issues.append(
            "Planning ideas are clustering around the same motif words in their titles: "
            + ", ".join(repeated_motif_tokens[:6])
            + ". Spread the ideas farther apart."
        )

    for index, idea in enumerate(ideas):
        left_tokens = idea_tokens(f"{idea.title} {idea.note_text}", exclude_generic=True)
        for other in ideas[index + 1 :]:
            right_tokens = idea_tokens(f"{other.title} {other.note_text}", exclude_generic=True)
            if significant_idea_overlap(left_tokens, right_tokens) >= 2 and jaccard_similarity(left_tokens, right_tokens) >= 0.35:
                issues.append(
                    f"Planning ideas '{idea.title}' and '{other.title}' are too similar to each other. Spread the concepts farther apart."
                )

    return issues


def get_planning_validation_issues(
    packet: dict[str, Any],
    result: PlanningFollowthroughResult,
    ideas: list[PlanningIdea],
) -> list[str]:
    issues: list[str] = []
    if not result.choice_note_updates:
        issues.append("Planning mode must update at least one frontier choice note.")
        return issues

    fresh_idea_lookup = {
        (idea.title.strip().lower(), idea.category): idea
        for idea in ideas
    }
    existing_ideas = ((packet.get("ideas_file") or {}).get("open_ideas") or [])
    existing_idea_lookup = {
        ((item.get("title") or "").strip().lower(), (item.get("category") or "").strip().lower()): item
        for item in existing_ideas
        if (item.get("title") or "").strip() and (item.get("category") or "").strip()
    }
    if not any(update.bound_idea is not None for update in result.choice_note_updates):
        issues.append("Planning mode must bind at least one updated choice to a concrete idea using bound_idea.")
    for update in result.choice_note_updates:
        if update.bound_idea is None:
            continue
        key = (update.bound_idea.title.strip().lower(), update.bound_idea.category)
        if update.bound_idea.source == "fresh":
            if key not in fresh_idea_lookup:
                issues.append(
                    f"Choice update {update.choice_id} binds fresh idea '{update.bound_idea.title}', but that idea is not one of fresh_ideas_for_this_run."
                )
        elif key not in existing_idea_lookup:
            issues.append(
                f"Choice update {update.choice_id} binds existing idea '{update.bound_idea.title}', but that idea is not present in IDEAS.md."
            )
    return issues


def validate_planning_result(packet: dict[str, Any], ideas: list[PlanningIdea], result: PlanningFollowthroughResult) -> None:
    issues = get_planning_validation_issues(packet, result, ideas)
    if issues:
        raise RuntimeError("\n".join(issues))


def apply_planning_result(
    *,
    packet: dict[str, Any],
    ideas: list[PlanningIdea],
    result: PlanningFollowthroughResult,
    client: TestClient,
    ideas_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    validate_planning_result(packet, ideas, result)
    choice_updates: list[int] = []
    note_records: list[dict[str, Any]] = []
    worldbuilding_records: list[dict[str, Any]] = []
    if not dry_run:
        append_ideas(ideas_path, ideas)
        for update in result.choice_note_updates:
            payload: dict[str, Any] = {"notes": update.notes}
            if update.bound_idea is not None:
                payload["idea_binding"] = update.bound_idea.model_dump(mode="json")
            response = client.post(f"/choices/{update.choice_id}", json=payload)
            response.raise_for_status()
            choice_updates.append(update.choice_id)
        for note in result.story_direction_notes:
            response = client.post("/story-notes", json=note.model_dump())
            response.raise_for_status()
            note_records.append(response.json())
        for note in result.worldbuilding_notes:
            response = client.post("/worldbuilding", json=note.model_dump())
            response.raise_for_status()
            worldbuilding_records.append(response.json())
    else:
        choice_updates = [update.choice_id for update in result.choice_note_updates]

    target_labels = {
        int(target["choice_id"]): target["choice_text"]
        for target in packet.get("planning_targets", [])
    }

    return {
        "run_mode": "planning",
        "planning_reason": packet.get("planning_reason"),
        "dry_run": dry_run,
        "updated_choice_ids": choice_updates,
        "choice_note_updates": [
            {
                "choice_id": update.choice_id,
                "choice_text": target_labels.get(update.choice_id),
                "notes": update.notes,
                "bound_idea": update.bound_idea.model_dump(mode="json") if update.bound_idea is not None else None,
            }
            for update in result.choice_note_updates
        ],
        "ideas_added": len(ideas),
        "ideas_appended": [idea.model_dump() for idea in ideas],
        "story_notes_added": len(note_records) if not dry_run else len(result.story_direction_notes),
        "story_notes_created": note_records if not dry_run else [note.model_dump() for note in result.story_direction_notes],
        "worldbuilding_notes_added": len(worldbuilding_records) if not dry_run else len(result.worldbuilding_notes),
        "worldbuilding_notes_created": (
            worldbuilding_records if not dry_run else [note.model_dump() for note in result.worldbuilding_notes]
        ),
        "summary": result.summary,
    }


def sync_scripted_scene_visibility_after_apply(
    *,
    story_node_id: int,
    state: NormalRunConversationState,
    resolution: dict[str, Any],
) -> dict[str, Any]:
    if state.scene_plan is None or state.scene_body is None:
        settings = Settings.from_env()
        with connect(settings.database_path) as connection:
            return StoryGraphService(connection).get_story_node(story_node_id) or {}

    compiled_scene_body, compile_issues = compile_scene_body_draft(
        state=state,
        draft=state.scene_body,
        resolution=resolution,
    )
    if compile_issues or compiled_scene_body is None:
        raise RuntimeError(f"Could not sync scripted scene visibility after apply: {compile_issues}")

    scene_cast_names = resolve_scene_cast_names(draft=state.scene_plan, resolution=resolution)
    settings = Settings.from_env()
    with connect(settings.database_path) as connection:
        story = StoryGraphService(connection)
        canon = CanonResolver(connection)
        node = story.get_story_node(story_node_id)
        if node is None:
            raise RuntimeError(f"Could not load applied node {story_node_id} to sync scripted scene visibility.")

        connection.execute(
            "DELETE FROM story_node_present_entities WHERE story_node_id = ? AND entity_type = 'character'",
            (story_node_id,),
        )

        protagonist_name = (resolution.get("protagonist_name") or "").strip().lower()
        protagonist_id = resolution.get("protagonist_id")
        support_slots = ["left-support", "right-support"]
        next_support_index = 0
        total_line_count = len(compiled_scene_body.dialogue_lines)

        for name in scene_cast_names:
            lowered = name.strip().lower()
            if not lowered:
                continue
            character = canon.find_character_by_name(name)
            if character is None or character.get("id") is None:
                raise RuntimeError(f"Could not resolve scripted SCENE_CAST character '{name}' after apply.")
            hidden_on_lines = list(compiled_scene_body.hidden_lines_by_character.get(lowered, []))
            if total_line_count > 0 and len(hidden_on_lines) >= total_line_count:
                continue
            if lowered == protagonist_name and protagonist_id is not None:
                slot = "hero-center"
                focus = True
                character_id = int(protagonist_id)
            else:
                if next_support_index >= len(support_slots):
                    continue
                slot = support_slots[next_support_index]
                focus = False
                next_support_index += 1
                character_id = int(character["id"])

            story.link_present_entity(
                story_node_id=story_node_id,
                entity_type="character",
                entity_id=character_id,
                slot=slot,
                focus=focus,
                hidden_on_lines=hidden_on_lines,
            )

        updated = story.get_story_node(story_node_id)
        if updated is None:
            raise RuntimeError(f"Could not reload applied node {story_node_id} after syncing scripted scene visibility.")
        return updated


def apply_normal_result(
    *,
    packet: dict[str, Any],
    candidate: GenerationCandidate,
    client: TestClient,
    dry_run: bool,
    project_root: Path,
    state: NormalRunConversationState | None = None,
    resolution: dict[str, Any] | None = None,
    validation_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation_payload = validation_payload or validate_candidate(client, candidate)
    if validation_payload.get("forced_preview_only"):
        return {
            "run_mode": "normal",
            "dry_run": True,
            "forced_preview_only": True,
            "pre_change_url": packet["pre_change_url"],
            "choice_id": packet["selected_frontier_item"]["choice_id"],
            "validation": validation_payload,
            "message": "FORCE NEXT was used in human mode. Later sections were shown, but nothing was validated or applied.",
        }
    if not validation_payload["valid"]:
        raise RuntimeError(f"Validation failed: {json.dumps(validation_payload['issues'], indent=2)}")

    if dry_run:
        return {
            "run_mode": "normal",
            "dry_run": True,
            "pre_change_url": packet["pre_change_url"],
            "choice_id": packet["selected_frontier_item"]["choice_id"],
            "validation": validation_payload,
        }

    apply_payload = {
        "branch_key": packet["preview_payload"]["branch_key"],
        "parent_node_id": packet["preview_payload"]["current_node_id"],
        "choice_id": packet["preview_payload"]["choice_id"],
        "candidate": candidate.model_dump(mode="json"),
    }
    apply_response = client.post("/jobs/apply-generation", json=apply_payload)
    apply_response.raise_for_status()
    applied = apply_response.json()

    if state is not None and resolution is not None:
        applied_node = sync_scripted_scene_visibility_after_apply(
            story_node_id=int(applied["node"]["id"]),
            state=state,
            resolution=resolution,
        )
        applied["node"] = applied_node

    generated_assets: list[dict[str, Any]] = []
    inferred_assets: list[dict[str, Any]] = []
    for asset_request in candidate.asset_requests:
        if (
            asset_request.asset_kind == "cutout"
            or asset_request.entity_type is None
            or asset_request.entity_id is None
            or asset_request.prompt is None
        ):
            continue
        payload = {
            "asset_kind": asset_request.asset_kind,
            "entity_type": asset_request.entity_type,
            "entity_id": asset_request.entity_id,
            "prompt": asset_request.prompt,
            "width": asset_request.width or default_dimensions_for_asset_kind(asset_request.asset_kind)[0],
            "height": asset_request.height or default_dimensions_for_asset_kind(asset_request.asset_kind)[1],
            "steps": asset_request.steps or 25,
            "guidance_scale": asset_request.guidance_scale or 4.0,
            "seed": asset_request.seed,
            "negative_prompt": asset_request.negative_prompt,
            "metadata": asset_request.metadata,
        }
        generated = client.post("/assets/generate", json=payload)
        generated.raise_for_status()
        generated_assets.append(generated.json())

    inferred_requests = infer_missing_asset_requests(
        node=applied["node"],
        explicit_requests=[request.model_dump(mode="json") for request in candidate.asset_requests],
        project_root=project_root,
        client=client,
    )
    for payload in inferred_requests:
        generated = client.post("/assets/generate", json=payload)
        generated.raise_for_status()
        inferred_assets.append(generated.json())

    asset_outputs = [
        {
            "source": "explicit",
            "asset_kind": asset.get("asset_kind"),
            "entity_type": asset.get("entity_type"),
            "entity_id": asset.get("entity_id"),
            "status": asset.get("status"),
            "file_path": asset.get("file_path"),
            "public_url": asset.get("public_url"),
        }
        for asset in generated_assets
    ] + [
        {
            "source": "inferred",
            "asset_kind": asset.get("asset_kind"),
            "entity_type": asset.get("entity_type"),
            "entity_id": asset.get("entity_id"),
            "status": asset.get("status"),
            "file_path": asset.get("file_path"),
            "public_url": asset.get("public_url"),
        }
        for asset in inferred_assets
    ]

    return {
        "run_mode": "normal",
        "dry_run": False,
        "pre_change_url": packet["pre_change_url"],
        "expanded_choice_id": packet["selected_frontier_item"]["choice_id"],
        "expanded_choice_text": packet["selected_frontier_item"]["choice_text"],
        "new_node_id": applied["node"]["id"],
        "new_node_title": applied["node"]["title"],
        "created_choice_ids": [choice["id"] for choice in applied["created_choices"]],
        "generated_asset_count": len(generated_assets) + len(inferred_assets),
        "explicit_asset_count": len(generated_assets),
        "inferred_asset_count": len(inferred_assets),
        "generated_assets": asset_outputs,
        "hooks_added": len(candidate.new_hooks),
        "global_direction_notes_added": len(candidate.global_direction_notes),
    }


def apply_revival_result(
    *,
    packet: dict[str, Any],
    result: RevivalChoiceResult,
    client: TestClient,
    dry_run: bool,
) -> dict[str, Any]:
    revival_context = packet.get("revival_context") or {}
    parent_node_id = int(revival_context["parent_node_id"])
    traversed_choice_id = int(revival_context["traversed_choice_id"])
    parent_total_choice_count = int(revival_context.get("parent_total_choice_count") or 0)
    max_choices_per_node = int(revival_context.get("max_choices_per_node") or 5)
    notes_payload = {
        "notes": result.notes,
        "choice_class": result.choice_class,
        "ending_category": result.ending_category,
    }
    stored_notes = json.dumps({key: value for key, value in notes_payload.items() if value is not None})

    if dry_run:
        action = "append" if parent_total_choice_count < max_choices_per_node else "replace"
        return {
            "run_mode": "revival",
            "dry_run": True,
            "parent_node_id": parent_node_id,
            "traversed_choice_id": traversed_choice_id,
            "revival_action": action,
            "new_choice_text": result.choice_text,
        }

    if parent_total_choice_count < max_choices_per_node:
        response = client.post(
            "/choices",
            json={
                "from_node_id": parent_node_id,
                "choice_text": result.choice_text,
                "status": "open",
                "notes": stored_notes,
            },
        )
        response.raise_for_status()
        created_choice = response.json()
        action = "append"
    else:
        response = client.post(
            f"/choices/{traversed_choice_id}/replace",
            json={
                "choice_text": result.choice_text,
                "status": "open",
                "notes": stored_notes,
                "to_node_id": None,
            },
        )
        response.raise_for_status()
        created_choice = response.json()
        action = "replace"

    return {
        "run_mode": "revival",
        "dry_run": False,
        "parent_node_id": parent_node_id,
        "traversed_choice_id": traversed_choice_id,
        "revival_action": action,
        "revived_choice_id": created_choice["id"],
        "new_choice_text": created_choice["choice_text"],
    }


def get_default_log_path(project_root: Path) -> Path:
    return project_root / "data" / "worker_logs" / "local_worker_runs.ndjson"


def get_default_validation_attempt_log_path(project_root: Path) -> Path:
    return project_root / "data" / "worker_logs" / "validation_attempts.md"


def append_run_log_record(*, log_path: Path, record: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def ensure_validation_attempt_log_capacity(*, log_path: Path, max_lines: int = 1000) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        with log_path.open("r", encoding="utf-8") as handle:
            line_count = sum(1 for _ in handle)
        if line_count >= max_lines:
            log_path.write_text("", encoding="utf-8")


def append_validation_attempt_run_separator(*, log_path: Path, record: dict[str, Any], max_lines: int = 1000) -> None:
    ensure_validation_attempt_log_capacity(log_path=log_path, max_lines=max_lines)
    metadata_bits = [str(record.get("timestamp") or "").strip(), str(record.get("model") or "").strip()]
    run_mode = str(record.get("run_mode") or "").strip()
    if run_mode:
        metadata_bits.append(f"run_mode={run_mode}")
    choice_id = record.get("choice_id")
    if choice_id is not None:
        metadata_bits.append(f"choice_id={choice_id}")
    separator = "-" * 72
    entry = (
        f"{separator}\n"
        f"{' | '.join(bit for bit in metadata_bits if bit)}\n"
        f"{separator}\n\n"
    )
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(entry)


def append_validation_attempt_log_record(*, log_path: Path, record: dict[str, Any], max_lines: int = 1000) -> None:
    ensure_validation_attempt_log_capacity(log_path=log_path, max_lines=max_lines)
    attempted_output = str(record.get("attempted_output") or "")
    formatted_output = attempted_output.replace("\\r\\n", "\n").replace("\\n", "\n")
    issues = [str(issue) for issue in (record.get("issues") or [])]
    metadata_bits = [str(record.get("timestamp") or "").strip(), str(record.get("model") or "").strip()]
    step = str(record.get("step") or "").strip()
    if step:
        metadata_bits.append(f"step={step}")
    retry_index = record.get("retry_index")
    if retry_index is not None:
        metadata_bits.append(f"retry={retry_index}")
    choice_id = record.get("choice_id")
    if choice_id is not None:
        metadata_bits.append(f"choice_id={choice_id}")
    choice_index = record.get("choice_index")
    if choice_index is not None:
        metadata_bits.append(f"choice_index={choice_index}")
    detail_target = record.get("detail_target") or {}
    detail_name = str(detail_target.get("name") or "").strip()
    detail_type = str(detail_target.get("type") or "").strip()
    if detail_name:
        metadata_bits.append(f"detail_target={detail_type}:{detail_name}")
    metadata_line = " | ".join(bit for bit in metadata_bits if bit)
    issue_lines = "\n".join(f"- {issue}" for issue in issues) if issues else "- (none)"
    entry = (
        f"{metadata_line}\n"
        "issues:\n"
        f"{issue_lines}\n"
        "attempted output:\n"
        "```text\n"
        f"{formatted_output}\n"
        "```\n\n\n\n"
    )
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(entry)


def build_log_timestamps() -> dict[str, str]:
    now = datetime.now().astimezone()
    return {
        "timestamp": now.strftime("%Y-%m-%d %I:%M:%S %p"),
    }


def append_run_started_log(
    *,
    log_path: Path,
    args: argparse.Namespace,
    packet: dict[str, Any],
) -> None:
    timestamps = build_log_timestamps()
    append_run_log_record(
        log_path=log_path,
        record={
            **timestamps,
            "status": "started",
            "model": args.model,
            "run_mode": packet.get("run_mode"),
            "dry_run": args.dry_run,
            "pre_change_url": packet.get("pre_change_url"),
            "choice_id": (packet.get("selected_frontier_item") or {}).get("choice_id"),
            "planning_reason": packet.get("planning_reason"),
        },
    )


def append_run_finished_log(
    *,
    log_path: Path,
    args: argparse.Namespace,
    result: dict[str, Any],
) -> None:
    timestamps = build_log_timestamps()
    append_run_log_record(
        log_path=log_path,
        record={
            **timestamps,
            "status": "succeeded",
            "model": args.model,
            "run_mode": result.get("run_mode"),
            "dry_run": result.get("dry_run"),
            "result": result,
        },
    )


def append_run_failed_log(
    *,
    log_path: Path,
    args: argparse.Namespace,
    packet: dict[str, Any] | None,
    error: BaseException | str,
) -> None:
    timestamps = build_log_timestamps()
    append_run_log_record(
        log_path=log_path,
        record={
            **timestamps,
            "status": "failed",
            "model": args.model,
            "run_mode": packet.get("run_mode") if packet else None,
            "dry_run": args.dry_run,
            "pre_change_url": packet.get("pre_change_url") if packet else None,
            "choice_id": ((packet or {}).get("selected_frontier_item") or {}).get("choice_id"),
            "planning_reason": (packet or {}).get("planning_reason"),
            "error": str(error),
        },
    )


def infer_missing_asset_requests(
    *,
    node: dict[str, Any],
    explicit_requests: list[dict[str, Any]],
    project_root: Path,
    client: TestClient,
) -> list[dict[str, Any]]:
    explicit_pairs = {
        (
            request.get("asset_kind"),
            request.get("entity_type"),
            int(request["entity_id"]),
        )
        for request in explicit_requests
        if request.get("asset_kind") and request.get("entity_type") and request.get("entity_id") is not None
    }

    app = cast(FastAPI, client.app)
    settings = app.state.settings
    from app.database import connect

    connection = connect(settings.database_path)
    try:
        assets = AssetService(connection, project_root)
        canon = CanonResolver(connection)
        inferred: list[dict[str, Any]] = []

        current_scene = next(
            (entity for entity in node.get("entities", []) if entity.get("role") == "current_scene" and entity.get("entity_type") == "location"),
            None,
        )
        if current_scene is not None:
            location_id = int(current_scene["entity_id"])
            if ("background", "location", location_id) not in explicit_pairs:
                background = assets.get_latest_asset(
                    entity_type="location",
                    entity_id=location_id,
                    asset_kind="background",
                )
                if background is None:
                    location = canon.get_location(location_id)
                    if location is not None:
                        inferred.append(
                            {
                                "asset_kind": "background",
                                "entity_type": "location",
                                "entity_id": location_id,
                                "prompt": build_asset_prompt(
                                    entity_type="location",
                                    entity=location,
                                    scene_summary=node.get("summary"),
                                    scene_text=node.get("scene_text"),
                                ),
                                "width": 1600,
                                "height": 896,
                                "metadata": {"source": "inferred_post_apply"},
                            }
                        )

        for present in node.get("present_entities", []):
            entity_type = present.get("entity_type")
            if entity_type not in {"character", "object"}:
                continue
            entity_id = int(present["entity_id"])
            preferred_kind = "portrait" if entity_type == "character" else "object_render"
            if (preferred_kind, entity_type, entity_id) in explicit_pairs:
                continue
            preferred_asset = assets.get_preferred_asset(
                entity_type=entity_type,
                entity_id=entity_id,
                preferred_kinds=["cutout", preferred_kind],
            )
            if preferred_asset is not None:
                continue
            if entity_type == "character":
                record = canon.get_character(entity_id)
                width, height = 1024, 1536
            else:
                record = canon.get_object(entity_id)
                width, height = 1024, 1024
            if record is None:
                continue
            inferred.append(
                {
                    "asset_kind": preferred_kind,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "prompt": build_asset_prompt(
                        entity_type=entity_type,
                        entity=record,
                        scene_summary=node.get("summary"),
                        scene_text=node.get("scene_text"),
                    ),
                    "width": width,
                    "height": height,
                    "metadata": {"source": "inferred_post_apply"},
                }
            )

        return inferred
    finally:
        connection.close()


def build_asset_prompt(
    *,
    entity_type: str,
    entity: dict[str, Any],
    scene_summary: str | None,
    scene_text: str | None,
) -> str:
    name = (entity.get("name") or "").strip()
    description = (entity.get("description") or "").strip()
    canonical_summary = (entity.get("canonical_summary") or "").strip()
    if entity_type == "location":
        fragments = [fragment for fragment in [description, canonical_summary] if fragment]
        joined = ". ".join(fragments[:2]).strip()
        suffix = "Static environment only. No characters. No separately rendered props or vehicles."
        return " ".join(part for part in [name + ".", joined, suffix] if part).strip()
    scene_hint = (scene_summary or scene_text or "").strip()
    fragments = [fragment for fragment in [description, canonical_summary, scene_hint] if fragment]
    joined = ". ".join(fragments[:3]).strip()
    if entity_type == "character":
        return f"Full-body portrait of {name}. {joined}".strip()
    return f"{name}. {joined}".strip()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    log_path = Path(args.log_file) if args.log_file else get_default_log_path(project_root)
    packet: dict[str, Any] | None = None
    normal_session_path: Path | None = None
    try:
        packet = run_prepare_story_run(args, project_root)
        append_validation_attempt_run_separator(
            log_path=get_default_validation_attempt_log_path(project_root),
            record={
                **build_log_timestamps(),
                "model": args.model,
                "run_mode": packet.get("run_mode"),
                "choice_id": ((packet.get("selected_frontier_item") or {}).get("choice_id")),
            },
        )
        append_run_started_log(log_path=log_path, args=args, packet=packet)
        worker_guide = load_worker_guide(project_root)
        system_prompt = build_system_prompt(worker_guide)
        last_error: Exception | None = None
        parsed: GenerationCandidate | PlanningFollowthroughResult | RevivalChoiceResult | None = None
        planning_ideas: list[PlanningIdea] = []
        validated_payload: dict[str, Any] | None = None
        normal_state: NormalRunConversationState | None = None
        normal_resolution: dict[str, Any] | None = None

        with get_test_client() as client:
            if packet["run_mode"] != "normal" and args.author_mode != "ai":
                raise RuntimeError("--author-mode human is only supported for normal runs.")
            if packet["run_mode"] == "planning":
                idea_system_prompt = build_planning_ideas_system_prompt()
                idea_user_prompt = build_planning_ideas_user_prompt(packet)
                idea_response_format = build_planning_ideas_response_format()
                idea_batch: PlanningIdeasResult | None = None

                for _ in range(max(args.max_retries, 1)):
                    raw = ""
                    try:
                        raw = call_lm_studio(
                            api_base=args.api_base,
                            model=args.model,
                            system_prompt=idea_system_prompt,
                            user_prompt=idea_user_prompt,
                            response_format=idea_response_format,
                            temperature=args.temperature,
                            max_tokens=args.max_tokens,
                            request_timeout=args.request_timeout,
                        )
                        idea_batch = parse_planning_ideas_result(raw)
                        idea_issues = get_planning_idea_issues(packet, idea_batch.ideas_to_append)
                        if not idea_issues:
                            planning_ideas = idea_batch.ideas_to_append
                            break
                        last_error = RuntimeError("Planning idea validation failed: " + json.dumps(idea_issues, indent=2))
                        idea_user_prompt = build_planning_ideas_retry_user_prompt(
                            packet=packet,
                            previous_result=idea_batch,
                            issues=idea_issues,
                        )
                    except ValidationError as exc:
                        last_error = exc
                        issue_lines = [
                            " -> ".join(str(part) for part in error.get("loc", [])) + f": {error.get('msg')}"
                            for error in exc.errors()
                        ] or [str(exc)]
                        if raw.strip():
                            idea_user_prompt = (
                                "Your previous fresh-idea response did not match the required JSON/schema.\n"
                                "Fix the listed schema issues and return a corrected JSON object only.\n\n"
                                f"Schema issues:\n{json.dumps(issue_lines, indent=2)}\n\n"
                                f"Previous invalid response:\n{raw.strip()}"
                            )
                    except json.JSONDecodeError as exc:
                        last_error = exc
                        if raw.strip():
                            idea_user_prompt = (
                                "Your previous fresh-idea response did not parse as JSON.\n"
                                "Fix it and return only valid JSON.\n\n"
                                f"JSON issue:\n{json.dumps([str(exc)], indent=2)}\n\n"
                                f"Previous invalid response:\n{raw.strip()}"
                            )
                    except (httpx.HTTPError, RuntimeError) as exc:
                        last_error = exc

                if not planning_ideas:
                    raise SystemExit(f"Local worker failed: {last_error}")

                user_prompt = build_planning_followthrough_user_prompt(
                    packet=packet,
                    ideas=planning_ideas,
                )
                response_format = build_response_format(packet)

                for _ in range(max(args.max_retries, 1)):
                    raw = ""
                    try:
                        raw = call_lm_studio(
                            api_base=args.api_base,
                            model=args.model,
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            response_format=response_format,
                            temperature=args.temperature,
                            max_tokens=args.max_tokens,
                            request_timeout=args.request_timeout,
                        )
                        parsed = parse_llm_result(packet, raw)
                        assert isinstance(parsed, PlanningFollowthroughResult)
                        planning_issues = get_planning_validation_issues(packet, parsed, planning_ideas)
                        if not planning_issues:
                            break
                        last_error = RuntimeError("Planning validation failed: " + json.dumps(planning_issues, indent=2))
                        user_prompt = build_planning_retry_user_prompt(
                            packet=packet,
                            ideas=planning_ideas,
                            previous_result=parsed,
                            issues=planning_issues,
                        )
                    except ValidationError as exc:
                        last_error = exc
                        issue_lines = [
                            " -> ".join(str(part) for part in error.get("loc", [])) + f": {error.get('msg')}"
                            for error in exc.errors()
                        ] or [str(exc)]
                        if raw.strip():
                            user_prompt = build_schema_retry_user_prompt(
                                packet=packet,
                                raw_text=raw,
                                issues=issue_lines,
                            )
                    except json.JSONDecodeError as exc:
                        last_error = exc
                        if raw.strip():
                            user_prompt = build_schema_retry_user_prompt(
                                packet=packet,
                                raw_text=raw,
                                issues=[str(exc)],
                            )
                    except (httpx.HTTPError, RuntimeError) as exc:
                        last_error = exc
            elif packet["run_mode"] == "revival":
                user_prompt = build_user_prompt(packet)
                response_format = build_response_format(packet)
                for _ in range(max(args.max_retries, 1)):
                    raw = ""
                    try:
                        raw = call_lm_studio(
                            api_base=args.api_base,
                            model=args.model,
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            response_format=response_format,
                            temperature=args.temperature,
                            max_tokens=args.max_tokens,
                            request_timeout=args.request_timeout,
                        )
                        parsed = parse_llm_result(packet, raw)
                        assert isinstance(parsed, RevivalChoiceResult)
                        revival_issues = get_revival_validation_issues(packet, parsed)
                        if not revival_issues:
                            break
                        last_error = RuntimeError("Revival validation failed: " + json.dumps(revival_issues, indent=2))
                        user_prompt = (
                            "Your previous revival choice failed validation.\n"
                            "Fix the listed issues and return one corrected revival-choice JSON object only.\n\n"
                            f"Issues:\n{json.dumps(revival_issues, indent=2)}\n\n"
                            f"Previous invalid result:\n{parsed.model_dump_json(indent=2)}"
                        )
                    except ValidationError as exc:
                        last_error = exc
                        issue_lines = [
                            " -> ".join(str(part) for part in error.get("loc", [])) + f": {error.get('msg')}"
                            for error in exc.errors()
                        ] or [str(exc)]
                        if raw.strip():
                            user_prompt = build_schema_retry_user_prompt(
                                packet=packet,
                                raw_text=raw,
                                issues=issue_lines,
                            )
                    except json.JSONDecodeError as exc:
                        last_error = exc
                        if raw.strip():
                            user_prompt = build_schema_retry_user_prompt(
                                packet=packet,
                                raw_text=raw,
                                issues=[str(exc)],
                            )
                    except (httpx.HTTPError, RuntimeError) as exc:
                        last_error = exc
            else:
                if packet["run_mode"] != "normal":
                    raise RuntimeError("Unexpected non-normal packet in normal-mode branch.")
                parsed, validated_payload, normal_session_path, normal_state, normal_resolution = run_normal_conversational_builder(
                    packet=packet,
                    args=args,
                    worker_guide=worker_guide,
                    project_root=project_root,
                    client=client,
                )
            if parsed is None:
                raise SystemExit(f"Local worker failed: {last_error}")

            if packet["run_mode"] == "planning":
                ideas_path = Path(args.ideas_file) if args.ideas_file else project_root / "IDEAS.md"
                result = apply_planning_result(
                    packet=packet,
                    ideas=planning_ideas,
                    result=parsed,
                    client=client,
                    ideas_path=ideas_path,
                    dry_run=args.dry_run,
                )
            elif packet["run_mode"] == "revival":
                assert isinstance(parsed, RevivalChoiceResult)
                result = apply_revival_result(
                    packet=packet,
                    result=parsed,
                    client=client,
                    dry_run=args.dry_run,
                )
            else:
                assert isinstance(parsed, GenerationCandidate)
                result = apply_normal_result(
                    packet=packet,
                    candidate=parsed,
                    client=client,
                    dry_run=args.dry_run,
                    project_root=project_root,
                    state=normal_state,
                    resolution=normal_resolution,
                    validation_payload=validated_payload,
                )

        append_run_finished_log(log_path=log_path, args=args, result=result)
        if packet and packet.get("run_mode") == "normal" and normal_session_path is not None:
            session = read_normal_session(normal_session_path)
            if session is not None:
                if result.get("dry_run"):
                    outcome_summary = "The previous normal run succeeded as a dry run. Nothing changed in canon."
                else:
                    outcome_summary = (
                        "The previous normal run succeeded and became canon. "
                        f"Accepted scene: {result.get('new_node_title') or result.get('run_mode')} "
                        f"(node {result.get('new_node_id', 'unknown')})."
                    )
                finalize_normal_session(
                    session=session,
                    session_path=normal_session_path,
                    outcome_summary=outcome_summary,
                )
        print(json.dumps(result, indent=2))
    except BaseException as exc:
        if packet and packet.get("run_mode") == "normal" and normal_session_path is not None:
            session = read_normal_session(normal_session_path)
            if session is not None:
                finalize_normal_session(
                    session=session,
                    session_path=normal_session_path,
                    outcome_summary="The previous normal run failed and nothing from it became canon.",
                )
        if not args.dry_run and packet and packet.get("run_mode") == "normal":
            choice_id = ((packet.get("selected_frontier_item") or {}).get("choice_id"))
            if choice_id is not None:
                try:
                    settings = Settings.from_env()
                    with connect(settings.database_path) as connection:
                        story = StoryGraphService(connection)
                        story.record_choice_worker_failure(
                            choice_id=int(choice_id),
                            error=str(exc),
                            auto_park_threshold=5,
                        )
                except Exception:
                    pass
        append_run_failed_log(log_path=log_path, args=args, packet=packet, error=exc)
        raise


if __name__ == "__main__":
    main()
