# Video Preview Matcher

Finds preview/trailer clips in a large video library that are derived from a
full-length video already in the library — even when the preview reorders
scenes, crops/zooms, horizontally flips footage, dubs in narration over the
original audio, or burns in logos/bars/scrollers — and gives you a local web
UI to confirm each match side-by-side before deleting anything.

Built for: ~6000 files, mixed mkv/mp4/etc, roughly half previews / half full
versions, unlabeled.

## How it works

```
1. 01_inventory.py    Walk the library, ffprobe every file -> SQLite
2. 02_fingerprint.py  Per video: scene-cut detection + perceptual hashes
                       (normal/cropped/flipped) + color signature +
                       audio chromaprint fingerprint
3. 03_match.py        Score every plausible preview/candidate pair,
                       store ranked matches
4. 04_serve.py        Local web UI: triggers/monitors scans (1-3) so
                       nothing needs to be run by hand, plus side-by-side
                       player, scene-jump buttons, approve (stage)/reject,
                       staging purge
```

Each stage writes to `data/library.db` (SQLite) and is independently
re-runnable. Re-running `02`/`03` after tuning thresholds does NOT
re-touch already-fingerprinted videos unless their file changed
(see `needs_reprobe` in `01_inventory.py`) — only the scoring in `03`
is fully recomputed each run, which is cheap once fingerprints exist.

**You normally don't run 01/02/03 by hand at all** — the "Library Scan"
panel in the web UI (04) runs all three for you, with progress, pause,
cancel, and resume. See "Triggering scans from the web UI" below. The CLI
commands in this README are what that panel runs under the hood, and
remain useful for scripting or direct control (e.g. via
`docker compose run`).

## Setup

```bash
pip install -r requirements.txt --break-system-packages
sudo apt install ffmpeg chromaprint     # chromaprint provides `fpcalc`
```

Both `imagehash` (Python) and `fpcalc` (chromaprint) are optional at the
code level — the pipeline degrades gracefully without them (a vendored
pHash fallback in `src/phash.py`, and audio scoring simply skipped) — but
install both for the real run. (Real `imagehash` has since been
cross-checked against the vendored fallback — see "Known limitations"
below — and produces correct results; no surprises switching to it.)

## Run

```bash
cd video-preview-matcher

python3 src/01_inventory.py /path/to/library [/path/to/another ...] --db data/library.db
# Multiple library roots can be scanned in one run (space-separated). If no
# paths are given on the command line, roots are read from the LIBRARY_PATHS
# env var (':'-separated) — this is how the Docker deployment configures it.
# --limit N caps how many files actually get (re-)probed, counted *after*
# skipping already-current files — so re-running with the same --limit
# walks forward through the library N files at a time, rather than
# re-checking the same already-done files forever.

python3 src/02_fingerprint.py --db data/library.db --workers 8
# ~6000 files: budget real time here. Each video needs a full decode pass
# for scene detection. Start with --limit 50 to sanity-check before
# committing to a multi-hour run. --workers should roughly match core count.
# Under the web UI (--run-id set), this count can be raised/lowered live
# while running, without restarting — see "Triggering scans" below.
# --hwaccel vaapi offloads decode to a VAAPI-capable GPU instead of the 
# CPU — opt-in, validate with --limit first.
# --debug-log path/to/file.log records every ffmpeg/ffprobe/fpcalc call
# (filename, duration, exit status, output) — useful if a run seems stuck;
# 01 supports the same flag. Always on for web-UI-triggered scans
# (data/subprocess.log).

python3 src/03_match.py --db data/library.db --workers 8
# Pure hash comparison, no video decoding or other I/O — scales cleanly
# across cores. --workers defaults to cpu count - 1; 1 = sequential (no
# process pool), useful for debugging. Safe to re-run repeatedly while
# tuning thresholds below. Defaults to --executor loky with periodic
# worker recycling and a --min-available-ram-percent safety abort, both
# prioritizing bounded memory over raw speed — see TUNING.md.

python3 src/04_serve.py --db data/library.db
# Open http://127.0.0.1:8000
```

## Keeping the database in sync with the library

Library files don't only get added — they also get deleted, moved,
renamed, or replaced (e.g. re-encoded). `01_inventory.py` handles all of
these automatically, every run, no extra flag needed:

- **Deleted, or moved somewhere not yet re-discovered**: a path that was
  previously probed but isn't found on this run's filesystem walk gets
  flagged `missing_since` (a timestamp, not a deletion — see below).
  **Scoped to the roots actually passed this run**: a partial scan (e.g.
  one root selected in the web UI) never flags a file under a
  *different*, unscanned root as missing just because this particular
  invocation didn't look at it. The flag clears automatically the moment
  the path is seen again on a future scan.
- **Moved or renamed**: when a brand-new path is probed and its exact
  `(size, duration)` matches *one* currently-missing row, that row is
  remapped to the new path instead of inserting a duplicate — its
  fingerprints, scenes, matches, and past review decisions all carry
  over, and it's **not** re-fingerprinted. This is a heuristic (no
  content hash), so an exact size+duration coincidence between two
  genuinely different files is a real, if rare, possible
  misattribution — accepted as a worthwhile tradeoff against the cost of
  hashing whole multi-GB files just to be sure. If more than one missing
  row matches, nothing is guessed — the file is treated as new instead.
- **Replaced with a different/re-encoded version at the same path**:
  detected via the existing size/mtime check, exactly as before this
  feature — triggers a full re-fingerprint on the next `02` run.

**Nothing is ever auto-deleted by any of this.** A flagged-missing
row just gets hidden from the active review queue (its matches won't
show up, but nothing about them is touched) until it either reappears on
its own or a human explicitly removes it via the queue page's "missing
files" panel / `POST /api/missing-files/prune` (same explicit-confirm
pattern as emptying the staging folder). **A preview you've already
staged for deletion is never flagged missing or pruned this way** — its
file is expected to be gone from the library (it's in the staging
folder, not deleted), and that's tracked separately; see the Staged tab
below.

## Triggering scans from the web UI

The queue page (`/`) has a "Library Scan" panel — this is the normal way
to run the pipeline; you don't need a terminal for day-to-day use:

- **Stage selection**: run the full pipeline, or just one stage —
  "Inventory only" (probe new/changed files), "Fingerprint only", or
  "Match only". Root selection, limit, and the GPU checkbox show or hide
  depending on which stages your selection actually uses (e.g. roots are
  irrelevant and hidden for "Match only").
- **Pick roots**: checkboxes for each directory in `LIBRARY_PATHS`. Check
  all of them for a full scan, or just one/some for a partial scan (e.g.
  only the directory you just added files to).
- **Optional limit**: caps how many files get processed, for a quick
  sanity check before committing to a full run (mirrors `--limit` above).
- **GPU decode (VAAPI)**: off by default, mirrors `--hwaccel vaapi` above
   — only affects the fingerprint stage's decode step, not the rest of
   the pipeline. Measured slower than CPU decode for most content on the
   reference hardware (see [BENCHMARKS.md](BENCHMARKS.md)), which is why
   this defaults off — VAAPI is still used automatically for any video
   above `--extract-max-dim` regardless of this checkbox, where it's a
   real win.
- **Fingerprint workers / Match workers** (optional, shown only when the
  relevant stage is selected): sets each stage's *starting* worker count
  (mirrors each stage's `--workers` above; both default if left blank).
  Once fingerprinting is running, a separate "fingerprint workers"
  control appears in the progress panel — unlike the starting value
  above, this one is **live**: raising or lowering it takes effect within
  about 2 seconds, without restarting the scan, and never kills a worker
  that's already partway through a video. Lowering it just stops handing
  off new videos until in-flight count drains down to the new target;
  raising it immediately hands off more. Bounded to [2, your CPU core
  count]. Match workers has no live control — matching stages typically
  finish in minutes, so there's less to gain from adjusting mid-run.
- **Start scan** runs the selected stage(s) as subprocesses, in the
  fixed order `01_inventory.py` → `02_fingerprint.py` → `03_match.py` —
  nothing is automated beyond that chaining; matches still require a
  human decision in the review UI before anything happens to a file.
- **Progress**: current stage, file/pair count, a progress bar, elapsed
  runtime, ETA, estimated total stage duration, and an estimated
  wall-clock end time — all computed from the current stage's own rate
  (inventory and fingerprinting cost wildly different amounts of time per
  file, so a whole-run rate would be meaningless right after a stage
  change). Before the first file of a stage finishes there's no rate to
  estimate from yet, so it shows "estimating…" rather than nothing.
  During fingerprinting specifically, each video is a full decode pass —
  expect the count to sit still for a while between completions on
  feature-length files (the message line still updates the moment a
  worker picks up a new video, so it's not literally frozen).
- **Pause** stops the current stage now; **Cancel** does the same thing
  with a different label — both are non-destructive, since `01`/`02` only
  ever add/update DB rows. Resuming later (or after a crash — see below)
  picks up where it left off rather than redoing finished work, because
  `01` only re-probes changed files and `02` only fingerprints videos that
  aren't fingerprinted yet.
- **Resume** appears after a paused, cancelled, failed, or interrupted
  scan, picking back up at the *stage* that was actually interrupted, not
  always restarting from inventory (an earlier version did that, which
  re-walked the library and could crowd out the specific files still left
  to fingerprint once the inventory pass discovered new ones). Resume is
  a true continuation, not a replay of the original request: it keeps
  whatever fingerprint-worker count was last in effect (including a live
  adjustment made mid-run, not just the value the scan originally started
  with), keeps `--limit` scoped to the *remaining* part of the original
  total (resuming a "limit 1000" run that got 134 done won't fingerprint
  1000 more on top), and the progress bar/elapsed time/ETA all continue
  from where the interrupted attempt left off instead of restarting at 0.
- If the container restarts mid-scan (crash, `docker kill`, host reboot),
  the scan shows as "interrupted" rather than stuck on "running" forever
  — click Resume to pick it back up.

Only one scan can run at a time. The pipeline stages above remain
runnable by hand (e.g. via `docker compose run`) if you want direct
control instead.

## Tuning

This was built and validated against a small synthetic test library (see
`tests/` and the conversation that produced this code), not your actual
6000-file library — the defaults are reasonable starting points, not
calibrated values. **See [TUNING.md](TUNING.md)** for every flag worth
adjusting (with the real false-positive incidents and benchmarks behind
each default) and **[BENCHMARKS.md](BENCHMARKS.md)** for the real numbers
those defaults were picked from. Run `contrib/benchmark_settings.py` (see
its own [README](contrib/README.md)) against your own library to find good
settings for your own hardware instead of assuming either file's numbers
transfer.

**Recommended first real run:** `--limit 100` (or point `01_inventory.py`
at a small subdirectory) through the whole pipeline, review results in the
UI, adjust thresholds, repeat — before committing to fingerprinting
thousands of files. Fingerprinting is the expensive stage.

## Review UI

The layout is responsive (queue rows, scan panel, and the side-by-side
video comparison all reflow for a phone-width screen) — useful for
checking on a long-running scan, or doing a quick approve/reject pass,
from a phone without it just rendering a zoomed-out desktop page. The
favicon doubles as the "add to home screen" icon on Android (via
`static/manifest.webmanifest`), so a shortcut on the home screen opens
without browser chrome, like an app.

- Queue (`/`) has three tabs: **Pending** lists every truly-undecided
  preview with at least one candidate match above the noise floor,
  sorted by confidence; **Staged** lists every preview currently staged
  for deletion, most recently staged first; **Rejected** lists every
  preview marked "not a match / keep," most recently rejected first —
  in case a rejection turns out to have been a mistake, you can find it
  and hit "undo" on its review page instead of it just disappearing
  into Pending. Staged and Rejected are both kept separate from Pending
  so you can review/undo a past decision without scrolling past
  everything still undecided, and (unlike Pending) neither is ever
  affected by a later re-scan or re-match changing what it's shown next
  to. Each tab is paginated, 40 rows per page; switching tabs
  or pages updates the list via AJAX (no full page reload), so it never
  resets your scroll position — and returning to the queue from a review
  page (via "back to queue" or right after deciding) restores the exact
  scroll offset you had before. A preview or
  candidate flagged missing (see "Keeping the database in sync with the
  library" above) is hidden from Pending — a "missing files" panel next
  to the staging-folder controls lists them and lets you prune the DB
  rows explicitly (confirm-gated; never touches a file, since these rows
  have none left to touch; a staged preview is never included here, see
  above).
- Detail page (`/review/{id}`) shows the preview and best-match candidate
  side-by-side (full file path shown for both), with buttons to jump each
  player to a matched scene timestamp. Switch between candidates (if a
  preview matched more than one full video) via dropdown or `←`/`→`. A
  "Match details" table below the players lists every matched scene's
  hash distance, both timestamps, each side's scene duration (gap to its
  own next scene-cut — short durations on both sides are exactly the
  shared-intro/logo false-positive pattern that `--min-scene-duration`
  guards against, see [TUNING.md](TUNING.md)), and crop/flip variant,
  plus a summary line stating whether the matches spread across multiple
  distinct moments in *both* the preview and the candidate, or collapse
  onto a single point in either one (weak, coincidental-prone evidence
  either way — see [TUNING.md](TUNING.md)'s `--min-matched-scenes`/
  `--min-match-spread`/`--min-candidate-match-spread`) — useful for judging a
  match even when (or especially when) playback isn't available, and for
  spotting a confidently-wrong match before trusting it.
- If a file fails to play in-browser, you'll see an inline message
  instead of a silent failure (most real causes — like the broken-codec-tag
  or wrong-container issues below — are auto-fixed transparently and
  shouldn't trigger this).
- `D` stages the preview for deletion, `K` keeps it (marks rejected), `X`
  dismisses the currently-selected candidate as "not a match" — distinct
  from `K`: it flags just that one pairing as wrong (remembered, won't be
  re-suggested even after re-running `03_match.py`) without deciding the
  preview as a whole, leaving any other candidates for it still open for
  review.
- **Nothing is hard-deleted by approving a match, and nothing is ever
  deleted automatically.** This tool only identifies candidates and waits
  — leaving a preview undecided is always fine, there's no time pressure
  or auto-action. Approval moves the file into the staging folder (a
  rename — instant, no extra disk space). The bottom-right "empty staging
  folder" button is the only irreversible action in the tool, and
  requires an explicit confirm.
- Undo is available on any decided preview from its review page.

## Docker deployment

Runs as a container via Docker Compose. `docker-compose.yml` in this repo
is a **generic sample** — copy it and fill in your own library/staging/
app-state paths (every spot that needs editing is commented). `rebuild.sh`
looks for `docker-preview-matcher.yml` (a gitignored local override with
your real paths — handy if you want to keep the tracked sample untouched)
and falls back to `docker-compose.yml` if that file isn't present:

```bash
./rebuild.sh   # docker compose build && docker compose up -d
```

The `app` service runs `04_serve.py` persistently. The normal way to run
the pipeline stages (`01`/`02`/`03`) is the web UI's "Library Scan" panel
(see above) — `04_serve.py` launches them as subprocesses against this
same image on request. They can also still be run as one-off jobs
directly, e.g. `docker compose run --rm app python3 src/01_inventory.py`
(no positional library paths or `--db` needed — `01` reads roots from the
`LIBRARY_PATHS` env var, and all four pipeline scripts default `--db` to
the `DB_PATH` env var, which the Dockerfile sets to `/data/library.db`
unless your compose file overrides it). Either way, nothing that touches
the library or recomputes matches runs without you explicitly invoking
it — there's no cron, no auto-trigger on container start.

Your whole library should be bind-mounted **once**, at an identical path
on both sides of the colon (see the sample compose file's volumes
comment for the full reasoning) — both so DB-stored absolute paths are
portable between container and host-venv runs, and because staging a
file is a rename (instant, no extra disk usage), which only stays
instant when source and destination share the same mount. Docker treats
every separate `-v` line as its own mount for this purpose, *even when*
the host paths are subdirectories of the same underlying filesystem —
confirmed live: two subdirectories of one NFS export, bind-mounted
separately, still raised `OSError: [Errno 18] Invalid cross-device link`
on rename, silently falling back to a slow copy-then-delete on every
approval. `LIBRARY_PATHS` and `STAGE_DIR` should both point at
subdirectories reached through that one library mount; the SQLite DB
(under `/data`, see `DB_PATH`) doesn't need this treatment and can live
anywhere persistent.

`<your /data mount>/remux_cache/` also lives there — lossless re-tagged
copies of any source file that isn't directly browser-playable, either
because its MP4 codec tag is broken or because its actual container
(regardless of file extension) isn't one a browser understands (see
"Review UI"), built once per affected file on first playback attempt.
Budget disk space for this if many files turn out to need it; safe to delete
the whole directory at any time (rebuilt on next playback).

## Project layout

```
video-preview-matcher/
├── requirements.txt
├── Dockerfile
├── docker-compose.yml     generic sample — copy/edit for your own paths
├── docker-preview-matcher.yml   your local override (gitignored, optional)
├── rebuild.sh             docker compose build && up -d
├── VERSION
├── CHANGELOG.md
├── TUNING.md              Every tunable flag, with the real incidents/benchmarks behind each default
├── BENCHMARKS.md          Real numbers backing TUNING.md's defaults
├── src/
│   ├── db.py              SQLite schema + connection helper
│   ├── phash.py           Vendored perceptual hash + color signature
│   ├── procutil.py        Hang-safe subprocess timeout + debug logging
│   ├── 01_inventory.py    Stage 1: ffprobe catalog (multi-dir aware)
│   ├── 02_fingerprint.py  Stage 2: scene detection + hashing (+ optional VAAPI)
│   ├── 03_match.py        Stage 3: scoring
│   └── 04_serve.py        Stage 4: review UI server + scan orchestration
├── templates/
│   ├── index.html         Review queue
│   └── review.html        Side-by-side comparison page
├── static/
│   ├── style.css
│   ├── logo.svg           Favicon + Android home-screen icon + topbar logo
│   └── manifest.webmanifest
├── tests/
│   ├── render_templates_test.py
│   ├── staging_logic_test.py
│   ├── match_feedback_test.py
│   ├── match_scoring_test.py
│   ├── multi_dir_inventory_test.py
│   ├── scan_orchestration_test.py
│   ├── phash_test.py
│   ├── procutil_test.py
│   ├── scene_extraction_timeout_test.py
│   ├── fingerprint_worker_scaling_test.py
│   ├── fingerprint_write_test.py
│   └── playback_remux_test.py
├── contrib/
│   └── benchmark_settings.py   Finds good --workers/--hwaccel/--executor
│                                settings for *your* machine and library
└── data/
    ├── library.db          Created on first run
    ├── subprocess.log      ffmpeg/ffprobe/fpcalc call log (debug)
    └── remux_cache/        Lossless re-tagged copies for files with a broken MP4 codec tag or wrong container
```
