"""Tests for conandeps.

The ``conan_env`` fixture (a throw-away conan_server with a fake package
graph, described in ``conftest.py``) is shared with the other tools' tests.
"""

import os
import re
from pathlib import Path

import pytest
from conftest import BUILD_TYPES, RECIPE, _conan

import conandeps


@pytest.fixture(scope="session")
def report(conan_env):
    return conandeps.build_report(
        conan_env["conanfile"], "test", [], [], [], list(BUILD_TYPES), update=False
    )


def test_dependencies_listed(report):
    refs = {d.ref for d in report.deps.values()}
    assert refs == {
        "libfoo/1.0",
        "libbar/1.1",
        "libbaz/2.0",
        "libqux/1.1",
        "hdr/1.0",
        "libdeep/1.0",
    }
    direct = {d.ref for d in report.deps.values() if d.direct}
    assert direct == {"libfoo/1.0", "libbaz/2.0", "libqux/1.1", "hdr/1.0"}
    # libbar is only reachable through libfoo -> indirect, even though the
    # consumer mentions it with override=True.
    assert not report.deps["libbar#host"].direct


def test_sorted_by_path(report):
    order = [(d.ref, d.path) for d in report.deps.values()]
    # Direct deps first (alphabetical), then by shortest path.
    assert order == [
        ("hdr/1.0", []),
        ("libbaz/2.0", []),
        ("libfoo/1.0", []),
        ("libqux/1.1", []),  # required directly, even though libbaz also pulls it in
        ("libbar/1.1", ["libfoo"]),
        ("libdeep/1.0", ["libfoo", "libbar"]),
    ]
    # libqux is required by both the consumer and libbaz -> two parents, but
    # it is direct, so the via column just says "-".
    assert sorted(report.deps["libqux#host"].parents) == ["libbaz", "your conanfile"]
    assert conandeps._via(report.deps["libqux#host"]) == "-"
    page = conandeps.render_html(report)
    assert "<th>via</th>" in page
    assert "<td>libdeep/1.0</td><td>libfoo \u2192 libbar</td><td>host</td>" in page


def test_via_lists_every_parent_path():
    boost = conandeps.Dep(ref="boost/1.8", context="host", depth=2, path=["MyDepA"])
    boost.parents = ["MyDepA", "Zigma"]
    boost.paths = [["MyDepA"], ["MyDepB", "Zigma"]]
    assert conandeps._via(boost) == "MyDepA\nMyDepB -> Zigma"
    assert conandeps._via(boost, " > ", "; ") == "MyDepA; MyDepB > Zigma"
    # Multi-line cells spread the row; other columns stay aligned and blank.
    lines = conandeps._table(
        ["package", "via", "ctx"],
        [["boost/1.8", conandeps._via(boost), "host"]],
        conandeps._Palette(False),
    )
    assert lines[2:] == [
        "boost/1.8  MyDepA           host",
        "           MyDepB -> Zigma",
    ]


def _mini_report(
    deps, overrides=(), updates=(), checked_updates=True, fallbacks=None, package=None, ranges=None
):
    """Hand-built single-build-type Report for renderer ordering tests."""
    return conandeps.Report(
        "conanfile.py",
        "test",
        {},
        ["Release"],
        deps,
        list(overrides),
        dict(ranges or {}),
        [],
        list(updates),
        checked_updates,
        dict(fallbacks or {}),
        package,
    )


def _mini_dep(ref, direct=False, paths=(), status="Remote", requires=(), fallback=None):
    dep = conandeps.Dep(ref=ref, context="host", direct=direct)
    dep.paths = [list(p) for p in paths]
    dep.requires = list(requires)
    dep.binaries = {"Release": (status, "f" * 40, "test" if status == "Remote" else None)}
    if fallback:
        dep.fallbacks = {"Release": fallback}
    return dep


def test_dependency_rows_sort_by_via_then_name():
    """The HTML dependency rows sort on the via column, then package name.

    Unlike the report's depth-first order, this groups each chain: the depth-3
    package via "apple -> beta" comes before the depth-2 one via "zebra".
    Direct deps stay on top because their via cell "-" sorts before letters.
    """
    deps = {  # deliberately scrambled insertion order
        "alpha#host": _mini_dep("alpha/1.0", paths=[["zebra"]]),
        "deep#host": _mini_dep("deep/1.0", paths=[["apple", "beta"]]),
        "zdirect#host": _mini_dep("zdirect/1.0", direct=True),
        "beta#host": _mini_dep("beta/1.0", paths=[["apple"]]),
    }
    expected = ["zdirect", "beta", "deep", "alpha"]
    page = conandeps.render_html(_mini_report(deps))
    assert re.findall(r"<td>(\w+)/1\.0</td>", page.split("<h2>Overrides")[0]) == expected


def test_overrides_and_missing_binaries_sorted():
    """Overrides (HTML) and missing binaries (text) both sort alphabetically."""
    deps = {
        "zeta#host": _mini_dep("zeta/1.0", direct=True, status="Missing"),
        "alpha#host": _mini_dep("alpha/1.0", direct=True, status="Missing"),
    }
    overrides = [
        conandeps.Override("zeta/1.0", "x/1.0", "x/1.1", "your conanfile", False),
        conandeps.Override("alpha/1.0", "y/1.0", "y/1.1", "your conanfile", True),
    ]
    page = conandeps.render_html(_mini_report(deps, overrides))
    ov_table = page.split("<h2>Overrides")[1].split("<h2>")[0]
    assert re.findall(r"<td>(\w+)/1\.0</td>", ov_table) == ["alpha", "zeta"]
    # The HTML report has no separate missing-binaries section: the cells of
    # the dependency table already say MISSING.
    assert "Missing binaries" not in page

    text = conandeps.render_text(_mini_report(deps, overrides))
    assert re.findall(r"^(\w+)/1\.0", text.split("Missing binaries")[1], re.M) == ["alpha", "zeta"]


def test_pick_latest_orders_loosely():
    """Conan's lenient Version copes with semver, dates and mixed schemes."""
    pick = conandeps._pick_latest
    assert pick("1.2.3", ["1.2.3", "1.10.0"]) == ("1.10.0", True)  # numeric, not lexicographic
    assert pick("1.10.0", ["1.2.3", "1.10.0"]) is None  # current already newest
    assert pick("2024.01", ["2024.01", "2024.2"]) == ("2024.2", True)
    assert pick("cci.20230505", ["cci.20230505", "cci.20240101"]) == ("cci.20240101", True)
    assert pick("1.2", ["1.2", "1.2.0"]) == ("1.2.0", False)  # equal -> timestamps decide
    assert pick("master", ["master", "1.0"]) == ("1.0", True)  # unorderable scheme
    assert pick("1.1", ["1.1"]) is None  # nothing else on the remote


def test_update_note():
    note = conandeps._update_note
    assert note(True, 100, 200) == "newer version"
    assert note(True, None, None) == "newer version"
    assert note(True, 200, 100) == "newer version, but exported before current"
    assert note(False, 100, 200) == "newer export date only"


def test_newest_tooltip_carries_the_export_dates():
    """The newest cell's tooltip is the only place the export dates are shown."""
    deps = {
        "alpha#host": _mini_dep("alpha/1.0", direct=True),
        "zeta#host": _mini_dep("zeta/1.0", direct=True),
    }
    ups = [
        conandeps.Update("zeta/1.0", "zeta/2.0", "test", 1700000000, 1760000000, "newer version"),
        conandeps.Update("alpha/1.0", "alpha/2.0", "test", None, None, "newer version"),
    ]
    page = conandeps.render_html(_mini_report(deps, updates=ups))
    assert "Newer versions" not in page  # folded into the dependency table
    assert 'title="newer version; exported 2023-11-14, newest exported 2025-10-09">2.0<' in page
    # Unreadable manifests render as ?, not a guess.
    assert 'title="newer version; exported ?, newest exported ?">2.0<' in page


def test_version_range_is_the_package_tooltip():
    deps = {
        "a#host": _mini_dep("a/1.2", direct=True),
        "b#host": _mini_dep("b/1.0", direct=True),
    }
    page = conandeps.render_html(_mini_report(deps, ranges={"a/1.2": "a/[>=1.0 <2]"}))
    assert '<td><span title="from a/[&gt;=1.0 &lt;2]">a/1.2</span></td>' in page
    assert "<td>b/1.0</td>" in page  # no range, no tooltip


def test_dependency_table_shows_newest_version():
    """The dependency table joins the update check onto every package row.

    The cell carries the version only - the update checker keeps name, user
    and channel, so that is the whole difference from the package column -
    and --no-check-updates drops the column instead of showing "up to date".
    """
    deps = {
        "a#host": _mini_dep("a/1.0", direct=True),
        "b#host": _mini_dep("b/1.0", direct=True),
    }
    ups = [conandeps.Update("a/1.0", "a/2.0", "test", None, None, "newer version")]

    table = conandeps.render_html(_mini_report(deps, updates=ups)).split("<h2>Overrides")[0]
    assert "<th>newest</th>" in table
    tip = "newer version; exported ?, newest exported ?"
    assert '<td><span class="new" title="%s">2.0</span></td>' % tip in table
    assert '<td><span class="ok">up to date</span></td>' in table

    plain = _mini_report(deps, checked_updates=False)
    assert "<th>newest</th>" not in conandeps.render_html(plain).split("<h2>Overrides")[0]


def test_updates_not_checked_message():
    deps = {"a#host": _mini_dep("a/1.0", direct=True)}
    report = _mini_report(deps, checked_updates=False)
    assert "newer versions not checked" in conandeps.render_text(report)  # summary line
    assert "Not checked (--no-check-updates)." in conandeps.render_html(report)


def test_update_order_waves_consumers_after_requirements():
    """Consumers of an outdated package are in the plan, deepest wave first.

    Only boost has a newer version, but MyDepA and Zigma have to be
    re-released against it before your conanfile can see it. Those two share
    a wave because neither requires the other; MyDepB waits for Zigma, and
    the conanfile is always last. hdr requires nothing outdated, so it is
    left out of the plan entirely.
    """
    deps = {
        "MyDepA#host": _mini_dep("MyDepA/1.0", direct=True, requires=["boost"]),
        "MyDepB#host": _mini_dep("MyDepB/1.0", direct=True, requires=["Zigma"]),
        "Zigma#host": _mini_dep("Zigma/1.0", paths=[["MyDepB"]], requires=["boost"]),
        "boost#host": _mini_dep("boost/1.8", paths=[["MyDepA"]]),
        "hdr#host": _mini_dep("hdr/1.0", direct=True),
    }
    ups = [conandeps.Update("boost/1.8", "boost/1.9", "test", None, None, "newer version")]
    steps = conandeps._update_order(_mini_report(deps, updates=ups))
    assert [(s.wave, s.ref) for s in steps] == [
        (1, "boost/1.8"),
        (2, "MyDepA/1.0"),
        (2, "Zigma/1.0"),
        (3, "MyDepB/1.0"),
        (4, "conanfile.py"),
    ]
    assert steps[0].action == "update to 1.9" and steps[0].blocked_by == []
    assert steps[1].action == "re-release" and steps[1].blocked_by == ["boost"]
    assert steps[-1].action == "bump requirements"
    assert steps[-1].blocked_by == ["MyDepA", "MyDepB"]  # direct requirements only
    # A conanfile that names itself is the last step by that name, not its path.
    named = conandeps._update_order(_mini_report(deps, updates=ups, package="myproject/1.2"))
    assert named[-1].ref == "myproject/1.2"

    page = conandeps.render_html(_mini_report(deps, updates=ups))
    html_section = page.split("<h2>Update order (4 waves)</h2>")[1]
    assert "<td>1</td><td>boost/1.8</td>" in html_section
    assert '<span class="new">update to 1.9</span>' in html_section


def test_update_order_empty_when_everything_is_newest():
    """No updates means no plan; --no-check-updates says so instead of "none"."""
    deps = {"a#host": _mini_dep("a/1.0", direct=True)}
    assert conandeps._update_order(_mini_report(deps)) == []
    assert "Nothing to update." in conandeps.render_html(_mini_report(deps))
    unchecked = conandeps.render_html(_mini_report(deps, checked_updates=False))
    assert "Not checked (--no-check-updates)." in unchecked.split("Update order (0 waves)")[1]


def test_update_order_from_the_real_graph(report):
    """libbar/1.1 is outdated, so libfoo (its only consumer) waits for it."""
    steps = {s.ref: s for s in conandeps._update_order(report)}
    assert steps["libbar/1.1"].wave == 1
    assert steps["libbar/1.1"].action == "update to 1.2"
    assert steps["libfoo/1.0"].wave == 2
    assert steps["libfoo/1.0"].blocked_by == ["libbar"]
    assert steps["myproject/0.1"].wave == 3  # the named conanfile, not its path
    assert "libqux/1.1" not in steps  # nothing outdated below it


def test_newer_versions(report):
    ups = {u.ref: u for u in report.updates}
    # libbar/1.2 sits on the remote unused; everything else is at its newest.
    assert set(ups) == {"libbar/1.1"}
    u = ups["libbar/1.1"]
    assert u.latest == "libbar/1.2" and u.remote == "test"
    assert u.note == "newer version"
    # Manifest timestamps come back for both refs, 1.2 exported after 1.1.
    assert u.current_time and u.latest_time and u.latest_time >= u.current_time
    page = conandeps.render_html(report)
    assert re.search(
        r'<td>libbar/1\.1</td>.*?<span class="new" title="newer version; '
        r'exported \d{4}-\d{2}-\d{2}, newest exported \d{4}-\d{2}-\d{2}">1\.2</span>',
        page,
    )


def test_missing_binaries(report):
    missing = {d.ref: d.missing() for d in report.deps.values() if d.missing()}
    assert missing == {"libbar/1.1": ["Debug", "RelWithDebInfo"]}
    # The others must be found on the remote (not in cache, we wiped it).
    for dep in report.deps.values():
        for bt, (status, pid, remote) in dep.binaries.items():
            if dep.ref != "libbar/1.1":
                assert status == "Remote", (dep.ref, bt, status)
                assert remote == "test"
                assert pid


def test_header_names_the_package_when_the_conanfile_has_one():
    """A conanfile that names and versions itself is identified in the report."""
    deps = {"a#host": _mini_dep("a/1.0", direct=True)}
    named = _mini_report(deps, package="myproject/0.1")
    assert "\npackage  : myproject/0.1\n" in conandeps.render_text(named)
    page = conandeps.render_html(named)
    assert "<title>conandeps: myproject/0.1</title>" in page
    assert "<b>package:</b> myproject/0.1<br><b>conanfile:</b> conanfile.py" in page

    # A conanfile without a name simply has no package line.
    anonymous = _mini_report(deps)
    assert "package" not in conandeps.render_text(anonymous).split("\n\n")[0]
    assert "<b>package:</b>" not in conandeps.render_html(anonymous)


def test_fallback_file_parsing(tmp_path):
    """One section per build type; package names keep their case."""
    ini = tmp_path / "fallbacks.ini"
    ini.write_text(
        "# packages we never build for every type\n"
        "[RelWithDebInfo]\n"
        "MyDepA = Release\n"
        "zlib = Release\n"
    )
    assert conandeps._load_fallbacks(str(ini)) == {
        "RelWithDebInfo": {"MyDepA": "Release", "zlib": "Release"}
    }
    # A package mapped to its own build type would do nothing at all.
    ini.write_text("[Release]\nzlib = Release\n")
    with pytest.raises(ValueError):
        conandeps._load_fallbacks(str(ini))


def test_fallback_is_spelled_out_in_both_renderers():
    """A binary found under a fallback never reads as one for the column itself."""
    deps = {
        "zlib#host": _mini_dep("zlib/1.2", direct=True, fallback="Debug"),
        "icu#host": _mini_dep("icu/70", direct=True, status="Missing", fallback="Debug"),
    }
    report = _mini_report(deps, fallbacks={"Release": {"zlib": "Debug", "icu": "Debug"}})

    text = conandeps.render_text(report)
    # Header labels line up: "remote   :", "profile  :", "fallbacks:".
    assert "\nfallbacks: Release: icu -> Debug, zlib -> Debug\n" in text
    # A fallback without a binary either is still missing, and the row says
    # which build type was actually looked for.
    assert re.search(r"^icu/70\s+Release \(via Debug\)\s+no binary anywhere$", text, re.M)

    page = conandeps.render_html(report)
    assert "<b>fallbacks:</b> Release: icu -&gt; Debug, zlib -&gt; Debug" in page
    assert ">test (via Debug)</span>" in page  # a found binary
    assert ">MISSING (via Debug)</span>" in page  # and one missing under the fallback too


def test_fallback_build_types(conan_env, tmp_path):
    """libbar has Release binaries only; a fallback file makes that acceptable.

    The fallback is handed to Conan as ``libbar:build_type=Release``, so all
    three columns end up on the very same package_id - the Release binary -
    and libbar drops out of the missing-binaries report and the exit code.
    """
    ini = tmp_path / "fallbacks.ini"
    ini.write_text("[Debug]\nlibbar = Release\n\n[RelWithDebInfo]\nlibbar = Release\n")
    fallbacks = conandeps._load_fallbacks(str(ini))

    report = conandeps.build_report(
        conan_env["conanfile"],
        "test",
        [],
        [],
        [],
        list(BUILD_TYPES),
        update=False,
        quiet=True,
        check_updates=False,
        fallbacks=fallbacks,
    )
    libbar = report.deps["libbar#host"]
    assert libbar.missing() == []
    assert libbar.fallbacks == {"Debug": "Release", "RelWithDebInfo": "Release"}
    assert len({pid for _, pid, _ in libbar.binaries.values()}) == 1  # one binary, three columns
    assert report.problems() == []
    assert not report.deps["libfoo#host"].fallbacks  # only the listed package moves

    assert "All packages have binaries on the remote" in conandeps.render_text(report)
    page = conandeps.render_html(report)
    assert page.count(">test (via Release)</span>") == 2  # Debug and RelWithDebInfo

    rc = conandeps.main(
        [conan_env["conanfile"], "-r", "test", "--no-check-updates", "--fallbacks", str(ini), "-q"]
    )
    assert rc == 0  # the known, accepted gap no longer fails CI


def test_cache_only_is_not_available(conan_env):
    """A binary that exists only in the local cache must not count as available.

    Build libbar/1.1 Debug locally without uploading it, and install the
    Release one so it is cached too.  Release must still be attributed to the
    remote; Debug must be reported as cache-only (a problem).
    """
    env = dict(os.environ)
    d = Path(conan_env["home"]) / "libbar-local"
    d.mkdir()
    (d / "conanfile.py").write_text(
        RECIPE.format(name="libbar", version="1.1", requires='requires = ("libdeep/1.0",)')
    )
    _conan(env, "create", str(d), "-s", "build_type=Debug")
    _conan(env, "install", "libbar/1.1@", "-s", "build_type=Release", "-r", "test")

    report = conandeps.build_report(
        conan_env["conanfile"], "test", [], [], [], list(BUILD_TYPES), update=False, quiet=True
    )
    libbar = report.deps["libbar#host"]
    assert libbar.binaries["Release"][0] == "Remote"
    assert libbar.binaries["Release"][2] == "test"
    assert libbar.binaries["Debug"][0] == "CacheOnly"
    assert libbar.binaries["RelWithDebInfo"][0] == "Missing"
    assert libbar.missing() == ["Debug", "RelWithDebInfo"]

    text = conandeps.render_text(report)
    assert "local cache only" in text  # the cell and the missing-binaries reason
    assert 'class="cacheonly"' in conandeps.render_html(report)
    _conan(env, "remove", "libbar/1.1@", "-f")  # leave the cache as other tests expect


def test_overrides(report):
    # Conan names the overrider with the consumer's own reference, because
    # this conanfile declares a name and a version; an unnamed one would be
    # reported as "your conanfile".
    ov = {(o.package, o.old, o.new, o.by, o.explicit) for o in report.overrides}
    assert ov == {
        ("libfoo/1.0", "libbar/1.0", "libbar/1.1", "myproject/0.1", True),
        ("libbaz/2.0", "libqux/1.0", "libqux/1.1", "myproject/0.1", False),
    }


def test_text_render_is_a_summary(report):
    """The terminal report is the header, the counts and the missing binaries.

    Everything the HTML report carries - the dependency table, the overrides,
    the newer versions, the update order and the tree - is deliberately absent
    here, so the text stays scannable in a CI log.
    """
    text = conandeps.render_text(report)
    assert text.startswith("conandeps report for ")
    assert "\npackage  : myproject/0.1\n" in text
    assert "\nremote   : test\n" in text
    assert "\n6 dependencies, 2 overrides, 1 newer version\n" in text
    assert "Missing binaries (2)" in text
    assert re.search(r"^libbar/1\.1\s+Debug\s+no binary anywhere$", text, re.M)
    for section in ("Dependencies (", "Overrides (", "Newer versions (", "Update order ("):
        assert section not in text
    assert "Dependency hierarchy" not in text and "`-- " not in text
    assert "\033[" not in text  # no ANSI codes unless asked for

    coloured = conandeps.render_text(report, color=True)
    assert "\033[1;31mno binary anywhere\033[0m" in coloured
    # Colour codes must not break column alignment: same visible layout.
    assert conandeps._ANSI_RE.sub("", coloured) == text


def test_html_render(report):
    page = conandeps.render_html(report)
    assert page.count('">test</span>') == 16  # every available cell names the remote
    # The host/build column is explained in prose under the table.
    assert '<p class="note"><b>context</b>: <code>host</code> means' in page
    assert "<code>build</code> means it is a build requirement" in page
    # Overrides table: wanted version struck through, forced version marked.
    assert '<td>libfoo/1.0</td><td><span class="old">libbar/1.0</span></td>' in page
    assert "explicit (override=True)" in page
    # A missing cell carries its package_id as a tooltip, like every cell.
    assert re.search(r'<span class="missing" title="package_id [0-9a-f]{40}">MISSING</span>', page)
    assert page.startswith("<!DOCTYPE html>")
    # dependencies, overrides, update order - nothing repeats the first one
    assert page.count("<table>") == 3
    assert "<td>3</td><td>myproject/0.1</td>" in page  # last update-order wave
    assert 'class="explicit"' in page and 'class="implicit"' in page

    # Two tabs; the bar is hidden until the script runs, so without scripts
    # both panels show.  The tables are in the report tab, the tree in its own.
    assert '<div role="tablist" aria-label="Report views" hidden>' in page
    report_tab = page.split('<section id="tab-report"')[1].split("</section>")[0]
    tree_tab = page.split('<section id="tab-tree"')[1].split("</section>")[0]
    assert report_tab.count("<table>") == 3 and "<table>" not in tree_tab
    assert '<ul class="tree" id="tree">' in tree_tab
    # The paths panel the script fills; hidden without scripts, and before
    # the tree in the page so a stacked layout reads panel-then-tree.
    panel = re.search(
        r'<div class="treeview"><aside class="paths" aria-labelledby="paths-title" hidden>'
        r'<h3 id="paths-title" aria-live="polite">Paths</h3>.*?<div id="paths-tree"></div>'
        r'.*?</aside><ul class="tree" id="tree">',
        tree_tab,
    )
    assert panel
    # Self-contained: the script is inline and nothing is fetched.
    assert "<script>" in page and " src=" not in page and " href=" not in page

    # The fixture's consumer is named, so the root is the package, not the
    # path; nesting follows the graph.
    button = r'<button type="button" data-name="%s" aria-pressed="false"[^>]*>%s</button>'
    assert re.search(
        r'<ul class="tree" id="tree"><li>'
        + button % ("myproject/0.1", r"myproject/0\.1")
        + r"<ul>.*?<li>"
        + button % ("libfoo", r"libfoo/1\.0")
        + r"<ul><li>"
        + button % ("libbar", r"libbar/1\.1")
        + r"<ul><li>"
        + button % ("libdeep", r"libdeep/1\.0"),
        tree_tab,
    )
    # libbar lacks Debug and RelWithDebInfo binaries: red in the tree, and its
    # tooltip spells out every build type in the table cells' words.
    libbar = re.search(
        r'data-name="libbar" aria-pressed="false" class="missing" title="([^"]*)"', tree_tab
    )
    assert libbar and "Debug: MISSING" in libbar[1] and "RelWithDebInfo: MISSING" in libbar[1]
    assert "Release: test" in libbar[1]
    # Every package has one, so hovering never looks broken; the root has none.
    assert tree_tab.count('<button type="button" data-name=') == tree_tab.count(" title=") + 1


def test_tree_marks_problems_and_nests_paths():
    """The user's example: A uses B and C, C uses D.

    B is missing (red), D is only in the local cache (orange), C is fine.
    A package both missing and cache-only is marked as missing, the worse one.
    """
    deps = {
        "B#host": _mini_dep("B/1.0", direct=True, status="Missing"),
        "C#host": _mini_dep("C/1.0", direct=True, requires=["D"]),
        "D#host": _mini_dep("D/1.0", paths=[["C"]], status="CacheOnly"),
    }
    root = conandeps._tree(_mini_report(deps, package="A/1.0"))
    assert root.label == "A/1.0" and [c.name for c in root.children] == ["B", "C"]
    b, c = root.children
    assert (b.status, b.children) == ("missing", [])
    assert c.status is None and [d.name for d in c.children] == ["D"]
    assert c.children[0].status == "cacheonly"

    page = conandeps._tree_html(root)
    assert 'data-name="C" aria-pressed="false" title="Release: test">C/1.0</button>' in page
    assert 'data-name="B" aria-pressed="false" class="missing" title="Release: MISSING">' in page
    assert (
        'data-name="D" aria-pressed="false" class="cacheonly" title="Release: local cache only">'
        in page
    )
    # D sits under C, not under B: outlining walks exactly this nesting.
    assert re.search(r'data-name="C"[^<]*</button><ul><li><button[^>]*data-name="D"', page)

    both = _mini_dep("E/1.0", direct=True)
    both.binaries = {"Debug": ("CacheOnly", "f" * 40, None), "Release": ("Missing", "f" * 40, None)}
    assert conandeps._tree_status(both) == "missing"


def test_cli_exit_code(conan_env, tmp_path, capsys):
    out = tmp_path / "r.html"
    args = [conan_env["conanfile"], "-r", "test", "--html", str(out), "--no-check-updates"]
    rc = conandeps.main(args)
    assert rc == 1  # missing binaries -> non-zero so CI can fail on it
    assert out.exists()
    captured = capsys.readouterr()
    assert "conandeps report" in captured.out
    assert "newer versions not checked" in captured.out
    assert "HTML report written to" in captured.out

    # Without --html the summary says where the rest of the report is.
    assert conandeps.main([conan_env["conanfile"], "-r", "test", "--no-check-updates", "-q"]) == 1
    assert "Full report: --html FILE" in capsys.readouterr().out
