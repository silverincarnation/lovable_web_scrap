"""nyc.gov Events Calendar scraper -- official NYC "Find Local Events" API.

Local New York City source only (city-sponsored events: NYC Parks, DOT street
events, farmers markets, festivals, etc.). Same standalone style as the other
scrapers (own helpers + CSV writing) plus the shared date-window filter.

Endpoint (Azure API Management):
    GET https://api.nyc.gov/calendar/search
Auth: subscription key sent as the `Ocp-Apim-Subscription-Key` header.
    scrape_runner passes it as config["apikey"] (from env NYC_EVENTS_API_KEY /
    a local .env). A built-in DEFAULT_KEY is used as a fallback so the module
    also works standalone.

The search service returns at most 10 events per page; we page through
`pagination.numPages`. Results have no lat/long, so those columns stay blank
(same as the RA scraper). This API is New York City only -- for any other city
it returns nothing.
"""

import csv
import datetime
import json
import os
import re
import time
import urllib.error
import urllib.parse
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


SEARCH_URL = "https://api.nyc.gov/calendar/search"

COLUMNS = [
    "name", "description", "location_name", "latitude", "longitude", "address",
    "start_time", "end_time", "city", "primary_category", "secondary_categories",
    "thumbnail_image", "additional_images", "external_link", "is_paid",
]

# Cities this local API actually covers (everything else -> no events).
_NYC_ALIASES = {
    "new york", "new york city", "nyc", "new york, ny", "manhattan",
    "brooklyn", "queens", "the bronx", "bronx", "staten island",
}

_TAG_RE = re.compile(r"<[^>]+>")


def get(d, *keys, default=""):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d if d is not None else default


def _apikey(config):
    # key comes from config / env / .env only -- never hard-coded in the repo
    return config.get("apikey") or os.environ.get("NYC_EVENTS_API_KEY")


def _is_nyc(config):
    city = (config.get("city") or "").strip().lower()
    # No city given -> assume NYC (the API is NYC-only anyway).
    return not city or city in _NYC_ALIASES


def _mmddyyyy(value, fallback):
    """'2026-07-01' -> '07/01/2026'. Accepts a date or ISO string."""
    s = str(value or "").strip()
    if not s:
        return fallback
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(2)}/{m.group(3)}/{m.group(1)}"
    return fallback


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


def build_params(config, page):
    today = datetime.date.today().strftime("%m/%d/%Y")
    start = _mmddyyyy(config.get("start_date"), today)
    end = _mmddyyyy(config.get("end_date"), start)
    params = {
        "startDate": f"{start} 12:00 AM",
        "endDate": f"{end} 11:59 PM",
        "sort": "DATE",
        "pageNumber": page,
    }
    # Optional passthroughs.
    for cfg_key, api_name in (("agency", "agency"), ("categories", "categories"),
                              ("boroughs", "boroughs"), ("zip", "zip")):
        if config.get(cfg_key):
            params[api_name] = config[cfg_key]
    kw = config.get("keywords") or config.get("keyword")
    if kw:
        params["keywords"] = kw
    return params


def fetch_json(url, apikey):
    req = urllib.request.Request(url, headers={
        "Ocp-Apim-Subscription-Key": apikey,
        "Accept": "application/json",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
    })
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    return {}


def fetch_events(config):
    if not _is_nyc(config):
        print(f"  nyc_events: skipping '{config.get('city')}' "
              f"(this source is New York City only).")
        return []

    apikey = _apikey(config)
    if not apikey:
        raise ValueError("config lack apikey (set NYC_EVENTS_API_KEY)")

    events, seen = [], set()
    page = 1
    while page <= 1000:
        url = SEARCH_URL + "?" + urllib.parse.urlencode(build_params(config, page))
        data = fetch_json(url, apikey)
        items = get(data, "items", default=[]) or []
        new = 0
        for ev in items:
            if not isinstance(ev, dict):
                continue
            key = ev.get("guid") or ev.get("id") or ev.get("permalink")
            if key in seen:
                continue
            seen.add(key)
            events.append(ev)
            new += 1
        num_pages = get(data, "pagination", "numPages", default=1)
        is_last = get(data, "pagination", "isLastPage", default=True)
        print(f"  page {page}/{num_pages}: {len(items)} found, "
              f"{new} new (total {len(events)})")
        time.sleep(0.25)
        page += 1
        if is_last or not items or page > (num_pages or 1):
            break
    return events


def map_event(event):
    cats_raw = event.get("categories", "") or ""
    cats = [c.strip() for c in cats_raw.split(",") if c.strip()]
    primary = cats[0] if cats else ""
    secondary = ",".join(cats[1:])

    description = strip_html(event.get("desc") or event.get("shortDesc", ""))
    address = (event.get("address", "") or "").strip()
    # `location` is empty for about half the events -- fall back to the first
    # line of the street address so location_name is still usable.
    location_name = (event.get("location", "") or "").strip()
    if not location_name and address:
        location_name = address.split(",")[0].strip()

    # Paid / free: the API has no price field; infer "free" from a Free
    # category or an explicit "free" in the name / description.
    text = f"{event.get('name', '')} {description}".lower()
    if any(c.lower() == "free" or c.lower().startswith("free ") for c in cats):
        is_paid = "false"
    elif (re.search(r"\bfree\b", text)
          and not re.search(r"(smoke|alcohol|barrier|gluten)[- ]free", text)):
        is_paid = "false"
    else:
        is_paid = ""

    return {
        "name": event.get("name", ""),
        "description": description,
        "location_name": location_name,
        "latitude": "",
        "longitude": "",
        "address": address,
        "start_time": event.get("startDate", ""),
        "end_time": event.get("endDate", ""),
        "city": "new york",
        "primary_category": primary,
        "secondary_categories": secondary,
        "thumbnail_image": event.get("imageUrl", ""),
        "additional_images": "",
        "external_link": event.get("permalink") or event.get("website", ""),
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
