"""Two-version evaluation preview ("what changes for these subjects?").

Pure, framework-free functions (plain dicts in, plain dicts out) like
``comparison.py``/``validation.py``, so the logic can be unit-tested without
FastAPI.

Semantics
---------
Given two pinned config versions, a list of flag names and a list of
subjects, every **subject × flag** cell is evaluated against *both*
versions and paired up:

* a cell is **changed** when the flag exists in one version but not the
  other (``exists`` differs), or when the two outcomes differ in JSON
  type or value of ``value``;
* a difference confined to ``reason``/``rule_id`` (same ``exists`` and
  the same ``value``) is still **unchanged** — e.g. a rule that was
  renumbered but keeps delivering the same value does not surface.

Evaluator scoping
-----------------
One :class:`~app.evaluator.FlagEvaluator` serves all flags of one
``(version, subject)`` pair, so a reusable audience segment referenced by
several of that subject's flags is computed once. Segment truth depends on
the subject's attributes, and the evaluator memoizes segment results on the
instance; therefore an evaluator (and its grouping cache) is **never reused
across subjects** — each subject gets two fresh evaluators, one per version.
"""

from __future__ import annotations

from typing import Any, Optional

from .evaluator import FlagEvaluator, FlagNotFound

# Request-size guards (mirrored by max_length on the request model).
MAX_SUBJECTS = 200
MAX_FLAGS = 50


def _json_type(value: Any) -> str:
    """Return the JSON type name of ``value``.

    ``bool`` is checked before ``int`` (``True`` is an ``int`` in Python but
    a JSON boolean, so ``true`` vs ``1`` must count as different types).
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _values_differ(left: Any, right: Any) -> bool:
    """True when two values differ in JSON type or value."""
    if _json_type(left) != _json_type(right):
        return True
    # Same JSON type: plain equality covers numbers/strings and structural
    # equality for arrays/objects (key order is irrelevant).
    return left != right


def _is_changed(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["exists"] != right["exists"]:
        return True
    return _values_differ(left["value"], right["value"])


def _evaluate_side(
    evaluator: FlagEvaluator,
    flag_name: str,
    attributes: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate one flag on one version; a missing flag is a defined outcome."""
    try:
        result = evaluator.evaluate(flag_name, attributes=attributes)
    except FlagNotFound:
        return {
            "exists": False,
            "value": None,
            "reason": "flag_not_found",
            "rule_id": None,
        }
    return {
        "exists": True,
        "value": result["value"],
        "reason": result["reason"],
        "rule_id": result["rule_id"],
    }


def preview_evaluations(
    from_config: dict[str, Any],
    to_config: dict[str, Any],
    flag_names: list[str],
    subjects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate the subject × flag matrix against both versions.

    ``subjects`` is a list of ``{"subject_id", "user_id", "attributes"}``
    dicts in request order; ``flag_names`` likewise preserves request order
    (including any duplicated entry — each requested position is one matrix
    column).

    Returns ``{"changed": [...], "summary": [...]}`` where ``changed`` lists
    every changed cell in request order (subject first, flag second) and
    ``summary`` reports changed/unchanged counts per requested flag.
    """
    from_flags = from_config.get("flags", {})
    from_segments = from_config.get("segments")
    to_flags = to_config.get("flags", {})
    to_segments = to_config.get("segments")

    # One counter pair per requested flag position, so duplicated flag names
    # never collapse onto one another.
    counts: list[list[int]] = [[0, 0] for _ in flag_names]
    changed: list[dict[str, Any]] = []

    for subject in subjects:
        user_id = subject.get("user_id")
        attributes: dict[str, Any] = dict(subject.get("attributes") or {})
        if user_id is not None and "user_id" not in attributes:
            attributes["user_id"] = user_id

        # Fresh evaluators per (version, subject): they share segment-group
        # computations across this subject's flags, but the grouping cache
        # must never leak to another subject.
        from_evaluator = FlagEvaluator(from_flags, segments=from_segments)
        to_evaluator = FlagEvaluator(to_flags, segments=to_segments)

        for idx, flag_name in enumerate(flag_names):
            left = _evaluate_side(from_evaluator, flag_name, attributes)
            right = _evaluate_side(to_evaluator, flag_name, attributes)
            if _is_changed(left, right):
                counts[idx][0] += 1
                changed.append(
                    {
                        "subject_id": subject["subject_id"],
                        "flag": flag_name,
                        "from": left,
                        "to": right,
                    }
                )
            else:
                counts[idx][1] += 1

    summary = [
        {"flag": flag_name, "changed": counts[idx][0], "unchanged": counts[idx][1]}
        for idx, flag_name in enumerate(flag_names)
    ]
    return {"changed": changed, "summary": summary}
