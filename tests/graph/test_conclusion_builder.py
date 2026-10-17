from datetime import UTC, datetime

from graph.nodes.conclusion_builder import conclusion_builder


def _evidence(**overrides):
    base = {
        "id": "ev-1",
        "source": "compute_fundamentals",
        "tool": "compute_fundamentals",
        "entity": "RELIANCE",
        "evidence_type": "fundamental_ratio",
        "payload": {"pe": 24.3, "roce": 15.2},
        "timestamp": datetime.now(UTC),
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
        evidence = [
            _evidence(
                id="ev-news",
                evidence_type="news_headline",
                payload=[{"title": "a"}, {"title": "b"}],
            )
        ]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert result["conclusions"][0]["value"] == 2

    def test_flagged_evidence_excluded_from_conclusions(self):
        evidence = [_evidence(id="ev-bad", payload={"pe": 999999999})]
        issues = [
            {"claim": "x", "issue_type": "unsupported", "evidence_ids": ["ev-bad"], "stage": "deterministic"}
        ]
        result = conclusion_builder({"evidence": evidence, "verification_issues": issues})
        assert result["conclusions"] == []

    def test_unflagged_evidence_still_included_when_others_flagged(self):
        good = _evidence(id="ev-good", entity="TCS", payload={"pe": 20.0})
        bad = _evidence(id="ev-bad", payload={"pe": 999999999})
        issues = [
            {"claim": "x", "issue_type": "unsupported", "evidence_ids": ["ev-bad"], "stage": "deterministic"}
        ]
        result = conclusion_builder({"evidence": [good, bad], "verification_issues": issues})
        assert len(result["conclusions"]) == 1
        assert result["conclusions"][0]["evidence_ids"] == ["ev-good"]

    def test_writes_journal_entry(self):
        result = conclusion_builder({"evidence": [_evidence()], "verification_issues": []})
        assert result["research_journal"][0]["node"] == "ConclusionBuilder"

    def test_empty_evidence_produces_empty_conclusions(self):
        result = conclusion_builder({"evidence": [], "verification_issues": []})
        assert result["conclusions"] == []

    def test_pdf_extract_text_produces_conclusion(self):
        evidence = [
            _evidence(
                id="ev-qr",
                tool="fetch_quarterly_results",
                evidence_type="pdf_extract",
                payload="Revenue grew 12% YoY, net profit up 8%.",
            )
        ]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert len(result["conclusions"]) == 1
        assert result["conclusions"][0]["citation_key"] == "RELIANCE.fetch_quarterly_results"
        assert result["conclusions"][0]["value"] == "Revenue grew 12% YoY, net profit up 8%."

    def test_pdf_extract_not_available_text_produces_no_conclusion(self):
        evidence = [
            _evidence(
                id="ev-ar",
                tool="fetch_annual_report_text",
                evidence_type="pdf_extract",
                payload="No annual report found for this ticker.",
            )
        ]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert result["conclusions"] == []

    def test_mf_comparison_text_produces_conclusion(self):
        evidence = [
            _evidence(
                id="ev-cmp",
                tool="fetch_mf_comparison",
                evidence_type="mf_comparison",
                payload="Fund A has lower expense ratio than Fund B.",
            )
        ]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert len(result["conclusions"]) == 1
        assert result["conclusions"][0]["citation_key"] == "RELIANCE.fetch_mf_comparison"

    def test_mf_dataframe_evidence_produces_match_count_conclusion(self):
        import pandas as pd

        evidence = [
            _evidence(
                id="ev-nav",
                tool="fetch_nav",
                evidence_type="mf_nav",
                entity="HDFC Flexicap",
                payload=pd.DataFrame({"scheme_name": ["A", "B", "C"]}),
            )
        ]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert result["conclusions"][0]["value"] == 3
        assert result["conclusions"][0]["citation_key"] == "HDFC Flexicap.mf_nav"

    def test_alert_list_evidence_produces_count_conclusion(self):
        evidence = [
            _evidence(
                id="ev-al",
                tool="alert_list",
                evidence_type="alert_list",
                payload=[{"id": 1}, {"id": 2}],
            )
        ]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert result["conclusions"][0]["value"] == 2
        assert result["conclusions"][0]["citation_key"] == "alerts.count"

    def test_screener_table_text_produces_conclusion(self):
        evidence = [
            _evidence(
                id="ev-tbl",
                tool="screener_to_markdown",
                evidence_type="screener_table",
                entity="market",
                payload="| symbol | pe |\n| TCS | 24.3 |",
            )
        ]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert len(result["conclusions"]) == 1
        assert result["conclusions"][0]["citation_key"] == "market.screener_to_markdown"
        assert "| TCS | 24.3 |" in result["conclusions"][0]["value"]

    def test_top_movers_dataframe_produces_count_conclusion(self):
        import pandas as pd

        evidence = [
            _evidence(
                id="ev-mov",
                tool="fetch_top_movers",
                evidence_type="top_movers",
                entity="market",
                payload=pd.DataFrame({"symbol": ["TCS", "INFY"]}),
            )
        ]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert result["conclusions"][0]["value"] == 2
        assert "top mover(s)" in result["conclusions"][0]["claim"]

    def test_bulk_deals_dataframe_produces_count_conclusion(self):
        import pandas as pd

        evidence = [
            _evidence(
                id="ev-bulk",
                tool="fetch_bulk_deals",
                evidence_type="bulk_deals",
                entity="TCS",
                payload=pd.DataFrame({"symbol": ["TCS"]}),
            )
        ]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert result["conclusions"][0]["value"] == 1
        assert "bulk deal(s)" in result["conclusions"][0]["claim"]

    def test_screener_saved_evidence_produces_conclusion(self):
        evidence = [
            _evidence(
                id="ev-save",
                tool="screener_save",
                evidence_type="screener_saved",
                entity="market",
                payload={"name": "My Screen", "filters": {"max_pe": 20}},
            )
        ]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert "My Screen" in result["conclusions"][0]["claim"]
        assert result["conclusions"][0]["citation_key"] == "market.screener_saved"

    def test_alert_action_evidence_produces_conclusion(self):
        evidence = [
            _evidence(
                id="ev-aa",
                tool="alert_cancel",
                evidence_type="alert_action",
                payload={"action": "cancel", "symbol": "TCS", "result": "removed", "alert_id": 5},
            )
        ]
        result = conclusion_builder({"evidence": evidence, "verification_issues": []})
        assert result["conclusions"][0]["claim"] == "Cancelled alert #5 on TCS"
        assert result["conclusions"][0]["citation_key"] == "RELIANCE.alert_cancel"


def _plain_evidence(etype, payload, entity="market"):
    from datetime import UTC, datetime

    return {
        "id": f"{etype}-1",
        "source": "t",
        "tool": "t",
        "entity": entity,
        "evidence_type": etype,
        "payload": payload,
        "timestamp": datetime.now(UTC),
        "confidence": 1.0,
        "citation_key": etype,
    }


def test_movers_conclusion_names_the_biggest_moves():
    import pandas as pd

    df = pd.DataFrame(
        {"symbol": ["TECHM", "TCS", "INFY", "WIPRO"], "ltp": [1, 2, 3, 4], "perChange": [4.2, 3.9, 3.1, 2.0]}
    )
    claims = [
        c["claim"]
        for c in conclusion_builder({"evidence": [_plain_evidence("top_movers", df)]})["conclusions"]
    ]
    assert any("TECHM +4.20%, TCS +3.90%, INFY +3.10%" in c for c in claims)


def test_movers_without_a_percent_column_only_get_the_count():
    import pandas as pd

    df = pd.DataFrame({"symbol": ["TECHM"], "ltp": [1790]})
    claims = [
        c["claim"]
        for c in conclusion_builder({"evidence": [_plain_evidence("top_movers", df)]})["conclusions"]
    ]
    assert claims == ["market: 1 top mover(s) on record"]


def test_market_overview_claims_become_conclusions():
    payload = {
        "indices": {},
        "claims": [
            {
                "claim": "Nifty 50 is at 24,850.35, +0.42% against the previous close",
                "value": 24850.35,
                "key": "nifty_50",
            }
        ],
    }
    out = conclusion_builder({"evidence": [_plain_evidence("market_overview", payload)]})["conclusions"]
    assert out[0]["claim"].startswith("Nifty 50 is at 24,850.35")


def test_screener_results_become_a_conclusion_naming_the_matches():
    rows = [{"symbol": f"S{i}", "pe": 10 + i} for i in range(12)]
    claims = [
        c["claim"]
        for c in conclusion_builder({"evidence": [_plain_evidence("screener_results", rows)]})["conclusions"]
    ]
    assert claims == [
        "market: 12 stock(s) matched the screen: S0, S1, S2, S3, S4, S5, S6, S7, S8, S9 and 2 more"
    ]
    assert (
        conclusion_builder({"evidence": [_plain_evidence("screener_results", [])]})["conclusions"][0]["value"]
        == 0
    )
