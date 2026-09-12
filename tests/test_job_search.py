import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.job_search_notify import format_shortlist, send_shortlist
from scripts.job_search_runner import (FRESH_RESULTS, NO_MATCHES, SOURCE_TEMPORARILY_UNAVAILABLE,
                                        SourceUnavailable, discover, match_and_sort, parse_jobs)


PROFILE = {"target_roles": ["Accountant"], "skills": ["Excel", "Oracle"], "locations": ["Qatar"], "preferred_keywords": ["finance"]}
RULES = {"shortlist_threshold": 70, "strong_match_threshold": 85, "role_points": 40, "skill_points": 30, "location_points": 20, "keyword_points": 10}


class JobSearchTests(unittest.TestCase):
    def test_successful_job_discovery_parsing(self):
        jobs = parse_jobs(json.dumps({"data": [{"title": "Accountant", "company_name": "Acme", "location": "Doha, Qatar", "url": "https://example.test/1", "description": "Excel Oracle finance"}]}), "fixture")
        self.assertEqual(jobs[0]["company"], "Acme")
        self.assertEqual(jobs[0]["location"], "Doha, Qatar")

    def test_failed_source_fallback_preserves_last_successful_results(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "results.json"
            good = {"status": FRESH_RESULTS, "result_origin": "FRESH", "jobs": [{"title": "Saved", "score": 90, "url": "https://example.test/saved"}]}
            state.write_text(json.dumps(good), encoding="utf-8")
            def fail(*args):
                raise SourceUnavailable("HTTP 429")
            result = discover({"sources": [{"name": "fixture", "url": "https://example.test"}]}, RULES, state, fail)
            self.assertEqual(result["status"], SOURCE_TEMPORARILY_UNAVAILABLE)
            self.assertEqual(result["result_origin"], "FALLBACK")
            self.assertEqual(result["jobs"], good["jobs"])

    def test_no_matches_is_distinct_from_source_failure(self):
        def empty(*args):
            return "application/json", b'{"data": []}'
        with tempfile.TemporaryDirectory() as directory:
            result = discover({"sources": [{"name": "fixture", "url": "https://example.test"}]}, RULES, Path(directory) / "results.json", empty)
            self.assertEqual(result["status"], NO_MATCHES)

    def test_match_scoring_and_tiers(self):
        jobs = [{"title": "Accountant", "description": "Excel Oracle finance", "location": "Doha Qatar", "url": "https://example.test/1", "company": "Acme"}]
        matched = match_and_sort(jobs, PROFILE, RULES)
        self.assertEqual(matched[0]["score"], 100)
        self.assertEqual(matched[0]["match_tier"], "STRONG_MATCH")

    def test_telegram_message_formatting(self):
        text = format_shortlist({"status": FRESH_RESULTS, "result_origin": "FRESH", "jobs": [{"title": "Accountant", "score": 90, "company": "Acme", "location": "Doha", "url": "https://example.test/1"}]})
        self.assertIn("Accountant (90/100)", text)
        self.assertIn("https://example.test/1", text)

    def test_telegram_sent_and_no_secret_leakage(self):
        captured = {}
        def fake_api(token, method, payload):
            captured.update({"token": token, "method": method, "payload": payload})
            return {"ok": True, "result": {"message_id": 42}}
        result = send_shortlist({"status": FRESH_RESULTS, "result_origin": "FRESH", "jobs": []}, token="x" * 25, chat_id="123", api_call=fake_api)
        self.assertEqual(result["status"], "SENT")
        self.assertNotIn(captured["token"], json.dumps(result))
        self.assertNotIn(captured["token"], json.dumps(captured["payload"]))

    def test_telegram_chat_id_auto_detection(self):
        calls = []
        def fake_api(token, method, payload):
            calls.append(method)
            if method == "getUpdates":
                return {"ok": True, "result": [{"message": {"chat": {"id": 987}}}]}
            return {"ok": True, "result": {"message_id": 43}}
        with patch.dict(os.environ, {"ZAMAL_TELEGRAM_CHAT_ID": ""}, clear=False):
            result = send_shortlist({"status": FRESH_RESULTS, "result_origin": "FRESH", "jobs": []}, token="x" * 25, api_call=fake_api)
        self.assertEqual(result["status"], "SENT")
        self.assertEqual(result["chat_id"], "987")
        self.assertEqual(calls, ["getUpdates", "sendMessage"])

    def test_auto_apply_and_owner_approval_contract(self):
        rules = json.loads((Path(__file__).parents[1] / "config" / "job_match_rules.json").read_text())
        self.assertFalse(rules["auto_apply"])
        self.assertTrue(rules["owner_approval_required_for_submission"])
        integration = json.loads((Path(__file__).parents[1] / "config" / "job_operator_integration.json").read_text())
        self.assertFalse(integration["linkedin"]["password"])
        self.assertFalse(integration["linkedin"]["cookies"])


if __name__ == "__main__":
    unittest.main()