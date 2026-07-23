"""Headless CLI runner: scrape -> raw CSVs -> done. Used by GitHub Actions.

Scrapes and saves the raw per-source CSVs in the existing format
(result/<City>/<Date>/<site>.csv) -- nothing else. Cleaning stays a separate,
manual step (clean_pipeline.py / clean_daily_csv.py).

New scrapers are picked up automatically: any Scrap/<site>_download.py with a
download(config) function joins the run without configuration.

API keys come from environment variables or a local .env
(TICKETMASTER_API_KEY, NYC_EVENTS_API_KEY).

Usage:
    python run_scrape.py                                   # today, NY + Mexico City
    python run_scrape.py --dates 2026-08-01 2026-08-02
    python run_scrape.py --dates today today+1             # relative dates
    python run_scrape.py --cities New_York_US              # subset of cities
    python run_scrape.py --sites eventbrite ticketmaster   # subset of sources
"""

import argparse
import datetime
import sys
from zoneinfo import ZoneInfo

import scraper_engine as engine

CITY_PRESETS = {
    "New_York_US": ("New York", "US", "America/New_York"),
    "Mexico_City_MX": ("Mexico City", "MX", "America/Mexico_City"),
    "Los_Angeles_US": ("Los Angeles", "US", "America/Los_Angeles"),
}


def resolve_dates(tokens, tz_name):
    """'today' / 'today+2' / 'YYYY-MM-DD' -> ISO dates (today = city-local)."""
    today = datetime.datetime.now(ZoneInfo(tz_name)).date()
    if not tokens:
        return [today.isoformat()]
    out = []
    for t in tokens:
        t = t.strip()
        if t == "today":
            out.append(today.isoformat())
        elif t.startswith("today+"):
            out.append((today + datetime.timedelta(days=int(t[6:]))).isoformat())
        else:
            datetime.date.fromisoformat(t)   # validate early
            out.append(t)
    return sorted(set(out))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", nargs="*", default=[],
                    help="YYYY-MM-DD, 'today', or 'today+N' (default: today)")
    ap.add_argument("--cities", nargs="*",
                    default=["New_York_US", "Mexico_City_MX"],
                    help="city folder names (default: New_York_US Mexico_City_MX)")
    ap.add_argument("--sites", nargs="*", default=None,
                    help="subset of scrapers (default: all discovered in Scrap/)")
    args = ap.parse_args(argv)

    cities = [c for c in args.cities if c in CITY_PRESETS]
    if not cities:
        raise SystemExit("no valid cities; choose from " + ", ".join(CITY_PRESETS))

    ok = errors = 0
    all_dates = set()
    for folder in cities:
        city, country, tz = CITY_PRESETS[folder]
        dates = resolve_dates(args.dates, tz)
        all_dates.update(dates)
        for date in dates:
            print(f"=== {city} ({country}) {date} ===", flush=True)
            res = engine.scrape(args.sites, city, country, date, date, tz_name=tz)
            for task in res["summary"]:
                status = task["status"]
                print(f"  {task['site']:14s} {task['rows']:5d} rows  {status}",
                      flush=True)
                if status == "ok":
                    ok += 1
                else:
                    errors += 1

    print(f"\nscrape done: {ok} task(s) ok, {errors} failed "
          f"(dates: {', '.join(sorted(all_dates))})")

    # red X in Actions only when nothing succeeded at all
    return 1 if ok == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
