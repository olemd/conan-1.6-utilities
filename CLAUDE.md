# CLAUDE.md

Guidance for working on this repository. It records decisions made when the
project was started (August 2026) that are not obvious from the code.

## What this is

Small single-file Python utilities for **Conan 1.x, targeting 1.66**. Not
Conan 2. The first (and so far only) tool is `conandeps.py`; see `README.md`
for what it does and how it is used.

## Commands (all inside the container)

```bash
./dev.sh build                       # first time, and after editing Containerfile
./dev.sh python -m pytest            # tests (starts a throw-away conan_server)
./dev.sh ruff check . && ./dev.sh ruff format .
shellcheck dev.sh                    # after editing the wrapper (runs on host)
./dev.sh uv lock                     # after changing pyproject.toml
```

Users install/upgrade on their own machine with
`uv tool install git+https://kode.naiv.no/olemd/conan-1.6-utilities.git` /
`uv tool upgrade conan-utils`; a pushed commit is immediately installable.

## Hard constraints

- **Conan 1.66 only.** Use the `conans.client.conan_api.ConanAPIV1` API and
  Conan 1 graph internals (`Node.binary`, `Node.dependencies`,
  `conanfile.requires`). Do not introduce Conan 2 APIs or a Conan 2 code path.
- **Python 3.8+.** Conan 1.66 runs on new Pythons too (it falls back to
  `importlib` where `imp` is gone) – verified on 3.14. It does emit
  SyntaxWarnings from its own modules there, which `conandeps.py` silences
  before importing `conans`; keep `conans` imports deferred so that filter
  is in place first. `requires-python` in `pyproject.toml` and the PEP 723
  header must stay in sync.
- **All development runs inside the container.** Use `./dev.sh <cmd>` for
  every python/pytest/ruff/uv invocation. Never run `conan` against the
  host's `~/.conan`; never `pip install` on the host. Rebuild the image with
  `./dev.sh build` after touching `Containerfile`.
- **Never depend on the private remote.** The user's real remote only
  exists on the machine where the tool is used and must not be named in the
  repository – use a placeholder such as `myremote` in docs and help text.
  Tests must use the local `conan_server` fixture in
  `tests/test_conandeps.py`.

## Design decisions (and why)

- **Binary availability is computed via Conan's graph, not `conan search`.**
  The graph is built once per build type through `ConanAPIV1.info()` and each
  node's `binary` status is read. This honours `package_id()` overrides,
  options and `default_package_id_mode` exactly like `conan install`. Do not
  replace this with settings-dict matching against `conan search` output.
- **"Missing" means missing for the exact profile.** Not "no binary with
  that build_type at all".
- **Build-type fallbacks are package-scoped settings, not report filtering.**
  `--fallbacks` (`_load_fallbacks`, INI, one section per build type) is turned
  into `-s <pkg>:build_type=<fallback>` for that build type's `api.info()`
  call, so Conan computes the fallback package_id and any knock-on effect on
  consumers. Nothing is subtracted from `Dep.missing()`: a listed package
  drops out of the missing-binaries report only because its binary was really
  found. `Dep.fallbacks[bt]` is recorded only when the node's own settings
  confirm Conan applied it, and both renderers append `(via <bt>)` to every
  affected cell so a fallback can never be read as a real binary.
- **Dependency table shows the shortest path, not a level number.**
  `_paths()` does a breadth-first walk from the root; `Dep.path` is the list
  of package names on the *shortest* path (empty = direct, so a package that
  is both direct and transitive is direct). `Dep.paths` holds one path per
  direct parent (shortest path to that parent + parent) – bounded, unlike
  "all paths". The `via` column renders them one per line as `A -> B`
  (`→`/`<br>` in HTML); `_table()` supports multi-line cells. The HTML
  renderer sorts dependency rows on (via, name) via `_dep_key` so each chain
  reads as a block, and puts overrides before missing binaries.
  `report.deps` itself keeps (depth, path, host before build, name) order –
  the tree section and `problems()` rely on it. The tree keeps graph order.
- **Tree tags are typed.** `_tree_rows()` yields `(head, [(kind, text)], tail)`
  with kinds path/build-require/override/range/missing; the HTML renderer
  colours by kind (`<span class=…>`) from that one source. Add new tree annotations there, never
  by string-replacing rendered lines.
- **Only the remote counts, and the source is always shown.** Conan says
  `Cache` for a locally cached binary without asking the remote, so
  `_RemoteIndex` verifies cache hits with `search_packages` on the remote.
  Cache-only binaries are reported as `local cache only` (one wording for
  the cell and the missing-binaries reason, `_reason`) and count as a
  problem (exit 1). Every available cell names the remote. The user rejected
  an opt-in flag for this – it is the tool's core question.
- **The update check is version-first, timestamp-informed.** `_UpdateChecker`
  lists each package's references with `search_recipes` (same user/channel
  only), orders them with Conan's lenient `Version` (`_pick_latest`), and
  fetches the recipe manifests' export timestamps of current+newest only when
  they differ (two extra requests). Re-exporting refreshes a manifest's time,
  so version order wins and the note column (`_update_note`) spells out any
  disagreement; unreadable manifests render as `?`. On by default
  (`--no-check-updates` disables, `Report.checked_updates`); never affects
  the exit code. Do not fetch every candidate's manifest. The HTML
  dependency table joins `report.updates` on `Dep.ref` for its `newest`
  column (`_newest`, version only – user/channel always match) and omits the
  column entirely when `checked_updates` is False, so an empty cell can
  never be read as "up to date".
- **The text output is a summary; the HTML report is the report.** The user
  reads the HTML one, so `render_text` is the header, `_summary()` (one line
  of counts) and the missing binaries – nothing else, and no package_id
  column. Everything else (dependency table, overrides, newer versions,
  update order, tree) is HTML-only; do not add a section back to the text
  report. `main()` prints `Full report: --html FILE` when `--html` was not
  used. Renderer tests therefore assert on HTML, except for the summary and
  the missing-binaries table.
- **The update order is a wave plan, not just a topological sort.**
  `_update_order()` grows the outdated set to a fixpoint over
  `Dep.requires` (a consumer of an outdated package must be re-released for
  the new reference to reach the root, even at its own newest version), then
  gives each package a wave one past its deepest blocker, so a wave is
  parallelisable and the conanfile is always last. Names resolve
  host-context-first like `_tree_rows`, and cycles stop the walk. It is
  derived data: a function over `Report`, not a field on it.
- **Overrides are parsed from Conan's WARN output.** Conan 1 does not record
  overrides on the graph; `Requirements.update()` mutates `req.ref` in place
  and only emits `"<pkg>: requirement <old> overridden by <who> to <new>"`.
  `conandeps` captures the output stream with a non-coloured `ConanOutput`
  and matches `_OVERRIDE_RE`. `test_overrides` pins this format – if a Conan
  patch release changes the wording, that test is the alarm.
- **Both explicit and implicit overrides are reported.** `override=True`
  requirements are collected from `conanfile.requires` to label an override
  as explicit; everything else parsed from the WARN lines is implicit.
- **`range_ref` is not a version range after an override.** Conan reuses
  `Requirement.range_ref` to hold the pre-override reference, so only treat
  it as a range when `req.version_range` is truthy.
- **Single file, stdlib + Conan only.** No third-party deps beyond Conan.
  Packaging is hatchling with `only-include = ["conandeps.py"]`; the PEP 723
  header makes the file usable via `uv run conandeps.py` without a checkout.
- **Exit codes:** `0` fine, `1` missing binaries, `2` Conan error. CI relies
  on this.
- **Tables and colour, consistently.** The text missing-binaries table uses
  `_table()`; every HTML table uses `_html_table()`. Colour is an aid only – every state is also spelled
  out in words – and `_table()` aligns on *visible* width so ANSI codes never
  break columns. `--color auto` is the default (terminal + no `NO_COLOR`).
- **stdout is the report, stderr is progress.** Graph resolution against a
  remote takes a while; progress lines (`_progress`) and `--verbose` live
  Conan output go to stderr only, so stdout can be redirected to a file.

## Workflow

- Lint/format: `./dev.sh ruff check .` and `./dev.sh ruff format .`
  (config in `pyproject.toml`, target py38, line length 100). Run
  `shellcheck dev.sh` after editing the wrapper.
- Tests: `./dev.sh python -m pytest`. The fixture spins up `conan_server`
  on a free port with a pre-written `server.conf` (the port cannot be given
  on the command line), creates fake packages whose `build()` does nothing,
  uploads them, then wipes the cache so binaries can only come from the
  remote. Fake recipes need `--build=missing` at create time because
  dependencies' binaries are deliberately incomplete.
- **Assert on table output with regexes, not fixed-width strings.** Column
  widths depend on the longest ref in the fixture and package_ids change
  whenever a fixture recipe's requirements change (`semver_direct_mode`).
  Use `re.search(r"libbar/1\.1\s+libfoo\s+host", text)` and `[0-9a-f]{40}`
  for ids. Also: `_conan()` in the tests raises with Conan's full output on
  failure – read it before guessing.
- **Use the `conan_env` fixture only for graph/remote behaviour.** Anything
  about rendering (`_via`, `_table`, `_tree_rows`, HTML) should be a
  fixture-free unit test on hand-built `Dep` objects, like
  `test_via_lists_every_parent_path` – instant, and no `conan_server`.
- Dependencies: `uv.lock` is committed; regenerate with `./dev.sh uv lock`
  after changing `pyproject.toml`.
- Commits: atomic, no `--amend`; run ruff + pytest + shellcheck before
  committing. Add a memory of anything a future session could not derive
  from the repository.
- Releases: bump `version` in `pyproject.toml` (must match the tag), commit,
  then `git tag -a vX.Y.Z && git push origin vX.Y.Z`, build with
  `./dev.sh uv build` (writes `dist/`, gitignored), write `SHA256SUMS`, and
  `fj release create --tag vX.Y.Z --attach dist/<wheel> --attach dist/<sdist>
  --attach dist/SHA256SUMS --body "..." "vX.Y.Z"` from inside the repo –
  `fj release` does **not** accept `--repo`. Then update the "Latest:" line
  in the README's Releases section and delete `dist/`.

## Adding another tool

Follow the `conandeps.py` pattern: one executable file with a module
docstring explaining *how* and *why*, a PEP 723 header, a `main(argv)` that
returns an exit code, a `[project.scripts]` entry, an `only-include` entry in
`pyproject.toml`, tests under `tests/` using the shared `conan_env` fixture,
and a section in `README.md`.
