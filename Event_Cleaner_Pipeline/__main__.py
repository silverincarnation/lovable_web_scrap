import sys
from .utils import HAS_TZ, run_pipeline

try:
    import pytz
    NY_TZ = pytz.timezone("America/New_York") if HAS_TZ else None
    MX_TZ = pytz.timezone("America/Mexico_City") if HAS_TZ else None
except ImportError:
    NY_TZ = None
    MX_TZ = None

CITIES = {
    "ny": {"city": "New York", "tz": NY_TZ, "region": "New York, USA"},
    "mx": {"city": "Mexico City", "tz": MX_TZ, "region": "Mexico"},
}


def main():
    if len(sys.argv) < 4:
        print("Usage: python -m Event_Cleaner_Pipeline <ny|mx> <output.csv> <input_dir_or_csv> [--geocode]")
        sys.exit(1)

    city_key = sys.argv[1].lower()
    if city_key not in CITIES:
        print(f"Unknown city: {city_key}. Choose from: {', '.join(CITIES.keys())}")
        sys.exit(1)

    cfg = CITIES[city_key]
    run_pipeline(
        output_path=sys.argv[2],
        input_arg=sys.argv[3],
        city_name=cfg["city"],
        tz=cfg["tz"],
        region_query=cfg["region"],
        do_geocode="--geocode" in sys.argv,
    )


if __name__ == "__main__":
    main()
