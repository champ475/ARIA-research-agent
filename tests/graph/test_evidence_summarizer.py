import json
from datetime import UTC, datetime, timedelta

import pandas as pd

from graph.nodes.evidence_summarizer import _summarize_payload, cap_evidence_for_prompt, evidence_summarizer


def _evidence(**overrides):
    base = {
        "id": "ev-1",
        "source": "fetch_info",
        "tool": "fetch_info",
        "entity": "RELIANCE",
        "evidence_type": "quote_info",
        "payload": {"pe": 24.3},
        "timestamp": datetime.now(UTC),
        "confidence": 0.9,
        "citation_key": "quote_info",
    }
    base.update(overrides)
    return base


class TestSummarizePayload:
    def test_dataframe_summarized_to_shape(self):
        df = pd.DataFrame({"close": [1, 2, 3], "open": [1, 2, 3]})
        result = _summarize_payload(df)
        assert result["rows"] == 3
        assert set(result["columns"]) == {"close", "open"}

    def test_dict_passed_through(self):
        assert _summarize_payload({"pe": 24.3}) == {"pe": 24.3}

    def test_list_summarized_to_count(self):
        assert _summarize_payload([1, 2, 3]) == {"count": 3}


class TestEvidenceSummarizer:
    def test_groups_by_entity_and_evidence_type(self):
        evidence = [
            _evidence(entity="RELIANCE", evidence_type="quote_info"),
            _evidence(id="ev-2", entity="TCS", evidence_type="quote_info"),
        ]
        result = evidence_summarizer({"evidence": evidence})
        assert set(result["evidence_summary"].keys()) == {"RELIANCE", "TCS"}

    def test_keeps_latest_when_duplicate_entity_type(self):
        old = _evidence(id="old", payload={"pe": 20.0}, timestamp=datetime.now(UTC) - timedelta(hours=1))
        new = _evidence(id="new", payload={"pe": 24.3}, timestamp=datetime.now(UTC))
        result = evidence_summarizer({"evidence": [old, new]})
        assert result["evidence_summary"]["RELIANCE"]["quote_info"]["evidence_id"] == "new"
        assert result["evidence_summary"]["RELIANCE"]["quote_info"]["summary"] == {"pe": 24.3}

    def test_separate_evidence_types_for_same_entity_both_kept(self):
        evidence = [
            _evidence(id="a", evidence_type="quote_info"),
            _evidence(id="b", evidence_type="fundamental_ratio", payload={"roce": 15.0}),
        ]
        result = evidence_summarizer({"evidence": evidence})
        assert set(result["evidence_summary"]["RELIANCE"].keys()) == {"quote_info", "fundamental_ratio"}

    def test_empty_evidence_produces_empty_summary(self):
        result = evidence_summarizer({"evidence": []})
        assert result["evidence_summary"] == {}

    def test_writes_journal_entry(self):
        result = evidence_summarizer({"evidence": [_evidence()]})
        assert result["research_journal"][0]["node"] == "EvidenceSummarizer"


class TestCapEvidenceForPrompt:
    """Bug fix: an uncapped evidence_summary reached 10,000-11,000 tokens
    against Groq free tier's 6,000 TPM cap in production. cap_evidence_for_prompt
    must keep the serialized payload under budget without ever truncating
    an individual entry mid-JSON."""

    def _big_summary(self, entity: str, evidence_type: str, confidence: float, field_count: int) -> dict:
        payload = {f"field_{i}": i for i in range(field_count)}
        return {
            entity: {
                evidence_type: {
                    "summary": payload,
                    "confidence": confidence,
                    "source": "fetch_info",
                    "evidence_id": f"{entity}-{evidence_type}",
                }
            }
        }

    def test_under_budget_summary_passes_through_unchanged(self):
        summary = self._big_summary("RELIANCE", "quote_info", 0.9, field_count=5)
        capped = cap_evidence_for_prompt(summary, char_budget=6000)
        assert capped == summary

    def test_oversized_single_entry_has_keys_truncated(self):
        # 200 fields is comfortably over _MAX_KEYS_PER_ENTRY (40) — mirrors
        # get_info()'s real-world 100+ yfinance fields.
        summary = self._big_summary("RELIANCE", "quote_info", 0.9, field_count=200)
        capped = cap_evidence_for_prompt(summary, char_budget=6000)
        capped_payload = capped["RELIANCE"]["quote_info"]["summary"]
        assert len(capped_payload) <= 41  # 40 kept keys + "_truncated_keys" marker
        assert "_truncated_keys" in capped_payload

    def test_result_serializes_under_budget(self):
        summary = {}
        for i in range(20):
            summary.update(self._big_summary(f"ENTITY{i}", "quote_info", 0.5, field_count=200))
        capped = cap_evidence_for_prompt(summary, char_budget=2000)
        assert len(json.dumps(capped, default=str)) <= 2000

    def test_higher_confidence_entries_survive_over_lower_confidence(self):
        low = self._big_summary("LOWCONF", "quote_info", 0.1, field_count=200)
        high = self._big_summary("HIGHCONF", "quote_info", 0.99, field_count=200)
        combined = {**low, **high}
        # Budget tight enough that only one of the two full entries fits.
        one_entry_size = len(json.dumps(cap_evidence_for_prompt(high, char_budget=999999), default=str))
        capped = cap_evidence_for_prompt(combined, char_budget=int(one_entry_size * 1.2))
        assert "HIGHCONF" in capped
        assert "LOWCONF" not in capped

    def test_no_entry_is_ever_partially_truncated_mid_json(self):
        # A dropped entry is absent entirely, never a partial/invalid fragment.
        summary = {}
        for i in range(10):
            summary.update(self._big_summary(f"ENTITY{i}", "quote_info", 1.0 - i * 0.05, field_count=200))
        capped = cap_evidence_for_prompt(summary, char_budget=1500)
        for _entity, by_type in capped.items():
            for _evidence_type, entry in by_type.items():
                # Every kept entry has its full expected shape — no
                # half-written keys.
                assert set(entry.keys()) >= {"summary", "confidence", "source", "evidence_id"}

    def test_empty_summary_returns_empty(self):
        assert cap_evidence_for_prompt({}, char_budget=6000) == {}
