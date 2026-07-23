"""Headless CLI runner: scrape a city over a date range -> raw CSVs.

Mirrors the Streamlit form (run.py): free-text city + country + a date range
(batch), optional timezone and site subset. Saves the raw per-source CSVs in
the existing format result/<City_COUNTRY>/<date>/<site>.csv -- nothing else.
Used by the GitHub Actions workflow; runnable locally with the same flags.

New scrapers are picked up automatically: any Scrap/<site>_download.py with a
download(config) function joins the run without configuration.

API keys come from environment variables or a local .env
(TICKETMASTER_API_KEY, NYC_EVENTS_API_KEY).

Examples:
    python run_scrape.py --city "New York" --country US --start today
    python run_scrape.py --city "Mexico City" --country MX --start 2026-08-01 --end 2026-08-07
    python run_scrape.py --city Guadalajara --country MX --start today --end today+3
    python run_scrape.py --city "Los Angeles" --country US --start 2026-08-01 --tz America/Los_Angeles
    python run_scrape.py --city Chicago --country US --start today --sites eventbrite ticketmaster
"""

import argparse
import datetime
import sys
from zoneinfo import ZoneInfo

import scraper_engine as engine

# Country code -> default timezone. Override with --tz for e.g. US west coast
# (America/Los_Angeles) so the per-day bucketing matches the venue's local day.
COUNTRY_TZ = {
    "US": "America/New_York", "MX": "America/Mexico_City",
    "CA": "America/Toronto", "GB": "Europe/London", "ES": "Europe/Madrid",
    "AR": "America/Argentina/Buenos_Aires", "BR": "America/Sao_Paulo",
    "FR": "Europe/Paris", "DE": "Europe/Berlin", "IT": "Europe/Rome",
}


def _resolve(tok, tz):
    """'today' / 'today+N' / 'YYYY-MM-DD' -> a date (today = local to tz)."""
    today = datetime.datetime.now(ZoneInfo(tz)).date()
    tok = (tok or "").strip()
    if not tok or tok.lower() == "today":
        return today
    if tok.lower().startswith("today+"):
        return today + datetime.timedelta(days=int(tok[6:]))
    return datetime.date.fromisoformat(tok)          # validates format


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", nargs="?", const="New York", default="New York",
                    help="city name, free text (e.g. \"New York\", Guadalajara)")
    ap.add_argument("--country", nargs="?", const="US", default="US",
                    help="2-letter country code (e.g. US, MX, CA)")
    ap.add_argument("--start", nargs="?", const="today", default="today",
                    help="start date: YYYY-MM-DD, 'today', or 'today+N'")
    ap.add_argument("--end", nargs="?", const="", default="",
                    help="end date (blank = same as start); range = batch pull")
    ap.add_argument("--tz", nargs="?", const="", default="",
                    help="timezone (blank = default for the country)")
    ap.add_argument("--sites", nargs="*", default=None,
                    help="subset of scrapers (default: all found in Scrap/)")
    args = ap.parse_args(argv)

    city = args.city.strip()
    country = args.country.strip().upper()
    tz = args.tz.strip() or COUNTRY_TZ.get(country, "America/New_York")
    try:
        ZoneInfo(tz)
    except Exception:
        print(f"unknown timezone '{tz}', falling back to America/New_York")
        tz = "America/New_York"

    start = _resolve(args.start, tz)
    end = _resolve(args.end, tz) if args.end.strip() else start
    if end < start:
        start, end = end, start

    ndays = (end - start).days + 1
    print(f"=== {city} ({country})  {start} .. {end}  ({ndays} day(s))  tz={tz} ===",
          flush=True)

    # engine.scrape does the whole date range day-by-day, with retries, and
    # writes result/<City_COUNTRY>/<date>/<site>.csv -- same as the Streamlit app.
    res = engine.scrape(args.sites, city, country,
                        start.isoformat(), end.isoformat(), tz_name=tz)

    ok = errors = 0
    for task in res["summary"]:
        print(f"  {task['date']}  {task['site']:14s} {task['rows']:5d} rows  "
              f"{task['status']}", flush=True)
        if task["status"] == "ok":
            ok += 1
        else:
            errors += 1

    print(f"\ntotal: {ok} ok, {errors} failed, {res['total_rows']} rows "
          f"-> {res['result_dir']}", flush=True)

    # red X in Actions only when nothing succeeded at all
    return 1 if ok == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
