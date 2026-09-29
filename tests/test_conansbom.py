"""Tests for conansbom.

Rendering is tested on hand-built ``Sbom`` objects; one integration test runs
against the shared ``conan_env`` fixture (see ``conftest.py``) with a project
that adds a licensed build tool on top of the fixture's packages.  Every
CycloneDX document is validated against the CycloneDX 1.6 JSON schema, every
SPDX document with the official spdx-tools validator.
"""

import json
import os
from collections import OrderedDict
from pathlib import Path

import pytest
from conftest import RECIPE, _conan
from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator
from spdx_tools.common.spdx_licensing import spdx_licensing
from spdx_tools.spdx.parser.parse_anything import parse_file
from spdx_tools.spdx.validation.document_validator import validate_full_spdx_document

import conansbom
from conansbom import Component, Sbom


def _validate(doc: dict) -> None:
    """Fail with the schema error when ``doc`` is not valid CycloneDX 1.6."""
    error = JsonStrictValidator(SchemaVersion.V1_6).validate_str(json.dumps(doc))
    assert error is None, error


def _validate_spdx(doc: dict, tmp_path: Path) -> None:
    """Fail with the validator's messages when ``doc`` is not valid SPDX 2.3."""
    path = tmp_path / "doc.spdx.json"
    path.write_text(json.dumps(doc))
    messages = validate_full_spdx_document(parse_file(str(path)))
    assert not messages, [m.validation_message for m in messages]


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
        build_requires=["cmake/3.29@acme/stable"],
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
    # A known SPDX identifier is an id, not just a name.
    assert subject["licenses"] == [{"license": {"id": "Apache-2.0", "acknowledgement": "declared"}}]
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
    assert cmake["licenses"] == [
        {"license": {"id": "BSD-3-Clause", "acknowledgement": "declared"}},
        {"license": {"name": "Proprietary", "acknowledgement": "declared"}},
    ]
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


def test_require_licenses_exit_code(monkeypatch, capsys):
    """Only with --require-licenses does a missing license change the exit code."""
    sbom = _hand_built()  # hdr/1.0 declares no license
    monkeypatch.setattr(conansbom, "build_sbom", lambda *args: sbom)
    assert conansbom.main(["conanfile.py"]) == 0
    assert "conansbom: WARN: no license declared: hdr/1.0\n" in capsys.readouterr().err
    assert conansbom.main(["conanfile.py", "--require-licenses"]) == 1
    assert "conansbom: error: no license declared: hdr/1.0\n" in capsys.readouterr().err

    sbom.components["hdr/1.0"].licenses = ["BSL-1.0"]
    assert conansbom.main(["conanfile.py", "--require-licenses"]) == 0
    assert "no license declared" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "declared, expected",
    [
        ("MIT", "MIT"),
        ("mit", "MIT"),  # identifiers match case-insensitively; the list's case wins
        ("GPL-2.0", "GPL-2.0"),  # deprecated, but recipes still declare it
        ("GPL-2.0+", "GPL-2.0+"),
        ("MIT or apache-2.0", "MIT OR Apache-2.0"),
        (
            "GPL-2.0-or-later WITH Classpath-exception-2.0",
            "GPL-2.0-or-later WITH Classpath-exception-2.0",
        ),
        ("(MIT OR BSD-3-Clause) AND Zlib", "(MIT OR BSD-3-Clause) AND Zlib"),
        ("Proprietary", None),
        ("LicenseRef-acme", None),  # a recipe cannot supply the text for it
        ("MIT OR", None),
        ("(MIT", None),
        ("MIT)", None),
        ("MIT Apache-2.0", None),
        ("MIT WITH Zlib", None),  # WITH takes an exception, not a license
        ("Classpath-exception-2.0", None),  # an exception on its own is no license
        ("(MIT) WITH Classpath-exception-2.0", None),
        ("", None),
    ],
)
def test_spdx_expression(declared, expected):
    assert conansbom.spdx_expression(declared) == expected


def test_embedded_spdx_list_is_current():
    """Every SPDX identifier the validator knows must be in the embedded list.

    When this fails, a newer license-expression (pulled in by spdx-tools)
    knows identifiers the embedded SPDX list lacks: regenerate the list (see
    CLAUDE.md).  The other direction is fine - the official list is often
    ahead of the validator's copy.
    """
    known = {k for k in spdx_licensing.known_symbols if not k.startswith("LicenseRef-")}
    embedded = set(conansbom._SPDX_LICENSES) | set(conansbom._SPDX_EXCEPTIONS)
    assert sorted(known - embedded) == []


def test_cyclonedx_compound_license_is_one_expression():
    """CycloneDX cannot mix license entries and an expression in one list."""
    assert conansbom._cdx_licenses(["MIT OR Apache-2.0", "ACME EULA"]) == [
        {
            "expression": "(MIT OR Apache-2.0) AND LicenseRef-ACME-EULA",
            "acknowledgement": "declared",
        }
    ]
    # "Apache-2.0+" is an expression (a license with "or later"), but "GPL-2.0+"
    # is itself a (deprecated) identifier on the SPDX list, so it stays an id.
    assert conansbom._cdx_licenses(["Apache-2.0+"]) == [
        {"expression": "Apache-2.0+", "acknowledgement": "declared"}
    ]
    assert conansbom._cdx_licenses(["GPL-2.0+"]) == [
        {"license": {"id": "GPL-2.0+", "acknowledgement": "declared"}}
    ]
    sbom = _hand_built()
    sbom.components["zlib/1.3"].licenses = ["GPL-2.0+", "MIT OR Apache-2.0"]
    sbom.components["hdr/1.0"].licenses = ["Apache-2.0+"]
    _validate(conansbom.to_cyclonedx(sbom))


def test_spdx_document(tmp_path):
    doc = conansbom.to_spdx(
        _hand_built(),
        namespace="https://spdx.org/spdxdocs/conansbom-test",
        timestamp="2026-09-29T12:00:00Z",
    )
    _validate_spdx(doc, tmp_path)
    assert doc["spdxVersion"] == "SPDX-2.3"
    assert doc["creationInfo"]["comment"] == "Conan settings: build_type=Release, os=Linux"
    pkgs = {p["name"]: p for p in doc["packages"]}
    project, cmake, hdr = pkgs["myproject"], pkgs["cmake"], pkgs["hdr"]
    assert project["primaryPackagePurpose"] == "APPLICATION"
    assert project["licenseDeclared"] == "Apache-2.0"
    # A tuple means all apply; a non-SPDX license is a LicenseRef with its text.
    assert cmake["licenseDeclared"] == "BSD-3-Clause AND LicenseRef-Proprietary"
    assert doc["hasExtractedLicensingInfos"] == [
        {
            "licenseId": "LicenseRef-Proprietary",
            "name": "Proprietary",
            "extractedText": "Proprietary",
            "comment": "Declared by the Conan recipe; not an SPDX license identifier.",
        }
    ]
    assert hdr["licenseDeclared"] == "NOASSERTION"  # nothing declared, nothing concluded
    assert cmake["comment"].startswith("Build only")
    assert "comment" not in hdr
    assert cmake["externalRefs"][0]["referenceLocator"] == (
        "pkg:conan/cmake@3.29?channel=stable&user=acme"
    )
    assert pkgs["zlib"]["sourceInfo"] == (
        "Conan recipe repository: https://github.com/conan-io/conan-center-index"
    )

    rels = {
        (r["spdxElementId"], r["relationshipType"], r["relatedSpdxElement"])
        for r in doc["relationships"]
    }
    root_id, cmake_id = project["SPDXID"], cmake["SPDXID"]
    assert ("SPDXRef-DOCUMENT", "DESCRIBES", root_id) in rels
    assert (root_id, "DEPENDS_ON", pkgs["zlib"]["SPDXID"]) in rels
    assert (cmake_id, "BUILD_DEPENDENCY_OF", root_id) in rels  # a build requirement
    assert (cmake_id, "DEPENDS_ON", hdr["SPDXID"]) in rels  # the tool's own requirement


def test_spdx_unnamed_project(tmp_path):
    sbom = _hand_built()
    sbom.root = Component("consumer", "consumer", None, is_package=False, requires=["zlib/1.3"])
    doc = conansbom.to_spdx(sbom)
    _validate_spdx(doc, tmp_path)
    project = doc["packages"][0]
    assert project["SPDXID"] == "SPDXRef-Project" and "externalRefs" not in project
    assert doc["documentNamespace"].startswith("https://spdx.org/spdxdocs/conansbom-consumer-")


def test_html_page():
    sbom = _hand_built()
    sbom.components["zlib/1.3"].homepage = "javascript:alert(1)"  # never a link
    page = conansbom.to_html(sbom, timestamp="2026-09-29T12:00:00Z")
    assert page.startswith("<!DOCTYPE html>") and "<script" not in page
    assert "<title>SBOM: myproject/1.2</title>" in page
    assert "<b>license:</b> Apache-2.0" in page
    assert "<b>build:</b> build_type=Release, os=Linux" in page
    assert 'href="javascript' not in page and "javascript:alert(1)" in page

    licenses = page.split("<h2>Licenses</h2>")[1].split("<h2>")[0]
    # One row per declared license: text, SPDX form, where it ships, where it only builds.
    assert (
        "<tr><td>BSD-3-Clause</td><td>BSD-3-Clause</td><td>-</td>"
        "<td>cmake/3.29@acme/stable</td></tr>" in licenses
    )
    assert '<td>Proprietary</td><td><span class="na">no</span></td>' in licenses
    # Unlicensed packages come last, flagged.
    assert licenses.index("Zlib") < licenses.index('<span class="missing">none declared</span>')

    shipped = page.split("<h2>Shipped (2)</h2>")[1].split("<h2>")[0]
    assert "<td>hdr/1.0</td>" in shipped and "<td>zlib/1.3</td>" in shipped
    assert "cmake" not in shipped
    build = page.split("<h2>Build only (1)</h2>")[1]
    assert "<td>cmake/3.29@acme/stable</td><td>BSD-3-Clause<br>Proprietary</td>" in build


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

    spdx = tmp_path / "sbom.spdx.json"
    assert conansbom.main([sbom_project, "-r", "test", "-f", "spdx", "--out", str(spdx)]) == 0
    doc = json.loads(spdx.read_text())
    _validate_spdx(doc, tmp_path)
    pkgs = {p["name"]: p for p in doc["packages"]}
    assert pkgs["tool"]["licenseDeclared"] == "MIT"
    assert pkgs["tool"]["comment"].startswith("Build only")

    page = tmp_path / "sbom.html"
    assert conansbom.main([sbom_project, "-r", "test", "--format", "html", "--out", str(page)]) == 0
    assert "<h2>Build only (2)</h2>" in page.read_text()  # tool and toolhelper

    # --require-licenses fails the run on the fixture's unlicensed packages, but
    # still writes the document, so the failing build's SBOM can be inspected.
    strict = tmp_path / "strict.json"
    assert (
        conansbom.main([sbom_project, "-r", "test", "--require-licenses", "--out", str(strict)])
        == 1
    )
    _validate(json.loads(strict.read_text()))
    assert "conansbom: error: no license declared: " in capsys.readouterr().err

    assert conansbom.main([str(Path(tmp_path) / "nope" / "conanfile.py")]) == 2
    assert "conansbom: error:" in capsys.readouterr().err
