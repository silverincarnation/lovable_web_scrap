import sys
try:
    from .utils import HAS_TZ, run_pipeline
except ImportError:
    from utils import HAS_TZ, run_pipeline

try:
    import pytz
    MEXICO_CITY_TZ = pytz.timezone("America/Mexico_City") if HAS_TZ else None
except ImportError:
    MEXICO_CITY_TZ = None

CITY = "Mexico City"
REGION = "Mexico"


def main():
    if len(sys.argv) < 3:
        print("Usage: python clean_mx_events.py <output.csv> <input_dir_or_csv> [--geocode]")
        sys.exit(1)
    run_pipeline(
        output_path=sys.argv[1],
        input_arg=sys.argv[2],
        city_name=CITY,
        tz=MEXICO_CITY_TZ,
        region_query=REGION,
        do_geocode="--geocode" in sys.argv,
    )


if __name__ == "__main__":
    main()
