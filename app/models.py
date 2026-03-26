from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


EntityType = Literal["world", "location", "character", "object"]
RelationEntityType = Literal["location", "character", "object"]


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
    entity_id: int
    role: str = "mentioned"


class StoryNodeCreate(BaseModel):
    branch_key: str = "default"
    title: str | None = None
    scene_text: str
    summary: str | None = None
    parent_node_id: int | None = None
    referenced_entities: list[EntityReference] = Field(default_factory=list)


class ChoiceCreate(BaseModel):
    from_node_id: int
    choice_text: str
    to_node_id: int | None = None
    status: str = "open"
    notes: str | None = None


class GenerationPayload(BaseModel):
    branch_key: str = "default"
    open_hooks: list[str] = Field(default_factory=list)
    focus_entity_ids: list[int] = Field(default_factory=list)


AssetJobType = Literal["generate_background", "generate_portrait", "generate_object", "remove_background"]
AssetKind = Literal["background", "portrait", "object_render", "cutout"]
AssetEntityType = Literal["location", "character", "object"]


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


class BackgroundRemovalRequest(BaseModel):
    source_image_path: str
    output_name: str | None = None
    entity_type: AssetEntityType | None = None
    entity_id: int | None = None
    model_repo: str = "briaai/RMBG-2.0"
    device: Literal["auto", "cpu", "cuda"] = "auto"
