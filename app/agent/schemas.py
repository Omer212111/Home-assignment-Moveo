"""The JSON contracts between the LLM and our code (Pydantic is the single source of truth).

- Tool inputs: the LLM must call tools with arguments that match these models
  (sent as JSON schema with strict: true, and validated again before running).
- AgentAnswer: the LLM's final answer must match this model
  (sent as the structured-output format, and validated again with Pydantic).

All fields are required (nullable where a value can be absent) so the schemas
work with strict mode.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import load_hubs
from app.settings import settings

Hazard = Literal["total", "winter", "flood", "high_wind", "heat", "tropical", "severe_storm"]
SingleHazard = Literal["winter", "flood", "high_wind", "heat", "tropical", "severe_storm"]
Region = Literal["Northeast", "Midwest", "South", "West"]
# The 50 hub ids from config/hubs.yaml, as an enum: a city that is not a hub ("paris") is rejected
# by the schema itself, and the model must map "Miami" -> "miami", "Arlington" -> "arlington_tx".
HubId = Literal[tuple(h.id for h in load_hubs())]
Season = Literal["winter", "spring", "summer", "autumn"]
Month = Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
# Calendar years inside the stored weather window (2021..2025).
Year = Literal[tuple(range(int(settings.history_start[:4]), int(settings.history_end[:4]) + 1))]

SEASON_DESC = ('a SEASON of the year (time period, all hazards unless a hazard is given): winter = Dec-Feb, '
               'spring = Mar-May, summer = Jun-Aug, autumn = Sep-Nov; null for the whole year. Not the same '
               'as the hazard "winter" (snow and ice)')
MONTHS_DESC = 'specific months 1-12 instead of a season, e.g. hurricane season = [6,7,8,9,10,11]; null otherwise'


class _Period(BaseModel):
    """A time period inside the year: a season or a list of months (not both), or null for all year."""
    model_config = ConfigDict(extra="forbid")
    season: Season | None = Field(description=SEASON_DESC)
    months: list[Month] | None = Field(description=MONTHS_DESC)

    @model_validator(mode="after")
    def _one_period(self):
        if self.season and self.months:
            raise ValueError("give either season or months, not both")
        return self


# ---------------------------------------------------------------- tool inputs

class RankHubsInput(_Period):
    """Rank hubs by expected disruption days per year, optionally within a season or months."""
    hazard: Hazard = Field(description='"total" for all hazards combined, or one hazard')
    region: Region | None = Field(description="US Census region filter, or null for all hubs")
    top_n: int = Field(ge=1, le=50, description="number of hubs to return")
    order: Literal["most", "least"] = Field(description='"most" exposed first, or "least" (safest hubs) first')


class ExplainHubInput(BaseModel):
    """Explain one hub's score: rank, per-hazard split, drivers, counted FEMA disasters."""
    model_config = ConfigDict(extra="forbid")
    hub_id: HubId = Field(description="hub id, e.g. dallas")


class CompareHubsInput(_Period):
    """Compare several hubs side by side, optionally on selected hazards and within a period."""
    hub_ids: list[HubId] = Field(min_length=2, max_length=5, description="2 to 5 hub ids")
    hazards: list[SingleHazard] | None = Field(
        description="hazards to compare (e.g. tropical and flood), or null for every hazard")


class HubBreakdownInput(BaseModel):
    """Split hubs' disruption days by month, season or year."""
    model_config = ConfigDict(extra="forbid")
    hub_ids: list[HubId] = Field(min_length=1, max_length=5, description="1 to 5 hub ids")
    hazard: Hazard = Field(description='"total" for all hazards, or one hazard')
    by: Literal["month", "season", "year"] = Field(
        description="month or season: average days per year in each period; year: actual days in each calendar year")


class WeatherStatInput(_Period):
    """A statistic of the raw daily weather (not disruption days) for hubs over a period."""
    hub_ids: list[HubId] = Field(min_length=1, max_length=5, description="1 to 5 hub ids")
    variable: Literal["snowfall_cm", "rain_mm", "precipitation_mm", "max_temp_c", "min_temp_c", "max_gust_kmh"] = \
        Field(description="daily weather variable")
    statistic: Literal["count_days", "sum", "mean", "max", "min"] = Field(
        description="count_days: number and percentage of days meeting the condition; sum/mean/max/min of the variable")
    condition_op: Literal[">", ">=", "<", "<="] | None = Field(
        description='for count_days: comparison, e.g. ">" with 0 for "days with any snowfall"; null otherwise')
    condition_value: float | None = Field(description="for count_days: threshold in the variable's unit; null otherwise")
    year: Year | None = Field(description=f'one calendar year; "last year" means {settings.history_end[:4]} '
                                          f'(the latest full year in the data); null for all years')

    @model_validator(mode="after")
    def _condition(self):
        if self.statistic == "count_days" and (self.condition_op is None or self.condition_value is None):
            raise ValueError("count_days needs condition_op and condition_value")
        return self


# ---------------------------------------------------------------- final answer

class RankedHub(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: int
    hub_id: str
    city: str
    days_per_year: float
    index: float = Field(description="standardised score: 100 = a typical hub")
    tier: str = Field(description="low / moderate / elevated / high / very high")
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
