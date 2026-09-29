"""Shared test fixture: a throw-away local conan_server with a fake package graph.

Used by the tests of every tool in this repository, so the server is started
and the packages are uploaded once per test session.

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

libbar/1.2 is uploaded to the remote as well but required by nothing, so the
update check can report libbar/1.1 as outdated.

Everything runs in an isolated CONAN_USER_HOME so the developer's cache and
remotes are never touched.
"""

import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

# The tools are single top-level modules, not an installed package.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
        name = "myproject"
        version = "0.1"
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
    # Never in the graph (nothing requires it): exists only so the update
    # check can report libbar/1.1 as outdated.
    create("libbar", "1.2", requires=("libdeep/1.0",), build_types=("Release",))
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
