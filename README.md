# conan-utils

Small, single-file utilities for **Conan 1.x** (targeting 1.66) that answer
questions the stock `conan info` / `conan search` commands make hard to answer.
Development and testing happen inside a pinned Podman container so nothing from
the host environment leaks in.

Licensed under the [MIT License](LICENSE).

## Tools

| Tool | What it does |
|------|--------------|
| [`conandeps`](#conandeps) | Lists direct/indirect dependencies, checks which build types have binaries on a remote for your exact profile, shows requirement overrides and reports packages with a newer version on the remote. |
| [`conansbom`](#conansbom) | Writes an SBOM of a conanfile's dependency graph – CycloneDX, SPDX or a readable HTML page: every package with its purl and declared license, build tools marked as not shipped. |

## conandeps

Given a `conanfile.py`, `conandeps` tells you four things in one report:

1. **What you depend on** – direct and indirect requirements, host and build
   context, as a table and as a tree in the HTML report.
2. **Which binaries are missing** – for every dependency, whether a package
   exists *on the remote* for **Release, Debug and RelWithDebInfo** (or any
   list you choose), evaluated for *your exact profile* (compiler, version,
   libcxx, arch, options, …), not "some binary with that build type". Every
   cell names the remote that has the binary; a binary that only lives in
   your local cache is reported as local cache only, and counts as missing.
3. **Which requirements were overridden** – both explicit
   `self.requires("x/2.0", override=True)` and implicit ones, where a
   downstream consumer simply asks for a newer version than a transitive
   dependency declared. Each override is listed with who wanted what and who
   forced the change.
4. **Which packages have a newer version on the remote** – versions are
   ordered with Conan's own lenient comparison (which copes with dates and
   most non-semver schemes), and the export timestamps of the current and
   newest recipe are fetched and shown (hover the newest version in the HTML
   report) so that schemes with no meaningful order (branch names, hashes)
   can still be judged by date. Informational
   only: it never affects the exit code. Disable with `--no-check-updates`.

### Installation

Requires Python 3.8 or newer. Install with [uv](https://docs.astral.sh/uv/)
or with [pip](#with-pip). Installing the package gives you both commands,
`conandeps` and [`conansbom`](#conansbom).

#### With uv

No uv yet? See the
[uv installation guide](https://docs.astral.sh/uv/getting-started/installation/)
for all platforms; on Linux and macOS it is one line:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then:

```bash
# As a command on your PATH, in its own venv with Conan 1.66:
uv tool install git+https://kode.naiv.no/olemd/conan-1.6-utilities.git
# with an SSH key:     uv tool install git+ssh://git@kode.naiv.no:2222/olemd/conan-1.6-utilities.git
# from a checkout:     uv tool install .
conandeps path/to/conanfile.py -r myremote

# Or run the single file directly, no checkout needed (PEP 723 inline metadata):
uv run conandeps.py path/to/conanfile.py -r myremote
```

Upgrade with `uv tool upgrade conan-utils`.

#### With pip

The package pulls in `conan>=1.66,<2`, so install it into a virtual
environment of its own. Otherwise it would replace or downgrade a Conan that
is already installed in the same environment:

```bash
python3 -m venv ~/.venvs/conan-utils
~/.venvs/conan-utils/bin/pip install git+https://kode.naiv.no/olemd/conan-1.6-utilities.git
# with an SSH key:     ... pip install git+ssh://git@kode.naiv.no:2222/olemd/conan-1.6-utilities.git
# without git:         ... pip install https://kode.naiv.no/olemd/conan-1.6-utilities/archive/main.tar.gz
# from a checkout:     ... pip install .
~/.venvs/conan-utils/bin/conandeps path/to/conanfile.py -r myremote
```

Either put `~/.venvs/conan-utils/bin` on your `PATH`, or symlink just the
command:
`ln -s ~/.venvs/conan-utils/bin/conandeps ~/.local/bin/`.

If you already build with Conan 1.66 from a venv, you can install into that
venv instead. pip sees that the Conan requirement is already met and leaves
Conan alone.

To upgrade, add `--force-reinstall --no-deps`:

```bash
~/.venvs/conan-utils/bin/pip install --force-reinstall --no-deps \
    git+https://kode.naiv.no/olemd/conan-1.6-utilities.git
```

A plain `pip install --upgrade` compares only the version number. Not every
pushed commit bumps it, so a plain upgrade can report success and still leave
the old code installed.

#### Conan configuration

The tool's venv carries its own Conan 1.66 and does not touch the Conan you
build with, but it reads the same `~/.conan` configuration: profiles,
`remotes.json` and stored remote credentials. Log in to your remote once with
`conan user -r <remote> -p` (or set `CONAN_LOGIN_USERNAME` / `CONAN_PASSWORD`)
if it requires authentication.

### Usage

```
conandeps CONANFILE [-r REMOTE] [-pr PROFILE] [-s KEY=VALUE] [-o PKG:KEY=VALUE]
          [--build-types Release,Debug,RelWithDebInfo] [-u] [--no-check-updates]
          [--fallbacks FILE] [--html FILE] [-v]
```

| Option | Meaning |
|--------|---------|
| `CONANFILE` | Path to `conanfile.py` (or a directory containing one). |
| `-r`, `--remote` | Remote to look for binaries in, e.g. `-r myremote`. Default: all configured remotes. |
| `-pr`, `--profile` | Profile to evaluate with (repeatable, like `conan -pr`). Default: your default profile. |
| `-s`, `-o` | Extra settings / options, repeatable, same syntax as `conan install`. |
| `--build-types` | Comma-separated build types to check. Default: `Release,Debug,RelWithDebInfo`. |
| `-u`, `--update` | Ask the remote for newer recipes/binaries (`conan -u`). |
| `--no-check-updates` | Skip the newer-version check (saves one recipe search per package, plus two manifest fetches per outdated one). |
| `--fallbacks FILE` | INI file naming, per build type, the packages to check under another build type instead. See [Build type fallbacks](#build-type-fallbacks). |
| `--html FILE` | Also write a self-contained HTML report (no scripts, no external assets). |
| `-v` | Stream Conan's own output to stderr live. |
| `-q` | Suppress the progress lines on stderr. |
| `--color auto\|always\|never` | Colour the text report. Default `auto`: only on a terminal, honours `NO_COLOR`. |

Progress (`[1/3] resolving graph for build_type=Release on myremote ...`) goes to
stderr; the report itself goes to stdout, so redirecting stdout to a file
gives a clean report.

Exit status: `0` all binaries present, `1` at least one is missing, `2` Conan
failed to build the graph (for example a version conflict). This makes it
usable as a CI gate. Newer versions found by the update check are
informational and never change the exit status.

### Example

```
$ conandeps MyProject/conanfile.py -r myremote --html report.html
conandeps report for MyProject/conanfile.py
package  : myproject/1.2
remote   : myremote
profile  : arch=x86_64, compiler=gcc, compiler.libcxx=libstdc++11, compiler.version=14, os=Linux

12 dependencies, 1 override, 1 newer version

Missing binaries (2)

package    build type      reason
--------------------------------------------
Zigma/1.0  Debug           local cache only
Zigma/1.0  RelWithDebInfo  no binary anywhere

HTML report written to report.html
```

That is the whole terminal report: the header, one line of counts, and what
is broken. The `package` line is the name and version the conanfile gives
itself, so a report kept as a file still says what it was about; a conanfile
that names neither simply has no such line. It matches the exit status, so a CI log shows the reason for a
failure without scrolling. Without `--html` the last line is instead
`Full report: --html FILE`. The reason column is coloured on a terminal (red
= no binary anywhere, yellow = local cache only); `--color never` or the
`NO_COLOR` environment variable turns that off, and redirecting stdout to a
file never produces colour codes unless you pass `--color always`.

### The HTML report

`--html FILE` writes the report proper: one self-contained page, no scripts
and no external assets, so it can be attached to a build or mailed to a
colleague. It follows the same colour coding, adapts to a dark theme, and has
three tables plus the dependency tree, each saying something the others do
not. The same header opens the page, and the
package name and version become the browser tab's title, which matters once
several reports are open at once.

**Dependencies and binary availability**, one row per package:

| package | via | context | newest | Release | Debug | RelWithDebInfo |
|---------|-----|---------|--------|---------|-------|----------------|
| MyDepA/1.0 | – | host | up to date | myremote | myremote | myremote |
| MyDepB/1.0 | – | host | up to date | myremote | myremote | myremote |
| cmake/3.29 | – | build | up to date | myremote | myremote | myremote |
| boost/1.8 | MyDepA<br>MyDepB → Zigma | host | 1.9 | myremote | myremote | myremote |
| Zigma/1.0 | MyDepB | host | up to date | myremote | myremote | MISSING |

The `via` column is the shortest path from your conanfile to the package –
`–` for a direct dependency, `MyDepB` for something MyDepB requires,
`MyDepB → Zigma` for something Zigma requires, and so on. A package required
by several others gets one line per parent (boost above is pulled in by both
MyDepA and Zigma), shortest path first. Rows are sorted on the via column and
then the package name, direct dependencies first, so a branch's transitive
dependencies stay together. Hover a package that was resolved from a version
range to see the range it was declared with.

The `context` column is Conan's host/build split. `host` means the package is
part of what you are building: it is compiled for your target platform and
linked into your binaries. `build` means it is a **build requirement**, a tool
that merely runs during the build (cmake, a code generator, a compiler
wrapper), so it is built for the machine doing the building, not for the
target. The distinction matters when you cross-compile, where the two are
different platforms and the same package can appear in both contexts. The
HTML report spells this out in a note under the table.

The `newest` column gives the newest version of that package on the remote, or
`up to date` when the resolved one is already the newest. Hover a newer version
to see how "newer" was decided and when the current and the newest recipe were
exported. `--no-check-updates` removes the column.

Each build-type cell is one of:

* a remote name (e.g. `myremote`) – the remote has a binary for the package_id your
  profile produces. Hover a cell in the HTML report to see the package_id.
* `local cache only` – the binary exists in your local Conan cache, but the
  remote does not have it, so a clean machine or CI would fail. Counts as
  missing.
* `MISSING` – no binary anywhere.
* `myremote (via Release)` – found, but under a fallback build type; see
  [Build type fallbacks](#build-type-fallbacks).
* `-` – not applicable (Conan marked the node `Skip` or `Editable`).
* Header-only packages show the remote in every column: their package_id
  ignores `build_type`, so one binary serves all of them.

There is no separate list of missing binaries or newer versions: the cells
above are that list. The terminal output still lists the missing binaries,
since that is what a CI log needs.

The **dependency hierarchy** at the end of the page shows the same graph as a
plain tree. It carries no annotations, since everything about a package is in
the table:

```
myproject/1.2
|-- MyDepA/1.0
|   `-- boost/1.8
`-- MyDepB/1.0
    `-- Zigma/1.0
        `-- boost/1.8
```

The **overrides** table lists what was rewritten. An override is credited to the
package that forced it, which for your own project is the name and version in
its conanfile, or `your conanfile` when it declares neither. The `Update order` section answers
"what do I bump first?". A package is in
the plan when the remote has a newer version of it, and also when one of its
requirements has one: MyDepA and Zigma above are at their newest version, but
they still have to be re-released against boost/1.9 before your project can
see it. Everything in a wave is independent of everything else in that wave,
so a wave can be done in parallel, and your conanfile is always the last one.
Like the root of the tree, it is shown by its package name, or by its path
when the conanfile declares no name and version.
`--no-check-updates` skips this section too.

Things worth knowing:

* Two sibling dependencies requiring different versions of the same package
  with no decision from your conanfile is a **conflict** in Conan 1.x, not an
  override. Conan refuses to build the graph, and `conandeps` prints Conan's
  conflict message and exits with `2`.
* An override can change the package_id of the package whose requirement was
  rewritten (with the default `semver_direct_mode`, a direct requirement's
  version is part of the id). If that makes *all* build types of a package go
  `MISSING`, the report is telling the truth: no binary on the remote was built
  against the overridden version.

### Build type fallbacks

Some packages are simply never built for every build type – zlib may exist as
`Release` and `Debug` only. Listing them in an INI file tells `conandeps`
which build type to check instead. A section is the build type being checked,
and each line names a package and the build type to use for it:

```ini
# fallbacks.ini
[RelWithDebInfo]
zlib = Release
openssl = Release

[Debug]
icu = Release
```

```
$ conandeps MyProject/conanfile.py -r myremote --fallbacks fallbacks.ini
```

The mapping is handed to Conan as a package-scoped setting
(`zlib:build_type=Release`) – exactly what you would put in your profile when
installing – so the package_id is the one Conan itself computes. If your
`default_package_id_mode` makes a dependency's settings part of its consumers'
ids, those consumers move along with it, just as they would in a real install.

Listed packages then stop counting as missing binaries, and stop failing the
exit code. Every cell that came from a fallback says so, so the report never
claims a binary that does not exist:

```
package   via   ctx   Release   Debug     RelWithDebInfo
--------------------------------------------------------------
zlib/1.3  -     host  myremote  myremote  myremote (via Release)
```

If the fallback has no binary either, it is still a missing binary, and the
row spells out which build type was actually looked for:

```
package   build type           package_id                                reason
--------------------------------------------------------------------------------------------
icu/70.1  Debug (via Release)  6b1f4c0a8d2e7b39f5c1a4d8e0b2f6c3a9d7e514  no binary anywhere
```

A section for a build type that is not in `--build-types` is dead config; the
tool says so on stderr. A package mapped to its own build type would do
nothing, and is rejected as an error.

### How it works

Rather than hand-matching `conan search` output against your profile, the
dependency graph is built once per build type through Conan's own `info` API –
the same code path `conan install` uses – and each node's binary status
(`Cache`, `Download`, `Update`, `Missing`, …) is read back. That is why
`package_id()` customisations, options and `default_package_id_mode` are
honoured exactly as a real install would. Conan reports `Cache` without
consulting the remote, so for those nodes the tool additionally runs a
package search on the remote and only counts the binary as available if the
same package_id is found there.

Conan 1.x does not keep override information on the graph object; the only
trace is a `WARN: <pkg>: requirement A overridden by B to C` line written while
the graph is resolved. The tool captures Conan's output stream during graph
construction and parses those lines. A test pins the message format.

## conansbom

Writes a software bill of materials for a `conanfile.py`, for inventory and
license compliance – to hand to a customer, load into Dependency-Track, or
read yourself. Three formats, all with the same content:

| `--format` | What you get |
|------------|--------------|
| `cyclonedx` (default) | [CycloneDX](https://cyclonedx.org/) 1.6 JSON |
| `spdx` | [SPDX](https://spdx.dev/) 2.3 JSON |
| `html` | A self-contained page for people: a license overview, then the packages that ship and the ones only used to build. No scripts, no external assets, same look as the `conandeps` report. |

```bash
conansbom path/to/conanfile.py -r myremote --out sbom.cdx.json
conansbom path/to/conanfile.py -r myremote -f spdx --out sbom.spdx.json
conansbom path/to/conanfile.py -r myremote -f html --out sbom.html
conansbom path/to/conanfile.py -pr myprofile -s build_type=Debug > sbom.cdx.json
```

| Option | Meaning |
|--------|---------|
| `CONANFILE` | Path to `conanfile.py` (or a directory containing one). |
| `-r`, `-pr`, `-s`, `-o`, `-u` | As for `conandeps`: remote, profile, settings, options, `conan -u`. |
| `-f`, `--format` | `cyclonedx` (default), `spdx` or `html`. |
| `--out FILE` | Write the SBOM to `FILE`. Default: stdout. |
| `-v` | Stream Conan's own output to stderr live. |

An SBOM describes one concrete build, so there is no build-type matrix: pass
the profile and settings you ship with. Exit status: `0` SBOM written, `2`
Conan failed to build the graph.

What is in it:

* **The project is the subject** (CycloneDX `metadata.component`, the
  package SPDX `DESCRIBES`), with the name, version and license its
  conanfile declares, and the profile's settings, so the document says which
  build it describes.
* **Every package in the graph, once**: name, version,
  [purl](https://github.com/package-url/purl-spec)
  (`pkg:conan/zlib@1.3`, with `user`/`channel` qualifiers when the package
  has them), description, homepage and recipe repository.
* **What ships and what only builds it.** A package the project reaches
  without crossing a build requirement ships. A build requirement, and
  everything only it pulls in, does not. CycloneDX says so with
  `scope: required` / `scope: excluded` ("used, but not delivered"); SPDX,
  which has no scope, with a `BUILD_DEPENDENCY_OF` relationship instead of
  `DEPENDS_ON` and a "Build only" comment. A package used both ways ships.
  It is still listed once (in CycloneDX with its Conan contexts and each
  context's package_id as properties).
* **The full dependency graph**, build-requirement edges included.
* **Licenses exactly as the recipe declares them.** Nothing is guessed or
  inferred from source code. A declared license that is an SPDX identifier
  or expression (`MIT`, `mit`, `MIT OR Apache-2.0`,
  `GPL-2.0-or-later WITH Classpath-exception-2.0`) is written as one, in the
  SPDX list's spelling: `license.id` or an `expression` in CycloneDX,
  `licenseDeclared` in SPDX. Anything else (`Proprietary`, `ACME EULA`) is
  kept verbatim: a named license in CycloneDX, and in SPDX a `LicenseRef-`
  whose text is the declared string. A tuple of licenses in a recipe means
  all of them apply (`AND`). SPDX's `licenseConcluded` and `copyrightText`
  are `NOASSERTION` – the tool concludes nothing.
* **Missing licenses are flagged.** Packages whose recipe declares no license
  are listed on stderr – `conansbom: WARN: no license declared: …` – and in
  red in the HTML page, since those are the ones a compliance review has to
  chase.

The SPDX license list (currently 3.29.0) is embedded, so the tool works
offline.

Components are sorted by reference, so two SBOMs of the same project diff
cleanly.

Why a tool of its own: SBOM generators that read a Conan 1 lockfile, such
as Syft, cannot report licenses – the lockfile does not record them – and do
not tell build tools from what ships. cyclonedx-conan, which did read Conan 1
recipes, was archived in 2023, and Conan's built-in SBOM support is Conan 2
only. `conansbom` reads the same graph `conan install` resolves, with the
recipes already loaded.

`conansbom` shares its Conan plumbing with `conandeps`, so unlike
`conandeps.py` it cannot be run as a lone downloaded file. It works installed
or from a checkout (`uv run conansbom.py …`).

## Releases

Tagged releases with a built wheel, source distribution and `SHA256SUMS` are
published on the
[releases page](https://kode.naiv.no/olemd/conan-1.6-utilities/releases).
Latest: [v0.5.0](https://kode.naiv.no/olemd/conan-1.6-utilities/releases/tag/v0.5.0).

Install a specific release with

```bash
uv tool install git+https://kode.naiv.no/olemd/conan-1.6-utilities.git@v0.1.0
# or, from the downloaded wheel:
uv tool install conan_utils-0.1.0-py3-none-any.whl

# the same with pip, inside the venv from "With pip" above:
pip install git+https://kode.naiv.no/olemd/conan-1.6-utilities.git@v0.1.0
pip install conan_utils-0.1.0-py3-none-any.whl
```

To cut a release: bump `version` in `pyproject.toml`, push an annotated tag
`vX.Y.Z`, build with `./dev.sh uv build`, and create the Forgejo release with
the artifacts attached (`fj release create --tag vX.Y.Z --attach ...`).

## Development

Everything runs inside the `conan-utils-dev` container (Conan 1.66, Python
3.11, ruff, pytest, uv) via the `dev.sh` wrapper, which mounts the checkout at
`/work`:

```bash
./dev.sh build                              # build the image
./dev.sh python -m pytest                   # run the tests
./dev.sh ruff check . && ./dev.sh ruff format .
./dev.sh                                    # interactive shell
```

The tests start a throw-away `conan_server` inside the container, upload a
small dependency graph with deliberately missing binaries and both kinds of
override, wipe the local cache, and run the tool against the server using an
isolated `CONAN_USER_HOME`. Your own `~/.conan` is never touched.

See [CLAUDE.md](CLAUDE.md) for the conventions that apply when working on this
repository.
