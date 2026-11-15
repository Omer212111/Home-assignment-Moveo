"""The JSON contracts between the LLM and our code (Pydantic is the single source of truth).

- Tool inputs: the LLM must call tools with arguments that match these models
  (sent as JSON schema with strict: true, and validated again before running).
- AgentAnswer: the LLM's final answer must match this model
  (sent as the structured-output format, and validated again with Pydantic).

All fields are required (nullable where a value can be absent) so the schemas
work with strict mode.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import load_hubs

Hazard = Literal["total", "winter", "flood", "high_wind", "heat", "tropical", "severe_storm"]
SingleHazard = Literal["winter", "flood", "high_wind", "heat", "tropical", "severe_storm"]
Region = Literal["Northeast", "Midwest", "South", "West"]
# The 50 hub ids from config/hubs.yaml, as an enum: a city that is not a hub ("paris") is rejected
# by the schema itself, and the model must map "Miami" -> "miami", "Arlington" -> "arlington_tx".
HubId = Literal[tuple(h.id for h in load_hubs())]


# ---------------------------------------------------------------- tool inputs

class RankHubsInput(BaseModel):
    """Rank hubs by expected disruption days per year."""
    model_config = ConfigDict(extra="forbid")
    hazard: Hazard = Field(description='"total" for all hazards combined, or one hazard')
    region: Region | None = Field(description="US Census region filter, or null for all hubs")
    top_n: int = Field(ge=1, le=50, description="number of hubs to return")


class ExplainHubInput(BaseModel):
    """Explain one hub's score: rank, per-hazard split, drivers, counted FEMA disasters."""
    model_config = ConfigDict(extra="forbid")
    hub_id: HubId = Field(description="hub id, e.g. dallas")


class CompareHubsInput(BaseModel):
    """Compare several hubs side by side, optionally on selected hazards."""
    model_config = ConfigDict(extra="forbid")
    hub_ids: list[HubId] = Field(min_length=2, max_length=5, description="2 to 5 hub ids")
    hazards: list[SingleHazard] | None = Field(
        description="hazards to compare (e.g. tropical and flood), or null for every hazard")


# ---------------------------------------------------------------- final answer

class RankedHub(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: int
    hub_id: str
    city: str
    days_per_year: float
    main_hazard: str = Field(description="hazard contributing the most disruption days")


class ModelAnswer(BaseModel):
    """What the LLM writes (the structured-output schema): the explanation, never the data."""
    model_config = ConfigDict(extra="forbid")
    answer_markdown: str = Field(description="the answer for the analyst, in Markdown")
    assumptions: list[str] = Field(description="assumptions the answer depends on")
    confidence: Literal["low", "medium", "high"]
    uncertainty_notes: str = Field(description="what could make this answer wrong")
    data_sources: list[str] = Field(description="sources and periods behind the numbers")


class AgentAnswer(ModelAnswer):
    """What the API returns: the model's answer plus the ranking, attached by the server from the
    engine's result. The model never re-types the ranking: that was slow (about 1,850 output
    tokens for 50 hubs) and a source of copy errors."""
    ranking: list[RankedHub] | None = Field(
        default=None, description="ranking from the last rank_hubs call (added by the server), or null")
