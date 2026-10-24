#!/usr/bin/env python3
"""router.py — Arch Browser Cost-Escalating Model Router.

Policy: Always use the cheapest available model first.
- Tier 0: Local Jev-Style-Qwen-2B ($0.00, ~150ms on CPU, 1-token decision/classification)
- Tier 1: Free / Micro cloud model (OpenRouter free tier, Gemini Flash, Groq) for primary navigation
- Tier 2: Mid-tier capable fallback (DeepSeek V3, Claude 3.5 Haiku) on action failure
- Tier 3: Frontier / Vision model or owner takeover request on stubborn blockers
"""

import json
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

LEGION_OPTION_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class ModelRouter:
    def __init__(self, env: Optional[Dict[str, str]] = None):
        self.env = env or os.environ
        self.jev_url = self.env.get("JEV_LOCAL_URL", "http://127.0.0.1:12345")
        self.jev_model = self.env.get("JEV_LOCAL_MODEL", "chaoliangUNSW/Jev-Style-Qwen3.5-2B-Decision-GGUF")
        self.openrouter_key = self.env.get("OPENROUTER_API_KEY", "")
        self.gemini_key = self.env.get("GEMINI_API_KEY", "")
        self.groq_key = self.env.get("GROQ_API_KEY", "")

        # Model tiers
        self.tier1_model = self.env.get("ARCH_TIER1_MODEL", "google/gemini-2.0-flash-lite-001")
        self.tier2_model = self.env.get("ARCH_TIER2_MODEL", "deepseek/deepseek-chat")
        self.tier3_model = self.env.get("ARCH_TIER3_MODEL", "anthropic/claude-3.5-sonnet")

    # ------------------------------------------------------------- Tier 0: Jev Local
    def _call_jev_local(self, prompt: str, options_len: int) -> Tuple[str, float, int]:
        started = time.perf_counter()
        payload = {
            "model": self.jev_model,
            "temperature": 0,
            "max_tokens": 1,
            "logprobs": True,
            "top_logprobs": min(options_len, 26),
            "messages": [{"role": "user", "content": prompt}],
        }
        req = urllib.request.Request(
            f"{self.jev_url.rstrip('/')}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        top_logprobs = data["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
        token_scores: Dict[str, float] = {}
        for item in top_logprobs:
            token = str(item.get("token", "")).strip()
            try:
                token_scores[token] = float(item["logprob"])
            except (KeyError, TypeError, ValueError):
                continue

        floor = min(token_scores.values()) - 5.0
        logits = [token_scores.get(LEGION_OPTION_LETTERS[i], floor) for i in range(options_len)]
        peak = max(logits)
        weights = [math.exp(l - peak) for l in logits]
        total = sum(weights) or 1.0
        probs = [w / total for w in weights]
        best_idx = probs.index(max(probs))
        latency = round((time.perf_counter() - started) * 1000)
        return LEGION_OPTION_LETTERS[best_idx], max(probs), latency

    # ------------------------------------------------------------- Cloud Callers
    def _call_openrouter(self, model: str, system: str, user_content: str, max_tokens: int = 600) -> Tuple[str, Dict[str, Any], int]:
        if not self.openrouter_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        started = time.perf_counter()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.openrouter_key}",
                "Content-Type": "application/json",
                "X-Title": "Arch Browser Model Router",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        latency = round((time.perf_counter() - started) * 1000)
        return content, usage, latency

    # ------------------------------------------------------------- High-Level: Classify Choice
    def classify(self, state: Dict[str, Any], question: str, options: List[str]) -> Dict[str, Any]:
        """Evaluate structured choice: Tier 0 (Local Jev) -> Tier 1 (Free Cloud) -> Tier 2."""
        if len(options) < 2 or len(options) > 26:
            raise ValueError("Options must be between 2 and 26 items")

        options_text = "\n".join(f"{LEGION_OPTION_LETTERS[i]}. {opt}" for i, opt in enumerate(options))
        prompt = (
            "You are a decision function. Read the state, then answer the question by choosing exactly one option letter.\n\n"
            f"[State]\n{json.dumps(state, ensure_ascii=False)}\n\n"
            f"[Question]\n{question}\n\n"
            f"[Options]\n{options_text}\n\nAnswer:"
        )

        attempts = []
        # 1. Try Tier 0 Local Jev
        try:
            letter, conf, latency = self._call_jev_local(prompt, len(options))
            idx = LEGION_OPTION_LETTERS.index(letter)
            choice = options[idx]
            attempts.append({"tier": "tier0_local_jev", "model": self.jev_model, "latency_ms": latency, "choice": choice, "confidence": conf, "status": "ok"})
            if conf >= 0.70:
                return {"choice": choice, "confidence": conf, "tier": "tier0_local_jev", "model": self.jev_model, "attempts": attempts}
        except Exception as err:
            attempts.append({"tier": "tier0_local_jev", "model": self.jev_model, "error": str(err), "status": "failed"})

        # 2. Escalate to Tier 1 Cloud
        try:
            sys_msg = "You are an exact decision function. Output a JSON object with {\"choice\": \"<option_text>\", \"confidence\": <float 0-1>}."
            user_msg = f"{prompt}\nOutput JSON only:"
            raw, usage, latency = self._call_openrouter(self.tier1_model, sys_msg, user_msg, max_tokens=150)
            res = json.loads(raw)
            chosen = res.get("choice")
            matched = next((o for o in options if o.lower() == str(chosen).lower() or chosen in options), options[0])
            attempts.append({"tier": "tier1_cheap", "model": self.tier1_model, "latency_ms": latency, "choice": matched, "usage": usage, "status": "ok"})
            return {"choice": matched, "confidence": res.get("confidence", 0.9), "tier": "tier1_cheap", "model": self.tier1_model, "attempts": attempts}
        except Exception as err:
            attempts.append({"tier": "tier1_cheap", "model": self.tier1_model, "error": str(err), "status": "failed"})

        # 3. Escalate to Tier 2 Capable
        sys_msg = "You are an authoritative decision function. Pick exactly one option. Output JSON: {\"choice\": \"<option_text>\", \"confidence\": <float>}."
        user_msg = f"{prompt}\nOutput JSON only:"
        raw, usage, latency = self._call_openrouter(self.tier2_model, sys_msg, user_msg, max_tokens=150)
        res = json.loads(raw)
        chosen = res.get("choice")
        matched = next((o for o in options if o.lower() == str(chosen).lower() or chosen in options), options[0])
        attempts.append({"tier": "tier2_capable", "model": self.tier2_model, "latency_ms": latency, "choice": matched, "usage": usage, "status": "ok"})
        return {"choice": matched, "confidence": res.get("confidence", 0.95), "tier": "tier2_capable", "model": self.tier2_model, "attempts": attempts}

    # ------------------------------------------------------------- Navigation Planning
    def plan_next_action(
        self,
        goal: str,
        url: str,
        title: str,
        dom_elements: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        failure_count: int = 0,
    ) -> Dict[str, Any]:
        """Generate the next browser action with automatic model tier escalation."""
        if failure_count >= 3:
            return {
                "action": "takeover_request",
                "reason": f"Failed {failure_count} consecutive attempts; flagging for human owner takeover.",
                "tier": "tier3_human_takeover",
                "model": None,
            }

        # Select model tier based on failure count
        if failure_count == 0:
            tier_name = "tier1_cheap"
            model = self.tier1_model
        else:
            tier_name = "tier2_capable"
            model = self.tier2_model

        sys_msg = (
            "You are Arch Browser, an autonomous browser navigation agent. "
            "Your goal is to complete the user's objective using DOM elements. "
            "Always output strict JSON with fields:\n"
            "{\n"
            "  \"thought\": \"brief reasoning\",\n"
            "  \"action\": \"click|fill|type|press|navigate|wait|done\",\n"
            "  \"selector\": \"css selector or [data-arch-id] (required for click/fill/type/wait)\",\n"
            "  \"text\": \"text to fill or type\",\n"
            "  \"key\": \"Enter|Tab|Escape\",\n"
            "  \"url\": \"destination url for navigate\",\n"
            "  \"summary\": \"result description if done\"\n"
            "}"
        )

        elements_sample = dom_elements[:100]  # Cap context to prevent overflow
        user_msg = (
            f"Goal: {goal}\n"
            f"Current URL: {url}\n"
            f"Page Title: {title}\n"
            f"Action Failure Count: {failure_count}\n"
            f"History: {json.dumps(history[-5:], ensure_ascii=False)}\n\n"
            f"Interactive DOM Elements:\n{json.dumps(elements_sample, ensure_ascii=False)}\n\n"
            "Provide the next single action in JSON:"
        )

        try:
            raw, usage, latency = self._call_openrouter(model, sys_msg, user_msg, max_tokens=350)
            parsed = json.loads(raw)
            parsed["tier"] = tier_name
            parsed["model"] = model
            parsed["latency_ms"] = latency
            parsed["usage"] = usage
            return parsed
        except Exception as err:
            if failure_count == 0:
                # Immediate escalation to Tier 2 on parse / API failure
                return self.plan_next_action(goal, url, title, dom_elements, history, failure_count=1)
            raise


if __name__ == "__main__":
    router = ModelRouter()
    print("ModelRouter initialized. Jev URL:", router.jev_url)