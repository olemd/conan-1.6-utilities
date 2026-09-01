"""Integration test for conandeps against a throw-away local conan_server.

The fixture builds this graph (all recipes are "fake" C++ packages: they have
the usual settings but their build() does nothing, so no compiler is needed):

    consumer (conanfile.py)
      |-- libfoo/1.0        binaries: Release, Debug, RelWithDebInfo
      |     `-- libbar/1.0  binaries: Release only          <- MISSING Debug/RWDI
      |           `-- libdeep/1.0  binaries: all three     <- depth 3
      |-- libbar/1.1 (override=True)                        <- explicit override
      |-- libbaz/2.0        binaries: all three
      |     `-- libqux/1.0  binaries: all three
      `-- libqux/1.1        binaries: all three             <- implicit override
      `-- hdr/1.0           header-only (no settings), binary always exists

Everything runs in an isolated CONAN_USER_HOME so the developer's cache and
remotes are never touched.
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import conandeps  # noqa: E402

RECIPE = textwrap.dedent("""\
    from conans import ConanFile

    class Pkg(ConanFile):
        name = "{name}"
        version = "{version}"
        settings = "os", "compiler", "build_type", "arch"
        {requires}
        def package(self):
            open("marker.txt", "w").write("x")
            self.copy("marker.txt")
    """)

HEADER_ONLY = textwrap.dedent("""\
    from conans import ConanFile

    class Pkg(ConanFile):
        name = "hdr"
        version = "1.0"
        def package(self):
            open("marker.txt", "w").write("x")
            self.copy("marker.txt")
    """)

CONSUMER = textwrap.dedent("""\
    from conans import ConanFile

    class Consumer(ConanFile):
        settings = "os", "compiler", "build_type", "arch"
        requires = ("libfoo/1.0", "libbaz/2.0", "libqux/1.1", "hdr/1.0")

        def requirements(self):
            self.requires("libbar/1.1", override=True)
    """)

PROFILE = textwrap.dedent("""\
    [settings]
    os=Linux
    arch=x86_64
    compiler=gcc
    compiler.version=12
    compiler.libcxx=libstdc++11
    build_type=Release
    """)

SERVER_CONF = textwrap.dedent("""\
    [server]
    jwt_secret: testsecrettestsecret
    jwt_expire_minutes: 120
    ssl_enabled: False
    port: {port}
    public_port:
    host_name: 127.0.0.1
    authorize_timeout: 1800
    disk_storage_path: ./data
    disk_authorize_timeout: 1800
    updown_secret: testsecrettestsecret

    [write_permissions]
    */*@*/*: demo

    [read_permissions]
    */*@*/*: *

    [users]
    demo: demo
    """)

BUILD_TYPES = ("Release", "Debug", "RelWithDebInfo")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _conan(env, *args):
    res = subprocess.run(
        ["conan", *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    if res.returncode:
        raise RuntimeError("conan %s failed:\n%s" % (" ".join(args), res.stdout))


@pytest.fixture(scope="session")
def conan_env(tmp_path_factory):
    home = tmp_path_factory.mktemp("conan_home")
    server_home = tmp_path_factory.mktemp("server_home")
    port = _free_port()

    # --- conan_server (ships with conan 1.x) -----------------------------
    # The port can only be set through server.conf, so write a minimal one
    # before the first start.  Reads are public, writes need demo/demo.
    (server_home / "server.conf").write_text(SERVER_CONF.format(port=port))
    server = subprocess.Popen(
        ["conan_server", "-d", str(server_home)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        server.kill()
        raise RuntimeError("conan_server did not start")

    env = dict(
        os.environ,
        CONAN_USER_HOME=str(home),
        CONAN_REVISIONS_ENABLED="0",
        CONAN_LOGIN_USERNAME="demo",
        CONAN_PASSWORD="demo",
    )
    profile_dir = home / ".conan" / "profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "default").write_text(PROFILE)

    _conan(env, "remote", "clean")
    _conan(env, "remote", "add", "test", "http://127.0.0.1:%d" % port)

    # --- create + upload packages ----------------------------------------
    work = tmp_path_factory.mktemp("recipes")

    def create(name, version, requires=(), build_types=BUILD_TYPES, header_only=False):
        d = work / ("%s-%s" % (name, version))
        d.mkdir()
        if header_only:
            (d / "conanfile.py").write_text(HEADER_ONLY)
            _conan(env, "create", str(d))
        else:
            req = "requires = (%s)" % "".join('"%s", ' % r for r in requires)
            (d / "conanfile.py").write_text(RECIPE.format(name=name, version=version, requires=req))
            for bt in build_types:
                # --build=missing lets dependencies whose binaries we deliberately
                # did not create (libbar Debug) be built locally; those local
                # binaries are never uploaded because each reference is
                # uploaded right after its own create, and the cache is wiped
                # at the end.
                _conan(env, "create", str(d), "-s", "build_type=%s" % bt, "--build=missing")
        _conan(env, "upload", "%s/%s" % (name, version), "-r", "test", "--all", "-c")

    create("libdeep", "1.0")
    create("libbar", "1.0", requires=("libdeep/1.0",), build_types=("Release",))
    create("libbar", "1.1", requires=("libdeep/1.0",), build_types=("Release",))
    create("libfoo", "1.0", requires=("libbar/1.0",))
    create("libqux", "1.0")
    create("libqux", "1.1")
    create("libbaz", "2.0", requires=("libqux/1.0",))
    create("hdr", "1.0", header_only=True)

    # Wipe the local cache so binaries can only come from the remote.
    _conan(env, "remove", "*", "-f")

    consumer = work / "consumer"
    consumer.mkdir()
    (consumer / "conanfile.py").write_text(CONSUMER)

    old_environ = dict(os.environ)
    os.environ.update(env)
    try:
        yield {"conanfile": str(consumer / "conanfile.py"), "home": home}
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
        server.terminate()
        server.wait(timeout=10)
        shutil.rmtree(work, ignore_errors=True)


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
    text = conandeps.render_text(report)
    assert re.search(r"hdr/1\.0\s+-\s+host", text)
    assert re.search(r"libbar/1\.1\s+libfoo\s+host", text)
    assert re.search(r"libdeep/1\.0\s+libfoo -> libbar\s+host", text)
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


def _mini_report(deps, overrides=()):
    """Hand-built single-build-type Report for renderer ordering tests."""
    return conandeps.Report("conanfile.py", "test", {}, ["Release"], deps, list(overrides), {}, [])


def _mini_dep(ref, direct=False, paths=(), status="Remote"):
    dep = conandeps.Dep(ref=ref, context="host", direct=direct)
    dep.paths = [list(p) for p in paths]
    dep.binaries = {"Release": (status, "f" * 40, "test" if status == "Remote" else None)}
    return dep


def test_dependency_rows_sort_by_via_then_name():
    """Text and HTML dependency rows sort on the via column, then package name.

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
    text = conandeps.render_text(_mini_report(deps))
    assert re.findall(r"^(\w+)/1\.0", text.split("Overrides")[0], re.M) == expected


def test_overrides_before_missing_and_both_sorted():
    """Overrides section precedes missing binaries; both sort alphabetically."""
    deps = {
        "zeta#host": _mini_dep("zeta/1.0", direct=True, status="Missing"),
        "alpha#host": _mini_dep("alpha/1.0", direct=True, status="Missing"),
    }
    overrides = [
        conandeps.Override("zeta/1.0", "x/1.0", "x/1.1", "your conanfile", False),
        conandeps.Override("alpha/1.0", "y/1.0", "y/1.1", "your conanfile", True),
    ]
    page = conandeps.render_html(_mini_report(deps, overrides))
    assert page.index("<h2>Overrides") < page.index("<h2>Missing binaries")
    ov_table = page.split("<h2>Overrides")[1].split("<h2>")[0]
    assert re.findall(r"<td>(\w+)/1\.0</td>", ov_table) == ["alpha", "zeta"]
    missing_table = page.split("<h2>Missing binaries")[1].split("<h2>")[0]
    assert re.findall(r"<td>(\w+)/1\.0</td>", missing_table) == ["alpha", "zeta"]

    text = conandeps.render_text(_mini_report(deps, overrides))
    assert text.index("Overrides") < text.index("Missing binaries")
    ov_table = text.split("Overrides")[1].split("Missing binaries")[0]
    assert re.findall(r"^(\w+)/1\.0", ov_table, re.M) == ["alpha", "zeta"]
    missing_table = text.split("Missing binaries")[1].split("Dependency hierarchy")[0]
    assert re.findall(r"^(\w+)/1\.0", missing_table, re.M) == ["alpha", "zeta"]


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
    assert "not available" in text and "in local cache only" in text
    assert 'class="cacheonly"' in conandeps.render_html(report)
    _conan(env, "remove", "libbar/1.1@", "-f")  # leave the cache as other tests expect


def test_overrides(report):
    ov = {(o.package, o.old, o.new, o.by, o.explicit) for o in report.overrides}
    assert ov == {
        ("libfoo/1.0", "libbar/1.0", "libbar/1.1", "your conanfile", True),
        ("libbaz/2.0", "libqux/1.0", "libqux/1.1", "your conanfile", False),
    }


def test_text_and_html_render(report, tmp_path):
    text = conandeps.render_text(report)
    assert "libbar/1.1" in text and "MISSING" in text
    table = text.split("Missing binaries")[0]
    assert table.count("test") >= 5 * 3 - 2  # every available cell names the remote
    assert "\033[" not in text  # no ANSI codes unless asked for
    # Missing binaries and overrides are tables with a header row.
    assert "package     build type      package_id" in text
    assert re.search(r"libbar/1\.1\s+Debug\s+[0-9a-f]{40}\s+no binary anywhere", text)
    assert "package     wanted      forced to   by              kind" in text
    assert "libfoo/1.0  libbar/1.0  libbar/1.1  your conanfile  explicit (override=True)" in text
    assert "overrides libbar/1.0 (explicit" in text  # tree edge annotation
    # Tree lines carry the full path of their branch; direct nodes do not.
    tree = text.split("Dependency hierarchy")[1]
    assert re.search(r"`-- libdeep/1\.0  \[libfoo -> libbar -> libdeep\]", tree)
    assert re.search(r"\|-- libfoo/1\.0\n", tree)

    coloured = conandeps.render_text(report, color=True)
    assert "\033[1;31mMISSING\033[0m" in coloured and "\033[32mtest\033[0m" in coloured
    # Tree tags: path dim, override yellow, MISSING bold red.
    assert "\033[2mlibfoo -> libbar -> libdeep\033[0m" in coloured
    assert "\033[33moverrides libbar/1.0 (explicit by your conanfile)\033[0m" in coloured
    assert "\033[1;31mMISSING: Debug,RelWithDebInfo\033[0m" in coloured
    # Colour codes must not break column alignment: same visible layout.
    strip = conandeps._ANSI_RE.sub
    assert strip("", coloured) == text
    assert "`-- " in text or "|-- " in text  # tree drawn
    page = conandeps.render_html(report)
    assert page.startswith("<!DOCTYPE html>") and 'class="missing"' in page
    assert page.count("<table>") == 3  # dependencies, missing binaries, overrides
    # The HTML tree carries the same tags as the text tree, coloured by kind.
    assert '[<span class="path">libfoo -&gt; libbar -&gt; libdeep</span>]' in page
    assert '<span class="new">overrides libbar/1.0 (explicit by your conanfile)</span>' in page
    assert '<span class="missing">MISSING: Debug,RelWithDebInfo</span>' in page
    assert 'class="explicit"' in page and 'class="implicit"' in page
    assert "<script" not in page  # self-contained, static


def test_cli_exit_code(conan_env, tmp_path, capsys):
    out = tmp_path / "r.html"
    rc = conandeps.main([conan_env["conanfile"], "-r", "test", "--html", str(out)])
    assert rc == 1  # missing binaries -> non-zero so CI can fail on it
    assert out.exists()
    captured = capsys.readouterr()
    assert "conandeps report" in captured.out
