#!/usr/bin/env python3
"""
test_host_base.py — standalone gate for G2SHost's --host-base derivation
(lite deployment mode; see deploy/LITE_DEPLOY.md).

Regression lock: with no arguments at all, G2SHost must still derive
EXACTLY today's three full-mode URLs (mediaDisplay default content, glass
content base, G2S host URI) — --host-base only changes behavior when an
operator actually passes it. In-process, no live host, stdlib only.

Run:  python3 tools/test_host_base.py
Must end "RESULT: N passed, 0 failed".
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from g2s_host import CABINET_VERSION, DEFAULT_HOST_BASE, G2SHost  # noqa: E402

_p = _f = 0


def check(name, ok, detail=""):
    global _p, _f
    if ok:
        _p += 1
        print(f"  ✅ {name}")
    else:
        _f += 1
        print(f"  ❌ {name} {detail}")


def main():
    print("— defaults (regression lock: today's full-mode URLs, byte-for-byte)")
    e = G2SHost()
    check("default host_base",
          e.host_base == "http://192.168.50.2:8081", e.host_base)
    check("default mediaDisplay content URI",
          e.md_default_content_uri == "http://192.168.50.2:8081/webui/hello.html",
          e.md_default_content_uri)
    check("default glass content base",
          e.glass_content_uri_base == "http://192.168.50.2:8081/webui/",
          e.glass_content_uri_base)
    check("default G2S host URI (derived — no --host-uri given)",
          e.host_uri == "http://192.168.50.2:8081/G2S", e.host_uri)
    check("DEFAULT_HOST_BASE constant matches", DEFAULT_HOST_BASE ==
          "http://192.168.50.2:8081", DEFAULT_HOST_BASE)

    print("— --host-base derives all three self-URLs (lite mode)")
    e2 = G2SHost(host_base="http://10.0.0.5:8081")
    check("host_base",
          e2.host_base == "http://10.0.0.5:8081", e2.host_base)
    check("mediaDisplay content URI derives",
          e2.md_default_content_uri == "http://10.0.0.5:8081/webui/hello.html",
          e2.md_default_content_uri)
    check("glass content base derives",
          e2.glass_content_uri_base == "http://10.0.0.5:8081/webui/",
          e2.glass_content_uri_base)
    check("G2S host URI derives (no explicit --host-uri)",
          e2.host_uri == "http://10.0.0.5:8081/G2S", e2.host_uri)

    print("— explicit --host-uri always wins over the derived one")
    e3 = G2SHost(host_base="http://10.0.0.5:8081",
                 host_uri="http://10.0.0.5:9999/G2S/custom")
    check("explicit host_uri beats host_base derivation",
          e3.host_uri == "http://10.0.0.5:9999/G2S/custom", e3.host_uri)
    check("...but content URLs still derive from host_base",
          e3.md_default_content_uri == "http://10.0.0.5:8081/webui/hello.html",
          e3.md_default_content_uri)

    print("— normalization: trailing slash + missing scheme")
    e4 = G2SHost(host_base="http://10.0.0.5:8081/")
    check("trailing slash stripped",
          e4.host_base == "http://10.0.0.5:8081", e4.host_base)
    e5 = G2SHost(host_base="10.0.0.5:8081")
    check("missing scheme defaults to http://",
          e5.host_base == "http://10.0.0.5:8081", e5.host_base)
    e6 = G2SHost(host_base="10.0.0.5:8081/")
    check("missing scheme + trailing slash both normalize",
          e6.host_base == "http://10.0.0.5:8081", e6.host_base)

    print("— engine_meta() surfaces hostBase + version")
    e7 = G2SHost(host_base="http://10.0.0.5:8081")
    meta = e7.engine_meta()
    check("engine_meta()['hostBase'] present and correct",
          meta.get("hostBase") == "http://10.0.0.5:8081", meta.get("hostBase"))
    check("engine_meta()['version'] present and correct",
          meta.get("version") == CABINET_VERSION, meta.get("version"))

    print("=" * 50)
    print(f"RESULT: {_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
