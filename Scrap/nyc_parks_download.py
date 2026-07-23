"""NYC Parks events scraper -- the live BigApps "Public Events" JSON feed.

Local New York City source only (NYC Parks & Recreation programming: tours,
fitness classes, nature walks, kids' activities, etc.). No API key needed.

Feed (updated daily, covers the upcoming ~14 days):
    GET https://www.nycgovparks.org/xml/events_300_rss.json
It returns a flat JSON array of events that already include coordinates and an
image, so this scraper fills the latitude/longitude columns the other NYC
sources leave blank. The feed has no date parameters, so we pull the whole feed
and let the shared date-window filter trim it to the requested day(s).

NOTE: the NYC Open Data "Parks Events Listing" dataset (data.cityofnewyork.us,
Socrata id fudw-fgrp) is a *different*, stale mirror -- it stopped updating at
the end of 2019 -- so this native feed is used instead. This source is NYC only.
"""

import csv
import datetime
import json
import re
import time
import urllib.error
import urllib.request

_print = print


def print(*args, **kwargs):  # noqa: A001 -- crash-proof console output
    """Logging must never kill a scrape: a broken/closed console handle on
    Windows (e.g. Streamlit launched from an IDE or a detached .bat) makes
    every print raise OSError [Errno 22]. Swallow console errors instead."""
    try:
        _print(*args, **kwargs)
    except Exception:
        pass


FEED_URL = "https://www.nycgovparks.org/xml/events_300_rss.json"

COLUMNS = [
    "name", "description", "location_name", "latitude", "longitude", "address",
    "start_time", "end_time", "city", "primary_category", "secondary_categories",
    "thumbnail_image", "additional_images", "external_link", "is_paid",
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://www.nycgovparks.org/events",
}

_NYC_ALIASES = {
    "new york", "new york city", "nyc", "new york, ny", "manhattan",
    "brooklyn", "queens", "the bronx", "bronx", "staten island",
}

_TAG_RE = re.compile(r"<[^>]+>")


def _is_nyc(config):
    city = (config.get("city") or "").strip().lower()
    return not city or city in _NYC_ALIASES


def strip_html(text):
    if not text:
        return ""
    text = _TAG_RE.sub(" ", str(text))
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&ndash;", "-").replace("&mdash;", "-")
            .replace("&rsquo;", "'").replace("&lsquo;", "'")
            .replace("&ldquo;", '"').replace("&rdquo;", '"')
            .replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def _iso(date_str, time_str):
    """'2026-07-01' + '7:00 am' -> '2026-07-01T07:00:00'. Date only if no time."""
    d = str(date_str or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        return d
    t = str(time_str or "").strip().lower()
    m = re.match(r"(\d{1,2}):(\d{2})\s*([ap]m)?", t)
    if not m:
        return d
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "pm" and hh != 12:
        hh += 12
    elif ap == "am" and hh == 12:
        hh = 0
    return f"{d}T{hh:02d}:{mm:02d}:00"


def _split_coords(value):
    s = str(value or "").strip()
    if not s or "," not in s:
        return "", ""
    lat, _, lon = s.partition(",")
    lat, lon = lat.strip(), lon.strip()
    try:
        float(lat)
        float(lon)
    except ValueError:
        return "", ""
    return lat, lon


def fetch_events(config):
    if not _is_nyc(config):
        print(f"  nyc_parks: skipping '{config.get('city')}' "
              f"(this source is New York City only).")
        return []

    req = urllib.request.Request(FEED_URL, headers=HEADERS)
    data = []
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise

    events, seen = [], set()
    for ev in (data if isinstance(data, list) else []):
        if not isinstance(ev, dict):
            continue
        key = ev.get("guid") or ev.get("link") or ev.get("title")
        if key in seen:
            continue
        seen.add(key)
        events.append(ev)
    print(f"  nyc_parks: {len(events)} events in feed (upcoming ~14 days)")
    return events


def map_event(event):
    lat, lon = _split_coords(event.get("coordinates"))
    cats = [c.strip() for c in str(event.get("categories") or "").split("|")
            if c.strip()]
    link = (event.get("link") or "").replace("http://", "https://")
    description = strip_html(event.get("description", ""))

    # Paid / free: the feed has no price field; infer it from the text
    # (a "$" / fee mention -> paid, an explicit "free" -> free).
    text = f"{event.get('title', '')} {description}".lower()
    if "$" in description or re.search(r"\bfee\b|\bregistration fee\b", text):
        is_paid = "true"
    elif (re.search(r"\bfree\b", text)
          and not re.search(r"(smoke|alcohol|barrier|gluten)[- ]free", text)):
        is_paid = "false"
    else:
        is_paid = ""

    return {
        "name": event.get("title", ""),
        "description": description,
        "location_name": event.get("location") or event.get("parknames", ""),
        "latitude": lat,
        "longitude": lon,
        "address": event.get("parknames", ""),
        "start_time": _iso(event.get("startdate"), event.get("starttime")),
        "end_time": _iso(event.get("enddate"), event.get("endtime")),
        "city": "new york",
        "primary_category": cats[0] if cats else "",
        "secondary_categories": ",".join(cats[1:]),
        "thumbnail_image": event.get("image") or "",
        "additional_images": "",
        "external_link": link,
        "is_paid": is_paid,
    }


# --------------------------------------------------------------------------- #
# Date-window filter + de-dupe + city back-fill (the "current" functionality)
# --------------------------------------------------------------------------- #
def keep_on_dates(rows, start_date=None, end_date=None, city="",
                  tz_name="America/New_York"):
    def zone():
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_name)
        except Exception:
            return datetime.timezone.utc

    def local_date(value):
        s = str(value or "").strip()
        if not s:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            try:
                return datetime.date.fromisoformat(s)
            except ValueError:
                return None
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.datetime.fromisoformat(iso)
        except ValueError:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
            return datetime.date.fromisoformat(m.group(1)) if m else None
        if dt.tzinfo is not None:
            dt = dt.astimezone(zone())
        return dt.date()

    start = datetime.date.fromisoformat(start_date) if start_date else None
    end = datetime.date.fromisoformat(end_date) if end_date else start
    if start and end and end < start:
        start, end = end, start
    city = (city or "").strip().lower()

    out, seen = [], set()
    for row in rows:
        day = local_date(row.get("start_time"))
        if day is None:
            if start or end:
                continue
        else:
            if start and day < start:
                continue
            if end and day > end:
                continue
        if city and not str(row.get("city") or "").strip():
            row = dict(row, city=city)
        key = (str(row.get("name") or "").strip().lower(),
               str(row.get("location_name") or "").strip().lower(),
               day.isoformat() if day else "")
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def download(config):
    print("pull events..")
    events = fetch_events(config)
    rows = [map_event(e) for e in events]
    rows = keep_on_dates(rows, config.get("start_date"), config.get("end_date"),
                         config.get("city", ""),
                         tz_name=config.get("tz_name") or "America/New_York")
    out = config.get("out", "events.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Finish: {len(rows)} rows -> {out}")
    return rows
