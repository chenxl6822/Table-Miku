"""Tests for review_scheduler — pure logic, no Qt dependency."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku.review_scheduler import (
    MAX_MASTERY,
    MAX_STAGE,
    MIN_MASTERY,
    REVIEW_INTERVALS,
    apply_review_result,
    default_review_state,
    due_sort_key,
    is_due,
)


class TestDefaultReviewState:
    def test_fields_present(self):
        state = default_review_state("wiki-test")
        assert state["card_id"] == "wiki-test"
        assert state["mastery"] == 0.0
        assert state["review_stage"] == 0
        assert state["next_review_at"] is not None
        assert state["review_count"] == 0
        assert state["last_reviewed_at"] is None
        assert state["created_at"] is not None
        assert state["updated_at"] is not None
        assert state["history"] == []

    def test_uses_provided_now(self):
        now = datetime(2026, 6, 3, 12, 0, 0)
        state = default_review_state("wiki-test", now)
        assert state["next_review_at"] == now.isoformat(timespec="seconds")
        assert state["created_at"] == now.isoformat(timespec="seconds")


class TestApplyReviewResult:
    def test_known_increases_stage_and_mastery(self):
        state = default_review_state("wiki-test")
        result = apply_review_result(state, "known")
        assert result["review_stage"] == 1
        assert result["mastery"] == 0.2
        assert result["review_count"] == 1
        assert result["last_reviewed_at"] is not None

    def test_fuzzy_keeps_stage_increases_mastery(self):
        state = default_review_state("wiki-test")
        state["review_stage"] = 2
        result = apply_review_result(state, "fuzzy")
        assert result["review_stage"] == 2
        assert result["mastery"] == 0.05

    def test_forgotten_resets_stage_to_zero(self):
        state = default_review_state("wiki-test")
        state["review_stage"] = 3
        state["mastery"] = 0.6
        result = apply_review_result(state, "forgotten")
        assert result["review_stage"] == 0
        assert abs(result["mastery"] - 0.45) < 0.001

    def test_mastery_capped_at_max(self):
        state = default_review_state("wiki-test")
        state["mastery"] = 0.95
        result = apply_review_result(state, "known")
        assert result["mastery"] == MAX_MASTERY

    def test_mastery_floor_at_min(self):
        state = default_review_state("wiki-test")
        state["mastery"] = 0.05
        result = apply_review_result(state, "forgotten")
        assert result["mastery"] == MIN_MASTERY

    def test_stage_capped_at_max(self):
        state = default_review_state("wiki-test")
        state["review_stage"] = MAX_STAGE
        result = apply_review_result(state, "known")
        assert result["review_stage"] == MAX_STAGE

    def test_history_appended(self):
        state = default_review_state("wiki-test")
        result = apply_review_result(state, "known", note="good")
        assert len(result["history"]) == 1
        assert result["history"][0]["result"] == "known"
        assert result["history"][0]["note"] == "good"

    def test_next_review_at_set_correctly_after_known(self):
        now = datetime(2026, 6, 3, 12, 0, 0)
        state = default_review_state("wiki-test", now)
        state = apply_review_result(state, "known", now=now)
        # stage is now 1 => 1 day interval
        expected = now + REVIEW_INTERVALS[1]
        assert state["next_review_at"] == expected.isoformat(timespec="seconds")

    def test_next_review_at_after_forgotten(self):
        now = datetime(2026, 6, 3, 12, 0, 0)
        state = default_review_state("wiki-test", now)
        state["review_stage"] = 4
        state = apply_review_result(state, "forgotten", now=now)
        # stage reset to 0 => 1 hour interval
        expected = now + REVIEW_INTERVALS[0]
        assert state["next_review_at"] == expected.isoformat(timespec="seconds")

    def test_review_count_increments(self):
        state = default_review_state("wiki-test")
        for i in range(3):
            state = apply_review_result(state, "known")
        assert state["review_count"] == 3

    def test_unknown_result_raises(self):
        state = default_review_state("wiki-test")
        try:
            apply_review_result(state, "bad")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


class TestIsDue:
    def test_past_date_is_due(self):
        past = datetime.now() - timedelta(hours=2)
        state = {"next_review_at": past.isoformat(timespec="seconds")}
        assert is_due(state) is True

    def test_future_date_not_due(self):
        future = datetime.now() + timedelta(days=7)
        state = {"next_review_at": future.isoformat(timespec="seconds")}
        assert is_due(state) is False

    def test_missing_next_at_is_due(self):
        assert is_due({}) is True

    def test_invalid_iso_is_due(self):
        assert is_due({"next_review_at": "not-a-date"}) is True

    def test_exact_now_is_due(self):
        now = datetime.now()
        state = {"next_review_at": now.isoformat(timespec="seconds")}
        assert is_due(state, now) is True


class TestDueSortKey:
    def test_sorts_by_next_at_then_mastery(self):
        item1 = {"state": {"next_review_at": "2026-06-03T12:00:00", "mastery": 0.5}}
        item2 = {"state": {"next_review_at": "2026-06-03T12:00:00", "mastery": 0.1}}
        item3 = {"state": {"next_review_at": "2026-06-03T14:00:00", "mastery": 0.0}}
        # item1 and item2 same date, item2 lower mastery => item2 < item1
        assert due_sort_key(item2) < due_sort_key(item1)
        # item3 later date => item1 < item3
        assert due_sort_key(item1) < due_sort_key(item3)

    def test_defaults_for_missing_state(self):
        item = {"card": {}}
        key = due_sort_key(item)
        assert key == ("9999", "0.000")
