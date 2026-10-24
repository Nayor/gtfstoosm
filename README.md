# GTFS to OSM Converter

Turns a public transport feed ([GTFS](https://gtfs.org/)) into a file of OpenStreetMap
[public transport v2](https://wiki.openstreetmap.org/wiki/Public_transport) objects
(route relations, route masters and, optionally, stops) that you review and upload
with [JOSM](https://josm.openstreetmap.de/).

It is a helper for mappers, not an automatic importer: it never uploads anything,
and every result has to be checked by a person before it goes into OpenStreetMap.

## What it does

For each route of the feed, and for each distinct sequence of stops used by its trips:

1. **Routes**: keeps the routes of the modes asked for (all of them by default, see
   below) and leaves out closed lines, i.e. routes without any service from today on.
2. **Stops**: looks for an existing OSM stop of the right kind (bus stop, tram stop,
   railway station, ferry terminal...) within a few metres of each GTFS stop. With
   `--add-missing-stops`, stops that are not found are added as new nodes.
3. **Ways**: follows the trip's shape (`shapes.txt`) on the network and lists the OSM
   ways it uses. Without `--osm-file` this is only done for bus and trolleybus routes
   (with Valhalla). Feeds without `shapes.txt` (most Swiss feeds) get their stops only.
4. **Relations**: writes a `type=route` relation (stops as `platform` members, then
   ways if any) per stop sequence, and a `type=route_master` relation grouping them.

Modes: `bus`, `trolleybus`, `tram`, `train`, `light_rail`, `subway`, `monorail`,
`funicular`, `ferry` and `aerialway` (gondolas, cable cars). Both the basic GTFS route
types and the extended ones (such as 100-117 for rail, 700 for buses, 900 for trams,
1400 for funiculars) are understood. Air, taxi and miscellaneous services are left out.

The result is an [osmChange](https://wiki.openstreetmap.org/wiki/OsmChange) file
(`.osc`) whose new objects have negative IDs.

What it does **not** do:

- It does not check whether the relations already exist in OSM: look before you add.
- It does not upload anything.
- Without `--osm-file` it does not find the track of trams, trains and the like: only
  bus and trolleybus routes get ways. Use an extract, or add them in JOSM. Feeds
  without shapes can get them from [pfaedle](#quick-start-with-docker-any-os).
- Stops of modes other than buses are only matched and created with minimal tags
  (`public_transport=platform`, plus `railway=platform` for rail modes): check them.

### Offline mode (recommended): `--osm-file`

With `--osm-file extract.osm.pbf`, stops and ways are looked up in a local OpenStreetMap
extract and **nothing is sent over the network**. It also works for tram, train and
the other modes: their ways are found from the shapes of the feed, on the rails.
Cut the extract to the area of the feed first, otherwise reading it takes a lot of
memory:

```bash
# Linux/macOS/Windows with Docker; region.osm.pbf is e.g. a Geofabrik download in ./data
docker compose --profile tools run --rm osmium extract     -b 5.9,45.8,10.5,47.9 /data/region.osm.pbf -o /data/area.osm.pbf
docker compose run --rm convert -i /data/feed.zip -o /data/routes.osc     --osm-file /data/area.osm.pbf
```

(`-b` is `min_lon,min_lat,max_lon,max_lat`; use the bounding box of your feed, a few
kilometres larger.) Ways are found by snapping the (densified) points of each shape
to the nearest way that can carry the mode. This is simple and fast but it is not a
full map matching: a street that a shape only crosses may be picked up, and a shape
that is far off the roads gets gaps. Check the relations in JOSM.

### Online services (without `--osm-file`)

Otherwise the lookups are made on public servers, so please convert one network at a
time and avoid repeated runs on large feeds. These addresses are written in
`convert.py`.

| What | Service | What is sent |
|---|---|---|
| Existing OSM stops | [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API) instance `maps.mail.ru` | Coordinates of the stops |
| Ways followed by a bus trip | [Valhalla](https://valhalla.github.io/valhalla/) instance `valhalla1.openstreetmap.de` | Points of the trip shape |



## Quick start with Docker (any OS)

Docker is the easiest way to run the tool: nothing else to install, the same result
on Linux, macOS and Windows.

1. Install Docker (see [Prerequisites](#prerequisites)).
2. Get the code and create a folder for your files:

   ```bash
   git clone https://github.com/whubsch/gtfstoosm.git
   cd gtfstoosm
   mkdir data
   ```

3. Copy your GTFS feed (a `.zip`) into `data/`, then run:

   ```bash
   docker compose run --rm convert -i /data/feed.zip -o /data/routes.osc
   ```

   The first run builds the image, which takes a few minutes. `data/routes.osc` is
   the result.

4. Open `routes.osc` in JOSM (see [Reviewing and uploading](#reviewing-and-uploading)).

On Windows with **Git Bash**, put `MSYS_NO_PATHCONV=1` in front of the command,
otherwise `/data/...` is rewritten into a Windows path. PowerShell and cmd do not
need it.

Other commands:

```bash
docker compose run --rm convert --help                  # all options
docker compose --profile dev run --rm test              # tests (no internet needed)
docker compose --profile dev run --rm lint              # ruff
docker compose --profile tools run --rm osmium --help   # osmium-tool, to cut OSM extracts
docker compose --profile tools run --rm pfaedle -x /data/region.osm.pbf /data/feed.zip
```

[`pfaedle`](https://github.com/ad-freiburg/pfaedle) (GPL-3.0, official image) generates
`shapes.txt` for feeds that lack them, from an OSM extract. It writes a copy of the feed
to `data/gtfs-out`.

## Prerequisites

| | With Docker (recommended) | Without Docker |
|---|---|---|
| **Linux** | [Docker Engine](https://docs.docker.com/engine/install/) with the Compose plugin (`docker compose version` must work) | Python 3.12, `python3-venv`, git |
| **macOS** | [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) | Python 3.12 (for instance `brew install python@3.12`), git |
| **Windows** | [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) with the WSL 2 backend, git | Python 3.12 from [python.org](https://www.python.org/downloads/), git |

In both cases you also need [JOSM](https://josm.openstreetmap.de/) to review the result
and an OpenStreetMap account to upload it.

## Installation without Docker

```bash
git clone https://github.com/whubsch/gtfstoosm.git
cd gtfstoosm
python -m venv .venv
```

Activate the environment:

- Linux and macOS: `source .venv/bin/activate`
- Windows (PowerShell): `.venv\Scripts\Activate.ps1`
- Windows (cmd): `.venv\Scripts\activate.bat`

Then install and run:

```bash
pip install .
python -m gtfstoosm.cli --input path/to/feed.zip --output routes.osc
```

If `pip` tries to compile the `regex` package and fails on Windows with
*"Microsoft Visual C++ 14.0 or greater is required"*, your Python version has no
prebuilt package yet: use Python 3.12, or Docker.

## Usage

```bash
# whole feed, with the stops missing from OSM added
python -m gtfstoosm.cli -i feed.zip -o routes.osc --add-missing-stops

# only the routes whose route_id is numeric, tagged with their network
python -m gtfstoosm.cli -i feed.zip -o routes.osc \
    --route-ref-pattern '^[0-9]+$' \
    --relation-tags 'network=My Bus;operator=My Agency'
```

The main options (all of them are in [docs/usage.md](docs/usage.md)):

| Option | Effect |
|---|---|
| `-i`, `--input` | GTFS feed (`.zip`) |
| `-o`, `--output` | Output `.osc` file |
| `--add-missing-stops` | Add stops that are not in OSM as new nodes |
| `--exclude-stops`, `--exclude-routes` | Leave the stops, or the routes, out of the output |
| `--route-ref-pattern REGEX` | Only convert routes whose `route_id` matches |
| `--osm-file FILE` | Read stops and ways from a local OSM extract instead of Overpass and Valhalla (no network) |
| `--modes MODE [MODE...]` | Only convert these modes (default: all supported modes) |
| `--active-on YYYY-MM-DD` | Leave out routes without service from this day on (default: today) |
| `--include-inactive` | Convert routes even if they no longer have service |
| `--relation-tags 'k=v;k=v'` | Tags added to the route and route_master relations |
| `--stop-search-radius M` | Distance in metres to match a GTFS stop to an OSM stop (10 at most) |
| `--add-route-direction` | Add the compass direction to route names |
| `--ptv2-names` | Name routes `bus 1: Origin => Destination` |
| `--feed-id ID` | Add `gtfs:feed`, `gtfs:route_id` and `gtfs:trip_id:sample` tags so the relations can be compared with the feed later (for instance with [PTNA](https://community.openstreetmap.org/t/ptna-new-feature-gtfs-osm-comparison/109659)) |
| `-v`, `--verbose` | Detailed log |

## Reviewing and uploading

1. Open JOSM and download the area of the network (*File → Download data*), so that the
   stops and ways the relations refer to are loaded.
2. Open the `.osc` file (*File → Open*).
3. Check each relation: stop order, ways, gaps in the line, the `name`, `ref` and
   `network` tags, and lines that already exist.
4. Upload with a meaningful changeset comment.

Before importing a feed, make sure that:

- its **licence is compatible** with OpenStreetMap's (ODbL), or that the publisher
  allowed the import. Many feeds, in particular in France, are published under the
  *Licence Ouverte*: its compatibility is discussed on the
  [OSM legal-talk list](https://lists.openstreetmap.org/pipermail/legal-talk/2020-September/008924.html),
  so check the current position of the Licensing Working Group first;
- you followed the [Import Guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines)
  and discussed the import with the local community.

## Output

- **Stops** (only with `--add-missing-stops`): `name` and `public_transport=platform`,
  plus `highway=bus_stop` for buses and trolleybuses or `railway=platform` for rail
  modes.
- **Routes**: `type=route`, `public_transport:version=2`, `route` (from the GTFS
  `route_type`), `ref` (from `route_short_name`), `name`, `from` and `to` (first and last
  stop), and `colour` when the route has one.
- **Route masters**: `type=route_master`, grouping the routes that have the same `ref`.

## Troubleshooting

- *Conversion fails*: run with `-v`, and check the feed with a
  [GTFS validator](https://gtfs.org/resources/validation/).
- *Routes without ways*: expected for feeds without `shapes.txt` and for modes other
  than bus and trolleybus.
- *A line is missing*: it may have no service from today on (`--include-inactive`), or
  its type is not supported (see `-v`).
- *"Request error by Overpass API (HTTP 504)"* or *429*: the public server is busy. The
  tool retries a few times; try again later, and use `--route-ref-pattern` to convert
  fewer routes at once.

## Development

Everything can run in Docker (`docker compose --profile dev run --rm test`), or
natively with [uv](https://docs.astral.sh/uv/):

```bash
uv run --python 3.12 --with pytest python -m pytest -m "not slow"
```

Tests marked `slow` call the online services and are skipped here. `nox` runs the tests
on several Python versions (`nox -s tests`), and has `lint` and `type_check` sessions.

```
gtfstoosm/
├── gtfstoosm/       # package: cli.py, convert.py, gtfs.py, osm.py, utils.py
├── tests/
├── docs/usage.md    # all the options, and the Python API
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

Contributions are welcome: fork, create a branch, and open a pull request.

## License

MIT, see [LICENSE](LICENSE).

## Acknowledgments

- [GTFS Reference](https://gtfs.org/documentation/schedule/reference/)
- [OpenStreetMap Wiki: Public transport](https://wiki.openstreetmap.org/wiki/Public_transport)
