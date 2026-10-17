"""UX-09 on a real Postgres: rename, search by title and by content, paging and
Markdown export of chats, all scoped to the owner; search over 500 chats is fast."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from services import chat as svc
from utils.errors import UserInputError

pytestmark = pytest.mark.integration


def _chat(db, user, title, *messages):
    sid = db.session_create(user, "chat", title=title)
    for i, text in enumerate(messages):
        db.chat_save(user, sid, "chat", "user" if i % 2 == 0 else "assistant", text)
    return sid


def test_rename_validates_and_is_scoped_to_the_owner(db, two_users):
    a, b = two_users
    sid = _chat(db, a, "old", "hi")
    assert svc.rename_session(a, sid, "  Tata   Motors  review ") == "Tata Motors review"
    assert db.session_get_title(a, sid) == "Tata Motors review"
    for bad in ("", "   ", "x" * 81):
        with pytest.raises(UserInputError):
            svc.rename_session(a, sid, bad)
    with pytest.raises(LookupError):
        svc.rename_session(b, sid, "mine now")
    assert db.session_get_title(a, sid) == "Tata Motors review"


def test_search_finds_by_title_and_by_what_was_said(db, two_users):
    a, b = two_users
    s1 = _chat(db, a, "Banking sector", "How is HDFCBANK doing?", "HDFCBANK looks stable.")
    s2 = _chat(db, a, "Fund ideas", "Best ELSS for tax saving", "Here are ELSS funds.")
    _chat(db, b, "Banking sector too", "HDFCBANK please")
    ids = lambda text: [r["id"] for r in svc.search_sessions(a, "chat", text)]  # noqa: E731
    assert ids("banking") == [s1]  # title
    assert ids("elss") == [s2]  # content, case-insensitive
    assert ids("hdfc") == [s1]  # word prefix
    assert ids("ELSS tax") == [s2]  # every word must appear
    assert ids("elss hdfcbank") == []
    assert ids("'; drop table chat_history;--") == []  # only words count: nothing is ever interpreted
    assert set(ids("%")) == set(ids("_")) == {s1, s2}  # no words in it: the plain list, not a wildcard
    assert [r["id"] for r in svc.search_sessions(b, "chat", "elss")] == []  # never someone else's chats
    assert {r["id"] for r in svc.search_sessions(a, "chat", "   ")} == {s1, s2}  # no words: the plain list


def test_list_pages(db, two_users):
    a, _ = two_users
    sids = [_chat(db, a, f"chat {i}", "hello") for i in range(25)]
    first = svc.list_sessions(a, "chat", limit=10)
    second = svc.list_sessions(a, "chat", limit=10, offset=10)
    third = svc.list_sessions(a, "chat", limit=10, offset=20)
    assert [len(first), len(second), len(third)] == [10, 10, 5]
    assert {r["id"] for r in first + second + third} == set(sids)
    assert len(svc.list_sessions(a, "chat")) == svc.SESSION_PAGE  # the default page


def test_export_is_markdown_with_the_conversation_and_only_for_the_owner(db, two_users):
    a, b = two_users
    sid = _chat(db, a, "TCS: a look", "How is TCS?", "TCS is fine.\n\n| a | b |\n|---|---|")
    link = svc.export_chat(a, sid)
    assert link and link.startswith("/download/file/")
    row = db.download_get(a, link.rsplit("/", 1)[1])
    text = row["content"].decode("utf-8")
    assert row["filename"] == "aria-tcs-a-look.md" and row["content_type"] == "text/markdown"
    assert (
        text.startswith("# TCS: a look")
        and "**You**\n\nHow is TCS?" in text
        and "**ARIA**\n\nTCS is fine." in text
    )
    assert svc.export_chat(b, sid) is None  # not theirs
    assert db.download_get(b, link.rsplit("/", 1)[1]) is None
    assert svc.export_chat(a, db.session_create(a, "chat", title="empty")) is None


def test_markdown_header_has_an_ist_timestamp():
    md = svc.chat_markdown(
        "T", [{"role": "user", "content": "q"}], when=datetime(2026, 10, 17, 4, 30, tzinfo=UTC)
    )
    assert "17 Oct 2026, 10:00 IST" in md


def test_search_over_500_chats_is_fast(db, two_users):
    a, _ = two_users
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        for i in range(500):
            cur.execute(
                "INSERT INTO chat_sessions (user_id, tab, title) VALUES (%s,'chat',%s) RETURNING id",
                (a, f"session {i}"),
            )
            sid = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO chat_history (user_id, session_id, tab, role, content) VALUES "
                "(%s,%s,'chat','user',%s), (%s,%s,'chat','assistant',%s)",
                (
                    a,
                    sid,
                    f"Tell me about company{i} and ratio{i % 7}",
                    a,
                    sid,
                    f"Answer {i} about valuation and growth",
                ),
            )
        conn.commit()
        cur.execute("ANALYZE chat_history")
        conn.commit()
    finally:
        db.release_conn(conn)
    needle = _chat(db, a, "needle", "the rarest word is zanzibarquartz here")
    times = []
    for query in ("zanzibarquartz", "company123 ratio4", "company123"):
        start = time.perf_counter()
        found = svc.search_sessions(a, "chat", query)
        times.append(time.perf_counter() - start)
        assert found
    assert [r["id"] for r in svc.search_sessions(a, "chat", "zanzibarquartz")] == [needle]
    assert max(times) < 0.3, f"search took {max(times) * 1000:.0f} ms"
