"""Tests for conansbom.

Rendering is tested on hand-built ``Sbom`` objects; one integration test runs
against the shared ``conan_env`` fixture (see ``conftest.py``) with a project
that adds a licensed build tool on top of the fixture's packages.  Every
document is validated against the CycloneDX 1.6 JSON schema.
"""

import json
import os
from collections import OrderedDict
from pathlib import Path

import pytest
from conftest import RECIPE, _conan
from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator

import conansbom
from conansbom import Component, Sbom


def _validate(doc: dict) -> None:
    """Fail with the schema error when ``doc`` is not valid CycloneDX 1.6."""
    error = JsonStrictValidator(SchemaVersion.V1_6).validate_str(json.dumps(doc))
    assert error is None, error


def _components(doc: dict) -> dict:
    return {c["name"]: c for c in doc["components"]}


def _props(component: dict) -> dict:
    return {p["name"]: p["value"] for p in component.get("properties", [])}


def test_purl():
    assert conansbom._purl("zlib", "1.3") == "pkg:conan/zlib@1.3"
    # Qualifiers sorted by key, as the purl spec requires.
    assert (
        conansbom._purl("openssl", "3.0.3", user="bincrafters", channel="stable")
        == "pkg:conan/openssl@3.0.3?channel=stable&user=bincrafters"
    )
    assert conansbom._purl("a b", "1+2") == "pkg:conan/a%20b@1%2B2"


def test_licenses_as_declared():
    assert conansbom._licenses(None) == []
    assert conansbom._licenses("MIT") == ["MIT"]
    assert conansbom._licenses(("MIT", " BSL-1.0 ", "")) == ["MIT", "BSL-1.0"]


def _hand_built() -> Sbom:
    """A project with a runtime library, a build tool, and a package used as both."""
    comps = OrderedDict()
    comps["zlib/1.3"] = Component(
        "zlib/1.3",
        "zlib",
        "1.3",
        licenses=["Zlib"],
        description="Compression",
        homepage="https://zlib.net",
        url="https://github.com/conan-io/conan-center-index",
        package_ids={"host": "a" * 40},
        runtime=True,
    )
    comps["hdr/1.0"] = Component(
        "hdr/1.0", "hdr", "1.0", package_ids={"host": "b" * 40, "build": "c" * 40}, runtime=True
    )
    comps["cmake/3.29@acme/stable"] = Component(
        "cmake/3.29@acme/stable",
        "cmake",
        "3.29",
        user="acme",
        channel="stable",
        licenses=["BSD-3-Clause", "Proprietary"],
        package_ids={"build": "d" * 40},
        runtime=False,
        requires=["hdr/1.0"],
    )
    root = Component(
        "myproject/1.2",
        "myproject",
        "1.2",
        licenses=["Apache-2.0"],
        requires=["zlib/1.3", "hdr/1.0", "cmake/3.29@acme/stable"],
    )
    return Sbom(root, comps, {"os": "Linux", "build_type": "Release"})


def test_cyclonedx_document():
    doc = conansbom.to_cyclonedx(
        _hand_built(),
        serial="urn:uuid:3e671687-395b-41f5-a30f-a58921a69b79",
        timestamp="2026-09-29T12:00:00Z",
    )
    _validate(doc)
    assert doc["specVersion"] == "1.6"

    # The project is the subject of the SBOM, not one of its components.
    subject = doc["metadata"]["component"]
    assert subject["name"] == "myproject" and subject["purl"] == "pkg:conan/myproject@1.2"
    assert subject["licenses"] == [
        {"license": {"name": "Apache-2.0", "acknowledgement": "declared"}}
    ]
    assert "myproject" not in _components(doc)
    assert {"name": "conan:setting:os", "value": "Linux"} in doc["metadata"]["properties"]

    comps = _components(doc)
    assert comps["zlib"]["scope"] == "required"
    assert comps["zlib"]["externalReferences"] == [
        {"type": "website", "url": "https://zlib.net"},
        {
            "type": "vcs",
            "url": "https://github.com/conan-io/conan-center-index",
            "comment": "Conan recipe repository",
        },
    ]
    # A build tool is used but not delivered.
    cmake = comps["cmake"]
    assert cmake["scope"] == "excluded"
    assert cmake["purl"] == "pkg:conan/cmake@3.29?channel=stable&user=acme"
    assert [lic["license"]["name"] for lic in cmake["licenses"]] == ["BSD-3-Clause", "Proprietary"]
    # A package in both contexts is one component that ships, with both ids.
    hdr = comps["hdr"]
    assert hdr["scope"] == "required" and "licenses" not in hdr
    assert _props(hdr) == {
        "conan:context": "host,build",
        "conan:package_id:host": "b" * 40,
        "conan:package_id:build": "c" * 40,
    }

    deps = {d["ref"]: d["dependsOn"] for d in doc["dependencies"]}
    assert deps["pkg:conan/myproject@1.2"] == [
        "pkg:conan/zlib@1.3",
        "pkg:conan/hdr@1.0",
        "pkg:conan/cmake@3.29?channel=stable&user=acme",
    ]
    assert deps["pkg:conan/cmake@3.29?channel=stable&user=acme"] == ["pkg:conan/hdr@1.0"]
    assert deps["pkg:conan/zlib@1.3"] == []  # every component has an entry


def test_unnamed_project_has_no_purl():
    sbom = _hand_built()
    sbom.root = Component("consumer", "consumer", None, is_package=False, requires=["zlib/1.3"])
    doc = conansbom.to_cyclonedx(sbom)
    _validate(doc)
    subject = doc["metadata"]["component"]
    assert subject["bom-ref"] == "project" and "purl" not in subject
    assert doc["dependencies"][0] == {"ref": "project", "dependsOn": ["pkg:conan/zlib@1.3"]}
    assert doc["serialNumber"].startswith("urn:uuid:")


def test_unlicensed_lists_what_review_has_to_chase():
    assert conansbom.unlicensed(_hand_built()) == ["hdr/1.0"]


PROJECT = """\
from conans import ConanFile

class Project(ConanFile):
    name = "sbomtest"
    version = "0.1"
    license = "Apache-2.0"
    description = "SBOM test project"
    settings = "os", "compiler", "build_type", "arch"
    requires = ("libfoo/1.0", "hdr/1.0")
    build_requires = ("tool/1.0",)
"""


@pytest.fixture(scope="module")
def sbom_project(conan_env, tmp_path_factory):
    """sbomtest/0.1: the fixture's libfoo and hdr, plus a licensed build tool.

    tool/1.0 requires toolhelper/1.0, so the build side is more than one
    package deep.  Both are uploaded like the fixture's packages and then
    removed from the cache, so the graph is resolved from the remote.
    """
    env = dict(os.environ)
    work = tmp_path_factory.mktemp("sbom")
    recipes = {
        "toolhelper": 'license = "BSD-3-Clause"',
        "tool": 'requires = ("toolhelper/1.0",)\n    license = "MIT"\n'
        '    homepage = "https://tool.example"',
    }
    for name, extra in recipes.items():
        d = work / name
        d.mkdir()
        (d / "conanfile.py").write_text(RECIPE.format(name=name, version="1.0", requires=extra))
        _conan(env, "create", str(d))
        _conan(env, "upload", "%s/1.0" % name, "-r", "test", "--all", "-c")
    for name in recipes:
        _conan(env, "remove", "%s/1.0" % name, "-f")
    project = work / "project"
    project.mkdir()
    (project / "conanfile.py").write_text(PROJECT)
    return str(project / "conanfile.py")


def test_sbom_from_the_real_graph(sbom_project):
    sbom = conansbom.build_sbom(sbom_project, "test", [], [], [], update=False)
    assert sbom.root.ref == "sbomtest/0.1" and sbom.root.licenses == ["Apache-2.0"]
    assert set(sbom.components) == {
        "libfoo/1.0",
        "libbar/1.0",
        "libdeep/1.0",
        "hdr/1.0",
        "tool/1.0",
        "toolhelper/1.0",
    }
    runtime = {ref for ref, c in sbom.components.items() if c.runtime}
    # The build tool's own requirement is build-only too.
    assert runtime == {"libfoo/1.0", "libbar/1.0", "libdeep/1.0", "hdr/1.0"}
    assert sbom.components["tool/1.0"].licenses == ["MIT"]
    assert sbom.components["tool/1.0"].homepage == "https://tool.example"
    assert sbom.components["libfoo/1.0"].requires == ["libbar/1.0"]
    assert sbom.settings["build_type"] == "Release"

    doc = conansbom.to_cyclonedx(sbom)
    _validate(doc)
    comps = _components(doc)
    assert comps["tool"]["scope"] == comps["toolhelper"]["scope"] == "excluded"
    assert comps["libdeep"]["scope"] == "required"
    deps = {d["ref"]: d["dependsOn"] for d in doc["dependencies"]}
    assert "pkg:conan/tool@1.0" in deps["pkg:conan/sbomtest@0.1"]  # build edges kept
    assert deps["pkg:conan/tool@1.0"] == ["pkg:conan/toolhelper@1.0"]


def test_cli(sbom_project, tmp_path, capsys):
    out = tmp_path / "sbom.json"
    assert conansbom.main([sbom_project, "-r", "test", "--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    _validate(doc)
    assert doc["metadata"]["component"]["name"] == "sbomtest"
    err = capsys.readouterr().err
    # The fixture's packages declare no license; the review has to chase them.
    assert "conansbom: WARN: no license declared: " in err
    assert "libfoo/1.0" in err and "tool/1.0" not in err

    # Without --out the document is stdout, and nothing else is.
    assert conansbom.main([sbom_project, "-r", "test"]) == 0
    assert json.loads(capsys.readouterr().out)["bomFormat"] == "CycloneDX"

    assert conansbom.main([str(Path(tmp_path) / "nope" / "conanfile.py")]) == 2
    assert "conansbom: error:" in capsys.readouterr().err
