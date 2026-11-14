#!/usr/bin/env python3
"""Phase 2 - Static extraction for Python.

Reads <out>/data/manifest.json, parses every Python file with `ast` (the code
is parsed, never imported or run) and writes <out>/data/code_graph.json:

  modules  path -> docstring, imports (resolved to repo files), symbols
  symbols  classes / functions / methods with exact line ranges, signatures,
           decorators and best-effort resolved calls
  routes   FastAPI / Flask style HTTP routes with composed router prefixes
  models   Pydantic-style models with their fields
  import_edges  file -> file dependencies inside the repository

Everything here is a parser fact except `calls[].resolved` and route prefix
composition, which are best-effort name resolution (no type inference,
no dependency injection, no dynamic dispatch). Treat those as hints.

Usage: python3 extract_python.py --out <output_dir>
"""
import argparse
import ast
import json
import os
import sys

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head", "websocket"}
MODEL_BASES = {"BaseModel", "BaseSettings", "SQLModel"}


def unparse(node, limit=200):
    if node is None:
        return None
    try:
        s = ast.unparse(node)
    except Exception:
        return None
    return s if len(s) <= limit else s[:limit] + "..."


def dotted(node):
    """a.b.c -> 'a.b.c' for Name/Attribute chains, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class ModuleIndex:
    """Maps dotted module names to repo files, tolerating unknown source roots."""

    def __init__(self, py_paths):
        self.paths = set(py_paths)
        self.by_suffix = {}
        for p in py_paths:
            mod = p[:-3].split("/")
            if mod[-1] == "__init__":
                mod = mod[:-1]
            for i in range(len(mod)):
                self.by_suffix.setdefault(".".join(mod[i:]), []).append((len(mod) - i, p))

    def lookup(self, name):
        c = self.by_suffix.get(name)
        if not c:
            return None
        best = max(n for n, _ in c)
        top = [p for n, p in c if n == best]
        return top[0] if len(top) == 1 else None

    def resolve(self, module, name, cur_file, level):
        """Return (file, symbol_or_None) for `from module import name` / `import module`."""
        if level:
            pkg = cur_file.split("/")[:-1]
            pkg = pkg[:len(pkg) - (level - 1)] if level > 1 else pkg
            base = "/".join(pkg + (module.split(".") if module else []))
            cands = []
            if name:
                cands += [(f"{base}/{name}.py", None), (f"{base}/{name}/__init__.py", None)]
            cands += [(f"{base}.py", name), (f"{base}/__init__.py", name)]
            for path, sym in cands:
                path = path.lstrip("/")
                if path in self.paths:
                    return path, sym
            return None, None
        if name:
            p = self.lookup(f"{module}.{name}")
            if p:
                return p, None
        p = self.lookup(module)
        if p:
            return p, name
        # `import a.b.c` where only a.b is a file is not a module import; give up.
        return None, None


def signature(node):
    if isinstance(node, ast.ClassDef):
        bases = [unparse(b) for b in node.bases]
        return f"class {node.name}({', '.join(b for b in bases if b)})" if bases else f"class {node.name}"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    ret = f" -> {unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({unparse(node.args, 400) or ''}){ret}"


def params(node):
    out = []
    a = node.args
    pos = a.posonlyargs + a.args
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    for arg, d in list(zip(pos, defaults)) + list(zip(a.kwonlyargs, a.kw_defaults)):
        if arg.arg in ("self", "cls"):
            continue
        item = {"name": arg.arg, "annotation": unparse(arg.annotation), "default": unparse(d)}
        if isinstance(d, ast.Call) and (dotted(d.func) or "").split(".")[-1] == "Depends":
            item["depends"] = unparse(d.args[0]) if d.args else None
        out.append(item)
    return out


def analyze_file(path, src, index):
    mod = {"docstring": None, "imports": [], "symbols": [], "parse_error": None}
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError) as e:
        mod["parse_error"] = str(e)
        return mod, [], [], {}, []
    mod["docstring"] = (ast.get_docstring(tree) or "")[:600] or None

    names = {}      # local name -> (file, symbol|None)  from imports
    local = {}      # qualname -> symbol dict
    routers = {}    # var -> prefix  (APIRouter / FastAPI / Blueprint assignments)
    includes = []   # include_router calls
    routes, models = [], []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                f, _ = index.resolve(al.name, None, path, 0)
                mod["imports"].append({"module": al.name, "name": None, "alias": al.asname,
                                       "line": node.lineno, "resolved": f})
                if f and (al.asname or "." not in al.name):
                    names[al.asname or al.name] = (f, None)
        elif isinstance(node, ast.ImportFrom):
            for al in node.names:
                f, sym = index.resolve(node.module or "", al.name, path, node.level)
                mod["imports"].append({"module": "." * node.level + (node.module or ""),
                                       "name": al.name, "alias": al.asname,
                                       "line": node.lineno, "resolved": f})
                if f:
                    names[al.asname or al.name] = (f, sym)

    def resolve_ref(expr, cls=None):
        """Resolve a dotted expression to 'file::qualname' (or 'file') if we can."""
        if not expr:
            return None
        parts = expr.split(".")
        if parts[0] in ("self", "cls") and cls and len(parts) == 2:
            q = f"{cls}.{parts[1]}"
            return f"{path}::{q}" if q in local else None
        if expr in local:
            return f"{path}::{expr}"
        if parts[0] in names:
            f, sym = names[parts[0]]
            rest = parts[1:]
            q = ".".join(([sym] if sym else []) + rest)
            return f"{f}::{q}" if q else f
        return None

    def collect_calls(fn, cls):
        seen, out = set(), []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                name = dotted(n.func)
                if name and name not in seen:
                    seen.add(name)
                    out.append({"name": name, "line": n.lineno, "resolved": resolve_ref(name, cls)})
        return out

    def add_symbol(node, parent=None):
        q = f"{parent}.{node.name}" if parent else node.name
        is_cls = isinstance(node, ast.ClassDef)
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        sym = {
            "qualname": q,
            "kind": "class" if is_cls else ("method" if parent else "function"),
            "line_start": start, "line_end": node.end_lineno,
            "signature": signature(node),
            "decorators": [unparse(d) for d in node.decorator_list],
            "docstring": (ast.get_docstring(node) or "")[:400] or None,
            "is_public": not node.name.startswith("_") or node.name == "__init__",
        }
        if isinstance(node, ast.AsyncFunctionDef):
            sym["async"] = True
        local[q] = sym
        mod["symbols"].append(sym)
        return sym

    # Pass 1: declare symbols (so calls can resolve forward references).
    pending = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pending.append((node, add_symbol(node), None))
        elif isinstance(node, ast.ClassDef):
            csym = add_symbol(node)
            csym["bases"] = [unparse(b) for b in node.bases]
            fields = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    pending.append((item, add_symbol(item, node.name), node.name))
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fields.append({"name": item.target.id, "annotation": unparse(item.annotation),
                                   "default": unparse(item.value), "line": item.lineno})
            csym["_fields"] = fields
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            callee = (dotted(node.value.func) or "").split(".")[-1]
            if callee in ("APIRouter", "FastAPI", "Blueprint", "Flask"):
                prefix = ""
                for kw in node.value.keywords:
                    if kw.arg in ("prefix", "url_prefix"):
                        prefix = const_str(kw.value) or ""
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        routers[t.id] = {"prefix": prefix, "kind": callee, "line": node.lineno}

    # Pass 2: calls and routes.
    for node, sym, cls in pending:
        sym["calls"] = collect_calls(node, cls)
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            attr, owner = dec.func.attr, dotted(dec.func.value)
            if not owner or attr not in HTTP_METHODS | {"route", "api_route"}:
                continue
            rpath = const_str(dec.args[0]) if dec.args else None
            kws = {kw.arg: kw.value for kw in dec.keywords if kw.arg}
            if rpath is None:
                rpath = const_str(kws.get("path")) if "path" in kws else None
            if rpath is None:
                continue
            methods = [attr.upper()]
            if attr in ("route", "api_route"):
                m = kws.get("methods")
                methods = ([const_str(e).upper() for e in m.elts if const_str(e)]
                           if isinstance(m, (ast.List, ast.Tuple, ast.Set)) else ["GET"])
            for method in methods:
                routes.append({
                    "method": method, "path": rpath, "router": owner,
                    "file": path, "handler": sym["qualname"],
                    "lines": [sym["line_start"], sym["line_end"]],
                    "params": params(node),
                    "returns": unparse(node.returns),
                    "response_model": unparse(kws.get("response_model")),
                    "status_code": unparse(kws.get("status_code")),
                    "tags": unparse(kws.get("tags")),
                    "dependencies": unparse(kws.get("dependencies")),
                })
            sym["route"] = True

    # include_router(...) anywhere in the file.
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("include_router", "register_blueprint") and n.args):
            parent, child = dotted(n.func.value), dotted(n.args[0])
            if not parent or not child:
                continue
            prefix = ""
            for kw in n.keywords:
                if kw.arg in ("prefix", "url_prefix"):
                    prefix = const_str(kw.value) or ""
            cparts = child.split(".")
            if cparts[0] in names:
                f, sym = names[cparts[0]]
                cvar = ".".join(([sym] if sym else []) + cparts[1:])
                target = (f, cvar)
            else:
                target = (path, child)
            includes.append({"parent": (path, parent), "child": target,
                             "prefix": prefix, "line": n.lineno})

    for sym in mod["symbols"]:
        if sym["kind"] == "class":
            models.append({"file": path, "name": sym["qualname"], "bases": sym.get("bases", []),
                           "fields": sym.pop("_fields", []),
                           "lines": [sym["line_start"], sym["line_end"]]})
    return mod, routes, models, routers, includes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    data_dir = os.path.join(os.path.abspath(args.out), "data")
    try:
        with open(os.path.join(data_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except OSError:
        sys.exit("manifest.json not found - run scan_repo.py first")
    root = manifest["root"]
    py = [f["path"] for f in manifest["files"] if f["path"].endswith(".py")]
    index = ModuleIndex(py)

    modules, routes, all_models = {}, [], []
    routers, includes = {}, []
    for path in py:
        with open(os.path.join(root, path), encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        mod, r, m, rt, inc = analyze_file(path, src, index)
        modules[path] = mod
        routes += r
        all_models += m
        for var, info in rt.items():
            routers[(path, var)] = info
        includes += inc

    # Keep only Pydantic-style models (transitively, by simple base name).
    known = set(MODEL_BASES)
    changed = True
    while changed:
        changed = False
        for m in all_models:
            if m["name"] not in known and any((b or "").split(".")[-1].split("[")[0] in known
                                               for b in m["bases"]):
                known.add(m["name"])
                changed = True
    models = [m for m in all_models if m["name"] in known]

    # Compose route prefixes: include chain prefixes + router's own prefix + path.
    parent_of = {}
    for inc in includes:
        parent_of.setdefault(tuple(inc["child"]), inc)

    def chain_prefix(key, depth=0):
        info = routers.get(key)
        own = info["prefix"] if info else ""
        inc = parent_of.get(key)
        if inc and depth < 8:
            up, ok = chain_prefix(tuple(inc["parent"]), depth + 1)
            return up + inc["prefix"] + own, ok
        # Resolved only if we reached a known application object.
        return own, bool(info and info["kind"] in ("FastAPI", "Flask"))

    for r in routes:
        key = (r["file"], r["router"])
        prefix, ok = chain_prefix(key)
        r["full_path"] = (prefix + r["path"]) or "/"
        r["prefix_resolved"] = ok
        r["id"] = f"{r['method']} {r['full_path']}"
    routes.sort(key=lambda r: (r["full_path"], r["method"]))

    edges = sorted({(p, i["resolved"]) for p, m in modules.items()
                    for i in m["imports"] if i["resolved"] and i["resolved"] != p})
    n_sym = sum(len(m["symbols"]) for m in modules.values())
    calls = [c for m in modules.values() for s in m["symbols"] for c in s.get("calls", [])]
    errors = {p: m["parse_error"] for p, m in modules.items() if m["parse_error"]}
    graph = {
        "language": "python",
        "modules": modules,
        "routes": routes,
        "models": models,
        "import_edges": [{"from": a, "to": b} for a, b in edges],
        "stats": {"python_files": len(py), "parsed": len(py) - len(errors),
                  "symbols": n_sym, "routes": len(routes), "models": len(models),
                  "import_edges": len(edges), "calls": len(calls),
                  "calls_resolved": sum(1 for c in calls if c["resolved"])},
        "parse_errors": errors,
    }
    dest = os.path.join(data_dir, "code_graph.json")
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(graph, fh, indent=1, ensure_ascii=False)

    s = graph["stats"]
    print(f"code graph: {dest}")
    print(f"parsed {s['parsed']}/{s['python_files']} python files, {s['symbols']} symbols, "
          f"{s['import_edges']} internal import edges")
    print(f"calls: {s['calls_resolved']}/{s['calls']} resolved to repo symbols (best effort)")
    print(f"routes: {s['routes']}, models: {s['models']}")
    unresolved = [r["id"] for r in routes if not r["prefix_resolved"]]
    if unresolved:
        print(f"routes whose prefix could not be traced to an app object ({len(unresolved)}): "
              + ", ".join(unresolved[:8]) + (" ..." if len(unresolved) > 8 else ""))
    for p, e in list(errors.items())[:10]:
        print(f"parse error: {p}: {e}")
    fan_in = {}
    for _, b in edges:
        fan_in[b] = fan_in.get(b, 0) + 1
    if fan_in:
        print("most imported modules (good places to start reading):")
        for p, n in sorted(fan_in.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {n:>3}  {p}")


if __name__ == "__main__":
    main()
