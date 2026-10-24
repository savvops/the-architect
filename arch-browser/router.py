#!/usr/bin/env python3
"""router.py — Arch Browser cost-escalating model router.

Policy: cheapest capable option first, escalate only on evidence of failure.
- Tier 0: deterministic rules + local Jev-Style Qwen 2B decider ($0, on-box).
          Rules handle trivially-decidable steps (e.g. goal names a URL -> navigate).
          Jev answers bounded multiple-choice questions via /api/classify.
- Tier 1: cheapest cloud model(s) (ARCH_TIER1_MODELS, comma list tried in order).
- Tier 2: capable fallback (ARCH_TIER2_MODELS) after a Tier 1 error or a failed action.
- Tier 3: owner takeover request after repeated failures.
"""

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

OPTION_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
URL_RE = re.compile(r"\b((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s'\"]*)?)", re.I)

DEFAULT_TIER1 = "gemini-3.5-flash-lite,gemini-3.8-flash,google/gemini-2.5-flash"
DEFAULT_TIER2 = "deepseek/deepseek-chat"


def match_option(chosen: Any, options: List[str]) -> Optional[str]:
    """Map a model's answer onto exactly one option, or None. Never defaults to options[0]."""
    if chosen is None:
        return None
    c = str(chosen).strip()
    if len(c) == 1 and c.upper() in OPTION_LETTERS[: len(options)]:
        return options[OPTION_LETTERS.index(c.upper())]
    m = re.match(r"^([A-Z])[\.\):]\s", c)
    if m and m.group(1) in OPTION_LETTERS[: len(options)]:
        return options[OPTION_LETTERS.index(m.group(1))]
    for o in options:
        if o == c:
            return o
    for o in options:
        if o.lower() == c.lower():
            return o
    return None


def _split(v: str) -> List[str]:
    return [m.strip() for m in v.split(",") if m.strip()]


class ModelRouter:
    def __init__(self, env: Optional[Dict[str, str]] = None):
        self.env = env if env is not None else os.environ
        self.jev_url = self.env.get("JEV_LOCAL_URL", "http://127.0.0.1:12345")
        self.jev_model = self.env.get("JEV_LOCAL_MODEL", "chaoliangUNSW/Jev-Style-Qwen3.5-2B-Decision-GGUF")
        self.jev_threshold = float(self.env.get("ARCH_JEV_CONFIDENCE", "0.70"))
        self.openrouter_key = self.env.get("OPENROUTER_API_KEY", "")
        self.gemini_key = self.env.get("GEMINI_API_KEY") or self.env.get("GOOGLE_API_KEY", "")
        # Back-compat: single-model env vars still work.
        self.tier1_models = _split(self.env.get("ARCH_TIER1_MODELS") or self.env.get("ARCH_TIER1_MODEL") or DEFAULT_TIER1)
        self.tier2_models = _split(self.env.get("ARCH_TIER2_MODELS") or self.env.get("ARCH_TIER2_MODEL") or DEFAULT_TIER2)

    def describe(self) -> Dict[str, Any]:
        return {"tier0": {"rules": True, "jev_url": self.jev_url, "jev_model": self.jev_model},
                "tier1": self.tier1_models, "tier2": self.tier2_models,
                "gemini_key_present": bool(self.gemini_key),
                "openrouter_key_present": bool(self.openrouter_key)}

    # ------------------------------------------------------------- Tier 0: Jev local
    def _call_jev_local(self, prompt: str, n_options: int) -> Tuple[str, float, int]:
        started = time.perf_counter()
        payload = {
            "model": self.jev_model, "temperature": 0, "max_tokens": 1,
            "logprobs": True, "top_logprobs": min(max(n_options, 5), 20),
            "messages": [{"role": "user", "content": prompt}],
        }
        req = urllib.request.Request(f"{self.jev_url.rstrip('/')}/v1/chat/completions",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        return self.score_jev(data, n_options) + (round((time.perf_counter() - started) * 1000),)

    @staticmethod
    def score_jev(data: Dict[str, Any], n_options: int) -> Tuple[str, float]:
        top = data["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
        scores: Dict[str, float] = {}
        for item in top:
            tok = str(item.get("token", "")).strip().upper()
            try:
                lp = float(item["logprob"])
            except (KeyError, TypeError, ValueError):
                continue
            if tok and (tok not in scores or lp > scores[tok]):
                scores[tok] = lp
        if not scores:
            raise ValueError("no logprobs returned")
        floor = min(scores.values()) - 5.0
        logits = [scores.get(OPTION_LETTERS[i], floor) for i in range(n_options)]
        peak = max(logits)
        weights = [math.exp(v - peak) for v in logits]
        total = sum(weights) or 1.0
        probs = [w / total for w in weights]
        best = probs.index(max(probs))
        return OPTION_LETTERS[best], probs[best]

    # ------------------------------------------------------------- Cloud
    def _call_gemini_direct(self, model: str, system: str, user_content: str, max_tokens: int = 600) -> Tuple[str, Dict[str, Any], int]:
        if not self.gemini_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        started = time.perf_counter()
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.gemini_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(f"HTTP {e.code} from Google Gemini direct ({model}): {detail}") from None
        content = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {})
        usage["cost"] = 0.0  # Google AI Studio free tier
        usage["provider"] = "google_direct"
        return content, usage, round((time.perf_counter() - started) * 1000)

    def _call_openrouter(self, model: str, system: str, user_content: str, max_tokens: int = 600) -> Tuple[str, Dict[str, Any], int]:
        if not self.openrouter_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        started = time.perf_counter()
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
            "temperature": 0.1, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "usage": {"include": True},
        }
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.openrouter_key}", "Content-Type": "application/json",
                     "X-Title": "Arch Browser"},
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(f"HTTP {e.code} from OpenRouter ({model}): {detail}") from None
        content = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {})
        usage["provider"] = "openrouter"
        return content, usage, round((time.perf_counter() - started) * 1000)

    def _call_cloud(self, model: str, system: str, user_content: str, max_tokens: int = 600) -> Tuple[str, Dict[str, Any], int]:
        clean_model = model.replace("google/", "") if model.startswith("google/") else model
        if ("gemini" in clean_model.lower()) and self.gemini_key:
            return self._call_gemini_direct(clean_model, system, user_content, max_tokens)
        return self._call_openrouter(model, system, user_content, max_tokens)

    @staticmethod
    def parse_json(raw: str) -> Dict[str, Any]:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        try:
            out = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                raise
            out = json.loads(m.group(0))
        if not isinstance(out, dict):
            raise ValueError("model did not return a JSON object")
        return out

    def _cloud_chain(self, tiers: List[Tuple[str, List[str]]], system: str, user: str, max_tokens: int,
                     attempts: List[Dict[str, Any]], validate) -> Optional[Dict[str, Any]]:
        for tier_name, models in tiers:
            for model in models:
                try:
                    raw, usage, latency = self._call_cloud(model, system, user, max_tokens)
                    parsed = self.parse_json(raw)
                    ok, why = validate(parsed)
                    attempts.append({"tier": tier_name, "model": model, "provider": usage.get("provider"),
                                     "latency_ms": latency, "cost": usage.get("cost"),
                                     "status": "ok" if ok else f"rejected: {why}"})
                    if ok:
                        parsed.update({"tier": tier_name, "model": model, "latency_ms": latency, "usage": usage})
                        return parsed
                except Exception as err:  # noqa: BLE001
                    attempts.append({"tier": tier_name, "model": model, "error": str(err)[:200], "status": "failed"})
        return None

    # ------------------------------------------------------------- Classify
    def classify(self, state: Dict[str, Any], question: str, options: List[str]) -> Dict[str, Any]:
        if not isinstance(options, list) or not 2 <= len(options) <= 26:
            raise ValueError("options must be a list of 2-26 items")
        options = [str(o) for o in options]
        opts = "\n".join(f"{OPTION_LETTERS[i]}. {o}" for i, o in enumerate(options))
        prompt = ("You are a decision function. Read the state, then answer the question by choosing exactly one option letter.\n\n"
                  f"[State]\n{json.dumps(state, ensure_ascii=False)}\n\n[Question]\n{question}\n\n[Options]\n{opts}\n\nAnswer:")
        attempts: List[Dict[str, Any]] = []
        jev_choice = None
        try:
            letter, conf, latency = self._call_jev_local(prompt, len(options))
            jev_choice = options[OPTION_LETTERS.index(letter)]
            attempts.append({"tier": "tier0_local_jev", "model": self.jev_model, "latency_ms": latency,
                             "choice": jev_choice, "confidence": round(conf, 3), "status": "ok"})
            if conf >= self.jev_threshold:
                return {"choice": jev_choice, "confidence": round(conf, 3), "tier": "tier0_local_jev",
                        "model": self.jev_model, "attempts": attempts}
        except Exception as err:  # noqa: BLE001
            attempts.append({"tier": "tier0_local_jev", "model": self.jev_model, "error": str(err)[:200], "status": "failed"})

        system = ('You are an exact decision function. Reply with JSON {"choice": "<option letter>", "confidence": <0-1>}.')

        def valid(p):
            return (match_option(p.get("choice"), options) is not None), "choice not one of the options"

        res = self._cloud_chain([("tier1_cheap", self.tier1_models), ("tier2_capable", self.tier2_models)],
                                system, prompt + "\nJSON only:", 60, attempts, valid)
        if res:
            return {"choice": match_option(res["choice"], options), "confidence": res.get("confidence"),
                    "tier": res["tier"], "model": res["model"], "attempts": attempts}
        if jev_choice is not None:
            return {"choice": jev_choice, "confidence": attempts[0].get("confidence"), "tier": "tier0_local_jev_lowconf",
                    "model": self.jev_model, "attempts": attempts}
        raise RuntimeError("all tiers failed: " + json.dumps(attempts)[:400])

    # ------------------------------------------------------------- Tier 0 rules for navigation
    @staticmethod
    def rule_action(goal: str, url: str, history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Deterministic first step: if the goal names a site we are not on yet, go there. No LLM."""
        if history:
            return None
        m = URL_RE.search(goal)
        if not m:
            return None
        target = m.group(1)
        host = re.sub(r"^https?://", "", target).split("/")[0].lower()
        if host and host in (url or "").lower():
            return None
        if not target.lower().startswith("http"):
            target = "https://" + target
        return {"action": "navigate", "url": target, "thought": f"goal names {host}", "tier": "tier0_rules", "model": None}

    def plan_next_action(self, goal: str, url: str, title: str, dom_elements: List[Dict[str, Any]],
                         history: List[Dict[str, Any]], failure_count: int = 0) -> Dict[str, Any]:
        if failure_count >= 3:
            return {"action": "takeover_request", "tier": "tier3_owner", "model": None,
                    "reason": f"{failure_count} consecutive failed actions; requesting owner takeover."}
        rule = self.rule_action(goal, url, history)
        if rule:
            return rule

        system = (
            "You are Arch Browser, an autonomous browser agent. Choose ONE next action toward the goal.\n"
            "Target elements by their numeric `index` from the element list. Never invent coordinates.\n"
            'Reply with strict JSON: {"thought": str, "action": "click|fill|type|press|navigate|scroll|back|wait|done", '
            '"index": int (click/fill/type), "text": str (fill/type), "key": "Enter|Tab|Escape|ArrowDown|PageDown", '
            '"url": str (navigate), "direction": "down|up" (scroll), "summary": str (done: the answer/result)}'
        )
        compact = [{k: e.get(k) for k in ("index", "role", "name", "href", "value", "checked", "disabled", "in_viewport") if e.get(k) not in (None, "", False)}
                   for e in dom_elements[:150]]
        recent = [{"step": h.get("step"), "action": (h.get("plan") or {}).get("action"), "index": (h.get("plan") or {}).get("index"),
                   "ok": (h.get("result") or {}).get("ok"), "error": (h.get("result") or {}).get("error")} for h in history[-6:]]
        user = (f"Goal: {goal}\nURL: {url}\nTitle: {title}\nConsecutive failures: {failure_count}\n"
                f"Recent steps: {json.dumps(recent, ensure_ascii=False)}\n"
                f"Elements: {json.dumps(compact, ensure_ascii=False)}\nNext action JSON:")

        valid_actions = {"click", "fill", "type", "press", "navigate", "scroll", "back", "wait", "done"}
        indices = {e.get("index") for e in dom_elements}

        def valid(p):
            a = p.get("action")
            if a not in valid_actions:
                return False, f"bad action {a!r}"
            if a in ("click", "fill") and p.get("index") not in indices and not p.get("selector"):
                return False, "index not in element list"
            if a == "navigate" and not p.get("url"):
                return False, "navigate without url"
            return True, ""

        tiers = [("tier1_cheap", self.tier1_models), ("tier2_capable", self.tier2_models)]
        if failure_count >= 1:
            tiers = [("tier2_capable", self.tier2_models)]  # escalate after a failed action
        attempts: List[Dict[str, Any]] = []
        res = self._cloud_chain(tiers, system, user, 400, attempts, valid)
        if res is None:
            return {"action": "takeover_request", "tier": "tier3_owner", "model": None,
                    "reason": "no model produced a valid action", "attempts": attempts}
        res["attempts"] = attempts
        return res


if __name__ == "__main__":
    print(json.dumps(ModelRouter().describe(), indent=2))
