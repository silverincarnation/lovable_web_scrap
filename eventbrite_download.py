"""eventbrite.com scraper -- self-contained, no API key.

Eventbrite's server-rendered search pages used to embed schema.org Event
JSON-LD; they removed that sitewide, so this reads the events out of the
``window.__SERVER_DATA__`` blob that every search page still ships in its raw
HTML (``search_data.events.results``). Stdlib only -- no BeautifulSoup.

URL:   https://www.eventbrite.com/d/{place}/{slug}/?page=N&start_date=...&...
Place: "{country_code}--{city}" (e.g. mx--mexico-city); Eventbrite redirects it
to its canonical form (mexico--mexico-city) and urllib follows the redirect.

Same date-window filter / de-dupe / city back-fill as the other scrapers.
"""

import csv
import datetime
import json
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


SEARCH_URL = "https://www.eventbrite.com/d/{place}/{slug}/"

COLUMNS = [
    "name", "description", "location_name", "latitude", "longitude", "address",
    "start_time", "end_time", "city", "primary_category", "secondary_categories",
    "thumbnail_image", "additional_images", "external_link", "is_paid",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
}

_SD_MARKER = "window.__SERVER_DATA__"

# The search page's __SERVER_DATA__ no longer carries is_free / prices, but the
# site's own frontend fetches them per event id from this public endpoint
# (no API key needed) -- we do the same to fill is_paid.
DEST_API = "https://www.eventbrite.com/api/v3/destination/events/"


def get(d, *keys, default=""):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d if d is not None else default


def _date_only(t):
    if not t:
        return ""
    return str(t).split("T")[0]


def _place_slug(config):
    if config.get("place"):
        return config["place"]
    city = (config.get("city") or "").strip().lower().replace(" ", "-")
    country = (config.get("country_code") or "").strip().lower()
    if city and country:
        return f"{country}--{city}"
    return city or "online"


def build_url(config, page):
    place = _place_slug(config)
    slug = config.get("slug", "all-events")
    params = {"page": page}
    q = config.get("q") or config.get("keyword")
    if q:
        params["q"] = q
    start_date = config.get("start_date") or _date_only(config.get("start_time"))
    end_date = config.get("end_date") or _date_only(config.get("end_time"))
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    url = SEARCH_URL.format(place=place, slug=slug)
    return url + "?" + urllib.parse.urlencode(params)


def fetch_html(url, retries=6):
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < retries - 1:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after and str(retry_after).isdigit():
                    wait = int(retry_after)
                else:
                    wait = min(60, 10 * (2 ** attempt))
                print(f"  rate limited ({e.code}), waiting {wait}s then retrying...")
                time.sleep(wait)
                continue
            raise
    return ""


def extract_server_data(html):
    """Pull the JSON assigned to ``window.__SERVER_DATA__`` out of the raw HTML.

    Scans from the marker's first ``{`` and brace-matches (string-aware) to the
    matching ``}`` so nested objects don't trip us up. Returns a dict or None.
    """
    i = html.find(_SD_MARKER)
    if i < 0:
        return None
    i = html.find("{", i)
    if i < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    start = i
    for j in range(i, len(html)):
        c = html[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:j + 1])
                    except ValueError:
                        return None
    return None


def extract_events_from_html(html):
    data = extract_server_data(html)
    if not data:
        return []
    results = get(data, "search_data", "events", "results", default=[])
    return results if isinstance(results, list) else []


def _text(value):
    if isinstance(value, dict):
        return value.get("text") or value.get("html") or ""
    return value or ""


def _combine(date, tm):
    date = str(date or "").strip()
    tm = str(tm or "").strip()
    if not date:
        return ""
    if re.fullmatch(r"\d{2}:\d{2}", tm):
        return f"{date}T{tm}:00"
    return date


def map_event(event):
    venue = event.get("primary_venue") or {}
    addr = venue.get("address") or {}
    if not isinstance(addr, dict):
        addr = {}

    image = event.get("image") or {}
    orig_url = ""
    if isinstance(image, dict):
        img_url = image.get("url", "")
        orig = image.get("original")
        if isinstance(orig, dict):
            orig_url = orig.get("url", "")
    elif isinstance(image, str):
        img_url = image
    else:
        img_url = ""

    tags = event.get("tags") or []
    tag_names = []
    for t in tags:
        if isinstance(t, dict):
            name = t.get("display_name") or t.get("name") or _text(t.get("text"))
            if name:
                tag_names.append(str(name))

    # Paid / free: `is_free` vanished from the current __SERVER_DATA__ payload,
    # so also check ticket_availability (flag first, then min/max ticket price).
    is_free = event.get("is_free")
    ta = event.get("ticket_availability")
    if not isinstance(ta, dict):
        ta = {}
    if is_free is None:
        is_free = ta.get("is_free")
    if is_free is None:
        prices = []
        for k in ("minimum_ticket_price", "maximum_ticket_price"):
            p = ta.get(k)
            v = p.get("major_value") if isinstance(p, dict) else None
            try:
                if v is not None:
                    prices.append(float(v))
            except (TypeError, ValueError):
                pass
        if prices:
            is_free = max(prices) == 0
    if is_free is None:
        is_paid = ""
    else:
        is_paid = "false" if is_free else "true"

    address = (addr.get("localized_address_display")
               or ", ".join([p for p in (addr.get("address_1"), addr.get("city"))
                             if p]))

    return {
        "name": _text(event.get("name")),
        "description": _text(event.get("summary") or event.get("full_description")),
        "location_name": venue.get("name", "") if isinstance(venue, dict) else "",
        "latitude": str(addr.get("latitude") or ""),
        "longitude": str(addr.get("longitude") or ""),
        "address": address or "",
        "start_time": _combine(event.get("start_date"), event.get("start_time")),
        "end_time": _combine(event.get("end_date"), event.get("end_time")),
        "city": str(addr.get("city") or "").lower(),
        "primary_category": tag_names[0] if tag_names else "",
        "secondary_categories": ",".join(tag_names[1:]),
        "thumbnail_image": img_url,
        "additional_images": (orig_url
                              if orig_url and orig_url != img_url else ""),
        "external_link": event.get("url", ""),
        "is_paid": is_paid,
    }


def fetch_ticket_availability(ids, delay=0.3):
    """{event id -> ticket_availability} via Eventbrite's destination API.

    Verified live (2026-07): GET DEST_API?event_ids=...&expand=ticket_availability
    returns ticket_availability with is_free + min/max ticket prices per event.
    Fails soft: on any error the remaining events just keep is_paid = "".
    """
    out = {}
    ids = [str(i) for i in ids if i]
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        url = (DEST_API + "?" + urllib.parse.urlencode(
            {"event_ids": ",".join(chunk),
             "expand": "ticket_availability", "page_size": len(chunk)}))
        req = urllib.request.Request(
            url, headers={**HEADERS, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as e:
            print(f"  eventbrite: ticket availability lookup failed ({e}); "
                  f"is_paid stays blank for {len(ids) - i} event(s)")
            break
        for ev in data.get("events") or []:
            if isinstance(ev, dict) and ev.get("id"):
                out[str(ev["id"])] = ev.get("ticket_availability")
        time.sleep(delay)
    return out


def fetch_events(config):
    events, seen = [], set()
    max_pages = config.get("max_pages") or 50
    delay = config.get("delay", 1.5)
    total_pages = None
    for page in range(1, max_pages + 1):
        url = build_url(config, page)
        try:
            html = fetch_html(url)
        except urllib.error.HTTPError as e:
            print(f"  stopped at page {page}: HTTP {e.code}, "
                  f"keeping {len(events)} events so far")
            break
        data = extract_server_data(html)
        batch = get(data, "search_data", "events", "results", default=[]) if data else []
        if total_pages is None:
            total_pages = (data or {}).get("page_count") or 1
        new = 0
        for ev in (batch or []):
            key = ev.get("url") or ev.get("id") or ev.get("name")
            if key and key in seen:
                continue
            seen.add(key)
            events.append(ev)
            new += 1
        print(f"  page {page}/{total_pages}: {len(batch or [])} found, "
              f"{new} new (total {len(events)})")
        time.sleep(delay)
        if new == 0 or page >= (total_pages or 1):
            break
    return events


# --------------------------------------------------------------------------- #
# Date-window filter + de-dupe + city back-fill (shared with the other scrapers)
# --------------------------------------------------------------------------- #
def keep_on_dates(rows, start_date=None, end_date=None, city="",
                  tz_name="America/Mexico_City"):
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
    need = [e.get("id") for e in events
            if isinstance(e, dict) and not e.get("ticket_availability")]
    ta_map = fetch_ticket_availability(need) if need else {}
    if ta_map:
        print(f"  eventbrite: ticket availability for "
              f"{len(ta_map)}/{len(events)} events")
    for e in events:
        if isinstance(e, dict) and not e.get("ticket_availability"):
            ta = ta_map.get(str(e.get("id")))
            if ta:
                e["ticket_availability"] = ta
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
