"""OpenFEMA Disaster Declarations Summaries: federal disaster declarations per county.

Note: the API returns one row per (disaster, county, program), so rows are
de-duplicated to one per (disaster_number, county). Statewide declarations
(county code 000) are not matched to a county and are therefore ignored.
"""
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from app.sources.http_cache import cached_get_json

URL = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
FIELDS = ["disasterNumber", "declarationType", "incidentType", "declarationTitle",
          "declarationDate", "incidentBeginDate", "fipsStateCode", "fipsCountyCode"]


class FemaDeclaration(BaseModel):
    disasterNumber: int
    declarationType: str
    incidentType: str
    declarationTitle: str
    declarationDate: str
    incidentBeginDate: str | None = None
    fipsStateCode: str
    fipsCountyCode: str


def fetch_county(county_fips: str, since: str, refresh: bool = False) -> tuple[pd.DataFrame, Path, bool]:
    state, county = county_fips[:2], county_fips[2:]
    params = {
        "$filter": (f"fipsStateCode eq '{state}' and fipsCountyCode eq '{county}' "
                    f"and declarationDate ge '{since}T00:00:00.000Z'"),
        "$select": ",".join(FIELDS),
        "$top": 1000,
        "$format": "json",
    }
    raw, path, from_cache = cached_get_json("fema_declarations", URL, params, refresh=refresh)
    rows = [FemaDeclaration.model_validate(r) for r in raw.get("DisasterDeclarationsSummaries", [])]
    df = pd.DataFrame([{
        "disaster_number": r.disasterNumber,
        "county_fips": county_fips,
        "incident_type": r.incidentType,
        "declaration_type": r.declarationType,
        "title": r.declarationTitle,
        "declaration_date": r.declarationDate[:10],
        "incident_begin_date": (r.incidentBeginDate or "")[:10] or None,
    } for r in rows], columns=["disaster_number", "county_fips", "incident_type", "declaration_type",
                               "title", "declaration_date", "incident_begin_date"])
    return df.drop_duplicates(["disaster_number", "county_fips"]), path, from_cache
