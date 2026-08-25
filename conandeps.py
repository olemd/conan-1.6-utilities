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
  * reports every requirement override - explicit ``override=True`` ones as
    well as implicit ones where a downstream consumer simply asks for a newer
    version than a transitive dependency declared - as a hierarchy showing who
    asked for what and which version won.

How it works
------------
Instead of hand-matching ``conan search`` output against the profile we ask
Conan itself: the dependency graph is built once per build_type through the
same ``conan info`` code path that ``conan install`` uses, and every node's
``binary`` status (Cache/Download/Update/Missing/...) is read back.  This
honours ``package_id()`` customisations, options and the configured
``default_package_id_mode`` exactly the way a real install would.

Conan 1.x does not record overrides on the graph; the only trace is a warning
emitted by ``Requirements.update()``.  We therefore capture Conan's output
stream while the graph is built and parse those lines.

Targets Conan 1.66.  Uses only the standard library plus the Conan API.
"""

from __future__ import annotations

import argparse
import html
import io
import os
import re
import sys
import time
import warnings
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

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
    build_require: bool = False
    requires: List[str] = field(default_factory=list)  # names of its own requirements
    settings: Dict[str, str] = field(default_factory=dict)
    # build_type -> (status, package_id, remote_name)
    binaries: Dict[str, Tuple[str, str, Optional[str]]] = field(default_factory=dict)

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
class Report:
    conanfile: str
    remote: Optional[str]
    profile: Dict[str, str]
    build_types: List[str]
    deps: "OrderedDict[str, Dep]"  # keyed by "<name>#<context>"
    overrides: List[Override]
    ranges: Dict[str, str]  # resolved ref -> declared range/ref
    root_requires: List[str]

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


def _node_key(node) -> str:
    return "%s#%s" % (node.ref.name, node.context)


def _key_of(dep: Dep) -> str:
    return "%s#%s" % (dep.name, dep.context)


def _explicit_overrides(graph) -> set:
    """Set of (declaring ref or 'your conanfile', package name) for override=True requires."""
    result = set()
    for node in graph.nodes:
        who = (
            "your conanfile"
            if node.ref is None or node.recipe in ("Consumer", "Virtual")
            else str(node.ref)
        )
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


def _collect(
    graph,
    build_type: str,
    deps: "OrderedDict[str, Dep]",
    ranges: Dict[str, str],
    index: _RemoteIndex,
):
    """Merge one build_type's graph into ``deps``.

    The graph may differ between build types (conditional requirements), so we
    union nodes rather than assume the first graph is complete.
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
            parent = "your conanfile" if edge.src is root else edge.src.ref.name
            if parent not in dep.parents:
                dep.parents.append(parent)
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
) -> Report:
    log = _Tee(echo=verbose)
    _progress(quiet, "loading Conan API")
    api = _make_api(log)
    deps: "OrderedDict[str, Dep]" = OrderedDict()
    ranges: Dict[str, str] = {}
    overrides: List[Override] = []
    root_requires: List[str] = []
    profile_settings: Dict[str, str] = {}
    explicit: set = set()
    index = _RemoteIndex(api, remote)

    total = len(build_types)
    for i, bt in enumerate(build_types, 1):
        _progress(
            quiet,
            "[%d/%d] resolving graph for build_type=%s on %s ..."
            % (i, total, bt, remote or "all remotes"),
        )
        t0 = time.monotonic()
        graph, root_conanfile = api.info(
            conanfile,
            remote_name=remote,
            settings=list(settings) + ["build_type=%s" % bt],
            options=list(options) or None,
            profile_names=profiles or None,
            update=update,
        )
        explicit |= _explicit_overrides(graph)
        _collect(graph, bt, deps, ranges, index)
        missing = sum(1 for d in deps.values() if d.binaries.get(bt, ("",))[0] in _PROBLEM)
        _progress(
            quiet,
            "[%d/%d] %s: %d packages, %d missing binaries (%.1fs)"
            % (i, total, bt, len(graph.nodes) - 1, missing, time.monotonic() - t0),
        )
        if not root_requires:
            root_requires = [str(r.ref) for r in root_conanfile.requires.values()]
            profile_settings = {
                k: str(v) for k, v in root_conanfile.settings.values_list if k != "build_type"
            }

    # The same override is warned once per graph build; parse once, de-duplicated.
    overrides = _parse_overrides(log.getvalue(), explicit)
    _progress(quiet, "%d overrides detected" % len(overrides))

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
    )


# --------------------------------------------------------------------------- #
# Text rendering
# --------------------------------------------------------------------------- #
_MARK = {
    "CacheOnly": "not available",
    "Missing": "MISSING",
    "Build": "build",
    "n/a": "-",
    "Unknown": "?",
    "Invalid": "invalid",
}


def _tree_lines(report: Report) -> List[str]:
    """Render the dependency hierarchy as an indented tree, marking overrides and gaps."""
    by_name = {}
    for dep in report.deps.values():
        by_name.setdefault(dep.name, dep)  # host first (insertion order), build ok as fallback
    # (parent ref, child name) -> Override, so the tree can annotate the exact
    # edge whose requirement was rewritten.
    edge_overrides = {(o.package, o.old.split("/", 1)[0]): o for o in report.overrides}
    lines: List[str] = []

    def walk(name: str, parent: str, prefix: str, last: bool, seen: Tuple[str, ...]):
        dep = by_name.get(name)
        branch = "`-- " if last else "|-- "
        if dep is None:
            lines.append(prefix + branch + name + "  (not in graph)")
            return
        tags = []
        if dep.build_require:
            tags.append("build-require")
        ov = edge_overrides.get((parent, name))
        if ov:
            tags.append(
                "overrides %s (%s by %s)"
                % (ov.old, "explicit" if ov.explicit else "implicit", ov.by)
            )
        if dep.ref in report.ranges:
            tags.append("from %s" % report.ranges[dep.ref])
        missing = dep.missing()
        if missing:
            tags.append("MISSING: " + ",".join(missing))
        line = prefix + branch + dep.ref + ("  [" + "; ".join(tags) + "]" if tags else "")
        if name in seen:
            lines.append(line + "  (cycle)")
            return
        lines.append(line)
        child_prefix = prefix + ("    " if last else "|   ")
        for i, child in enumerate(dep.requires):
            walk(child, dep.ref, child_prefix, i == len(dep.requires) - 1, seen + (name,))

    roots = [d.name for d in report.deps.values() if d.direct]
    lines.append(report.conanfile)
    for i, name in enumerate(roots):
        walk(name, "your conanfile", "", i == len(roots) - 1, ())
    return lines


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
    """Render a column-aligned text table; cells may contain ANSI colour codes."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _visible_len(cell))

    def fmt(cells: List[str]) -> str:
        padded = [c + " " * (widths[i] - _visible_len(c)) for i, c in enumerate(cells)]
        return "  ".join(padded).rstrip()

    lines = [pal.bold(fmt(headers)), "-" * (sum(widths) + 2 * (len(widths) - 1))]
    lines.extend(fmt(r) for r in rows)
    return lines


def _cell(status: str, _package_id: str, remote: Optional[str], pal: _Palette) -> str:
    """Cell text: the remote that has the binary, or why it is not available."""
    if status == "Remote":
        return pal.ok(remote or "remote")
    text = _MARK.get(status, status)
    if status == "Missing":
        return pal.bad(text)
    if status == "CacheOnly":
        return pal.warn(text)
    return pal.dim(text)


def _override_kind(o: Override) -> str:
    return "explicit (override=True)" if o.explicit else "implicit (newer requirement downstream)"


def _via(dep: Dep, arrow: str = " -> ") -> str:
    """Shortest path to the package: '-' for direct, 'A -> B' for consumer -> A -> B -> pkg.

    ``(+N)`` marks N further packages that also require it directly; the
    dependency hierarchy section shows every path in full.
    """
    if dep.direct:
        return "-"
    via = arrow.join(dep.path)
    extra = len(dep.parents) - 1
    return via + (" (+%d)" % extra if extra > 0 else "")


def render_text(report: Report, color: bool = False) -> str:
    pal = _Palette(color)
    out: List[str] = []
    w = out.append
    w(pal.bold("conandeps report for %s" % report.conanfile))
    w("remote : %s" % (report.remote or "(all configured remotes)"))
    w("profile: " + ", ".join("%s=%s" % kv for kv in report.profile.items()))
    w("")

    # ---- dependencies ----------------------------------------------------
    w(pal.bold("Dependencies (%d) and binary availability" % len(report.deps)))
    w("")
    rows = [
        [dep.ref, _via(dep), dep.context]
        + [_cell(*dep.binaries[bt], pal) for bt in report.build_types]
        for dep in report.deps.values()
    ]
    out.extend(_table(["package", "via", "ctx"] + list(report.build_types), rows, pal))
    w("")

    # ---- missing binaries ------------------------------------------------
    problems = report.problems()
    n_missing = sum(len(d.missing()) for d in problems)
    w(pal.bold("Missing binaries (%d)" % n_missing))
    w("")
    if problems:
        rows = []
        for dep in problems:
            for bt in dep.missing():
                status, pid, _ = dep.binaries[bt]
                reason = (
                    pal.warn("not on remote, in local cache only")
                    if status == "CacheOnly"
                    else pal.bad("no binary anywhere")
                )
                rows.append([dep.ref, bt, pid, reason])
        out.extend(_table(["package", "build type", "package_id", "reason"], rows, pal))
    else:
        w(pal.ok("All packages have binaries on the remote for: " + ", ".join(report.build_types)))
    w("")

    # ---- overrides -------------------------------------------------------
    w(pal.bold("Overrides (%d)" % len(report.overrides)))
    w("")
    if report.overrides:
        rows = [
            [
                o.package,
                pal.dim(o.old),
                pal.warn(o.new),
                o.by,
                pal.bold(_override_kind(o)) if o.explicit else _override_kind(o),
            ]
            for o in report.overrides
        ]
        out.extend(_table(["package", "wanted", "forced to", "by", "kind"], rows, pal))
    else:
        w("none detected")
    w("")

    # ---- hierarchy -------------------------------------------------------
    w(pal.bold("Dependency hierarchy"))
    w("")
    for line in _tree_lines(report):
        line = line.replace("MISSING:", pal.bad("MISSING:")) if color else line
        w(line)
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
.explicit{color:#1b4e8a;font-weight:700}.implicit{color:#555}
pre{background:#f5f5f5;padding:1rem;overflow-x:auto}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}th{background:#222}
th,td{border-color:#444}pre{background:#1c1c1c}.ok{color:#6fd66f}.missing{color:#ff7b7b}
.cacheonly{color:#ffc857}.new{color:#ffc857}.explicit{color:#8ab8f0}.na,.old,.implicit{color:#999}}
"""


def _html_table(headers: List[str], rows: List[List[str]]) -> str:
    """Rows are lists of already-escaped/markup cell strings."""
    head = "".join("<th>%s</th>" % html.escape(h) for h in headers)
    body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in row) for row in rows)
    return "<table><tr>%s</tr>%s</table>" % (head, body)


def render_html(report: Report) -> str:
    e = html.escape
    parts: List[str] = []
    w = parts.append
    w('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">')
    w("<title>conandeps: %s</title><style>%s</style></head><body>" % (e(report.conanfile), _CSS))
    w("<h1>conandeps report</h1>")
    w(
        "<p><b>conanfile:</b> %s<br><b>remote:</b> %s<br><b>profile:</b> %s</p>"
        % (
            e(report.conanfile),
            e(report.remote or "(all configured remotes)"),
            e(", ".join("%s=%s" % kv for kv in report.profile.items())),
        )
    )

    # ---- dependencies ----------------------------------------------------
    w("<h2>Dependencies (%d) and binary availability</h2>" % len(report.deps))
    rows = []
    for dep in report.deps.values():
        row = [e(dep.ref), e(_via(dep, " \u2192 ")), e(dep.context)]
        for bt in report.build_types:
            status, pid, remote = dep.binaries[bt]
            cls = {"Remote": "ok", "Missing": "missing", "CacheOnly": "cacheonly"}.get(status, "na")
            text = e(_cell(status, pid, remote, _Palette(False)))
            row.append('<span class="%s" title="package_id %s">%s</span>' % (cls, e(pid), text))
        rows.append(row)
    w(_html_table(["package", "via", "context"] + list(report.build_types), rows))

    # ---- missing binaries ------------------------------------------------
    problems = report.problems()
    n_missing = sum(len(d.missing()) for d in problems)
    w("<h2>Missing binaries (%d)</h2>" % n_missing)
    if problems:
        rows = []
        for dep in problems:
            for bt in dep.missing():
                status, pid, _ = dep.binaries[bt]
                reason = (
                    '<span class="cacheonly">not on remote, in local cache only</span>'
                    if status == "CacheOnly"
                    else '<span class="missing">no binary anywhere</span>'
                )
                rows.append([e(dep.ref), e(bt), "<code>%s</code>" % e(pid), reason])
        w(_html_table(["package", "build type", "package_id", "reason"], rows))
    else:
        w(
            '<p class="ok">All packages have binaries on the remote for %s.</p>'
            % e(", ".join(report.build_types))
        )

    # ---- overrides -------------------------------------------------------
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
            for o in report.overrides
        ]
        w(_html_table(["package", "wanted", "forced to", "by", "kind"], rows))
    else:
        w("<p>None detected.</p>")

    w("<h2>Dependency hierarchy</h2><pre>%s</pre>" % e("\n".join(_tree_lines(report))))
    w("</body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="conandeps",
        description="List Conan dependencies, check Release/Debug/RelWithDebInfo binary "
        "availability and show requirement overrides.",
    )
    p.add_argument("conanfile", help="path to conanfile.py (or a directory containing one)")
    p.add_argument(
        "-r",
        "--remote",
        help="remote to look for binaries in (e.g. knor); default: all configured remotes",
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
        )
    except Exception as exc:  # ConanException and friends
        sys.stderr.write("conandeps: error: %s\n" % exc)
        return 2

    sys.stdout.write(render_text(report, color))
    if args.html:
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(render_html(report))
        sys.stdout.write("HTML report written to %s\n" % args.html)
    return 1 if report.problems() else 0


if __name__ == "__main__":
    sys.exit(main())
