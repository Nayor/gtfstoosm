# GTFS to OSM Converter Usage Guide

This guide explains how to use the GTFS to OSM converter to transform public transit data from the General Transit Feed Specification (GTFS) format into OpenStreetMap (OSM) objects. The [README](../README.md) explains what it does and how to install it.

## Command-Line Interface

The only thing to give is the feed:

```bash
python -m gtfstoosm.cli /path/to/gtfs.zip
```

This writes `/path/to/gtfs.osm`, a file in JOSM's format. The OSM data around the stops of the feed is downloaded (and cached) as needed, `network` and `operator` are taken from the agency, and `gtfs:feed` from the name of the file. The options below change what is guessed.

### Output

- `FEED` (or `--input`, `-i`): the GTFS feed, a zip file
- `--output`, `-o`: the file to write (default: the feed's name with `.osm`)
- `--format osm|osc|both`: the file format of JOSM (default), osmChange (`.osc`, for osmium and scripts), or both side by side

### What to convert

- `--only stops|routes`: only the stops, or only the routes (default: both)
- `--modes MODE [MODE...]`: modes to convert, among `bus`, `trolleybus`, `tram`, `train`, `light_rail`, `subway`, `monorail`, `funicular`, `ferry`, `aerialway` (default: all of them)
  - Basic and extended GTFS route types are mapped to these modes; air, taxi and miscellaneous services are left out
- `--route-ref-pattern REGEX`: only the routes whose `route_id` matches. Most GTFS feeds use the route's reference as `route_id`, but some use a number
  - `"^[0-9]+$"`: numeric route IDs; `"^C"`: routes starting with C; `"^(10|20|30)$"`: routes 10, 20 or 30
- `--include-inactive`: also convert routes that no longer have service. By default a route is left out when none of its services runs today or later, except seasonal lines whose season ended less than a year ago (they come back) (this needs `calendar.txt` and/or `calendar_dates.txt`; without them nothing is filtered)
- `--no-new-stops`: do not create the stops that are missing from OSM
- `--no-service-tags`: do not estimate `duration`, `interval`, `opening_hours` and `interval:conditional`

Routes whose trips have no `stop_times` are converted from their shape, with ways and no stops, and a `fixme` tag. Routes with no trip, or with neither `stop_times` nor a shape, are listed in the log and left out.

### Tags

- `--feed-id ID`: the identifier of the feed, written in `gtfs:feed` (default: the name of the file, lowercased). Use a stable, meaningful one, for example `FR-IDF-IDFM`: it is how a later update finds the relations again, and [PTNA](https://community.openstreetmap.org/t/ptna-new-feature-gtfs-osm-comparison/109659) uses `gtfs:feed` and `gtfs:trip_id:sample` to compare a route with its GTFS trip
- `--network NAME`, `--operator NAME`: instead of the name of the agency of each route
- `--tag KEY=VALUE`: another tag for the route and route_master relations; can be repeated

### OpenStreetMap data

- `--osm-file FILE`: read existing stops and the ways followed by the shapes from this OSM extract (anything osmium reads: `.osm.pbf`, `.osm`...) instead of downloading one. Cut it to the area of the feed first (see the README)
- `--refresh`: download the OSM data again even if the cache is recent. The regions come from OSM France, then Geofabrik, as the README explains; the `robots.txt` of the sites is followed. The cache is `$GTFSTOOSM_CACHE`, `data/cache` with Docker, `~/.cache/gtfstoosm` otherwise; files less than 7 days old are used as they are, older ones are checked with a conditional request
- `--online`: ask the public Overpass and Valhalla servers instead (small networks only; only bus and trolleybus routes get ways)
- `--stop-search-radius METRES`: distance within which a GTFS stop is matched with an existing OSM stop (default: 10). GTFS positions are often 10 to 30 m off, so this one is worth adjusting. At most 100, and 10 with `--online` because Overpass has to look around every stop. The closest pairs are matched first

### Checking the feed

- `--no-validate`: do not check the feed. The full Docker image carries the [MobilityData GTFS validator](https://github.com/MobilityData/gtfs-validator) and checks every feed: the conversion stops on `ERROR` notices, warnings are logged, and the report is saved next to the result (`feed.validation.html`). Without the validator (lite image, native install) the feed is not checked
- `--validation-report report.json`: use this validator report instead of running the validator
- `--ignore-validation CODE [CODE...]`: error codes of the report that do not stop the conversion

### Logging

- `--verbose`, `-v`: detailed (DEBUG) log

## Examples

```bash
# everything guessed
python -m gtfstoosm.cli transit_agency.zip

# only the buses and trams with a numeric id, with a network name
python -m gtfstoosm.cli regional_transit.zip -o city.osm \
    --modes bus tram --route-ref-pattern '^[0-9]+$' --network "City Bus"

# the stops only, matched up to 25 m
python -m gtfstoosm.cli transit_agency.zip --only stops --stop-search-radius 25
```

With Docker, the feed and the result live in the `data` folder (see the README):

```bash
docker compose run --rm convert /data/transit_agency.zip
```

## Python API Usage

You can also use the converter as a Python library in your own code. Its defaults are the ones of the original project (online lookups, no guessed tags); the command line sets the options below:

```python
from gtfstoosm.convert import convert_gtfs_to_osm

# Basic conversion (Overpass and Valhalla)
convert_gtfs_to_osm('path/to/gtfs.zip', 'output.osc')

# What the command line does by default
convert_gtfs_to_osm(
    'path/to/gtfs.zip',  # gtfs_path
    'output.osc',  # osm_path
    auto_extract=True,  # download and cache the OSM data, instead of Overpass
    add_missing_stops=True,
    ptv2_names=True,
    frequency_tags=True,
    agency_tags=True,
    feed_id='my-feed',
    stop_search_radius=10.0,
)
```

The other options are `exclude_stops`, `exclude_routes`, `route_ref_pattern`, `relation_tags` (a dict), `modes` (a list), `active_on` (a `datetime.date`), `osm_file` and `refresh`.

## Output Format

The converter generates OSM XML containing (JOSM's format by default, osmChange with `--format osc`; the library writes osmChange unless `output_format` says otherwise):

1. Nodes for the transit stops that are not found in OSM, with tags:
   - `public_transport=platform`
   - `highway=bus_stop` (buses), or `railway=platform` (rail modes)
   - `name=*` (from GTFS stop_name)

2. Relations for routes with tags:
   - `type=route`, `public_transport:version=2`
   - `route=*` (bus, tram, train, etc. based on GTFS route_type)
   - `name=*` (`bus 1: Origin => Destination`), `ref=*` (from GTFS route_short_name), `from=*`, `to=*`
   - `network=*`, `operator=*` (the agency), `colour=*`
   - `gtfs:feed`, `gtfs:route_id`, `gtfs:trip_id:sample`
   - `duration`, and for regular services `interval`, `opening_hours` and `interval:conditional` (see the README)

3. Route master relations for grouping variants of the same route

## Importing to OSM

The generated OSM files are not meant to be uploaded directly to OpenStreetMap. Note that this application does not check for existing relations. Instead:

1. Use the JOSM editor to open the generated OSM file
2. Review and adjust the data as needed
3. Upload using proper changeset tags and comments

Always follow the [OSM Import Guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines) when importing any data to OpenStreetMap.

## Troubleshooting

1. **Invalid GTFS feed**: check it with a GTFS validator
2. **Missing required files**: the feed must contain at least `agency.txt`, `stops.txt`, `routes.txt`, `trips.txt` and `stop_times.txt`
3. **Large feeds**: convert fewer routes at once with `--route-ref-pattern`
4. **See what happens**: run with `--verbose`
