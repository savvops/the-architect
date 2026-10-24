"""Unit tests for Arch Browser (stdlib unittest; no browser or network needed).

Run:  python3 -m unittest discover -s tests -v      (from arch-browser/)
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.pop("ARCH_TOKEN", None)

import archd  # noqa: E402
from router import ModelRouter, match_option  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SelectorTests(unittest.TestCase):
    def test_index_forms(self):
        for s in ("7", 7, "#7", "arch-7", "@arch-7"):
            self.assertEqual(archd.normalize_selector(s), '[data-arch-id="arch-7"]', s)

    def test_css_passthrough(self):
        self.assertEqual(archd.normalize_selector("a.storylink"), "a.storylink")
        self.assertEqual(archd.normalize_selector("arch-browser-x"), "arch-browser-x")
        self.assertEqual(archd.normalize_selector(None), "")


class AuthTests(unittest.TestCase):
    def test_disabled_when_no_token(self):
        self.assertTrue(archd.token_ok("", None))

    def test_requires_exact_bearer(self):
        self.assertTrue(archd.token_ok("s3cret", "Bearer s3cret"))
        self.assertFalse(archd.token_ok("s3cret", None))
        self.assertFalse(archd.token_ok("s3cret", "Bearer nope"))
        self.assertFalse(archd.token_ok("s3cret", "s3cret"))
        self.assertFalse(archd.token_ok("s3cret", "Bearer s3cre"))


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.b = archd.Broker(clock=self.clock)

    def test_free_when_no_lease(self):
        self.assertIsNone(self.b.check(None))

    def test_takeover_blocks_agents_not_owner(self):
        self.b.takeover()
        self.assertEqual(self.b.check(None)["code"], 423)
        self.assertIsNone(self.b.check(None, actor="owner"))
        self.b.release_takeover()
        self.assertIsNone(self.b.check(None))

    def test_lease_exclusive(self):
        a = self.b.acquire("agent-a", 60)
        self.assertTrue(a["ok"])
        self.assertFalse(self.b.acquire("agent-b", 60)["ok"])
        self.assertIsNone(self.b.check(a["lease_id"]))
        self.assertEqual(self.b.check(None)["code"], 409)
        self.assertEqual(self.b.check("wrong")["code"], 409)

    def test_lease_renew_same_holder(self):
        a = self.b.acquire("agent-a", 60)
        again = self.b.acquire("agent-a", 60)
        self.assertEqual(a["lease_id"], again["lease_id"])

    def test_lease_expiry(self):
        self.b.acquire("agent-a", 10)
        self.clock.t += 11
        self.assertIsNone(self.b.check(None))
        self.assertTrue(self.b.acquire("agent-b", 10)["ok"])

    def test_takeover_beats_lease(self):
        a = self.b.acquire("agent-a", 60)
        self.b.takeover()
        self.assertEqual(self.b.check(a["lease_id"])["code"], 423)
        self.assertFalse(self.b.acquire("agent-c", 60)["ok"])

    def test_release(self):
        a = self.b.acquire("agent-a", 60)
        self.assertFalse(self.b.release("bogus")["ok"])
        self.assertTrue(self.b.release(a["lease_id"])["ok"])
        self.assertIsNone(self.b.lease)


class MatchOptionTests(unittest.TestCase):
    OPTS = ["new", "past", "comments", "ask"]

    def test_exact_text_not_first_option(self):
        # Regression: old code returned options[0] whenever the answer was any valid option.
        self.assertEqual(match_option("past", self.OPTS), "past")
        self.assertEqual(match_option("ASK", self.OPTS), "ask")

    def test_letters(self):
        self.assertEqual(match_option("B", self.OPTS), "past")
        self.assertEqual(match_option("c", self.OPTS), "comments")
        self.assertEqual(match_option("D. ask", self.OPTS), "ask")

    def test_no_silent_default(self):
        self.assertIsNone(match_option("jobs", self.OPTS))
        self.assertIsNone(match_option(None, self.OPTS))
        self.assertIsNone(match_option("Z", self.OPTS))


class RouterTests(unittest.TestCase):
    def make(self, responses, env=None):
        r = ModelRouter(env=dict({"OPENROUTER_API_KEY": "test", "ARCH_TIER1_MODELS": "cheap-a,cheap-b",
                                  "ARCH_TIER2_MODELS": "capable", "JEV_LOCAL_URL": "http://127.0.0.1:1"}, **(env or {})))
        calls = []

        def fake(model, system, user, max_tokens=600):
            calls.append(model)
            resp = responses.get(model)
            if isinstance(resp, Exception):
                raise resp
            return json.dumps(resp), {"cost": 0.0001}, 5

        r._call_openrouter = fake
        r._call_jev_local = lambda prompt, n: (_ for _ in ()).throw(ConnectionError("jev down"))
        return r, calls

    def test_score_jev(self):
        data = {"choices": [{"logprobs": {"content": [{"top_logprobs": [
            {"token": "B", "logprob": -0.1}, {"token": " A", "logprob": -2.5}, {"token": "x", "logprob": -4}]}]}}]}
        letter, conf = ModelRouter.score_jev(data, 3)
        self.assertEqual(letter, "B")
        self.assertGreater(conf, 0.8)

    def test_classify_tier1_fallthrough_to_second_cheap_model(self):
        r, calls = self.make({"cheap-a": RuntimeError("HTTP 404"), "cheap-b": {"choice": "B"}})
        out = r.classify({}, "q", ["new", "past"])
        self.assertEqual(out["choice"], "past")
        self.assertEqual(out["model"], "cheap-b")
        self.assertEqual(calls, ["cheap-a", "cheap-b"])

    def test_classify_rejects_invalid_then_escalates(self):
        r, calls = self.make({"cheap-a": {"choice": "jobs"}, "cheap-b": {"choice": "nope"}, "capable": {"choice": "A"}})
        out = r.classify({}, "q", ["new", "past"])
        self.assertEqual((out["choice"], out["tier"]), ("new", "tier2_capable"))

    def test_rule_navigates_without_llm(self):
        r, calls = self.make({})
        plan = r.plan_next_action("Find the top post on news.ycombinator.com", "about:blank", "", [], [])
        self.assertEqual(plan["action"], "navigate")
        self.assertEqual(plan["url"], "https://news.ycombinator.com")
        self.assertEqual(plan["tier"], "tier0_rules")
        self.assertEqual(calls, [])

    def test_rule_skips_when_already_there(self):
        r, calls = self.make({"cheap-a": {"action": "done", "summary": "x"}})
        plan = r.plan_next_action("top post on news.ycombinator.com", "https://news.ycombinator.com/", "", [], [])
        self.assertEqual(plan["action"], "done")
        self.assertEqual(calls, ["cheap-a"])

    def test_plan_rejects_hallucinated_index(self):
        els = [{"index": 1, "role": "a", "name": "new"}]
        r, calls = self.make({"cheap-a": {"action": "click", "index": 99}, "cheap-b": {"action": "click", "index": 1}})
        plan = r.plan_next_action("open new", "https://x.test", "", els, [])
        self.assertEqual(plan["index"], 1)
        self.assertEqual(plan["model"], "cheap-b")

    def test_failure_escalates_to_tier2(self):
        r, calls = self.make({"capable": {"action": "scroll", "direction": "down"}})
        plan = r.plan_next_action("g", "https://x.test", "", [], [{"step": 1}], failure_count=1)
        self.assertEqual(plan["tier"], "tier2_capable")
        self.assertEqual(calls, ["capable"])

    def test_three_failures_request_owner(self):
        r, _ = self.make({})
        self.assertEqual(r.plan_next_action("g", "u", "", [], [{}], failure_count=3)["action"], "takeover_request")

    def test_all_models_fail_requests_owner(self):
        r, _ = self.make({"cheap-a": RuntimeError("x"), "cheap-b": RuntimeError("y"), "capable": RuntimeError("z")})
        self.assertEqual(r.plan_next_action("g", "https://x.test", "", [], [])["action"], "takeover_request")

    def test_parse_json_fenced(self):
        self.assertEqual(ModelRouter.parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(ModelRouter.parse_json('Sure: {"a": 2} done'), {"a": 2})

    def test_gemini_direct_routing(self):
        r = ModelRouter(env={"GEMINI_API_KEY": "test-key", "ARCH_TIER1_MODELS": "gemini-3.5-flash-lite"})
        called = []
        def fake_gemini(model, system, user, max_tokens=600):
            called.append(model)
            return '{"action": "done", "summary": "ok"}', {"provider": "google_direct", "cost": 0.0}, 10
        r._call_gemini_direct = fake_gemini
        plan = r.plan_next_action("do task", "https://x.test", "", [], [{"step": 1}])
        self.assertEqual(plan["action"], "done")
        self.assertEqual(called, ["gemini-3.5-flash-lite"])
        self.assertEqual(plan["usage"]["provider"], "google_direct")
        self.assertEqual(plan["usage"]["cost"], 0.0)


if __name__ == "__main__":
    unittest.main()
