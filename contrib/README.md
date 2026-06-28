# contrib/

Auxiliary tooling that isn't part of the core pipeline (`01`-`04`) — not
required for normal use, not run automatically by anything.

## `benchmark_settings.py`

Finds good `--workers` / `--hwaccel` / `--executor` /
`--pool-generation-chunks` settings for **your** machine and **your**
library. Every number in [BENCHMARKS.md](../BENCHMARKS.md) comes from
one specific machine — useful as a reference point, not a guarantee for
different hardware, a different GPU, or a different mix of file
sizes/resolutions/bitrates. This script measures your own, instead of
assuming someone else's numbers transfer.

```bash
python3 contrib/benchmark_settings.py --db data/library.db

# Inside Docker, to also exercise VAAPI decode (needs /dev/dri
# passthrough, only available there):
docker compose run --rm app python3 contrib/benchmark_settings.py --db /data/library.db
```

**Never touches your real database** — every test runs against a
throwaway copy, deleted when the script finishes (or on an early exit —
nothing it does is destructive to `--db` itself).

What it needs to actually have something to measure:
- The **fingerprinting** benchmark needs `01_inventory.py` to have
  already run (so `videos.duration_sec`/`width`/`height` are
  populated) — it doesn't need anything fingerprinted yet, and it
  doesn't touch your real fingerprint progress; it forces re-fingerprinting
  of a *sampled copy* of some already-probed videos.
- The **matching** benchmark needs at least some videos already
  fingerprinted — it's read-only against `scenes`/`audio_fp`, no
  fingerprinting happens here at all.

Either half is skipped, with a clear message, if there isn't enough
data for it yet — it won't error out just because you haven't run the
whole pipeline first.

The default worker-count sweep (1, half, all cores - 1) is deliberately
small so a first run finishes in a reasonable time. For a more thorough
sweep — e.g. the one used to produce this project's own README
benchmarks — pass the worker counts explicitly:

```bash
python3 contrib/benchmark_settings.py --db data/library.db \
    --fp-workers 1 4 8 12 15 16 \
    --match-workers 1 4 8 12 15 16 \
    --pool-generation-chunks 0 20 50 100 200
```

Sample sizes are bounded by default (`--fp-sample-size`, `--match-max-videos`)
so a large library doesn't turn this into a multi-hour run by accident —
raise them if you want a result closer to true full-library behavior, at
the cost of a much longer benchmark.

At the end it prints every result it measured, plus a recommendation —
for matching, biased toward the **lowest peak memory** option by
default, not the fastest, matching this project's own stated priority
(see [TUNING.md](../TUNING.md)): a slower run that finishes safely
beats a fast one that risks taking the whole host down. The fastest
alternative is still shown alongside it, for anyone who'd rather
optimize for speed on a host that has memory to spare.
