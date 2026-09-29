#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = ["conan>=1.66,<2"]
# ///
"""conansbom - CycloneDX software bill of materials for a Conan 1.x project.

Given a conanfile.py this tool writes a CycloneDX 1.6 JSON SBOM of every
package in its dependency graph, for inventory and license compliance:

  * the project itself is the SBOM's subject (``metadata.component``),
  * every package is one component with its purl, the license(s) its recipe
    declares, its description and homepage/recipe URLs,
  * packages that end up in the product have ``scope: required``; packages
    only used to build it (build requirements and everything they pull in)
    have ``scope: excluded``,
  * ``dependencies`` records every edge of the graph, build requirements
    included.

Why not an existing tool
------------------------
Tools that read a Conan 1 lockfile (e.g. Syft) cannot report licenses - the
lockfile does not carry them - and do not tell build requirements from what
ships.  cyclonedx-conan, the one Conan 1 tool that read recipes, was archived
in 2023.  Conan 2's built-in SBOM tool does not exist in Conan 1.

How it works
------------
The graph is resolved once through the same ``conan info`` code path
``conan install`` uses (shared with ``conandeps``, see
``conandeps._resolve_graph``), so profile, settings, options and overrides
are exactly those of a real install of that configuration.  Each node's
recipe is already loaded at that point, so the declared ``license``,
``description``, ``homepage`` and ``url`` are read straight off it.

"Runtime" is reachability: a package is ``required`` when the project reaches
it without crossing a build-requirement edge.  That is independent of whether
a separate build profile is used, and it puts a build tool's own
requirements on the build side too.  A package that is both (a header-only
library also used by a code generator, say) is ``required``: it ships.

A reference used in both the host and the build context is one component,
not two: it is one piece of software, and two components with the same purl
break tools such as Dependency-Track.  Its contexts and each context's
package_id are recorded as properties.

Licenses are reported as declared by the recipe, never guessed or mapped:
each is a named license with ``acknowledgement: declared``.  Packages whose
recipe declares no license are listed on stderr: those are the ones a
compliance review has to chase.

Exit status: ``0`` SBOM written, ``2`` Conan could not build the graph.

Targets Conan 1.66.  Uses only the standard library, the Conan API and
``conandeps`` from this repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import quote

# Importing conandeps also installs its warning filters, which have to be in
# place before Conan is imported (Conan imports stay deferred for that reason).
import conandeps

SPEC_VERSION = "1.6"


@dataclass
class Component:
    """One package reference in the graph, merged over its contexts."""

    ref: str  # "name/version[@user/channel]"
    name: str
    version: Optional[str]
    user: Optional[str] = None
    channel: Optional[str] = None
    licenses: List[str] = field(default_factory=list)
    description: Optional[str] = None
    homepage: Optional[str] = None
    url: Optional[str] = None  # the recipe's repository, in Conan 1 terms
    # context ("host"/"build") -> package_id; ordered host first
    package_ids: Dict[str, str] = field(default_factory=dict)
    runtime: bool = False  # reachable from the project without a build-requirement edge
    requires: List[str] = field(default_factory=list)  # refs, build requirements included
    # False only for a project conanfile that names no package: it has no purl.
    is_package: bool = True

    @property
    def purl(self) -> Optional[str]:
        if not self.is_package:
            return None
        return _purl(self.name, self.version, self.user, self.channel)

    @property
    def bom_ref(self) -> str:
        """Unique id inside the document: the purl, "project" for an unnamed project."""
        return self.purl or "project"


@dataclass
class Sbom:
    root: Component  # the project; runtime/package_ids unused
    components: "OrderedDict[str, Component]"  # keyed by ref
    settings: Dict[str, str]  # the profile's settings, to say which build this describes


def _purl(name: str, version: Optional[str], user=None, channel=None) -> str:
    """Package URL for a Conan reference, per the purl spec's conan type.

    ``user`` and ``channel`` are qualifiers, only present when the package has
    them; qualifiers are sorted by key as the spec requires.
    """
    purl = "pkg:conan/%s" % quote(name, safe="")
    if version:
        purl += "@%s" % quote(version, safe="")
    qualifiers = [(k, v) for k, v in (("channel", channel), ("user", user)) if v]
    if qualifiers:
        purl += "?" + "&".join("%s=%s" % (k, quote(v, safe="")) for k, v in qualifiers)
    return purl


def _licenses(value) -> List[str]:
    """A recipe's ``license`` attribute (None, a string or a tuple) as a list."""
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


def _text(value) -> Optional[str]:
    """An optional recipe attribute as a stripped string, None when unset."""
    return (str(value).strip() or None) if value else None


def _component(ref, conanfile) -> Component:
    return Component(
        ref=str(ref),
        name=ref.name,
        version=str(ref.version) if ref.version else None,
        user=ref.user,
        channel=ref.channel,
        licenses=_licenses(getattr(conanfile, "license", None)),
        description=_text(getattr(conanfile, "description", None)),
        homepage=_text(getattr(conanfile, "homepage", None)),
        url=_text(getattr(conanfile, "url", None)),
    )


def _runtime_nodes(root) -> set:
    """Ids of the nodes reachable from ``root`` without a build-requirement edge."""
    seen = set()
    stack = [root]
    while stack:
        node = stack.pop()
        for edge in node.dependencies:
            if not edge.build_require and id(edge.dst) not in seen:
                seen.add(id(edge.dst))
                stack.append(edge.dst)
    return seen


def collect(graph, root_conanfile, conanfile_path: str) -> Sbom:
    """Turn a resolved Conan graph into an ``Sbom``."""
    root_node = graph.root
    runtime = _runtime_nodes(root_node)
    components: "OrderedDict[str, Component]" = OrderedDict()
    # Host before build, so a package in both contexts lists host first.
    nodes = sorted(
        (n for n in graph.nodes if n is not root_node), key=lambda n: n.context != "host"
    )
    for node in nodes:
        ref = str(node.ref)
        comp = components.get(ref)
        if comp is None:
            comp = components[ref] = _component(node.ref, node.conanfile)
        comp.package_ids.setdefault(node.context, node.package_id or "")
        comp.runtime = comp.runtime or id(node) in runtime
        for edge in node.dependencies:
            if str(edge.dst.ref) not in comp.requires:
                comp.requires.append(str(edge.dst.ref))

    name = getattr(root_conanfile, "name", None)
    if name:
        root = Component(
            ref=conandeps._root_ref(root_conanfile) or name,
            name=name,
            version=_text(getattr(root_conanfile, "version", None)),
            licenses=_licenses(getattr(root_conanfile, "license", None)),
            description=_text(getattr(root_conanfile, "description", None)),
            homepage=_text(getattr(root_conanfile, "homepage", None)),
            url=_text(getattr(root_conanfile, "url", None)),
        )
    else:
        # A consumer conanfile need not name itself; the directory it lives in
        # is the best name there is, and it gets no purl (it is not a package).
        folder = os.path.basename(os.path.dirname(os.path.abspath(conanfile_path)))
        root = Component(ref=folder, name=folder, version=None, is_package=False)
    # Sorted by reference, so two SBOMs of the same project diff cleanly.
    components = OrderedDict(sorted(components.items(), key=lambda kv: kv[0].lower()))
    root.requires = [str(e.dst.ref) for e in root_node.dependencies]
    try:
        settings = {k: str(v) for k, v in root_conanfile.settings.values_list}
    except Exception:  # a conanfile without settings
        settings = {}
    return Sbom(root, components, settings)


def build_sbom(
    conanfile: str,
    remote: Optional[str],
    profiles: List[str],
    settings: List[str],
    options: List[str],
    update: bool,
    verbose: bool = False,
) -> Sbom:
    log = conandeps._Tee(echo=verbose)
    api = conandeps._make_api(log)
    graph, root_conanfile = conandeps._resolve_graph(
        api, conanfile, remote, profiles, settings, options, update
    )
    return collect(graph, root_conanfile, conanfile)


# --------------------------------------------------------------------------- #
# CycloneDX
# --------------------------------------------------------------------------- #
def _tool_version() -> str:
    try:
        from importlib.metadata import version

        return version("conan-utils")
    except Exception:  # run from a checkout, not installed
        return "unknown"


def _cdx_component(comp: Component, is_root: bool = False) -> dict:
    out: dict = {
        "type": "application" if is_root else "library",
        "bom-ref": comp.bom_ref,
        "name": comp.name,
    }
    if comp.version:
        out["version"] = comp.version
    if comp.description:
        out["description"] = comp.description
    if not is_root:
        # "excluded" is CycloneDX's word for used but not delivered: build tools.
        out["scope"] = "required" if comp.runtime else "excluded"
    if comp.licenses:
        out["licenses"] = [
            {"license": {"name": lic, "acknowledgement": "declared"}} for lic in comp.licenses
        ]
    if comp.purl:
        out["purl"] = comp.purl
    refs = []
    if comp.homepage:
        refs.append({"type": "website", "url": comp.homepage})
    if comp.url:
        refs.append({"type": "vcs", "url": comp.url, "comment": "Conan recipe repository"})
    if refs:
        out["externalReferences"] = refs
    props = []
    if comp.package_ids:
        props.append({"name": "conan:context", "value": ",".join(comp.package_ids)})
        for context, package_id in comp.package_ids.items():
            if package_id:
                props.append({"name": "conan:package_id:%s" % context, "value": package_id})
    if props:
        out["properties"] = props
    return out


def to_cyclonedx(sbom: Sbom, serial: Optional[str] = None, timestamp: Optional[str] = None) -> dict:
    """The SBOM as a CycloneDX 1.6 JSON document (a dict ready for json.dump).

    ``serial`` and ``timestamp`` are generated when not given; tests pass
    fixed ones.
    """
    by_ref = {ref: c.bom_ref for ref, c in sbom.components.items()}
    dependencies = [
        {"ref": c.bom_ref, "dependsOn": [by_ref[r] for r in c.requires if r in by_ref]}
        for c in [sbom.root] + list(sbom.components.values())
    ]
    metadata: dict = {
        "timestamp": timestamp
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "tools": {
            "components": [{"type": "application", "name": "conansbom", "version": _tool_version()}]
        },
        "component": _cdx_component(sbom.root, is_root=True),
    }
    if sbom.settings:
        # Which build this bill of materials describes.
        metadata["properties"] = [
            {"name": "conan:setting:%s" % k, "value": v} for k, v in sorted(sbom.settings.items())
        ]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        "serialNumber": serial or "urn:uuid:%s" % uuid.uuid4(),
        "version": 1,
        "metadata": metadata,
        "components": [_cdx_component(c) for c in sbom.components.values()],
        "dependencies": dependencies,
    }


def unlicensed(sbom: Sbom) -> List[str]:
    """References whose recipe declares no license, the project included."""
    comps = [sbom.root] + list(sbom.components.values())
    return [c.ref for c in comps if not c.licenses]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="conansbom",
        description="Write a CycloneDX %s JSON SBOM (packages, declared licenses, "
        "dependency graph) for a Conan 1.x conanfile." % SPEC_VERSION,
    )
    p.add_argument("conanfile", help="path to conanfile.py (or a directory containing one)")
    p.add_argument(
        "-r",
        "--remote",
        help="remote to fetch missing recipes from (e.g. myremote); default: all configured",
    )
    p.add_argument(
        "-pr",
        "--profile",
        action="append",
        default=[],
        help="profile to use (repeatable, same as conan -pr)",
    )
    p.add_argument(
        "-s", "--settings", action="append", default=[], help="extra settings, key=value"
    )
    p.add_argument(
        "-o", "--options", action="append", default=[], help="extra options, pkg:key=value"
    )
    p.add_argument("-u", "--update", action="store_true", help="check for newer recipes (conan -u)")
    p.add_argument("--out", metavar="FILE", help="write the SBOM to FILE (default: stdout)")
    p.add_argument("-v", "--verbose", action="store_true", help="stream Conan's output to stderr")
    args = p.parse_args(argv)

    try:
        sbom = build_sbom(
            args.conanfile,
            args.remote,
            args.profile,
            args.settings,
            args.options,
            args.update,
            args.verbose,
        )
    except Exception as exc:  # ConanException and friends
        sys.stderr.write("conansbom: error: %s\n" % exc)
        return 2

    text = json.dumps(to_cyclonedx(sbom), indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    missing = unlicensed(sbom)
    if missing:
        sys.stderr.write("conansbom: WARN: no license declared: %s\n" % ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
