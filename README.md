# conan-utils

Small utilities for **Conan 1.x** (targets 1.66). All development happens inside
a pinned Podman container so nothing from the host environment leaks in.

## conandeps.py

Given a `conanfile.py`, `conandeps` reports:

* direct and indirect dependencies (host and build context),
* whether a binary exists on the remote for **Release, Debug and RelWithDebInfo**
  (configurable) for *your exact profile*,
* every requirement **override** – explicit `override=True` and implicit ones
  (a downstream consumer asking for a newer version than a transitive
  dependency declared) – shown both as a list and on the edges of the
  dependency tree.

### Installing with uv

```
uv tool install git+https://<your-forge>/conan-utils   # or: uv tool install . from a checkout
conandeps path/to/conanfile.py -r knor
```

This puts a `conandeps` command on your PATH in its own virtualenv with
Conan 1.66 — it does not touch or depend on the Conan you use for builds
(but it does read the same `~/.conan` config, profiles and remotes).

The script also carries PEP 723 inline metadata, so the single file works on
its own without a checkout:

```
uv run conandeps.py path/to/conanfile.py -r knor
```

Python 3.8–3.11 is required; Conan 1.x does not run on 3.12+.

### Usage

```
./conandeps.py path/to/conanfile.py -r knor                  # text report
./conandeps.py path/to/conanfile.py -r knor -pr myprofile    # explicit profile
./conandeps.py path/to/conanfile.py -r knor --html report.html
./conandeps.py path/to/conanfile.py -r knor --build-types Release,Debug
```

Options mirror `conan info`: `-pr/--profile`, `-s/--settings`, `-o/--options`
(all repeatable) and `-u/--update`. Exit status is `1` when any binary is
missing, so it can gate a CI job.

### How it works

Rather than matching `conan search` output by hand, the dependency graph is
built once per build type through Conan's own `info` code path and each node's
binary status (`Cache`/`Download`/`Missing`, …) is read back. That means
`package_id()` customisations, options and `default_package_id_mode` are
honoured exactly as a real `conan install` would.

Conan 1.x does not keep override information on the graph – the only trace is a
`WARN: … requirement A overridden by B to C` line. The tool captures Conan's
output while building the graph and parses those lines.

## Development

```
./dev.sh build                 # build the conan-utils-dev image (Conan 1.66, Python 3.11)
./dev.sh python -m pytest      # run the tests
./dev.sh ruff check . && ./dev.sh ruff format .
./dev.sh                       # interactive shell
```

The tests start a throw-away `conan_server` inside the container, upload a
small graph with deliberately missing binaries and overrides, and run the tool
against it with an isolated `CONAN_USER_HOME`.
