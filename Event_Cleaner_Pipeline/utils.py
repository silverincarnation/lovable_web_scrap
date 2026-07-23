import csv
import re
import os
import hashlib
import unicodedata
import glob as globmod
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

try:
    from timezonefinder import TimezoneFinder
    import pytz
    HAS_TZ = True
except ImportError:
    HAS_TZ = False
    print("WARNING: timezonefinder or pytz not installed. UTC conversion disabled. Install with: pip install timezonefinder pytz")

try:
    from dateutil import parser as dateutil_parser
    HAS_DATEUTIL = True
except ImportError:
    HAS_DATEUTIL = False

tf = TimezoneFinder() if HAS_TZ else None

SOURCE_FIELDS = [
    "name", "description", "location_name", "latitude", "longitude",
    "address", "start_time", "end_time", "city", "primary_category",
    "secondary_categories", "thumbnail_image", "additional_images",
    "external_link", "is_paid",
]

TARGET_FIELDS = [
    "id", "name", "description", "location_name", "address",
    "latitude", "longitude", "start_time", "end_time", "city",
    "primary_category", "tags", "thumbnail_image", "additional_images",
    "external_link", "is_paid",
]

CATEGORY_KEYWORDS = [
    ("Food & Drink", [
        "food", "drink", "cooking", "chef", "cuisine", "tasting", "brewery",
        "wine", "beer", "cocktail", "dinner", "lunch", "brunch", "restaurant",
        "culinary", "cheese", "pizza", "taco", "ice cream", "chocolate",
        "bakery", "pastry", "coffee", "tea", "mixology", "dining", "bake",
        "baking", "supper", "feast", "foodie", "noodles", "sushi", "vegan",
        "vegetarian", "pickle", "farfalle", "cooking class", "food tour",
        "steak dinner", "bar", "dessert", "gourmet",
        "mezcal", "tequila", "taco tour", "mercado",
        "taco", "taqueria", "tortilla", "quesadilla", "tostada", "tamal",
        "pozole", "menudo", "birria", "carnitas", "al pastor", "chorizo",
        "chilaquiles", "enchilada", "mole", "salsa", "guacamole", "ceviche",
        "aguachile", "gordita", "sope", "huarache", "tlacoyo",
    ]),
    ("Music", [
        "concert", "live music", "band", "dj", "festival", "gig", "show",
        "music", "acoustic", "electronic", "rock", "jazz", "hip hop", "rap",
        "indie", "pop", "classical", "opera", "symphony", "orchestra",
        "karaoke", "open mic", "session", "vinyl", "record", "album",
        "reggaeton", "cumbia", "salsa", "banda", "norteno", "corrido",
        "mariachi", "ranchera", "banda sinaloense", "grupero",
    ]),
    ("Arts & Theatre", [
        "theatre", "theater", "play", "musical", "opera", "ballet", "dance",
        "comedy", "improv", "standup", "stand-up", "performance", "art",
        "exhibition", "gallery", "museum", "workshop", "class", "course",
        "painting", "drawing", "sculpture", "photography", "film", "cinema",
        "teatro", "obra", "monologo", "stand up", "improvisacion",
    ]),
    ("Sports & Fitness", [
        "sport", "fitness", "yoga", "running", "marathon", "cycling", "hiking",
        "climbing", "swimming", "gym", "workout", "training", "bootcamp",
        "crossfit", "pilates", "martial arts", "boxing", "mma", "wrestling",
        "soccer", "football", "basketball", "baseball", "tennis", "golf",
        "futbol", "ciclismo", "senderismo", "escalada",
    ]),
    ("Nightlife", [
        "club", "nightclub", "bar", "pub", "lounge", "rooftop", "speakeasy",
        "afterparty", "after-party", "late night", "dance", "edm", "techno",
        "house", "trance", "dnb", "drum and bass", "reggaeton", "perreo",
        "antro", "discoteca", "barra", "cantina", "mezcaleria",
    ]),
    ("Tech & Business", [
        "tech", "technology", "startup", "business", "networking", "conference",
        "workshop", "meetup", "hackathon", "coding", "programming", "ai",
        "machine learning", "data science", "blockchain", "crypto", "fintech",
        "emprendimiento", "conferencia", "taller",
    ]),
    ("Health & Wellness", [
        "health", "wellness", "meditation", "mindfulness", "therapy",
        "counseling", "psychology", "mental health", "spa", "massage",
        "holistic", "alternative medicine", "herbal", "naturopathy",
        "meditacion", "terapia", "psicologia",
    ]),
    ("Travel & Outdoor", [
        "travel", "tour", "hiking", "camping", "backpacking", "adventure",
        "excursion", "day trip", "weekend getaway", "road trip", "vanlife",
        "senderismo", "excursion", "viaje", "aventura",
    ]),
    ("Community & Social", [
        "community", "social", "meetup", "gathering", "volunteer", "charity",
        "fundraiser", "nonprofit", "ngo", "activism", "protest", "march",
        "community service", "neighborhood", "vecindario", "comunitario",
        "voluntariado", "beneficencia", "recaudacion",
    ]),
    ("Family & Kids", [
        "kids", "children", "family", "infantil", "ninos", "ninas", "familia",
        "kinder", "guarderia", "taller infantil", "cuentacuentos", "payaso",
    ]),
]

CATEGORY_KEYWORDS_FLAT = [(cat, kw.lower()) for cat, kws in CATEGORY_KEYWORDS for kw in kws]


def clean_text(text):
    if not text:
        return ""
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#x27;|&apos;|&#39;", "'", text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"&\w+;", "", text)
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201C", '"').replace("\u201D", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = text.replace("\u2026", "...")
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def generate_id(name, start_time, location):
    raw = f"{name}|{start_time}|{location}".lower().strip()
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def load_csv(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def parse_dt(dt_str):
    if not dt_str or not dt_str.strip():
        return None
    dt_str = dt_str.strip()
    if HAS_DATEUTIL:
        try:
            return dateutil_parser.parse(dt_str)
        except Exception:
            pass
    fmts = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            pass
    return None


def is_date_only(dt_str):
    if not dt_str:
        return False
    dt_str = dt_str.strip()
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", dt_str))


def format_dt(dt, tz):
    if dt is None:
        return ""
    if HAS_TZ:
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        else:
            dt = dt.astimezone(tz)
    return dt.strftime("%-m/%-d/%Y  %-I:%M %p")


def format_dt_utc(dt, tz, utc):
    if dt is None:
        return ""
    if HAS_TZ:
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        dt_utc = dt.astimezone(utc)
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        return dt.strftime("%Y-%m-%dT%H:%M:%S")


def infer_category(name, desc, primary_cat, secondary_cats):
    text = " ".join(filter(None, [name, desc, primary_cat, secondary_cats])).lower()
    for cat, kw in CATEGORY_KEYWORDS_FLAT:
        if kw in text:
            return cat
    cat = (primary_cat or "Other").strip().lower()
    if cat in ("undefined", "unknown", "n/a", "na", "none", ""):
        return "Other"
    return cat


def build_tags(primary_cat, secondary_cats):
    tags = []
    if primary_cat:
        tag = clean_text(primary_cat)
        if tag:
            tags.append(tag)
    if secondary_cats:
        for tag in secondary_cats.split(","):
            tag = clean_text(tag)
            if tag:
                tags.append(tag)
    return ", ".join(dict.fromkeys(tags))


def dedup_key(row):
    name = row.get("name", "").strip().lower()
    start = row.get("start_time", "").strip()
    loc = row.get("location_name", "").strip().lower()
    return f"{name}|{start}|{loc}"


def map_row(row, city_name, tz):
    start_dt = parse_dt(row.get("start_time", ""))
    end_dt = parse_dt(row.get("end_time", ""))
    end_raw = row.get("end_time", "").strip()
    if start_dt and not end_dt:
        end_dt = start_dt + timedelta(hours=2)
    elif start_dt and end_dt and is_date_only(end_raw):
        end_dt = start_dt + timedelta(hours=2)
    name = clean_text(row.get("name", ""))
    desc = clean_text(row.get("description", ""))
    loc = clean_text(row.get("location_name", ""))

    return {
        "id": generate_id(name, row.get("start_time", "").strip(), loc),
        "name": name,
        "description": desc,
        "location_name": loc,
        "address": clean_text(row.get("address", "")),
        "latitude": row.get("latitude", "").strip(),
        "longitude": row.get("longitude", "").strip(),
        "start_time": format_dt(start_dt, tz),
        "end_time": format_dt(end_dt, tz),
        "city": city_name,
        "primary_category": infer_category(
            name, desc,
            row.get("primary_category", ""),
            row.get("secondary_categories", "")
        ),
        "tags": build_tags(
            row.get("primary_category", ""),
            row.get("secondary_categories", "")
        ),
        "thumbnail_image": row.get("thumbnail_image", "").strip(),
        "additional_images": row.get("additional_images", "").strip(),
        "external_link": row.get("external_link", "").strip(),
        "is_paid": row.get("is_paid", "").strip().lower() in ("true", "1", "yes"),
    }


def geocode(address, city, region_query):
    if not HAS_TZ:
        return None, None
    query = f"{address}, {city}, {region_query}"
    url = f"https://nominatim.openstreetmap.org/search?{urllib.parse.urlencode({'q': query, 'format': 'json', 'limit': 1})}"
    req = urllib.request.Request(url, headers={"User-Agent": "EventCleaner/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None, None


def run_pipeline(output_path, input_arg, city_name, tz, region_query, do_geocode=False):
    if os.path.isdir(input_arg):
        input_paths = sorted(globmod.glob(os.path.join(input_arg, "**", "*.csv"), recursive=True))
        if not input_paths:
            print(f"No CSV files found in {input_arg}")
            return
    else:
        input_paths = [a for a in [input_arg] if not a.startswith("--")]

    all_rows = []
    for path in input_paths:
        all_rows.extend(load_csv(path))

    seen = {}
    unique = []
    for row in all_rows:
        key = dedup_key(row)
        if key not in seen:
            seen[key] = True
            unique.append(map_row(row, city_name, tz))

    print(f"Read {len(all_rows)} rows, kept {len(unique)} after dedup", flush=True)

    if do_geocode:
        missing = [r for r in unique if not r.get("latitude", "").strip() or r.get("latitude", "").strip() == "0"]
        print(f"Geocoding {len(missing)} events missing lat/long...", flush=True)
        geocoded = 0
        for i, r in enumerate(missing):
            lat, lon = geocode(r.get("address", ""), r.get("city", ""), region_query)
            if lat is not None:
                r["latitude"] = str(lat)
                r["longitude"] = str(lon)
                geocoded += 1
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(missing)} done ({geocoded} geocoded)", flush=True)
            time.sleep(1)
        print(f"Geocoded {geocoded}/{len(missing)} events", flush=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TARGET_FIELDS)
        writer.writeheader()
        writer.writerows(unique)

    print(f"Wrote {output_path}", flush=True)
