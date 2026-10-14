from unittest.mock import patch

from graph.nodes.human_review import research_confirmation, verification_gate


def _task(**overrides):
    base = {
        "id": "t1",
        "description": "x",
        "tool": "fetch_info",
        "entity": "RELIANCE",
        "depends_on": [],
        "status": "pending",
        "result_evidence_ids": [],
    }
    base.update(overrides)
    return base


class TestResearchConfirmation:
    def test_skips_confirmation_below_threshold(self):
        tasks = [_task(id=str(i)) for i in range(3)]
        with patch("graph.nodes.human_review.interrupt") as mock_interrupt:
            result = research_confirmation({"tasks": tasks, "complexity": "complex"})
        mock_interrupt.assert_not_called()
        assert "tasks" not in result

    def test_skips_confirmation_when_not_complex(self):
        tasks = [_task(id=str(i)) for i in range(10)]
        with patch("graph.nodes.human_review.interrupt") as mock_interrupt:
            result = research_confirmation({"tasks": tasks, "complexity": "moderate"})
        mock_interrupt.assert_not_called()

    def test_asks_when_complex_and_over_threshold(self):
        tasks = [_task(id=str(i)) for i in range(10)]
        with patch("graph.nodes.human_review.interrupt", return_value=True) as mock_interrupt:
            research_confirmation({"tasks": tasks, "complexity": "complex"})
        mock_interrupt.assert_called_once()

    def test_approved_leaves_tasks_untouched(self):
        tasks = [_task(id=str(i)) for i in range(10)]
        with patch("graph.nodes.human_review.interrupt", return_value=True):
            result = research_confirmation({"tasks": tasks, "complexity": "complex"})
        assert "tasks" not in result
        assert "final_report" not in result

    def test_declined_marks_pending_tasks_skipped_and_sets_report(self):
        tasks = [_task(id=str(i)) for i in range(10)]
        with patch("graph.nodes.human_review.interrupt", return_value=False):
            result = research_confirmation({"tasks": tasks, "complexity": "complex"})
        assert all(t["status"] == "skipped" for t in result["tasks"])
        assert "declined" in result["final_report"]

    def test_writes_journal_entry(self):
        with patch("graph.nodes.human_review.interrupt", return_value=True):
            result = research_confirmation(
                {"tasks": [_task(id=str(i)) for i in range(10)], "complexity": "complex"}
            )
        assert result["research_journal"][0]["node"] == "ResearchConfirmation"


class TestVerificationGate:
    def test_passes_through_when_no_issues(self):
        with patch("graph.nodes.human_review.interrupt") as mock_interrupt:
            result = verification_gate({"verification_issues": []})
        mock_interrupt.assert_not_called()
        assert "final_report" not in result

    def test_asks_when_issues_present(self):
        issues = [{"claim": "x", "issue_type": "unsupported", "evidence_ids": []}]
        with patch("graph.nodes.human_review.interrupt", return_value=True) as mock_interrupt:
            verification_gate({"verification_issues": issues})
        mock_interrupt.assert_called_once()

    def test_approved_leaves_state_untouched(self):
        issues = [{"claim": "x", "issue_type": "unsupported", "evidence_ids": []}]
        with patch("graph.nodes.human_review.interrupt", return_value=True):
            result = verification_gate({"verification_issues": issues})
        assert "final_report" not in result
        assert "conclusions" not in result

    def test_declined_withholds_report(self):
        issues = [{"claim": "x", "issue_type": "unsupported", "evidence_ids": []}]
        with patch("graph.nodes.human_review.interrupt", return_value=False):
            result = verification_gate({"verification_issues": issues})
        assert result["conclusions"] == []
        assert "withheld" in result["final_report"]

    def test_writes_journal_entry(self):
        with patch("graph.nodes.human_review.interrupt", return_value=True):
            result = verification_gate(
                {"verification_issues": [{"claim": "x", "issue_type": "unsupported", "evidence_ids": []}]}
            )
        assert result["research_journal"][0]["node"] == "VerificationGate"
