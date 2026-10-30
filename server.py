"""ASSET local project intelligence backend.

This service analyzes uploaded source and retrieves public GitHub source for
comparison. It never executes uploaded or retrieved code.
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / "logs"
CACHE_ROOT = ROOT / "github-cache"
MAX_BODY_BYTES = 25 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_GITHUB_FILE_BYTES = 512 * 1024
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = ""#change with your token 
IGNORED_PARTS = {".git", "node_modules", "venv", ".venv", "dist", "build", "coverage", ".next", ".cache", "vendor"}
TEXT_SUFFIXES = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php", ".cs", ".c", ".cpp", ".h", ".hpp", ".html", ".css", ".scss", ".vue", ".svelte", ".sql", ".json", ".yaml", ".yml", ".md", ".txt", ".toml", ".ini", ".cfg", ".xml"}


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str = "analysis") -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def safe_relative_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Every project path must be a non-empty string.")
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if normalized.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"Absolute paths are not allowed: {value}")
    if any(part in ("", ".", "..") for part in pure.parts):
        raise ValueError(f"Unsafe project path: {value}")
    if any(part.lower() in IGNORED_PARTS for part in pure.parts):
        raise ValueError(f"Ignored/generated project path is not accepted: {value}")
    return Path(*pure.parts)


def safe_content(value: object, limit: int = MAX_FILE_BYTES) -> str:
    if not isinstance(value, str):
        raise ValueError("File content must be text.")
    if len(value.encode("utf-8")) > limit:
        raise ValueError("File is too large for local analysis.")
    return value


def normalize_files(raw: object) -> dict[str, str]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("projectFiles must contain at least one file.")
    files: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each project file must be an object.")
        path = safe_relative_path(item.get("path")).as_posix()
        files[path] = safe_content(item.get("content"))
    return files


def package_info(files: dict[str, str]) -> dict:
    for path, content in files.items():
        if Path(path).name.lower() == "package.json":
            try:
                package = json.loads(content)
            except json.JSONDecodeError:
                return {"package": {}, "dependencies": {}, "scripts": {}}
            dependencies = {}
            for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                if isinstance(package.get(section), dict):
                    dependencies.update(package[section])
            return {"package": package, "dependencies": dependencies, "scripts": package.get("scripts", {})}
    return {"package": {}, "dependencies": {}, "scripts": {}}


PYTHON_STDLIB = {"abc","argparse","asyncio","base64","collections","contextlib","csv","dataclasses","datetime","difflib","enum","functools","glob","hashlib","http","inspect","io","itertools","json","logging","math","os","pathlib","re","secrets","shutil","socket","sqlite3","statistics","string","subprocess","sys","tempfile","textwrap","threading","time","traceback","typing","types","unittest","urllib","uuid","warnings","xml","zipfile"}

def imported_dependencies(files: dict[str, str]) -> list[str]:
    found = set()
    for path, content in files.items():
        suffix = Path(path).suffix.lower()
        if suffix == ".py":
            for module in re.findall(r"^\s*import\s+([A-Za-z_][\w.]*)", content, re.MULTILINE):
                root = module.split(".")[0]
                if root.lower() not in PYTHON_STDLIB:
                    found.add(root)
            for module in re.findall(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+", content, re.MULTILINE):
                root = module.split(".")[0]
                if root.lower() not in PYTHON_STDLIB:
                    found.add(root)
        elif suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
            for module in re.findall(r"(?:from\s+|require\(\s*[\"'])([A-Za-z0-9_@./-]+)", content):
                if module.startswith(".") or module.startswith("/"):
                    continue
                found.add(module.split("/")[0] if not module.startswith("@") else "/".join(module.split("/")[:2]))
    return sorted(found, key=str.lower)


def inventory(files: dict[str, str]) -> dict:
    package = package_info(files)
    paths = list(files)
    config_names = {"package.json", "tsconfig.json", "next.config.js", "next.config.ts", "vite.config.ts", "jest.config.ts", "pyproject.toml", "requirements.txt"}
    source_dirs = sorted({path.split("/")[0] for path in paths if "/" in path and Path(path).suffix.lower() in TEXT_SUFFIXES})
    tests = [path for path in paths if re.search(r"(^|/)(tests?|__tests__)(/|$)|\.(test|spec)\.", path, re.I)]
    docs = [path for path in paths if Path(path).suffix.lower() in {".md", ".txt"} or Path(path).name.lower().startswith("readme")]
    language_counts: dict[str, int] = {}
    for path in paths:
        suffix = Path(path).suffix.lower()
        language = {".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript", ".java": "Java", ".go": "Go", ".rs": "Rust"}.get(suffix)
        if language:
            language_counts[language] = language_counts.get(language, 0) + 1
    language = max(language_counts, key=language_counts.get) if language_counts else "Unknown"
    deps = sorted(set(package["dependencies"].keys()) | set(imported_dependencies(files)), key=str.lower)
    framework = "Next.js" if "next" in deps or any("next.config" in path for path in paths) else ("React" if "react" in deps else ("FastAPI" if any("fastapi" in content.lower() for content in files.values()) else "Unknown"))
    package_manager = "npm" if "package-lock.json" in {Path(path).name for path in paths} else ("pip" if "requirements.txt" in {Path(path).name for path in paths} else "Unknown")
    functions = []
    for path, content in files.items():
        if Path(path).suffix.lower() not in TEXT_SUFFIXES:
            continue
        functions.extend(f"{path}:{match}" for match in re.findall(r"(?:def|function|class)\s+([A-Za-z_]\w*)", content)[:30])
    return {"fileCount": len(paths), "sourceDirectories": source_dirs, "configFiles": [path for path in paths if Path(path).name in config_names], "testFiles": tests, "documentationFiles": docs, "language": language, "framework": framework, "packageManager": package_manager, "dependencies": deps, "symbols": functions[:100]}


def strip_json_fences(text: str) -> str:
    value = text.strip()
    if value.startswith("```json"):
        value = value[7:].strip()
    elif value.startswith("```JSON"):
        value = value[7:].strip()
    elif value.startswith("```"):
        value = value[3:].strip()
    if value.endswith("```"):
        value = value[:-3].strip()
    return value


def extract_json(raw: object) -> object:
    if isinstance(raw, (dict, list)):
        return raw

    if not isinstance(raw, str):
        raise ValueError("Ollama response is not text or JSON.")

    text = strip_json_fences(raw)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError(
            "Ollama returned invalid JSON. No JSON object/array was found."
        )

    decoder = json.JSONDecoder()
    try:
        parsed, _ = decoder.raw_decode(text[min(starts):])
        return parsed
    except json.JSONDecodeError as exc:
        preview = text[:1500].replace("\n", "\\n")
        print("[asset] Ollama raw response preview:", preview)
        raise ValueError(
            "Ollama returned invalid JSON. "
            f"Response preview: {preview}"
        ) from exc


PROJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {
            "type": "string"
        },
        "structure": {
            "type": "string"
        },
        "features": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },
        "dependencies": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },
        "risks": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },
        "unknowns": {
            "type": "array",
            "items": {
                "type": "string"
            }
        }
    },
    "required": [
        "project",
        "structure",
        "features",
        "dependencies",
        "risks",
        "unknowns"
    ]
}


GITHUB_DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "project_summary": {"type": "string"},
        "languages": {"type": "array", "items": {"type": "string"}},
        "frameworks": {"type": "array", "items": {"type": "string"}},
        "libraries": {"type": "array", "items": {"type": "string"}},
        "technologies": {"type": "array", "items": {"type": "string"}},
        "project_concepts": {"type": "array", "items": {"type": "string"}},
        "implemented_capabilities": {"type": "array", "items": {"type": "string"}},
        "github_queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "project_summary",
        "languages",
        "frameworks",
        "libraries",
        "technologies",
        "project_concepts",
        "implemented_capabilities",
        "github_queries",
    ],
}

COMPARISON_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "localFile": {"type": "string"},
                    "referenceRepository": {"type": "string"},
                    "referenceFile": {"type": "string"},
                    "similarity": {"type": "number"},
                    "type": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": [
                    "localFile", "referenceRepository", "referenceFile",
                    "similarity", "type", "evidence",
                ],
            },
        },
        "architecture": {"type": "string"},
        "features": {"type": "array", "items": {"type": "string"}},
        "codeQuality": {"type": "array", "items": {"type": "string"}},
        "security": {"type": "array", "items": {"type": "string"}},
        "performance": {"type": "array", "items": {"type": "string"}},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "criteria": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "overview", "matches", "architecture", "features", "codeQuality",
        "security", "performance", "dependencies", "recommendations",
        "criteria", "evidence", "unknowns",
    ],
}


def ollama(
    prompt: str,
    ollama_url: str | None = None,
    model: str | None = None,
    schema: dict | None = None,
) -> dict:
    endpoint = (ollama_url or OLLAMA_URL).rstrip("/")
    selected_model = (model or OLLAMA_MODEL).strip() or OLLAMA_MODEL

    body = {
        "model": selected_model,
        "prompt": prompt,
        "stream": False,
        "format": schema or {"type": "object"},
        "options": {
            "temperature": 0.1,
        },
    }

    print("\n" + "=" * 100)
    print("OLLAMA REQUEST")
    print("=" * 100)
    print("Endpoint:", endpoint)
    print("Model:", selected_model)
    print("Prompt length:", len(prompt))
    print("Schema supplied:", schema is not None)
    print("=" * 100 + "\n")

    request = urllib.request.Request(
        f"{endpoint}/api/generate",
        data=json_bytes(body),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = json.loads(
                response.read().decode("utf-8", errors="replace")
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1500]
        raise ValueError(
            f"Ollama HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ValueError(
            f"Ollama is unavailable at {endpoint}. "
            f"Make sure Ollama is running and '{selected_model}' is installed. "
            f"Details: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise ValueError("Ollama timed out while generating the analysis.") from exc

    raw = payload.get("response", "")

    print("\n" + "=" * 100)
    print("OLLAMA RAW RESPONSE")
    print("=" * 100)
    print(repr(raw))
    print("=" * 100)
    print("OLLAMA RAW RESPONSE END")
    print("=" * 100 + "\n")

    if not raw:
        raise ValueError("Ollama returned an empty response.")

    try:
        result = extract_json(raw)
    except Exception as exc:
        print("\n" + "=" * 100)
        print("JSON PARSING FAILED")
        print("=" * 100)
        print("RAW RESPONSE:")
        print(repr(raw))
        print("\nPARSING ERROR:")
        print(repr(exc))
        print("=" * 100 + "\n")

        raise ValueError(
            "Ollama returned invalid JSON. "
            "The complete raw response has been printed in the server terminal."
        ) from exc

    if not isinstance(result, dict):
        print("OLLAMA RETURNED:", repr(result))
        raise ValueError("Ollama returned JSON, but not a JSON object.")

    return result


def validate_project_result(result: dict) -> dict:
    defaults = {
        "project": "Unknown",
        "structure": "Unknown",
        "features": [],
        "dependencies": [],
        "risks": [],
        "unknowns": [],
    }

    for key, default in defaults.items():
        if not isinstance(result.get(key), type(default)):
            result[key] = default

    return result

def validate_comparison_result(result: dict) -> dict:
    defaults = {
        "overview": "No overview returned.",
        "matches": [],
        "architecture": "Unknown",
        "features": [],
        "codeQuality": [],
        "security": [],
        "performance": [],
        "dependencies": [],
        "recommendations": [],
        "criteria": [],
        "evidence": [],
        "unknowns": [],
    }
    for key, default in defaults.items():
        if not isinstance(result.get(key), type(default)):
            result[key] = default
    return result


def analysis_prompt(files: dict[str, str], facts: dict) -> str:
    source = "".join(
        f"\n\n================ FILE: {path} ================\n"
        f"{content[:100000]}"
        for path, content in files.items()
    )

    return f"""
You are ASSET, an autonomous software engineering and code intelligence engine.

Your task is to perform a DEEP SOFTWARE PROJECT ANALYSIS of the complete
source code provided below.

This is NOT a short project summary.

You must inspect and reason about the ENTIRE provided project before producing
the result.

============================================================
CORE OBJECTIVE
============================================================

Build a detailed understanding of:

1. What the software actually does.
2. How the software is architected.
3. How the files and modules interact.
4. What functionality is actually implemented.
5. Which technologies and dependencies are actually used.
6. How data flows through the application.
7. What APIs, routes, services, models, components, and utilities exist.
8. What technical risks are visible in the implementation.
9. What important information cannot be determined from the source.

The final result must be detailed enough that a software engineer who has
never seen this project can understand its architecture and implementation
without opening every source file.

============================================================
CRITICAL: ANALYZE THE ENTIRE PROJECT
============================================================

You MUST inspect EVERY provided source file.

There are {facts.get("fileCount", "multiple")} source files.

Do NOT analyze only:
- main.py
- server.py
- index.js
- App.jsx
- package.json
- README
- or any other entry-point file.

The entry point is only one part of the project.

Trace relationships between files.

For example:

frontend
    ->
API request
    ->
backend route
    ->
service/function
    ->
external API/model/database
    ->
response
    ->
frontend

If such a flow exists in the source, explain it.

============================================================
EVIDENCE POLICY
============================================================

EVERY important claim must be supported by the provided source.

Use actual:
- file paths
- imports
- functions
- classes
- routes
- API calls
- configuration
- environment variables
- components
- modules
- data structures
- package manifests

Do NOT invent anything.

Do NOT infer a feature merely because:
- the project name suggests it
- a filename suggests it
- a comment mentions it
- a README describes it
- it is common for similar projects

A feature should be considered IMPLEMENTED only when source code provides
evidence that it exists.

If documentation says a feature exists but implementation cannot be found,
do NOT report it as implemented. Put the uncertainty in "unknowns".

============================================================
PROJECT FIELD
============================================================

The "project" field MUST contain a detailed explanation.

Write approximately 3–6 sentences.

Explain:

- actual purpose of the application
- application type
- frontend/backend architecture if present
- primary technologies
- important external services
- major implemented workflow
- how the major parts work together

DO NOT return only the project name.

DO NOT claim functionality simply because the project appears to be
designed for that purpose.

============================================================
STRUCTURE FIELD
============================================================

The "structure" field MUST provide a detailed architectural description.

Write approximately 5–10 sentences.

DO NOT return only:
"server/main.py"

or:
"src folder"

or:
"frontend and backend".

Describe:

- root-level structure
- frontend directory
- backend directory
- important source files
- entry points
- configuration files
- API modules
- service modules
- UI components
- utility modules
- model-related modules
- database-related modules if present
- relationships between important files

Mention actual file paths.

Do not invent paths. Use only paths that actually exist.

============================================================
FEATURES FIELD
============================================================

The "features" array must contain a COMPREHENSIVE list of implemented
capabilities.

Do NOT stop after 3 features.

Analyze ALL source files and identify every meaningful user-facing or
system-level capability that is actually implemented.

Aim for approximately 8–20 features when the source supports that many.

Possible categories to inspect:

FRONTEND
- pages
- screens
- forms
- editors
- buttons/actions
- state management
- API integration
- streaming UI
- error display
- loading states
- file handling
- authentication UI

BACKEND
- API endpoints
- request models
- response models
- validation
- prompt construction
- model calls
- streaming
- error handling
- health checks
- configuration
- middleware
- CORS
- logging

APPLICATION LOGIC
- parsing
- transformation
- processing
- search
- generation
- analysis
- storage
- caching
- external service integration

For EVERY feature:

1. Explain what the feature actually does.
2. Mention the actual file path.
3. Mention the relevant route/function/component when useful.

Do not simply write:
"Chat endpoint"

============================================================
DEPENDENCIES FIELD
============================================================

Analyze all actual dependencies.

Inspect:

- package.json
- requirements.txt
- pyproject.toml
- imports
- lock files
- configuration
- SDK usage

Return approximately 8–20 dependencies when actually present.

For each dependency explain:

DEPENDENCY
+
WHERE IT IS USED
+
WHAT IT DOES

Do NOT list a dependency merely because its name appears in documentation.

============================================================
RISKS FIELD
============================================================

Perform a REAL CODE-LEVEL RISK ANALYSIS.

Do NOT return generic labels such as:

"Security"
"Performance"
"Error handling"

Instead explain the actual issue.

Inspect for:

- missing input validation
- missing authentication
- missing authorization
- exposed endpoints
- unsafe CORS
- hard-coded secrets
- environment configuration
- unhandled exceptions
- missing timeouts
- blocking operations
- unlimited request sizes
- resource exhaustion
- concurrency problems
- unsafe subprocess usage
- unsafe file operations
- injection risks
- external API failures
- model failures
- missing logging
- missing monitoring
- missing tests
- duplicated logic
- tight coupling
- poor separation of concerns

Only report risks supported by actual source.

Each risk should contain:

WHAT is the risk
+
WHERE it occurs
+
WHY it matters

============================================================
UNKNOWNS FIELD
============================================================

Only include genuinely unknown information.

Do NOT put obvious information here.

Do NOT say:
"The UI is unknown"
when frontend source is provided.

Do NOT say:
"The backend is unknown"
when backend source is provided.

============================================================
ARCHITECTURE AND DATA FLOW
============================================================

Although the JSON schema does not have a separate architecture field,
include architecture and data-flow information inside the "project" and
"structure" fields.

Trace:

USER INPUT
    ↓
FRONTEND
    ↓
API REQUEST
    ↓
BACKEND ROUTE
    ↓
BUSINESS LOGIC
    ↓
EXTERNAL SERVICE / MODEL / DATABASE
    ↓
BACKEND RESPONSE
    ↓
FRONTEND
    ↓
USER

Only describe stages that actually exist.

============================================================
PROJECT INVENTORY
============================================================

The following inventory was computed locally from the project:

{json.dumps(facts, ensure_ascii=False, indent=2)}

Use this inventory as supporting evidence, but ALWAYS verify important claims
against the actual source code.

============================================================
COMPLETE SOURCE CODE
============================================================

{source}

============================================================
FINAL QUALITY REQUIREMENTS
============================================================

Before generating the final JSON, internally perform the following analysis:

STEP 1:
Identify every provided file.

STEP 2:
Identify the role of every important file.

STEP 3:
Identify the application's entry points.

STEP 4:
Identify all frontend/backend boundaries.

STEP 5:
Trace important data flows.

STEP 6:
Identify all meaningful implemented features.

STEP 7:
Identify all actual dependencies.

STEP 8:
Identify concrete source-supported risks.

STEP 9:
Identify genuinely unknown information.

STEP 10:
Cross-check every claim against the source.

STEP 11:
Remove anything that is speculation or unsupported inference.

STEP 12:
Make the final answer detailed and comprehensive.

IMPORTANT:

A SHORT ANSWER IS NOT ACCEPTABLE.

Do not stop after identifying the entry point.

Do not stop after identifying three API endpoints.

Do not summarize the project in only a few words.

Use the available output fields to provide as much useful,
source-supported engineering information as possible.

============================================================
OUTPUT FORMAT
============================================================

Return EXACTLY this JSON object:

{{
  "project": "Detailed 3–6 sentence explanation of the complete project",
  "structure": "Detailed 5–10 sentence explanation of the actual project structure, architecture, important files, and relationships",
  "features": [
    "Detailed implemented feature with file-path evidence"
  ],
  "dependencies": [
    "Dependency + where it is used + its role"
  ],
  "risks": [
    "Specific source-supported technical risk + location + explanation"
  ],
  "unknowns": [
    "Genuinely unknown information"
  ]
}}

============================================================
STRICT JSON RULES
============================================================

- Return ONLY the JSON object.
- No Markdown.
- No ```json.
- No code fences.
- No explanation before JSON.
- No explanation after JSON.
- No comments.
- All array elements must be strings.
- Escape quotes correctly.
- Ensure valid JSON.
- The result must be directly parseable by Python json.loads().
- Do not output your internal reasoning.
- Do not output the validation process.

FINAL INSTRUCTION:

Perform a deep, comprehensive software engineering analysis of ALL provided
source files and return ONLY the final JSON object.
""".strip()


def github_discovery_prompt(
    files: dict[str, str],
    facts: dict,
    profile: dict,
) -> str:
    source = "".join(
        f"\n--- FILE: {path} ---\n{content[:60000]}"
        for path, content in files.items()
    )

    return f"""
You are the ASSET GitHub repository discovery engine.

FIRST PRINCIPLE:
The project has ALREADY been analyzed by ASSET. Your job now is NOT to
reanalyze it from scratch. Use the verified project analysis and source
evidence to create high-quality GitHub repository search queries.

GOAL:
Find PUBLIC GitHub repositories whose actual implementation is likely to
be similar to this project.

IMPORTANT:
- Do NOT search using only the project name.
- Do NOT search using generic terms such as "software project", "AI project",
  "Python project", or "web app".
- Use distinctive combinations of actual technologies, implemented
  capabilities, APIs, architecture, and project concepts.
- Queries must be suitable for GitHub repository search.
- Prefer several independent queries because one query can miss relevant
  repositories.
- Do not invent technologies or features.
- Every query must be derived from the supplied analysis/source.
- Do not include stars, forks, popularity, or quality judgments.
- Do not attempt to identify a repository by guessing an exact repository name.

SEARCH STRATEGY:
Create 4–8 queries from different angles:
1. technology + framework + core capability
2. framework + distinctive implementation pattern
3. language + external service/model + capability
4. core capability + architecture
5. distinctive API/integration combination
6. a broader fallback query
7. another fallback using a different vocabulary
8. optional highly specific query

A query should normally contain 2–5 meaningful terms.
Avoid excessively long natural-language sentences.

The queries are DISCOVERY QUERIES, not proof of similarity.
Actual source comparison happens later after repositories are retrieved.

PROJECT INVENTORY:
{json.dumps(facts, ensure_ascii=False, indent=2)}

VERIFIED PROJECT ANALYSIS:
{json.dumps(profile, ensure_ascii=False, indent=2)}

SOURCE CODE:
{source}

RETURN ONLY THIS JSON:

{{
  "project_summary": "One factual sentence describing the implementation that should guide repository discovery.",
  "languages": ["actual language"],
  "frameworks": ["actual framework"],
  "libraries": ["actual library"],
  "technologies": ["actual technology or external service"],
  "project_concepts": ["distinctive implementation concept"],
  "implemented_capabilities": ["actual implemented capability"],
  "github_queries": [
    "query 1",
    "query 2",
    "query 3",
    "query 4"
  ]
}}

FINAL RULES:
- Return ONLY valid JSON.
- No Markdown.
- No code fences.
- No commentary.
- Never invent evidence.
- Every query must be traceable to the project analysis or source.
""".strip()


def normalize_discovery_queries(result: dict) -> list[str]:
    raw = result.get("github_queries", [])
    if not isinstance(raw, list):
        return []

    queries = []
    seen = set()

    for item in raw:
        if not isinstance(item, str):
            continue

        query = re.sub(r"\s+", " ", item).strip()
        if not query:
            continue

        # GitHub repository search should receive compact queries.
        if len(query) > 180:
            query = query[:180].rsplit(" ", 1)[0].strip()

        key = query.lower()
        if key in seen:
            continue

        seen.add(key)
        queries.append(query)

    return queries[:8]


def fallback_discovery(facts: dict, profile: dict) -> dict:
    """Create useful deterministic GitHub queries if the second Ollama call times out."""
    deps = facts.get("dependencies", [])
    language = facts.get("language", "")
    framework = facts.get("framework", "")
    text = " ".join(str(v) for v in profile.get("features", []) + profile.get("dependencies", []))
    lower = text.lower()
    terms = []
    if "ollama" in lower or "ollama" in " ".join(deps).lower():
        terms.append("Ollama")
    if "stream" in lower:
        terms.append("streaming")
    if "code" in lower:
        terms.append("code assistant")
    if "fastapi" in lower or framework == "FastAPI":
        terms.insert(0, "FastAPI")
    if language and language != "Unknown":
        terms.insert(0, language)
    unique = []
    for q in [
        " ".join(terms),
        "FastAPI Ollama code assistant",
        "FastAPI Ollama streaming",
        "FastAPI external LLM HTTP chat",
        "FastAPI code generation chat",
        "FastAPI chat streaming API",
    ]:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q.lower() not in {x.lower() for x in unique}:
            unique.append(q)
    return {
        "project_summary": profile.get("project", "Unknown"),
        "languages": [language] if language and language != "Unknown" else [],
        "frameworks": [framework] if framework and framework != "Unknown" else [],
        "libraries": deps,
        "technologies": [],
        "project_concepts": [],
        "implemented_capabilities": profile.get("features", [])[:10],
        "github_queries": unique[:8],
    }


def github_discover(
    files: dict[str, str],
    facts: dict,
    profile: dict,
    ollama_url: str | None = None,
    model: str | None = None,
) -> dict:
    """Generate discovery queries, with a deterministic fallback so a slow
    second Ollama call never prevents the already-completed project analysis."""
    try:
        discovery = ollama(
            github_discovery_prompt(files, facts, profile),
            ollama_url=ollama_url,
            model=model,
            schema=GITHUB_DISCOVERY_SCHEMA,
        )
    except Exception as exc:
        print(f"[asset] GitHub discovery Ollama step skipped: {exc}")
        discovery = fallback_discovery(facts, profile)

    defaults = {
        "project_summary": "Unknown", "languages": [], "frameworks": [],
        "libraries": [], "technologies": [], "project_concepts": [],
        "implemented_capabilities": [], "github_queries": [],
    }
    for key, default in defaults.items():
        if not isinstance(discovery.get(key), type(default)):
            discovery[key] = default

    queries = normalize_discovery_queries(discovery)
    repository_map = {}
    for query in queries:
        try:
            result = github_search({"query": query, "limit": 10})
            for repository in result.get("repositories", []):
                full_name = repository.get("fullName")
                if full_name:
                    repository_map[full_name] = repository
        except ValueError as exc:
            print(f"[asset] GitHub discovery query failed: {query!r}: {exc}")

    repositories = list(repository_map.values())
    return {
        "discovery": discovery,
        "queriesRun": queries,
        "repositories": repositories[:30],
        "repositoryCount": len(repositories),
    }


def github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ASSET-local-analysis"}
    token = GITHUB_TOKEN.strip()
    if token == "YOUR_GITHUB_TOKEN_HERE":
        token = ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_get(path: str, params: dict | None = None) -> tuple[object, dict]:
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    request = urllib.request.Request(f"{GITHUB_API}{path}{query}", headers=github_headers())
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")), dict(response.headers)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404, 422, 429}:
            raise ValueError(f"GitHub API returned HTTP {exc.code}; check repository access or rate limits.") from exc
        raise


def github_search(payload: dict) -> dict:
    query_parts = [str(payload.get("query", "")).strip()]
    if payload.get("language"):
        query_parts.append(f"language:{payload['language']}")
    if payload.get("framework"):
        query_parts.append(str(payload["framework"]))
    limit = max(1, min(int(payload.get("limit", 10)), 20))
    data, headers = github_get("/search/repositories", {"q": " ".join(part for part in query_parts if part), "per_page": limit, "sort": "updated"})
    repositories = []
    for item in data.get("items", []):
        repositories.append({"fullName": item.get("full_name"), "name": item.get("name"), "owner": item.get("owner", {}).get("login"), "description": item.get("description"), "url": item.get("html_url"), "defaultBranch": item.get("default_branch"), "language": item.get("language"), "stars": item.get("stargazers_count", 0), "forks": item.get("forks_count", 0), "updatedAt": item.get("updated_at"), "license": (item.get("license") or {}).get("spdx_id"), "topics": item.get("topics", [])})
    return {"repositories": repositories, "authenticated": bool(GITHUB_TOKEN.strip() and GITHUB_TOKEN.strip() != "YOUR_GITHUB_TOKEN_HERE"), "rateLimit": headers.get("x-ratelimit-remaining")}


def relevant_path(path: str) -> bool:
    parts = set(Path(path).parts)
    name = Path(path).name.lower()
    return not parts.intersection(IGNORED_PARTS) and (Path(path).suffix.lower() in TEXT_SUFFIXES or name in {"package.json", "package-lock.json", "requirements.txt", "pyproject.toml", "readme.md"})


def github_repository(payload: dict) -> dict:
    full_name = str(payload.get("fullName", "")).strip()
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", full_name):
        raise ValueError("fullName must be in owner/repository format.")
    metadata, _ = github_get(f"/repos/{full_name}")
    branch = payload.get("ref") or metadata.get("default_branch")
    tree, _ = github_get(f"/repos/{full_name}/git/trees/{urllib.parse.quote(branch, safe='')}", {"recursive": "1"})
    files = [item["path"] for item in tree.get("tree", []) if item.get("type") == "blob" and relevant_path(item["path"]) and item.get("size", 0) <= MAX_GITHUB_FILE_BYTES]
    contents = {}
    for path in files[:80]:
        cache_key = CACHE_ROOT / full_name.replace("/", "__") / branch / path
        cache_key.parent.mkdir(parents=True, exist_ok=True)
        try:
            if cache_key.exists():
                contents[path] = cache_key.read_text(encoding="utf-8")
                continue
            data, _ = github_get(f"/repos/{full_name}/contents/{urllib.parse.quote(path, safe='/')}", {"ref": branch})
            text = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
            cache_key.write_text(text, encoding="utf-8")
            contents[path] = text
        except ValueError:
            continue
    return {"repository": {"fullName": full_name, "url": metadata.get("html_url"), "defaultBranch": branch, "language": metadata.get("language"), "license": (metadata.get("license") or {}).get("spdx_id"), "stars": metadata.get("stargazers_count", 0), "forks": metadata.get("forks_count", 0), "topics": metadata.get("topics", [])}, "files": contents, "fileCount": len(files), "retrievedCount": len(contents), "evidenceBase": f"https://github.com/{full_name}/blob/{branch}/"}


def normalize_source(content: str) -> str:
    value = content.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"//.*?$", "", value, flags=re.MULTILINE)
    value = re.sub(r"#.*?$", "", value, flags=re.MULTILINE)
    value = re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL)
    value = re.sub(r"\s+", " ", value).strip()
    return value.lower()


def deterministic_matches(
    local_files: dict[str, str],
    references: list[dict],
) -> tuple[list[dict], list[dict]]:
    exact_matches = []
    structural_matches = []

    local_norm = {
        path: normalize_source(content)
        for path, content in local_files.items()
        if content.strip()
    }

    for ref in references:
        repo_name = (ref.get("repository") or {}).get(
            "fullName", "Unknown repository"
        )

        for local_path, local_text in local_norm.items():
            if not local_text:
                continue

            for ref_path, ref_content in (ref.get("files") or {}).items():
                reference_text = normalize_source(str(ref_content))
                if not reference_text:
                    continue

                if local_text == reference_text:
                    exact_matches.append({
                        "localFile": local_path,
                        "referenceRepository": repo_name,
                        "referenceFile": ref_path,
                        "similarity": 1.0,
                        "type": "exact-normalized",
                        "evidence": (
                            "The two files are identical after conservative "
                            "line-ending/comment/whitespace normalization."
                        ),
                    })
                    continue

                if Path(local_path).suffix.lower() != Path(ref_path).suffix.lower():
                    continue

                if len(local_text) < 80 or len(reference_text) < 80:
                    continue

                similarity = difflib.SequenceMatcher(
                    None,
                    local_text,
                    reference_text,
                    autojunk=False,
                ).ratio()

                if similarity >= 0.78:
                    structural_matches.append({
                        "localFile": local_path,
                        "referenceRepository": repo_name,
                        "referenceFile": ref_path,
                        "similarity": round(similarity, 4),
                        "type": "structural",
                        "evidence": (
                            "High normalized source similarity was computed "
                            "deterministically."
                        ),
                    })

    return exact_matches, structural_matches


def comparison_prompt(
    local_files: dict[str, str],
    references: list[dict],
    local_facts: dict,
    reference_facts: list[dict],
) -> str:
    local_source = "".join(
        f"\\n--- LOCAL FILE: {path} ---\\n{content[:50000]}"
        for path, content in local_files.items()
    )

    reference_source = "".join(
        f"\\n--- REFERENCE FILE: "
        f"{ref.get('repository', {}).get('fullName', 'Unknown')}/{path} ---\\n"
        f"{str(content)[:50000]}"
        for ref in references
        for path, content in (ref.get("files") or {}).items()
    )

    return f"""
You are the ASSET code-comparison engine.

Compare ACTUAL LOCAL SOURCE with ACTUAL RETRIEVED GITHUB SOURCE.

STRICT OUTPUT RULES:
- Return ONLY one valid JSON object.
- No markdown.
- No code fences.
- No explanation before or after JSON.
- Never invent source files, code, APIs, dependencies, or behavior.
- Use source-file paths as evidence.
- Do not use stars, forks, repository popularity, or descriptions as evidence of code quality.
- Do not reproduce long copyrighted source excerpts.
- Similarity numbers are evidence, not proof of copying.

RETURN EXACTLY:
{{
  "overview": "string",
  "matches": [
    {{
      "localFile": "string",
      "referenceRepository": "owner/repository",
      "referenceFile": "string",
      "similarity": 0.0,
      "type": "exact|structural|semantic",
      "evidence": "string"
    }}
  ],
  "architecture": "string",
  "features": ["string"],
  "codeQuality": ["string"],
  "security": ["string"],
  "performance": ["string"],
  "dependencies": ["string"],
  "recommendations": ["string"],
  "criteria": ["string"],
  "evidence": ["string"],
  "unknowns": ["string"]
}}

LOCAL INVENTORY:
{json.dumps(local_facts, ensure_ascii=False)}

REFERENCE INVENTORIES:
{json.dumps(reference_facts, ensure_ascii=False)}

LOCAL SOURCE:
{local_source}

RETRIEVED GITHUB SOURCE:
{reference_source}
""".strip()


def compare_projects(
    local_files: dict[str, str],
    references: list[dict],
    ollama_url: str | None = None,
    model: str | None = None,
) -> dict:
    local_facts = inventory(local_files)

    reference_facts = [
        {
            "repository": ref.get("repository"),
            "inventory": inventory(ref.get("files", {})),
        }
        for ref in references
    ]

    exact_matches, structural_matches = deterministic_matches(
        local_files,
        references,
    )

    report = ollama(
        comparison_prompt(
            local_files,
            references,
            local_facts,
            reference_facts,
        ),
        ollama_url=ollama_url,
        model=model,
        schema=COMPARISON_SCHEMA,
    )
    report = validate_comparison_result(report)

    # Deterministic source matching is authoritative.
    # Qwen may explain evidence, but it must never create unverified matches.
    report["matches"] = [
        *exact_matches,
        *structural_matches,
    ]

    report["criteria"] = [
        "Actual retrieved source was used for comparison.",
        "Exact normalized matches were computed deterministically.",
        "Structural similarity was computed before Qwen's explanation.",
        "Repository popularity metadata was not treated as code-quality evidence.",
        *report.get("criteria", []),
    ]

    return {
        "referenceInventories": reference_facts,
        "exactMatches": exact_matches,
        "matches": structural_matches,
        "report": report,
    }

def persist_analysis(result: dict) -> dict:
    analysis_id = result.setdefault("analysisId", new_id())
    result.setdefault("timestamp", iso_now())
    LOG_ROOT.mkdir(exist_ok=True)
    (LOG_ROOT / analysis_id).mkdir(exist_ok=True)
    (LOG_ROOT / analysis_id / "analysis.json").write_bytes(json_bytes(result))
    return result


class AssetHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, status: int, value: object) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/config":
            self.send_json(
                HTTPStatus.OK,
                {"ollamaUrl": OLLAMA_URL, "model": OLLAMA_MODEL},
            )
            return

        if parsed.path == "/api/health":
            self.send_json(
                HTTPStatus.OK,
                {"ok": True, "ollamaUrl": OLLAMA_URL, "model": OLLAMA_MODEL},
            )
            return

        if parsed.path == "/api/github/rate-limit":
            try:
                data, headers = github_get("/rate_limit")
                self.send_json(HTTPStatus.OK, {"authenticated": bool(GITHUB_TOKEN.strip() and GITHUB_TOKEN.strip() != "YOUR_GITHUB_TOKEN_HERE"), "rate": data.get("rate"), "remaining": headers.get("x-ratelimit-remaining")})
            except ValueError as exc:
                self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return
        if parsed.path == "/api/logs":
            logs = []
            LOG_ROOT.mkdir(exist_ok=True)
            for directory in sorted(LOG_ROOT.iterdir(), reverse=True):
                target = directory / "analysis.json"
                if target.exists():
                    try:
                        logs.append(json.loads(target.read_text(encoding="utf-8")))
                    except json.JSONDecodeError:
                        pass
            self.send_json(HTTPStatus.OK, {"analyses": logs})
            return
        if parsed.path.startswith("/api/analysis/") or parsed.path.startswith("/api/logs/"):
            analysis_id = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            target = LOG_ROOT / analysis_id / "analysis.json"
            if not target.exists():
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Analysis not found."})
            else:
                self.send_json(HTTPStatus.OK, json.loads(target.read_text(encoding="utf-8")))
            return
        super().do_GET()

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("Request body is missing or too large.")
            payload = json.loads(self.rfile.read(length))
            route = urllib.parse.urlparse(self.path).path
            if route == "/api/analyze-project":
                files = normalize_files(payload.get("projectFiles"))
                facts = inventory(files)

                profile = ollama(
                    analysis_prompt(files, facts),
                    ollama_url=payload.get("ollamaUrl"),
                    model=payload.get("model"),
                    schema=PROJECT_SCHEMA,
                )
                profile = validate_project_result(profile)

                # ============================================================
                # ASSET PIPELINE
                # 1. Analyze the uploaded project.
                # 2. Use the verified analysis to generate GitHub queries.
                # 3. Search public GitHub automatically.
                #
                # GitHub discovery is intentionally AFTER project analysis.
                # This prevents the search model from guessing the project
                # before ASSET has established what the code actually does.
                # ============================================================
                github_discovery = github_discover(
                    files,
                    facts,
                    profile,
                    ollama_url=payload.get("ollamaUrl"),
                    model=payload.get("model"),
                )

                self.send_json(
                    HTTPStatus.OK,
                    persist_analysis({
                        "type": "project-analysis",
                        "projectFiles": list(files),
                        "inventory": facts,
                        "profile": profile,
                        "githubDiscovery": github_discovery,
                    }),
                )

            elif route == "/api/github/search":
                self.send_json(HTTPStatus.OK, github_search(payload))

            elif route == "/api/github/repository":
                self.send_json(HTTPStatus.OK, github_repository(payload))

            elif route == "/api/github/compare":
                local = normalize_files(payload.get("projectFiles"))
                references = payload.get("references")

                if not isinstance(references, list) or not references:
                    raise ValueError(
                        "Select at least one retrieved GitHub repository."
                    )

                if len(references) > 5:
                    raise ValueError(
                        "Select at most 5 repositories for one comparison."
                    )

                comparison = compare_projects(
                    local,
                    references,
                    ollama_url=payload.get("ollamaUrl"),
                    model=payload.get("model"),
                )

                self.send_json(
                    HTTPStatus.OK,
                    persist_analysis({
                        "type": "comparison",
                        "projectFiles": list(local),
                        "references": [
                            {
                                "repository": ref.get("repository"),
                                "files": list(
                                    (ref.get("files") or {}).keys()
                                ),
                            }
                            for ref in references
                        ],
                        "comparison": comparison,
                    }),
                )

            else:
                self.send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "Endpoint not found."},
                )
        except (ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self.log_error("ASSET request failed: %s", exc)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "ASSET analysis failed safely."})

    def log_message(self, format: str, *args) -> None:
        print(f"[asset] {self.address_string()} - {format % args}")


def main() -> None:
    LOG_ROOT.mkdir(exist_ok=True)
    CACHE_ROOT.mkdir(exist_ok=True)
    port = int(os.environ.get("ASSET_PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), AssetHandler)
    print(f"ASSET server running at http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nASSET server stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()