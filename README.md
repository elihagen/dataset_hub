# Dataset Hub

Upload UI, metadata catalog and NAS folder structure for robot recordings.
Result of the Data Management Focus Sprint (15 Oct 2026).

- **Upload UI** (runs on the workstation): pick a bag folder, the hub detects
  date / length / GPS / topics, you fill in the rest. Required fields and the
  folder layout are enforced. You can't upload without them.
- **Catalog** (SQLite) with filters on every metadata field, plus bulk edit
  for a selected subset.
- **Sensor registry**: `sensor_id` → type, model, driver version. Datasets
  reference sensor_ids.
- **Legacy migration**: scan the old NAS into a CSV, complete it in a
  spreadsheet, import it. Originals stay untouched (`copy` mode).
- **marina** ([uos/marina](https://github.com/uos/marina)) as the optional
  transport/registry backend: every ingested dataset can be pushed to a marina
  registry under a reference derived from its metadata.

Only the Python 3.9+ standard library is needed. `PyYAML` and `mcap` are used
when installed. Without them, rosbag2 `metadata.yaml` is parsed by a small
built-in fallback.

## Quick start

```bash
cd dataset_hub
python3 scripts/make_mock_datasets.py ./mock_nas      # fake old NAS with 5 bags
cp config.example.json config.json                    # adjust storage_root etc.
export DATASET_HUB_CONFIG=$PWD/config.json

python3 -m hub sensor-add ouster_os1 lidar --model "OS1-128" --driver-version 0.11
python3 -m hub serve                                  # http://127.0.0.1:8080
```

Tests (use the mock dataset, no config needed):

```bash
python3 -m unittest discover -s tests -v
```

## Metadata

Defined once in `hub/schema.py`. The form, DB columns, validation, filters and
sidecar are all generated from it.

| field | notes |
|---|---|
| customer \* | `NR` for internal |
| project \* | |
| robot \* | |
| date \* | auto from bag start time |
| site \* | |
| robot_task | |
| sensors | list of registered `sensor_id`s |
| has_gps | auto: NavSatFix topic with >0 messages |
| stack_version | |
| ai_model_metadata | JSON |
| length_s | auto from bag duration |
| responsible | |
| comment | free text |

\* required. To add a field, append it to `FIELDS`. The DB column is added
automatically on the next start.

## Folder structure

`path_template` in the config, default:

```
<storage_root>/{customer}/{project}/{robot}/{date}_{site}_{robot_task}_{dataset_id}/
    dataset.json          <- full metadata sidecar
    <original bag files>
```

Values are slugified (`Customer A` → `customer-a`), and empty optional
fields become `na`. If a bulk edit changes a field that is part of the path,
the folder is moved and empty parent folders are removed. The `dataset.json`
sidecar means the catalog can be rebuilt from storage alone:
`python3 -m hub reindex`.

Ingest modes: `copy` (default: originals stay where they are), `move`, and
`register` (catalog only, data stays in place).

## Migrating the existing NAS

```bash
# 1. scan: one row per rosbag2 folder / bag-containing folder
python3 -m hub scan-legacy /mnt/nas/old -o legacy.csv --default customer=NR

# 2. open legacy.csv, fill customer/project/robot/site/... per row
#    (filter + fill-down works well for bulk-setting a subset), set import=yes

# 3. validate, then import
python3 -m hub import-csv legacy.csv --dry-run
python3 -m hub import-csv legacy.csv
```

Rows that were already imported (same `source_path`) are skipped with an
error, so the CSV can be re-run as more rows get completed.

## CLI overview

```bash
python3 -m hub ingest <folder> customer=NR project=x robot=r1 site=yard sensors=ouster_os1
python3 -m hub list --where customer=NR has_gps=true
python3 -m hub bulk-set --where project=x --set stack_version=2.3.0
python3 -m hub sensors
python3 -m hub marina-sync
```

## marina

Install marina and add a registry (e.g. the NAS: `marina registry add nas folder:///mnt/nas/marina`),
then set `"marina_registry": "nas"` in the config. Every ingest then runs
`marina push <ref> <dataset folder>` with

```
<customer>/<project>-<robot>-<date>:<dataset_id>:<site>:<robot_task>
```

and stores the ref in the catalog, so anyone can `marina pull` it. The
pushed copy includes `dataset.json`. The hub pushes with `--copy-to-cache`
(plain `marina push` *moves* the folder into marina's cache) and then drops
the local cache copy, so storage stays untouched and nothing is duplicated.

Push failures don't lose the upload. The dataset is marked
`status=marina_push_failed` instead. If a bulk edit changes a field in the
ref (customer, project, robot, date, site, robot_task), the dataset is
pushed under the new ref and the old ref is removed from the registry.

```bash
python -m hub marina-sync --dry-run          # what's missing, failed or stale
python -m hub marina-sync                    # push those (e.g. after enabling marina)
python -m hub marina-sync --where project=x --force   # re-push regardless
```

In the Catalog tab, a marina column shows each dataset's state, and
**Push to marina** does the same as `marina-sync` for the selected rows.

Note that marina compresses PointCloud2 lossily (1 mm) by default, so the
registry copy is not bit-identical. Change it in marina's own config if
that matters. The storage copy is always the original.

Tests run against the real CLI when it's installed
(`pip install marina-cli`, or `MARINA_BIN=/path/to/marina`), with an
isolated marina config and a throwaway registry.

## Layout

```
hub/schema.py          metadata fields (single source of truth) + validation
hub/bag_info.py        size, topics, duration, date, GPS detection from bags
hub/db.py              SQLite catalog, sensor registry, filter queries
hub/ingest.py          upload pipeline, folder layout, bulk update, reindex
hub/legacy.py          NAS scan -> CSV -> import
hub/marina_backend.py  marina CLI wrapper
hub/server.py          web API + static UI
static/index.html      upload / catalog / sensors UI
scripts/make_mock_datasets.py
tests/test_pipeline.py
```

## Open points / next steps

- Uploads run synchronously in the request, so the UI waits during large
  copies. Move them to a background job queue (the same queue can then run
  the statistics / bag-replay jobs from the brainstorming).
- Dataset preview: link each catalog row to a Lichtblick instance on the
  workstation.
- Quality flag set by replay jobs (add a `quality` field and a job runner).
- Which metadata can be written on the robot in the field (a `dataset.json`
  written at record time would be picked up by the upload form as prefill).
- No auth. Bind to localhost or the workstation LAN only.
