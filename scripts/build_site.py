#!/usr/bin/env python3
"""Phase 6 - Build the static explorer.

Bundles manifest, code graph, docs, the validation report and the source text
of the scanned files into one self-contained <out>/dist/index.html
(no build step, no network, opens from file://).

Usage: python3 build_site.py --out <output_dir> [--lang zh|en] [--no-source]

The interface language is Chinese by default. It follows "lang" in
docs/overview.json when that is set, and --lang overrides both.
"""
import argparse
import glob
import json
import os
import sys

MAX_FILE_BYTES = 300_000
MAX_TOTAL_BYTES = 40_000_000
LANGS = ("zh", "en")
DEFAULT_LANG = "zh"


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-source", action="store_true",
                    help="do not embed source text (evidence links then show path and lines only)")
    ap.add_argument("--lang", choices=LANGS,
                    help="interface language; default: overview.json \"lang\", else zh")
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    manifest = load(os.path.join(out, "data", "manifest.json"))
    if not manifest:
        sys.exit("manifest.json missing - run scan_repo.py first")
    graph = load(os.path.join(out, "data", "code_graph.json"),
                 {"modules": {}, "routes": [], "models": [], "import_edges": []})
    validation = load(os.path.join(out, "data", "validation_report.json"))
    docs_dir = os.path.join(out, "docs")
    modules = {}
    for mp in sorted(glob.glob(os.path.join(docs_dir, "modules", "*.json"))):
        doc = load(mp)
        if isinstance(doc, dict) and doc.get("file"):
            modules[doc["file"]] = doc
    docs = {
        "overview": load(os.path.join(docs_dir, "overview.json"), {}),
        "architecture": load(os.path.join(docs_dir, "architecture.json"), {}),
        "api": load(os.path.join(docs_dir, "api.json"), {}),
        "flows": load(os.path.join(docs_dir, "flows.json"), {}),
        "modules": modules,
    }

    doc_lang = (docs["overview"] or {}).get("lang")
    if doc_lang is not None and doc_lang not in LANGS:
        print(f"warning: overview.json lang {doc_lang!r} is not one of {LANGS}; using {DEFAULT_LANG}")
        doc_lang = None
    lang = args.lang or doc_lang or DEFAULT_LANG
    if args.lang and doc_lang and args.lang != doc_lang:
        print(f"warning: --lang {args.lang} differs from overview.json lang {doc_lang}; "
              "the interface and the prose will be in different languages")

    sources, total, skipped = {}, 0, 0
    if not args.no_source:
        for f in manifest["files"]:
            if f["bytes"] > MAX_FILE_BYTES or total + f["bytes"] > MAX_TOTAL_BYTES:
                skipped += 1
                continue
            try:
                with open(os.path.join(manifest["root"], f["path"]), encoding="utf-8",
                          errors="replace") as fh:
                    sources[f["path"]] = fh.read()
                total += f["bytes"]
            except OSError:
                skipped += 1

    public_manifest = {k: v for k, v in manifest.items() if k != "root"}  # no absolute local path
    data = {"manifest": public_manifest, "graph": graph, "docs": docs,
            "sources": sources, "validation": validation, "lang": lang}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets",
                            "site_template.html")
    with open(tpl_path, encoding="utf-8") as fh:
        tpl = fh.read()
    marker = "/*__DATA__*/null"
    if marker not in tpl:
        sys.exit("site template is missing the data marker")
    html = tpl.replace(marker, payload, 1)
    os.makedirs(os.path.join(out, "dist"), exist_ok=True)
    dest = os.path.join(out, "dist", "index.html")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(html)

    print(f"site: {dest} ({len(html) / 1e6:.1f} MB), interface language: {lang}")
    print(f"module docs: {len(modules)}, flows: {len(docs['flows'].get('flows', []))}, "
          f"endpoints: {len(graph.get('routes', []))} extracted + "
          f"{len(docs['api'].get('endpoints', []))} described, "
          f"sources embedded: {len(sources)} ({skipped} skipped)")
    if validation is None:
        print("warning: no validation report - run validate_docs.py before building")
    elif not validation.get("ok"):
        print(f"warning: validation has {len(validation['errors'])} errors; "
              "the site shows them on the overview page")


if __name__ == "__main__":
    main()
