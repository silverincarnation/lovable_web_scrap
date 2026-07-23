"""Discover + run the Scrap/<site>_download.py scrapers (factored out of run.py).

Runs day by day with timeout retries and saves one CSV per site/day to
``result/<Location>/<YYYY-MM-DD>/<site>.csv`` -- same behaviour as run.py --
and returns the scraped rows tagged with source / location / date.

Optional env overrides: SCRAP_DIR, RESULT_DIR.
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import re
import socket
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULT_DIR = os.environ.get("RESULT_DIR") or os.path.join(ROOT, "result")
SCRAP_DIR = os.environ.get("SCRAP_DIR") or os.path.join(ROOT, "Scrap")
SUFFIX = "_download.py"
MAX_ATTEMPTS, RETRY_WAIT_SEC = 4, 20     # timeouts only; base wait scaled by attempt

NA_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu",
    "America/Toronto", "America/Vancouver", "America/Mexico_City",
]

# Load a local .env so <SITE>_API_KEY (e.g. TICKETMASTER_API_KEY) reaches the scrapers.
_env = os.path.join(ROOT, ".env")
if os.path.exists(_env):
    for _line in open(_env, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

_MODS: dict[str, object] = {}


def discover() -> dict[str, str]:
    """{site: path} for every Scrap/<site>_download.py."""
    if not os.path.isdir(SCRAP_DIR):
        return {}
    return {f[: -len(SUFFIX)]: os.path.join(SCRAP_DIR, f)
            for f in sorted(os.listdir(SCRAP_DIR))
            if f.endswith(SUFFIX) and not f.startswith("_")}


def _download_fn(site: str, path: str):
    """Import <site>_download.py once and return its ``download`` function."""
    if path not in _MODS:
        if SCRAP_DIR not in sys.path:
            sys.path.insert(0, SCRAP_DIR)
        spec = importlib.util.spec_from_file_location("scrap_" + site, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODS[path] = mod.download
    return _MODS[path]


def _is_timeout(e: Exception) -> bool:
    return (isinstance(e, (TimeoutError, socket.timeout))
            or isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout))
            or "timeout" in str(e).lower() or "timed out" in str(e).lower())


def location_folder(city: str, country: str) -> str:
    """'New York','US' -> 'New_York_US'."""
    part = re.sub(r"[^A-Za-z0-9]+", "_", city.strip()).strip("_") or "Location"
    part = "_".join(w.capitalize() if w.islower() else w for w in part.split("_"))
    tail = re.sub(r"[^A-Za-z0-9]+", "_", country.strip().upper()).strip("_")
    return f"{part}_{tail}" if tail else part


def _config(site, city, country, date, out, tz_name) -> dict:
    """Per-site/day config for download() -- identical to run.py.build_config."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = datetime.timezone.utc
    d = datetime.date.fromisoformat(date)

    def utc(t):
        return datetime.datetime.combine(d, t, tzinfo=tz).astimezone(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cfg = {"city": city.strip(), "country_code": country.strip().upper(),
           "start_date": date, "end_date": date,
           "start_time": utc(datetime.time.min), "end_time": utc(datetime.time.max),
           "size": 200, "max_pages": 1000, "keyword": None, "out": out,
           "tz_name": tz_name}
    key = os.environ.get(f"{site.upper()}_API_KEY")
    if key:
        cfg["apikey"] = key
    return cfg


def scrape(sites, city, country, start_date, end_date=None,
           tz_name="America/New_York") -> dict:
    """Scrape ``sites`` for city/country over a day range; save CSVs; return a dict.

    Day by day (every site for one day before the next), with timeout retries and
    empty-CSV cleanup -- exactly like run.py.
    """
    scrapers = discover()
    sites = [s for s in (sites or scrapers) if s in scrapers]
    s = datetime.date.fromisoformat(start_date)
    e = datetime.date.fromisoformat(end_date) if end_date else s
    s, e = min(s, e), max(s, e)
    dates = [(s + datetime.timedelta(days=i)).isoformat()
             for i in range((e - s).days + 1)]
    location = location_folder(city, country)

    summary, events = [], []
    for date in dates:                                   # day by day
        out_dir = os.path.join(RESULT_DIR, location, date)
        os.makedirs(out_dir, exist_ok=True)
        for site in sites:
            out = os.path.join(out_dir, f"{site}.csv")
            rows, status = None, None
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    rows = _download_fn(site, scrapers[site])(
                        _config(site, city, country, date, out, tz_name))
                    status = "ok"
                    break
                except Exception as ex:                  # timeout -> wait+retry
                    if _is_timeout(ex) and attempt < MAX_ATTEMPTS:
                        time.sleep(RETRY_WAIT_SEC * attempt)
                        continue
                    tb = traceback.extract_tb(ex.__traceback__)
                    where = (f" @ {os.path.basename(tb[-1].filename)}:{tb[-1].lineno}"
                             if tb else "")
                    status = ("TimeoutError: gave up after retries"
                              if _is_timeout(ex)
                              else f"{type(ex).__name__}: {ex}{where}")
                    break
            rows = rows or []
            if not rows and os.path.exists(out):         # no data -> no header-only CSV
                try:
                    os.remove(out)
                except OSError:
                    pass
            for r in rows:
                events.append({"source": site, "location": location, "date": date, **r})
            summary.append({"site": site, "date": date, "rows": len(rows),
                            "status": status,
                            "file": os.path.relpath(out, ROOT) if rows else None})

    return {"location": location, "city": city, "country": country.strip().upper(),
            "timezone": tz_name, "dates": dates, "sites": sites,
            "summary": summary, "events": events, "total_rows": len(events),
            "ok": sum(1 for r in summary if r["status"] == "ok"),
            "tasks": len(summary), "result_dir": os.path.join(RESULT_DIR, location)}


if __name__ == "__main__":
    print("Scrap :", SCRAP_DIR)
    print("Result:", RESULT_DIR)
    print("Sites :", list(discover()))
