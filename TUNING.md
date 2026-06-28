# Tuning — READ THIS BEFORE TRUSTING THE OUTPUT

This was built and validated against a small synthetic test library (see
`tests/` and the conversation that produced this code), not your actual
6000-file library. The defaults are reasonable starting points, not
calibrated values. Expect to spend real time here. [BENCHMARKS.md](BENCHMARKS.md)
has real numbers from one specific machine — useful as a reference point,
not a guarantee for yours; run `contrib/benchmark_settings.py` against your
own library to find good `--workers`/`--hwaccel`/`--executor` settings for
your own hardware instead of assuming these transfer.

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
  stored if at least this many *distinct candidate scenes* matched, *in
  addition to* clearing `--min-visual-score`'s fraction — not the raw
  count of matched preview scenes; several preview scenes matching the
  identical candidate scene collapse into one piece of evidence (see
  the `--min-candidate-match-spread` entry below for the real false
  positive — video #936 — that found this distinction mattered, not
  just in theory). Added because a preview with very few total scenes
  (e.g. 2) only needs *one* coincidental match to clear a fraction-only
  threshold (1/2 = 50%) — exactly what happened with three real false
  positives in this library, each a single isolated match at the
  hash-threshold boundary on a 2-6-scene preview. A real preview
  splices together several moments from its source; one matching scene
  is weak, easily-coincidental evidence on its own, not confirmation.

- **`--min-scene-duration`** (`03_match.py`, default `5.0` seconds): drops
  any scene whose gap to the next scene-cut in its own video is shorter
  than this, before scoring. Added after a real false positive (review
  video #2237): a shared intro/logo animation got chopped by scene
  detection into several quick cuts, all of which matched and cleared
  `--min-matched-scenes` (3/3) — but they were all the same ~4-second
  sting, not independent evidence. A scene this short isn't an
  independently identifiable moment either way. Raised from an initial
  `2.0` after real review kept surfacing short flash/strobe-ish cuts
  that still slipped through at that floor — raise it further if
  rapid-cut footage keeps slipping through, lower it if legitimate
  short scenes are being dropped.

- **`--min-match-spread`** (`03_match.py`, default `2.0` seconds): skips
  storing a match if its matched scenes' *preview* timestamps span less
  than this many seconds — several matched scenes that are all really
  the same narrow moment (e.g. a shared title card) aren't independent
  corroboration just because there happen to be `--min-matched-scenes`
  of them. Same starting-point caveat as `--min-scene-duration` above.

- **`--min-candidate-match-spread`** (`03_match.py`, default `2.0`
  seconds): the same spread check, mirrored onto the matched scenes'
  *candidate* timestamps. Added after a real false positive (review
  video #4059): a ~2.8s intro appeared three separate times in the
  preview, well spread out — clearing `--min-match-spread` on the
  preview side — but the candidate only had that intro once, so all
  three matches collapsed onto one candidate timestamp. The candidate
  wasn't a match at all; `--min-match-spread` alone can't see that,
  since it never looks at the candidate side. Both spread checks must
  pass — corroboration needs independence on both sides, not just one.
  **This alone wasn't enough**, found via a second real false positive
  (review video #936): 6 preview scenes (a repeated camera-flash frame)
  all matched the *identical* candidate timestamp, but one *additional*,
  genuinely unrelated coincidental match elsewhere in the candidate was
  on its own enough to clear this spread check — spread (max − min) is
  blind to *repetition* of the same value, since duplicates never move
  the min or the max. Fixed in `--min-matched-scenes` itself (see
  above), not here — this check's own spread math needed no change.

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
  count, once this is on. **Measured live to be consistently slower than
  CPU decode across this library** (~3x on average, no resolution/bitrate
  pocket where it clearly wins on its own) — see
  [BENCHMARKS.md](BENCHMARKS.md) for the full per-video breakdown. Off by
  default in both the CLI and the web UI's scan panel for this reason.

- **`--extract-max-dim`** (`02_fingerprint.py`, default `1920`): downscales
  extracted frames above this size before hashing — pHash never needs full
  source resolution. A genuine no-op below the threshold (the filter is
  skipped entirely, not just asked to do nothing) and accuracy-preserving
  above it (hash distance 0-2 vs the unscaled version on the one real 4K
  video on record, far under `--hash-threshold`'s default of 8) — see
  [BENCHMARKS.md](BENCHMARKS.md).

- **`--force-vaapi-above-max-dim`** (`02_fingerprint.py`, default on):
  forces VAAPI decode for any single video whose known resolution exceeds
  `--extract-max-dim`, regardless of `--hwaccel`/the scan panel checkbox.
  VAAPI *alone* is slower than CPU even on 4K content, but VAAPI
  *combined* with the downscale above is a real, substantial win
  specifically for oversized video (scaling on the GPU before the
  transfer back to host memory, instead of after a slow full-resolution
  CPU decode) — see [BENCHMARKS.md](BENCHMARKS.md) for the numbers this
  is based on.

- **`--workers`** (`02_fingerprint.py`, default `4`): each worker is a
  full ffmpeg decode pass plus GPU/CPU contention if `--hwaccel vaapi` is
  on, so this is genuinely constrained by hardware throughput, not just
  "more is better." Measured live on this hardware: CPU-mode throughput
  plateaus hard at 4 workers and stays flat all the way to 16 (no further
  benefit, no real penalty either) — see
  [BENCHMARKS.md](BENCHMARKS.md). Can be raised or lowered *live* while a
  web-UI-triggered scan is running (min 2, max = CPU core count) — see
  "Triggering scans from the web UI" in [README.md](README.md) — so it's
  worth experimenting with mid-run rather than guessing up front and
  restarting.

- **`--workers`** (`03_match.py`, default `8`): no I/O/GPU contention
  (pure in-memory hashing after the preload), but RAM behaves very
  differently depending on `--executor` (below) — see
  [BENCHMARKS.md](BENCHMARKS.md) for the full worker-count-vs-memory
  matrix this default was picked from.

- **`--executor`** (`03_match.py`, default **`loky`**): `loky`
  (third-party, `pip install loky` — already in `requirements.txt`)
  launches workers via `fork()`-then-`exec()` instead of plain
  `fork()`, which is what makes periodic recycling *safe* (no inherited
  threads/locks survive the `exec()`, sidestepping a real, still-open
  CPython deadlock —
  [cpython#90622](https://github.com/python/cpython/issues/90622),
  [#115634](https://github.com/python/cpython/issues/115634) — present
  in every Python version with `--max-tasks-per-child`, 3.11 onward, not
  specific to the version in use here; a Python upgrade or downgrade will
  not avoid it) — at the cost of giving up copy-on-write sharing of the
  preloaded scene/audio data entirely; each worker deserializes its own
  private copy from the start instead. `--executor fork`'s memory climbs
  unboundedly on a large enough library (copy-on-write divergence — see
  [BENCHMARKS.md](BENCHMARKS.md) for the mechanism and the numbers); loky
  alone doesn't fix that (nothing was shared to diverge from in the first
  place, so removing the divergence mechanism doesn't help), but loky
  *combined* with `--pool-generation-chunks` (below) does, and is why
  this combination is now the default — **promoted from opt-in on
  explicit instruction**: bounded memory matters more than speed, full
  stop, a slower run that finishes safely beats a fast one that risks
  taking the host down. `--executor fork` remains available as the
  explicit "raw"/opt-out path (e.g. to avoid the `loky`/`psutil` runtime
  dependencies entirely), but its memory use is *not* bounded by anything
  else in this script except `--min-available-ram-percent` below. Two
  real loky bugs had to be worked around to get here (a `ctypes.CDLL`
  global that crashed every loky run outright, and a confirmed open
  upstream bug where loky's default pickler makes initializer-set state
  invisible to later tasks) — see `03_match.py`'s module docstring for
  both.

- **`--pool-generation-chunks`** (`03_match.py`, default `100` under
  `--executor loky`, `0`/disabled under `--executor fork`): periodically
  tears down and recreates the whole worker pool, re-forking/re-spawning
  fresh workers. Under `fork`, this measured *worse*, not better, against
  the real library (peak memory went up, not down — still an open,
  unexplained result) — leave at `0` there. Under `loky`, this is what
  actually keeps memory bounded — every interval tested (20/50/100
  chunks) held memory in a narrow ~2.4-2.74GB band while runtime varied
  ~35% across them; `100` (the fastest of the three) was picked since
  that memory difference is too small to matter once already this far
  inside a safe range — see [BENCHMARKS.md](BENCHMARKS.md) for the full
  matrix across worker counts and chunk values this default was picked
  from.

- **`--min-available-ram-percent`** (`03_match.py`, default `8.0`,
  `--workers > 1` only): a second, independent safety net, on top of
  (not instead of) the executor choice above — checks real system
  memory (`psutil.virtual_memory()`'s AVAILABLE figure, not FREE) after
  every completed chunk, under *either* executor, and aborts the moment
  it drops below this percentage of total RAM: no matches written,
  existing data untouched, the same safety property any other
  interruption already has. Kills every worker process directly rather
  than waiting for in-flight chunks to drain (`_force_kill_pool()` —
  loky's own `shutdown(kill_workers=True)`, or a direct `kill()` of each
  `ProcessPoolExecutor` child under `--executor fork`). Exists
  specifically so `--executor fork` (chosen deliberately) or some
  future `--workers`/`--pool-generation-chunks`/library-size combination
  under `--executor loky` that isn't actually bounded the way the tested
  combinations were still fails safely instead of taking the whole
  host down. Verified live by forcing an immediate trip (an unreachable
  threshold against a small seeded DB, both executors): clean exit,
  zero rows written, zero leftover worker processes — never verified by
  actually exhausting host memory for real, by design.

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

See [BENCHMARKS.md](BENCHMARKS.md) for the real numbers behind these
defaults, and run `contrib/benchmark_settings.py` (see its own
[README](contrib/README.md)) to find good settings for your own hardware
and library instead of assuming these transfer.
