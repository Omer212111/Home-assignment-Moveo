"""Compute disruption days for all hubs from the database and print a ranking (no LLM).

    uv run python -m app.scoring.report                 # top 10, all hazards
    uv run python -m app.scoring.report --hazard winter --region Midwest
    uv run python -m app.scoring.report --hub dallas     # full breakdown for one hub
"""
import argparse

from app.config import load_scoring
from app.scoring import service
from app.scoring.disruption import HubDisruption, rank


def current_results() -> list[HubDisruption]:
    return service.load_results(restore=True)


def print_hub(r: HubDisruption):
    print(f"\n{r.city}, {r.state} ({r.region}): {r.total_days_per_year:.1f} disruption days/year")
    print(f"weather data: {r.weather_period or 'none'} (coverage {r.weather_coverage:.0%}"
          f"{'' if r.weather_complete else ', INCOMPLETE'}); "
          f"{r.weather_days_deduplicated} weather days attributed to declarations")
    for h in sorted(r.hazards.values(), key=lambda h: -h.days_per_year):
        if h.days_per_year == 0:
            continue
        print(f"  {h.label:<40} {h.days_per_year:6.2f} d/yr  "
              f"(weather {h.from_weather:.2f}, declarations {h.from_declarations:.2f}, {h.events_per_year:.1f} events/yr)")
        for d in h.drivers[:4]:
            print(f"      - {d.label:<60} {d.days_per_year:5.2f} d/yr  [{d.source}, n={d.occurrences:g}]")
    if r.declarations_counted:
        print("  counted FEMA disasters:", ", ".join(f"{c.title} ({c.declaration_date[:4]})"
                                                  for c in r.declarations_counted[:6]))


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--hazard", default="total", choices=["total", *load_scoring().hazards])
    p.add_argument("--region")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--hub")
    args = p.parse_args(argv)
    results = current_results()
    if args.hub:
        print_hub(next(r for r in results if r.hub_id == args.hub))
        return
    out = rank(results, args.hazard, args.region, args.top)
    print(f"Expected disruption days per year - hazard: {args.hazard}, region: {args.region or 'all'}")
    for row in out["ranking"]:
        print(f"{row['rank']:>3}. {row['city']:<18} {row['state']}  {row['days_per_year']:6.2f}")
    if out["excluded_incomplete_data"]:
        print(f"\nNot ranked - incomplete weather data ({len(out['excluded_incomplete_data'])}): "
              + ", ".join(out["excluded_incomplete_data"]))


if __name__ == "__main__":
    main()
