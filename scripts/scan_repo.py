#!/usr/bin/env python3
"""Phase 1 - Repository discovery.

Walks a repository (read-only) and writes <out>/data/manifest.json:
file tree, languages, line counts, framework hints, entry-point hints,
and a list of files that were excluded and why.

Usage: python3 scan_repo.py <repo> --out <output_dir>
Standard library only. Never executes code from the target repository.
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time

EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env", "__pycache__",
    "dist", "build", ".next", ".nuxt", "target", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".idea", ".vscode", "vendor", "site-packages",
    ".gradle", ".terraform", "coverage", ".cache",
}
SECRET_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa*", "id_ed25519*", "credentials", "credentials.*", "secrets.*",
    "*.secret", ".netrc", ".npmrc", ".pypirc",
]
SECRET_ALLOW = {".env.example", ".env.sample", ".env.template"}
GENERATED_PATTERNS = [
    "*.min.js", "*.min.css", "*.map", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock", "go.sum",
    "*.pb.go", "*_pb2.py", "*_pb2_grpc.py", "*.generated.*",
]
LANGS = {
    ".py": "Python", ".pyi": "Python", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby",
    ".php": "PHP", ".cs": "C#", ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++",
    ".swift": "Swift", ".scala": "Scala", ".sh": "Shell", ".sql": "SQL",
    ".md": "Markdown", ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
    ".toml": "TOML", ".html": "HTML", ".css": "CSS", ".vue": "Vue", ".svelte": "Svelte",
    ".proto": "Protobuf", ".tf": "Terraform", ".ini": "INI", ".cfg": "INI",
}
DOC_LANGS = {"Markdown", "JSON", "YAML", "TOML", "INI", "HTML", "CSS"}
FRAMEWORK_HINTS = {  # dependency-file keyword -> framework name
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "starlette": "Starlette",
    "sqlalchemy": "SQLAlchemy", "pydantic": "Pydantic", "celery": "Celery",
    "langgraph": "LangGraph", "langchain": "LangChain", "anthropic": "Anthropic SDK",
    "openai": "OpenAI SDK", "typer": "Typer", "click": "Click", "pytest": "pytest",
    "express": "Express", "react": "React", "next": "Next.js", "vue": "Vue",
    "nestjs": "NestJS", "@nestjs/core": "NestJS", "prisma": "Prisma",
}
DEP_FILES = {"requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
             "Pipfile", "package.json", "go.mod", "Cargo.toml"}
ENTRY_NAMES = {
    "main.py", "app.py", "manage.py", "__main__.py", "wsgi.py", "asgi.py", "cli.py",
    "server.py", "index.ts", "index.js", "main.ts", "main.js", "server.ts",
    "server.js", "main.go", "main.rs",
}
MAX_BYTES = 1_000_000


def matches(name, patterns):
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def list_files(repo, out_abs):
    """Prefer git (honours .gitignore); fall back to a plain walk."""
    try:
        res = subprocess.run(
            ["git", "-C", repo, "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True, timeout=60)
        if res.returncode == 0 and res.stdout:
            paths = [p for p in res.stdout.decode("utf-8", "replace").split("\0") if p]
            return sorted(set(paths)), "git ls-files"
    except (OSError, subprocess.SubprocessError):
        pass
    paths = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS
                         and os.path.abspath(os.path.join(root, d)) != out_abs)
        for f in files:
            paths.append(os.path.relpath(os.path.join(root, f), repo).replace(os.sep, "/"))
    return sorted(paths), "filesystem walk"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repo")
    ap.add_argument("--out", required=True, help="output directory (not inside source dirs)")
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)
    out_abs = os.path.abspath(args.out)
    if not os.path.isdir(repo):
        sys.exit(f"not a directory: {repo}")

    paths, method = list_files(repo, out_abs)
    files, excluded, frameworks, entries = [], [], {}, []
    languages = {}

    for rel in paths:
        parts = rel.split("/")
        name = parts[-1]
        full = os.path.join(repo, rel)
        if os.path.abspath(full).startswith(out_abs + os.sep):
            continue
        if any(p in EXCLUDE_DIRS or p.endswith(".egg-info") for p in parts[:-1]):
            excluded.append({"path": rel, "reason": "dependency or build directory"})
            continue
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        if name not in SECRET_ALLOW and matches(name, SECRET_PATTERNS):
            excluded.append({"path": rel, "reason": "possible secret"})
            continue
        size = os.path.getsize(full)
        if matches(name, GENERATED_PATTERNS):
            excluded.append({"path": rel, "reason": "generated or lock file"})
            continue
        if size > MAX_BYTES:
            excluded.append({"path": rel, "reason": f"larger than {MAX_BYTES} bytes"})
            continue
        try:
            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            excluded.append({"path": rel, "reason": f"unreadable: {e}"})
            continue
        if b"\0" in raw[:8192]:
            excluded.append({"path": rel, "reason": "binary"})
            continue
        text = raw.decode("utf-8", "replace")
        lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        ext = os.path.splitext(name)[1].lower()
        lang = LANGS.get(ext) or ("Dockerfile" if name.startswith("Dockerfile") else "Other")
        files.append({"path": rel, "language": lang, "lines": lines, "bytes": size,
                      "is_code": lang not in DOC_LANGS and lang != "Other",
                      "is_test": bool(re.search(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|"
                                                r"_test\.[a-z]+$|\.(test|spec)\.[a-z]+$", rel))})
        agg = languages.setdefault(lang, {"files": 0, "lines": 0})
        agg["files"] += 1
        agg["lines"] += lines

        if name in DEP_FILES:
            for i, line in enumerate(text.splitlines(), 1):
                low = line.lower()
                for key, fw in FRAMEWORK_HINTS.items():
                    if fw not in frameworks and re.search(
                            r"(^|[\"'\s\[,=])" + re.escape(key) + r"($|[\"'\s\[\]<>=~!,;:^])", low):
                        frameworks[fw] = {"name": fw, "evidence": {"file": rel, "lines": [i, i]}}
        if name in ENTRY_NAMES and len(parts) <= 4:
            entries.append({"file": rel, "reason": f"conventional entry file name ({name})"})
        if ext == ".py":
            m = re.search(r"^if __name__ == [\"']__main__[\"']", text, re.M)
            if m:
                ln = text.count("\n", 0, m.start()) + 1
                entries.append({"file": rel, "reason": "__main__ guard", "lines": [ln, ln]})
        if name == "pyproject.toml":
            in_scripts = False
            for i, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if s.startswith("["):
                    in_scripts = s in ("[project.scripts]", "[tool.poetry.scripts]")
                elif in_scripts and "=" in s:
                    entries.append({"file": rel, "reason": f"console script: {s}", "lines": [i, i]})
        if name == "package.json":
            try:
                pkg = json.loads(text)
                for k in ("main", "bin"):
                    if pkg.get(k):
                        entries.append({"file": rel, "reason": f"package.json {k}: {pkg[k]}"})
            except ValueError:
                pass

    code = [f for f in files if f["is_code"]]
    manifest = {
        "repo_name": os.path.basename(repo.rstrip("/")),
        "root": repo,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "listing_method": method,
        "stats": {"files": len(files), "code_files": len(code),
                  "code_lines": sum(f["lines"] for f in code),
                  "excluded": len(excluded)},
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1]["lines"])),
        "frameworks": list(frameworks.values()),
        "entry_points": entries,
        "files": files,
        "excluded": excluded,
    }
    os.makedirs(os.path.join(out_abs, "data"), exist_ok=True)
    dest = os.path.join(out_abs, "data", "manifest.json")
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, ensure_ascii=False)

    s = manifest["stats"]
    print(f"manifest: {dest}")
    print(f"files: {s['files']} ({s['code_files']} code, {s['code_lines']} code lines), "
          f"excluded: {s['excluded']} [{method}]")
    print("languages: " + ", ".join(f"{k} {v['lines']}L/{v['files']}f"
                                    for k, v in list(manifest["languages"].items())[:8]))
    print("frameworks: " + (", ".join(f["name"] for f in manifest["frameworks"]) or "none detected"))
    print("entry point hints: " + (", ".join(sorted({e['file'] for e in entries})[:12]) or "none"))
    # Directory size summary helps the agent choose a documentation scope.
    dirs = {}
    for f in code:
        top = "/".join(f["path"].split("/")[:2]) if "/" in f["path"] else "."
        d = dirs.setdefault(top, [0, 0])
        d[0] += 1
        d[1] += f["lines"]
    print("largest code areas:")
    for k, (n, ln) in sorted(dirs.items(), key=lambda kv: -kv[1][1])[:15]:
        print(f"  {k:<40} {n:>4} files {ln:>7} lines")


if __name__ == "__main__":
    main()
