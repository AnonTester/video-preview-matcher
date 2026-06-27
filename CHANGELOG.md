# Changelog

## 2026-06-27 — 0.16.0

- **Added a Rejected tab** to the review queue, alongside Pending/
  Staged — rejecting a preview by mistake previously meant it just sank
  to the bottom of the Pending list, mixed in with genuinely undecided
  ones, with no dedicated way to find and reconsider it. Rejected
  previews are now sourced directly from `decisions` (`rejected_queue_rows`
  in `04_serve.py`, sharing a new `_decision_queue_rows` helper with
  `staged_queue_rows`) — same reasoning as staged: independent of
  `matches`/`missing_since`, so a rejection stays reachable and undoable
  even if a later `03_match.py` re-score drops the pair below threshold.
  The existing "undo" button on the review page already worked for any
  decision status, not just staged, so no changes were needed there —
  verified live end-to-end: rejecting, then undoing from the Rejected
  tab, correctly returns a preview to Pending. The Pending tab itself
  now only ever shows truly-undecided previews (it used to also include
  rejected ones, sunk to the bottom) — Pending/Staged/Rejected are now a
  clean three-way split with no overlap.
- **Removed the preview's duration ("344s") from the queue list rows**
  — per feedback, raw seconds in a list view isn't useful information
  (duration is still shown, human-readable, on the review page's card
  headers). Also dropped `candidate_duration`, which turned out to
  already be selected by the queue queries but never actually rendered
  anywhere.

## 2026-06-27 — 0.15.2

- **Fix (real feedback, immediately after 0.15.1 shipped): tab/
  pagination clicks still moved the scroll position — 0.15.1's "jump to
  just below the topbar" was an improvement on jumping to the absolute
  page top, but still not what was wanted.** Switching tabs/pages swaps
  the row list in place via AJAX; nothing above or around it changes,
  so there's no reason to move scroll at all. Removed
  `scrollQueueIntoView()`'s call entirely — `loadQueue` no longer
  touches scroll position in any way (it's still naturally clamped if
  the new tab/page has less content than the current scroll offset,
  which is unavoidable, not a bug).
- **Fix (real bug, same feedback): the scroll position restored after
  returning from a review page drifted a little lower each time,
  compounding on repeated round trips.** Root cause: the restore ran on
  `window.addEventListener('load', ...)`, but `refreshStaging()`/
  `refreshMissing()`/`loadScanRoots()`/the first `pollScanStatus()` are
  independent fetches that can resolve *after* `load` and change page
  height above the queue (e.g. the staging-bar becoming visible).
  Restoring before they'd settled set the right scrollY for the page's
  *current*, not-yet-final height; when one of them then inserted
  content above, Chrome's scroll anchoring nudged scrollY again to keep
  the same pixels in view — landing a bit lower than originally saved.
  That drifted position then got re-saved as "correct" on the next
  `pagehide`, compounding further on every subsequent round trip.
  Fixed by waiting for all four of those calls to settle
  (`Promise.allSettled`, not `Promise.all` — one failed fetch must not
  block the others from letting the restore happen) before restoring,
  instead of just waiting for `load`. Verified live: tab and bottom-
  pagination clicks now leave `scrollY` provably unchanged, and the
  review round-trip restores the *exact* scroll offset across two
  repeated trips in a row (no drift at all, not just a smaller one).

## 2026-06-27 — 0.15.1

- **Fix (real bug, found via user report on mobile, immediately after
  0.15.0 shipped): pagination/tabs were still doing a full page
  reload, not AJAX.** `loadQueue` is an `async function`; the tab/
  pagination links called it as `onclick="return loadQueue(...)"` —
  but that makes the handler's *synchronous* return value the Promise
  `loadQueue` returns immediately, which is truthy, not the literal
  `false` needed to cancel the link's default navigation. The browser
  always followed the `href` regardless, while `loadQueue` raced
  pointlessly in the background. 0.15.0's own verification missed this
  because it called `loadQueue(...)` directly via `Runtime.evaluate`,
  never dispatching a real click through the actual `<a>` element —
  exercising the function, not the bug. Fixed by changing every
  occurrence to `onclick="loadQueue(...); return false;"` (fire the
  async call, then synchronously return the literal `false` the click
  handler actually needs). Re-verified this time with real
  `element.click()` dispatch, not direct function calls.
- **Fix (real bug, same report): switching tabs/pages jumped the
  scroll all the way to the absolute top of the page** (above the
  topbar/staging-bar/scan-panel), not just to the top of the queue
  list — a much bigger, more disorienting jump on mobile, where that's
  a lot of vertical space to re-scroll past on every click. Was a
  literal `window.scrollTo(0, 0)`. Replaced with `scrollQueueIntoView()`,
  which scrolls so `.queue-header`'s top lands just below the sticky
  topbar's *actual* rendered height (read live via
  `getBoundingClientRect()`, not a hardcoded pixel guess — correct
  whether the topbar is single-line on desktop or wraps taller on
  mobile).
- Re-verified the full flow (AJAX tab switch, AJAX pagination, and
  returning from a review page) end-to-end at a real mobile viewport
  size (375×812) with real click dispatch this time, not just the
  desktop-sized, direct-function-call check 0.15.0 shipped with.

## 2026-06-27 — 0.15.0

- **Queue tabs/pagination now use AJAX instead of full page navigation.**
  Every tab switch or page click was a full reload, which reset scroll
  position every time — annoying when paging through a long list. Added
  `GET /api/queue?tab=&page=` (backed by the same `_queue_page_data()`
  the HTML route already used) and client-side JS (`loadQueue`/
  `renderQueue` in index.html) that re-renders the title, tabs, both
  pagination blocks, and the row list in place, using `history.pushState`
  to keep the URL in sync without a reload. Browser back/forward between
  these AJAX-loaded states is handled via a `popstate` listener — no
  reload there either. `history.scrollRestoration = 'manual'` is set
  explicitly so the browser's own scroll-restoration heuristics never
  fight this.
- **Scroll position is now restored when returning to the queue from a
  review page — both via "back to queue" and after staging/keeping/
  dismissing.** Saved to `sessionStorage` on `pagehide` (fires on any
  navigation away, including the real, full-page navigation to
  `/review/{id}` and back), restored on the queue page's `load` event.
  Found via live testing (a real, reproducible failure, not assumed):
  restoring synchronously in the inline script — *before* `load` — did
  move `scrollY`, but the browser's own "reset scroll to top for a new
  navigation" behavior ran afterward and silently undid it every time;
  deferring the restore to `load` (after that reset has already
  happened) fixes it. Verified against a real headless-browser session
  driving the actual AJAX tab/pagination flow end-to-end (scripted over
  raw DevTools Protocol via a small stdlib-only WebSocket client, no new
  dependency) — confirmed AJAX tab switches update the title/active-tab/
  URL/row-count/scroll-to-top correctly, browser back/forward between
  them works without a reload, and the realistic "scroll down → open a
  review page → return" flow restores the exact scroll offset.

## 2026-06-26 — 0.14.3

- **Fix (real cleanup gap, found via direct user question before it
  caused a real complaint): `remux_cache/{id}.mp4` was never deleted
  when its video stopped existing.** `/api/purge-staging` permanently
  deletes a staged file but never touched a cached remux of it (built
  if anyone played it before purging); `/api/missing-files/prune`
  cascades away a video's entire DB history but nothing ever pointed
  back at its remux cache entry either. Both would have leaked an
  orphaned copy on disk forever. Fixed by extracting both endpoints'
  core logic into pure, testable functions (`purge_staging_files`,
  and `prune_missing_files` which now also takes the remux cache dir)
  that delete each affected video's `remux_cache/{id}.mp4` as part of
  the same operation — keyed off exactly the ids being purged/pruned,
  never a directory-wide sweep. The live cache had exactly one entry at
  the time (not orphaned), so no retroactive cleanup was needed, only
  the prospective fix.

## 2026-06-26 — 0.14.2

- **Review page card redesign.** Each video-comparison card's header
  showed the filename right next to the "preview"/"matched full
  version" tag, with the full path repeated as a single line below it —
  redundant, and left no room for duration/resolution/filesize. The
  header now shows those three (human-readable, right-aligned) instead
  of the filename; the path line below is now folder on its own line,
  filename on the next. Computed server-side (`_fmt_video_meta` in
  `04_serve.py`) for the initial render, mirrored in JS (`fmtVideoMeta`)
  for the candidate panel, which re-renders without a page reload when
  switching between match candidates. Mobile: `.panel-head` now stacks
  the tag and meta into a column instead of one row, since the tag
  alone wrapping to two lines was squeezing meta down to where it got
  truncated mid-word.
- **Fix (real bug, found via user report — video #5855): a file named
  `*.mp4` was actually raw MPEG-TS, and failed to play with "no video
  with supported format and MIME type found."** The existing broken-
  codec-tag fix only ever checked `codec_tag`, which for this file is a
  real, non-zero MPEG-TS stream_type (not the broken-MP4-tag pattern at
  all), so it never triggered a remux. Widened the check
  (`_probe_playability`/`_parse_playability` in `04_serve.py`, replacing
  `_has_broken_codec_tag`) to also remux when the file's actual
  container (`ffprobe`'s `format_name`) isn't one a browser understands,
  regardless of extension or codec_tag.
- **Safety fix found while building the above, before it shipped:
  applying the codec-tag check container-agnostically would have
  flagged every real `.mkv`/`.webm` file in the library as "broken."**
  ffprobe reports `codec_tag=0x0000` for every Matroska-family file
  unconditionally — normal, not a defect. `matroska`/`webm` are now
  explicitly exempt from both checks. Verified live against this
  library's real files: a real `.mkv` is still served directly with no
  remux triggered; video #5855 now gets correctly remuxed into a clean
  MP4 with proper `avc1`/`mp4a` tags.
- **A second safety check, found by inspecting the library's actual
  codec mix rather than by an incident:** the remux hardcodes
  `-tag:v avc1 -tag:a mp4a`, correct only for h264 video with aac (or
  no) audio. This library has a handful of av1/hevc/vp9 videos — forcing
  avc1 onto one of those would mislabel it, not fix it. `_ensure_playable`
  now checks `_safe_to_remux` before ever invoking ffmpeg, falling back
  to the original file (same as a failed remux) when it fails.

## 2026-06-26 — 0.14.1

- **Fix (real bug, found via user report): an Android mobile browser
  kept serving a stale `style.css` after a deploy.** `/static/style.css`
  is now referenced as `/static/style.css?v={{ app_version }}` in both
  templates, so the URL itself changes on every version bump instead of
  relying on cache-control headers — `review_detail()` now passes
  `app_version` into its template context too (it didn't before), so
  this covers `/review/{id}` as well as `/`.
- **Fix (real false positive, found via real review — video #4059): a
  fifth false-positive class, where several matches spread out in the
  preview all collapse onto the *same single moment in the candidate*.**
  A ~2.8s intro appeared three separate times in the preview (well
  spread out — clearing `--min-match-spread`), but the candidate only
  had that intro once, so all three matches pointed at one candidate
  timestamp; the candidate wasn't actually a match. `--min-match-spread`
  only ever checks the preview side, so it couldn't see this. Added
  `--min-candidate-match-spread` (default 2.0s, mirrors `--min-match-
  spread`): `score_pair()` now also computes `candidate_match_spread_sec`
  over the matched scenes' `candidate_ts` values, and both spread checks
  must pass — corroboration needs independence on both sides, not just
  one. The review UI's "Match details" summary now reports both spans
  and flags it explicitly when matches collapse onto one candidate
  moment despite a wide preview-side spread.

## 2026-06-25 — 0.14.0

- **Fix (real production incident, found via user report): staging a
  file and then running any later scan made it unrecoverable through
  the UI.** Approving a preview moves its file into the staging folder,
  which lives outside every scanned library root — the very next
  inventory run saw the original path gone and flagged it
  `missing_since` exactly like a real deletion, which hid the staged
  preview (and its undo button) from the review queue and exposed its
  `decisions` row to `/api/missing-files/prune`'s cascading delete — the
  *only* record of where to undo it back to. Reported live: a real
  27-file staging batch vanished from the queue after a routine full
  scan, with all 27 sitting in the missing-files list one confirm-click
  away from permanent, unrecoverable data loss. Fixed in three
  independent layers (see CLAUDE.md's Architecture for the full
  writeup): `01_inventory.py`'s `reconcile_missing()` now never flags a
  staged video missing and self-heals any row already wrongly flagged;
  `04_serve.py`'s missing-files listing/pruning independently exclude
  staged decisions too; and the review queue's staged previews are now
  sourced directly from `decisions`, never through `matches` or
  filtered by `missing_since` at all, closing a second, separate latent
  path to the same symptom (a re-scored `03_match.py` run dropping the
  pair's `matches` row below threshold).
- **Added Pending/Staged tabs and pagination to the review queue** —
  140+ rows on one page was unwieldy, and the fix above already needed
  a `decisions`-driven query for staged previews, which doubles as
  exactly what a dedicated Staged tab needs. `/` now takes
  `?tab=pending|staged&page=N`, 40 rows per page, with pagination
  controls above and below the list. "Back to queue" and the post-
  decide/undo redirects now return to whichever tab/page the human
  actually came from instead of always resetting to Pending page 1.

## 2026-06-25 — 0.13.3

- **Fix (real bug, found via user report immediately after 0.13.2
  shipped): the staging-bar's bottom-right floating fix from 0.13.2 was
  itself a usability regression** — it overlapped review-queue content,
  was hard to read against whatever was scrolled underneath it, and sat
  exactly where a misclick during review could land. Removed the
  floating/`position: fixed` behavior entirely; `.staging-bar` is now a
  second static bar directly below `.topbar`, full width, same as
  mobile already did (mobile's version of this bar was never floating
  to begin with, for the same underlying reason — no screen real estate
  to spare on a corner widget that overlaps content). Desktop and mobile
  now share the one `.staging-bar` rule; the mobile-only override block
  that used to exist for this is gone, since there's nothing left for it
  to do differently. Verified live with screenshots at both 1280px and
  375px width.

## 2026-06-25 — 0.13.2

- **Fix (real bug, found via user report): a staged preview's video
  player 404'd on the review page.** `/stream/{video_id}` always read
  `videos.path`, which deliberately keeps pointing at a preview's
  pre-staging location (display purposes only — see Architecture) — but
  once approved, the actual file has been renamed into the staging
  folder, so the original path no longer exists on disk. Added
  `_staged_file_path()`, which resolves a staged preview's real on-disk
  location (plain filename, or the `__{preview_id}` collision suffix —
  see `/api/decide`), and wired `/stream/{video_id}` to check it whenever
  the video has a `'staged'` decision. `/api/undo/{preview_id}` now
  shares the same helper instead of duplicating the lookup inline.
- **Fix (real bug, found by `_staged_file_path`'s own regression test
  while writing the above): two staged previews sharing the same bare
  filename could resolve to the wrong file.** The old undo logic (now
  shared via `_staged_file_path`) checked the plain filename *before*
  the `__{preview_id}` suffix — but the plain name only ever
  unambiguously belongs to whichever preview claimed it *first*; if a
  second, later-staged preview also has that filename, only it carries
  the disambiguating suffix. Checking plain-name first could therefore
  resolve the suffixed preview's lookup to its sibling's file instead.
  Fixed by checking the `__{preview_id}`-suffixed name first, falling
  back to the plain name only when no suffixed file exists for this
  specific preview — the suffix, when present, is always unambiguous.
- **Fix (real bug, found via user report): the staging-folder summary
  ("staging: N files, M MB" + "empty staging folder") was invisible on
  desktop at any file count, not just hidden-at-zero as intended** (a
  prior fix had correctly added hide-at-zero, but the box was unusable
  regardless of count). Root cause: `.staging-bar` is `position: fixed`
  with no `z-index`, while `.topbar` is `position: sticky; z-index: 10`
  — the staging-bar was genuinely rendered (confirmed via DOM dump: not
  `display:none`, correct text) but painted entirely behind the
  topbar's opaque background. Moving it from the top-right corner (which
  the topbar's own right-aligned stats already occupy) to bottom-right
  (the one corner nothing else in this app claims — `.action-bar`, the
  only other fixed/sticky bar, only exists on review.html) fixes the
  visibility bug without just trading it for a same-corner text overlap;
  a defensive `z-index: 11` was kept too. Verified live with real
  screenshots (1280px and 375px) against a throwaway test instance: the
  box now renders correctly, unobstructed, at both widths, both for zero
  and non-zero staged-file counts.
- Clarified `CLAUDE.md`'s "Testing on homeserver" section: this repo's
  working directory, when accessed by an agent, is a network mount of
  the real homeserver checkout — `.venv/`, `data/`, etc. being visible
  there does not mean they're executable from wherever the mount is
  being read from. All Python execution (tests, the app itself) must go
  through an actual `ssh homeserver` command; the live container's HTTP
  API can be queried directly over the network without SSH.

## 2026-06-23 — 0.13.1

- **Confirmed live against the real library: `--pool-generation-chunks`
  does not fix the memory-growth problem, and makes the peak worse.**
  `cgroup_anon` reached ~10.7GB (vs. ~8GB with no pool recreation at
  all), oscillating in the 9.4-10.7GB range for a sustained stretch
  before completing (runtime unaffected: 439s). No confident explanation
  yet for why teardown/recreation produces a *higher* peak. Left at
  default (disabled) — not recommended for use. 
- **Researched whether the underlying `max_tasks_per_child` CPython bug
  is version-specific** (could upgrading/downgrading Python avoid it?):
  no. Confirmed via the GitHub API that
  [cpython#115634](https://github.com/python/cpython/issues/115634) —
  describing the exact hang signature hit here — is still open with
  comments as recent as May 2026, and that the original
  [cpython#90622](https://github.com/python/cpython/issues/90622) fix
  PR was never merged. Spans every Python version with this feature
  (3.11 onward); no fix landed or imminent even for 3.15. 
- **Condensed README's Tuning section significantly** for `03_match.py`'s
  memory-growth investigation — was a step-by-step blow-by-blow of every
  attempted fix; now a short summary mentioning the CPython bug and its
  effect.
- **Added a "Benchmarks" section to README** with real numbers from one
  specific test machine (AMD Ryzen 7 7840HS / Radeon 780M / 32GB RAM):
  matching-stage timing and memory before/after vectorization, and a
  qualitative note that 4 fingerprint workers tested faster than 6 or 8.
  Fingerprinting CPU-vs-GPU decode timing is explicitly flagged as not
  yet benchmarked (only correctness has been validated) rather than
  guessed at.

## 2026-06-22 — 0.13.0

- **`_record_candidate` (0.12.5) confirmed live to also not be the
  dominant memory driver**: same ~2.7GB → ~5.5GB → ~8GB curve as every
  attempt before it, on the real ~5000-video library. The fix itself is
  still correct and kept (it removes genuinely unbounded growth and is a
  real efficiency win), it just wasn't the multi-GB story.
- **Found the actual mechanism, via a new `--debug-memory-objects`
  diagnostic** (logs each worker's `gc.get_objects()` count and peak
  RSS every 10 chunks): confirmed live that neither the main process's
  RSS nor any individual worker's RSS grows over a run — all 15 workers'
  full lifetimes traced, each plateauing within its first 1-3 chunks
  (~5MB total) and then sitting completely flat for the rest of its life
  (up to 816,149 further pairs processed with zero further RSS growth).
  `gc.get_objects()` per worker also stays flat — ruling out a
  Python-level reference leak entirely. Yet the cgroup-wide total climbs
  the whole run. The only mechanism that reconciles "every process's own
  memory size is flat" with "the aggregate keeps climbing" is
  copy-on-write divergence: pages that started out genuinely shared
  across the main process and all 15 workers gradually become
  exclusively-owned private copies, one worker at a time, as each one's
  `scenes_by_video.get(id)` calls touch (dirtying the page of) more of
  the ~4765 `VideoScenes` container objects over its lifetime. A touched
  page's size doesn't change for the worker now privately holding it —
  so no individual RSS reading moves — but the cgroup's distinct-page
  count goes up every time another worker's copy diverges from the
  others. This also retroactively explains why `malloc_trim()`/
  `gc.collect()` did nothing (no freed memory here to reclaim — live,
  referenced pages losing shared status isn't garbage) and why
  `--max-tasks-per-child` was conceptually the *right* idea (a fresh
  fork gets fresh sharing) just implemented via a buggy mechanism.
- **Added `--pool-generation-chunks`**: periodically does a full, clean
  `shutdown(wait=True)` of the entire `ProcessPoolExecutor` and
  constructs a brand new one, re-forking fresh workers from the main
  process and restoring full sharing. Unlike `--max-tasks-per-child`'s
  in-place worker replacement (forks a replacement while the pool's own
  management thread is alive — the unresolved CPython bug), this fully
  joins the old pool, including its management thread, before
  constructing a new one — going through `ProcessPoolExecutor`'s
  initial-launch code path instead of its dynamic-replacement one.
  Verified correctness live: forcing pool recreation every 2 chunks
  against a seeded DB still produced the right match counts, and the
  only row-level differences from the sequential path were exact
  `combined_score` ties at the eviction boundary — confirmed to be
  pre-existing, inherent to the parallel path's already-nondeterministic
  arrival order (the *same* kind of tie-breaking discrepancy, in fact
  more of it, occurs with the existing single-pool parallel path too,
  with or without this change).
- **Not yet validated**: whether this actually keeps memory bounded over
  a real full-length run on the real library — that's the next live
  test, same protocol as every memory-related change tonight.

## 2026-06-22 — 0.12.5

- **`--trim-worker-memory` (0.12.4) confirmed live to not fix the memory
  growth**: total runtime went *up* ~27% (7.5min → 9.5min) while memory
  growth and the final ~8GB ceiling were essentially unchanged from
  before — periodic small drops were visible (so the call does
  *something*), but the dominant driver clearly lives outside the
  workers. Now off by default (opt-in via `--trim-worker-memory`) rather
  than always-on, kept available rather than deleted in case it's worth
  re-testing once the real fix below is also in play.
- **Found and fixed the actual dominant driver**: `results_by_preview`
  in `03_match.py`'s `main()` accumulated *every* pair that passed the
  visual-score/matched-scenes/spread thresholds for the *entire run*, in
  the *main* process, only sorting and trimming to `--top-n` once at the
  very end. A worker-side fix could never have touched this. A rough
  size estimate (a `(candidate_id, res)` entry with ~5 matched scenes is
  ~2.5KB) makes the magnitude plausible: at 11.7M pairs, even a modest
  pass rate against `--min-visual-score`'s fairly loose 0.15 default
  means millions of accumulated entries, squarely in the observed
  multi-GB range.
- **Added `_record_candidate()`**: maintains a bounded (≤`--top-n` per
  preview) min-heap as results stream in, evicting the worst candidate
  immediately when a better one arrives, instead of holding every
  passing candidate until the end. Verified live with a seeded DB
  containing more passing candidates than `--top-n` for one preview (8
  candidates with distinct scores, `--top-n 5`): correctly kept only the
  5 highest-scoring ones, identically under `--workers 1` and
  `--workers 3`. Also unit-tested: bounded size, correct eviction,
  tie-breaking at the eviction boundary matches the old
  stable-sort-then-slice behavior (earlier arrival wins).
- **Not yet validated**: whether this actually keeps memory bounded over
  a real full-length run on the real ~5000-video library — the next
  real match run should confirm via `docker stats`/`free -m` watched
  throughout, same as every memory-related change to this stage so far.

## 2026-06-22 — 0.12.4

- **Added a safer alternative for the memory-growth problem
  `--max-tasks-per-child` was meant to fix** (0.12.2, reverted in
  0.12.3 after deadlocking a production run): `_trim_worker_memory()`,
  called at the end of every `_score_chunk()` in `03_match.py`. Calls
  glibc's `malloc_trim(0)` via `ctypes` (preceded by `gc.collect()` so
  CPython's own small-object allocator can release empty arenas back to
  glibc first), asking the allocator to hand freed memory back to the OS
  instead of retaining it for reuse. No process replacement, no fork, no
  new thread — runs entirely within the same already-alive worker
  process, so it carries none of `--max-tasks-per-child`'s deadlock risk
  (that risk was specifically about *forking a replacement* while
  `ProcessPoolExecutor`'s management thread is alive; this never forks
  at all). No-op on non-glibc platforms. Always on for `--workers > 1`,
  no new flag.
- Verified live that this doesn't change scoring results: `--workers 1`
  vs `--workers 3` (with `--progress-interval` near-zero, forcing the
  trim call on every single chunk rather than just occasionally) against
  identical seeded DBs still produced byte-identical `matches` rows.
- **Not yet validated**: whether this actually keeps memory bounded over
  a real full-length run on the real ~5000-video library — the next
  real match run should confirm via `docker stats`/`free -m` watched
  throughout, same as every memory-related change to this stage so far.

## 2026-06-22 — 0.12.3

- **Reverted 0.12.2's `--max-tasks-per-child` default to disabled (0)
  after it deadlocked a real production run.** Workers bled out to zero
  with no error in any log (docker logs, debug log) and the run hung
  forever; `docker top` showed 18 threads in the main process where ~2
  are expected. Root cause confirmed directly in this Python version's
  own `concurrent/futures/process.py` source: `_adjust_process_count`
  forks a replacement worker while `ProcessPoolExecutor`'s own
  background management thread (`_ExecutorManagerThread`) is still
  running — a known, **unresolved** CPython bug
  ([cpython#90622](https://github.com/python/cpython/issues/90622)),
  acknowledged in that exact function's own source comment ("there is
  still a potential deadlock bug"). Forking a multi-threaded parent is a
  classic hazard: a lock held by some thread other than the one calling
  fork() stays locked forever in the child, since that thread doesn't
  exist there to release it. This is a property of the default `fork`
  multiprocessing start method (which this script always uses — `spawn`
  would mean re-pickling and re-sending the entire preloaded scene
  dataset over IPC for every worker, including the initial ones,
  defeating the copy-on-write sharing this stage relies on), not
  something fixable by configuring `max_tasks_per_child` differently.
  `--max-tasks-per-child` now defaults to 0 (never recycle, the original
  behavior) and its help text/docstring warn against enabling it.
- The underlying memory-growth problem `--max-tasks-per-child` was
  trying to fix (glibc/numpy allocator retention, not a real leak — see
  0.12.2) is still real and still unaddressed. Next candidate: a
  periodic `malloc_trim()` call from *within* each long-lived worker (no
  process replacement, no fork, no thread-safety hazard) — not yet
  implemented.

## 2026-06-22 — 0.12.2

- **Confirmed live**: 0.12.0's vectorization fix sped up a real
  ~11.7M-pair/5000-video match run from ~16.5min to ~7.5min at 15
  workers, and fixed the shared-base-data-staying-shared half of the
  prior incident's likely cause (each worker's individual RSS was close
  to the pool's *total* `docker stats` figure — the shared preloaded
  scene data was genuinely counted once across the pool, not duplicated
  per worker).
- **Found a second, separate memory issue from the same live run**:
  total memory still climbed steadily throughout (~2.9GB shortly after
  starting → ~5.4GB at 50% progress → ~8GB near completion), dropping to
  baseline (~42MB) the instant workers exited. That drop-on-exit, plus
  no code path that accumulates state across pairs/chunks, points to
  glibc/numpy allocator retention from millions of small per-pair
  temporary-array/result-dict allocations, not a Python-level reference
  leak.
- **Added `--max-tasks-per-child`** (`03_match.py`, default 20,
  `--workers > 1` only): recycles a worker process after this many
  chunks, via `ProcessPoolExecutor`'s built-in support for exactly this
  pattern. A recycled worker re-forks fresh from the same long-lived
  main process — cheap, and it still gets the shared base data via
  copy-on-write — so only that worker's own accumulated private growth
  resets. `0` disables recycling. Verified live that recycling doesn't
  lose or duplicate work: forcing recycling after *every single chunk*
  (`--max-tasks-per-child 1`) against a seeded DB still produced
  byte-identical `matches` rows vs. the sequential (`--workers 1`) path.
  **Not yet validated**: whether 20 actually keeps memory bounded over a
  real full-length run — the next real match run should confirm via
  `docker stats`/`free -m` watched throughout.

## 2026-06-22 — 0.12.1

- **Added `--progress-interval`** (`03_match.py`, default 10.0 seconds,
  `--workers > 1` only): `_chunk_pairs`'s chunk count was fixed at
  `workers * 4` (floored at 40) regardless of pair count — fine for a
  small test run, but means a real full-library run's chunks grow in
  lockstep with pair count, so progress updates land only every few
  minutes once there are millions of pairs. Now additionally scales
  chunk count up so each chunk represents roughly `--progress-interval`
  seconds of one worker's time, via a rough throughput estimate
  (`PAIRS_PER_WORKER_SEC`, documented in `_chunk_pairs`'s docstring as
  derived from the one pre-vectorization benchmark on record — likely
  conservative now, expect updates to land sooner than requested rather
  than later). Never lowers chunk count below the existing
  `workers*4`/40 floor, so small runs are unaffected.

## 2026-06-22 — 0.12.0

- **Rewrote 03_match.py's scene comparison to be numpy-vectorized**
  (`VideoScenes`/`score_scenes`, replacing the old per-scene Python-dict
  list and nested-loop `best_scene_match`), as the most likely real fix
  for 0.11.2's incident — not just a speedup. Two distinct problems, one
  fix:
  - **Memory footprint**: a 64-bit hash as a Python int is 36 bytes
    (measured via `sys.getsizeof`); a bare per-scene dict shell is ~270
    bytes before any of its values. Packed into columnar numpy arrays
    (`uint64` hashes, `float64` timestamps, `uint8` color-histogram
    signatures), the same data is roughly an order of magnitude smaller.
  - **The actual likely cause of the crash**: `ProcessPoolExecutor`'s
    `initargs` hands every forked worker the same preloaded scene data.
    Linux `fork()` normally shares pages copy-on-write, but CPython's
    refcounting touches every object's refcount on *any* read, which
    dirties the page and forces a private copy — with a Python
    dict-of-dicts, every hash/timestamp lookup during scoring is exactly
    such a touch, so each worker's RSS creeps up gradually over a run as
    it touches more of the preloaded data. This fits 0.11.2's incident
    (crashed at ~77% progress, not at the start) far better than the
    chunking bug fixed in 0.11.2, which spikes once, upfront. A
    vectorized numpy op reads the raw buffer in C without creating a
    Python object per element, so it never touches a per-hash refcount —
    those pages stay genuinely shared across forked workers for the
    buffer's whole lifetime.
  - `score_scenes()` also computes the full (n_preview, n_candidate, 3, 3)
    Hamming-distance tensor for an entire pair in a handful of numpy
    calls instead of n_preview * n_candidate * 9 individual Python-level
    comparisons — a real speedup, but secondary to the above.
  - Tie-breaking (which candidate scene/variant combo wins when several
    are equally close) is bit-for-bit identical to the old "first
    strictly-lower distance wins" rule — verified against the old
    implementation across thousands of randomized trials with
    deliberately-induced ties before it was replaced, and `--workers 1`
    vs `--workers 3` against identical seeded DBs re-confirmed
    byte-identical output afterward (same values as before this change,
    not just internally consistent with each other).
  - Requires `numpy>=2.0.0` now (bumped from `>=1.26.0` in
    requirements.txt) for `np.bitwise_count`, the vectorized popcount.
  - **Not yet validated**: this against a real multi-minute match run on
    the actual ~5000-video library with RSS watched live over the whole
    run — the synthetic tests here confirm correctness and the
    fork-sharing *mechanism*, not that the production incident is fully
    resolved. Recommended next step before trusting a full `--workers`
    cores-1 run again: a smaller/`--limit`-bounded run with `free -m`/
    `docker stats` watched throughout.

## 2026-06-22 — 0.11.2

- **Incident**: running a match-only scan (5000 videos, ~11.7M pairs,
  `--workers` auto = 15) ran fine on all 15 cores for ~12 minutes, then
  workers started dying off, load average hit 76, and the server became
  unresponsive to SSH (still answered ping) with `kswapd0` pegged and
  swap fully used — required a hard reboot. Kernel logs showed an
  amdgpu VCN fault; other ffmpeg/GPU work was running concurrently, so
  that specific fault isn't confidently attributable to this stage
  (03_match.py touches no GPU/ffmpeg at all). What *is* attributable:
  - **Fixed real bug**: 0.11.1's cost-aware chunking (`_chunk_pairs`)
    sorted a `(cost, preview_id, candidate_id)` copy of every pair
    before bin-packing — at ~11.7M pairs, ~2.5-3GB of purely transient
    allocation that didn't exist before 0.11.1, on a system already
    reporting ~0 free RAM and fully-used swap. Rewritten to bin-pack in
    arrival order instead (no sort, no per-pair allocation beyond what
    plain equal-count slicing already needed) — see `_chunk_pairs`'s
    docstring for the full reasoning on why dropping the sort doesn't
    meaningfully weaken the load-balancing fix.
  - **However**: the crash happened at ~77% progress (9M/11.7M pairs),
    not at the start — the chunking spike happens once, upfront, before
    any pairs are scored, so it doesn't fully explain a failure that far
    in. The more likely primary driver is pre-existing and not
    introduced by 0.11.1: `ProcessPoolExecutor`'s `initargs` hands all
    15 forked workers the same preloaded `scenes_by_video`/`audio_by_video`
    dicts, and CPython's refcounting touches every object's refcount on
    any read, defeating `fork()`'s copy-on-write sharing — so each
    worker's RSS can creep up over the run as it touches more of the
    preloaded data, not just at startup. This wasn't exercised at
    today's ~5000-video scale before (last validated around 2000
    videos). Documented in README's `--workers` tuning entry: don't
    assume `cpu_count - 1` is RAM-safe at large library sizes without
    watching actual memory over a real run.
  - Progress display wording fixed: the match stage's "estimating…"
    message said "no files finished yet this stage" — it's pairs, not
    files, during matching. Now stage-aware (`STAGE_UNITS`).

## 2026-06-22 — 0.11.1

- **Fix (found live on homeserver, 16 cores, `--workers` auto)**: the
  match stage's running worker count visibly drained over the course of
  a real run, well before the run finished — most chunks completed
  quickly while a couple of workers kept grinding alone, leaving the
  rest of the cores idle. `ProcessPoolExecutor` already reassigns an
  idle worker to the next queued chunk dynamically, but that only helps
  if every chunk costs about the same; `_chunk_pairs`'s old equal-*count*
  slicing didn't guarantee that, since `score_pair()`'s real cost is
  `O(preview_scenes * candidate_scenes)`, not uniform per pair, and
  `pairs` is built by a preview-outer loop — every pair sharing one
  preview is contiguous, so a single high-scene-count preview produced a
  run of expensive pairs that equal-count slicing could drop into just
  one or two chunks (stragglers). Fixed with greedy LPT bin-packing
  (`_chunk_pairs`'s new optional `cost_fn` parameter, wired to a
  scene-count-product proxy in `main()`): pairs are sorted by estimated
  cost and dealt into whichever chunk currently has the lowest running
  total, balancing each chunk's *estimated total cost* up front rather
  than just its pair count. Composes with (doesn't replace) the existing
  dynamic reassignment. Verified live: `--workers 1` and `--workers 3`
  against identical seeded DBs still produce byte-for-byte identical
  `matches` rows; a synthetic skewed-cost test confirms the new chunking
  beats plain equal-count slicing on worst-chunk cost.

## 2026-06-22 — 0.11.0

- **New false-positive class, found via real review (video #2237)**: a
  preview and an unrelated candidate both opened with the same shared
  intro/logo animation. Scene detection chopped it into three quick cuts
  within ~2 seconds, all of which matched (hash distance 0, 0, 2) and
  cleared `--min-matched-scenes` (3/3) — but all three were the same
  ~4-second sting, not three independent corroborating moments.
  `--min-matched-scenes` guards against one coincidental hit; it did
  nothing against several hits that are really the same hit.
- **Added `--min-scene-duration` (`03_match.py`, default `2.0`s)**: drops
  any scene whose gap to the next scene-cut in its own video is shorter
  than this, before scoring — applied to both sides of every pair via
  `load_all_scenes()`, since a video can be someone else's candidate. A
  rapid-cut logo sting isn't an independently identifiable scene.
- **Added `--min-match-spread` (`03_match.py`, default `2.0`s)**: skips
  storing a match whose matched scenes' *preview* timestamps span less
  than this many seconds — guards against scenes that individually pass
  the duration floor but still all land within the same narrow moment
  (e.g. a longer single shared title card). Both new thresholds are
  starting points, not calibrated values.
- **Match details table now shows each matched scene's duration** (gap
  to its own next scene-cut, on both the preview and candidate side, "—"
  when unknown/last-scene) — this is exactly the information that had to
  be inferred by hand to diagnose video #2237's false positive.
- **Scoring progress display now uses thousand-separators** for
  candidate-pair counts (console output, the scan panel's live "done /
  total" counter, and `scan_runs.message`) — the match stage's pair
  count is routinely in the hundreds of thousands to millions and was
  unreadable as a bare digit string.

## 2026-06-21 — 0.10.6

- **Fix (real bug in production, found via user review of the 0.10.5
  sample compose file)**: staging a file (the "approve for deletion"
  rename) had been silently doing a slow copy-then-delete instead of
  the instant rename the whole design assumes, since the very first
  deployment. Both the real `docker-preview-matcher.yml` and the sample
  `docker-compose.yml` mounted each library subdirectory (and
  `_to_delete`) as a *separate* `-v` line — even though all of them are
  subdirectories of one NFS export on the host, Docker treats each
  separate bind-mount directive as its own mount point for rename-
  atomicity purposes. Confirmed live in the running production
  container: `os.rename()` between two of these separately-mounted
  paths raised `OSError: [Errno 18] Invalid cross-device link`;
  `shutil.move()` (used by `/api/decide/{preview_id}`) silently catches
  exactly this and falls back to copy+delete, so the failure was never
  visible as an error, just as a slower-than-claimed approval using
  temporary extra disk space proportional to file size. Fixed by
  mounting the library's root directory **once**, with `LIBRARY_PATHS`
  and `STAGE_DIR` both reached as subdirectories through that single
  mount, instead of one `-v` line per subdirectory. Verified live,
  twice: the failing case (separate mounts, `errno 18`) and the fix
  (single parent mount, ~5-6ms rename, no error) on the same real NFS
  export. Redeployed to production and resumed the in-progress
  fingerprint job with no data loss.

## 2026-06-21 — 0.10.5

- **Prepared for git/GitHub**: removed deployment-identifying details
  (hostname, IP, specific library-tool naming, GPU model) from every
  comment and doc that gets committed.
  `docker-compose.yml` is now a generic, commented sample meant to be
  copied and filled in; `rebuild.sh` uses `docker-preview-matcher.yml`
  if present, falling back to the sample otherwise. The Dockerfile no
  longer bakes in an absolute `--db`/`--stage-dir` path — `04_serve.py`
  and the three pipeline scripts now default `--db` to a `DB_PATH` env
  var (and `04_serve.py`'s `--stage-dir` to `STAGE_DIR`), which the
  Dockerfile sets to `/data/library.db`/`/data/_to_delete`; an explicit
  flag still overrides either, and plain CLI usage outside Docker is
  unaffected (env var unset → same `data/library.db` default as before).
  Verified live: built the updated Dockerfile fresh, ran it against a
  throwaway bind mount with no `--db`/`--stage-dir` flags at all, and
  confirmed it created the DB at the expected env-var-derived path with
  no manual configuration.
- **Fix (real bug, found via user report)**: the "empty staging folder"
  button and summary showed unconditionally, even with zero staged
  files — barely noticeable in its small fixed-corner desktop placement,
  but impossible to miss in the full-width bar mobile reflows it into.
  `refreshStaging()` updated the summary *text* every poll but never
  toggled visibility at all, unlike the equivalent (and already-correct)
  missing-files panel right next to it. Fixed to hide both the summary
  and the button when `file_count === 0`, matching the missing-files
  panel's existing pattern exactly. Verified visually (screenshot at
  both 1280px and 375px width) against a real, separate copy of the
  library DB with zero staged files — confirmed hidden on both.

## 2026-06-21 — 0.10.4

- **Fix (real bug, found via user report)**: on mobile, the review
  queue's preview/candidate filenames didn't wrap and weren't length-
  limited, overflowing past the screen edge and breaking the layout
  (a horizontal scrollbar appeared). Root cause: the mobile media query
  switches `.row` from a grid (desktop, where the `1fr` track constrains
  `.filename`'s width, letting its existing single-line nowrap+ellipsis
  rule work) to a flex column with `align-items: flex-start` — which
  sizes children to their own content width by default, so the
  single-line ellipsis rule never got a width to truncate against in
  the first place. Fixed with a mobile-only override: `width: 100%` to
  reclaim the row's actual available width, plus a 3-line `line-clamp`
  (with `overflow-wrap: break-word` as a safety net for any filename
  without spaces) so long filenames wrap and truncate with an ellipsis
  the same way the desktop single-line version does, just over up to 3
  lines instead of 1 — a stacked mobile card has more vertical room
  than a single grid row, so a single truncated line would waste most
  of it. Verified visually (screenshot at 375px width against the real
  queue data) before and after: before, a long filename ran off the
  screen edge with a horizontal scrollbar; after, it wraps cleanly with
  no overflow, and a genuinely very long one (e.g. "KarinaLil - Cute
  Petite Asian with Incredibly Tight Wet Pussy (2026-04-…") correctly
  clamps to 3 lines with a trailing ellipsis.

## 2026-06-21 — 0.10.3

- **Fix (real bug, found via user report)**: right after resuming a
  paused/interrupted/failed/cancelled scan, the progress panel showed
  "estimating… (no files finished yet this stage)" until this
  invocation's own first completion — even with hours of measured
  throughput already on record from before the pause. `eta_sec`'s rate
  calculation only ever divided this invocation's own raw `stage_done`
  by its own raw elapsed time since `stage_started_at`, both of which
  are genuinely 0 immediately after a resume, regardless of how much
  history exists. Fixed with a second baseline column,
  `resume_baseline_elapsed` (real seconds actually spent *processing* —
  not paused — in prior attempts at the same stage, accumulated across
  resume hops the same way as the existing `resume_baseline_done`):
  `scan_status()` now computes rate as cumulative done over cumulative
  *active* elapsed, so immediately after a resume the rate is
  essentially the prior attempt's own measured throughput, and blends
  toward this invocation's fresh numbers as they accumulate. Verified
  live with rapid-fire polling immediately after a real resume: at the
  exact instant the stage starts (the status row's `updated_at` and
  `stage_started_at` differing by 4 microseconds — zero completions
  from the new invocation), `eta_sec` was already populated from the
  carried-forward baseline alone.

## 2026-06-21 — 0.10.2

- **Fix (real bugs, found via user report against an actual --limit 1000
  fingerprint run that got interrupted by the 0.10.1 deploy and
  resumed)**: resuming a paused/interrupted/failed/cancelled scan had
  three real problems, all in `04_serve.py`'s `/api/scan/resume`:
  1. **Worker count silently reverted to the scan's original starting
     value.** `/api/scan/workers` only ever updates the *live* `scan_runs`
     row's `target_workers` — it never touches `params_json`, which is
     fixed at the moment the scan was first started. Resuming just
     replayed that stale original value, discarding a live adjustment
     made mid-run (8 workers silently became the original 4 again).
  2. **`--limit` overshot the user's actual intended total.** `--limit`
     applies to whatever's currently outstanding, which after a partial
     run already excludes the completed items — replaying the original
     `--limit` unmodified started a *fresh* batch of that size on top of
     what was already done (134 done + a new 1000 = 1134, not the 1000
     actually asked for).
  3. **Progress, elapsed time, and ETA all reset to 0/from-scratch**
     instead of continuing from where the interrupted attempt left off.
  Fixed via a new pure `_resume_plan()` function (carries forward the
  row's current `target_workers`, reduces `--limit` by however much was
  already completed across every resume hop so far, and carries forward
  `started_at` plus a `resume_baseline_done` count that `scan_status()`
  folds back into the displayed progress) — see its docstring in
  `04_serve.py` for the full writeup of each bug. A new
  `scan_runs.resume_baseline_done` column stores the carried-forward
  count; reset to 0 on any stage transition that isn't itself a resume,
  so it doesn't leak into a later, unrelated stage's progress display.
  **A second bug was found and fixed while writing the regression
  test**: the first implementation reduced `--limit` using only the
  immediately-prior row's own `stage_done`, not the full cumulative
  total across however many times the scan had already been resumed —
  undercounting (and therefore still overshooting `--limit`) on a
  *second* resume of an already-once-resumed run. Verified live end-to-
  end against a real (synthetic-content, real ffmpeg) fingerprint run:
  started with `--limit 200`/2 workers, live-adjusted to 4 workers,
  paused at 12/200, resumed (confirmed 4 workers and continuous
  12/200 + elapsed time, not a reset to 0/4-workers-reverted), paused
  again at 72/200, resumed a second time (the specific scenario the
  above sub-bug affected), and let it run to completion — confirmed
  exactly 200 videos fingerprinted in the end, not 212 or more.

## 2026-06-21 — 0.10.1

- **Fix (real bugs, found via user report on the live deployment)**: the
  scan panel's live fingerprint-worker control, shipped in 0.9.0, had
  three problems:
  1. The progress panel could appear frozen until a manual page reload.
     Root cause: browsers throttle `setInterval` timers in backgrounded/
     hidden tabs (sometimes to once a minute or less) — a scan started
     and then left in a background tab looks stuck even though the
     timer is technically still scheduled. A controlled, actively-focused
     headless-browser repro (real ffmpeg, a real ~30s fingerprint run)
     showed the polling loop itself updates correctly every cycle with no
     JS errors, narrowing this specifically to the backgrounded-tab case.
     Fixed by forcing an immediate poll on `visibilitychange`/`focus`,
     so the moment the tab regains attention it refreshes instantly
     instead of waiting for a throttled timer to catch up.
  2. Typing a new worker count into the live field could get silently
     reverted by the next poll. The original guard only checked
     `document.activeElement` — which doesn't cover every way the value
     can change (confirmed: the native number-input spin arrows don't
     necessarily move focus into the field, so an edit made that way had
     no protection at all) and stops protecting the instant focus moves
     away, e.g. the moment you click "Apply" itself, racing the poll
     against your own submission. Fixed by tracking *dirty* state via
     the input's own `input` event instead of focus — covers typing,
     spin arrows, and the click-Apply race uniformly, and (unlike a
     "never touch it again" fix would) still lets the field update from
     polling once clean, which is also what makes a worker-count change
     made on a *different* device/tab show up here.
  3. The "applied — takes effect within ~2s" hint stayed forever once
     shown. Fixed with a debounced auto-clear timeout.
  All three fixed behaviors verified live against a real isolated
  instance (real ffmpeg fingerprinting a real, if synthetic, video set)
  driven via actual Chrome DevTools Protocol automation — not just code
  review: dirty-tracked input survives multiple real poll cycles
  unclobbered, the hint clears within its promised window, and a
  worker-count change posted from a separate "device" (a second HTTP
  client) correctly propagates to the clean tab's display.

## 2026-06-21 — 0.10.0

- **New: missing-file detection.** `01_inventory.py` now compares every
  scan's filesystem walk against the DB and marks any previously-known
  path no longer found as missing (`videos.missing_since`), clearing the
  flag the moment a path reappears. Deliberately NOT a delete — this
  library lives on an NFS mount, and a transient mount hiccup mid-scan
  would otherwise make thousands of real files look "deleted" for a few
  seconds, which would cascade away real scenes/matches/decisions on a
  false alarm. **Scoped to the roots actually passed each run**: a
  partial scan (one root selected in the web UI) never flags files under
  an *un*scanned root as missing, verified live (deleted a file under an
  unscanned root, confirmed untouched; deleted one under the scanned
  root, confirmed flagged; re-scanned, confirmed it cleared on
  reappearance).
- **New: move/rename detection (size + duration heuristic).** When a
  genuinely new path is probed and its exact `(size_bytes, duration_sec)`
  matches *one* currently-missing row, that row's path is remapped
  instead of inserting a new one — preserving its fingerprints, scenes,
  matches, and past decisions, and skipping a redundant re-fingerprint.
  Ambiguous matches (more than one missing row with the same size+
  duration) deliberately fall back to treating the file as brand new
  rather than guessing. Verified live: moved a fingerprinted+scened
  video to a new path under a different root, confirmed the same
  `video_id` followed it with `fingerprinted_at` and its scene rows
  intact, no orphan row left behind at the old path.
- **New**: the review queue (`/`) now hides any preview or candidate
  flagged missing — a preview with a missing #1 candidate but a viable
  second-best one still surfaces with that one, rather than disappearing
  just because its top match happened to be the file that vanished. A
  preview whose candidates are *all* missing is correctly absent.
  Nothing is deleted by this; it only affects what's shown.
- **New**: `/api/missing-files` (list) and `/api/missing-files/prune`
  (confirm-gated permanent removal, same `{"confirm": "DELETE"}` pattern
  as `/api/purge-staging`) plus a small queue-page panel to view and
  prune them. Pruning never touches a file on disk — these rows already
  have no file backing them; it only clears their now-meaningless DB
  history via the existing `ON DELETE CASCADE` foreign keys.
- The review detail page now shows a banner when the preview or the
  currently-selected candidate is flagged missing, so a stale link
  (bookmarked, or reached before this preview's match data caught up to
  a missing-file scan) is self-explanatory instead of just failing to
  play with no context.
- **Fix (real bug, found while auditing the above)**: re-fingerprinting
  a file that was replaced/re-encoded at the same path (`01_inventory.py`
  already correctly resets `fingerprinted_at` for this) could leave
  *stale scene rows* behind if the new encode produced fewer scenes than
  the old one — `02_fingerprint.py`'s scene write was `INSERT OR REPLACE`
  keyed on `(video_id, scene_index)`, which only overwrites matching
  indices and never removes extras. A shrinking re-encode would silently
  mix old and new content's hashes for that video in all future matching.
  Fixed by deleting a video's existing scene rows before writing fresh
  ones; covered by a new regression test that fingerprints a video twice
  with a decreasing scene count and asserts no stale rows survive.
- `02_fingerprint.py`'s per-result DB write (scenes + audio_fp +
  `fingerprinted_at`) is now a standalone `write_fingerprint_result()`
  function instead of inline code in the scheduling loop, specifically so
  the bug fix above is unit-testable without a real `ProcessPoolExecutor`
  or real ffmpeg.

## 2026-06-21 — 0.9.0

- **Fix (real performance bug, found live against the actual 2000-video
  library)**: `03_match.py` was taking ~45 minutes to score 1,851,501
  candidate pairs. The cause was NOT missing indices and NOT SQLite
  page-cache misses (`scenes` already had `idx_scenes_video`, and the
  whole DB is ~30MB — trivially OS-cached) — it was query *count*:
  `score_pair()` issued two fresh `SELECT ... WHERE video_id = ?` calls
  per pair, every pair, against a library where each of ~2000 videos
  appears in *thousands* of pairs — millions of redundant round-trips
  re-fetching identical rows. Fixed by `load_all_scenes()`/
  `load_all_audio()`: one full-table `SELECT` each, up front, into
  in-memory dicts keyed by video_id; every pair's scoring after that is
  pure in-memory comparison with zero DB access. Hashes are also parsed
  hex→int once at load time instead of on every Hamming-distance call,
  and the distance computation itself now uses `int.bit_count()` instead
  of `bin(x).count("1")`.
- **New**: `03_match.py --workers N` parallelizes scoring across
  processes (default: cpu count − 1). With the preload fix above, scoring
  is pure CPU work with no shared DB/file/GPU contention, so this should
  mostly just help — unlike `02_fingerprint.py`'s worker tuning, which is
  genuinely constrained by decode/GPU throughput. Workers receive the
  preloaded scene/audio data once via a pool initializer (not re-sent per
  task) and score *chunks* of pairs, not one task per pair, to keep
  IPC/scheduling overhead from drowning out the actual work at 1.85M
  pairs. All DB writes still happen only in the main process. Verified
  live: sequential (`--workers 1`) and parallel (`--workers 4`) runs
  against an identical seeded DB produced byte-for-byte identical
  `matches` rows. Real-library before/after timing and the actual
  sweet-spot worker count (cores−1 vs. cores/2 vs. 3/4 vs. 2/3) are not
  yet benchmarked — see README's Tuning section.
- **New**: `02_fingerprint.py`'s `--workers` count can now be raised or
  lowered *while a scan is running*, from the web UI's scan panel
  (min 2, max = CPU core count, default unchanged at 4). The scheduling
  loop polls a new `scan_runs.target_workers` column roughly every 2s and
  adjusts how many fingerprinting tasks it keeps in flight — critically,
  **an already-running worker is never killed** to shrink concurrency;
  lowering the target just stops backfilling new work until in-flight
  count drains down naturally, and raising it submits more immediately.
  New endpoint `POST /api/scan/workers {"workers": N}`. Verified live with
  a controlled-timing probe (real `ProcessPoolExecutor`, sleep-based
  tasks): concurrency stayed at the starting value, jumped up within one
  poll cycle of a live increase, and drained down (not killed) after a
  live decrease, with every task still running to completion.
- Both `02_fingerprint.py` and `03_match.py`'s scan-panel "Start scan"
  controls gained optional worker-count inputs (fingerprint workers,
  match workers), threaded through `_build_cmd`.
- `scan_runs` gained a `target_workers` column via an explicit
  `ALTER TABLE` migration in `init_db()` (the table already existed in
  deployed DBs, so `CREATE TABLE IF NOT EXISTS` alone wouldn't add it) —
  idempotent, safe to call on every process start.

## 2026-06-21 — 0.8.0

- **Fix (critical, real production deadlock)**: `procutil.run_with_hard_timeout`
  polled `Popen.wait()` but never read `stdout`/`stderr` until *after* the
  child exited. ffmpeg with `-loglevel info` + the `showinfo` filter (one
  line of stats per frame that passes) can write past the OS pipe buffer
  (~64KB on Linux) before exiting on a video with many scenes — once full,
  the child blocks on `write()` forever, since nothing was draining the
  pipe to make room. Confirmed live: four "stuck" ffmpeg processes were
  all in synchronous-IO sleep (`pipe_write`/`futex_wait_queue`), GPU at
  ~1%, CPU-time totals frozen minutes apart. This likely explains some of
  the "needs a longer timeout for long videos" cases logged just before
  this fix too — scaling the timeout up just meant waiting longer to hit
  the same deadlock. Fixed by draining both pipes continuously in
  background threads for the child's whole lifetime, not just at the end.
  Added a regression test that pushes 200KB through the pipe and asserts
  it returns in under 5 seconds, not 15.
- Scene-extraction timeout now scales with video duration (floor 300s,
  1× duration, capped at 1 hour) instead of a flat 300s — two real
  ~45-minute 720p files genuinely needed more than 5 minutes under
  software decode.
- **Fix**: cancelling/pausing a scan only sent `SIGTERM`/`SIGKILL` to the
  orchestrated subprocess's own process group — but `run_with_hard_timeout`
  starts each ffmpeg/ffprobe/fpcalc call with `start_new_session=True`
  (needed so *its own* timeout-triggered kill targets just that one call,
  not sibling workers), which puts it in a *different* group the
  orchestrator's `killpg` can't reach. Confirmed live: cancelling left 4
  ffmpeg processes running, still holding the GPU, until killed by PID by
  hand. Fixed with `_all_descendant_pids()`, which walks `/proc` instead
  of relying on process-group membership, capturing the full tree
  *before* sending any signal (since killing the parent first reparents
  its children, after which they're no longer found as its descendants).
- **Fix**: resuming a scan that was interrupted mid-fingerprint (or
  mid-match) always restarted from the inventory stage. Since inventory
  re-walks the whole library and can discover a fresh batch of
  new-to-probe files, those crowded out the *specific* files the
  interrupted fingerprint stage still had left once `02_fingerprint.py`'s
  own `--limit` was re-applied to the now-larger backlog — reported live
  as "resume found 50 new files instead of finishing the previous 38" (it
  wasn't redoing finished work, it just stopped prioritizing the right
  remaining work). Resume now starts from the stage that was actually
  interrupted, skipping ones already finished.
- Added per-stage control to the scan panel: run the full pipeline, or
  just inventory, just fingerprinting, or just matching. Root
  selection/GPU checkbox/limit show or hide depending on which stages
  are relevant to the selected mode.
- Topbar now also shows a fingerprinted-video count alongside the total.

## 2026-06-21 — 0.6.0

- **Fix (real false positives, root-caused live)**: three matches stored
  against the real library all turned out to be wrong. Investigation
  found all three were a *single* isolated scene match each, sitting at
  exactly `--hash-threshold`'s boundary distance, on previews with only
  2-6 total scenes — meaning one coincidental hit on a near-featureless
  frame (skin tone, blur) was enough to clear a deceptively high
  *fraction* (1/2 = 50%) despite being one data point with zero
  corroboration. A real preview splices together several moments from
  its source; matching now requires both a minimum visual-score
  *fraction* (`--min-visual-score`, unchanged) **and** a minimum
  *absolute* matched-scene count (`--min-matched-scenes`, new, default
  3) before storing a match — so a tiny preview can no longer pass on
  fraction alone. Also tightened `--hash-threshold`'s default 12 → 8,
  since all three observed false positives sat exactly at the old
  boundary. Re-ran matching against the real library after the fix: all
  three false positives are gone (0 matches stored, vs. the in-flight
  state before the fix).
- Review page's "Match details" table now states whether matches spread
  across multiple distinct moments in the preview or are a single
  isolated point — visualizing exactly the distinction the fix above is
  based on, so a borderline case is easy to judge by eye even before
  checking the numbers.

## 2026-06-21 — 0.5.0

- **Fix (real bug, reported live)**: `--limit N` on `01_inventory.py`
  capped the raw candidate-file list *before* filtering out files already
  up to date. Since the filesystem walk order is stable, repeated runs
  with the same `--limit` kept re-selecting the same already-probed files
  at the front of that order and never made progress through the rest of
  the library — confirmed with the exact reported scenario (53
  already-probed files, `--limit 50`) both failing before the fix and
  correctly advancing through three consecutive runs after it.
- **Fix (real bug, root-caused live)**: the inventory/fingerprint stages'
  progress display could appear to freeze on one file for minutes,
  looking exactly like a hang. It wasn't one — direct manual ffprobe on
  the "stuck" file, run while the scan was still showing it, returned in
  well under a second. The actual cause: the main loop's db connection
  held an uncommitted write transaction open across many iterations
  (only committing periodically), which blocked `update_scan_run()`'s own
  short-lived connections — including the ones fingerprint worker
  *processes* use to report "processing: X" — past their busy_timeout,
  freezing the *displayed* message while the real work kept going
  underneath the whole time. Fixed by committing after every item
  instead of periodically, in both `01_inventory.py` and
  `02_fingerprint.py`. `update_scan_run()` also now logs (instead of
  silently swallowing) any write failure, so this class of bug doesn't
  take as long to diagnose again.
- Added `procutil.py`: a `subprocess.run(..., timeout=N)`-alike that's
  guaranteed to return within `N` seconds even if the child ends up stuck
  in an uninterruptible (D-state) sleep — which a process reading a
  slow/unresponsive NFS share can enter even after being sent SIGKILL,
  a state plain `subprocess.run`'s own timeout-cleanup path can block on
  indefinitely. Real, defensible Python behavior worth guarding against
  on this NFS-backed library (confirmed `hard`-mounted, no fixed retry
  bound) — but is *not* what caused the hang above; see its docstring.
  Applied to every ffmpeg/ffprobe/fpcalc subprocess call across
  `01_inventory.py`, `02_fingerprint.py`, and `04_serve.py`.
- Added optional debug logging (`--debug-log <path>`, always on for
  web-UI-triggered scans, written to `data/subprocess.log`): one entry
  per ffmpeg/ffprobe/fpcalc call with the target filename, duration,
  exit status, and (truncated) output. Already paid for itself once:
  using it surfaced that `_has_broken_codec_tag()` was re-running ffprobe
  on *every* HTTP range request for the same file during normal video
  playback (a `<video>` tag makes many small range requests while
  loading/seeking) — fixed with a small in-memory cache keyed by file
  mtime.
- Fixed a real race in `02_fingerprint.py`'s progress reporting: a
  worker's "processing: X" message could get overwritten by the main
  process's "N/M processed" message moments later, leaving the UI
  showing no filename for however long the *next* file took — reported
  live as "stuck at 49/50, no filename, ETA ~6s, not progressing" (the
  scan had in fact completed normally; only the display was misleading).
  The numeric "N/M processed" message was redundant with `stage_done`
  anyway (already rendered as "N / total"), so it's simply not sent
  anymore — only the filename message remains.
- Scan progress panel: filename now sits on its own line below the
  runtime/ETA stats instead of before them, so a long/short filename no
  longer shifts those numbers around on every update. Added estimated
  total stage duration and estimated wall-clock end time alongside the
  existing elapsed/ETA-remaining figures.
- Removed redundant explanatory text from the scan panel's GPU and limit
  option hints, per request.

## 2026-06-21 — 0.4.0

- **Fix**: neither template had a `viewport` meta tag, so mobile browsers
  assumed a desktop-width layout and just zoomed the whole page out
  instead of reflowing it — no amount of responsive CSS could have taken
  effect without this. Added to both `index.html` and `review.html`,
  plus a `@media (max-width: 720px)` pass: the queue rows, scan panel,
  topbar/stats, and the review page's side-by-side video comparison and
  match-details table all now stack/reflow sensibly on a phone instead of
  overflowing. Verified with real screenshots (headless Chromium) at
  375px and 1400px widths, not just by reading the CSS.
- Scan progress now shows an estimated total stage duration and an
  estimated wall-clock end time (e.g. "ends ~14:32"), alongside the
  existing elapsed-time and ETA-remaining figures.
- `02_fingerprint.py` now reports "processing: <filename>" the moment a
  worker picks up a video, not just when one finishes — investigated a
  report of the fingerprint stage appearing frozen (counts not moving for
  a while); root cause is structural (each video is a full decode pass,
  so with the default worker count, multi-minute gaps between
  *completions* are normal for long files), but the new message at least
  shows something is actively happening between completions. The
  progress UI also shows "estimating…" instead of a blank ETA before the
  first file of a stage finishes, rather than just silently showing
  nothing while elapsed time ticks up.
- Confirmed, while investigating the above: a scan transitioning from
  "Inventory" (`stage_total` = files found, including ones already
  known) to "Fingerprinting" (`stage_total` = only videos actually
  needing it) with a different, smaller total is expected/correct
  behavior, not a bug — the two stages count different things.

## 2026-06-21 — 0.3.0

- **Fix (scoring accuracy)**: found and fixed a real false-positive
  source while investigating a user-reported bad match. A completely
  uniform/blank frame's DCT is mathematically zero everywhere, so the
  median-threshold step produces the *same* degenerate all-zero pHash
  for every blank frame in the library, regardless of source video —
  confirmed live: two unrelated movies' blank intro/fade-to-black frames
  "matched" with Hamming distance 0. `02_fingerprint.py` now skips
  storing near-uniform frames as scenes at all (`--blank-std-threshold`,
  default 4.0; see `phash.is_blank()`). Applies to both the vendored and
  real `imagehash` backends — this isn't a vendored-fallback bug, it's
  inherent to DCT-based pHash on content-free frames. Existing degenerate
  scenes purged from the real DB and re-fingerprinted; the reported false
  match is confirmed gone.
- **Fix (playback)**: some real library files (likely re-muxed from
  scraped sources) have a valid h264/aac bitstream but a missing/zero
  codec FourCC tag in their MP4 sample description — ffmpeg/VLC tolerate
  this, but browsers reject it outright ("no supported source was
  found"). `/stream/{video_id}` now detects this and transparently
  serves a cached, re-tagged `-c copy` remux instead (lossless, no
  re-encode, built once per affected file under `data/remux_cache/`).
  Original files are never modified. The review UI also now shows a
  clear inline message instead of a silent player failure if playback
  fails for any other reason.
- **Fix**: the scan orchestrator never actually passed `--hwaccel` to
  `02_fingerprint.py` — VAAPI decode was built and validated last
  release, but every web-UI-triggered scan silently used the CPU anyway.
  Added a "GPU decode (VAAPI)" checkbox to the scan panel, default on;
  verified live via `/sys/.../amdgpu_pm_info` GPU-load readout while a
  triggered scan ran.
- Queue page (stats, match list) now auto-refreshes when a scan you're
  watching finishes, instead of needing a manual page reload.
- Review page now shows the candidate's full path (previously only the
  preview's path was shown) and a "Match details" table — per-scene hash
  distance, timestamps, and crop/flip variant for every matched scene —
  so a match can be judged even when (or especially when) playback isn't
  available.
- Header now shows the running app version next to "Preview Matcher".

## 2026-06-20 — 0.2.0

- **Fix**: `04_serve.py` never actually mounted `/static` — `StaticFiles`
  was imported but `app.mount("/static", ...)` was missing, so
  `style.css` 404'd and every page rendered as unstyled HTML. One-line
  fix (`app.mount(...)` added right after `app = FastAPI(...)`).
- Add web UI scan orchestration — the app no longer requires SSH/manual
  commands for day-to-day use:
  - New "Library Scan" panel on the queue page: pick any subset of the
    configured library roots ("partial" vs "full" scan), an optional file
    limit for quick tests, and click "Start scan" — `04_serve.py` runs
    `01_inventory.py` → `02_fingerprint.py` → `03_match.py` as subprocesses
    in sequence, no terminal needed.
  - Live progress: current stage, file/pair count (done/total), a
    progress bar, elapsed runtime, and an ETA — polled from a new
    `scan_runs` table that the stage scripts update directly via a new
    `--run-id` flag (no stdout-scraping).
  - Pause and cancel (same mechanism, different status) terminate the
    active stage's whole process group (`os.killpg`, escalating to
    SIGKILL after 5s) so `02`'s worker processes and the ffmpeg/fpcalc
    subprocesses they spawn don't get orphaned.
  - Resume replays the last scan's exact parameters. Since `01`/`02` are
    already idempotent (skip unchanged files / already-fingerprinted
    videos), resuming is a genuine continuation, not a restart — verified
    by killing the container mid-fingerprint and confirming a resumed
    scan picks up without redoing finished work.
  - A startup hook marks any `scan_runs` row still `'running'` as
    `'interrupted'` (e.g. after a container crash/restart whose
    orchestrating thread died with it), so the UI never shows a stale
    "running" forever — verified live with `docker kill` + restart.
  - Only one scan runs at a time — starting a second is rejected (HTTP
    409), enforced both server-side (a lock) and in the UI (start button
    hidden while a scan is active).
  - Submitted scan roots are validated against the configured
    `LIBRARY_PATHS` and rejected otherwise.

## 2026-06-20 — 0.1.0

- Support scanning multiple configurable library directories in
  `01_inventory.py` (positional CLI args, or the `LIBRARY_PATHS` env var
  when none are given).
- Add "dismiss this candidate" (not-a-match) feedback at the
  (preview, candidate) pair granularity, stored in a new `match_feedback`
  table so a dismissed false positive doesn't resurface when
  `03_match.py` is re-run. Distinct from the existing preview-level
  approve/keep decision; never touches a file.
- Add optional VAAPI hardware-accelerated decode (`--hwaccel vaapi`) to
  `02_fingerprint.py`. Defaults to software decode.
- Add `Dockerfile`, `docker-compose.yml`, `.dockerignore`, and
  `rebuild.sh` to run the app as a container, with bind mounts for the 
  library directories and DB/config.
- Add `VERSION` (semver) and this changelog.
- Fix `04_serve.py`'s two `TemplateResponse(...)` calls for current
  Starlette (1.3.1+), which requires `request` as the first positional
  argument rather than inside the context dict — the old call shape threw
  `TypeError: unhashable type: 'dict'` on every page load. Found by
  actually running the server end-to-end for the first time (via a venv
  on the deployment host and inside the built container), something the
  original dev sandbox couldn't do (no network egress to install
  FastAPI/uvicorn).
  Full approve→stage→undo, dismiss-candidate, and purge-confirm flows
  verified live against a synthetic test library afterward.
