from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


EntityType = Literal["world", "location", "character", "object"]
RelationEntityType = Literal["location", "character", "object"]
AssetEntityType = Literal["location", "character", "object"]
AssetJobType = Literal["generate_background", "generate_portrait", "generate_object", "remove_background"]
AssetKind = Literal["background", "portrait", "object_render", "cutout"]
BranchTagType = Literal["clue", "state", "quest", "relationship", "travel", "mystery"]
HookImportance = Literal["major", "minor", "local"]
HookStatus = Literal["active", "payoff_ready", "resolved", "blocked"]
RelationshipStance = Literal["ally", "friendly", "neutral", "wary", "hostile", "unknown"]
AffordanceStatus = Literal["unlocked", "suspended", "retired"]
InventoryStatus = Literal["owned", "stored", "spent", "lost"]
ActPhase = Literal["early", "middle", "late"]
DirectionNoteStatus = Literal["active", "parked", "resolved"]
ChoiceStatus = Literal["open", "fulfilled", "parked", "closed"]
ChoiceClass = Literal["inspection", "progress", "commitment", "ending"]
EndingCategory = Literal["death", "dead_end", "capture", "transformation", "hub_return"]
WorldbuildingStatus = Literal["active", "parked", "resolved"]


class LocationSeed(BaseModel):
    name: str
    description: str | None = None
    canonical_summary: str | None = None


class CharacterSeed(BaseModel):
    name: str
    description: str | None = None
    canonical_summary: str | None = None
    home_location_name: str | None = None


class ObjectSeed(BaseModel):
    name: str
    description: str | None = None
    canonical_summary: str | None = None
    default_location_name: str | None = None


class RelationSeed(BaseModel):
    subject_type: RelationEntityType
    subject_name: str
    relation_type: str
    object_type: RelationEntityType
    object_name: str
    notes: str | None = None


class FactSeed(BaseModel):
    entity_type: EntityType
    fact_text: str
    entity_name: str | None = None
    entity_id: int | None = None
    is_locked: bool = False
    source: str = "seed"


class WorldSeed(BaseModel):
    premise: str | None = None
    locked_rules: list[str] = Field(default_factory=list)
    locations: list[LocationSeed] = Field(default_factory=list)
    characters: list[CharacterSeed] = Field(default_factory=list)
    objects: list[ObjectSeed] = Field(default_factory=list)
    relations: list[RelationSeed] = Field(default_factory=list)
    facts: list[FactSeed] = Field(default_factory=list)


class EntityReference(BaseModel):
    entity_type: RelationEntityType
    entity_id: int = Field(ge=1)
    role: str = "mentioned"


class DialogueLine(BaseModel):
    speaker: str = "Narrator"
    text: str


class ScenePresentEntity(BaseModel):
    entity_type: RelationEntityType
    entity_id: int = Field(ge=1)
    slot: Literal[
        "hero-center",
        "left-support",
        "right-support",
        "left-foreground-object",
        "right-foreground-object",
        "center-foreground-object",
    ]
    scale: float | None = None
    offset_x_percent: float = 0.0
    offset_y_percent: float = 0.0
    focus: bool = False
    hidden_on_lines: list[int] = Field(default_factory=list)
    use_player_fallback: bool = False


class StoryNodeCreate(BaseModel):
    branch_key: str = "default"
    title: str | None = None
    scene_text: str
    summary: str | None = None
    parent_node_id: int | None = None
    dialogue_lines: list[DialogueLine] = Field(default_factory=list)
    referenced_entities: list[EntityReference] = Field(default_factory=list)
    present_entities: list[ScenePresentEntity] = Field(default_factory=list)


class ChoiceCreate(BaseModel):
    from_node_id: int
    choice_text: str
    to_node_id: int | None = None
    status: ChoiceStatus = "open"
    notes: str | None = None


class ChoiceIdeaBinding(BaseModel):
    title: str
    category: Literal["character", "location", "object", "event"]
    source: Literal["fresh", "existing"] = "fresh"
    steering_note: str | None = None


class ChoiceUpdate(BaseModel):
    notes: str = Field(
        min_length=20,
        pattern=r"^NEXT_NODE:\s*\S[\s\S]*FURTHER_GOALS:\s*\S[\s\S]*$",
    )
    idea_binding: ChoiceIdeaBinding | None = None


class ChoiceReplace(BaseModel):
    choice_text: str
    notes: str | None = None
    status: ChoiceStatus = "open"
    to_node_id: int | None = None


class BranchTagCreate(BaseModel):
    branch_key: str = "default"
    tag: str
    tag_type: BranchTagType = "state"
    source: str = "manual"
    notes: str | None = None


class InventoryEntryCreate(BaseModel):
    branch_key: str = "default"
    object_id: int
    quantity: int = 1
    status: InventoryStatus = "owned"
    source_node_id: int | None = None
    notes: str | None = None


class AffordanceCreate(BaseModel):
    branch_key: str = "default"
    name: str
    description: str
    source_object_id: int | None = None
    source_character_id: int | None = None
    availability_note: str | None = None
    required_state_tags: list[str] = Field(default_factory=list)
    status: AffordanceStatus = "unlocked"
    notes: str | None = None


class RelationshipStateCreate(BaseModel):
    branch_key: str = "default"
    character_id: int
    stance: RelationshipStance = "neutral"
    notes: str | None = None
    state_tags: list[str] = Field(default_factory=list)


class StoryHookCreate(BaseModel):
    branch_key: str = "default"
    hook_type: str
    importance: HookImportance = "minor"
    summary: str
    payoff_concept: str | None = None
    must_not_imply: list[str] = Field(default_factory=list)
    linked_entity_type: RelationEntityType | None = None
    linked_entity_id: int | None = None
    introduced_at_depth: int | None = None
    min_distance_to_payoff: int = 0
    min_distance_to_next_development: int = 0
    required_clue_tags: list[str] = Field(default_factory=list)
    required_state_tags: list[str] = Field(default_factory=list)
    status: HookStatus = "active"
    notes: str | None = None


class StoryDirectionNoteCreate(BaseModel):
    note_type: str = "plotline"
    title: str
    note_text: str
    status: DirectionNoteStatus = "active"
    priority: int = 2
    related_entity_type: RelationEntityType | None = None
    related_entity_id: int | None = None
    related_hook_id: int | None = None
    source_branch_key: str | None = None
    notes: str | None = None
    created_by: str = "manual"


class StoryDirectionNoteUpdate(BaseModel):
    note_type: str | None = None
    title: str | None = None
    note_text: str | None = None
    status: DirectionNoteStatus | None = None
    priority: int | None = None
    related_entity_type: RelationEntityType | None = None
    related_entity_id: int | None = None
    related_hook_id: int | None = None
    source_branch_key: str | None = None
    notes: str | None = None


class GenerationPayload(BaseModel):
    branch_key: str = "default"
    current_node_id: int | None = None
    choice_id: int | None = None
    focus_entity_ids: list[int] = Field(default_factory=list)
    requested_choice_count: int = 2
    branch_summary: str | None = None


class GeneratedChoice(BaseModel):
    choice_text: str
    notes: str = Field(
        min_length=20,
        pattern=r"^NEXT_NODE:\s*\S[\s\S]*FURTHER_GOALS:\s*\S[\s\S]*$",
    )
    choice_class: ChoiceClass | None = None
    ending_category: EndingCategory | None = None
    required_affordances: list[str] = Field(default_factory=list)
    target_node_id: int | None = None


class HookProposal(BaseModel):
    hook_type: str
    importance: HookImportance = "minor"
    summary: str
    payoff_concept: str | None = None
    must_not_imply: list[str] = Field(default_factory=list)
    linked_entity_type: RelationEntityType | None = None
    linked_entity_id: int | None = None
    min_distance_to_payoff: int = 0
    min_distance_to_next_development: int = 0
    required_clue_tags: list[str] = Field(default_factory=list)
    required_state_tags: list[str] = Field(default_factory=list)
    notes: str | None = None


class HookUpdate(BaseModel):
    hook_id: int
    status: HookStatus
    progress_note: str | None = None
    resolution_text: str | None = None
    next_min_distance_to_development: int | None = None
    add_required_clue_tags: list[str] = Field(default_factory=list)
    add_required_state_tags: list[str] = Field(default_factory=list)


class DirectionNoteProposal(BaseModel):
    note_type: str = "plotline"
    title: str
    note_text: str
    status: DirectionNoteStatus = "active"
    priority: int = 2
    related_entity_type: RelationEntityType | None = None
    related_entity_id: int | None = None
    related_hook_id: int | None = None
    source_branch_key: str | None = None
    notes: str | None = None


class WorldbuildingNoteCreate(BaseModel):
    note_type: str = "world_pressure"
    title: str
    note_text: str
    status: WorldbuildingStatus = "active"
    priority: int = 2
    pressure: int = 2
    source_branch_key: str | None = None
    notes: str | None = None
    created_by: str = "manual"


class WorldbuildingNoteUpdate(BaseModel):
    note_type: str | None = None
    title: str | None = None
    note_text: str | None = None
    status: WorldbuildingStatus | None = None
    priority: int | None = None
    pressure: int | None = None
    source_branch_key: str | None = None
    notes: str | None = None


class WorldbuildingNoteProposal(BaseModel):
    note_type: str = "world_pressure"
    title: str
    note_text: str
    status: WorldbuildingStatus = "active"
    priority: int = 2
    pressure: int = 2
    source_branch_key: str | None = None
    notes: str | None = None


class InventoryChange(BaseModel):
    action: Literal["add", "remove"]
    object_id: int | None = None
    object_name: str | None = None
    quantity: int = 1
    notes: str | None = None


class AffordanceChange(BaseModel):
    action: Literal["unlock", "suspend", "restore", "retire"]
    name: str
    description: str | None = None
    source_object_id: int | None = None
    source_character_id: int | None = None
    availability_note: str | None = None
    required_state_tags: list[str] = Field(default_factory=list)
    notes: str | None = None


class RelationshipUpdate(BaseModel):
    character_id: int
    stance: RelationshipStance = "neutral"
    notes: str | None = None
    state_tags: list[str] = Field(default_factory=list)


class FloatingCharacterIntroduction(BaseModel):
    character_id: int
    intro_text: str = Field(min_length=12)


class GenerationCandidate(BaseModel):
    branch_key: str = "default"
    scene_title: str | None = None
    scene_summary: str
    scene_text: str
    dialogue_lines: list[DialogueLine] = Field(default_factory=list)
    choices: list[GeneratedChoice]
    new_locations: list[LocationSeed] = Field(default_factory=list)
    new_characters: list[CharacterSeed] = Field(default_factory=list)
    new_objects: list[ObjectSeed] = Field(default_factory=list)
    floating_character_introductions: list[FloatingCharacterIntroduction] = Field(default_factory=list)
    entity_references: list[EntityReference] = Field(default_factory=list)
    scene_present_entities: list[ScenePresentEntity] = Field(default_factory=list)
    fact_updates: list[FactSeed] = Field(default_factory=list)
    relation_updates: list[RelationSeed] = Field(default_factory=list)
    new_hooks: list[HookProposal] = Field(default_factory=list)
    hook_updates: list[HookUpdate] = Field(default_factory=list)
    global_direction_notes: list[DirectionNoteProposal] = Field(default_factory=list)
    inventory_changes: list[InventoryChange] = Field(default_factory=list)
    affordance_changes: list[AffordanceChange] = Field(default_factory=list)
    relationship_changes: list[RelationshipUpdate] = Field(default_factory=list)
    asset_requests: list["AssetRequest"] = Field(default_factory=list)
    discovered_clue_tags: list[str] = Field(default_factory=list)
    discovered_state_tags: list[str] = Field(default_factory=list)


class AssetRequest(BaseModel):
    job_type: AssetJobType
    asset_kind: AssetKind
    entity_type: AssetEntityType | None = None
    entity_id: int | None = None
    model_repo: str | None = None
    prompt: str | None = None
    negative_prompt: str | None = None
    width: int | None = None
    height: int | None = None
    steps: int | None = None
    guidance_scale: float | None = None
    seed: int | None = None
    source_image_path: str | None = None
    output_name: str | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class AssetGenerateRequest(BaseModel):
    asset_kind: Literal["background", "portrait", "object_render"]
    entity_type: AssetEntityType
    entity_id: int
    prompt: str
    workflow_name: str = "text-to-image"
    filename_base: str | None = None
    negative_prompt: str | None = None
    width: int | None = None
    height: int | None = None
    steps: int = 25
    guidance_scale: float = 4.0
    seed: int | None = None
    remove_background: bool = False
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class BackgroundRemovalRequest(BaseModel):
    source_image_path: str
    output_name: str | None = None
    entity_type: AssetEntityType | None = None
    entity_id: int | None = None
    model_repo: str = "briaai/RMBG-2.0"
    device: Literal["auto", "cpu", "cuda"] = "auto"


class ApplyGenerationRequest(BaseModel):
    branch_key: str = "default"
    parent_node_id: int
    choice_id: int | None = None
    candidate: GenerationCandidate


GenerationCandidate.model_rebuild()
