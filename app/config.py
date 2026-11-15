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


WEATHER_VARIABLES = {"tmax", "tmin", "precip_sum", "rain_sum", "snowfall_sum",
                     "wind_gusts_max", "wind_speed_max", "weather_code"}


class Condition(BaseModel):
    """One test on a daily value: `variable op threshold`."""
    model_config = ConfigDict(extra="forbid")
    variable: str
    op: Literal[">=", "<=", "<", "in"]
    threshold: float | list[int]

    @model_validator(mode="after")
    def check(self):
        if self.variable not in WEATHER_VARIABLES:
            raise ValueError(f"unknown weather variable '{self.variable}'")
        if (self.op == "in") != isinstance(self.threshold, list):
            raise ValueError(f"{self.variable}: op 'in' needs a list threshold, other ops a number")
        return self


class WeatherRule(BaseModel):
    """A daily trigger: if ALL conditions hold, the day loses `severity` of a day.

    Single-condition rules can be written inline (variable/op/threshold) for readability.
    """
    model_config = ConfigDict(extra="forbid")
    hazard: str
    conditions: list[Condition] = []
    variable: str | None = None
    op: Literal[">=", "<=", "<", "in"] | None = None
    threshold: float | list[int] | None = None
    severity: float = Field(gt=0, le=1)
    recovery_days: int = Field(ge=0)
    label: str

    @model_validator(mode="after")
    def normalise(self):
        if self.variable is not None:
            self.conditions = [Condition(variable=self.variable, op=self.op, threshold=self.threshold),
                               *self.conditions]
        if not self.conditions:
            raise ValueError(f"rule '{self.label}' has no conditions")
        return self


class DeclarationRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hazard: str
    incident_types: list[str]
    days_per_event: float = Field(gt=0)
    label: str


class Tier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    min: float = Field(ge=0)


class IndexConfig(BaseModel):
    """Standardised index: 100 = reference_days (a typical hub's total); tiers on the index."""
    model_config = ConfigDict(extra="forbid")
    reference_days: float = Field(gt=0)
    tiers: list[Tier]

    @model_validator(mode="after")
    def check(self):
        mins = [t.min for t in self.tiers]
        if not mins or mins[0] != 0 or mins != sorted(mins) or len(set(mins)) != len(mins):
            raise ValueError("index tiers must start at 0 and have strictly increasing lower bounds")
        return self

    def index(self, days: float) -> float:
        return round(100 * days / self.reference_days, 1)

    def tier(self, index: float) -> str:
        return [t.name for t in self.tiers if index >= t.min][-1]


class DisruptionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_severity: float = Field(gt=0, le=1)
    hazards: dict[str, str]
    weather_rules: list[WeatherRule]
    declaration_types_counted: list[str]
    declaration_window_years: int = Field(gt=0)
    declaration_rules: list[DeclarationRule]
    declaration_dedup_window_days: int = Field(ge=0)
    min_weather_coverage: float = Field(gt=0, le=1)
    index: IndexConfig

    @model_validator(mode="after")
    def check(self):
        for r in [*self.weather_rules, *self.declaration_rules]:
            if r.hazard not in self.hazards:
                raise ValueError(f"rule '{r.label}' uses unknown hazard '{r.hazard}'")
        types = [t for r in self.declaration_rules for t in r.incident_types]
        if len(types) != len(set(types)):
            raise ValueError("an incident type is mapped to more than one declaration rule")
        return self


@lru_cache
def load_hubs() -> list[Hub]:
    return HubsFile.model_validate(yaml.safe_load(settings.hubs_file.read_text(encoding="utf-8"))).hubs


@lru_cache
def load_scoring() -> DisruptionConfig:
    return DisruptionConfig.model_validate(yaml.safe_load(settings.scoring_file.read_text(encoding="utf-8")))


def scoring_version() -> str:
    """Short hash of the scoring config, stored with every score so results are reproducible."""
    return hashlib.sha256(settings.scoring_file.read_bytes()).hexdigest()[:10]
