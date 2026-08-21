# Comparison: PoC (libsolv lazy-loading) vs. `filelists-lazy-auto-loading` (resolve-failure retry)

## Architecture

| Aspect | PoC (libsolv callback) | Original branch (retry) |
|---|---|---|
| **Mechanism** | libsolv's native `pool_setloadcallback` + `REPODATA_STUB` | Full `Goal::resolve()` retry after failure detection |
| **Trigger** | `pool_addfileprovides_queue()` hits stubs during pool preparation — fires on every run | Explicit — `has_file_dependency_problem()` inspects solve logs for unresolved path deps |
| **When filelists are downloaded** | Always, during `make_provides_ready()`, before the solver runs | Only when the solver reports an unresolved file dependency |
| **Download scope** | All repos — libsolv searches all repos' repodata for file providers | All repos — iterates all enabled repos |
| **Number of solves** | 1 | 2 (only when filelists are needed) |

## Pros of the PoC (libsolv callback)

1. **Single solve pass.** When filelists *are* needed, the retry approach runs the full solver twice. The PoC loads filelists inline and solves once. However, this advantage is diminished by the fact that the PoC loads filelists even when they're *not* needed (see cons).

2. **Simpler control flow.** No `resolve_attempt` lambda, no transaction copy semantics, no retry orchestration in `goal.cpp`. The 314-line refactor of `Goal::resolve()` is avoided.

3. **Smaller diff.** +157/-8 lines across 5 files vs. +631/-116 lines across 22 files.

4. **Covers all trigger paths uniformly.** The retry approach must pattern-match solver error logs (`RULE_PKG_NOTHING_PROVIDES_DEP` with path-like params) and needed a special case for `--no-best` (`SOLVER_PROBLEM_STRICT_RESOLVEMENT`). The PoC delegates triggering entirely to libsolv. (Though as written, it over-triggers — see cons.)

## Pros of the Original (retry) approach

1. **Downloads filelists only when actually needed.** The retry approach only downloads filelists when the solver has a concrete unresolved file dependency. The PoC as written does not set `POOL_FLAG_ADDFILEPROVIDESFILTERED`, so `pool_addfileprovides_queue()` searches all repodata (including stubs) for *every* file dep — even standard ones already in primary. This means filelists are downloaded on **every run**, regardless of whether the transaction needs them. The "lazy" loading is effectively eager.

2. **No libsolv internals coupling.** Doesn't depend on `REPODATA_STUB`, `REPOSITORY_EXTERNAL` flexarray, `REPO_USE_LOADING`, or pool callback semantics. These are semi-internal APIs used by libsolv's example code but not part of a documented stable interface.

3. **Download integrity.** Uses the full `RepoDownloader` pipeline with GPG signature verification, metalink/mirrorlist selection, and cache management. The PoC uses `FileDownloader` with a bare URL, manually constructs the destination path, and bypasses checksum/GPG verification.

4. **Solv cache integration.** The retry approach works with the existing `.solv`/`.solvx` cache via `load_repo_ext()`. The PoC loads filelists XML directly into the stub without writing `.solvx` cache files, so filelists must be re-downloaded and re-parsed on every run.

5. **User visibility and control.** The retry approach logs a clear message ("Automatically downloaded and loaded filelists...") and provides the `filelists_auto_load` config option to disable the behavior. The PoC happens silently inside a libsolv callback with only debug-level log lines and no opt-out.

6. **Test coverage.** 5 dedicated test cases with fixture RPMs covering: basic auto-load, disabled option, `--no-best` interaction, missing provider, and pre-loaded filelists. The PoC has none.

7. **Network I/O outside of libsolv.** The retry approach downloads files in a well-defined phase between solve attempts. The PoC downloads inside a libsolv callback during pool traversal, introducing network I/O (with potential timeouts, retries, failures) in the middle of an internal libsolv operation.

## Could the PoC be fixed?

### Attempt 1: `POOL_FLAG_ADDFILEPROVIDESFILTERED` + suppress during `pool_addfileprovides_queue()`

Setting `POOL_FLAG_ADDFILEPROVIDESFILTERED = 1` combined with `REPODATA_FILELIST_EXTENSION` on stubs successfully prevents filelists downloads during `pool_addfileprovides_queue()`. Standard file deps (`*bin/*`, `/etc/*`, `/usr/lib/sendmail`) are resolved from primary's filtered filelists. Non-standard deps are separated into `pool->nonstd_ids` and deferred to lazy resolution during solving.

This fixes the `pool_addfileprovides_queue` phase — stubs remain in `REPODATA_STUB` state and are not loaded.

### Attempt 2: The solver phase problem

However, `pool_createwhatprovides()` sets lazy markers (`whatprovides[id] = 0`) for all non-standard file deps. During solving, when `pool_whatprovides()` encounters a lazy marker, it calls `pool_addstdproviders()` which performs a **pool-wide** search:

```c
dataiterator_init(&di, pool, 0, 0, SOLVABLE_FILELIST, str, SEARCH_STRING|SEARCH_FILES);
```

The `repo = 0` parameter means "all repos". This triggers stub loading for **every** repo in the pool — not just repos that might contain the needed file. The callback receives only the `Repodata *` being loaded and has no way to know which file dep triggered it or whether this repo could satisfy it.

Crucially, the non-standard file deps in `nonstd_ids` come from **all available packages** in the pool (e.g., thousands of Fedora packages with `Requires: /usr/share/...`), not just from the transaction target. So even `dnf install acpi` — a package with zero file dependencies — triggers filelists downloads for all repos because the solver evaluates supplements/enhances of unrelated packages that have non-standard file deps.

### Attempt 3: Suppress during solving + retry

Suppressing stubs during solving (returning 0 from the callback) prevents downloads but causes stubs to enter `REPODATA_ERROR` state. This can be combined with a retry:

1. Suppress stubs during solving → solver fails for packages with unresolved file deps
2. Detect broken file deps (`RULE_PKG_NOTHING_PROVIDES_DEP` where dep starts with `/`)
3. Reset stubs (`REPODATA_ERROR` → `REPODATA_STUB`), `pool_freewhatprovides()`, re-run `make_provides_ready()`, re-solve

This works correctly — `acpi` succeeds without filelists, `test-file-deps` triggers retry and succeeds with filelists. But it's functionally identical to the retry approach on the original branch, adding libsolv stub complexity without benefit.

### Real-world brittleness: non-standard Recommends

Even with `POOL_FLAG_ADDFILEPROVIDESFILTERED` and stub suppression during `pool_addfileprovides_queue()`, a single package with a non-standard file dep is enough to trigger filelists loading on every transaction, regardless of whether the transaction actually needs file dependency resolution.

Example: `x264-libs` has `Recommends: /usr/lib64/libOpenCL.so.1`. This path doesn't match the standard filter (`*bin/*`, `/etc/*`, `/usr/lib/sendmail`), so `pool_addfileprovides_queue()` places it in `nonstd_ids` and defers resolution. During `pool_createwhatprovides()`, the dep has no providers from primary metadata (`whatprovides[id]` is already 0), so it stays as a lazy marker. When the solver evaluates x264-libs' Recommends — which it does on every transaction, not just transactions involving x264-libs — it calls `pool_whatprovides(/usr/lib64/libOpenCL.so.1)`, finds `whatprovides[id] == 0`, and enters `pool_addstdproviders()` which performs the pool-wide `dataiterator` search and triggers stub loading.

This is by design: `POOL_FLAG_ADDFILEPROVIDESFILTERED` defers non-standard deps to lazy resolution, not skips them. Any installed or available package with a non-standard file path in Requires, Recommends, Supplements, or any other dep type will cause filelists to be loaded on every single transaction — even `dnf install acpi` which has zero file dependencies.

### Fundamental limitation

libsolv's lazy-loading mechanism is designed to defer metadata loading, not to conditionally skip it. The pool-wide `dataiterator` in `pool_addstdproviders()` makes per-repo selective loading impossible from the callback. To achieve true on-demand per-repo loading, libsolv itself would need changes — e.g., a per-dep or per-repo callback interface so the solver only loads filelists for repos that might satisfy the specific dep being resolved.

## Verdict

The PoC demonstrates that libsolv's stub mechanism *can* be wired into dnf5, but it cannot achieve the original goal of single-pass solving with conditional filelists loading. The `pool_addstdproviders()` pool-wide search means stubs are triggered for all repos whenever any non-standard file dep is looked up — even from packages unrelated to the transaction. The only way to avoid unnecessary downloads is to suppress stubs and retry on failure, which reduces the PoC to the same retry architecture as the original branch but with added libsolv internals coupling, no GPG verification, no solv caching, no tests, and network I/O inside callbacks. The retry approach is the correct design for this problem.
