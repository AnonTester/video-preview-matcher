# Benchmarks

Real numbers from one specific machine — useful as a reference point, not
a guarantee for your hardware. Run `contrib/benchmark_settings.py` (see its
own [README](contrib/README.md)) against your own library to find good
settings for your own machine instead of assuming these transfer.

**Test environment**: AMD Ryzen 7 7840HS w/ Radeon 780M Graphics (16
logical cores), 32GB RAM, library on a network share, ~5907 fingerprinted
videos unless noted otherwise. Several of these runs were on an
otherwise-busy homelab host (~50 other containers running concurrently)
— absolute numbers reflect that, not an idle dedicated benchmark machine.

## Fingerprinting (`02_fingerprint.py`)

### CPU vs. VAAPI worker-count matrix

80-video stratified-random sample (23.6h of content total, spanning the
library's actual duration/resolution distribution — not hand-picked),
`--limit 80`, each row a full run from a fresh "needs fingerprinting"
state:

| `--workers` | `--hwaccel none` | `--hwaccel vaapi` |
|---|---|---|
| 1  | 560s (9.3m)  | 1916s (31.9m) |
| 4  | 468s (7.8m)  | 1321s (22.0m) |
| 8  | 468s (7.8m)  | 1436s (23.9m) |
| 12 | 470s (7.8m)  | 1462s (24.3m) |
| 15 | 470s (7.8m)  | 1726s (28.7m) |
| 16 | 473s (7.9m)  | 1893s (31.5m) |

**Takeaways:**
- CPU throughput plateaus *hard* at 4 workers and stays completely flat
  through 16 — going beyond 4 buys nothing, but doesn't hurt either.
  `--workers 1` → 4 gives a real ~17% speedup; nothing past that helps.
- VAAPI is consistently and substantially slower than CPU at every worker
  count tested — never closes the gap, let alone wins. Its own sweet spot
  is also around 4 workers (1321s), with real degradation both below
  (workers=1: 1916s) and above (workers=15-16: 1726-1893s) that point —
  consistent with a single shared hardware decode block that a few
  concurrent streams can usefully overlap, but that gets *worse* under
  more contention past a point, not just flat.
- **Conclusion: `--hwaccel none` (the default) is the right choice for
  this hardware for typical content.** The web UI's "GPU decode (VAAPI)"
  checkbox now defaults unticked for this reason.

### Per-video breakdown — does resolution/bitrate/duration change the picture?

Same 80-video sample, `--workers 4`, joined against each video's known
duration/resolution/bitrate from the `videos` table:

| Bucket | n | mean ratio (vaapi_time / cpu_time) |
|---|---|---|
| **All videos** | 80 | **3.00** |
| sub-720p | 25 | 3.23 |
| 720p+ | 55 | 2.90 |
| high-bitrate half | 40 | 2.77 |
| low-bitrate half | 40 | 3.24 |
| long-duration half | 40 | 2.97 |
| short-duration half | 40 | 3.04 |

**Takeaways:** the resolution/bitrate correlation is real but weak (~10-15%
relative difference between buckets) — VAAPI's disadvantage shrinks
slightly at higher resolution/bitrate, but never closes, and the only two
genuine 1080p samples in this set actually scored *above* the overall mean
ratio (3.54, 3.74), contradicting a clean "VAAPI wins above 1080p" story.
Duration shows essentially no correlation (2.97 vs 3.04) — what matters is
per-frame decode complexity, not total frame count. One video showed both
CPU and VAAPI finishing in <0.2s despite being 603s/720p — almost
certainly a corrupt/unreadable file that failed fast, excluded from the
pattern above.

### `--scene-detection-mode keyframe` (EXPERIMENTAL, not default)

Same 80-video sample, `--workers 4`, `--hwaccel none`:

| Mode | Time | Scene count (mean) | Scene count (median) | Hit `--max-scenes` (60) cap |
|---|---|---|---|---|
| `filter` (default) | 468s (7.8m) | 21.7 | 15 | 1/80 (1%) |
| `keyframe` | 334s (5.5m) | 52.6 | 59 | 29/80 (36%) |

**Takeaway:** `-skip_frame nokey` is a genuine ~29% fingerprinting speedup,
but scene counts balloon (keyframe placement is a GOP-interval/encoder
property, not a content-aware cut signal) — 36% of videos saturate
`--max-scenes`, vs. 1% under the real scene-cut signal. Since matching
cost scales roughly with scene-count², a ~2.4x scene-count increase would
plausibly make `03_match.py` several times *more* expensive per pair —
likely outweighing the fingerprinting-side savings entirely. Kept
experimental, not promoted to default.

### `--extract-max-dim` (downscale before hashing, default `1920`)

Same 80-video sample, `--workers 4`, `--hwaccel none`. First attempt
applied the scale filter *before* `select` (scaling every decoded frame,
not just the kept ones):

| Downscale ordering | Time |
|---|---|
| none (baseline) | 468s (7.8m) |
| before `select` (bug) | 2772s (46.2m) — **5.9x slower** |
| after `select` (fixed) | 464s (7.7m) — neutral |

Fixed by moving the scale filter to run only on frames that already
passed scene selection. Confirmed a true no-op for already-sub-threshold
content: when a video's known width/height fit within `--extract-max-dim`,
the filter is skipped from the ffmpeg command entirely, producing
byte-identical `scenes` rows to `--extract-max-dim 0`.

### The one real 4K video on record (id 5330, 3840x2160 h264, 27.5min)

`--workers 1`, each row a fresh fingerprint pass of just this video:

| `--hwaccel` | Downscale | Time | Peak memory |
|---|---|---|---|
| none | full-res | 206.5s | 1159MB |
| none | →1280px | 207.0s | 1030MB |
| vaapi | full-res | 281.4s | 924MB |
| vaapi | →1920px | **135.3s** | **838MB** |

Accuracy check (CPU full-res vs. CPU →1280px, same video): resulting pHash
values differed by Hamming distance **[2, 0, 0]** across the 3 detected
scenes (max 2, mean 0.67) — far under `--hash-threshold`'s default of 8.

**Takeaways:**
- VAAPI *alone* is still slower than CPU even at genuine 4K resolution
  (281.4s vs. 206.5s) — "VAAPI wins above 1080p" on its own doesn't hold.
- VAAPI *combined* with downscaling is dramatically faster than every
  other combination (135.3s — ~35% faster than the next-best,
  CPU+downscale's 207.0s) and uses the least memory. The mechanism:
  `scale_vaapi` shrinks the frame *before* `hwdownload`, cutting a
  per-frame GPU-to-host transfer whose cost scales with the *original*
  (huge, 4K) frame size — a benefit that's negligible at 720p (where the
  transfer is already small) but substantial at 4K.
- **This is why `--force-vaapi-above-max-dim` (default on) exists**:
  force VAAPI specifically for any video that already needs downscaling
  (i.e. exceeds `--extract-max-dim`), regardless of the general
  `--hwaccel`/scan-panel setting. Verified live: a normal sub-1920px video
  used plain CPU decode (`--hwaccel none` honored), while the 4K video in
  the same run automatically got `-hwaccel vaapi` + `scale_vaapi` despite
  `--hwaccel none` being passed for the run as a whole.

## Matching (`03_match.py`)

### Historical (pre-vectorization / pre-loky)

Real 5000-video library, 11,728,306 candidate pairs, `--workers 15`,
`--executor fork` (the only executor that existed at the time):

- Pre-vectorization (nested Python loops): ~16.5 minutes.
- Post-vectorization (numpy `score_scenes`): ~7.5 minutes — ~2.2x faster.
- Memory (`docker stats`, whole container): climbs from ~2.7-2.9GB
  shortly after starting to ~5.3-5.5GB at 50% progress to ~8GB near
  completion, dropping to ~42MB the instant the run finishes. Traced to
  copy-on-write page divergence under `fork`: pages shared between the
  main process and every forked worker gradually become private copies
  as each worker touches more of the preloaded scene data over the
  run — not a leak (no process's own RSS grows), not reclaimable by
  `gc.collect()`/`malloc_trim()` (nothing's freed; shared pages are just
  losing their shared status). See [TUNING.md](TUNING.md)'s `--executor`
  entry for why this is what motivated the `loky` work below.
- ~6000-video library, 16,673,930 pairs, 8 workers: ~12 minutes, memory
  climbing to **11.2GB**. Worker count didn't significantly change the
  peak.

### `--executor` and `--pool-generation-chunks`

Real ~6000-video library (5907 videos, 16,418,461 pairs):

| `--executor` | `--workers` | `--pool-generation-chunks` | Time | Peak memory | Completed? |
|---|---|---|---|---|---|
| fork | 8 | n/a | ~12min (documented above) | ~11.2GB | yes |
| loky | 8 | 0 (none) | — | >9.4GB, still climbing | **no — auto-aborted at 77%** |
| loky | 4 | 50 | 1510.9s (25.2min) | ~2.09GB | yes |
| loky | 8 | 20 | 1187.1s (19.8min) | ~2.40GB | yes |
| loky | 8 | 50 | 879.6s (14.7min) | ~2.55GB | yes |
| loky | 8 | 100 | **766.0s (12.8min)** | ~2.74GB | yes |
| loky | 16 | 50 | 916.8s (15.3min) | ~3.46GB | yes |

**Takeaways:**
- `--executor loky` *alone* (no forced recycling) is no better than
  `fork` — removing copy-on-write divergence doesn't help when there was
  never any sharing to diverge from in the first place under loky; memory
  climbed past 9.4GB before the run's own `--min-available-ram-percent`
  safety net aborted it (cleanly — no matches written, no leftover
  processes).
- `loky` + forced recycling is the first thing in this entire
  investigation that actually bounds memory — every recycling interval
  tested (20/50/100 chunks) completed at ~2.4-2.74GB peak (~4x lower
  than `fork`'s 11.2GB).
- **Worker count**: `--workers 4` doesn't meaningfully reduce the memory
  ceiling vs. 8 (~2.09GB vs ~2.55GB) but is dramatically slower (+72%
  runtime) — a worse trade-off, not better. `--workers 16` raises the
  ceiling substantially (~3.46GB, +35% vs. 8) for *no* speed benefit
  (916.8s vs. 879.6s — actually marginally slower) — confirms each loky
  worker's own private-copy cost scales with worker count, unlike
  `fork`. **`--workers 8` is the real sweet spot**, now confirmed by
  direct comparison rather than just matching an older `fork` benchmark
  for comparability — this is why `03_match.py`'s `--workers` default
  changed from an auto-detected `cpu_count - 1` to a fixed `8`.
- **Recycling interval**: across 20/50/100 chunks, memory stayed in a
  narrow ~2.40-2.74GB band (a ~14% spread) while runtime varied by ~35%
  (1187.1s down to 766.0s) — looser recycling (100) is meaningfully
  faster for a memory cost too small to matter once already this far
  inside a safe range. **`--pool-generation-chunks 100` is the new
  default** for this reason — the priority is avoiding the kind of
  unbounded multi-GB growth seen with no recycling at all, not shaving
  the last few hundred MB once already safely bounded.

### Fingerprinting worker count (older, mixed-library field notes)

Real experienced results, but with different mixed video sets each time —
informative, not a controlled benchmark:

- 1000 mixed videos, 8 workers: ~4h
- 2000 mixed videos, 6 workers: ~13h
- 1000 mixed videos, 8 workers: ~9h
- 1000 mixed videos, 4 workers: ~6h

Memory usage with 4 workers: ~1.8GB.
