# Documentation schema

All docs are JSON files under `OUT/docs/`. The validator and the site read
exactly these shapes. Unknown extra fields are ignored. Text fields are plain
text; backticks render as inline code; no Markdown or HTML.

## Contents

- Shared shapes: evidence, claim, symbol reference
- `modules/*.json`
- `overview.json`
- `architecture.json`
- `api.json`
- `flows.json`

## Shared shapes

### Evidence

```json
{ "file": "src/services/question_service.py", "lines": [42, 97], "quote": "if not self.validator.check(q):" }
```

- `file`: repository-relative path exactly as in `manifest.json`.
- `lines`: `[start, end]`, 1-based, inclusive. Cite the narrowest range that
  shows the point: the 3 lines of the `if`, not the whole 80-line function.
  Omit `lines` only when the evidence is the file as a whole.
- `quote` (optional): a snippet copied from within those lines. Whitespace is
  normalised before comparison; everything else must match. Use it on the
  steps that matter most.

### Claim

```json
{ "text": "Rejects questions whose answer is not among the options.",
  "status": "confirmed",
  "evidence": [ { "file": "src/services/validator.py", "lines": [18, 24] } ] }
```

- `status`: `confirmed`, `inferred` or `unknown`.
- `confirmed` requires at least one evidence item.
- `inferred` should cite what the inference rests on.
- `unknown` states what could not be determined; evidence is optional.

### Symbol reference

A string `"<file>::<qualname>"` with the qualname exactly as in
`code_graph.json`, for example `"src/services/question_service.py::QuestionService.generate"`.
The site turns it into a link to the symbol's lines.

## `modules/<any-name>.json` - one per file

Name the file anything unique, for example the path with `/` replaced by `__`.

```json
{
  "file": "src/services/question_service.py",
  "summary": "Turns a teacher's topic request into validated questions: builds the prompt, calls the LLM, rejects malformed output.",
  "responsibilities": [
    { "text": "Retries generation up to 3 times when validation fails.",
      "status": "confirmed",
      "evidence": [ { "file": "src/services/question_service.py", "lines": [61, 70] } ] }
  ],
  "symbols": [
    { "name": "QuestionService.generate",
      "summary": "Generates and validates a set of questions for one topic.",
      "details": "Input: topic, level, count. Output: list of Question. Raises GenerationError after the last failed retry.",
      "status": "confirmed" }
  ],
  "notes": [
    { "text": "The LLM client is injected; which provider is used in production is decided outside this file.",
      "status": "unknown" }
  ],
  "related": ["src/services/validator.py"]
}
```

- `file` and `summary` are required.
- `symbols[].name` is the qualname from the code graph (`Class.method` for
  methods). The symbol's own line range is its implied evidence, so `evidence`
  is only needed to point somewhere more specific. For files the extractor did
  not parse (non-Python), give each symbol entry explicit `evidence`.
- `responsibilities`, `notes`: lists of claims. `related`: file paths.

## `overview.json`

```json
{
  "name": "Trivium",
  "lang": "zh",
  "summary": "Two or three sentences: what the system does, for whom, and its overall shape.",
  "scope": {
    "include": ["src/**"],
    "exclude": ["src/migrations/**"],
    "note": "Covers the API and services. Migrations, tests and the admin frontend are not explained."
  },
  "tech_stack": [
    { "text": "FastAPI for the HTTP layer.", "status": "confirmed",
      "evidence": [ { "file": "pyproject.toml", "lines": [12, 12] } ] }
  ],
  "entry_points": [
    { "text": "`src/main.py` creates the FastAPI app and mounts the routers.", "status": "confirmed",
      "evidence": [ { "file": "src/main.py", "lines": [8, 21] } ] }
  ],
  "reading_guide": [
    { "title": "1. Follow one request", "text": "Start with the question generation flow.", "link": "#/flows/generate-questions" }
  ],
  "open_questions": [
    { "text": "How questions are published to students is not in this repository.", "status": "unknown" }
  ]
}
```

- `lang`: `"zh"` (default) or `"en"`: the language the prose in all docs is
  written in. The site interface is shown in the same language.
- `scope.include` / `scope.exclude`: glob patterns over repository paths;
  coverage is computed over code files that match. Default is everything.
- `reading_guide[].link`: a site link - `#/flows/<id>`, `#/code/<path>`,
  `#/arch/<component id>` or `#/api/<endpoint id>`.

## `architecture.json`

```json
{
  "summary": "A layered service: routers call services, services call the LLM client and repositories.",
  "components": [
    { "id": "api", "name": "HTTP API", "layer": "interface",
      "description": "Validates requests and delegates to services. Holds no business rules.",
      "files": ["src/api/"] }
  ],
  "edges": [
    { "from": "api", "to": "services", "label": "calls use cases", "status": "confirmed",
      "evidence": [ { "file": "src/api/routes.py", "lines": [3, 5] } ] }
  ],
  "notes": []
}
```

- `components[].id`: short, unique, URL-safe. `files`: file paths or directory
  paths (ending in `/`) that exist in the manifest. `layer` is an optional label.
- `edges[].from` / `to`: component ids. Direction is "depends on / calls".
  Keep labels under ~25 characters; the diagram truncates longer ones.
- The diagram is laid out from the edges, top to bottom, so point edges from
  callers to callees.

## `api.json`

```json
{
  "endpoints": [
    { "id": "POST /api/questions/generate",
      "summary": "Generates questions for a topic and returns them without saving.",
      "status": "confirmed",
      "auth": { "text": "Requires a teacher token via `get_current_teacher`.", "status": "confirmed",
                "evidence": [ { "file": "src/api/routes.py", "lines": [30, 31] } ] },
      "errors": [
        { "text": "502 when the LLM output fails validation after all retries.", "status": "confirmed",
          "evidence": [ { "file": "src/api/routes.py", "lines": [40, 43] } ] }
      ],
      "notes": [] }
  ]
}
```

- `id` is `"<METHOD> <full path>"` and must equal the `id` of a route in
  `code_graph.json` so the description attaches to the extracted route.
- An `id` with no extracted route is shown as "described from source" and must
  carry `evidence` for where it is defined.
- Parameters, response models and handler locations come from the parser; do
  not repeat them.

## `flows.json`

```json
{
  "flows": [
    {
      "id": "generate-questions",
      "title": "Teacher generates questions with AI",
      "question": "What happens between the request and the validated question list?",
      "summary": "The route validates the request, the service prompts the LLM, and the output is checked before it is returned.",
      "trigger": "`POST /api/questions/generate`",
      "status": "confirmed",
      "steps": [
        { "id": "route",
          "title": "Route receives the request",
          "description": "FastAPI parses the body into `GenerateRequest` and resolves the current teacher.",
          "status": "confirmed",
          "symbols": ["src/api/routes.py::generate_questions"],
          "evidence": [ { "file": "src/api/routes.py", "lines": [28, 36] } ],
          "branches": [
            { "condition": "token missing or invalid", "text": "The dependency raises 401 before the handler runs.",
              "status": "confirmed",
              "evidence": [ { "file": "src/api/deps.py", "lines": [12, 19] } ] }
          ] }
      ],
      "gaps": [
        { "text": "What the LLM provider does with the prompt is outside the repository.", "status": "unknown" }
      ]
    }
  ]
}
```

- `id`: short, unique, URL-safe. Steps are shown in list order.
- Every step needs `evidence`, unless its `status` is `unknown`.
- `symbols` on a step is what links the flow to the Code and API views (a file's
  page lists the flows that pass through it), so include the function that
  does the step's work.
- A flow's own `status` is `confirmed` only if every step is; otherwise `inferred`.
- `gaps`: claims, usually `unknown`, for where the trail leaves the code you can see.
