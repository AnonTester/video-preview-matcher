# Preview Matcher

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
cd preview-matcher

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
# tuning thresholds below.

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
- **GPU decode (VAAPI)**: on by default, mirrors `--hwaccel vaapi` above
   — only affects the fingerprint stage's decode step, not the rest of 
   the pipeline.
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

## Tuning — READ THIS BEFORE TRUSTING THE OUTPUT

This was built and validated against a small synthetic test library (see
`tests/` and the conversation that produced this code), not your actual
6000-file library. The defaults are reasonable starting points, not
calibrated values. Expect to spend real time here:

- **`--scene-threshold`** (`02_fingerprint.py`, default `0.3`): ffmpeg's
  scene-change sensitivity. Lower = more scenes detected. In testing
  against low-texture synthetic clips this missed real cuts at `0.15`,
  let alone `0.3` — run `--limit 20` against a handful of *real* preview
  files first and manually check `scenes` table row counts look sane
  (`SELECT video_id, COUNT(*) FROM scenes GROUP BY video_id`) before
  committing to a full run.

- **`--top-margin` / `--bottom-margin` / `--side-margin`**
  (`02_fingerprint.py`, defaults `0.12/0.12/0.06`): fraction of the frame
  excluded before hashing, to dodge logos/bars/scrollers. These are
  guesses. Pull a few actual preview frames
  (`ffmpeg -i preview.mp4 -ss 5 -frames:v 1 frame.jpg`) and eyeball where
  your overlays actually sit — adjust margins to comfortably clear them
  without cropping into real content.

- **`--blank-std-threshold`** (`02_fingerprint.py`, default `4.0`): skips
  fingerprinting a frame if its grayscale standard deviation is below
  this — catches fade-to-black transitions and blank intro/logo cards,
  which otherwise hash to the same degenerate value across *every* video
  in the library and produce confident-looking but meaningless matches
  (confirmed in real use — see CHANGELOG 0.3.0). Raise it if you suspect
  legitimately low-contrast (but not actually blank) scenes are getting
  skipped; lower it if it's not catching frames that are visibly blank.

- **`--hash-threshold`** (`03_match.py`, default `8`, range 0-64): max
  Hamming distance to count two frames as the same scene. Lower = stricter.
  Real-world re-encoding/bitrate differences will push this up from what
  worked in lightly-compressed synthetic tests. If you see obviously-correct
  matches scoring low, raise this; if you see false positives, lower it.
  (Lowered from the original default of 12 after three real false
  positives all turned out to sit exactly at that boundary — see
  `--min-matched-scenes` below for the other half of that fix.)

- **`--color-threshold`** (`03_match.py`, default `0.25`): guards against
  pHash's color-blind-spot (see `phash.py` docstring on `color_signature`)
  — two structurally-similar but differently-colored frames shouldn't
  count as a match. Loosen if legitimately-cropped scenes are getting
  rejected (cropping shifts the color histogram).

- **`--min-ratio` / `--max-ratio`** (`03_match.py`, defaults `0.02`/`0.95`):
  preview-duration / candidate-duration bounds for the prefilter. If your
  previews can be a larger fraction of the source than 95%, widen this.

- **`--min-matched-scenes`** (`03_match.py`, default `3`): a match is only
  stored if at least this many scenes matched, *in addition to* clearing
  `--min-visual-score`'s fraction. Added because a preview with very few
  total scenes (e.g. 2) only needs *one* coincidental match to clear a
  fraction-only threshold (1/2 = 50%) — exactly what happened with three
  real false positives in this library, each a single isolated match at
  the hash-threshold boundary on a 2-6-scene preview. A real preview
  splices together several moments from its source; one matching scene
  is weak, easily-coincidental evidence on its own, not confirmation.

- **`--min-scene-duration`** (`03_match.py`, default `2.0` seconds): drops
  any scene whose gap to the next scene-cut in its own video is shorter
  than this, before scoring. Added after a real false positive (review
  video #2237): a shared intro/logo animation got chopped by scene
  detection into several quick cuts, all of which matched and cleared
  `--min-matched-scenes` (3/3) — but they were all the same ~4-second
  sting, not independent evidence. A scene this short isn't an
  independently identifiable moment either way. 2.0s is a starting
  point, not calibrated — raise it if real rapid-cut footage keeps
  slipping through, lower it if legitimate short scenes are being
  dropped.

- **`--min-match-spread`** (`03_match.py`, default `2.0` seconds): skips
  storing a match if its matched scenes' *preview* timestamps span less
  than this many seconds — several matched scenes that are all really
  the same narrow moment (e.g. a shared title card) aren't independent
  corroboration just because there happen to be `--min-matched-scenes`
  of them. Same starting-point caveat as `--min-scene-duration` above.

- **Audio scoring** (`chromaprint_similarity` in `03_match.py`): shipped
  as a deliberately simple bit-overlap comparator, explicitly flagged in
  its docstring as a placeholder. Since previews here may have narration
  replacing the original audio, audio is already weighted lower (0.35)
  and skipped entirely (not penalized) when fingerprints don't both exist
  — but for the cases where it *does* apply, swapping in proper
  `pyacoustid`-based offset-alignment would meaningfully improve audio
  scoring accuracy. Worth doing if early review passes show visual-only
  scoring isn't discriminating well enough on its own.

- **`--hwaccel`** (`02_fingerprint.py`, default `none`): set to `vaapi` to
  decode on a VAAPI-capable GPU instead of the CPU (the `select`
  scene-detection filter still runs in software either way, since it
  needs raw frame data — only decode is offloaded). This uses the AMD GPU
  at `/dev/dri/renderD128` by default (`--hwaccel-device` to override). 
  Multiple `--workers` share the same physical decode hardware under 
  vaapi, so tune worker count against GPU throughput, not just CPU core 
  count, once this is on.

- **`--workers`** (`02_fingerprint.py`, default `4`): each worker is a
  full ffmpeg decode pass plus GPU/CPU contention if `--hwaccel vaapi` is
  on, so this is genuinely constrained by hardware throughput, not just
  "more is better" — 4 was found to be about right for CPU-only decode, 
  and roughly the same under VAAPI (GPU `Enc` sits at 90%+ already at that
  count per `nvtop`/`radeontop`). Can be raised or lowered *live* while a
  web-UI-triggered scan is running (min 2, max = CPU core count) — see
  "Triggering scans from the web UI" above — so it's worth experimenting
  with mid-run rather than guessing up front and restarting.

- **`--workers`** (`03_match.py`, default cpu count − 1): no I/O/GPU
  contention (pure in-memory hashing after the preload), so more cores
  should mostly help CPU-wise — but RAM is a separate, still-unresolved
  story. On the real ~5000-video library, a full 15-worker run's memory
  climbs steadily over the run (~2.7GB → ~5.5GB → ~8GB), dropping to
  baseline the instant it finishes. This traces to copy-on-write
  divergence: pages shared between the main process and every forked
  worker gradually become private copies as each worker touches more of
  the preloaded scene data — confirmed *not* a leak (no process's own
  RSS or object count grows over a run) and not reclaimable by
  `gc.collect()`/`malloc_trim()` (nothing's actually freed; shared pages
  are just losing their shared status). The obvious fix — recycling
  workers via `--max-tasks-per-child` — is unsafe: `ProcessPoolExecutor`
  forks a replacement worker while its own background thread is still
  alive, a real, currently unresolved CPython bug present in every
  version with this feature (3.11 onward), not specific to the version
  in use here —
  [cpython#90622](https://github.com/python/cpython/issues/90622) and
  [#115634](https://github.com/python/cpython/issues/115634) (open,
  with active discussion as recently as May 2026 — a Python upgrade or
  downgrade will not avoid it). Leave `--max-tasks-per-child` at `0`.
  `--pool-generation-chunks` (periodic full pool teardown + recreation
  instead of in-place recycling) sidesteps that specific deadlock, but
  measured live against the real library it made the memory *peak*
  worse, not better — still an open problem. `--workers 1` is the only
  currently memory-bounded option for very large libraries. 

- **`--progress-interval`** (`03_match.py`, default `10.0` seconds,
  `--workers > 1` only): target seconds of work per chunk, so progress
  updates land roughly this often. Without it, chunk *count* is fixed at
  `workers * 4` (floored at 40) regardless of how many pairs there are —
  fine for a small test run (each chunk finishes in seconds either way)
  but means a real full-library run's chunks grow in lockstep with the
  pair count, reporting progress only every few minutes once there are
  millions of pairs. Translated to a pairs-per-chunk target via a rough
  single-worker throughput estimate (`PAIRS_PER_WORKER_SEC` in
  `03_match.py`) measured from the one real pre-vectorization benchmark
  on record — likely conservative now that scoring is vectorized
  (chunks will probably finish faster than requested, not slower), so
  treat the actual observed cadence as the thing to calibrate against,
  not this default.

**Recommended first real run:** `--limit 100` (or point `01_inventory.py`
at a small subdirectory) through the whole pipeline, review results in the
UI, adjust thresholds, repeat — before committing to fingerprinting 
thousands of files. Fingerprinting is the expensive stage.

### Benchmarks

Real numbers from one specific machine (AMD Ryzen 7 7840HS w/ Radeon
780M Graphics, 32GB RAM) — a useful reference point, not a guarantee for
your hardware.

- **Matching (`03_match.py`)**, real 5000-video library, 11,728,306
  candidate pairs after the duration prefilter, `--workers 15`:
  - Pre-vectorization (nested Python loops): ~16.5 minutes.
  - Post-vectorization (numpy `score_scenes`): ~7.5 minutes — roughly
    2.2x faster, consistent across multiple runs.
  - Memory (`docker stats`, whole container): climbs from ~2.7-2.9GB
    shortly after starting to ~5.3-5.5GB at 50% progress to ~8GB near
    completion, dropping to ~42MB the instant the run finishes — see
    the `--workers` entry above for why this still isn't resolved.
  - ~6000-video library, 16,673,930 candidate pairs, 8 workers, total time ~12 minutes, memory usage gradually climbing to 11.2GB
  - amount of CPU workers does not appear to significantly impact the memory usage during matching
- **Fingerprinting (`02_fingerprint.py`) worker count**: 4 workers
  tested faster than 6 or 8 on this hardware — more workers contending
  for the same decode hardware/CPU cache stopped paying off well before
  the core count. Real experienced results for info, but with mixed 
  different videos - not a benchmark!:
  - 1000 mixed videos - 8 workers - total time ~4h
  - 2000 mixed videos - 6 workers - total time ~13h
  - 1000 mixed videos - 8 workers - total time ~9h
  - 1000 mixed videos - 4 workers - total time ~6h
  Memory usage with 4 workers is around 1.8GB
- **Fingerprinting CPU vs. GPU (`--hwaccel vaapi`) decode time**: not
  yet benchmarked head-to-head. VAAPI decode has been validated for
  *correctness* (right scene count, correct decode) on this hardware's
  AMD iGPU, but no real before/after timing comparison has been run —
  worth doing before assuming GPU decode is actually faster in practice
  for this specific workload.

## Review UI

The layout is responsive (queue rows, scan panel, and the side-by-side
video comparison all reflow for a phone-width screen) — useful for
checking on a long-running scan, or doing a quick approve/reject pass,
from a phone without it just rendering a zoomed-out desktop page.

- Queue (`/`) has two tabs: **Pending** lists every undecided-or-rejected
  preview with at least one candidate match above the noise floor,
  sorted by confidence (undecided + highest-confidence first); **Staged**
  lists every preview currently staged for deletion, most recently staged
  first — kept separate so you can review/undo what's about to be
  deleted without scrolling past everything else, and (unlike Pending)
  never affected by a later re-scan or re-match changing what it's
  shown next to. Each tab is paginated, 40 rows per page. A preview or
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
  guards against, see "Tuning"), and crop/flip variant, plus a summary
  line stating whether the matches spread across multiple distinct
  moments in the preview or are a single isolated point (a single match
  is weak, coincidental-prone evidence — see "Tuning" →
  `--min-matched-scenes`/`--min-match-spread`) — useful for judging a
  match even when (or especially when) playback isn't available, and for
  spotting a confidently-wrong match before trusting it.
- If a file fails to play in-browser, you'll see an inline message
  instead of a silent failure (most real causes — like the broken-codec-tag
  issue below — are auto-fixed transparently and shouldn't trigger this).
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
copies of any source file whose MP4 codec tag is broken (see "Review
UI"), built once per affected file on first playback attempt. Budget
disk space for this if many files turn out to need it; safe to delete
the whole directory at any time (rebuilt on next playback).

## Project layout

```
preview-matcher/
├── requirements.txt
├── Dockerfile
├── docker-compose.yml     generic sample — copy/edit for your own paths
├── docker-preview-matcher.yml   your local override (gitignored, optional)
├── rebuild.sh             docker compose build && up -d
├── VERSION
├── CHANGELOG.md
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
├── static/style.css
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
│   └── fingerprint_write_test.py
└── data/
    ├── library.db          Created on first run
    ├── subprocess.log      ffmpeg/ffprobe/fpcalc call log (debug)
    └── remux_cache/        Lossless re-tagged copies for files with broken MP4 codec tags
```
