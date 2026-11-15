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

Hazard = Literal["total", "winter", "flood", "high_wind", "heat", "tropical", "severe_storm"]
Region = Literal["Northeast", "Midwest", "South", "West"]


# ---------------------------------------------------------------- tool inputs

class RankHubsInput(BaseModel):
    """Rank hubs by expected disruption days per year."""
    model_config = ConfigDict(extra="forbid")
    hazard: Hazard = Field(description='"total" for all hazards combined, or one hazard')
    region: Region | None = Field(description="US Census region filter, or null for all hubs")
    top_n: int = Field(ge=1, le=50, description="number of hubs to return")


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
