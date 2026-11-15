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


class WeatherRule(BaseModel):
    """A daily trigger: if `variable op threshold`, the day loses `severity` of a day."""
    model_config = ConfigDict(extra="forbid")
    hazard: str
    variable: str
    op: Literal[">=", "<=", "in"]
    threshold: float | list[int]
    severity: float = Field(gt=0, le=1)
    recovery_days: int = Field(ge=0)
    label: str

    @model_validator(mode="after")
    def check(self):
        if self.variable not in WEATHER_VARIABLES:
            raise ValueError(f"unknown weather variable '{self.variable}'")
        if (self.op == "in") != isinstance(self.threshold, list):
            raise ValueError(f"rule '{self.label}': op 'in' needs a list threshold, other ops a number")
        return self


class DeclarationRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hazard: str
    incident_types: list[str]
    days_per_event: float = Field(gt=0)
    label: str


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
