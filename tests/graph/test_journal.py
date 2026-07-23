from graph.journal import log_decision


class TestLogDecision:
    def test_returns_research_journal_partial_update(self):
        update = log_decision("Planner", "skip ResearchExecutor", "complexity=simple, no tasks needed")
        assert list(update.keys()) == ["research_journal"]
        assert len(update["research_journal"]) == 1

    def test_entry_has_all_required_fields(self):
        update = log_decision("Planner", "decision-x", "reason-y")
        entry = update["research_journal"][0]
        assert entry["node"] == "Planner"
        assert entry["decision"] == "decision-x"
        assert entry["reason"] == "reason-y"
        assert entry["timestamp"] is not None

    def test_entries_from_multiple_nodes_accumulate_via_reducer(self):
        from graph.state import GraphState  # noqa: F401 — ensures reducer import path is valid
        import operator

        first = log_decision("QueryAnalyzer", "classified intent=equity", "keyword+entity match")
        second = log_decision("Planner", "generated 3 tasks", "objective needs fundamentals+technicals+news")
        merged = operator.add(first["research_journal"], second["research_journal"])
        assert [e["node"] for e in merged] == ["QueryAnalyzer", "Planner"]
