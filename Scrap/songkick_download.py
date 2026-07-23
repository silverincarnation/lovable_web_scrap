"""songkick.com scraper -- public metro-area pages, parsed from JSON-LD.

No API key needed. Songkick stopped issuing new API keys, but each metro-area
page is server-side rendered and embeds every listed concert as a
``<script type="application/ld+json">`` MusicEvent block (name, startDate,
venue, geo, address, performers, ticket url). We fetch those pages and parse the
JSON-LD -- same standalone style as the other scrapers, plus the shared
date-window filter.

Songkick locates by a numeric *metro area id*. Mexico City = 34385 (verified:
``songkick.com/metro-areas/34385-mexico-mexico-city``). Pass another city's id
via config["metro_id"] / ["area_id"] / ["place"], or add it to METRO_SLUGS.

Coverage is live music / concerts (mainstream, rock, pop, latin, festivals,
family shows), so it complements the RA scraper (electronic/club only) rather
than duplicating it. Events are listed in ascending date order, so we can stop
paging once a page runs past the requested end date.
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


SITE = "https://www.songkick.com"

COLUMNS = [
    "name", "description", "location_name", "latitude", "longitude", "address",
    "start_time", "end_time", "city", "primary_category", "secondary_categories",
    "thumbnail_image", "additional_images", "external_link", "is_paid",
]

# City name -> Songkick metro-area slug ("<id>-<slug>"). The id alone also works
# (Songkick redirects /metro-areas/<id> to the full slug), so a bare numeric id
# from config is fine too. Cities not listed here are looked up automatically
# via Songkick's search (see _lookup_metro), so this map is just a fast path for
# the common ones (verified against songkick.com/metro-areas/<id>-<slug>).
METRO_SLUGS = {
    "new york": "7644-us-new-york-nyc",
    "new york city": "7644-us-new-york-nyc",
    "nyc": "7644-us-new-york-nyc",
    "new york, ny": "7644-us-new-york-nyc",
    "manhattan": "7644-us-new-york-nyc",
    "brooklyn": "7644-us-new-york-nyc",
    "mexico city": "34385-mexico-mexico-city",
    "cdmx": "34385-mexico-mexico-city",
    "ciudad de mexico": "34385-mexico-mexico-city",
    "ciudad de méxico": "34385-mexico-mexico-city",
    # Verified live via songkick.com/search (2026-07):
    "los angeles": "17835-us-los-angeles-la",
    "guadalajara": "31015-mexico-guadalajara",
    "monterrey": "31051-mexico-monterrey",
}

# Matches a Songkick metro-area link/slug like "7644-us-new-york-nyc".
_METRO_RE = re.compile(r"/metro-areas/(\d+-[a-z0-9\-]+)", re.IGNORECASE)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
}

_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def get(d, *keys, default=""):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d if d is not None else default


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


def _lookup_metro(city, country_code=""):
    """Resolve a city name to a Songkick metro slug via the site's search page.

    Songkick's search results embed ``/metro-areas/<id>-<country>-<city>`` links;
    we pull them out and pick the best match by country code + city name. Returns
    a slug like "7644-us-new-york-nyc", or None if nothing suitable is found.
    """
    term = (city or "").strip()
    if not term:
        return None
    url = (SITE + "/search?"
           + urllib.parse.urlencode({"query": term, "type": "locations"}))
    try:
        html = fetch_html(url)
    except Exception as e:
        print(f"  songkick: metro lookup failed for '{city}': {e}")
        return None

    slugs, seen = [], set()
    for slug in _METRO_RE.findall(html or ""):
        slug = slug.lower()
        if slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    if not slugs:
        return None

    cc = (country_code or "").strip().lower()
    # Songkick slugs use country *words*, not ISO codes ("mexico-guadalajara",
    # "uk-london") -- map the ISO code so the country bonus actually applies.
    cc_word = {"us": "us", "mx": "mexico", "ca": "canada", "gb": "uk",
               "uk": "uk", "es": "spain", "ar": "argentina", "cl": "chile",
               "co": "colombia", "br": "brazil", "de": "germany",
               "fr": "france", "it": "italy", "au": "australia",
               "jp": "japan", "nl": "netherlands"}.get(cc, cc)
    city_key = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")

    def score(slug):
        parts = slug.split("-")
        s = 0
        # City name is the primary signal; the country word breaks ties
        # (Guadalajara MX vs Guadalajara ES). Search pages also embed footer
        # links to unrelated metros -- those score 0 and are never picked
        # over a real match.
        if city_key and city_key in slug:
            s += 2
        if cc_word and len(parts) >= 2 and parts[1] == cc_word:
            s += 1
        return s

    best = max(slugs, key=score)
    print(f"  songkick: resolved '{city}' -> metro {best}")
    return best


def _metro(config):
    """Resolve a Songkick metro path segment from config.

    Order: explicit config id -> the built-in METRO_SLUGS map -> auto-lookup by
    city name via Songkick search.
    """
    for key in ("metro_id", "area_id", "place"):
        v = str(config.get(key) or "").strip()
        if v:
            return v  # numeric id (redirects) or a full slug
    mapped = METRO_SLUGS.get((config.get("city") or "").strip().lower())
    if mapped:
        return mapped
    return _lookup_metro(config.get("city"), config.get("country_code"))


def fetch_html(url):
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ""
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    return ""


def _iter_ld(html):
    """Yield each JSON-LD object embedded in the page (flattening @graph)."""
    for block in _LD_RE.findall(html):
        block = block.strip()
        if not block:
            continue
        try:
            data = json.loads(block)
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("@graph"), list):
                for sub in item["@graph"]:
                    if isinstance(sub, dict):
                        yield sub
            else:
                yield item


def _is_event(item):
    t = item.get("@type", "")
    if isinstance(t, list):
        return any("event" in str(x).lower() for x in t)
    return "event" in str(t).lower()


def parse_events(html):
    return [it for it in _iter_ld(html) if _is_event(it)]


def _first(value):
    """location/image can be a dict/str or a list of them -- take the first."""
    if isinstance(value, list):
        return value[0] if value else {}
    return value if value is not None else {}


def map_event(event):
    loc = _first(event.get("location")) or {}
    if not isinstance(loc, dict):
        loc = {}
    addr = loc.get("address") or {}
    if not isinstance(addr, dict):
        addr = {}
    geo = loc.get("geo") or {}
    if not isinstance(geo, dict):
        geo = {}

    performers = event.get("performer") or []
    if isinstance(performers, dict):
        performers = [performers]
    artists = [p.get("name", "") for p in performers
               if isinstance(p, dict) and p.get("name")]

    images = event.get("image") or []
    if isinstance(images, str):
        images = [images]
    images = [i for i in images if isinstance(i, str)]

    offers = event.get("offers") or []
    if isinstance(offers, dict):
        offers = [offers]
    is_paid = "true" if offers else ""

    street = addr.get("streetAddress", "")
    locality = addr.get("addressLocality", "")
    address = ", ".join([p for p in (street, locality) if p])

    lat = geo.get("latitude")
    lon = geo.get("longitude")

    return {
        "name": event.get("name", ""),
        "description": strip_html(event.get("description", "")),
        "location_name": loc.get("name", ""),
        "latitude": "" if lat is None else str(lat),
        "longitude": "" if lon is None else str(lon),
        "address": address,
        "start_time": event.get("startDate", ""),
        # Songkick's endDate carries no time (bare date = midnight before the
        # show starts) -- keep it only if it has a time component.
        "end_time": (event.get("endDate", "")
                     if "T" in str(event.get("endDate", "")) else ""),
        "city": str(locality or "").lower(),
        "primary_category": "Music",
        "secondary_categories": ",".join(artists),
        "thumbnail_image": images[0] if images else "",
        "additional_images": ",".join(images[1:]),
        "external_link": event.get("url", ""),
        "is_paid": is_paid,
    }


def _date_only(value):
    s = str(value or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return datetime.date.fromisoformat(m.group(1)) if m else None


def fetch_events(config):
    metro = _metro(config)
    if not metro:
        print(f"  songkick: no metro id for '{config.get('city')}' "
              f"(Mexico City = 34385; set config['metro_id']).")
        return []

    base = f"{SITE}/metro-areas/{metro}"
    end = config.get("end_date") or config.get("start_date") or ""
    end_date = datetime.date.fromisoformat(end) if end else None

    page_cap = min(int(config.get("max_pages") or 40), 40)
    events, seen = [], set()
    page = 1
    while page <= page_cap:
        url = base if page == 1 else f"{base}?page={page}"
        html = fetch_html(url)
        batch = parse_events(html) if html else []
        new, page_max_day = 0, None
        for ev in batch:
            key = ev.get("url") or (ev.get("name"), ev.get("startDate"))
            if key in seen:
                continue
            seen.add(key)
            events.append(ev)
            new += 1
            d = _date_only(ev.get("startDate"))
            if d and (page_max_day is None or d > page_max_day):
                page_max_day = d
        print(f"  songkick page {page}: {len(batch)} found, {new} new "
              f"(total {len(events)})")
        # Stop: empty/duplicate page, or -- since events are date-sorted -- once
        # this page has run past the requested window.
        if new == 0:
            break
        if end_date and page_max_day and page_max_day > end_date:
            break
        page += 1
        time.sleep(0.5)
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
