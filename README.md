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
   exists on the remote for **Release, Debug and RelWithDebInfo** (or any list
   you choose), evaluated for *your exact profile* (compiler, version, libcxx,
   arch, options, …), not "some binary with that build type".
3. **Which requirements were overridden** – both explicit
   `self.requires("x/2.0", override=True)` and implicit ones, where a
   downstream consumer simply asks for a newer version than a transitive
   dependency declared. Each override is listed with who wanted what and who
   forced the change, and annotated on the exact edge of the dependency tree
   where it happened.

### Installation

Requires Python 3.8–3.11 (Conan 1.x does not run on 3.12+). With
[uv](https://docs.astral.sh/uv/):

```bash
# As a command on your PATH, in its own venv with Conan 1.66:
uv tool install git+https://<your-forge>/conan-utils     # or `uv tool install .` from a checkout
conandeps path/to/conanfile.py -r knor

# Or run the single file directly, no checkout needed (PEP 723 inline metadata):
uv run conandeps.py path/to/conanfile.py -r knor
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
| `-r`, `--remote` | Remote to look for binaries in, e.g. `-r knor`. Default: all configured remotes. |
| `-pr`, `--profile` | Profile to evaluate with (repeatable, like `conan -pr`). Default: your default profile. |
| `-s`, `-o` | Extra settings / options, repeatable, same syntax as `conan install`. |
| `--build-types` | Comma-separated build types to check. Default: `Release,Debug,RelWithDebInfo`. |
| `-u`, `--update` | Ask the remote for newer recipes/binaries (`conan -u`). |
| `--html FILE` | Also write a self-contained HTML report (no scripts, no external assets). |
| `-v` | Echo Conan's own output to stderr. |

Exit status: `0` all binaries present, `1` at least one is missing, `2` Conan
failed to build the graph (for example a version conflict). This makes it
usable as a CI gate.

### Example

```
$ conandeps MyProject/conanfile.py -r knor
conandeps report for MyProject/conanfile.py
remote : knor
profile: arch=x86_64, compiler=gcc, compiler.libcxx=libstdc++11, compiler.version=14, os=Linux

Dependencies (4) and binary availability

package      kind    ctx   Release         Debug           RelWithDebInfo
-------------------------------------------------------------------------
MyDepA/1.0   direct  host  ok              ok              ok
boost/1.8    indir.  host  ok              ok              ok
MyDepB/1.0   direct  host  ok              ok              ok
Zigma/1.0    indir.  host  ok              ok              MISSING

MISSING BINARIES (1 packages)
  Zigma/1.0: missing RelWithDebInfo
      RelWithDebInfo package_id 3f9c...e21a

Overrides (1)
  Zigma/1.0 wanted boost/1.7
      -> forced to boost/1.8 by your conanfile  [implicit (newer direct requirement)]

Dependency hierarchy
MyProject/conanfile.py
|-- MyDepA/1.0
|   `-- boost/1.8
`-- MyDepB/1.0
    `-- Zigma/1.0  [MISSING: RelWithDebInfo]
        `-- boost/1.8  [overrides boost/1.7 (implicit by your conanfile)]
```

Reading the table:

* `ok` – a binary for your profile exists (in the remote, or already in your
  cache).
* `MISSING` – no binary for the package_id your profile produces. Hover the
  cell in the HTML report to see the package_id.
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
honoured exactly as a real install would.

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
