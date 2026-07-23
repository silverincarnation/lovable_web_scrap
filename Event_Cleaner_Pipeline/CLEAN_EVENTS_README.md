# Event Data Cleaning Scripts

Python scripts for cleaning and normalizing event data from scraped CSV files.

## Overview

These scripts process raw event data from Eventbrite and other sources, normalizing dates, categorizing events, deduplicating entries, and optionally geocoding missing coordinates.

## Scripts

- **clean_mx_events.py** - Processes Mexico City events
- **clean_ny_events.py** - Processes New York events

Both scripts share nearly identical functionality with city-specific configurations.

## Usage

```bash
# Basic usage
python clean_mx_events.py output.csv input_directory/

# With geocoding (adds missing lat/long coordinates)
python clean_mx_events.py output.csv input_directory/ --geocode

# Process specific CSV files
python clean_mx_events.py output.csv file1.csv file2.csv
```

## Features

### Date/Time Normalization
- Parses multiple date formats (ISO 8601, various datetime strings)
- Converts to local timezone format (Mexico City or New York)
- Optionally converts to UTC ISO format for API storage

### Category Inference
Automatically categorizes events based on keywords in title, description, and existing categories:

| Category | Example Keywords |
|----------|------------------|
| Food & Drink | food, restaurant, taco, coffee, mezcal |
| Music | concert, live music, band, dj, reggaeton |
| Arts & Theatre | theatre, exhibition, gallery, comedy |
| Sports & Fitness | fitness, yoga, running, soccer |
| Nightlife | club, bar, rooftop, dance |
| Tech & Business | tech, startup, conference, hackathon |
| Family & Kids | kids, children, family, infantil |
| Health & Wellness | wellness, meditation, spa |
| Travel & Outdoor | travel, tour, hiking, adventure |
| Community & Social | community, volunteer, meetup |

### Deduplication
Removes duplicate events based on:
- Event name
- Start time
- Location name

### Geocoding (Optional)
When `--geocode` flag is used:
- Queries OpenStreetMap Nominatim API for missing coordinates
- Rate-limited to 1 request per second
- Only geocodes events with missing or zero lat/long values

## Input Format

Expects CSV files with these columns:

| Column | Description |
|--------|-------------|
| name | Event title |
| description | Event description |
| location_name | Venue name |
| latitude | Latitude coordinate |
| longitude | Longitude coordinate |
| address | Street address |
| start_time | Event start datetime |
| end_time | Event end datetime |
| city | City name |
| primary_category | Event category |
| secondary_categories | Additional categories |
| thumbnail_image | Image URL |
| additional_images | More image URLs |
| external_link | Event page URL |
| is_paid | Whether event is paid (true/false) |

## Output Format

Cleaned CSV with normalized fields:

| Column | Description |
|--------|-------------|
| id | Empty (for API assignment) |
| name | Cleaned event title |
| description | Cleaned description |
| location_name | Venue name |
| address | Street address |
| latitude | Latitude (or geocoded value) |
| longitude | Longitude (or geocoded value) |
| start_time | Normalized datetime (local format) |
| end_time | Normalized datetime (local format) |
| city | City name (hardcoded) |
| primary_category | Inferred or original category |
| tags | Combined category tags |
| thumbnail_image | Image URL |
| additional_images | More image URLs |
| external_link | Event page URL |
| is_paid | Boolean |

## Dependencies

```bash
pip install timezonefinder pytz python-dateutil
```

All dependencies are optional but recommended:
- **timezonefinder** + **pytz**: Enables timezone conversion
- **python-dateutil**: Improves datetime parsing

## Examples

```bash
# Clean Mexico City events from a folder
python clean_mx_events.py output_mx.csv Mexico_City_MX/

# Clean New York events with geocoding
python clean_ny_events.py output_ny.csv ny_input/ --geocode

# Process a single file
python clean_mx_events.py clean_events.csv raw_events.csv
```

## Environment Variables

None required. All configuration is done via command-line arguments.

## Notes

- The `--geocode` flag makes HTTP requests to OpenStreetMap (rate-limited)
- Large directories may take several minutes to process with geocoding
- Output CSV uses UTF-8 encoding for international characters
