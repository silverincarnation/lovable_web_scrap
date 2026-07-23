"""ticketmaster.com scraper -- official Discovery API (needs an API key).

Self-contained: same structure as the standalone repo version, plus a date-window
filter so the output only contains events on the requested day(s) (de-duplicated,
city backfilled). scrape_runner passes the key in as config["apikey"] (.env).
"""

import csv
import datetime
import re
import time
import urllib.parse
import urllib.request
import urllib.error

_print = print


def print(*args, **kwargs):  # noqa: A001 -- crash-proof console output
    """Logging must never kill a scrape: a broken/closed console handle on
    Windows (e.g. Streamlit launched from an IDE or a detached .bat) makes
    every print raise OSError [Errno 22]. Swallow console errors instead."""
    try:
        _print(*args, **kwargs)
    except Exception:
        pass


API_URL = "https://app.ticketmaster.com/discovery/v2/events.json"

COLUMNS = [
    "name", "description", "location_name", "latitude", "longitude", "address",
    "start_time", "end_time", "city", "primary_category", "secondary_categories",
    "thumbnail_image", "additional_images", "external_link", "is_paid",
]


def get(d, *keys, default=""):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d if d is not None else default


# The Discovery API's free-text `city` filter is unreliable (Ticketmaster
# stores Mexican cities in Spanish / by borough, and suburb venues carry their
# own city name), so we search by geo radius instead. Known metros are listed
# here as a fast path; any other city is geocoded automatically (see _geocode),
# so all of the US / North America works out of the box.
CITY_LATLONG = {
    # US
    "new york": "40.7128,-74.0060", "nyc": "40.7128,-74.0060",
    "new york city": "40.7128,-74.0060", "brooklyn": "40.6782,-73.9442",
    "los angeles": "34.0522,-118.2437", "la": "34.0522,-118.2437",
    "chicago": "41.8781,-87.6298", "houston": "29.7604,-95.3698",
    "phoenix": "33.4484,-112.0740", "philadelphia": "39.9526,-75.1652",
    "san antonio": "29.4241,-98.4936", "san diego": "32.7157,-117.1611",
    "dallas": "32.7767,-96.7970", "austin": "30.2672,-97.7431",
    "san jose": "37.3382,-121.8863", "san francisco": "37.7749,-122.4194",
    "seattle": "47.6062,-122.3321", "denver": "39.7392,-104.9903",
    "boston": "42.3601,-71.0589", "miami": "25.7617,-80.1918",
    "atlanta": "33.7490,-84.3880", "washington": "38.9072,-77.0369",
    "washington dc": "38.9072,-77.0369", "las vegas": "36.1699,-115.1398",
    "detroit": "42.3314,-83.0458", "minneapolis": "44.9778,-93.2650",
    "new orleans": "29.9511,-90.0715", "nashville": "36.1627,-86.7816",
    "portland": "45.5152,-122.6784", "orlando": "28.5384,-81.3789",
    "charlotte": "35.2271,-80.8431", "st louis": "38.6270,-90.1994",
    "st. louis": "38.6270,-90.1994", "salt lake city": "40.7608,-111.8910",
    # Canada
    "toronto": "43.6532,-79.3832", "montreal": "45.5017,-73.5673",
    "vancouver": "49.2827,-123.1207", "calgary": "51.0447,-114.0719",
    "ottawa": "45.4215,-75.6972", "edmonton": "53.5461,-113.4938",
    # Mexico
    "mexico city": "19.4326,-99.1332", "cdmx": "19.4326,-99.1332",
    "ciudad de mexico": "19.4326,-99.1332", "ciudad de méxico": "19.4326,-99.1332",
    "guadalajara": "20.6597,-103.3496", "monterrey": "25.6866,-100.3161",
}

# Fallback country -> timezone for the local-date filter. Each event carries
# its own venue timezone (dates.timezone) which takes precedence, so a Los
# Angeles show is bucketed by Pacific time even when the country is just "US".
COUNTRY_TZ = {
    "MX": "America/Mexico_City", "US": "America/New_York",
    "CA": "America/Toronto", "GB": "Europe/London", "ES": "Europe/Madrid",
    "AR": "America/Argentina/Buenos_Aires",
}

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_GEO_CACHE = {}


def _geocode(city, country_code=""):
    """Resolve any city to "lat,long" via the free Open-Meteo geocoder (no key).

    Lets the geo-radius search work for any US / North American city, not just
    the ones hard-coded in CITY_LATLONG. Fails soft: on any error it returns
    None and fetch_events falls back to the `city` text filter.
    """
    key = ((city or "").strip().lower(), (country_code or "").strip().upper())
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    ll = None
    try:
        url = GEOCODE_URL + "?" + urllib.parse.urlencode(
            {"name": city, "count": 5, "language": "en", "format": "json"})
        data = fetch_json(url)
        results = data.get("results") or []
        cc = key[1]
        if cc:
            results = ([r for r in results
                        if str(r.get("country_code", "")).upper() == cc]
                       or results)
        if results:
            lat, lon = results[0].get("latitude"), results[0].get("longitude")
            if lat is not None and lon is not None:
                ll = f"{round(float(lat), 4)},{round(float(lon), 4)}"
                print(f"  ticketmaster: geocoded '{city}' -> {ll}")
    except Exception as e:
        print(f"  ticketmaster: geocode failed for '{city}': {e}")
    _GEO_CACHE[key] = ll
    return ll


def _latlong(config):
    v = str(config.get("latlong") or "").strip()
    if v:
        return v
    city = (config.get("city") or "").strip()
    if not city:
        return None
    return (CITY_LATLONG.get(city.lower())
            or _geocode(city, config.get("country_code")))


def build_params(config, page, extra=None):
    params = {
        "apikey": config["apikey"],
        "size": config.get("size", 100),
        "page": page,
    }
    mapping = {
        "start_time": "startDateTime",
        "end_time": "endDateTime",
        "country_code": "countryCode",
        "keyword": "keyword",
    }
    for key, api_name in mapping.items():
        if config.get(key):
            params[api_name] = config[key]
    for k, v in (extra or {}).items():
        if v not in (None, ""):
            params[k] = v
    return params


def fetch_json(url):
    import json
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    return {}


def _fetch_pages(config, extra):
    events = []
    size = config.get("size", 100)
    page_cap = max(1, 1000 // size)
    max_pages = config.get("max_pages") or 10**9
    max_pages = min(max_pages, page_cap)

    page = 0
    data = {}
    while page < max_pages:
        url = API_URL + "?" + urllib.parse.urlencode(build_params(config, page, extra))
        data = fetch_json(url)
        time.sleep(0.25)
        batch = get(data, "_embedded", "events", default=[])
        events.extend(batch)

        total_pages = get(data, "page", "totalPages", default=1)
        page += 1
        if not batch or page >= total_pages:
            break

    total = get(data, "page", "totalElements", default=len(events))
    if total > 1000:
        print("  ticketmaster: API caps at 1000 results; narrow the date range.")
    return events


def fetch_events(config):
    """Try geo radius, then the city string, then country-wide -- return the
    first attempt that yields events. Each attempt is logged, so 0 rows is
    always explainable (empty API result vs. filtered out later)."""
    strategies = []
    ll = _latlong(config)
    if ll:
        strategies.append(("geo radius " + ll,
                           {"latlong": ll, "radius": config.get("radius", 100),
                            "unit": config.get("unit", "km")}))
    if config.get("city"):
        strategies.append(("city=%s" % config["city"], {"city": config["city"]}))
    strategies.append(("countryCode only", {}))

    for label, extra in strategies:
        events = _fetch_pages(config, extra)
        print(f"  ticketmaster: {len(events)} events via {label}")
        if events:
            return events
    return []


def map_event(event):
    venue = get(event, "_embedded", "venues", default=[{}])[0]
    classifications = event.get("classifications") or [{}]
    cls = classifications[0]
    image_urls = [img["url"] for img in event.get("images", []) if img.get("url")]
    thumbnail = image_urls[0] if image_urls else ""
    additional = ",".join(image_urls[1:])

    # Description: `info` is rarely filled -- fall back to other text fields.
    description = (event.get("info") or event.get("description")
                   or event.get("pleaseNote") or "")

    # Categories: primary = segment; secondary = genre / subGenre / extra
    # segments across every classification (de-duplicated, primary excluded).
    primary = get(cls, "segment", "name")
    secondary, seen_cat = [], {primary.lower()} if primary else set()
    for c in classifications:
        for part in ("genre", "subGenre", "segment"):
            nm = get(c, part, "name")
            if nm and nm.lower() not in seen_cat and nm.lower() != "undefined":
                seen_cat.add(nm.lower())
                secondary.append(nm)

    # Paid / free: Ticketmaster is a ticketing platform, so default to paid;
    # only an explicit 0-to-0 price range means free.
    pr = event.get("priceRanges") or []
    if pr:
        try:
            mx = max(float(p.get("max") or p.get("min") or 0) for p in pr)
        except (TypeError, ValueError):
            mx = None
        is_paid = "false" if mx == 0 else "true"
    else:
        is_paid = "true"

    return {
        "name": event.get("name", ""),
        "description": description,
        "location_name": venue.get("name", ""),
        "latitude": get(venue, "location", "latitude"),
        "longitude": get(venue, "location", "longitude"),
        "address": get(venue, "address", "line1"),
        "start_time": get(event, "dates", "start", "dateTime"),
        "end_time": get(event, "dates", "end", "dateTime"),
        "city": get(venue, "city", "name").lower(),
        "primary_category": primary,
        "secondary_categories": ",".join(secondary),
        "thumbnail_image": thumbnail,
        "additional_images": additional,
        "external_link": event.get("url", ""),
        "is_paid": is_paid,
        "_tz": get(event, "dates", "timezone"),   # venue-local tz (internal)
    }


# --------------------------------------------------------------------------- #
# Date-window filter + de-dupe + city back-fill (the "current" functionality)
# --------------------------------------------------------------------------- #
def keep_on_dates(rows, start_date=None, end_date=None, city="",
                  tz_name="America/New_York"):
    def zone(name):
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            return datetime.timezone.utc

    default_zone = zone(tz_name)

    def local_date(value, row_tz=None):
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
            dt = dt.astimezone(zone(row_tz) if row_tz else default_zone)
        return dt.date()

    start = datetime.date.fromisoformat(start_date) if start_date else None
    end = datetime.date.fromisoformat(end_date) if end_date else start
    if start and end and end < start:
        start, end = end, start
    city = (city or "").strip().lower()

    out, seen = [], set()
    for row in rows:
        day = local_date(row.get("start_time"), row.get("_tz") or None)
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
    if not config.get("apikey"):
        raise ValueError("config lack apikey")
    print("pull events..")
    events = fetch_events(config)
    rows = [map_event(e) for e in events]
    tz = (COUNTRY_TZ.get((config.get("country_code") or "").strip().upper())
          or config.get("tz_name") or "America/New_York")
    rows = keep_on_dates(rows, config.get("start_date"), config.get("end_date"),
                         config.get("city", ""), tz_name=tz)
    for r in rows:                       # internal helper column -- not in CSV
        r.pop("_tz", None)
    print(f"  ticketmaster: {len(events)} from API -> {len(rows)} rows after "
          f"date filter (tz {tz})")
    out = config.get("out", "events.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Finish: {len(rows)} rows -> {out}")
    return rows
