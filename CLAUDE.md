# CLAUDE.md

Guidance for working on this repository. It records decisions made when the
project was started (August 2026) that are not obvious from the code.

## What this is

Small single-file Python utilities for **Conan 1.x, targeting 1.66**. Not
Conan 2. The tools are `conandeps.py` (dependency/binary report) and
`conansbom.py` (CycloneDX/SPDX/HTML SBOM); see `README.md` for what they do and how
they are used.

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
pip works too (plain PEP 517, documented in the README). pip upgrades need
`--force-reinstall --no-deps`, because `--upgrade` skips a git URL whose
`version` is unchanged.

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
  Tests must use the local `conan_server` fixture (`conan_env`) in
  `tests/conftest.py`, shared by every tool's tests.

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
  reads as a block.
  `report.deps` itself keeps (depth, path, host before build, name) order –
  the tree section and `problems()` rely on it. The tree keeps graph order.
- **The HTML report says each thing once.** The dependency table is the
  one place for per-package facts: availability cells (package_id as
  tooltip), `newest` (update note and both export dates as tooltip) and the
  version range (package cell tooltip). There is deliberately no HTML
  "Missing binaries" or "Newer versions" section. The user asked for this to cut
  duplication; do not add a section that repeats table cells. (The text
  summary keeps its missing-binaries list.)
- **The tree is its own, interactive tab.** The page has two ARIA tabs,
  Report (the tables) and Dependency tree. `_tree()` builds `_TreeNode`s,
  `_tree_html()` renders nested `<ul>`s of buttons, and the inline
  `_TREE_JS` switches tabs (`#tree` opens the tree) and, on click, outlines
  every path from the root to *every* occurrence of that package – its
  ancestors only, never siblings or children. The one annotation the user
  wanted in the tree is the problem marking: red/✗ `missing` (wins) or
  orange/⚠ `cacheonly`, reusing the table cells' classes, with the build
  types as a `title`. The user found that tooltip does not show for them,
  so the page and README do not tell readers to hover (the table has the
  build types). Add nothing else to it. Progressive enhancement: the tab
  bar starts `hidden`, so without scripts both panels show stacked. The JS
  is checked manually in a browser (no JS runner in the tests).
  Real trees are taller than a screen, so the selection is also shown in a
  sticky side panel (`aside.paths`): a clone of `#tree` with every `li` that
  has no selected button removed – just the paths, merged where they share
  ancestors. Clicks in the panel select too. It is first in the DOM (the
  grid puts it on the right) so the stacked narrow layout keeps visual and
  focus order the same, and it starts `hidden` like the tab bar.
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
- **The report identifies the conanfile's own package.** `_root_ref()` reads
  `name`/`version` off the root conanfile (both optional in Conan 1) into
  `Report.package`; `_header()` is the one place that orders the header
  label/value pairs for both renderers, and the HTML title falls back to the
  path when the conanfile names nothing.
- **The text output is a summary; the HTML report is the report.** The user
  reads the HTML one, so `render_text` is the header, `_summary()` (one line
  of counts) and the missing binaries – nothing else, and no package_id
  column. Everything else (dependency table with the newest versions,
  overrides, update order, tree) is HTML-only; do not add a section back to the text
  report. `main()` prints `Full report: --html FILE` when `--html` was not
  used. Renderer tests therefore assert on HTML, except for the summary and
  the missing-binaries table.
- **The update order is a wave plan, not just a topological sort.**
  `_update_order()` grows the outdated set to a fixpoint over
  `Dep.requires` (a consumer of an outdated package must be re-released for
  the new reference to reach the root, even at its own newest version), then
  gives each package a wave one past its deepest blocker, so a wave is
  parallelisable and the conanfile is always last. Names resolve
  host-context-first like `_tree`, and cycles stop the walk. It is
  derived data: a function over `Report`, not a field on it.
- **Overrides are parsed from Conan's WARN output.** Conan 1 does not record
  overrides on the graph; `Requirements.update()` mutates `req.ref` in place
  and only emits `"<pkg>: requirement <old> overridden by <who> to <new>"`.
  `conandeps` captures the output stream with a non-coloured `ConanOutput`
  and matches `_OVERRIDE_RE`. `test_overrides` pins this format – if a Conan
  patch release changes the wording, that test is the alarm. `<who>` is the
  *downstream node's own reference*; Conan only writes "your conanfile" when
  the consumer declares no name/version, so `_explicit_overrides` builds the
  same string from `node.ref` – get that wrong and an explicit override is
  reported as implicit. The test fixture's consumer is named
  (`myproject/0.1`) precisely to keep that path covered.
- **Both explicit and implicit overrides are reported.** `override=True`
  requirements are collected from `conanfile.requires` to label an override
  as explicit; everything else parsed from the WARN lines is implicit.
- **`range_ref` is not a version range after an override.** Conan reuses
  `Requirement.range_ref` to hold the pre-override reference, so only treat
  it as a range when `req.version_range` is truthy.
- **Single file, stdlib + Conan only.** No third-party runtime deps beyond
  Conan (dev/test deps are fine). Packaging is hatchling with `only-include`
  listing each tool file; the PEP 723 header makes `conandeps.py` usable via
  `uv run conandeps.py` without a checkout. `conansbom.py` imports
  `conandeps` for the Conan plumbing (`_make_api`, `_Tee`, `_root_ref`,
  `_resolve_graph`) – the user chose one graph implementation over
  standalone files – so it runs installed or from a checkout only.
  `_resolve_graph` is the one place any tool calls `api.info()`.
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
  about rendering (`_via`, `_table`, `_tree`, HTML) should be a
  fixture-free unit test on hand-built `Dep` objects, like
  `test_via_lists_every_parent_path` – instant, and no `conan_server`.
- Dependencies: `uv.lock` is committed; regenerate with `./dev.sh uv lock`
  after changing `pyproject.toml`. The container installs dev tools with pip
  from the `Containerfile`, not from the dev group, so a new dev dependency
  goes in **both** and needs `./dev.sh build`.
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

## conansbom design decisions (and why)

- **Own tool.** Checked 2026-09-29: Syft on a Conan 1 lockfile works but
  the lockfile has no license field and Syft does not mark build
  requirements (duplicate purls, no scope); cyclonedx-conan is archived;
  Conan's SBOM tool is Conan 2 only. The user needs inventory *and*
  customer-facing license compliance.
- **Three formats from one `Sbom`** (`--format`): CycloneDX 1.6 JSON
  (default), SPDX 2.3 JSON (the version customer tooling reads; not 3.0),
  and a self-contained HTML page (user's choice over Markdown/text) that
  reuses `conandeps._CSS`/`_html_table`. Every format says the same things;
  add a fact to the `Sbom` model, then to all three renderers.
- **One build, one graph.** An SBOM describes a concrete build: one
  `_resolve_graph` call with the given profile/settings, no build-type
  matrix, no remote binary checks.
- **Scope is reachability, not context.** `required` = reachable from the
  root without crossing a `build_require` edge (`_runtime_nodes`), else
  `excluded`. This works with or without a build profile and puts a build
  tool's own requirements on the build side. SPDX has no scope: the edge
  kind (`Component.build_requires`) becomes `BUILD_DEPENDENCY_OF` vs
  `DEPENDS_ON`, and build-only packages get a "Build only" comment.
- **One component per reference.** A ref in both contexts is merged (it
  ships → `required`), contexts and per-context package_ids are
  properties. Two components with one purl break Dependency-Track.
- **Licenses as declared, classified against the embedded SPDX list.**
  The user chose to embed the list (`_SPDX_LICENSES`/`_SPDX_EXCEPTIONS`,
  `SPDX_LIST_VERSION`, deprecated ids included) over LicenseRef-everything.
  `spdx_expression()` accepts known ids (case-insensitive, canonical case
  out), `+`, AND/OR (any case), WITH + listed exception, parentheses;
  never `LicenseRef-` (a recipe cannot supply its text). SPDX → canonical
  expression in `licenseDeclared` / `license.id` or `expression` in
  CycloneDX (which cannot mix entries and an expression); anything else →
  `LicenseRef-<text>` with `hasExtractedLicensingInfos` in SPDX,
  `license.name` in CycloneDX. A recipe tuple means AND.
  `licenseConcluded`/`copyrightText` stay `NOASSERTION`.
- **Missing licenses warn by default; `--require-licenses` fails (exit 1).**
  `unlicensed()` (the project included) is the single list behind both the
  stderr line (`WARN:`, or `error:` with the flag) and the exit code, so they
  cannot disagree. The SBOM is written before the exit so CI can keep the
  failing build's document. Exit codes mirror conandeps: 1 = problem found,
  2 = Conan error.
- **Regenerating the SPDX list**: fetch `json/licenses.json` and
  `json/exceptions.json` from
  https://github.com/spdx/license-list-data (all ids, deprecated included),
  write them as the two whitespace-separated blocks sorted case-insensitively
  and wrapped at 96 columns, and bump `SPDX_LIST_VERSION`.
  `test_embedded_spdx_list_is_current` fails when the validator's
  license-expression knows an SPDX id the list lacks; the reverse is normal
  (the official list is ahead of the validator). SPDX's
  `licenseListVersion` must be `M.N`, so the patch part is dropped there.
- **HTML links only http(s)** homepages (`_link`), so a recipe cannot
  inject a `javascript:` URL into the page.
- **purl per the spec's conan type**: `pkg:conan/name@version`, `user` and
  `channel` as sorted qualifiers; the purl is the bom-ref. An unnamed
  project has no purl and bom-ref `project`.
- **Every document is validated in the tests**: CycloneDX with
  `cyclonedx-python-lib`'s `JsonStrictValidator` (V1_6), SPDX with
  `spdx-tools`' `validate_full_spdx_document`. Keep that for any output
  change.

## Adding another tool

Follow the `conandeps.py`/`conansbom.py` pattern: one executable file with a module
docstring explaining *how* and *why*, a PEP 723 header, a `main(argv)` that
returns an exit code, a `[project.scripts]` entry, an `only-include` entry in
`pyproject.toml`, tests under `tests/` using the shared `conan_env` fixture
(`tests/conftest.py`), graph resolution through `conandeps._resolve_graph`,
and a section in `README.md`.
