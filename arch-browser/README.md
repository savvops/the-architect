# Arch Browser (`arch-browser`)

The Architect's S-Tier shared browser for humans and autonomous agents:
- **Headed Chromium** running in virtual `Xvnc :99` with persistent profile `/data/profile`
- **Live Human Desktop via noVNC** on port `6080`
- **Owner Takeover Broker**: Top-bar **Take Over** button locks out agents; **Allow Agents** resumes
- **S-Tier REST API & CLI** on port `8901`: semantic DOM (`/api/dom`), accessibility tree (`/api/a11y`), and tab control
- **Cost-Escalating Model Router**:
  - **Tier 0**: Local `Jev-Style-Qwen3.5-2B` ($0.00, ~150ms on CPU, 1-token structured decision)
  - **Tier 1**: Free/Cheap cloud model (Gemini Flash, Groq, OpenRouter free/micro) for primary DOM navigation
  - **Tier 2**: Capable escalation (DeepSeek V3, Claude Haiku 3.5) if an action fails to advance page state
  - **Tier 3**: Vision / Frontier model or flags owner takeover request

## Quick Start

```bash
cp .env.example .env
docker compose up -d --build
```

- Web Desktop + Takeover Bar: `http://localhost:6080` (or over Tailscale)
- Agent API & Mobile UI: `http://localhost:8901`

## Agent CLI

```bash
python3 bin/arch status
python3 bin/arch goto https://example.com
python3 bin/arch dom
python3 bin/arch click "button#submit"
python3 bin/arch task "Find the latest release notes and summarize them"
```