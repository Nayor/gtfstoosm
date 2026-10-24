# Two tests to run in JOSM

The way this tool hands its work over to JOSM rests on two behaviours of JOSM that its
documentation does not fully settle. Each takes a few minutes. Please tell what you see.

You need [JOSM](https://josm.openstreetmap.de/) (see
[its download page](https://josm.openstreetmap.de/wiki/Download)) and, for the second test, an
account on the **OSM development server** (a copy of OSM made for tests: nothing done there
touches the real map). Create it as you would an OSM account, on
<https://master.apis.dev.openstreetmap.org/>.

## Test 1: a conflict shown by JOSM itself

**Question.** When a file proposes a change to an object that has a newer version on the server,
does JOSM open its conflict window, with "my version" (the feed) and "their version" (OSM)?

`conflict-test.osm` has two real stops of Chorges (Hautes-Alpes), both marked `action="modify"`:

- node `10794760165`, in the version the server has (3): a tag `gtfs:stop_id:chorges` is
  added. Nothing to arbitrate;
- node `10814567028`, in version 2 whereas the server has version 3 (someone edited it since),
  with another `name` and the same new tag. A conflict is expected.

The file carries `upload="never"`, so JOSM will not let you upload it: these are real objects,
do not try to. (The versions are those of 24 October 2026: if the stops were edited since, the
first node may be in conflict too, and the second not. The point is the behaviour, not the
objects.)

**Do.** Open the file (*File → Open*), then *File → Update modified* (Ctrl+Alt+M).

**Expected.** A conflict on node `10814567028` only. Its window shows the two names, and lets
you choose one for each tag. **To tell me:** was a conflict raised? On which node(s)? Did the
window show both versions of the name?

## Test 2: upload one line, with its new stops, out of a file that has two

**Question.** The file has two lines, TESTA and TESTB, that share a new stop (`-1`), a stop of
their own each, and a `route_master`. Does *Upload selection* with line TESTA selected send the
line **and its new stops** (including the shared one) but not line B? And once it is uploaded,
does line B (still local) refer to the shared stop by its new, real id?

**Set up JOSM for the development server.** *Edit → Preferences → OSM server*: untick "Use the
default OSM server", set the server URL to `https://api06.dev.openstreetmap.org/api`, and log in
with the account of the development server. **Check this before uploading anything.**

**Do.**

1. Open `upload-selection-test.osm`.
2. Select line A: *Edit → Search* (Ctrl+F) with `type:relation ref=TESTA`.
3. *File → Upload selection*. **Look at the list of objects before confirming.**
4. Confirm, with a changeset comment such as "test of upload selection".
5. Select line B (`type:relation ref=TESTB`) and *Upload selection* again. Look at the list.

**Expected.**

- Step 3 lists relation TESTA and the nodes `-1` and `-2`; not TESTB, not node `-3`.
- After step 4, the shared stop has a positive id. In step 5 the list holds TESTB and node
  `-3` only (the shared stop is not sent again, and no duplicate is made).

**To tell me:** what was in the list at step 3? What was in it at step 5? Was there an error
(for instance "relation refers to a new object that is not uploaded")? If the stop of line A is
missing from the list in step 3, tell me too: it is the information I need.

When you are done, you may delete your test objects on the development server, or leave them:
it is wiped from time to time.
