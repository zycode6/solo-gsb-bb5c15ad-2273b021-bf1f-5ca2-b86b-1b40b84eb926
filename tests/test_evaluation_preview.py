"""Tests for the two-version evaluation preview.

The pure ``preview_evaluations`` logic is standard-library only; the HTTP
test case is skipped when fastapi/httpx are not installed.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import evaluator as evaluator_mod  # noqa: E402
from app.evaluation_preview import (  # noqa: E402
    MAX_FLAGS,
    MAX_SUBJECTS,
    _json_type,
    preview_evaluations,
)

try:
    from fastapi.testclient import TestClient as _TestClient

    _HAS_HTTP = True
except Exception:  # pragma: no cover - exercised only without third-party deps
    _HAS_HTTP = False


def flag(**kw):
    base = {"enabled": True, "default": False, "prerequisites": [], "rules": []}
    base.update(kw)
    return base


def rule(conditions, value=True, id=None):
    r = {"conditions": conditions, "value": value}
    if id is not None:
        r["id"] = id
    return r


def subj(subject_id, user_id=None, attributes=None):
    s = {"subject_id": subject_id}
    if user_id is not None:
        s["user_id"] = user_id
    s["attributes"] = attributes or {}
    return s


class JsonTypeTests(unittest.TestCase):
    def test_bool_is_not_integer(self):
        self.assertEqual(_json_type(True), "boolean")
        self.assertEqual(_json_type(False), "boolean")
        self.assertEqual(_json_type(1), "integer")
        self.assertEqual(_json_type(0), "integer")

    def test_json_types(self):
        self.assertEqual(_json_type(None), "null")
        self.assertEqual(_json_type(1.5), "number")
        self.assertEqual(_json_type("x"), "string")
        self.assertEqual(_json_type([]), "array")
        self.assertEqual(_json_type({}), "object")


class PreviewValueChangeTests(unittest.TestCase):
    def setUp(self):
        self.from_config = {
            "flags": {
                "bool_flag": flag(default=False),
                "str_flag": flag(default="off"),
                "int_flag": flag(default=1),
                "kept": flag(default=True),
            }
        }
        self.to_config = {
            "flags": {
                "bool_flag": flag(default=True),
                "str_flag": flag(default="on"),
                # Same truthiness/equality in Python but a different JSON type.
                "int_flag": flag(default=True),
                "kept": flag(default=True),
            }
        }
        self.flags = ["bool_flag", "str_flag", "int_flag", "kept"]

    def test_changed_sides_carry_full_outcomes(self):
        result = preview_evaluations(
            self.from_config, self.to_config, self.flags, [subj("s1")]
        )
        changed = result["changed"]
        by_flag = {item["flag"]: item for item in changed}
        self.assertEqual(set(by_flag), {"bool_flag", "str_flag", "int_flag"})
        bool_item = by_flag["bool_flag"]
        self.assertEqual(bool_item["subject_id"], "s1")
        self.assertTrue(bool_item["from"]["exists"])
        self.assertEqual(bool_item["from"]["value"], False)
        self.assertEqual(bool_item["from"]["reason"], "default")
        self.assertIsNone(bool_item["from"]["rule_id"])
        self.assertTrue(bool_item["to"]["exists"])
        self.assertEqual(bool_item["to"]["value"], True)
        self.assertEqual(bool_item["to"]["reason"], "default")
        self.assertEqual(by_flag["int_flag"]["from"]["value"], 1)
        self.assertEqual(by_flag["int_flag"]["to"]["value"], True)

    def test_same_value_is_unchanged_even_when_rule_reason_differs(self):
        # v1: catch-all rule "r1" returns true; v2: different rule id/order
        # ("r2") returns the same value -> unchanged.
        cfg_a = {"flags": {"f": flag(rules=[rule([], True, id="r1")])}}
        cfg_b = {"flags": {"f": flag(rules=[rule([], True, id="r2")])}}
        result = preview_evaluations(cfg_a, cfg_b, ["f"], [subj("s1")])
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["summary"], [{"flag": "f", "changed": 0, "unchanged": 1}])

    def test_default_vs_rule_same_value_is_unchanged(self):
        cfg_a = {"flags": {"f": flag(default="x")}}
        cfg_b = {"flags": {"f": flag(default="x", rules=[rule([], value="x", id="r")])}}
        result = preview_evaluations(cfg_a, cfg_b, ["f"], [subj("s1")])
        self.assertEqual(result["changed"], [])

    def test_number_types_are_distinct(self):
        # 1 (integer) vs 1.0 (number) differ in JSON type even though ==.
        result = preview_evaluations(
            {"flags": {"f": flag(default=1)}},
            {"flags": {"f": flag(default=1.0)}},
            ["f"],
            [subj("s1")],
        )
        self.assertEqual(len(result["changed"]), 1)
        # "1" (string) vs 1 (integer) likewise.
        result = preview_evaluations(
            {"flags": {"f": flag(default="1")}},
            {"flags": {"f": flag(default=1)}},
            ["f"],
            [subj("s1")],
        )
        self.assertEqual(len(result["changed"]), 1)

    def test_array_value_structural_compare(self):
        result = preview_evaluations(
            {"flags": {"f": flag(default=[1, 2])}},
            {"flags": {"f": flag(default=[1, 2])}},
            ["f"],
            [subj("s1")],
        )
        self.assertEqual(result["changed"], [])
        result = preview_evaluations(
            {"flags": {"f": flag(default=[1, 2])}},
            {"flags": {"f": flag(default=[2, 1])}},
            ["f"],
            [subj("s1")],
        )
        self.assertEqual(len(result["changed"]), 1)


class PreviewExistenceTests(unittest.TestCase):
    def setUp(self):
        self.from_config = {"flags": {"gone": flag(default="v"), "stays": flag(default=1)}}
        self.to_config = {"flags": {"stays": flag(default=1), "added": flag(default="x")}}

    def test_removed_flag_is_changed_with_flag_not_found_side(self):
        result = preview_evaluations(
            self.from_config, self.to_config, ["gone"], [subj("s1")]
        )
        item = result["changed"][0]
        self.assertTrue(item["from"]["exists"])
        self.assertEqual(item["from"]["value"], "v")
        self.assertFalse(item["to"]["exists"])
        self.assertIsNone(item["to"]["value"])
        self.assertEqual(item["to"]["reason"], "flag_not_found")
        self.assertIsNone(item["to"]["rule_id"])

    def test_added_flag_is_changed(self):
        result = preview_evaluations(
            self.from_config, self.to_config, ["added"], [subj("s1")]
        )
        item = result["changed"][0]
        self.assertFalse(item["from"]["exists"])
        self.assertEqual(item["from"]["reason"], "flag_not_found")
        self.assertTrue(item["to"]["exists"])
        self.assertEqual(item["to"]["value"], "x")

    def test_missing_in_both_versions_is_unchanged(self):
        result = preview_evaluations(
            self.from_config, self.to_config, ["ghost"], [subj("s1")]
        )
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["summary"], [{"flag": "ghost", "changed": 0, "unchanged": 1}])


class PreviewOrderingAndSummaryTests(unittest.TestCase):
    def setUp(self):
        self.cfg_a = {"flags": {"f1": flag(default=False), "f2": flag(default="a")}}
        self.cfg_b = {"flags": {"f1": flag(default=True), "f2": flag(default="b")}}
        self.flags = ["f1", "f2"]
        self.subjects = [subj("b"), subj("a"), subj("c")]

    def test_changed_items_follow_request_order_subject_major(self):
        result = preview_evaluations(
            self.cfg_a, self.cfg_b, self.flags, self.subjects
        )
        keys = [(item["subject_id"], item["flag"]) for item in result["changed"]]
        self.assertEqual(
            keys,
            [("b", "f1"), ("b", "f2"), ("a", "f1"), ("a", "f2"), ("c", "f1"), ("c", "f2")],
        )

    def test_summary_per_flag_in_request_order(self):
        result = preview_evaluations(
            self.cfg_a,
            {"flags": {"f1": flag(default=True), "f2": flag(default="a")}},
            self.flags,
            self.subjects,
        )
        self.assertEqual(
            result["summary"],
            [
                {"flag": "f1", "changed": 3, "unchanged": 0},
                {"flag": "f2", "changed": 0, "unchanged": 3},
            ],
        )

    def test_duplicate_flag_positions_are_counted_separately(self):
        result = preview_evaluations(
            self.cfg_a, self.cfg_b, ["f1", "f1"], [subj("s1")]
        )
        self.assertEqual(len(result["changed"]), 2)
        self.assertEqual(
            result["summary"],
            [
                {"flag": "f1", "changed": 1, "unchanged": 0},
                {"flag": "f1", "changed": 1, "unchanged": 0},
            ],
        )

    def test_limits(self):
        self.assertEqual(MAX_SUBJECTS, 200)
        self.assertEqual(MAX_FLAGS, 50)


class PreviewEvaluatorScopingTests(unittest.TestCase):
    def _configs(self):
        segments = {"vip": {"conditions": [{"attr": "tier", "op": "eq", "value": "gold"}]}}
        cfg_v1 = {
            "segments": segments,
            "flags": {
                "a": flag(rules=[rule([{"op": "segment", "value": "vip"}], id="r")]),
                "b": flag(rules=[rule([{"op": "segment", "value": "vip"}], id="r")]),
            },
        }
        # Same config both versions: every cell is unchanged, but if the
        # grouping cache leaked across subjects the non-gold subject would
        # inherit the gold subject's memoized segment match.
        return cfg_v1, cfg_v1

    def test_segment_cache_does_not_leak_across_subjects(self):
        from_config, to_config = self._configs()
        subjects = [
            subj("gold", attributes={"tier": "gold"}),
            subj("free", attributes={"tier": "free"}),
            subj("gold-2", attributes={"tier": "gold"}),
        ]
        result = preview_evaluations(
            from_config, to_config, ["a", "b"], subjects
        )
        # Nothing differs between versions...
        self.assertEqual(result["changed"], [])
        # ...and independently recompute per subject to prove fresh caches:
        # patch the condition function so the segment condition runs, and
        # verify a free subject evaluates the segment for itself.
        real_match = evaluator_mod._compare
        calls: list[str] = []

        def spy(op, attr, expected):
            if op == "eq":
                calls.append(attr)
            return real_match(op, attr, expected)

        with mock.patch.object(evaluator_mod, "_compare", side_effect=spy):
            preview_evaluations(from_config, to_config, ["a"], subjects)
        # Three subjects x two versions -> one segment computation per
        # (version, subject): "free" must still be observed for the middle
        # subject, which it would not be if a previous subject's memo leaked.
        self.assertEqual(calls, ["gold", "gold", "free", "free", "gold", "gold"])

    def test_one_evaluator_serves_all_flags_of_one_subject_version(self):
        # Two flags share one segment for one subject: the segment condition
        # runs once per (version, subject), proving evaluator reuse within
        # the version x subject cell.
        from_config, _ = self._configs()
        calls = []
        real_match = evaluator_mod._compare

        def spy(op, attr, expected):
            if op == "eq":
                calls.append((attr, len(calls)))
            return real_match(op, attr, expected)

        with mock.patch.object(evaluator_mod, "_compare", side_effect=spy):
            preview_evaluations(
                from_config,
                from_config,
                ["a", "b"],
                [subj("gold", attributes={"tier": "gold"})],
            )
        # One subject, two versions, two flags: the vip segment condition is
        # evaluated exactly once per version (twice total), not per flag.
        self.assertEqual([c[0] for c in calls], ["gold", "gold"])

    def test_user_id_drives_rollout_when_absent_from_attributes(self):
        cfg_a = {"flags": {"f": flag(rollout={"percentage": 0, "key": "user_id"})}}
        cfg_b = {"flags": {"f": flag(rollout={"percentage": 100, "key": "user_id"})}}
        result = preview_evaluations(
            cfg_a, cfg_b, ["f"], [subj("s1", user_id="user-1")]
        )
        item = result["changed"][0]
        self.assertEqual(item["from"]["reason"], "rollout_out")
        self.assertEqual(item["to"]["reason"], "rollout_in")


@unittest.skipUnless(_HAS_HTTP, "fastapi/httpx not installed")
class PreviewEndpointHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLAG_DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        from app import db as db_mod
        from app.main import app

        self.db_mod = db_mod
        self.app = app
        # TestClient used without a ``with`` block does not run the lifespan
        # handler, so create the schema explicitly.
        conn = db_mod.connect()
        try:
            db_mod.init_db(conn)
        finally:
            conn.close()
        self.client = _TestClient(app)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()
        os.environ.pop("FLAG_DB_PATH", None)

    def _publish(self, version, flags, segments=None):
        payload = {"version": version, "flags": flags}
        if segments:
            payload["segments"] = segments
        r = self.client.post("/api/v1/configs", json=payload)
        self.assertEqual(r.status_code, 201, r.text)

    def _request(self, **overrides):
        body = {
            "from_version": "v1",
            "to_version": "v2",
            "flags": ["f"],
            "subjects": [subj("s1")],
        }
        body.update(overrides)
        return self.client.post("/api/v1/evaluation-previews", json=body)

    def test_preview_end_to_end(self):
        self._publish("v1", {
            "roll": flag(rollout={"percentage": 0, "key": "user_id"}),
            "gone": flag(default="x"),
        })
        self._publish("v2", {
            "roll": flag(rollout={"percentage": 100, "key": "user_id"}),
            "added": flag(default=True),
        })
        r = self._request(
            flags=["roll", "gone", "added", "ghost"],
            subjects=[subj("u1", user_id="u1"), subj("u2", user_id="u2")],
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["from_version"], "v1")
        self.assertEqual(data["to_version"], "v2")
        keys = [(i["subject_id"], i["flag"]) for i in data["changed"]]
        self.assertEqual(
            keys,
            [("u1", "roll"), ("u1", "gone"), ("u1", "added"),
             ("u2", "roll"), ("u2", "gone"), ("u2", "added")],
        )
        self.assertEqual(
            data["summary"],
            [
                {"flag": "roll", "changed": 2, "unchanged": 0},
                {"flag": "gone", "changed": 2, "unchanged": 0},
                {"flag": "added", "changed": 2, "unchanged": 0},
                {"flag": "ghost", "changed": 0, "unchanged": 2},
            ],
        )

    def test_missing_from_version_is_404_without_partial_results(self):
        self._publish("v1", {"f": flag()})
        r = self._request(to_version="v2")  # v2 absent
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertEqual(body["error"]["code"], "version_not_found")
        self.assertNotIn("changed", body)
        self.assertNotIn("summary", body)

    def test_missing_to_version_is_404(self):
        self._publish("v2", {"f": flag()})
        r = self._request(from_version="v1")  # v1 absent
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "version_not_found")

    def test_duplicate_subject_id_is_422(self):
        self._publish("v1", {"f": flag()})
        self._publish("v2", {"f": flag()})
        r = self._request(subjects=[subj("a"), subj("b"), subj("a")])
        self.assertEqual(r.status_code, 422)
        errors = r.json()["error"]["errors"]
        self.assertTrue(any(e["type"] == "duplicate_subject_id" for e in errors))

    def test_too_many_subjects_is_422(self):
        r = self._request(subjects=[subj(f"s{i}") for i in range(MAX_SUBJECTS + 1)])
        self.assertEqual(r.status_code, 422)

    def test_too_many_flags_is_422(self):
        r = self._request(flags=[f"f{i}" for i in range(MAX_FLAGS + 1)],
                          subjects=[subj("s")])
        self.assertEqual(r.status_code, 422)

    def test_empty_subjects_and_flags_are_422(self):
        self.assertEqual(self._request(subjects=[]).status_code, 422)
        self.assertEqual(self._request(flags=[], subjects=[subj("s")]).status_code, 422)

    def test_request_validation_precedes_version_lookup(self):
        # Malformed request (duplicate subjects) against missing versions
        # must be 422, never 404.
        r = self._request(
            from_version="ghost", to_version="phantom",
            subjects=[subj("a"), subj("a")],
        )
        self.assertEqual(r.status_code, 422)

    def test_duplicate_flag_names_are_allowed(self):
        self._publish("v1", {"f": flag(default=False)})
        self._publish("v2", {"f": flag(default=True)})
        r = self._request(flags=["f", "f"], subjects=[subj("s")])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["changed"]), 2)

    def test_error_envelope_is_json(self):
        r = self._request(subjects=[])
        self.assertEqual(r.headers["content-type"], "application/json")
        self.assertIn("error", r.json())

    def test_openapi_documents_endpoint(self):
        path = self.app.openapi()["paths"]["/api/v1/evaluation-previews"]
        self.assertIn("post", path)


if __name__ == "__main__":
    unittest.main()
