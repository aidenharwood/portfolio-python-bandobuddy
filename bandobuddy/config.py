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

# What a place *was*, shown as its map icon. First match wins; tested against the place's name, OSM
# kind, evidence text and Wikidata types. Order matters: a ruined chapel is a chapel, a railway
# tunnel is a tunnel, and anything else that's ruined falls through to "Ruins & castles".
CATEGORIES = [
    ("military", "Military", r"bunker|pillbox|military|barracks|\bfort\b|air[- ]raid|anti[- ]aircraft|\broc\b"
                             r"|blockhouse|aeroway|aerodrome|airfield|\braf\b|firing range|searchlight|gun emplacement"
                             r"|decoy"),
    ("underground", "Mines, caves & tunnels", r"\bmines?\b|\badit|shaft|quarr|\bcaves?\b|cave entrance|colliery|tunnel"
                                              r"|catacomb|underground"),
    ("rail", "Railways", r"railway|train station|\bhalt\b|viaduct|signal box|platform|\brail\b|tramway"),
    ("industrial", "Industrial", r"\bmills?\b|windmill|watermill|sawmill|factory|\bworks\b|brewery|distillery|maltings"
                                 r"|power station|power plant|\bpower\b|gasworks|warehouse|foundry|kiln|chimney"
                                 r"|industrial|pumping|engine house|brickworks|reservoir|water tower|depot|\bdocks?\b"
                                 r"|wharf|sewage|substation|brownfield|tannery"),
    ("religious", "Churches & chapels", r"church|chapel|place[ _]of[ _]worship|monastery|convent|abbey|priory|cathedral"
                                        r"|temple|mosque|synagogue|meeting house|methodist|baptist|wesleyan"
                                        r"|congregational|minster|mission hall"),
    ("institutional", "Hospitals & schools", r"hospital|asylum|sanator|infirmary|\bschool|college|university|prison"
                                             r"|gaol|\bjail|workhouse|orphanage|courthouse|police|fire station|town hall"
                                             r"|library|institute|clinic|nursing|care home|almshouse"),
    ("leisure", "Shops & leisure", r"\bshop|\bpub\b|\binn\b|hotel|motel|cinema|movie|theat|lido|swimming|\bpool\b"
                                   r"|bowling|amusement|casino|night ?club|restaurant|\bcafe|\bbank\b|supermarket"
                                   r"|\bmall\b|retail|commercial|office|fuel|petrol|filling station|toilets|\bclub\b"
                                   r"|pitch|sports|stadium|golf|tennis|ice rink|skating|holiday camp|leisure|tourism"),
    ("historic", "Ruins & castles", r"ruin|castle|tower|monument|archaeolog|\bhistoric\b|manor|mansion|folly"),
    ("buildings", "Houses & buildings", r"\bhouse\b|cottage|\bbarn\b|farm|residential|detached|terrace|apartments"
                                        r"|bungalow|\blodge\b|\bhall\b|building|dwelling|\bhut\b|\bshed\b|garage"),
]
OTHER_CATEGORY = ("other", "Other")

# How strong the evidence is. Not shown as a number any more: "weak" leads (closed shop units,
# heritage ruins, caves, brownfield...) are hidden unless asked for, and stronger leads win when
# the map is zoomed out.
STRENGTHS = [(35, "strong"), (20, "good"), (0, "weak")]
WEAK_BELOW = 20
