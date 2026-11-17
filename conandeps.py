#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = ["conan>=1.66,<2"]
# ///
"""conandeps - dependency, binary-availability and override report for Conan 1.x.

Given a conanfile.py this tool:

  * lists the direct and indirect dependencies (host and build context),
  * checks, for every dependency, whether a binary package exists on the
    chosen remote for each build_type we care about (Release, Debug and
    RelWithDebInfo by default) using the *exact* profile you would build with,
  * accepts a fallback map (``--fallbacks``) for packages that are simply
    not built for one of those build types - "check zlib's Release binary
    when asked about RelWithDebInfo" - so a known and accepted gap stops
    being reported as a missing binary,
  * reports every requirement override - explicit ``override=True`` ones as
    well as implicit ones where a downstream consumer simply asks for a newer
    version than a transitive dependency declared - showing who asked for
    what and which version won,
  * checks whether the remote holds a newer version of each package (same
    user/channel).  Versions are ordered with Conan's lenient ``Version``
    (which copes with dates and most non-semver schemes); because some
    schemes do not order at all, the export timestamps from the recipe
    manifests of the current and newest reference are fetched too, shown in
    the report, and used to catch a reference that is newer only by date.
    Informational only - never affects the exit code.  Disable with
    ``--no-check-updates``.
  * derives the order in which those packages have to be updated for the root
    to end up on the newest references: a consumer of an outdated package
    needs a re-release even when it is at its newest version itself, so the
    plan is grouped into waves that can each be done in parallel, deepest
    first, with your conanfile last.

How it works
------------
Instead of hand-matching ``conan search`` output against the profile we ask
Conan itself: the dependency graph is built once per build_type through the
same ``conan info`` code path that ``conan install`` uses, and every node's
``binary`` status (Cache/Download/Update/Missing/...) is read back.  This
honours ``package_id()`` customisations, options and the configured
``default_package_id_mode`` exactly the way a real install would.

Build-type fallbacks use the same route: a fallback is passed to Conan as a
package-scoped setting (``zlib:build_type=Release``), exactly what you would
put in the profile when installing, so the package_id is Conan's, not ours,
and the effect on the packages that depend on it is whatever the configured
``default_package_id_mode`` says it is.

Conan 1.x does not record overrides on the graph; the only trace is a warning
emitted by ``Requirements.update()``.  We therefore capture Conan's output
stream while the graph is built and parse those lines.

The terminal output is a summary - the header, one line of counts and the
missing binaries.  The full report (dependency table with availability and
newest versions, overrides, update order, tree) is the HTML one written by
``--html``.

Targets Conan 1.66.  Uses only the standard library plus the Conan API.
"""

from __future__ import annotations

import argparse
import configparser
import html
import io
import os
import re
import sys
import time
import warnings
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

# Conan 1.x predates the stricter escape-sequence checks of recent Pythons and
# emits a screenful of SyntaxWarning/DeprecationWarning from its own modules
# (patch_ng.py, model/ref.py, ...) when they are byte-compiled.  They are not
# actionable for the user of this tool, so silence them before Conan is
# imported (imports of ``conans`` are deliberately deferred to _make_api()).
warnings.filterwarnings("ignore", category=SyntaxWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

DEFAULT_BUILD_TYPES = ("Release", "Debug", "RelWithDebInfo")

# Conan 1.x: "<pkg>: requirement <old> overridden by <who> to <new> " where
# <who> is either a reference or the literal "your conanfile".
_OVERRIDE_RE = re.compile(
    r"requirement (?P<old>\S+) overridden by (?P<by>your conanfile|\S+) to (?P<new>\S+)"
)

# Binary states, after our post-processing of Conan's node.binary
# (see conans/client/graph/graph.py for Conan's own set):
#   Remote    - the remote has the binary (Conan said Download/Update, or said
#               Cache and we confirmed the remote has the same package_id)
#   CacheOnly - only the local cache has it; a clean machine would fail
#   Missing   - nowhere
_AVAILABLE = {"Remote"}
_PROBLEM = {"Missing", "CacheOnly"}
# States that mean "does not apply", not "missing".
_NOT_APPLICABLE = {"Skip", "Editable"}


@dataclass
class Dep:
    """One package in the graph plus its binary status per build_type."""

    ref: str
    context: str  # "host" or "build"
    direct: bool = False
    # Shortest path length from the consumer: 1 = direct, 2 = required by a
    # direct dependency, ... Used to order the table by level of indirection.
    depth: int = 0
    # Package names on the shortest path from the consumer to this package,
    # excluding the package itself: [] for direct, ["A"] for A -> pkg, ...
    path: List[str] = field(default_factory=list)
    # Names of every package that directly requires this one (all paths).
    parents: List[str] = field(default_factory=list)
    # One path per direct parent: shortest path to that parent plus the
    # parent itself.  Bounded by the number of parents, unlike "all paths".
    paths: List[List[str]] = field(default_factory=list)
    build_require: bool = False
    requires: List[str] = field(default_factory=list)  # names of its own requirements
    settings: Dict[str, str] = field(default_factory=dict)
    # build_type -> (status, package_id, remote_name)
    binaries: Dict[str, Tuple[str, str, Optional[str]]] = field(default_factory=dict)
    # build_type -> build type actually resolved for it (see --fallbacks).
    # Only set when Conan really applied the fallback, so it can be spelled
    # out wherever the binary is reported.
    fallbacks: Dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.ref.split("/", 1)[0]

    def missing(self) -> List[str]:
        """Build types without a binary on the remote (cache-only counts as missing)."""
        return [bt for bt, (status, _, _) in self.binaries.items() if status in _PROBLEM]


@dataclass
class Override:
    """A requirement that was changed by a downstream consumer."""

    package: str  # who had its requirement rewritten (or "your conanfile")
    old: str
    new: str
    by: str  # who forced it ("your conanfile" for the root)
    explicit: bool  # True when `by` declared it with override=True


@dataclass
class Update:
    """A package for which the remote holds a newer reference."""

    ref: str  # currently resolved reference
    latest: str  # newest reference on the remote
    remote: str  # remote that has ``latest``
    current_time: Optional[int]  # export timestamp (epoch) of ``ref``'s recipe
    latest_time: Optional[int]  # export timestamp of ``latest``'s recipe
    note: str  # how "newer" was decided (see _update_note)


@dataclass
class Step:
    """One package in the update order (see ``_update_order``)."""

    ref: str  # package reference, or the conanfile itself for the last step
    wave: int  # 1-based; every step in a wave can be done in parallel
    action: str  # what to do, spelled out
    latest: Optional[str]  # newer version to move to, None when only re-released
    blocked_by: List[str]  # requirement names that have to be updated first


@dataclass
class Report:
    conanfile: str
    remote: Optional[str]
    profile: Dict[str, str]
    build_types: List[str]
    deps: "OrderedDict[str, Dep]"  # keyed by "<name>#<context>"
    overrides: List[Override]
    ranges: Dict[str, str]  # resolved ref -> declared range/ref
    root_requires: List[str]
    updates: List[Update] = field(default_factory=list)
    checked_updates: bool = True  # False when --no-check-updates skipped the check
    # build_type -> {package name: build type checked instead} (--fallbacks)
    fallbacks: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # "name/version" of the conanfile itself, when it declares them
    package: Optional[str] = None

    def problems(self) -> List[Dep]:
        return [d for d in self.deps.values() if d.missing()]


# --------------------------------------------------------------------------- #
# Graph collection
# --------------------------------------------------------------------------- #
class _Tee(io.TextIOBase):
    """Capture Conan's output in memory and optionally echo it live to stderr.

    The in-memory copy is parsed for override warnings afterwards; the live
    echo (``--verbose``) lets the user watch recipe downloads and remote
    queries while a slow graph resolution is running.
    """

    def __init__(self, echo: bool):
        self.buffer_ = io.StringIO()
        self.echo = echo

    def write(self, data: str) -> int:
        self.buffer_.write(data)
        if self.echo:
            sys.stderr.write(data)
            sys.stderr.flush()
        return len(data)

    def getvalue(self) -> str:
        return self.buffer_.getvalue()


def _progress(quiet: bool, msg: str) -> None:
    """Status line on stderr so stdout stays a clean, parseable report."""
    if not quiet:
        sys.stderr.write("conandeps: %s\n" % msg)
        sys.stderr.flush()


def _make_api(log: _Tee):
    """Create a Conan API that writes everything to ``log`` instead of stdout.

    Colour is disabled so the override warnings can be parsed reliably.
    """
    from conans.client.conan_api import ConanAPIV1
    from conans.client.output import ConanOutput

    return ConanAPIV1(output=ConanOutput(log, log, color=False))


def _resolve_graph(
    api,
    conanfile: str,
    remote: Optional[str],
    profiles: List[str],
    settings: List[str],
    options: List[str],
    update: bool,
):
    """Build the dependency graph the way ``conan install`` would.

    Returns ``(graph, root conanfile)``.  The one place every tool in this
    repository asks Conan for a graph, so ``-pr``/``-s``/``-o``/``-r`` mean
    the same thing everywhere.
    """
    return api.info(
        conanfile,
        remote_name=remote,
        settings=list(settings),
        options=list(options) or None,
        profile_names=profiles or None,
        update=update,
    )


def _node_key(node) -> str:
    return "%s#%s" % (node.ref.name, node.context)


def _key_of(dep: Dep) -> str:
    return "%s#%s" % (dep.name, dep.context)


def _root_ref(conanfile) -> Optional[str]:
    """The conanfile's own "name/version", when it declares them.

    A consumer conanfile.py need not name or version itself; Conan leaves both
    None then and the report simply has no package line.
    """
    name = getattr(conanfile, "name", None)
    version = getattr(conanfile, "version", None)
    if not name:
        return None
    return "%s/%s" % (name, version) if version else name


def _explicit_overrides(graph) -> set:
    """Set of (declaring ref or 'your conanfile', package name) for override=True requires.

    Conan names the overrider in its WARN line with the downstream node's own
    reference, and only falls back to "your conanfile" for a consumer that does
    not name and version itself.  The same string has to be built here, or an
    explicit override by a named conanfile is read back as an implicit one.
    """
    result = set()
    for node in graph.nodes:
        who = str(node.ref) if node.ref is not None and node.ref.name else "your conanfile"
        for req in node.conanfile.requires.values():
            if req.override:
                result.add((who, req.ref.name))
    return result


def _parse_overrides(text: str, explicit: set) -> List[Override]:
    seen = set()
    result = []
    for line in text.splitlines():
        m = _OVERRIDE_RE.search(line)
        if not m:
            continue
        # Line looks like "pkg/1.0: WARN: pkg/1.0: requirement ..."; the package
        # is the token right before "requirement".
        head = line[: m.start()].rstrip(": ")
        package = head.split(":")[-1].strip() or "your conanfile"
        key = (package, m["old"], m["new"], m["by"])
        if key in seen:
            continue
        seen.add(key)
        name = m["old"].split("/", 1)[0]
        result.append(
            Override(package, m["old"], m["new"], m["by"], explicit=(m["by"], name) in explicit)
        )
    return result


class _RemoteIndex:
    """Answers "does remote X have package_id Y of ref Z?" with one search per ref.

    Conan reports ``Cache`` for a binary it already has locally without ever
    asking the remote.  The question this tool answers is whether the *remote*
    has it, so cache hits are verified here via ``conan search`` on the remote
    (or all remotes when none was given).
    """

    def __init__(self, api, remote: Optional[str]):
        self._api = api
        self._remote = remote or "all"
        self._cache: Dict[str, Dict[str, str]] = {}  # ref -> {package_id: remote_name}

    def remote_for(self, ref: str, package_id: str) -> Optional[str]:
        if ref not in self._cache:
            self._cache[ref] = self._search(ref)
        return self._cache[ref].get(package_id)

    def _search(self, ref: str) -> Dict[str, str]:
        from conans.errors import ConanException

        found: Dict[str, str] = {}
        try:
            info = self._api.search_packages(ref, remote_name=self._remote)
        except ConanException:
            return found  # recipe not on the remote at all
        for remote_info in info.get("results", []):
            for item in remote_info.get("items", []):
                for pkg in item.get("packages", []):
                    found.setdefault(pkg["id"], remote_info["remote"])
        return found


def _pick_latest(current: str, versions: List[str]) -> Optional[Tuple[str, bool]]:
    """Version-wise newest candidate, or None when nothing beats ``current``.

    Uses Conan's lenient ``Version`` ordering, which copes with most schemes
    (1.2.3, 2024.01, cci.20240101, ...).  Returns (latest, strictly_newer);
    ``strictly_newer`` is False when the two merely differ textually (1.2 vs
    1.2.0 compare equal) - the export timestamps decide such a tie.
    """
    from conans.model.version import Version

    others = [v for v in versions if v != current]
    if not others:
        return None
    latest = max(others, key=Version)
    if Version(latest) < Version(current):
        return None  # every other version on the remote is older
    return latest, Version(latest) > Version(current)


def _update_note(version_newer: bool, current_time: Optional[int], latest_time: Optional[int]):
    """Say how "newer" was decided, so ambiguous version schemes stay honest.

    A re-export refreshes a recipe's manifest timestamp, so neither signal is
    trusted blindly: a version-wise newer reference wins, but a timestamp that
    contradicts it is called out for the reader to judge.
    """
    if not version_newer:
        return "newer export date only"
    if current_time is not None and latest_time is not None and latest_time < current_time:
        return "newer version, but exported before current"
    return "newer version"


class _UpdateChecker:
    """Finds newer references of each resolved package on the remote.

    One recipe search per package lists every reference; candidates sharing
    the current reference's user/channel are ordered by ``_pick_latest``.
    When a candidate differs, the recipe manifests of the current and newest
    reference are fetched (two extra requests) for their export timestamps -
    shown in the report and used by ``_update_note``.  A manifest that cannot
    be fetched renders as "?", never as a guess.
    """

    def __init__(self, api, remote: Optional[str], quiet: bool):
        self._api = api
        self._remote = remote or "all"
        self._quiet = quiet

    def check(self, refs: List[str]) -> List[Update]:
        return [u for u in (self._check_one(r) for r in refs) if u]

    def _check_one(self, ref: str) -> Optional[Update]:
        from conans.errors import ConanException
        from conans.model.ref import ConanFileReference

        current = ConanFileReference.loads(ref)
        # version -> (full ref, remote name); first remote to report a version wins.
        candidates: Dict[str, Tuple[str, str]] = {}
        try:
            info = self._api.search_recipes("%s/*" % current.name, remote_name=self._remote)
        except ConanException as exc:
            _progress(self._quiet, "WARN: version search for %s failed: %s" % (ref, exc))
            return None
        for remote_info in info.get("results", []):
            for item in remote_info.get("items", []):
                try:
                    found = ConanFileReference.loads(item["recipe"]["id"])
                except ConanException:
                    continue
                same = (found.name, found.user, found.channel) == (
                    current.name,
                    current.user,
                    current.channel,
                )
                if same:
                    candidates.setdefault(str(found.version), (str(found), remote_info["remote"]))
        picked = _pick_latest(str(current.version), list(candidates))
        if not picked:
            return None
        latest_version, version_newer = picked
        latest_ref, latest_remote = candidates[latest_version]
        mine = candidates.get(str(current.version))
        current_time = self._export_time(ref, mine[1]) if mine else None
        latest_time = self._export_time(latest_ref, latest_remote)
        newer_by_time = (
            current_time is not None and latest_time is not None and latest_time > current_time
        )
        if not version_newer and not newer_by_time:
            return None
        note = _update_note(version_newer, current_time, latest_time)
        return Update(ref, latest_ref, latest_remote, current_time, latest_time, note)

    def _export_time(self, ref: str, remote_name: str) -> Optional[int]:
        """Export timestamp from the recipe's conanmanifest.txt, or None."""
        from conans.errors import ConanException
        from conans.model.ref import ConanFileReference

        try:
            remote = self._api.app.cache.registry.load_remotes()[remote_name]
            manifest, _ = self._api.app.remote_manager.get_recipe_manifest(
                ConanFileReference.loads(ref), remote
            )
            return int(manifest.time)
        except (ConanException, KeyError, TypeError, ValueError):
            return None


def _paths(root) -> Dict[str, List[str]]:
    """Breadth-first shortest path (list of package names) from the root to every node.

    The path excludes the node itself, so direct dependencies get [].  Among
    equally short paths the first one found in graph order wins; the other
    parents are still reported through ``Dep.parents``.
    """
    paths: Dict[str, List[str]] = {}
    frontier = [(e.dst, []) for e in root.dependencies]
    while frontier:
        next_frontier = []
        for node, path in frontier:
            key = _node_key(node)
            if key in paths:
                continue
            paths[key] = path
            next_frontier.extend((e.dst, path + [node.ref.name]) for e in node.dependencies)
        frontier = next_frontier
    return paths


def _load_fallbacks(path: str) -> Dict[str, Dict[str, str]]:
    """Read the ``--fallbacks`` file: one INI section per build type.

    Some packages are simply never built for every build type.  The file says
    which build type to check instead, per package::

        [RelWithDebInfo]
        zlib = Release
        openssl = Release

        [Debug]
        icu = Release

    Returns ``{build_type: {package name: build type to check instead}}``.
    Package names keep their case (Conan references are case sensitive), and a
    package mapped to its own section is rejected: it would silently do
    nothing, which is never what the line was meant to say.
    """
    parser = configparser.ConfigParser()
    parser.optionxform = str  # do not lower-case package names
    with open(path, encoding="utf-8") as fh:
        parser.read_file(fh)
    fallbacks: Dict[str, Dict[str, str]] = {}
    for build_type in parser.sections():
        for package, fallback in parser.items(build_type):
            if not fallback or fallback == build_type:
                raise ValueError(
                    "%s: [%s] %s = %s: expected a different build type"
                    % (path, build_type, package, fallback)
                )
            fallbacks.setdefault(build_type, {})[package] = fallback
    return fallbacks


def _collect(
    graph,
    build_type: str,
    deps: "OrderedDict[str, Dep]",
    ranges: Dict[str, str],
    index: _RemoteIndex,
    fallbacks: Dict[str, str],
):
    """Merge one build_type's graph into ``deps``.

    The graph may differ between build types (conditional requirements), so we
    union nodes rather than assume the first graph is complete.

    ``fallbacks`` maps a package name to the build type asked for instead of
    ``build_type``; it is recorded on the ``Dep`` only when the node's settings
    confirm Conan applied it, so the report never claims a fallback that did
    not happen (a header-only package has no build_type to override).
    """
    root = graph.root
    direct = {_node_key(e.dst) for e in root.dependencies}
    paths = _paths(root)
    for node in graph.nodes:
        if node is root:
            continue
        key = _node_key(node)
        dep = deps.get(key)
        if dep is None:
            dep = Dep(ref=str(node.ref), context=node.context)
            deps[key] = dep
        dep.direct = dep.direct or key in direct
        path = paths.get(key, [])
        depth = len(path) + 1
        if not dep.depth or depth < dep.depth:
            dep.depth, dep.path = depth, path
        for edge in node.dependants:
            if edge.src is root:
                parent, via = "your conanfile", []
            else:
                parent = edge.src.ref.name
                via = paths.get(_node_key(edge.src), []) + [parent]
            if parent not in dep.parents:
                dep.parents.append(parent)
                dep.paths.append(via)
        dep.build_require = dep.build_require or any(e.build_require for e in node.dependants)
        for edge in node.dependencies:
            name = edge.dst.ref.name
            if name not in dep.requires:
                dep.requires.append(name)
        for req in node.conanfile.requires.values():
            # range_ref also differs from ref after an override, so only
            # record genuine "[...]" version ranges here.
            if req.version_range:
                ranges[str(req.ref)] = str(req.range_ref)
        try:
            dep.settings = {k: str(v) for k, v in node.conanfile.settings.values_list}
        except Exception:  # header-only / no settings
            dep.settings = {}
        fallback = fallbacks.get(node.ref.name)
        if fallback and dep.settings.get("build_type") == fallback:
            dep.fallbacks[build_type] = fallback
        status = node.binary or "Unknown"
        package_id = node.package_id or ""
        remote = node.binary_remote.name if node.binary_remote else None
        if status in _NOT_APPLICABLE:
            status = "n/a"
        elif status in ("Download", "Update"):
            status = "Remote"
        elif status == "Cache":
            remote = index.remote_for(str(node.ref), package_id)
            status = "Remote" if remote else "CacheOnly"
        dep.binaries[build_type] = (status, package_id, remote)


def build_report(
    conanfile: str,
    remote: Optional[str],
    profiles: List[str],
    settings: List[str],
    options: List[str],
    build_types: List[str],
    update: bool,
    verbose: bool = False,
    quiet: bool = False,
    check_updates: bool = True,
    fallbacks: Optional[Dict[str, Dict[str, str]]] = None,
) -> Report:
    log = _Tee(echo=verbose)
    _progress(quiet, "loading Conan API")
    api = _make_api(log)
    deps: "OrderedDict[str, Dep]" = OrderedDict()
    ranges: Dict[str, str] = {}
    overrides: List[Override] = []
    root_requires: List[str] = []
    profile_settings: Dict[str, str] = {}
    package: Optional[str] = None
    explicit: set = set()
    index = _RemoteIndex(api, remote)
    fallbacks = fallbacks or {}
    # A section for a build type that is not being checked is dead config,
    # almost always a typo - say so rather than quietly ignoring it.
    unused = sorted(set(fallbacks) - set(build_types))
    if unused:
        _progress(
            quiet, "WARN: fallbacks for build types that are not checked: %s" % ", ".join(unused)
        )

    total = len(build_types)
    for i, bt in enumerate(build_types, 1):
        _progress(
            quiet,
            "[%d/%d] resolving graph for build_type=%s on %s ..."
            % (i, total, bt, remote or "all remotes"),
        )
        t0 = time.monotonic()
        # A fallback is just a package-scoped setting, the same thing you would
        # write in a profile, so Conan computes the package_id for it.
        scoped = ["%s:build_type=%s" % (p, f) for p, f in sorted(fallbacks.get(bt, {}).items())]
        graph, root_conanfile = _resolve_graph(
            api,
            conanfile,
            remote,
            profiles,
            list(settings) + ["build_type=%s" % bt] + scoped,
            options,
            update,
        )
        explicit |= _explicit_overrides(graph)
        _collect(graph, bt, deps, ranges, index, fallbacks.get(bt, {}))
        missing = sum(1 for d in deps.values() if d.binaries.get(bt, ("",))[0] in _PROBLEM)
        _progress(
            quiet,
            "[%d/%d] %s: %d packages, %d missing binaries (%.1fs)"
            % (i, total, bt, len(graph.nodes) - 1, missing, time.monotonic() - t0),
        )
        package = package or _root_ref(root_conanfile)
        if not root_requires:
            root_requires = [str(r.ref) for r in root_conanfile.requires.values()]
            profile_settings = {
                k: str(v) for k, v in root_conanfile.settings.values_list if k != "build_type"
            }

    # The same override is warned once per graph build; parse once, de-duplicated.
    overrides = _parse_overrides(log.getvalue(), explicit)
    _progress(quiet, "%d overrides detected" % len(overrides))

    updates: List[Update] = []
    if check_updates:
        _progress(quiet, "checking for newer versions on %s ..." % (remote or "all remotes"))
        updates = _UpdateChecker(api, remote, quiet).check(sorted({d.ref for d in deps.values()}))
        _progress(quiet, "%d packages have a newer version" % len(updates))

    # Direct dependencies first, then by increasing level of indirection,
    # alphabetically within a level.  Build-context nodes sort after host.
    ordered = sorted(
        deps.values(),
        key=lambda d: (d.depth, [p.lower() for p in d.path], d.context != "host", d.name.lower()),
    )
    deps = OrderedDict((_key_of(d), d) for d in ordered)
    for dep in deps.values():  # keep column order stable
        dep.binaries = OrderedDict(
            (bt, dep.binaries.get(bt, ("n/a", "", None))) for bt in build_types
        )
    return Report(
        conanfile,
        remote,
        profile_settings,
        list(build_types),
        deps,
        overrides,
        ranges,
        root_requires,
        updates,
        check_updates,
        fallbacks,
        package,
    )


# --------------------------------------------------------------------------- #
# Text rendering
# --------------------------------------------------------------------------- #
_MARK = {
    "CacheOnly": "local cache only",
    "Missing": "MISSING",
    "Build": "build",
    "n/a": "-",
    "Unknown": "?",
    "Invalid": "invalid",
}


@dataclass
class _TreeNode:
    """One line of the dependency hierarchy (see ``_tree``)."""

    label: str  # the reference, or the conanfile's package/path for the root
    name: str  # what a click matches on: every node with this name is outlined
    # "missing" / "cacheonly" when some build type has that problem, else None.
    status: Optional[str] = None
    # Binary status per build type, in the table cells' words (the tooltip).
    tip: str = ""
    note: str = ""  # "cycle" / "not in graph"
    children: List["_TreeNode"] = field(default_factory=list)


def _tree_status(dep: Dep) -> Optional[str]:
    """The tree's problem marking: missing wins over cache-only (it is worse).

    Which build types are affected is left to the dependency table.
    """
    statuses = {s for s, _, _ in dep.binaries.values()}
    for status, cls in (("Missing", "missing"), ("CacheOnly", "cacheonly")):
        if status in statuses:
            return cls
    return None


def _tree(report: Report) -> _TreeNode:
    """Build the dependency hierarchy as nested nodes.

    The annotations are the problem marking (missing / local cache only),
    which the user asked for so problems stand out in the hierarchy, and a
    tooltip with the binary status per build type on *every* package: with
    one only on problem packages, hovering an available one showed nothing
    and read as broken.  Paths, context, overrides and version ranges stay
    in the report's tables.  Names resolve host-context-first, and a cycle
    stops the walk.
    """
    by_name = {}
    for dep in report.deps.values():
        by_name.setdefault(dep.name, dep)  # host first (insertion order), build ok as fallback

    def walk(name: str, seen: Tuple[str, ...]) -> _TreeNode:
        dep = by_name.get(name)
        if dep is None:
            return _TreeNode(name, name, note="not in graph")
        # binaries[bt] is (status, package_id, remote), _cell's first arguments.
        tip = "; ".join(
            "%s: %s" % (bt, _cell(*dep.binaries[bt], _Palette(False), dep.fallbacks.get(bt)))
            for bt in report.build_types
        )
        node = _TreeNode(dep.ref, name, _tree_status(dep), tip)
        if name in seen:
            node.note = "cycle"
            return node
        node.children = [walk(child, seen + (name,)) for child in dep.requires]
        return node

    # The root is named like the rest of the report: by the package the
    # conanfile declares, or by its path when it declares neither.
    label = report.package or report.conanfile
    root = _TreeNode(label, label)
    root.children = [walk(d.name, ()) for d in report.deps.values() if d.direct]
    return root


def _update_order(report: Report) -> List[Step]:
    """The order in which packages must be updated for the root to be newest.

    A package is in the plan when the remote has a newer version of it, and
    also when one of its requirements has one: a consumer must be re-released
    against the new reference for it to reach your conanfile, even when the
    consumer itself is at its newest version.  That set is grown to a fixpoint
    rather than by one upward walk, so a package reached late still pulls its
    own consumers in.

    A step's wave is one past the deepest requirement it waits for, so every
    step in a wave can be done in parallel and your conanfile always comes
    last.  Names are resolved host-context-first exactly like ``_tree``,
    and a cycle (which Conan rejects, but the renderers defend against
    anyway) stops the walk instead of recursing forever.
    """
    newest = {u.ref: u for u in report.updates}
    by_name: Dict[str, Dep] = {}
    for dep in report.deps.values():
        by_name.setdefault(dep.name, dep)  # host first (insertion order)

    affected = {name for name, dep in by_name.items() if dep.ref in newest}
    changed = bool(affected)
    while changed:
        changed = False
        for name, dep in by_name.items():
            if name not in affected and any(r in affected for r in dep.requires):
                affected.add(name)
                changed = True
    if not affected:
        return []

    waves: Dict[str, int] = {}

    def wave_of(name: str, seen: Tuple[str, ...]) -> int:
        if name in waves:
            return waves[name]
        if name in seen:
            return 0  # cycle: do not recurse, the wave of the entry point wins
        blockers = _blockers(by_name[name].requires, affected)
        waves[name] = 1 + max([wave_of(b, seen + (name,)) for b in blockers] or [0])
        return waves[name]

    steps = []
    for name in sorted(affected):
        dep = by_name[name]
        update = newest.get(dep.ref)
        action = "update to %s" % _newest(update) if update else "re-release"
        blocked = _blockers(dep.requires, affected)
        steps.append(
            Step(dep.ref, wave_of(name, ()), action, update.latest if update else None, blocked)
        )
    # The root is not a graph node: it always comes last, and its blockers are
    # the direct requirements that moved.
    direct = [d.name for d in report.deps.values() if d.direct]
    steps.append(
        Step(
            report.package or report.conanfile,  # as in _tree
            max(s.wave for s in steps) + 1,
            "bump requirements",
            None,
            _blockers(direct, affected),
        )
    )
    return sorted(steps, key=lambda s: (s.wave, s.ref.lower()))


def _blockers(requires: List[str], affected: Set[str]) -> List[str]:
    """The requirement names in ``affected``, de-duplicated and sorted."""
    return sorted({r for r in requires if r in affected}, key=str.lower)


class _Palette:
    """ANSI colours for the terminal report; a no-op when disabled.

    Colour is a readability aid only: every state is also spelled out in
    words, so a colour-blind reader or a log file loses nothing.
    """

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return "\033[%sm%s\033[0m" % (code, text) if self.enabled else text

    def ok(self, text: str) -> str:
        return self._wrap("32", text)  # green

    def bad(self, text: str) -> str:
        return self._wrap("1;31", text)  # bold red

    def warn(self, text: str) -> str:
        return self._wrap("33", text)  # yellow

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def _table(headers: List[str], rows: List[List[str]], pal: _Palette) -> List[str]:
    """Render a column-aligned text table.

    Cells may contain ANSI colour codes (alignment uses the visible width) and
    newlines: a multi-line cell spreads the row over several lines, with the
    other columns left blank on the continuation lines.
    """
    split_rows = [[c.split("\n") for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in split_rows:
        for i, cell_lines in enumerate(row):
            widths[i] = max([widths[i]] + [_visible_len(line) for line in cell_lines])

    def fmt(cells: List[str]) -> str:
        padded = [c + " " * (widths[i] - _visible_len(c)) for i, c in enumerate(cells)]
        return "  ".join(padded).rstrip()

    lines = [pal.bold(fmt(headers)), "-" * (sum(widths) + 2 * (len(widths) - 1))]
    for row in split_rows:
        height = max(len(cell_lines) for cell_lines in row)
        for n in range(height):
            lines.append(fmt([cell[n] if n < len(cell) else "" for cell in row]))
    return lines


def _cell(
    status: str,
    _package_id: str,
    remote: Optional[str],
    pal: _Palette,
    fallback: Optional[str] = None,
) -> str:
    """Cell text: the remote that has the binary, or why it is not available.

    ``fallback`` is the build type that was actually resolved for this package
    (see ``--fallbacks``).  It is always appended, for a found binary as well
    as a missing one, so a cell can never be read as a binary for the column's
    own build type.
    """
    if status == "Remote":
        text = pal.ok(remote or "remote")
    elif status == "Missing":
        text = pal.bad(_MARK[status])
    elif status == "CacheOnly":
        text = pal.warn(_MARK[status])
    else:
        text = pal.dim(_MARK.get(status, status))
    return (text + pal.dim(" (via %s)" % fallback)) if fallback else text


def _reason(status: str) -> str:
    """Why a binary counts as missing; the same words in both renderers."""
    return _MARK["CacheOnly"] if status == "CacheOnly" else "no binary anywhere"


def _override_kind(o: Override) -> str:
    return "explicit (override=True)" if o.explicit else "implicit (newer requirement downstream)"


def _date(ts: Optional[int]) -> str:
    """Export timestamp as a UTC date, '?' when the manifest could not be read."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "?"


def _resolved_bt(dep: Dep, build_type: str) -> str:
    """Label a build type with the fallback that was checked instead, if any."""
    fallback = dep.fallbacks.get(build_type)
    return "%s (via %s)" % (build_type, fallback) if fallback else build_type


def _fallback_summary(fallbacks: Dict[str, Dict[str, str]]) -> str:
    """The configured fallbacks on one line, for the report header."""
    return "; ".join(
        "%s: %s" % (bt, ", ".join("%s -> %s" % (p, f) for p, f in sorted(pkgs.items())))
        for bt, pkgs in sorted(fallbacks.items())
    )


def _via(dep: Dep, arrow: str = " -> ", newline: str = "\n") -> str:
    """How the package is reached: '-' for direct, otherwise one line per path.

    ``A -> B`` means consumer -> A -> B -> pkg.  A package required by several
    others gets one line per parent (shortest path to that parent), shortest
    first.
    """
    if dep.direct:
        return "-"
    paths = sorted(dep.paths, key=lambda p: (len(p), [x.lower() for x in p]))
    return newline.join(arrow.join(p) for p in paths)


# Render-order keys shared by the text and HTML renderers.  Dependency rows
# group by their via chain (direct deps' "-" sorts before letters, so they
# stay on top); the overrides and missing-binaries tables go alphabetical.
# report.deps itself keeps the depth-based graph order.
def _dep_key(dep: Dep):
    return (_via(dep).lower(), dep.name.lower())


def _override_key(o: Override):
    return (o.package.lower(), o.old.lower())


def _plural(n: int, singular: str, plural: Optional[str] = None) -> str:
    return "%d %s" % (n, singular if n == 1 else (plural or singular + "s"))


def _summary(report: Report) -> str:
    """The one-line count under the text report's header."""
    parts = [
        _plural(len(report.deps), "dependency", "dependencies"),
        _plural(len(report.overrides), "override"),
        _plural(len(report.updates), "newer version")
        if report.checked_updates
        else "newer versions not checked",
    ]
    return ", ".join(parts)


def _header(report: Report, with_conanfile: bool = False) -> List[Tuple[str, str]]:
    """The report header as label/value pairs, in order, skipping what is unset.

    Shared by both renderers so they never drift apart.  The text report puts
    the conanfile path in its title line instead, so that pair is optional.
    """
    pairs = [("package", report.package)]
    if with_conanfile:
        pairs.append(("conanfile", report.conanfile))
    pairs += [
        ("remote", report.remote or "(all configured remotes)"),
        ("profile", ", ".join("%s=%s" % kv for kv in report.profile.items())),
        ("fallbacks", _fallback_summary(report.fallbacks) if report.fallbacks else None),
    ]
    return [(label, value) for label, value in pairs if value]


def render_text(report: Report, color: bool = False) -> str:
    """The terminal report: the header, a one-line count and the problems.

    Everything else - the dependency table with each package's newest
    version, the overrides, the update order and the tree - is in the HTML
    report, which is where the detail is meant to be read.  What is left is
    short enough to scan in a CI log, where the only question is what is
    missing, and it matches the exit code exactly.
    """
    pal = _Palette(color)
    out: List[str] = []
    w = out.append
    w(pal.bold("conandeps report for %s" % report.conanfile))
    # Labels are padded to the longest one there is, so the header reads as a
    # block whether or not the optional lines are present.
    for label, value in _header(report):
        w("%-9s: %s" % (label, value))
    w("")
    w(_summary(report))
    w("")

    problems = report.problems()
    n_missing = sum(len(d.missing()) for d in problems)
    w(pal.bold("Missing binaries (%d)" % n_missing))
    w("")
    if problems:
        # No package_id column: it is one more thing to skip past here, and
        # the HTML table carries it as a tooltip anyway.
        rows = []
        for dep in sorted(problems, key=lambda d: d.ref.lower()):
            for bt in dep.missing():
                status = dep.binaries[bt][0]
                colour = pal.warn if status == "CacheOnly" else pal.bad
                rows.append([dep.ref, _resolved_bt(dep, bt), colour(_reason(status))])
        out.extend(_table(["package", "build type", "reason"], rows, pal))
    else:
        w(pal.ok("All packages have binaries on the remote for: " + ", ".join(report.build_types)))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# HTML rendering (self-contained, no external assets)
# --------------------------------------------------------------------------- #
_CSS = """
body{font:14px/1.4 system-ui,sans-serif;margin:2rem;color:#1b1b1b;background:#fff}
h1,h2{font-weight:600}table{border-collapse:collapse;margin:1rem 0}
th,td{border:1px solid #bbb;padding:.3rem .6rem;text-align:left;vertical-align:top}
th{background:#eee}code{font-size:.95em}
.ok{color:#1b6e1b}.ok::before{content:"\\2713 "}
.missing{color:#a40000;font-weight:700}.missing::before{content:"\\2717 "}
.cacheonly{color:#8a5a00;font-weight:700}.cacheonly::before{content:"\\26A0 "}
.na{color:#777}.old{color:#777;text-decoration:line-through}.new{color:#8a5a00;font-weight:700}
.note{color:#555;max-width:60em;font-size:.92em}
.explicit{color:#1b4e8a;font-weight:700}.implicit{color:#555}
pre{background:#f5f5f5;padding:1rem;overflow-x:auto}
[hidden]{display:none!important}
[role=tablist]{display:flex;gap:.25rem;border-bottom:1px solid #bbb;margin:1rem 0}
[role=tab]{font:inherit;color:inherit;background:#eee;border:1px solid #bbb;border-bottom:0;
padding:.4rem .9rem;cursor:pointer;margin-bottom:-1px}
[role=tab][aria-selected=true]{background:#fff;font-weight:700;border-bottom:1px solid #fff}
.tree,.tree ul{list-style:none;margin:0;padding:0}
.tree{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.95em}
.tree ul{margin-left:.7rem;padding-left:1rem;border-left:1px solid #bbb}
.tree li{margin:.15rem 0}
:where(.tree) button{font:inherit;color:inherit;background:none;border:0;padding:0 .25rem;
cursor:pointer;text-align:left}
.tree .onpath,.tree .sel{outline:2px solid currentColor;outline-offset:1px}
.tree .sel{font-weight:700;background:#f0f0f0}
.treeview.split{display:grid;grid-template-columns:minmax(0,1fr) minmax(16rem,32rem);gap:2rem;
align-items:start}.treeview.split>.paths{grid-column:2;grid-row:1}
.treeview.split>.tree{grid-column:1;grid-row:1}
.paths{position:sticky;top:1rem;max-height:calc(100vh - 2rem);overflow:auto;
box-sizing:border-box;border:1px solid #bbb;padding:.75rem}
.paths h3{margin:0 0 .5rem;font-size:1em;overflow-wrap:anywhere}
.paths .tree .onpath,.paths .tree .sel{outline:none}.paths .tree .sel{background:none}
#paths-clear{font:inherit;color:inherit;background:#eee;border:1px solid #bbb;
margin-top:.75rem;padding:.2rem .8rem;cursor:pointer}
@media (max-width:60em){.treeview.split{display:block}
.paths{position:static;max-height:none;margin-bottom:1rem}}
button:focus-visible{outline:3px solid #1b4e8a!important;outline-offset:2px}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}th{background:#222}
th,td{border-color:#444}pre{background:#1c1c1c}.ok{color:#6fd66f}.missing{color:#ff7b7b}
.cacheonly{color:#ffc857}.new{color:#ffc857}.explicit{color:#8ab8f0}
.na,.old,.implicit,.note{color:#999}
[role=tablist],[role=tab],.tree ul,.paths,#paths-clear{border-color:#444}
[role=tab],#paths-clear{background:#222}
[role=tab][aria-selected=true]{background:#111;border-bottom-color:#111}
.tree .sel{background:#2a2a2a}button:focus-visible{outline-color:#8ab8f0!important}}
"""

# Tabs and path outlining for the HTML report.  Inline so the page stays a
# single self-contained file.  Progressive enhancement: the tab bar starts
# hidden and both panels are visible, so a viewer that strips scripts still
# shows everything, just stacked.
#
# Outlining: a click on a package selects every node with that name (a
# package required by several parents appears several times in the tree, and
# each occurrence is a path from the conanfile) and outlines the ancestors of
# each one, walking li -> enclosing li.  Siblings and children are never
# touched, so clicking the root outlines only the root.  A second click on
# the selected package clears the outline.
_TREE_JS = """
(function () {
  var tabs = [].slice.call(document.querySelectorAll('[role=tab]'));
  function remember(tab) {
    try { history.replaceState(null, '', '#' + tab.dataset.hash); } catch (err) {}
  }
  function show(tab, focus) {
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
    });
    if (focus) tab.focus();
  }
  tabs.forEach(function (t, i) {
    t.addEventListener('click', function () { show(t); remember(t); });
    t.addEventListener('keydown', function (ev) {
      var j = {ArrowLeft: i - 1, ArrowRight: i + 1, Home: 0, End: tabs.length - 1}[ev.key];
      if (j === undefined) return;
      ev.preventDefault();
      var next = tabs[(j + tabs.length) % tabs.length];
      show(next, true);
      remember(next);
    });
  });
  document.querySelector('[role=tablist]').hidden = false;
  var start = tabs.filter(function (t) { return '#' + t.dataset.hash === location.hash; })[0];
  show(start || tabs[0]);

  var tree = document.getElementById('tree');
  var panel = document.getElementById('paths-tree');
  var title = document.getElementById('paths-title');
  var empty = document.getElementById('paths-empty');
  var clear = document.getElementById('paths-clear');
  var current = null;
  // Without scripts the panel could never fill, so it starts hidden and the
  // tree only becomes two columns here.
  panel.parentElement.hidden = false;
  tree.parentElement.classList.add('split');

  // The side panel: a clone of the tree pruned to the selected package's
  // paths.  Every li without a selected button inside it is neither the
  // package nor one of its ancestors, so it goes; shared ancestors stay
  // merged because the clone keeps the tree's own structure.
  function showPaths(label, count) {
    panel.textContent = '';
    clear.hidden = count === 0;
    empty.hidden = count !== 0;
    if (!count) { title.textContent = 'Paths'; return; }
    var copy = tree.cloneNode(true);
    copy.removeAttribute('id');
    [].forEach.call(copy.querySelectorAll('li'), function (li) {
      if (!li.querySelector('.sel')) li.remove();
    });
    panel.appendChild(copy);
    title.textContent = count + (count === 1 ? ' path' : ' paths') + ' to ' + label;
  }

  // Select a package: outline every occurrence and its ancestors in the
  // main tree, then mirror just those paths in the panel.  Selecting the
  // current package again clears both.
  function select(name) {
    [].forEach.call(tree.querySelectorAll('.sel, .onpath'), function (b) {
      b.classList.remove('sel', 'onpath');
      b.setAttribute('aria-pressed', 'false');
    });
    current = current === name ? null : name;
    var label = '', count = 0;
    [].forEach.call(tree.querySelectorAll('button[data-name]'), function (b) {
      if (current === null || b.dataset.name !== current) return;
      label = b.textContent;
      count++;
      b.classList.add('sel');
      b.setAttribute('aria-pressed', 'true');
      for (var li = b.parentElement.parentElement.closest('li'); li;
           li = li.parentElement.closest('li')) {
        li.querySelector(':scope > button').classList.add('onpath');
      }
    });
    showPaths(label, count);
  }

  function onClick(ev) {
    var button = ev.target.closest('button[data-name]');
    if (button) select(button.dataset.name);
    return button;
  }
  tree.addEventListener('click', onClick);
  panel.addEventListener('click', function (ev) {
    // The panel was rebuilt under the pointer; keep keyboard focus in it.
    if (!onClick(ev)) return;
    (panel.querySelector('.sel') || tree.querySelector('button')).focus();
  });
  clear.addEventListener('click', function () {
    if (current === null) return;
    var name = current;
    select(name);
    // Clear hides itself; put focus on the package in the tree instead of
    // dropping it on the page.
    [].filter.call(tree.querySelectorAll('button[data-name]'), function (b) {
      return b.dataset.name === name;
    })[0].focus();
  });
})();
"""


def _version(ref: str) -> str:
    """The version part of a "name/version[@user/channel]" reference."""
    return ref.split("/", 1)[-1].split("@", 1)[0]


def _newest(update: Optional[Update]) -> str:
    """The dependency table's "newest" cell text, shared by both renderers.

    Only the version is rendered: ``_UpdateChecker`` considers candidates
    with the same name/user/channel only, so the version is the whole
    difference from the reference already shown in the package column.
    "up to date" is spelled out instead of left blank, so the cell never
    reads like a value that could not be determined.
    """
    return _version(update.latest) if update else "up to date"


def _html_table(headers: List[str], rows: List[List[str]]) -> str:
    """Rows are lists of already-escaped/markup cell strings."""
    head = "".join("<th>%s</th>" % html.escape(h) for h in headers)
    body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in row) for row in rows)
    return "<table><tr>%s</tr>%s</table>" % (head, body)


def _tree_html(node: _TreeNode, top: bool = True) -> str:
    """The hierarchy as nested lists of buttons, for ``_TREE_JS`` to outline.

    A problem node reuses the table cells' class, colour plus a glyph, so
    colour is never the only signal.  Every package's tooltip is its
    binary status per build type (``_TreeNode.tip``).
    """
    e = html.escape
    attrs = ' class="%s"' % node.status if node.status else ""
    if node.tip:
        attrs += ' title="%s"' % e(node.tip)
    item = '<button type="button" data-name="%s" aria-pressed="false"%s>%s</button>' % (
        e(node.name),
        attrs,
        e(node.label),
    )
    if node.note:
        item += ' <span class="na">(%s)</span>' % e(node.note)
    if node.children:
        item += "<ul>%s</ul>" % "".join(_tree_html(c, top=False) for c in node.children)
    item = "<li>%s</li>" % item
    return '<ul class="tree" id="tree">%s</ul>' % item if top else item


def render_html(report: Report) -> str:
    e = html.escape
    parts: List[str] = []
    w = parts.append
    w('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">')
    # The package, when the conanfile names itself, is the better browser-tab
    # title: a path says little when several reports are open at once.
    w(
        "<title>conandeps: %s</title><style>%s</style></head><body>"
        % (e(report.package or report.conanfile), _CSS)
    )
    w("<h1>conandeps report</h1>")
    header = _header(report, with_conanfile=True)
    w("<p>%s</p>" % "<br>".join("<b>%s:</b> %s" % (e(k), e(v)) for k, v in header))

    # Two tabs: the tables, and the interactive tree.  The tab bar is hidden
    # until _TREE_JS runs, so without scripts both panels show stacked.
    w('<div role="tablist" aria-label="Report views" hidden>')
    for key, label in (("report", "Report"), ("tree", "Dependency tree")):
        w(
            '<button type="button" role="tab" id="tabbtn-%s" aria-controls="tab-%s" '
            'data-hash="%s">%s</button>' % (key, key, key, label)
        )
    w("</div>")
    w('<section id="tab-report" role="tabpanel" aria-labelledby="tabbtn-report">')

    # ---- dependencies ----------------------------------------------------
    w("<h2>Dependencies (%d) and binary availability</h2>" % len(report.deps))
    # This table is the one place per-package facts live; there is no separate
    # "newer versions" or "missing binaries" section repeating its cells.
    # With --no-check-updates nothing was searched, so the newest column is
    # dropped rather than filled with cells that would read as "up to date".
    newest = {u.ref: u for u in report.updates}
    rows = []
    for dep in sorted(report.deps.values(), key=_dep_key):
        # A version picked from a range says so in the package cell's tooltip.
        rng = report.ranges.get(dep.ref)
        package = '<span title="from %s">%s</span>' % (e(rng), e(dep.ref)) if rng else e(dep.ref)
        row = [package, e(_via(dep, " \u2192 ")).replace("\n", "<br>"), e(dep.context)]
        if report.checked_updates:
            u = newest.get(dep.ref)
            # How "newer" was decided, and both export dates, are the tooltip.
            if u:
                tip = "%s; exported %s, newest exported %s" % (
                    u.note,
                    _date(u.current_time),
                    _date(u.latest_time),
                )
                row.append('<span class="new" title="%s">%s</span>' % (e(tip), e(_newest(u))))
            else:
                row.append('<span class="ok">%s</span>' % _newest(u))
        for bt in report.build_types:
            status, pid, remote = dep.binaries[bt]
            cls = {"Remote": "ok", "Missing": "missing", "CacheOnly": "cacheonly"}.get(status, "na")
            text = e(_cell(status, pid, remote, _Palette(False), dep.fallbacks.get(bt)))
            row.append('<span class="%s" title="package_id %s">%s</span>' % (cls, e(pid), text))
        rows.append(row)
    headers = ["package", "via", "context"]
    if report.checked_updates:
        headers.append("newest")
    w(_html_table(headers + list(report.build_types), rows))
    # host/build is Conan vocabulary, and the column is two words wide, so the
    # page explains it in prose rather than in a tooltip nobody can reach on a
    # phone.  Always shown: a graph without build requirements today may have
    # one tomorrow, and the reader is rarely the person who ran the tool.
    w(
        '<p class="note"><b>context</b>: <code>host</code> means the package is part of '
        "what you are building, compiled for your target platform and linked into your "
        "binaries. <code>build</code> means it is a build requirement, a tool that only "
        "runs during the build, such as cmake or a code generator, so it is built for the "
        "machine doing the building. The two are the same platform unless you "
        "cross-compile, and a package can appear in both contexts.</p>"
    )

    # ---- overrides --------------------------------------------------------
    w("<h2>Overrides (%d)</h2>" % len(report.overrides))
    if report.overrides:
        rows = [
            [
                e(o.package),
                '<span class="old">%s</span>' % e(o.old),
                '<span class="new">%s</span>' % e(o.new),
                e(o.by),
                '<span class="%s">%s</span>'
                % ("explicit" if o.explicit else "implicit", e(_override_kind(o))),
            ]
            for o in sorted(report.overrides, key=_override_key)
        ]
        w(_html_table(["package", "wanted", "forced to", "by", "kind"], rows))
    else:
        w("<p>None detected.</p>")

    # ---- update order (same words as render_text) ------------------------
    steps = _update_order(report)
    w("<h2>Update order (%d waves)</h2>" % (steps[-1].wave if steps else 0))
    if steps:
        rows = [
            [
                str(s.wave),
                e(s.ref),
                '<span class="new">%s</span>' % e(s.action) if s.latest else e(s.action),
                e(", ".join(s.blocked_by)) or "-",
            ]
            for s in steps
        ]
        w(_html_table(["wave", "package", "action", "blocked by"], rows))
    elif not report.checked_updates:
        w("<p>Not checked (--no-check-updates).</p>")
    else:
        w('<p class="ok">Nothing to update.</p>')

    w("</section>")

    w('<section id="tab-tree" role="tabpanel" aria-labelledby="tabbtn-tree">')
    w("<h2>Dependency hierarchy</h2>")
    w(
        '<p class="note">Click a package to outline every path from the conanfile to it '
        "and to list just those paths in the panel; click it again to clear. "
        '<span class="missing">red</span> means a binary is missing, '
        '<span class="cacheonly">orange</span> that it is only in the local cache. '
        "Hover any package for its binary status per build type.</p>"
    )
    # Large trees do not fit on a screen, so the selected package's paths
    # are also shown on their own in a panel that stays in view (_TREE_JS
    # fills it with a pruned copy of the tree).
    # The panel comes first in the page so that on a narrow screen, where
    # the columns stack, it sits above the tree in both visual and focus
    # order; on a wide one the grid puts it on the right.
    w('<div class="treeview">')
    w(
        '<aside class="paths" aria-labelledby="paths-title" hidden>'
        '<h3 id="paths-title" aria-live="polite">Paths</h3>'
        '<p class="note" id="paths-empty">Select a package to see only its paths here.</p>'
        '<div id="paths-tree"></div>'
        '<button type="button" id="paths-clear" hidden>Clear</button>'
        "</aside>"
    )
    w(_tree_html(_tree(report)))
    w("</div>")
    w("</section>")
    w("<script>%s</script>" % _TREE_JS)
    w("</body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="conandeps",
        description="List Conan dependencies, check Release/Debug/RelWithDebInfo binary "
        "availability, show requirement overrides and report newer versions on the remote.",
    )
    p.add_argument("conanfile", help="path to conanfile.py (or a directory containing one)")
    p.add_argument(
        "-r",
        "--remote",
        help="remote to look for binaries in (e.g. myremote); default: all configured remotes",
    )
    p.add_argument(
        "-pr",
        "--profile",
        action="append",
        default=[],
        help="profile to use (repeatable, same as conan -pr)",
    )
    p.add_argument(
        "-s",
        "--settings",
        action="append",
        default=[],
        help="extra settings, key=value (repeatable)",
    )
    p.add_argument(
        "-o",
        "--options",
        action="append",
        default=[],
        help="extra options, pkg:key=value (repeatable)",
    )
    p.add_argument(
        "--build-types",
        default=",".join(DEFAULT_BUILD_TYPES),
        help="comma separated build types to check (default: %(default)s)",
    )
    p.add_argument(
        "-u",
        "--update",
        action="store_true",
        help="check the remote for newer recipes/binaries (conan -u)",
    )
    p.add_argument(
        "--no-check-updates",
        action="store_true",
        help="do not check the remote for newer versions of each package",
    )
    p.add_argument(
        "--fallbacks",
        metavar="FILE",
        help="INI file naming, per build type, the packages to check under another "
        "build type instead (e.g. [RelWithDebInfo] with 'zlib = Release')",
    )
    p.add_argument("--html", metavar="FILE", help="also write an HTML report to FILE")
    p.add_argument(
        "-v", "--verbose", action="store_true", help="stream Conan's own output to stderr live"
    )
    p.add_argument("-q", "--quiet", action="store_true", help="no progress lines on stderr")
    p.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colour the text report (default: auto = only on a terminal, honours NO_COLOR)",
    )
    args = p.parse_args(argv)
    color = args.color == "always" or (
        args.color == "auto" and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    )

    build_types = [b.strip() for b in args.build_types.split(",") if b.strip()]
    try:
        fallbacks = _load_fallbacks(args.fallbacks) if args.fallbacks else {}
        report = build_report(
            args.conanfile,
            args.remote,
            args.profile,
            args.settings,
            args.options,
            build_types,
            args.update,
            args.verbose,
            args.quiet,
            check_updates=not args.no_check_updates,
            fallbacks=fallbacks,
        )
    except Exception as exc:  # bad --fallbacks file, ConanException and friends
        sys.stderr.write("conandeps: error: %s\n" % exc)
        return 2

    sys.stdout.write(render_text(report, color))
    if args.html:
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(render_html(report))
        sys.stdout.write("HTML report written to %s\n" % args.html)
    else:
        # The text report is deliberately a summary; say where the rest is.
        sys.stdout.write("Full report: --html FILE\n")
    return 1 if report.problems() else 0


if __name__ == "__main__":
    sys.exit(main())
