# The Architect

SavvOps' computer-use R&D line: owner-controlled desktop agents. A federation of small,
reliable tools — the model chooses among them; adapters do the work; verification decides
whether the work actually happened.

**Principle:** route every task from semantic control toward visual control. Reliability
falls as meaning disappears. Pixels are an observation, coordinates are an actuator target —
neither explains what a control means.

## Control surfaces tier list

| Tier | Lane | Rule |
|------|------|------|
| S | Native API / CLI | Use the richest interface the software exposes. Speed, repeatability, auditability. |
| A | DOM / a11y tree | The desktop DOM: UIA (Windows), AX (macOS), AT-SPI (Linux). Select by role/name/state, invoke actions. |
| B | Keyboard nodes | Hotkeys, palettes, Vim/tmux macros — as typed, verified workflow nodes with focus preconditions and postconditions. |
| C | Local vision | Screenshot → OCR → visual grounding. Only after semantic lanes miss. |
| D | Raw pixels | Fixed coordinates. Last resort, narrow stable targets only. |

The same tool can move tiers: a keyboard action with no state check is D-tier; wrapped with
preconditions, focus checks, output capture, and verification it is B-tier approaching A-tier.

## Mental model

- The **accessibility tree is the desktop DOM** — roles, names, values, states, actions, bounds.
- **Tree says what, screenshot shows appearance, grounding links meaning to location, coordinates say where to act.**
- Browser lane: app API → DOM → browser a11y tree → local vision → coordinates.
- Desktop lane: app API / CLI → OS a11y tree → verified keyboard node → local vision → coordinates.

## Pipeline: observe → route → act → verify → recover

1. **Observe** — process list, active window, DOM/a11y tree, screenshot, terminal output
2. **Normalize** — reduce to app, role, name, value, state, bounds, confidence
3. **Route** — choose API/CLI, semantic tree, keyboard node, local vision, or raw input
4. **Plan** — one typed action, one expected state change
5. **Validate** — schema, allowlist, arguments, permissions, risk
6. **Act** — exactly one adapter, node, or actuator
7. **Verify** — re-observe, compare actual vs expected state
8. **Recover** — safe retry, lane switch, state restore, or ask a human
9. **Learn** — record trace, failure cause, reusable workflow candidate

Failure moves down one lane; never an unbounded retry loop. Every action has a before,
an after, and evidence.

## Build order

1. [x] `appctl` — cross-platform open/focus/status/quit lifecycle adapter
2. [x] Tool registry with JSON schemas (CLI-Anything harnesses for trapped functionality)
3. [x] Vim/tmux macro-node runner with focus + state contracts
4. [x] Windows/macOS/Linux accessibility adapters (Windows UIA desktop DOM)
5. [x] Sub-1GB local router benchmark (lane choice, tool selection, argument filling — never raw shell or invented coordinates)
6. [x] Local OCR + visual grounding fallback (offline Windows.Media.Ocr / visual grounding)
7. [x] Ephemeral browser worker & profile-forking adapter (consolidated 1-owner shared browser + N temporary credentialed copies)

## Coverage scorecard (Benchmark Results)

| Metric | Direction | Benchmark Result |
|--------|-----------|------------------|
| Semantic coverage (% actions without pixels) | Up | **75.0%** (15/20 non-pixel) |
| Lane accuracy (% optimal control tier chosen) | Up | **100.0%** |
| Tool selection accuracy | Up | **100.0%** |
| Schema compliance (% valid tool arguments) | Toward 100% | **100.0%** |
| Fallback rate (% escalating to vision) | Down | **20.0%** |
| Raw pixel rate (% fixed coordinates) | Down | **5.0%** |
| Local ratio (% decisions without paid inference) | Up | **100.0%** |
| Decision latency | Low | **0.21 ms** (avg) / **0.96 ms** (max) |
| Runtime memory footprint | Low | **35 MB** (sub-1GB compliant) |

Benchmark: run `appctl benchmark` after every change. Evaluates fixed 20-task suite across S, A, B, C, and D tiers.


## Full reference

The 9-page Computer Use Tier List Cheat Sheet (2026-09-24) covers the tier list,
accessibility trees, CLI-first control, CLI-Anything, keyboard-as-node, the sub-1GB
router, the pipeline, the phased roadmap, and coverage metrics in full.
