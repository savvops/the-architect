# appctl — the hands layer (The Architect v0.6)

`appctl` is The Architect's **lifecycle, actuation & accessibility adapter**: one typed command vocabulary for
opening, focusing, observing, querying desktop DOM, executing verified macro-node contracts, and interacting with
applications on any OS. The agent calls `appctl`; it never invents raw shell text or guesses coordinates.

Zero external pip dependencies (pure Python standard library only).

## Command Overview

### Core Lifecycle (Phase 1 & 2)
```bash
appctl open <app> [--args ...]               # Start an application
appctl focus <app>                           # Bring its window to foreground
appctl status <app>                          # Check status (PID, window title)
appctl list                                  # List running GUI processes
appctl quit <app> [--force]                  # Graceful close or force-kill
```

### Observation & Actuation (Tier C & D)
```bash
appctl see <app> [--output file.png]         # Window snapshot to PNG
appctl diff <before.png> <after.png>         # Pixel difference verification
appctl type <app> <text>                     # Type text into focused element
appctl key <app> <key_combo>                 # Dispatch keystroke (e.g. ctrl+s, escape)
appctl click <app> <x> <y> [--button ...]    # Actuate window-relative click
```

### Tool Registry & JSON Schema Engine (Phase 3 & 4)
```bash
appctl tools [app]                           # List registered tools and schemas
appctl schema <tool_id>                      # Display JSON schema for a tool
appctl exec <tool_id> [--args-json ...]      # Validate arguments & execute tool
```

### Tier B: Macro-Node Runner (Phase 5)
```bash
appctl node <spec_json_or_file> [--params-json ...]
```
Executes typed workflow contracts with:
- **Preconditions**: target window exists, `focused` check, `title_match` regex, baseline `see` snapshot.
- **Typed Steps**: sequence of `key`, `type` (with `{param}` templates), `click`, and `sleep`.
- **Postconditions**: `focused` assertion, `title_match` regex assertion, visual pixel `diff` range verification (`assert_changed`, `assert_unchanged`).
- **Recovery**: automated fallback sequence if preconditions, steps, or postconditions fail.
- **Verifiable Evidence**: typed JSON payload with before/after state verification.

### Tier A: Accessibility Adapter / Desktop DOM (Phase 6)
```bash
appctl tree <app> [--depth N] [--max-children N]
appctl query <app> [--role <role>] [--name <pattern>] [--id <id>]
appctl a11y-click <app> [--role <role>] [--name <pattern>] [--id <id>] [--button <left|right|double>]
```
Desktop DOM / semantic tree control:
- **`tree`**: Inspects native accessibility tree (roles, names, IDs, bounding boxes).
- **`query`**: Searches semantic elements matching criteria without hardcoded coordinates.
- **`a11y-click`**: Semantically locates the element, calculates its exact center relative to the window, focuses, and clicks without coordinate guesswork.

### Tier C: Local Vision & OCR Grounding (Phase 7)
```bash
appctl ocr <app_or_image>
appctl vision-find <app_or_image> <text_query>
appctl vision-click <app> <text_query> [--button <left|right|double>]
```
Offline local visual grounding (native Windows.Media.Ocr):
- **`ocr`**: Extracts recognized text, lines, and word bounding boxes `[x, y, w, h]` with zero pip dependencies.
- **`vision-find`**: Grounds query text visually to relative coordinates and confidence score.
- **`vision-click`**: Captures snapshot, visually grounds target text, and clicks it when semantic a11y tree is unavailable.

### Sub-1GB Local Decision Router & Benchmark (Build Order 5)
```bash
appctl route "<task_description>" [--context-json ...]
appctl benchmark [--json]
```
Local System 1 routing engine (<35MB RAM, <1ms latency):
- **`route`**: Maps natural language task intent to optimal control tier (S -> A -> B -> C -> D), registered tool, and validated arguments.
- **`benchmark`**: Evaluates standardized 20-task suite, generating coverage scorecard metrics.

### Ephemeral Browser Workers & Profile-Forking Adapter (Consolidated Browser Architecture)
```bash
appctl browser fork [--browser BRAVE|CHROME|FIREFOX] [--url URL] [--headless] [--cookie-file FILE]
appctl browser list
appctl browser destroy <worker_id|all>
appctl browser status
appctl browser bridge [--url URL] [--cookie-file FILE] [--headless] [--no-ephemeral]
```
Enables 1 shared canonical browser for the human owner while allowing AI agents to spawn and destroy N isolated, credentialed temporary browser copies on demand:
- **`fork`**: Clones non-locked profile items (`Local State`, `Preferences`, `Bookmarks`, `Login Data`), assigns an isolated `%TEMP%\architect_browser\<worker_id>` directory, launches detached browser with auto-allocated `--remote-debugging-port`, and dynamically injects pre-authenticated cookies via pure stdlib CDP WebSocket (`Network.setCookies`).
- **`list`**: Inspects running workers, verifies PID liveness, and auto-prunes dead processes.
- **`destroy`**: Terminates the worker process tree (`taskkill /T /F`) and purges the temporary user-data directory from disk with zero residual trace.
- **`status`**: Queries local browser readiness (Brave, Chrome, Chromium, Firefox, Edge) and checks connectivity to remote SAO browser bridges (`savv-spine:6092`).
- **`bridge`**: Connects remote SAO Browser (`savv-spine:6092`) with Architect's ephemeral worker infrastructure. Automatically inspects owner state (`paused: true` / "Take over") and queue load, dynamically offloading AI browser requests into isolated background workers so the human owner's viewport is never hijacked or disrupted.

## Verifier Contract

Every command exits with `0` (ok) or `1` (failure) and prints typed JSON on stdout conforming to The Architect verifier contract:

```json
{
  "ok": true,
  "action": "a11y-click",
  "app": "firefox",
  "evidence": {
    "pid": 26260,
    "window": "GitHub - savvops/the-architect ...",
    "matched_element": {
      "name": "Sidebars",
      "role": "button",
      "id": "sidebar-button",
      "bounds": [13, 59, 53, 50],
      "center": [39, 84],
      "relative_center": [27, 80]
    },
    "relative_coordinates": {"x": 27, "y": 80},
    "button": "left",
    "clicked": true,
    "total_matches": 1
  }
}
```
