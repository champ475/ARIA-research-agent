"""Tests for agents/conversation_memory.py — history windowing + overflow summarization."""
from agents.conversation_memory import ConversationMemory


def _fill(mem, n_turns):
    for i in range(n_turns):
        mem.add_user(f"user msg {i}")
        mem.add_assistant(f"assistant reply {i}")


class TestNoSummarizer:
    def test_under_cap_returns_full_history_no_summary(self):
        mem = ConversationMemory(cap=20)
        _fill(mem, 5)  # 10 messages
        ctx = mem.context()
        assert len(ctx) == 10
        assert ctx[0]["role"] == "user"

    def test_over_cap_without_summarizer_truncates_silently(self):
        mem = ConversationMemory(cap=20)
        _fill(mem, 30)  # 60 messages
        ctx = mem.context()
        assert len(ctx) == 20
        assert ctx[0]["role"] != "system"


class TestWithSummarizer:
    def test_overflow_triggers_summarizer_call(self):
        calls = []

        def fake_summarizer(old, chunk):
            calls.append((old, chunk))
            return "SUMMARY: " + chunk[:20]

        mem = ConversationMemory(cap=20, summarizer=fake_summarizer)
        _fill(mem, 15)  # 30 messages, 10 overflow
        ctx = mem.context()
        assert len(calls) == 1
        assert ctx[0]["role"] == "system"
        assert "SUMMARY" in ctx[0]["content"]
        assert len(ctx) == 21  # 1 summary message + 20 recent

    def test_summarizer_not_called_again_when_no_new_overflow(self):
        calls = []

        def fake_summarizer(old, chunk):
            calls.append(1)
            return "summary"

        mem = ConversationMemory(cap=20, summarizer=fake_summarizer)
        _fill(mem, 15)
        mem.context()
        mem.context()  # no new messages added — must not re-summarize
        assert len(calls) == 1

    def test_further_overflow_folds_incrementally(self):
        seen_chunks = []

        def fake_summarizer(old, chunk):
            seen_chunks.append(chunk)
            return (old + " | " if old else "") + "S"

        mem = ConversationMemory(cap=20, summarizer=fake_summarizer)
        _fill(mem, 15)   # 30 msgs -> 10 overflow, 1st summarization
        mem.context()
        _fill(mem, 5)    # +10 msgs -> 40 total, 20 overflow, 10 new to fold
        mem.context()
        assert len(seen_chunks) == 2
        # second call should only include the newly-overflowed chunk, not re-include the first
        assert "user msg 0" in seen_chunks[0]
        assert "user msg 0" not in seen_chunks[1]

    def test_failed_summarizer_keeps_overflow_for_retry(self):
        state = {"fail": True}

        def flaky_summarizer(old, chunk):
            if state["fail"]:
                raise RuntimeError("boom")
            return "recovered summary"

        mem = ConversationMemory(cap=20, summarizer=flaky_summarizer)
        _fill(mem, 15)
        ctx = mem.context()  # summarizer fails, degrades to plain truncation
        assert ctx[0]["role"] != "system"

        state["fail"] = False
        ctx2 = mem.context()  # retried successfully next call
        assert ctx2[0]["role"] == "system"
        assert "recovered summary" in ctx2[0]["content"]


class TestLoadAndClear:
    def test_load_resets_summary_state(self):
        mem = ConversationMemory(cap=20, summarizer=lambda o, c: "s")
        _fill(mem, 15)
        mem.context()
        mem.load([{"role": "user", "content": "hi"}])
        assert len(mem) == 1
        assert mem.context() == [{"role": "user", "content": "hi"}]

    def test_clear_resets_everything(self):
        mem = ConversationMemory(cap=20, summarizer=lambda o, c: "s")
        _fill(mem, 15)
        mem.context()
        mem.clear()
        assert len(mem) == 0
        assert mem.context() == []

    def test_pop_last_removes_most_recent_message(self):
        mem = ConversationMemory(cap=20)
        mem.add_user("hi")
        mem.pop_last()
        assert len(mem) == 0

    def test_pop_last_on_empty_is_noop(self):
        mem = ConversationMemory(cap=20)
        mem.pop_last()
        assert len(mem) == 0
