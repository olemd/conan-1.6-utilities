# conan-utils

Small, single-file utilities for **Conan 1.x** (targeting 1.66) that answer
questions the stock `conan info` / `conan search` commands make hard to answer.
Development and testing happen inside a pinned Podman container so nothing from
the host environment leaks in.

Licensed under the [MIT License](LICENSE).

## Tools

| Tool | What it does |
|------|--------------|
| [`conandeps`](#conandeps) | Lists direct/indirect dependencies, checks which build types have binaries on a remote for your exact profile, and shows requirement overrides. |

## conandeps

Given a `conanfile.py`, `conandeps` tells you three things in one report:

1. **What you depend on** – direct and indirect requirements, host and build
   context, as a table and as a tree.
2. **Which binaries are missing** – for every dependency, whether a package
   exists *on the remote* for **Release, Debug and RelWithDebInfo** (or any
   list you choose), evaluated for *your exact profile* (compiler, version,
   libcxx, arch, options, …), not "some binary with that build type". Every
   cell names the remote that has the binary; a binary that only lives in
   your local cache is reported as not available.
3. **Which requirements were overridden** – both explicit
   `self.requires("x/2.0", override=True)` and implicit ones, where a
   downstream consumer simply asks for a newer version than a transitive
   dependency declared. Each override is listed with who wanted what and who
   forced the change, and annotated on the exact edge of the dependency tree
   where it happened.

### Installation

Requires Python 3.8 or newer. With
[uv](https://docs.astral.sh/uv/):

```bash
# As a command on your PATH, in its own venv with Conan 1.66:
uv tool install git+https://kode.naiv.no/olemd/conan-1.6-utilities.git
# with an SSH key:     uv tool install git+ssh://git@kode.naiv.no:2222/olemd/conan-1.6-utilities.git
# from a checkout:     uv tool install .
conandeps path/to/conanfile.py -r myremote

# Or run the single file directly, no checkout needed (PEP 723 inline metadata):
uv run conandeps.py path/to/conanfile.py -r myremote
```

The tool's venv carries its own Conan 1.66 and does not touch the Conan you
build with, but it reads the same `~/.conan` configuration: profiles,
`remotes.json` and stored remote credentials. Log in to your remote once with
`conan user -r <remote> -p` (or set `CONAN_LOGIN_USERNAME` / `CONAN_PASSWORD`)
if it requires authentication.

### Usage

```
conandeps CONANFILE [-r REMOTE] [-pr PROFILE] [-s KEY=VALUE] [-o PKG:KEY=VALUE]
          [--build-types Release,Debug,RelWithDebInfo] [-u] [--html FILE] [-v]
```

| Option | Meaning |
|--------|---------|
| `CONANFILE` | Path to `conanfile.py` (or a directory containing one). |
| `-r`, `--remote` | Remote to look for binaries in, e.g. `-r myremote`. Default: all configured remotes. |
| `-pr`, `--profile` | Profile to evaluate with (repeatable, like `conan -pr`). Default: your default profile. |
| `-s`, `-o` | Extra settings / options, repeatable, same syntax as `conan install`. |
| `--build-types` | Comma-separated build types to check. Default: `Release,Debug,RelWithDebInfo`. |
| `-u`, `--update` | Ask the remote for newer recipes/binaries (`conan -u`). |
| `--html FILE` | Also write a self-contained HTML report (no scripts, no external assets). |
| `-v` | Stream Conan's own output to stderr live. |
| `-q` | Suppress the progress lines on stderr. |
| `--color auto\|always\|never` | Colour the text report. Default `auto`: only on a terminal, honours `NO_COLOR`. |

Progress (`[1/3] resolving graph for build_type=Release on myremote ...`) goes to
stderr; the report itself goes to stdout, so redirecting stdout to a file
gives a clean report.

Exit status: `0` all binaries present, `1` at least one is missing, `2` Conan
failed to build the graph (for example a version conflict). This makes it
usable as a CI gate.

### Example

```
$ conandeps MyProject/conanfile.py -r myremote
conandeps report for MyProject/conanfile.py
remote : myremote
profile: arch=x86_64, compiler=gcc, compiler.libcxx=libstdc++11, compiler.version=14, os=Linux

Dependencies (4) and binary availability

package     via              ctx   Release   Debug     RelWithDebInfo
---------------------------------------------------------------------
MyDepA/1.0  -                host  myremote  myremote  myremote
MyDepB/1.0  -                host  myremote  myremote  myremote
boost/1.8   MyDepA           host  myremote  myremote  myremote
            MyDepB -> Zigma
Zigma/1.0   MyDepB           host  myremote  myremote  MISSING

Missing binaries (1)

package    build type      package_id                                reason
---------------------------------------------------------------------------------------
Zigma/1.0  RelWithDebInfo  3f9c2b7d0e6a4f18c5d9a0b3e7f1c2d4a5b6e21a  no binary anywhere

Overrides (1)

package    wanted     forced to  by              kind
-------------------------------------------------------------------------------------
Zigma/1.0  boost/1.7  boost/1.8  your conanfile  implicit (newer requirement downstream)

Dependency hierarchy

MyProject/conanfile.py
|-- MyDepA/1.0
|   `-- boost/1.8
`-- MyDepB/1.0
    `-- Zigma/1.0  [MISSING: RelWithDebInfo]
        `-- boost/1.8  [overrides boost/1.7 (implicit by your conanfile)]
```

On a terminal the tables are coloured (green = on the remote, red = missing,
yellow = cache-only / overridden version); `--color never` or the `NO_COLOR`
environment variable turns that off, and redirecting stdout to a file never
produces colour codes unless you pass `--color always`. The HTML report uses
the same colour coding.

Reading the table: the `via` column is the shortest path from your conanfile
to the package – `-` for a direct dependency, `MyDepB` for something MyDepB
requires, `MyDepB -> Zigma` for something Zigma requires, and so on. A
package required by several others gets one line per parent (boost above is
pulled in by both MyDepA and Zigma), shortest path first. Rows are sorted
direct-first, then by path, so a branch's transitive dependencies stay
together; the dependency hierarchy section at the end shows the same
information as a tree.


* a remote name (e.g. `myremote`) – the remote has a binary for the package_id your
  profile produces. Hover a cell in the HTML report to see the package_id.
* `not available` – the binary exists only in your local Conan cache; the
  remote does not have it, so a clean machine or CI would fail. Counts as
  missing.
* `MISSING` – no binary anywhere.
* `-` – not applicable (Conan marked the node `Skip` or `Editable`).
* Header-only packages show `ok` everywhere: their package_id ignores
  `build_type`.

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
