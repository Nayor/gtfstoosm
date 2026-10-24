# GTFS to OSM Converter

Turns a public transport feed ([GTFS](https://gtfs.org/)) into a file of OpenStreetMap
[public transport v2](https://wiki.openstreetmap.org/wiki/Public_transport) objects
(stops, route relations and route masters) that you review and upload with
[JOSM](https://josm.openstreetmap.de/).

```bash
docker compose run --rm convert /data/feed.zip      # writes data/feed.osm
```

Giving the feed is enough. The OSM data around its stops is downloaded once and cached,
the stops and ways are found in it, and the tags are guessed from the feed.

It is a helper for mappers, not an automatic importer: it never uploads anything, and
every result has to be checked by a person before it goes into OpenStreetMap.

## What it does

1. **Routes**: keeps the routes of the supported modes and leaves out closed lines, i.e.
   routes without any service from today on. A seasonal line (a summer shuttle in a feed
   valid for a year) whose season ended less than a year ago is kept: it comes back. Modes: `bus`, `trolleybus`, `tram`,
   `train`, `light_rail`, `subway`, `monorail`, `funicular`, `ferry` and `aerialway`
   (gondolas, cable cars). The basic and the extended GTFS route types are understood.
   Air, taxi and miscellaneous services are left out.
2. **Stops**: matches each GTFS stop with an existing OSM stop of the right kind (bus
   stop, tram stop, railway station, ferry terminal...) and creates the ones that are
   missing.
3. **Ways**: follows the shape of each trip (`shapes.txt`) on the roads, rails or cables
   of the OSM data and lists the ways it uses. A feed without `shapes.txt` (most Swiss
   feeds) gets its stops only.
4. **Relations**: writes a `type=route` relation per sequence of stops (stops as
   `platform` members, then ways), and a `type=route_master` grouping them. A route
   whose trips have a shape but no `stop_times` still gets a relation with its ways, and
   a `fixme` tag.
5. **Tags**: `network` and `operator` (the agency), `ref`, `colour`, `from`, `to`,
   a PTv2 `name` (`bus 1: Origin => Destination`), the link with the feed (`gtfs:feed`,
   `gtfs:route_id`, `gtfs:trip_id:sample`), and an estimate of `duration`, `interval`,
   `opening_hours` and `interval:conditional` from the timetable (see [Output](#output)).

The result is a file in JOSM's own format (`.osm`; the new objects have negative IDs).
`--format osc` writes an [osmChange](https://wiki.openstreetmap.org/wiki/OsmChange) file
instead (`.osc`, for tools such as osmium and for scripts), `--format both` the two. JOSM
is documented to open `.osm` files; I have not checked that it opens `.osc`.

What it does **not** do:

- It does not check whether the relations already exist in OSM: look before you add.
- It does not upload anything.
- The ways come from snapping the shape of the trips to the nearest way that can carry
  the mode. This is simple and fast but it is not a full map matching: a street that a
  shape only crosses may be picked up, one-way streets are not considered, and a shape
  far off the network gets gaps. Check the relations in JOSM.
- Stops of modes other than buses get minimal tags (`public_transport=platform`, plus
  `railway=platform` for rail modes): check them.

## The OpenStreetMap data

By default the tool gets what it needs from the extracts that two sites publish, and
keeps it in a cache, so that the servers are asked as little as possible:

1. it looks at where the stops of the feed are and picks the smallest region that
   contains them:
   - [OSM France](https://download.openstreetmap.fr/) (`osmfr`) first: the world by
     continents and countries, and for many countries their regions: the départements
     and régions of France, the cantons of Switzerland, the Länder of Germany, the
     provinces of Italy, Spain, Canada (down to cities such as Montréal)... Small files:
     Hautes-Alpes is 45 MB, the region around it 391 MB;
   - [Geofabrik](https://download.geofabrik.de/) (`geofabrik`) when OSM France has
     nothing smaller: countries and some regions of the whole world;
2. the region file is downloaded the first time. The big ones (several hundred MB) take
   a while: the first run is long;
3. a file less than 7 days old is used without asking the server anything; an older
   one is checked with a conditional request, which costs nothing if it has not
   changed; `--refresh` downloads it again whatever its age;
4. the region is cut to the area of the feed (with [osmium-tool](https://osmcode.org/osmium-tool/),
   included in the Docker image), and the cut is cached too.

Where the cache is: the `data/cache` folder with Docker, `~/.cache/gtfstoosm` otherwise
(or `$GTFSTOOSM_CACHE`). Geofabrik's extracts keep the version and the date of every
object (not the name of the contributors), which is what a later update needs.

**robots.txt.** The tool follows the `robots.txt` of the sites it downloads from.
OSM France's says `Disallow: /*.pbf$`, i.e. robots must not fetch its `.pbf` files, so
the tool does not download them by itself (it still reads the lists of regions and
their outlines, which are allowed). When a smaller file exists there, the log gives its
address and the name to save it under in the cache:

```
A smaller file exists for this area: https://download.openstreetmap.fr/extracts/europe/france/provence_alpes_cote_d_azur/hautes_alpes.osm.pbf (osmfr, ...)
... download it yourself and save it as data/cache/regions/osmfr-europe_france_provence_alpes_cote_d_azur_hautes_alpes.osm.pbf, or give it with --osm-file
```

Save the file as asked (from your browser, for instance) and the next run uses it;
until then the Geofabrik region is downloaded. If OSM France changes its robots.txt,
its files are downloaded automatically.

To use your own file (another source, no internet, a bigger area): `--osm-file`. Cut it
to the area of the feed first, otherwise reading it takes a lot of memory:

```bash
docker compose --profile tools run --rm osmium extract \
    -b 5.9,45.8,10.5,47.9 /data/region.osm.pbf -o /data/area.osm.pbf
docker compose run --rm convert /data/feed.zip --osm-file /data/area.osm.pbf
```

(`-b` is `min_lon,min_lat,max_lon,max_lat`.)

### Online mode (`--online`)

The tool can instead ask public servers: an [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API)
instance for the existing stops and a [Valhalla](https://valhalla.github.io/valhalla/)
instance for the ways. This is slow, only bus and trolleybus routes get ways, and it
puts a load on servers that are not ours: use it for small networks only. The addresses
are written in `convert.py`.

| What | Service | What is sent |
|---|---|---|
| Existing OSM stops | Overpass instance `maps.mail.ru` | Coordinates of the stops |
| Ways followed by a bus trip | Valhalla instance `valhalla1.openstreetmap.de` | Points of the trip shape |

## Routes and stops that OSM has already

The tool reads the route relations and the stops of the OSM data it uses, and compares
them with what the feed gives, so that nothing is created twice. A route of the feed is
matched with a relation of OSM through the link `gtfs:feed` + `gtfs:route_id` when
it is there (a first import writes it), else through the `ref` and the stops they have
in common (at least half of them, or 80 % whatever the `ref`). The two directions of a
line are told apart by their ends and by the order of the stops. Each route gets one of
these statuses:

| Status | Meaning | In the file |
|---|---|---|
| new | nothing in OSM looks like it | the relation is created |
| same | OSM has it, with all the tags the feed gives | nothing |
| link | OSM has it and only lacks tags (the link with the feed, `network`, `duration`...) | a `modify` that adds them |
| different | a tag has another value, or the stops are not the same (other stops, another order, a stop of the feed that has no node in OSM) | nothing: it is reported in the log |
| ambiguous | several relations of OSM look equally like it | nothing: it is reported in the log |

The tool **only adds**. It never removes a tag, changes the value of one, moves a node
or changes the members of a relation of OSM: those differences are for the person who
reviews the file. What it adds to objects that are in OSM (written as `action="modify"`
with their current `version`): the missing tags of a route that is linked, the tag
`gtfs:stop_id:<feed>` on the stops of OSM that the stops of the feed were matched with,
and the new routes in the `route_master` of OSM when there is one. The routes of the
feed that OSM lacks are created. The roles of the stops follow what OSM has: `stop` for
a `public_transport=stop_position`, `platform` otherwise.

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
   docker compose run --rm convert /data/feed.zip
   ```

   The first run builds the image, which takes a few minutes, and downloads the OSM
   data. `data/feed.osm` is the result.

4. Open `feed.osm` in JOSM (see [Reviewing and uploading](#reviewing-and-uploading)).

On Windows with **Git Bash**, put `MSYS_NO_PATHCONV=1` in front of the command,
otherwise `/data/...` is rewritten into a Windows path. PowerShell and cmd do not
need it.

Other commands:

```bash
docker compose run --rm convert --help                  # all options
docker compose --profile lite run --rm convert-lite /data/feed.zip   # without the validator
docker compose --profile tools run --rm osmium --help   # osmium-tool
docker compose --profile tools run --rm pfaedle -x /data/region.osm.pbf /data/feed.zip
```

`convert` is the full image (about 830 MB): it carries Java and the GTFS validator.
`convert-lite` (about 580 MB) leaves them out and does not check the feed.

[`pfaedle`](https://github.com/ad-freiburg/pfaedle) (GPL-3.0, official image) generates
`shapes.txt` for feeds that lack them, from an OSM extract. It writes a copy of the feed
to `data/gtfs-out`, which you can then convert.

## Checking the feed

The full image checks every feed with the official
[MobilityData GTFS validator](https://github.com/MobilityData/gtfs-validator)
(Apache-2.0, a Java program, version 8.0.1) before converting it. Every `ERROR` notice
stops the conversion; warnings are only logged. The human-readable report is saved next to
the result (`feed.validation.html`).

- Some errors do not matter for this tool: let them through with
  `--ignore-validation CODE [CODE...]`, using the code shown in the report.
- `--no-validate` skips the check; `--validation-report report.json` uses a report you
  already have instead of running the validator.
- Without Java (the lite image, or a native install) the feed is simply not checked.
  Outside Docker, `$GTFS_VALIDATOR_JAR` can point to the validator's `cli.jar`.
- A feed that is valid is not necessarily convertible well: for example a feed without
  `shapes.txt` is valid but its routes get no ways. The validator also reports calendars
  that have expired.
- For very large feeds Java may need more memory: set `JAVA_TOOL_OPTIONS=-Xmx4g` in the
  `environment:` of the service in `docker-compose.yml`.

## Prerequisites

| | With Docker (recommended) | Without Docker |
|---|---|---|
| **Linux** | [Docker Engine](https://docs.docker.com/engine/install/) with the Compose plugin (`docker compose version` must work) | Python 3.12, `python3-venv`, git, osmium-tool |
| **macOS** | [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) | Python 3.12 (for instance `brew install python@3.12`), git, `brew install osmium-tool` |
| **Windows** | [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) with the WSL 2 backend, git | Python 3.12 from [python.org](https://www.python.org/downloads/), git; osmium-tool is hard to get on Windows: prefer Docker |

In both cases you also need [JOSM](https://josm.openstreetmap.de/) to review the result
and an OpenStreetMap account to upload it, and an internet connection (to download the
OSM data once, unless you give it with `--osm-file`).

Without osmium-tool the tool still works, but reads the whole region file, which needs
several GB of memory.

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
python -m gtfstoosm.cli path/to/feed.zip
```

If `pip` tries to compile the `regex` package and fails on Windows with
*"Microsoft Visual C++ 14.0 or greater is required"*, your Python version has no
prebuilt package yet: use Python 3.12, or Docker.

## Options

None is required: `python -m gtfstoosm.cli feed.zip` does everything. The options
tune what is guessed (all of them are in [docs/usage.md](docs/usage.md)):

| Option | Effect |
|---|---|
| `-o`, `--output FILE` | Output file (default: the feed's name with `.osm`) |
| `--format osm\|osc\|both\|maproulette` | JOSM's file format (default), osmChange for other tools, both, or the file for a MapRoulette challenge |
| `--only stops\|routes` | Only the stops, or only the routes |
| `--modes MODE [MODE...]` | Only these modes |
| `--route-ref-pattern REGEX` | Only routes whose `route_id` matches |
| `--include-inactive` | Also convert routes that no longer have service |
| `--no-new-stops` | Do not create the stops that are missing from OSM |
| `--no-service-tags` | Do not estimate `duration`, `interval`, `opening_hours` |
| `--feed-id ID` | `gtfs:feed` (default: the feed's file name), e.g. `FR-IDF-IDFM` |
| `--network NAME`, `--operator NAME` | Instead of the agency's name |
| `--tag KEY=VALUE` | Another tag on the relations (repeatable) |
| `--osm-file FILE` | Use this OSM extract instead of downloading one |
| `--refresh` | Download the OSM data again |
| `--online` | Ask Overpass and Valhalla instead (small networks only) |
| `--stop-search-radius M` | Distance to match a GTFS stop with an OSM stop (default 10; at most 100, or 10 with `--online`) |
| `--no-validate` | Do not check the feed with the GTFS validator |
| `--validation-report report.json`, `--ignore-validation CODE...` | Use this validator report instead of running it; let some error codes through |
| `-v`, `--verbose` | Detailed log |

The stop search radius is the one to adjust by trial: GTFS positions are often 10 to 30 m
off. A larger radius matches more stops with existing OSM ones but can pair a stop with
its neighbour's; the closest pairs are matched first.

## Reviewing and uploading

1. Open JOSM and download the area of the network (*File → Download data*), so that the
   stops and ways the relations refer to are loaded.
2. Open the `.osm` file (*File → Open*): it opens as a new layer. Merge it into the data
   layer you downloaded (*Layer → Merge*, or Ctrl+M) so that the stops and ways it refers to
   are completed.
3. Check each relation: stop order, ways, gaps in the line, the `name`, `ref` and
   `network` tags, the `fixme` tags, and lines that already exist.
4. Upload with a meaningful changeset comment.

### Reviewing with several people: MapRoulette

To share the review of a network between several mappers, the changes can be published
as a [MapRoulette](https://maproulette.org/) "cooperative" challenge: one task per route or
stop, each reviewed by a person who confirms it in JOSM (the change is loaded in the editor)
and uploads it with their own account.

```bash
gtfstoosm feed.zip --format maproulette            # feed.maproulette.osm
docker compose --profile tools run --rm maproulette cooperative change     --out /data/feed.challenge.json /data/feed.maproulette.osm
```

then create a challenge on MapRoulette and upload `feed.challenge.json` to it. The file
for MapRoulette has no `route_master` relation (MapRoulette's tool `mr-cli` cannot read
relations of relations): add them afterwards in JOSM, or with a normal run. Changes
that only add tags can be made [tag fix tasks](https://learn.maproulette.org/en-US/documentation/tag-fix-challenges/)
(`cooperative tag` instead of `cooperative change`), which the mapper approves in the
browser. Things to know: `mr cooperative` is marked experimental by its authors (check the
tasks it makes), it asks the OSM servers for the current version of the objects, and a
challenge is public. On the data of this repository's test feed it makes one task per
route and per modified stop; I have not published a challenge. The import rules below
apply as for any other import.

Before importing a feed, make sure that:

- its **licence is compatible** with OpenStreetMap's (ODbL), or that the publisher
  allowed the import. Many feeds, in particular in France, are published under the
  *Licence Ouverte*: its compatibility is discussed on the
  [OSM legal-talk list](https://lists.openstreetmap.org/pipermail/legal-talk/2020-September/008924.html),
  so check the current position of the Licensing Working Group first;
- you followed the [Import Guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines)
  and discussed the import with the local community.

## Output

- **Stops** (created when OSM lacks them): `name` and `public_transport=platform`, plus
  `highway=bus_stop` for buses and trolleybuses or `railway=platform` for rail modes.
- **Routes**: `type=route`, `public_transport:version=2`, `route` (from the GTFS
  `route_type`), `ref` (from `route_short_name`), `name`, `from` and `to` (first and last
  stop), `network` and `operator` (the agency), `colour` when the route has one,
  `gtfs:feed`, `gtfs:route_id` and `gtfs:trip_id:sample`.
- **Service**: `duration` (median travel time), and for regular services `interval`
  (time between departures), `opening_hours` (first to last departure, per group of
  days) and `interval:conditional` (other headways, for instance at peak hours). They
  come from `frequencies.txt` when the stop sequence has rows in it, otherwise from the
  departures at the first stop. These are estimates, as OSM wants: a route gets an
  interval only with at least 4 departures per group of days and a regular headway, and
  the last departure is not the end of service. The days come from `calendar.txt` (and
  `calendar_dates.txt`). A service that runs during a small part of the feed's validity
  (a summer shuttle in a feed valid for a year) is seasonal and gets a date range, such
  as `Jul 04-Aug 23 Mo-Su 09:00-18:00`; the school holidays are not in a GTFS feed and
  are not guessed.
- **Route masters**: `type=route_master`, grouping the routes that have the same `ref`.
- **`fixme`**: on a route whose stops are unknown (no `stop_times` in the feed).

## Troubleshooting

- *Conversion fails*: run with `-v`, and check the feed with the validator.
- *Routes without ways*: expected for feeds without `shapes.txt`, or when the shape is
  far from the roads or tracks of the OSM data.
- *A line is missing*: it may have no service from today on (`--include-inactive`), no
  trip, or neither `stop_times` nor a shape, or its type is not supported (see `-v`).
- *"No OSM region found for this feed"*: the stops are outside every Geofabrik region
  (check their coordinates) — or give your own file with `--osm-file`.
- *The download is long*: the first run downloads the region (hundreds of MB; Geofabrik
  sometimes serves slowly, a few tens of KB/s); the following ones use the cache. For
  a quicker start, take the smaller OSM France file the log tells about, or your own.

## Development

Everything runs in Docker. `Dockerfile.dev` builds an image without the source: compose
mounts the `gtfstoosm/` and `tests/` folders of your disk in it, so a change is seen at
once, with no rebuild. `up` starts it, and the tests run again at each change:

```bash
docker compose --profile dev up                       # the tests, again at each change
docker compose exec dev gtfstoosm /data/feed.zip      # in another terminal: the tool, from your code
docker compose exec dev pytest                        # the tests, once
docker compose exec dev ruff check .                  # lint
```

(Without `up -d`, the same work in a one-shot container:
`docker compose --profile dev run --rm dev pytest`.) To bring the image up to date,
`docker compose --profile dev build --pull dev`. The images used for running the tool
(`Dockerfile`: `convert` and `convert-lite`) have the code inside: after a change,
rebuild with `docker compose build --pull convert`.

Without Docker, with [uv](https://docs.astral.sh/uv/):

```bash
uv run --python 3.12 --with pytest python -m pytest -m "not slow"
```

Tests marked `slow` call the online services and are skipped here; the others never use
the network, nor run the validator. `nox` runs the tests on several Python versions
(`nox -s tests`), and has `lint` and `type_check` sessions.

```
gtfstoosm/
├── gtfstoosm/       # package: cli.py, convert.py, extracts.py, osm_data.py, ...
├── tests/
├── docs/usage.md    # all the options, and the Python API
├── Dockerfile       # the tool: lite and full (with the validator) images
├── Dockerfile.dev   # for working on the code
├── docker-compose.yml
└── pyproject.toml
```

Contributions are welcome: fork, create a branch, and open a pull request.

## License

MIT, see [LICENSE](LICENSE).

## Acknowledgments

- [GTFS Reference](https://gtfs.org/documentation/schedule/reference/)
- [OpenStreetMap Wiki: Public transport](https://wiki.openstreetmap.org/wiki/Public_transport)
- [Geofabrik](https://download.geofabrik.de/) for the regional extracts
