"""Settings and vocabulary. Everything bandobuddy uses is free and open: OpenStreetMap, Wikidata,
Wikipedia, Nominatim and Panoramax - no accounts or API keys."""
from __future__ import annotations

import os
from pathlib import Path

USER_AGENT = "bandobuddy/0.2 (open-source urbex map; https://www.openstreetmap.org/copyright)"

# Where the database and the OSM extract live. The extract is ~2.3 GB for the UK.
DATA_DIR = Path(os.environ.get("BANDOBUDDY_DATA") or Path.home() / ".bandobuddy")
DB_NAME = "bandobuddy.db"

# Whole-UK coverage (Great Britain, Northern Ireland, Channel Islands, Isle of Man).
UK_BBOX = (49.8, -8.7, 60.95, 1.9)  # south, west, north, east
GEOFABRIK_UK_URL = "https://download.geofabrik.de/europe/united-kingdom-latest.osm.pbf"

DEFAULT_UPDATE_DAYS = 7
DEFAULT_MIN_SCORE = 15

# Building kinds usually worth the trip if they're dead. Matched exactly against OSM kinds and
# Wikidata types (so "sports_school" or "driving_school" don't count as schools).
URBEX_TYPES = {
    "hospital", "school", "primary_school", "secondary_school", "university", "college",
    "church", "chapel", "place_of_worship", "monastery", "hotel", "motel", "resort_hotel",
    "movie_theater", "cinema", "theatre", "performing_arts_theater", "shopping_mall",
    "department_store", "amusement_park", "water_park", "bowling_alley", "ice_skating_rink",
    "stadium", "arena", "train_station", "station", "casino", "night_club", "nightclub",
    "swimming_pool", "prison", "barracks", "bunker", "factory", "works", "mill", "warehouse",
    "power_plant", "brewery", "mine", "quarry", "castle", "manor", "nursing_home",
    "adit", "mineshaft", "pillbox",
}
# Whole words/phrases in a place's name that point at an explorable building.
URBEX_NAME_WORDS = [
    "asylum", "sanatorium", "sanitorium", "infirmary", "workhouse", "orphanage", "hospital",
    "mill", "factory", "foundry", "ironworks", "gasworks", "brickworks", "colliery", "brewery",
    "distillery", "power station", "pumping station", "cinema", "theatre", "theater", "lido",
    "barracks", "bunker", "fort", "castle", "abbey", "chapel", "church", "hotel", "motel",
    "manor house", "mansion", "quarry", "mine", "mines", "railway station", "ice rink", "bowling",
]
DEAD_NAME_WORDS = ["abandoned", "derelict", "disused", "defunct", "ruin", "ruins", "former", "closed down"]

# Categories for filtering, first match wins. Tested against a place's kind, evidence and name.
CATEGORIES = [
    ("military", "Military", r"bunker|pillbox|military|barracks|\bfort\b|air[- ]raid|anti[- ]aircraft|\broc\b|blockhouse"
                             r"|aeroway|aerodrome|airfield|\braf\b"),
    ("underground", "Underground", r"\bmines?\b|\badit|shaft|quarr|\bcaves?\b|cave entrance|colliery|tunnel|catacomb"),
    ("rail", "Rail", r"railway|\bstation\b|\bhalt\b|viaduct|signal box|platform|\brail\b"),
    ("industrial", "Industrial", r"\bmill|factory|\bworks\b|brewery|distillery|power|gasworks|warehouse|foundry|kiln"
                                 r"|chimney|industrial|pumping|engine house|brickworks|reservoir|water tower"),
    ("ruins", "Ruins", r"ruin|castle|abbey|priory"),
    ("buildings", "Buildings", r"hospital|asylum|sanator|school|church|chapel|hotel|cinema|theat|prison|workhouse"
                               r"|\bhouse\b|manor|mansion|\bbarn\b|farm|\bpub\b|shop|office|hall|institute|toilets"
                               r"|building|amenity|detached|terrace|apartments|retail|commercial|tower"),
]
OTHER_CATEGORY = ("other", "Other")
