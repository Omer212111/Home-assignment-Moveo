"""Typed loaders for the YAML config files.

These are Pydantic models, so a typo or a missing field in hubs.yaml or scoring.yaml
fails at startup with a clear message instead of in the middle of a demo.
"""
import hashlib
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.settings import settings

Region = Literal["Northeast", "Midwest", "South", "West"]


class Hub(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    city: str
    state: str = Field(min_length=2, max_length=2)
    lat: float = Field(ge=18, le=72)
    lon: float = Field(ge=-170, le=-60)
    county_fips: str = Field(pattern=r"^\d{5}$")
    region: Region
    pop_rank: int


class HubsFile(BaseModel):
    hubs: list[Hub]

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [h.id for h in self.hubs]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate hub ids: {dupes}")
        return self


class Component(BaseModel):
    """One input to a hazard sub-score, normalised as min(value / cap, 1) * 100."""
    model_config = ConfigDict(extra="forbid")
    name: str
    source: Literal["metric", "declarations"]
    cap: float = Field(gt=0)
    weight: float = Field(gt=0)
    description: str


class Hazard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    weight: float = Field(ge=0)
    components: list[Component]


class Tier(BaseModel):
    name: str
    min_score: float


class ScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thresholds: dict[str, float]
    declaration_incident_types: dict[str, list[str]]
    declaration_types_counted: list[str]
    hazards: dict[str, Hazard]
    tiers: list[Tier]

    @model_validator(mode="after")
    def check(self):
        total = sum(h.weight for h in self.hazards.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"hazard weights must sum to 1, got {total}")
        for key, h in self.hazards.items():
            for c in h.components:
                if c.source == "declarations" and c.name not in self.declaration_incident_types:
                    raise ValueError(f"{key}: no incident-type mapping for declarations component '{c.name}'")
        self.tiers.sort(key=lambda t: t.min_score, reverse=True)
        return self


@lru_cache
def load_hubs() -> list[Hub]:
    return HubsFile.model_validate(yaml.safe_load(settings.hubs_file.read_text(encoding="utf-8"))).hubs


@lru_cache
def load_scoring() -> ScoringConfig:
    return ScoringConfig.model_validate(yaml.safe_load(settings.scoring_file.read_text(encoding="utf-8")))


def scoring_version() -> str:
    """Short hash of scoring.yaml, stored with every score so results are reproducible."""
    return hashlib.sha256(settings.scoring_file.read_bytes()).hexdigest()[:10]
