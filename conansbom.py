#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = ["conan>=1.66,<2"]
# ///
"""conansbom - software bill of materials for a Conan 1.x project.

Given a conanfile.py this tool writes an SBOM of every package in its
dependency graph, for inventory and license compliance, in one of three
formats (``--format``):

  * ``cyclonedx`` (default) - CycloneDX 1.6 JSON,
  * ``spdx`` - SPDX 2.3 JSON,
  * ``html`` - a self-contained page for people: a license overview, what
    ships and what is only used to build.

All three say the same things:

  * the project itself is the subject of the document,
  * every package is listed once, with its purl, the license(s) its recipe
    declares, its description and homepage/recipe URLs,
  * packages that end up in the product are told apart from packages only
    used to build it (build requirements and everything they pull in) -
    ``scope: required``/``excluded`` in CycloneDX, ``DEPENDS_ON`` versus
    ``BUILD_DEPENDENCY_OF`` relationships in SPDX,
  * the dependency graph, build requirements included.

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

"Runtime" is reachability: a package ships when the project reaches it
without crossing a build-requirement edge.  That is independent of whether a
separate build profile is used, and it puts a build tool's own requirements
on the build side too.  A package that is both (a header-only library also
used by a code generator, say) ships.

A reference used in both the host and the build context is listed once: it is
one piece of software, and two entries with the same purl break tools such
as Dependency-Track.

Licenses
--------
Licenses are reported as the recipe declares them, never guessed.  A
declared string that is a valid SPDX license expression over known
identifiers (``MIT``, ``GPL-2.0-or-later WITH Classpath-exception-2.0``,
``MIT OR Apache-2.0``) is used as one, in the SPDX list's canonical spelling.
Anything else (``Proprietary``, ``ACME commercial``) is kept verbatim: a
named license in CycloneDX, a ``LicenseRef-`` with the declared text in
SPDX.  Telling the two apart needs the SPDX license list, which is embedded
below (``SPDX_LIST_VERSION``) so the tool works offline; a test fails when
the validator the tests use knows an identifier the list lacks.  A recipe's
tuple of licenses means all of them apply, i.e. ``AND``.

Packages whose recipe declares no license (the project included) are listed
on stderr: those are the ones a compliance review has to chase.  With
``--require-licenses`` that list fails the run, for CI; the SBOM is still
written, so the failing build's document can be inspected.

Exit status: ``0`` SBOM written, ``1`` written but ``--require-licenses`` found
packages without a license, ``2`` Conan could not build the graph.

Targets Conan 1.66.  Uses only the standard library, the Conan API and
``conandeps`` from this repository.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

# Importing conandeps also installs its warning filters, which have to be in
# place before Conan is imported (Conan imports stay deferred for that reason).
import conandeps

CYCLONEDX_VERSION = "1.6"
SPDX_VERSION = "SPDX-2.3"
FORMATS = ("cyclonedx", "spdx", "html")


@dataclass
class Component:
    """One package reference in the graph, merged over its contexts."""

    ref: str  # "name/version[@user/channel]"
    name: str
    version: Optional[str]
    user: Optional[str] = None
    channel: Optional[str] = None
    licenses: List[str] = field(default_factory=list)  # as declared by the recipe
    description: Optional[str] = None
    homepage: Optional[str] = None
    url: Optional[str] = None  # the recipe's repository, in Conan 1 terms
    # context ("host"/"build") -> package_id; ordered host first
    package_ids: Dict[str, str] = field(default_factory=dict)
    runtime: bool = False  # reachable from the project without a build-requirement edge
    requires: List[str] = field(default_factory=list)  # refs, build requirements included
    build_requires: List[str] = field(default_factory=list)  # the subset required to build
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
    components: "OrderedDict[str, Component]"  # keyed by ref, sorted
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


# --------------------------------------------------------------------------- #
# Graph collection
# --------------------------------------------------------------------------- #
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


def _add_edges(comp: Component, node) -> None:
    for edge in node.dependencies:
        ref = str(edge.dst.ref)
        if ref not in comp.requires:
            comp.requires.append(ref)
        if edge.build_require and ref not in comp.build_requires:
            comp.build_requires.append(ref)


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
        _add_edges(comp, node)

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
    _add_edges(root, root_node)
    # Sorted by reference, so two SBOMs of the same project diff cleanly.
    components = OrderedDict(sorted(components.items(), key=lambda kv: kv[0].lower()))
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


def unlicensed(sbom: Sbom) -> List[str]:
    """References whose recipe declares no license, the project included."""
    comps = [sbom.root] + list(sbom.components.values())
    return [c.ref for c in comps if not c.licenses]


# --------------------------------------------------------------------------- #
# Licenses
# --------------------------------------------------------------------------- #
# SPDX License List, embedded so the tool works offline.  Regenerate from
# https://github.com/spdx/license-list-data (json/licenses.json and
# json/exceptions.json, deprecated identifiers included - recipes still
# declare "GPL-2.0"); see CLAUDE.md for the command.
SPDX_LIST_VERSION = "3.29.0"
_SPDX_LICENSES = """
0BSD 3D-Slicer-1.0 AAL Abstyles AdaCore-doc Adobe-2006 Adobe-Display-PostScript Adobe-Glyph
Adobe-Utopia ADSL Advanced-Cryptics-Dictionary AFL-1.1 AFL-1.2 AFL-2.0 AFL-2.1 AFL-3.0 Afmparse
AGPL-1.0 AGPL-1.0-only AGPL-1.0-or-later AGPL-3.0 AGPL-3.0-only AGPL-3.0-or-later Aladdin
ALGLIB-Documentation AMD-newlib AMDPLPA AML AML-glslang AMPAS ANTLR-PD ANTLR-PD-fallback any-OSI
any-OSI-perl-modules Apache-1.0 Apache-1.1 Apache-2.0 APAFML APL-1.0 App-s2p APSL-1.0 APSL-1.1
APSL-1.2 APSL-2.0 Arphic-1999 Artistic-1.0 Artistic-1.0-cl8 Artistic-1.0-Perl Artistic-2.0
Artistic-dist Aspell-RU ASWF-Digital-Assets-1.0 ASWF-Digital-Assets-1.1 atc-game Baekmuk Bahyph
Barr bcrypt-Solar-Designer Beerware Bitstream-Charter Bitstream-Vera BitTorrent-1.0
BitTorrent-1.1 blessing BlueOak-1.0.0 Boehm-GC Boehm-GC-without-fee BOLA-1.1 Borceux
Brian-Gladman-2-Clause Brian-Gladman-3-Clause Brian-Gladman-3-Clause-no-conversion BSD-1-Clause
BSD-2-Clause BSD-2-Clause-Darwin BSD-2-Clause-first-lines BSD-2-Clause-FreeBSD
BSD-2-Clause-NetBSD BSD-2-Clause-Patent BSD-2-Clause-pkgconf-disclaimer
BSD-2-Clause-pos-unchanged BSD-2-Clause-Views BSD-3-Clause BSD-3-Clause-acpica
BSD-3-Clause-Attribution BSD-3-Clause-Clear BSD-3-Clause-flex BSD-3-Clause-HP BSD-3-Clause-LBNL
BSD-3-Clause-Modification BSD-3-Clause-No-Military-License BSD-3-Clause-No-Nuclear-License
BSD-3-Clause-No-Nuclear-License-2014 BSD-3-Clause-No-Nuclear-Warranty BSD-3-Clause-Open-MPI
BSD-3-Clause-OpenWebUI BSD-3-Clause-Sun BSD-3-Clause-Tso BSD-4-Clause BSD-4-Clause-Shortened
BSD-4-Clause-UC BSD-4.3RENO BSD-4.3TAHOE BSD-Advertising-Acknowledgement BSD-ask-to-endorse
BSD-Attribution-HPND-disclaimer BSD-Inferno-Nettverk BSD-Mark-Modifications BSD-Protection
BSD-Source-alt-GPL BSD-Source-beginning-file BSD-Source-Code BSD-Source-Code-no-disclaimer
BSD-Systemics BSD-Systemics-W3Works BSL-1.0 Buddy Bugroff BUSL-1.1 bzip2-1.0.5 bzip2-1.0.6
C-UDA-1.0 CAL-1.0 CAL-1.0-Combined-Work-Exception Caldera Caldera-no-preamble CAPEC-tou Catharon
CATOSL-1.1 CC-BY-1.0 CC-BY-2.0 CC-BY-2.5 CC-BY-2.5-AU CC-BY-3.0 CC-BY-3.0-AT CC-BY-3.0-AU
CC-BY-3.0-DE CC-BY-3.0-IGO CC-BY-3.0-NL CC-BY-3.0-US CC-BY-4.0 CC-BY-NC-1.0 CC-BY-NC-2.0
CC-BY-NC-2.5 CC-BY-NC-3.0 CC-BY-NC-3.0-DE CC-BY-NC-3.0-IGO CC-BY-NC-4.0 CC-BY-NC-ND-1.0
CC-BY-NC-ND-2.0 CC-BY-NC-ND-2.5 CC-BY-NC-ND-3.0 CC-BY-NC-ND-3.0-DE CC-BY-NC-ND-3.0-IGO
CC-BY-NC-ND-4.0 CC-BY-NC-SA-1.0 CC-BY-NC-SA-2.0 CC-BY-NC-SA-2.0-DE CC-BY-NC-SA-2.0-FR
CC-BY-NC-SA-2.0-UK CC-BY-NC-SA-2.5 CC-BY-NC-SA-3.0 CC-BY-NC-SA-3.0-DE CC-BY-NC-SA-3.0-IGO
CC-BY-NC-SA-4.0 CC-BY-ND-1.0 CC-BY-ND-2.0 CC-BY-ND-2.5 CC-BY-ND-3.0 CC-BY-ND-3.0-DE CC-BY-ND-4.0
CC-BY-SA-1.0 CC-BY-SA-2.0 CC-BY-SA-2.0-UK CC-BY-SA-2.1-JP CC-BY-SA-2.5 CC-BY-SA-3.0
CC-BY-SA-3.0-AT CC-BY-SA-3.0-DE CC-BY-SA-3.0-IGO CC-BY-SA-4.0 CC-PDDC CC-PDM-1.0 CC-SA-1.0
CC0-1.0 CDDL-1.0 CDDL-1.1 CDL-1.0 CDLA-Permissive-1.0 CDLA-Permissive-2.0 CDLA-Sharing-1.0
CECILL-1.0 CECILL-1.1 CECILL-2.0 CECILL-2.1 CECILL-B CECILL-C CERN-OHL-1.1 CERN-OHL-1.2
CERN-OHL-P-2.0 CERN-OHL-S-2.0 CERN-OHL-W-2.0 CFITSIO check-cvs checkmk ClArtistic Clips CMU-Mach
CMU-Mach-nodoc CNRI-Jython CNRI-Python CNRI-Python-GPL-Compatible COIL-1.0 Community-Spec-1.0
Condor-1.1 copyleft-next-0.3.0 copyleft-next-0.3.1 Cornell-Lossless-JPEG CPAL-1.0 CPL-1.0
CPOL-1.02 Cronyx Crossword CryptoSwift CrystalStacker CUA-OPL-1.0 Cube curl cve-tou D-FSL-1.0
DEC-3-Clause diffmark DL-DE-BY-2.0 DL-DE-ZERO-2.0 DOC DocBook-DTD DocBook-Schema
DocBook-Stylesheet DocBook-XML Dotseqn DRL-1.0 DRL-1.1 DSDP dtoa dvipdfm ECL-1.0 ECL-2.0
eCos-2.0 EFL-1.0 EFL-2.0 eGenix Elastic-2.0 Entessa EPICS EPL-1.0 EPL-2.0 ErlPL-1.1
ESA-PL-permissive-2.4 ESA-PL-strong-copyleft-2.4 ESA-PL-weak-copyleft-2.4 etalab-2.0 EUDatagrid
EUPL-1.0 EUPL-1.1 EUPL-1.2 Eurosym Fair FBM FDK-AAC FDK-MPEG-H Ferguson-Twofish Frameworx-1.0
FreeBSD-DOC FreeImage FSFAP FSFAP-no-warranty-disclaimer FSFUL FSFULLR FSFULLRSD FSFULLRWD
FSL-1.1-ALv2 FSL-1.1-MIT FTL Furuseth fwlw Game-Programming-Gems GCR-docs GD generic-xts
GFDL-1.1 GFDL-1.1-invariants-only GFDL-1.1-invariants-or-later GFDL-1.1-no-invariants-only
GFDL-1.1-no-invariants-or-later GFDL-1.1-only GFDL-1.1-or-later GFDL-1.2
GFDL-1.2-invariants-only GFDL-1.2-invariants-or-later GFDL-1.2-no-invariants-only
GFDL-1.2-no-invariants-or-later GFDL-1.2-only GFDL-1.2-or-later GFDL-1.3
GFDL-1.3-invariants-only GFDL-1.3-invariants-or-later GFDL-1.3-no-invariants-only
GFDL-1.3-no-invariants-or-later GFDL-1.3-only GFDL-1.3-or-later Giftware GL2PS Glide Glulxe
GLWTPL gnuplot GPL-1.0 GPL-1.0+ GPL-1.0-only GPL-1.0-or-later GPL-2.0 GPL-2.0+ GPL-2.0-only
GPL-2.0-or-later GPL-2.0-with-autoconf-exception GPL-2.0-with-bison-exception
GPL-2.0-with-classpath-exception GPL-2.0-with-font-exception GPL-2.0-with-GCC-exception GPL-3.0
GPL-3.0+ GPL-3.0-only GPL-3.0-or-later GPL-3.0-with-autoconf-exception
GPL-3.0-with-GCC-exception Graphics-Gems gSOAP-1.3b gtkbook Gutmann HaskellReport HDF5 hdparm
HIDAPI Hippocratic-2.1 Hippocratic-3.0-core HP-1986 HP-1989 HPND HPND-DEC HPND-doc HPND-doc-sell
HPND-export-US HPND-export-US-acknowledgement HPND-export-US-modify HPND-export2-US
HPND-Fenneberg-Livingston HPND-INRIA-IMAG HPND-Intel HPND-Kevlin-Henney HPND-Markus-Kuhn
HPND-merchantability-variant HPND-MIT-disclaimer HPND-Netrek HPND-Pbmplus
HPND-sell-MIT-disclaimer-xserver HPND-sell-regexpr HPND-sell-variant
HPND-sell-variant-critical-systems HPND-sell-variant-MIT-disclaimer
HPND-sell-variant-MIT-disclaimer-rev HPND-SMC HPND-UC HPND-UC-export-US HTMLTIDY
hyphen-bulgarian IBM-pibs ICU IEC-Code-Components-EULA IJG IJG-short ImageMagick iMatix Imlib2
Info-ZIP Informatica Inner-Net-2.0 InnoSetup Intel Intel-ACPI Interbase-1.0 IPA IPL-1.0 ISC
ISC-Veillard ISO-permission Jam JasPer-2.0 jove JPL-image JPNIC JSON Kastrup Kazlib Knuth-CTAN
LAL-1.2 LAL-1.3 Latex2e Latex2e-translated-notice Leptonica LGPL-2.0 LGPL-2.0+ LGPL-2.0-only
LGPL-2.0-or-later LGPL-2.1 LGPL-2.1+ LGPL-2.1-only LGPL-2.1-or-later LGPL-3.0 LGPL-3.0+
LGPL-3.0-only LGPL-3.0-or-later LGPLLR Libpng libpng-1.6.35 libpng-2.0 libselinux-1.0 libtiff
libutil-David-Nugent LiLiQ-P-1.1 LiLiQ-R-1.1 LiLiQ-Rplus-1.1 Linux-man-pages-1-para
Linux-man-pages-copyleft Linux-man-pages-copyleft-2-para Linux-man-pages-copyleft-var
Linux-OpenIB LOOP LPD-document LPL-1.0 LPL-1.02 LPPL-1.0 LPPL-1.1 LPPL-1.2 LPPL-1.3a LPPL-1.3c
lsof Lucida-Bitmap-Fonts LZMA-SDK-9.11-to-9.20 LZMA-SDK-9.22 Mackerras-3-Clause
Mackerras-3-Clause-acknowledgment magaz mailprio MakeIndex man2html Martin-Birgmeier
McPhee-slideshow metamail Minpack MIPS MirOS MIT MIT-0 MIT-advertising MIT-Click MIT-CMU
MIT-enna MIT-feh MIT-Festival MIT-Khronos-old MIT-Modern-Variant MIT-open-group MIT-STK
MIT-testregex MIT-Wu MITNFA MMIXware MMPL-1.0.1 Motosoto MPEG-SSG mpi-permissive mpich2 MPL-1.0
MPL-1.1 MPL-2.0 MPL-2.0-no-copyleft-exception mplus MS-LPL MS-PL MS-RL MTLL MulanPSL-1.0
MulanPSL-2.0 Multics Mup MVT-1.1 NAIST-2003 NASA-1.3 Naumen NBPL-1.0 NCBI-PD NCGL-UK-2.0 NCL
NCSA Net-SNMP NetCDF Newsletr NGPL ngrep NICTA-1.0 NIST-PD NIST-PD-fallback NIST-PD-TNT
NIST-Software NLOD-1.0 NLOD-2.0 NLPL Nokia NOSL Noweb NPL-1.0 NPL-1.1 NPOSL-3.0 NRL NTIA-PD NTP
NTP-0 Nunit O-UDA-1.0 OAR OCCT-PL OCLC-2.0 ODbL-1.0 ODC-By-1.0 OFFIS OFL-1.0 OFL-1.0-no-RFN
OFL-1.0-RFN OFL-1.1 OFL-1.1-no-RFN OFL-1.1-RFN OGC-1.0 OGDL-Taiwan-1.0 OGL-Canada-2.0 OGL-UK-1.0
OGL-UK-2.0 OGL-UK-3.0 OGTSL OLDAP-1.1 OLDAP-1.2 OLDAP-1.3 OLDAP-1.4 OLDAP-2.0 OLDAP-2.0.1
OLDAP-2.1 OLDAP-2.2 OLDAP-2.2.1 OLDAP-2.2.2 OLDAP-2.3 OLDAP-2.4 OLDAP-2.5 OLDAP-2.6 OLDAP-2.7
OLDAP-2.8 OLFL-1.3 OML OpenMDW-1.0 OpenPBS-2.3 OpenSSL OpenSSL-standalone OpenVision OPL-1.0
OPL-UK-3.0 OPUBL-1.0 OSC-1.0 OSET-PL-2.1 OSL-1.0 OSL-1.1 OSL-2.0 OSL-2.1 OSL-3.0 OSSP PADL
ParaType-Free-Font-1.3 Parity-6.0.0 Parity-7.0.0 PDDL-1.0 PHP-3.0 PHP-3.01 Pixar pkgconf Plexus
pnmstitch PolyForm-Noncommercial-1.0.0 PolyForm-Small-Business-1.0.0 PostgreSQL PPL PSF-2.0
psfrag psutils Python-2.0 Python-2.0.1 python-ldap Qhull QPL-1.0 QPL-1.0-INRIA-2004 radvd Rdisc
RHeCos-1.1 RPL-1.1 RPL-1.5 RPSL-1.0 RSA-MD RSCPL Ruby Ruby-pty SAX-PD SAX-PD-2.0 Saxpath SCEA
SchemeReport Sendmail Sendmail-8.23 Sendmail-Open-Source-1.1 SGI-B-1.0 SGI-B-1.1 SGI-B-2.0
SGI-OpenGL SGMLUG-PM SGP4 SHL-0.5 SHL-0.51 SimPL-2.0 SISSL SISSL-1.2 SL Sleepycat SMAIL-GPL
SMLNJ SMPPL SNIA snprintf SOFA softSurfer Soundex Spencer-86 Spencer-94 Spencer-99 SPL-1.0
ssh-keyscan SSH-OpenSSH SSH-short SSLeay-standalone SSPL-1.0 StandardML-NJ SugarCRM-1.1.3
SUL-1.0 Sun-PPP Sun-PPP-2000 SunPro SWL swrule Symlinks TAPR-OHL-1.0 TCL TCP-wrappers TekHVC
TermReadKey TGPPL-1.0 ThirdEye threeparttable TMate TORQUE-1.1 TOSL TPDL TPL-1.0 TrustedQSL TTWL
TTYP0 TU-Berlin-1.0 TU-Berlin-2.0 Ubuntu-font-1.0 UCAR UCL-1.0 ulem UMich-Merit Unicode-3.0
Unicode-DFS-2015 Unicode-DFS-2016 Unicode-TOU UnixCrypt Unlicense Unlicense-libtelnet
Unlicense-libwhirlpool UnRAR UPL-1.0 URT-RLE Vim Vixie-Cron VOSTROM VSL-1.0 W3C W3C-19980720
W3C-20150513 w3m Watcom-1.0 Widget-Workshop WordNet Wsuipa WTFNMFPL WTFPL wwl wxWindows X11
X11-distribute-modifications-variant X11-no-permit-persons X11-swapped Xdebug-1.03 Xerox Xfig
XFree86-1.1 xinetd xkeyboard-config-Zinoviev xlock Xnet xpp XSkat xzoom YPL-1.0 YPL-1.1 Zed
Zeeff Zend-2.0 Zimbra-1.3 Zimbra-1.4 Zlib zlib-acknowledgement ZPL-1.1 ZPL-2.0 ZPL-2.1
""".split()

_SPDX_EXCEPTIONS = """
389-exception Asterisk-exception Asterisk-linking-protocols-exception Autoconf-exception-2.0
Autoconf-exception-3.0 Autoconf-exception-generic Autoconf-exception-generic-3.0
Autoconf-exception-macro Bison-exception-1.24 Bison-exception-2.2 Bootloader-exception
CGAL-linking-exception Classpath-exception-2.0 Classpath-exception-2.0-short CLISP-exception-2.0
cryptsetup-OpenSSL-exception Digia-Qt-LGPL-exception-1.1 DigiRule-FOSS-exception
eCos-exception-2.0 erlang-otp-linking-exception Fawkes-Runtime-exception FLTK-exception
fmt-exception Font-exception-2.0 freertos-exception-2.0 GCC-exception-2.0 GCC-exception-2.0-note
GCC-exception-3.1 Gmsh-exception GNAT-exception GNOME-examples-exception GNU-compiler-exception
gnu-javamail-exception Google-Patent-WebM GPL-3.0-389-ds-base-exception
GPL-3.0-interface-exception GPL-3.0-linking-exception GPL-3.0-linking-source-exception
GPL-CC-1.0 GStreamer-exception-2005 GStreamer-exception-2008 harbour-exception
i2p-gpl-java-exception Independent-modules-exception KiCad-libraries-exception
kvirc-openssl-exception LGPL-3.0-linking-exception libpri-OpenH323-exception Libtool-exception
Linux-syscall-note LLGPL LLVM-exception LZMA-exception mif-exception mxml-exception
Nokia-Qt-exception-1.1 OCaml-LGPL-linking-exception OCCT-exception-1.0
OpenJDK-assembly-exception-1.0 openvpn-openssl-exception PCRE2-exception polyparse-exception
PS-or-PDF-font-exception-20170817 QPL-1.0-INRIA-2004-exception Qt-GPL-exception-1.0
Qt-LGPL-exception-1.1 Qwt-exception-1.0 romic-exception RRDtool-FLOSS-exception-2.0
rsync-linking-exception SANE-exception SHL-2.0 SHL-2.1 Simple-Library-Usage-exception
Spelling-Provider-LGPL-exception sqlitestudio-OpenSSL-exception stunnel-exception SWI-exception
Swift-exception Texinfo-exception u-boot-exception-2.0 UBDL-exception
Universal-FOSS-exception-1.0 vsftpd-openssl-exception WxWindows-exception-3.1
x11vnc-openssl-exception
""".split()

# SPDX identifiers match case-insensitively; the list's spelling is canonical.
_LICENSE_IDS = {i.lower(): i for i in _SPDX_LICENSES}
_EXCEPTION_IDS = {i.lower(): i for i in _SPDX_EXCEPTIONS}
_TOKEN_RE = re.compile(r"\(|\)|[^\s()]+")


def spdx_expression(text: str) -> Optional[str]:
    """``text`` as a canonical SPDX license expression, or None when it is not one.

    Accepts license identifiers from the embedded list (with an optional
    ``+``), ``AND``/``OR`` in any case, ``WITH`` followed by a listed
    exception, and parentheses.  ``LicenseRef-`` identifiers are not
    accepted: a recipe that declares one gives no text for it, so it is
    treated like any other non-SPDX license.
    """
    out: List[str] = []
    depth = 0
    want_operand = True  # start, after an operator or "("
    want_exception = False  # right after WITH
    last = ""  # kind of the previous token: "license", "exception", ")" or operator
    for tok in _TOKEN_RE.findall(text):
        op = tok.upper()
        if tok == "(":
            if not want_operand or want_exception:
                return None
            depth += 1
            out.append(tok)
            last = "("
        elif tok == ")":
            depth -= 1
            if want_operand or depth < 0:
                return None
            out.append(tok)
            last = ")"
        elif op in ("AND", "OR", "WITH"):
            if want_operand or (op == "WITH" and last != "license"):
                return None
            want_operand, want_exception = True, op == "WITH"
            out.append(op)
            last = op
        else:
            if not want_operand:
                return None
            if want_exception:
                canon = _EXCEPTION_IDS.get(tok.lower())
                last = "exception"
            else:
                plus = tok.endswith("+") and len(tok) > 1
                canon = _LICENSE_IDS.get((tok[:-1] if plus else tok).lower())
                canon = canon + "+" if canon and plus else canon
                last = "license"
            if not canon:
                return None
            out.append(canon)
            want_operand = want_exception = False
    if want_operand or depth:
        return None
    return " ".join(out).replace("( ", "(").replace(" )", ")")


def _license_ref(text: str) -> str:
    """A ``LicenseRef-`` identifier derived from a non-SPDX declared license."""
    return "LicenseRef-" + (re.sub(r"[^A-Za-z0-9.]+", "-", text).strip("-") or "unnamed")


def _declared_expression(declared: List[str], ref_for: Callable[[str], str]) -> str:
    """All of a package's declared licenses as one expression (a tuple means AND).

    Non-SPDX licenses are replaced by ``ref_for(text)``, a ``LicenseRef-``.
    """
    parts = []
    for text in declared:
        expr = spdx_expression(text) or ref_for(text)
        parts.append("(%s)" % expr if " " in expr and len(declared) > 1 else expr)
    return " AND ".join(parts)


# --------------------------------------------------------------------------- #
# CycloneDX
# --------------------------------------------------------------------------- #
def _tool_version() -> str:
    try:
        from importlib.metadata import version

        return version("conan-utils")
    except Exception:  # run from a checkout, not installed
        return "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cdx_licenses(declared: List[str]) -> List[dict]:
    """CycloneDX ``licenses``: one entry per declared license, or one expression.

    A plain SPDX identifier becomes ``license.id`` and anything else
    ``license.name``.  CycloneDX cannot mix entries with an expression, so a
    compound expression (``MIT OR Apache-2.0``) turns the whole list into a
    single expression.
    """
    exprs = [spdx_expression(d) for d in declared]
    if all(e is None or e in _LICENSE_IDS.values() for e in exprs):
        return [
            {"license": {"id": e, "acknowledgement": "declared"}}
            if e
            else {"license": {"name": d, "acknowledgement": "declared"}}
            for d, e in zip(declared, exprs)
        ]
    return [
        {"expression": _declared_expression(declared, _license_ref), "acknowledgement": "declared"}
    ]


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
        out["licenses"] = _cdx_licenses(comp.licenses)
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
        "timestamp": timestamp or _now(),
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
        "specVersion": CYCLONEDX_VERSION,
        "serialNumber": serial or "urn:uuid:%s" % uuid.uuid4(),
        "version": 1,
        "metadata": metadata,
        "components": [_cdx_component(c) for c in sbom.components.values()],
        "dependencies": dependencies,
    }


# --------------------------------------------------------------------------- #
# SPDX
# --------------------------------------------------------------------------- #
def _spdx_id(text: str) -> str:
    """An SPDXID from a reference: only letters, digits, "." and "-" are allowed."""
    return "SPDXRef-Package-" + re.sub(r"[^A-Za-z0-9.]+", "-", text).strip("-")


def to_spdx(sbom: Sbom, namespace: Optional[str] = None, timestamp: Optional[str] = None) -> dict:
    """The SBOM as an SPDX 2.3 JSON document (a dict ready for json.dump).

    SPDX has no "scope": a build-only package is recognisable by its
    ``BUILD_DEPENDENCY_OF`` relationship and says so in its ``comment``.
    Non-SPDX licenses become ``LicenseRef-`` identifiers whose declared text
    is in ``hasExtractedLicensingInfos``.  ``licenseConcluded`` and
    ``copyrightText`` are ``NOASSERTION``: the tool reports what recipes
    declare and concludes nothing.  ``namespace`` and ``timestamp`` are
    generated when not given; tests pass fixed ones.
    """
    comps = [sbom.root] + list(sbom.components.values())
    ids: Dict[str, str] = {}
    for comp in comps:
        base = _spdx_id(comp.ref) if comp.is_package else "SPDXRef-Project"
        spdx_id, n = base, 2
        while spdx_id in ids.values():  # two refs that sanitise alike
            spdx_id, n = "%s-%d" % (base, n), n + 1
        ids[comp.ref] = spdx_id

    refs: Dict[str, str] = OrderedDict()  # declared text -> LicenseRef

    def ref_for(text: str) -> str:
        if text not in refs:
            base = _license_ref(text)
            name, n = base, 2
            while name in refs.values():
                name, n = "%s-%d" % (base, n), n + 1
            refs[text] = name
        return refs[text]

    packages = []
    for comp in comps:
        is_root = comp is sbom.root
        pkg: dict = {"SPDXID": ids[comp.ref], "name": comp.name}
        if comp.version:
            pkg["versionInfo"] = comp.version
        pkg.update(
            {
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": (
                    _declared_expression(comp.licenses, ref_for) if comp.licenses else "NOASSERTION"
                ),
                "copyrightText": "NOASSERTION",
                "primaryPackagePurpose": "APPLICATION" if is_root else "LIBRARY",
            }
        )
        if comp.homepage:
            pkg["homepage"] = comp.homepage
        if comp.url:
            pkg["sourceInfo"] = "Conan recipe repository: %s" % comp.url
        if comp.description:
            pkg["description"] = comp.description
        if comp.purl:
            pkg["externalRefs"] = [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": comp.purl,
                }
            ]
        if not is_root and not comp.runtime:
            pkg["comment"] = "Build only: used to build the project, not shipped with it."
        packages.append(pkg)

    relationships = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": ids[sbom.root.ref],
        }
    ]
    for comp in comps:
        for ref in comp.requires:
            if ref not in ids:
                continue
            if ref in comp.build_requires:
                rel = (ids[ref], "BUILD_DEPENDENCY_OF", ids[comp.ref])
            else:
                rel = (ids[comp.ref], "DEPENDS_ON", ids[ref])
            relationships.append(
                {"spdxElementId": rel[0], "relationshipType": rel[1], "relatedSpdxElement": rel[2]}
            )

    creation: dict = {
        "created": timestamp or _now(),
        "creators": ["Tool: conansbom-%s" % _tool_version()],
        # The spec wants "M.N"; the list itself is versioned "M.N.P".
        "licenseListVersion": ".".join(SPDX_LIST_VERSION.split(".")[:2]),
    }
    if sbom.settings:
        creation["comment"] = "Conan settings: " + ", ".join(
            "%s=%s" % kv for kv in sorted(sbom.settings.items())
        )
    doc: dict = {
        "spdxVersion": SPDX_VERSION,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": sbom.root.ref,
        "documentNamespace": namespace
        or "https://spdx.org/spdxdocs/conansbom-%s-%s"
        % (re.sub(r"[^A-Za-z0-9.-]+", "-", sbom.root.ref), uuid.uuid4()),
        "creationInfo": creation,
        "packages": packages,
        "relationships": relationships,
    }
    if refs:
        doc["hasExtractedLicensingInfos"] = [
            {
                "licenseId": ref,
                "name": text,
                "extractedText": text,
                "comment": "Declared by the Conan recipe; not an SPDX license identifier.",
            }
            for text, ref in refs.items()
        ]
    return doc


# --------------------------------------------------------------------------- #
# HTML (self-contained, no scripts, no external assets)
# --------------------------------------------------------------------------- #
def _link(url: Optional[str]) -> str:
    """A homepage as a link - only http(s), so a recipe cannot inject javascript:."""
    if not url:
        return "-"
    e = html.escape(url)
    return '<a href="%s">%s</a>' % (e, e) if re.match(r"https?://", url) else e


def _license_cell(comp: Component) -> str:
    if not comp.licenses:
        return '<span class="missing">none declared</span>'
    return "<br>".join(html.escape(d) for d in comp.licenses)


def to_html(sbom: Sbom, timestamp: Optional[str] = None) -> str:
    """The SBOM as a page for people: licenses first, then what ships and what builds it."""
    e = html.escape
    root = sbom.root
    parts: List[str] = []
    w = parts.append
    w('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">')
    w(
        "<title>SBOM: %s</title><style>%sa{color:inherit}</style></head><body>"
        % (e(root.ref), conandeps._CSS)
    )
    w("<h1>Software bill of materials: %s</h1>" % e(root.ref))
    header: List[Tuple[str, str]] = [
        ("license", ", ".join(root.licenses) or "none declared"),
        ("build", ", ".join("%s=%s" % kv for kv in sorted(sbom.settings.items())) or "-"),
        ("generated", "%s by conansbom %s" % (timestamp or _now(), _tool_version())),
    ]
    if root.description:
        header.insert(0, ("description", root.description))
    w("<p>%s</p>" % "<br>".join("<b>%s:</b> %s" % (e(k), e(v)) for k, v in header))

    comps = list(sbom.components.values())
    shipped = [c for c in comps if c.runtime]
    build_only = [c for c in comps if not c.runtime]

    # ---- licenses: the compliance view ------------------------------------
    w("<h2>Licenses</h2>")
    by_license: Dict[str, List[Component]] = OrderedDict()
    for comp in comps:
        for text in comp.licenses or [""]:
            by_license.setdefault(text, []).append(comp)
    rows = []
    for text in sorted(by_license, key=lambda t: (t == "", t.lower())):
        users = by_license[text]
        spdx = spdx_expression(text) if text else None
        rows.append(
            [
                e(text) if text else '<span class="missing">none declared</span>',
                e(spdx) if spdx else '<span class="na">%s</span>' % ("-" if not text else "no"),
                e(", ".join(c.ref for c in users if c.runtime)) or "-",
                e(", ".join(c.ref for c in users if not c.runtime)) or "-",
            ]
        )
    w(conandeps._html_table(["license (as declared)", "SPDX", "shipped in", "build only"], rows))
    w(
        '<p class="note">Licenses are listed exactly as each package\'s recipe declares '
        "them; nothing is inferred from source code. <b>SPDX</b> gives the SPDX "
        "identifier or expression when the declared text is one (SPDX License List "
        "%s), <code>no</code> when it is not. Packages with no declared license need "
        "a manual review.</p>" % e(SPDX_LIST_VERSION)
    )

    # ---- the packages ------------------------------------------------------
    def table(title: str, items: List[Component], note: str) -> None:
        w("<h2>%s (%d)</h2>" % (e(title), len(items)))
        if not items:
            w("<p>None.</p>")
            return
        rows = [
            [
                e(c.ref),
                _license_cell(c),
                _link(c.homepage),
                e(c.description or "-"),
            ]
            for c in items
        ]
        w(conandeps._html_table(["package", "license", "homepage", "description"], rows))
        w('<p class="note">%s</p>' % note)

    table(
        "Shipped",
        shipped,
        "Packages that are part of the product: the project reaches them without "
        "going through a build requirement.",
    )
    table(
        "Build only",
        build_only,
        "Packages used to build the product (build requirements and what only they "
        "need), not shipped with it.",
    )
    w("</body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def render(sbom: Sbom, fmt: str) -> str:
    """The SBOM in ``fmt`` (one of ``FORMATS``) as text ready to write."""
    if fmt == "html":
        return to_html(sbom) + "\n"
    doc = to_spdx(sbom) if fmt == "spdx" else to_cyclonedx(sbom)
    return json.dumps(doc, indent=2) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="conansbom",
        description="Write an SBOM (packages, declared licenses, dependency graph) for a "
        "Conan 1.x conanfile: CycloneDX %s JSON, SPDX 2.3 JSON or an HTML page."
        % CYCLONEDX_VERSION,
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
    p.add_argument(
        "-f",
        "--format",
        choices=FORMATS,
        default="cyclonedx",
        help="output format (default: %(default)s)",
    )
    p.add_argument("--out", metavar="FILE", help="write the SBOM to FILE (default: stdout)")
    p.add_argument(
        "--require-licenses",
        action="store_true",
        help="exit 1 when a package (or the project) declares no license; "
        "the SBOM is written either way",
    )
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

    text = render(sbom, args.format)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    # The same list decides the warning and the exit code, so they cannot disagree.
    missing = unlicensed(sbom)
    if missing:
        level = "error" if args.require_licenses else "WARN"
        sys.stderr.write("conansbom: %s: no license declared: %s\n" % (level, ", ".join(missing)))
    return 1 if missing and args.require_licenses else 0


if __name__ == "__main__":
    sys.exit(main())
