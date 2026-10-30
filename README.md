
# ASSET

ASSET is a local-first software code intelligence system. It reads an uploaded project in the browser, builds a deterministic inventory, asks local Ollama `<span>qwen2.5-coder:7b</span>` for a grounded project profile, searches public GitHub, retrieves actual reference source, computes code matches, and produces an evidence-based comparison report.

**Docker is intentionally removed from this version. Uploaded and retrieved code is never executed.**

## Run

Python 3.10+ is required. The backend uses only the Python standard library.

```
python server.py
```

Open `<span>http://127.0.0.1:8000</span>`.

## Ollama

Ollama should be running locally with `<span>qwen2.5-coder:7b</span>` available. The backend defaults to `<span>http://127.0.0.1:11434</span>`. You can override it with environment variables:

```
$env:OLLAMA_URL = "http://127.0.0.1:11434"
$env:OLLAMA_MODEL = "qwen2.5-coder:7b"
python server.py
```

## GitHub

Public repository search and source retrieval use the GitHub REST API. An optional fine-grained GitHub token can be supplied only to the backend:

```
$env:GITHUB_TOKEN = "your-token"
python server.py
```

Never place the token in frontend JavaScript, HTML, browser storage, prompts, or analysis logs.

## Workflow

1. Upload a complete local project folder.
2. ASSET inventories paths, languages, framework signals, dependencies, symbols, configuration and tests.
3. Local Qwen produces a grounded project profile.
4. Search GitHub for comparable public repositories.
5. Select up to five references.
6. ASSET retrieves actual source files from the selected repositories.
7. ASSET computes exact normalized matches and structural code similarity candidates.
8. Qwen compares the actual local source with the retrieved reference source.
9. The report covers architecture, features, code quality, security, performance, dependencies and criteria-based assessments.
10. Analysis history is persisted under `<span>logs/</span>`. Retrieved GitHub text is cached under `<span>github-cache/</span>`.

## Comparison philosophy

ASSET does not treat GitHub stars, forks or popularity as proof of code quality. It also does not claim that two projects are the same merely because their filenames are similar. File/function relationships and code similarities must be supported by retrieved source.

When the user asks which implementation is stronger, ASSET evaluates explicit criteria such as security, architecture, maintainability or performance and explains the evidence. If evidence is insufficient, the report says so rather than inventing a conclusion. Similarity alone is never labeled plagiarism.

## API

* `<span>POST /api/analyze-project</span>`
* `<span>POST /api/github/search</span>`
* `<span>POST /api/github/repository</span>`
* `<span>POST /api/github/compare</span>`
* `<span>GET /api/config</span>`
* `<span>GET /api/github/rate-limit</span>`
* `<span>GET /api/logs</span>`
* `<span>GET /api/logs/{analysisId}</span>`

## Privacy and execution

The browser reads the selected project and sends source to the local ASSET backend and configured local Ollama. GitHub source is retrieved by the backend. No uploaded or retrieved source is automatically executed.
