from datetime import datetime, timezone

from graph.nodes.conclusion_builder import conclusion_builder


def _evidence(**overrides):
    base = {
        "id": "ev-1",
        "source": "compute_fundamentals",
        "tool": "compute_fundamentals",
        "entity": "RELIANCE",
        "evidence_type": "fundamental_ratio",
        "payload": {"pe": 24.3, "roce": 15.2},
        "timestamp": datetime.now(timezone.utc),
        "confidence": 0.9,
        "citation_key": "fundamental_ratio",
    }
    base.update(overrides)
    return base


class TestConclusionBuilder:
    def test_every_conclusion_traces_to_an_evidence_id_present_in_input(self):
        evidence = [_evidence()]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        evidence_ids = {ev["id"] for ev in evidence}
        for c in result["conclusions"]:
            assert set(c["evidence_ids"]).issubset(evidence_ids)

    def test_canonical_ratio_keys_get_exact_citation_key(self):
        evidence = [_evidence(payload={"pe": 24.3})]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert result["conclusions"][0]["citation_key"] == "pe"

    def test_non_canonical_field_gets_scoped_citation_key(self):
        evidence = [_evidence(payload={"weird_metric": 5.0})]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert result["conclusions"][0]["citation_key"] == "RELIANCE.weird_metric"

    def test_one_conclusion_per_numeric_field(self):
        evidence = [_evidence(payload={"pe": 24.3, "roce": 15.2, "de": 0.4})]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert len(result["conclusions"]) == 3

    def test_non_numeric_fields_ignored(self):
        evidence = [_evidence(payload={"pe": 24.3, "sector": "Energy"})]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert len(result["conclusions"]) == 1

    def test_nan_field_excluded(self):
        evidence = [_evidence(payload={"pe": float("nan")})]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert result["conclusions"] == []

    def test_news_headline_list_produces_count_conclusion(self):
        evidence = [_evidence(
            id="ev-news", evidence_type="news_headline",
            payload=[{"title": "a"}, {"title": "b"}],
        )]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert result["conclusions"][0]["value"] == 2

    def test_flagged_evidence_excluded_from_conclusions(self):
        evidence = [_evidence(id="ev-bad", payload={"pe": 999999999})]
        issues = [{"claim": "x", "issue_type": "unsupported", "evidence_ids": ["ev-bad"], "stage": "deterministic"}]
        result = conclusion_builder({"evidence": evidence, "verification_issues": issues})
        assert result["conclusions"] == []

    def test_unflagged_evidence_still_included_when_others_flagged(self):
        good = _evidence(id="ev-good", entity="TCS", payload={"pe": 20.0})
        bad = _evidence(id="ev-bad", payload={"pe": 999999999})
        issues = [{"claim": "x", "issue_type": "unsupported", "evidence_ids": ["ev-bad"], "stage": "deterministic"}]
        result = conclusion_builder({"evidence": [good, bad], "verification_issues": issues})
        assert len(result["conclusions"]) == 1
        assert result["conclusions"][0]["evidence_ids"] == ["ev-good"]

    def test_writes_journal_entry(self):
        result = conclusion_builder({"evidence": [_evidence()], "verification_issues": []})
        assert result["research_journal"][0]["node"] == "ConclusionBuilder"

    def test_empty_evidence_produces_empty_conclusions(self):
        result = conclusion_builder({"evidence": [], "verification_issues": []})
        assert result["conclusions"] == []
