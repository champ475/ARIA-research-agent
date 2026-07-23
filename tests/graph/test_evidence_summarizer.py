from datetime import datetime, timedelta, timezone

import pandas as pd

from graph.nodes.evidence_summarizer import evidence_summarizer, _summarize_payload


def _evidence(**overrides):
    base = {
        "id": "ev-1",
        "source": "fetch_info",
        "tool": "fetch_info",
        "entity": "RELIANCE",
        "evidence_type": "quote_info",
        "payload": {"pe": 24.3},
        "timestamp": datetime.now(timezone.utc),
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
        old = _evidence(id="old", payload={"pe": 20.0}, timestamp=datetime.now(timezone.utc) - timedelta(hours=1))
        new = _evidence(id="new", payload={"pe": 24.3}, timestamp=datetime.now(timezone.utc))
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
