"""ra.co (Resident Advisor) scraper -- self-contained, the site's GraphQL API.

RA renders listings client-side from GraphQL, so the page HTML has no useful
JSON-LD. We call the same public endpoint the website uses:
    POST https://ra.co/graphql   ->  eventListings(filters, page, pageSize)

Same standalone style as the other scrapers (own helpers + CSV writing), plus a
date-window filter. RA locates by a numeric *area id* (New York = 8); pass another
city's id as config["area_id"] (or config["place"]) -- unknown cities are
resolved automatically via RA's `areas` search.

The listing query also asks for the event description (`content`), venue
coordinates, and cost / ticketing info so those CSV columns are filled; if RA's
schema ever rejects one of those fields, the scraper automatically falls back
to a basic field set instead of failing.
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


GQL_URL = "https://ra.co/graphql"
SITE = "https://ra.co"

COLUMNS = [
    "name", "description", "location_name", "latitude", "longitude", "address",
    "start_time", "end_time", "city", "primary_category", "secondary_categories",
    "thumbnail_image", "additional_images", "external_link", "is_paid",
]

AREA_IDS = {
    "new york": 8, "nyc": 8, "london": 13, "berlin": 34, "amsterdam": 26,
    "paris": 44, "los angeles": 23, "san francisco": 20, "chicago": 5,
    "miami": 16, "barcelona": 28, "tokyo": 27, "sydney": 2, "toronto": 21,
    # Mexico City (verified against ra.co/graphql: area 399 -> "Mexico City").
    "mexico city": 399, "cdmx": 399, "ciudad de mexico": 399,
    "ciudad de méxico": 399, "mexico city, mx": 399,
    # Verified live via ra.co/graphql areas search (2026-07):
    "guadalajara": 522, "monterrey": 70, "tijuana": 524, "austin": 321,
}

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://ra.co/events",
    "Origin": "https://ra.co",
    "ra-content-language": "en",
}

QUERY = """
query GET_EVENT_LISTINGS($filters: FilterInputDtoInput, $page: Int, $pageSize: Int) {
  eventListings(filters: $filters, pageSize: $pageSize, page: $page) {
    totalResults
    data { event {
      id title content startTime endTime contentUrl cost isTicketed
      venue { name address area { name } location { latitude longitude } }
      artists { name }
      images { filename }
    } }
  }
}
"""

# Fallback used automatically if RA's schema rejects any enhanced field above.
QUERY_BASIC = """
query GET_EVENT_LISTINGS($filters: FilterInputDtoInput, $page: Int, $pageSize: Int) {
  eventListings(filters: $filters, pageSize: $pageSize, page: $page) {
    totalResults
    data { event {
      id title startTime endTime contentUrl
      venue { name address area { name } }
      artists { name }
      images { filename }
    } }
  }
}
"""

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    """RA's `content` field is HTML -- reduce it to clean plain text."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", str(text))
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&ndash;", "-").replace("&mdash;", "-")
            .replace("&rsquo;", "'").replace("&lsquo;", "'")
            .replace("&ldquo;", '"').replace("&rdquo;", '"')
            .replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def get(d, *keys, default=""):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d if d is not None else default


AREAS_QUERY = """
query GET_AREAS($searchTerm: String!, $limit: Int) {
  areas(searchTerm: $searchTerm, limit: $limit) {
    id
    name
    country { name }
  }
}
"""

# ISO country code -> RA country name, to disambiguate same-named cities
# (e.g. Paris/FR vs Paris/US, London/GB vs London/CA). Unknown codes => no filter.
_CC_TO_NAME = {
    "US": "United States", "GB": "United Kingdom", "UK": "United Kingdom",
    "DE": "Germany", "FR": "France", "JP": "Japan", "AU": "Australia",
    "CA": "Canada", "ES": "Spain", "NL": "Netherlands", "IT": "Italy",
    "BR": "Brazil", "MX": "Mexico", "AR": "Argentina", "BE": "Belgium",
    "PT": "Portugal", "CH": "Switzerland", "AT": "Austria", "SE": "Sweden",
    "NO": "Norway", "DK": "Denmark", "FI": "Finland", "IE": "Ireland",
    "PL": "Poland", "GR": "Greece", "CO": "Colombia", "CL": "Chile",
    "ZA": "South Africa", "KR": "South Korea", "SG": "Singapore",
    "TH": "Thailand", "IN": "India", "NZ": "New Zealand", "TR": "Turkey",
    "AE": "United Arab Emirates", "HK": "Hong Kong",
}


def _lookup_area_id(city, country_code=""):
    """Resolve a city name to an RA area id via the public `areas` query."""
    term = (city or "").strip()
    if not term:
        return None
    try:
        data = _post({"searchTerm": term, "limit": 5}, AREAS_QUERY)
    except Exception as e:
        print(f"  ra: area lookup failed for '{city}': {e}")
        return None
    areas = ((data or {}).get("data") or {}).get("areas") or []
    if not areas:
        return None
    want = _CC_TO_NAME.get((country_code or "").strip().upper(), "").lower()

    def score(a):
        s = 0
        if (a.get("name") or "").strip().lower() == term.lower():
            s += 2
        country = ((a.get("country") or {}).get("name") or "").strip().lower()
        if want and country == want:
            s += 2
        return s

    best = max(areas, key=score)
    try:
        area = int(str(best.get("id")).strip())
    except (TypeError, ValueError):
        return None
    print(f"  ra: resolved '{city}' -> area {area} "
          f"({best.get('name')}, {get(best, 'country', 'name')})")
    return area


def _area_id(config):
    # 1) explicit override via config["area_id"] / ["place"]
    for key in ("area_id", "place"):
        v = str(config.get(key) or "").strip()
        if v.isdigit():
            return int(v)
    # 2) fast path: built-in city map
    mapped = AREA_IDS.get((config.get("city") or "").strip().lower())
    if mapped:
        return mapped
    # 3) auto-lookup by city name via RA's `areas` query
    return _lookup_area_id(config.get("city"), config.get("country_code"))


def _post(variables, query=QUERY):
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(GQL_URL, data=body, headers=HEADERS)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    return {}


def _gql_failed(data):
    """True if the GraphQL response carries errors and no usable listings."""
    if not isinstance(data, dict):
        return True
    return bool(data.get("errors")) and not get(
        data, "data", "eventListings", "data", default=[])


def fetch_events(config):
    area = _area_id(config)
    if not area:
        print(f"  ra: no area id for '{config.get('city')}' (New York = 8).")
        return []
    start = config.get("start_date") or ""
    end = config.get("end_date") or start
    filters = {"areas": {"eq": area}}
    if start:
        filters["listingDate"] = {"gte": start, "lte": end or start}

    events, seen = [], set()
    page = 1
    query = QUERY
    while page <= 1000:
        data = _post({"filters": filters, "page": page, "pageSize": 20}, query)
        if query is QUERY and _gql_failed(data):
            print("  ra: enhanced query rejected by the API; "
                  "falling back to basic fields")
            query = QUERY_BASIC
            data = _post({"filters": filters, "page": page, "pageSize": 20}, query)
        listings = get(data, "data", "eventListings", "data", default=[])
        if not listings:
            break
        new = 0
        for row in listings:
            event = row.get("event") if isinstance(row, dict) else None
            if not isinstance(event, dict):
                continue
            if event.get("id") in seen:
                continue
            seen.add(event.get("id"))
            events.append(event)
            new += 1
        total = get(data, "data", "eventListings", "totalResults", default=len(events))
        print(f"  page {page}: {new} new (total {len(events)})")
        page += 1
        if new == 0 or len(events) >= total:
            break
    return events


def map_event(event):
    venue = event.get("venue") or {}
    if not isinstance(venue, dict):
        venue = {}
    loc = venue.get("location") or {}
    if not isinstance(loc, dict):
        loc = {}
    artists = [a.get("name", "") for a in (event.get("artists") or []) if a.get("name")]
    images = [i.get("filename", "") for i in (event.get("images") or [])
              if isinstance(i, dict) and i.get("filename")]
    url = event.get("contentUrl", "")
    if url.startswith("/"):
        url = SITE + url

    # Paid / free: isTicketed means RA sells tickets for it; otherwise read
    # the `cost` string ("$20", "FREE", ...). Unknown stays "".
    cost = str(event.get("cost") or "").strip()
    if event.get("isTicketed") is True:
        is_paid = "true"
    elif re.search(r"(?i)\bfree\b|\bgratis\b", cost) or cost in ("0", "$0", "0.00"):
        is_paid = "false"
    elif cost:
        is_paid = "true"
    else:
        is_paid = ""

    lat = loc.get("latitude")
    lon = loc.get("longitude")
    try:  # RA returns 0,0 for TBA/secret venues -- that's "unknown", not a place
        if float(lat) == 0 and float(lon) == 0:
            lat = lon = None
    except (TypeError, ValueError):
        pass

    return {
        "name": event.get("title", ""),
        "description": strip_html(event.get("content", "")),
        "location_name": venue.get("name", ""),
        "latitude": "" if lat is None else str(lat),
        "longitude": "" if lon is None else str(lon),
        "address": venue.get("address", ""),
        "start_time": event.get("startTime", ""),
        "end_time": event.get("endTime", ""),
        "city": (get(venue, "area", "name") or "").lower(),
        "primary_category": "Music",
        "secondary_categories": ",".join(artists),
        "thumbnail_image": images[0] if images else "",
        "additional_images": ",".join(images[1:]),
        "external_link": url,
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
