"""OPS-02 on a real Postgres: a download link works on any replica (two
separate app instances share only the database), only for its owner, and
never touches the serving replica's disk."""

from __future__ import annotations

import re
import sys
import types
from datetime import UTC, datetime, timedelta

import gradio as gr
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def _replica():
    from server import create_app

    with gr.Blocks() as demo:
        gr.Textbox()
    return TestClient(create_app(demo), base_url="http://testserver")


@pytest.fixture
def replicas(db, monkeypatch):
    from utils import auth

    auth.clear_session_cache()

    class _HTML:  # WeasyPrint without native libraries
        def __init__(self, **kw):
            pass

        def write_pdf(self, target=None):
            return b"%PDF-1.7 fake"

    monkeypatch.setitem(sys.modules, "weasyprint", types.SimpleNamespace(HTML=_HTML))
    a, b = _replica(), _replica()
    with a, b:
        yield a, b


def _login(client, email):
    def form(page, **data):
        html = client.get(page).text
        csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
        return client.post(page, data={**data, "csrf": csrf}, follow_redirects=False)

    form("/signup", email=email, password="download-pass-1", agree="yes")
    form("/login", email=email, password="download-pass-1")
    return client.cookies.get("aria_session")


def test_links_work_on_another_replica_and_only_for_the_owner(replicas, db, tmp_path, monkeypatch):
    a, b = replicas
    cookie = _login(a, "dl@example.com")
    uid = db.user_by_email("dl@example.com")["id"]
    report_id = db.report_save(uid, "TCS", "IC", "IC — TCS", "# TCS\n\nRevenue grew 10%.")
    token = db.download_put(uid, "aria_screener_results.csv", "text/csv", b"symbol,pe\r\nTCS,24.3\r\n")

    # Replica B never saw the turn that made these; it has the cookie and the DB.
    b.cookies.set("aria_session", cookie)
    monkeypatch.chdir(tmp_path)
    md = b.get(f"/download/report/{report_id}.md")
    assert md.status_code == 200 and b"Revenue grew 10%" in md.content
    assert 'filename="TCS_IC.md"' in md.headers["content-disposition"]
    pdf = b.get(f"/download/report/{report_id}.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    csv = b.get(f"/download/file/{token}")
    assert csv.status_code == 200 and csv.content.startswith(b"symbol,pe")
    assert csv.headers["cache-control"] == "private, no-store"
    assert list(tmp_path.iterdir()) == []  # nothing written on the serving replica

    # Someone else's session: same 404 as a missing file.
    other = TestClient(a.app, base_url="http://testserver")
    _login(other, "other@example.com")
    assert other.get(f"/download/report/{report_id}.md").status_code == 404
    assert other.get(f"/download/file/{token}").status_code == 404
    assert other.get("/download/report/999999.md").status_code == 404
    assert other.get(f"/download/report/{report_id}.exe").status_code == 404
    assert other.get("/download/file/..%2F..%2Fetc%2Fpasswd").status_code == 404

    anon = TestClient(a.app, base_url="http://testserver")
    assert anon.get(f"/download/file/{token}", follow_redirects=False).status_code == 303


def test_stored_files_expire_and_are_purged(db, two_users):
    a, _ = two_users
    token = db.download_put(a, "x.csv", "text/csv", b"x")
    assert db.download_get(a, token)["content"] == b"x"
    db._query(
        "UPDATE downloads SET expires_at=%s WHERE token=%s",
        (datetime.now(UTC) - timedelta(minutes=1), token),
        fetch=None,
        commit=True,
    )
    assert db.download_get(a, token) is None
    assert db.downloads_purge_expired() == 1
    with pytest.raises(ValueError):
        db.download_put(a, "big.csv", "text/csv", b"x" * (db.MAX_DOWNLOAD_BYTES + 1))
