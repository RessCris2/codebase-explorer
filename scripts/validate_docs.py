#!/usr/bin/env python3
"""Phase 5 - Verify the documentation against the source.

Checks every claim in <out>/docs/ against the real files:

  errors    evidence pointing at a missing file, line ranges outside the file,
            quotes that do not occur in the cited lines, symbols that the
            parser did not find, invalid status values, "confirmed" claims
            with no evidence, broken cross references, invalid JSON
  warnings  documentation gaps (files, public symbols and routes in scope
            that nobody described), flows with no evidence

Writes <out>/data/validation_report.json and exits 1 if there are errors.

Usage: python3 validate_docs.py --out <output_dir>
"""
import argparse
import fnmatch
import glob
import json
import os
import sys

STATUSES = {"confirmed", "inferred", "unknown"}


def load(path, problems):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except ValueError as e:
        problems.append(("error", os.path.basename(path), f"invalid JSON: {e}"))
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    problems = []  # (level, where, message)

    manifest = load(os.path.join(out, "data", "manifest.json"), problems)
    if not manifest:
        sys.exit("manifest.json missing or invalid - run scan_repo.py first")
    graph = load(os.path.join(out, "data", "code_graph.json"), problems) or {"modules": {}, "routes": []}
    root = manifest["root"]
    file_lines = {f["path"]: f["lines"] for f in manifest["files"]}
    symbols = {f"{p}::{s['qualname']}" for p, m in graph["modules"].items() for s in m["symbols"]}
    src_cache = {}

    def source_lines(path):
        if path not in src_cache:
            try:
                with open(os.path.join(root, path), encoding="utf-8", errors="replace") as fh:
                    src_cache[path] = fh.read().splitlines()
            except OSError:
                src_cache[path] = []
        return src_cache[path]

    counts = {"claims": 0, "confirmed": 0, "inferred": 0, "unknown": 0, "evidence": 0}

    def check_evidence(ev, where):
        counts["evidence"] += 1
        if not isinstance(ev, dict) or "file" not in ev:
            problems.append(("error", where, f"evidence must be an object with a file: {ev!r}"))
            return
        f = ev["file"]
        if f not in file_lines:
            problems.append(("error", where, f"evidence file not in repository manifest: {f}"))
            return
        lines = ev.get("lines")
        if lines is None:
            return
        if (not isinstance(lines, list) or len(lines) != 2
                or not all(isinstance(x, int) for x in lines)):
            problems.append(("error", where, f"lines must be [start, end]: {lines!r} ({f})"))
            return
        a, b = lines
        if a < 1 or b < a or b > file_lines[f]:
            problems.append(("error", where,
                             f"lines {a}-{b} outside {f} (file has {file_lines[f]} lines)"))
            return
        quote = ev.get("quote")
        if quote:
            norm = lambda s: " ".join(s.split())
            if norm(quote) not in norm("\n".join(source_lines(f)[a - 1:b])):
                problems.append(("error", where, f"quote not found in {f}:{a}-{b}: {quote[:80]!r}"))

    def walk(node, where, implied_evidence=False):
        """Validate every object that carries a status and/or evidence."""
        if isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{where}[{i}]", implied_evidence)
            return
        if not isinstance(node, dict):
            return
        ev = node.get("evidence")
        if ev is not None:
            if not isinstance(ev, list):
                problems.append(("error", where, "evidence must be a list"))
                ev = []
            for e in ev:
                check_evidence(e, where)
        if "status" in node:
            st = node["status"]
            counts["claims"] += 1
            if st not in STATUSES:
                problems.append(("error", where, f"status must be one of {sorted(STATUSES)}: {st!r}"))
            else:
                counts[st] += 1
                if st == "confirmed" and not ev and not implied_evidence:
                    problems.append(("error", where,
                                     "status is confirmed but there is no evidence; "
                                     "cite source lines or downgrade to inferred"))
        for ref in node.get("symbols", []) if isinstance(node.get("symbols"), list) else []:
            if isinstance(ref, str) and "::" in ref and symbols and ref not in symbols:
                if ref.split("::")[0] in graph["modules"]:
                    problems.append(("error", where, f"symbol not found by the parser: {ref}"))
                elif ref.split("::")[0] not in file_lines:
                    problems.append(("error", where, f"symbol file not in manifest: {ref}"))
        for k, v in node.items():
            if k not in ("evidence",) and isinstance(v, (dict, list)):
                if k == "symbols" and isinstance(v, list) and v and isinstance(v[0], str):
                    continue
                walk(v, f"{where}.{k}", False)

    docs = os.path.join(out, "docs")
    overview = load(os.path.join(docs, "overview.json"), problems) or {}
    arch = load(os.path.join(docs, "architecture.json"), problems) or {}
    api = load(os.path.join(docs, "api.json"), problems) or {}
    flows = load(os.path.join(docs, "flows.json"), problems) or {}
    for name, doc in (("overview", overview), ("architecture", arch), ("api", api), ("flows", flows)):
        if not doc:
            problems.append(("warning", name, f"docs/{name}.json is missing or empty"))
        if name != "api":
            walk(doc, name)
    # An endpoint that matches an extracted route has the handler as implied evidence.
    extracted_ids = {r["id"] for r in graph.get("routes", [])}
    for i, ep in enumerate(api.get("endpoints", [])):
        walk(ep, f"api.endpoints[{i}]", implied_evidence=ep.get("id") in extracted_ids)

    # Module docs.
    documented_files, documented_syms = set(), set()
    for mp in sorted(glob.glob(os.path.join(docs, "modules", "*.json"))):
        doc = load(mp, problems)
        if not isinstance(doc, dict):
            continue
        where = f"modules/{os.path.basename(mp)}"
        f = doc.get("file")
        if f not in file_lines:
            problems.append(("error", where, f"'file' is not in the repository manifest: {f!r}"))
            continue
        if f in documented_files:
            problems.append(("error", where, f"second module doc for {f}"))
        documented_files.add(f)
        if not doc.get("summary"):
            problems.append(("error", where, "module doc has no summary"))
        for i, s in enumerate(doc.get("symbols", [])):
            ref = f"{f}::{s.get('name')}"
            known = ref in symbols
            if f in graph["modules"] and not known:
                problems.append(("error", where, f"symbol not found by the parser: {ref}"))
            documented_syms.add(ref)
            walk(s, f"{where}.symbols[{i}]", implied_evidence=known)
        walk({k: v for k, v in doc.items() if k != "symbols"}, where)

    # Architecture cross references.
    comp_ids = {c.get("id") for c in arch.get("components", [])}
    for c in arch.get("components", []):
        for f in c.get("files", []):
            if not any(p == f or p.startswith(f.rstrip("/") + "/") for p in file_lines):
                problems.append(("error", "architecture", f"component {c.get('id')}: no such file or directory: {f}"))
    for e in arch.get("edges", []):
        for end in ("from", "to"):
            if e.get(end) not in comp_ids:
                problems.append(("error", "architecture", f"edge references unknown component: {e.get(end)!r}"))

    # Flows.
    for fl in flows.get("flows", []):
        fid = fl.get("id") or "?"
        steps = fl.get("steps", [])
        if not steps:
            problems.append(("error", f"flows.{fid}", "flow has no steps"))
        for st in steps:
            if not st.get("evidence") and st.get("status") != "unknown":
                problems.append(("error", f"flows.{fid}.{st.get('id')}",
                                 "flow step has no evidence; cite code or mark it unknown"))

    # Coverage inside the declared scope.
    scope = (overview.get("scope") or {})
    include = scope.get("include") or ["**"]
    exclude = scope.get("exclude") or []

    def in_scope(p):
        hit = lambda pats: any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(p, g.rstrip("/") + "/*")
                               or g == "**" for g in pats)
        return hit(include) and not hit(exclude)

    code_files = [f["path"] for f in manifest["files"]
                  if f["is_code"] and not f["is_test"] and f["lines"] > 0 and in_scope(f["path"])]
    missing_files = [p for p in code_files if p not in documented_files]
    public = {f"{p}::{s['qualname']}" for p in code_files if p in graph["modules"]
              for s in graph["modules"][p]["symbols"]
              if s["is_public"] and "." not in s["qualname"]}
    missing_syms = sorted(public - documented_syms)
    route_ids = {r["id"] for r in graph.get("routes", []) if in_scope(r["file"])}
    api_ids = {e.get("id") for e in api.get("endpoints", [])}
    missing_routes = sorted(route_ids - api_ids)
    for e in sorted(i for i in api_ids - route_ids if i):
        ep = next(x for x in api["endpoints"] if x.get("id") == e)
        if not ep.get("evidence"):
            problems.append(("error", "api", f"endpoint {e} was not extracted by the parser and "
                                             "has no evidence; cite the code that defines it"))
    if missing_files:
        problems.append(("warning", "coverage", f"{len(missing_files)} code files in scope have no "
                         f"module doc: {', '.join(missing_files[:10])}" + (" ..." if len(missing_files) > 10 else "")))
    if missing_syms:
        problems.append(("warning", "coverage", f"{len(missing_syms)} public top-level symbols are "
                         f"undocumented: {', '.join(missing_syms[:8])}" + (" ..." if len(missing_syms) > 8 else "")))
    if missing_routes:
        problems.append(("warning", "coverage", f"{len(missing_routes)} extracted routes have no entry "
                         f"in api.json: {', '.join(missing_routes[:8])}"))

    pct = lambda done, total: round(100 * done / total, 1) if total else 100.0
    errors = [p for p in problems if p[0] == "error"]
    warnings = [p for p in problems if p[0] == "warning"]
    report = {
        "ok": not errors,
        "errors": [{"where": w, "message": m} for _, w, m in errors],
        "warnings": [{"where": w, "message": m} for _, w, m in warnings],
        "coverage": {
            "scope": {"include": include, "exclude": exclude},
            "files": {"documented": len(code_files) - len(missing_files), "total": len(code_files),
                      "percent": pct(len(code_files) - len(missing_files), len(code_files))},
            "public_symbols": {"documented": len(public) - len(missing_syms), "total": len(public),
                               "percent": pct(len(public) - len(missing_syms), len(public))},
            "routes": {"documented": len(route_ids) - len(missing_routes), "total": len(route_ids),
                       "percent": pct(len(route_ids) - len(missing_routes), len(route_ids))},
            "flows": len(flows.get("flows", [])),
        },
        "claims": counts,
        "missing": {"files": missing_files, "symbols": missing_syms, "routes": missing_routes},
    }
    dest = os.path.join(out, "data", "validation_report.json")
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, ensure_ascii=False)

    c = report["coverage"]
    print(f"report: {dest}")
    print(f"coverage: files {c['files']['documented']}/{c['files']['total']} ({c['files']['percent']}%), "
          f"public symbols {c['public_symbols']['documented']}/{c['public_symbols']['total']} "
          f"({c['public_symbols']['percent']}%), routes {c['routes']['documented']}/{c['routes']['total']}, "
          f"flows {c['flows']}")
    print(f"claims: {counts['claims']} ({counts['confirmed']} confirmed, {counts['inferred']} inferred, "
          f"{counts['unknown']} unknown), evidence references checked: {counts['evidence']}")
    for _, w, m in errors:
        print(f"ERROR   {w}: {m}")
    for _, w, m in warnings:
        print(f"warning {w}: {m}")
    print(f"{len(errors)} errors, {len(warnings)} warnings")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
