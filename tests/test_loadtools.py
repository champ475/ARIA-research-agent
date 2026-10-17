"""TEST-06: the load tooling's own logic: fault injection, the report's verdicts,
the chaos timeline, and the Gradio queue client's parsing. The load run itself
(loadtests/run.py) needs Locust and a server, so it is run by hand or by the
loadtest workflow, not here."""

from __future__ import annotations

import json
import time

import pytest

import config
from loadtests import chaos, gradio_http, report
from utils import faults

# ── utils/faults.py ──────────────────────────────────────────────────────────


@pytest.fixture
def faults_file(tmp_path, monkeypatch):
    path = tmp_path / "faults.json"
    monkeypatch.setattr(config, "ARIA_FAULTS_FILE", str(path))
    faults.reset()
    yield path
    faults.reset()


def _set(path, **values):
    path.write_text(json.dumps(values), encoding="utf-8")
    faults.reset()


def test_no_file_means_no_faults(monkeypatch):
    monkeypatch.setattr(config, "ARIA_FAULTS_FILE", "")
    faults.reset()
    start = time.perf_counter()
    faults.llm_call()
    faults.data_call()
    faults.llm_first_token()
    assert time.perf_counter() - start < 0.05


def test_latency_is_injected(faults_file):
    _set(faults_file, llm_latency_ms=120, data_latency_ms=80, llm_first_token_ms=50)
    for fn, floor in ((faults.llm_call, 0.12), (faults.data_call, 0.08), (faults.llm_first_token, 0.05)):
        start = time.perf_counter()
        fn()
        assert time.perf_counter() - start >= floor * 0.9


def test_error_rates_one_always_fails_zero_never(faults_file):
    _set(faults_file, llm_error_rate=1.0, data_error_rate=1.0)
    with pytest.raises(RuntimeError, match="injected LLM"):
        faults.llm_call()
    with pytest.raises(ConnectionError, match="injected upstream"):
        faults.data_call()
    _set(faults_file, llm_error_rate=0.0, data_error_rate=0.0)
    faults.llm_call()
    faults.data_call()


def test_garbage_in_the_file_is_ignored(faults_file):
    faults_file.write_text("{not json", encoding="utf-8")
    faults.reset()
    faults.llm_call()
    _set(faults_file, llm_latency_ms="soon", data_error_rate=-3)
    faults.llm_call()
    faults.data_call()


def test_the_fakes_honour_the_faults(faults_file):
    from agents.providers.fake_llm import FakeLLMProvider
    from tools.data.providers.fake import FakeProvider

    _set(faults_file, llm_error_rate=1.0, data_error_rate=1.0)
    with pytest.raises(RuntimeError):
        FakeLLMProvider("", "planner").ask("Objective: x")
    with pytest.raises(ConnectionError):
        FakeProvider().quote("TCS")
    _set(faults_file)
    assert FakeProvider().quote("TCS")["currentPrice"] > 0


# ── loadtests/chaos.py ───────────────────────────────────────────────────────


def test_timeline_parsing_sorts_and_validates():
    assert chaos.parse_timeline("240:db_restart,60:slow_llm:45") == [
        (60.0, "slow_llm", 45.0),
        (240.0, "db_restart", 0.0),
    ]
    with pytest.raises(ValueError):
        chaos.parse_timeline("10:meteor_strike")
    with pytest.raises(ValueError):
        chaos.parse_timeline("slow_llm")


def test_chaos_run_sets_and_clears_the_faults_file(tmp_path, monkeypatch):
    seen = []
    ff = tmp_path / "f.json"
    real_sleep = time.sleep

    def sleep(s):
        seen.append(json.loads(ff.read_text(encoding="utf-8")))
        real_sleep(0)

    monkeypatch.setattr(chaos.time, "sleep", sleep)
    monkeypatch.setattr(chaos, "_ready", lambda url, timeout=120.0: 0.5)
    events = chaos.run([(0, "upstream_500", 5)], ff, "http://x", "", tmp_path / "log.json")
    assert {"data_error_rate": 0.5} in seen  # on while the scenario ran
    assert json.loads(ff.read_text(encoding="utf-8")) == {}  # and off afterwards
    assert [e["event"] for e in events] == ["start", "end"] and events[-1]["ready_after_s"] == 0.5
    assert json.loads((tmp_path / "log.json").read_text(encoding="utf-8")) == events


# ── loadtests/report.py ──────────────────────────────────────────────────────


def _stats(equity_ttft=(3000, 8000), failures=0, count=100, report_p95=20000, single=None):
    def row(p50, p95, n=count, f=0):
        return {"count": n, "failures": f, "p50": p50, "p95": p95}

    run = {
        ("TTFT", "equity"): row(*equity_ttft),
        ("TURN", "equity"): row(4000, 9000, f=failures),
        ("TURN", "report"): row(10000, report_p95, n=10),
        ("TTFT", "report"): row(5000, 9000, n=10),
    }
    base = {("TTFT", "equity"): row(*(single or (2500, 6000)), n=10)}
    return run, base


def _by_name(checks):
    return {c["name"]: c["ok"] for c in checks}


def test_a_clean_good_run_passes_every_target():
    run, base = _stats()
    checks = report.evaluate(run, base, None)
    assert all(c["ok"] for c in checks), checks


@pytest.mark.parametrize(
    "kwargs,failing",
    [
        ({"failures": 5}, "turn error rate < 1%"),
        ({"equity_ttft": (7000, 8000)}, "equity TTFT p50 <= 6 s"),
        ({"equity_ttft": (3000, 16000)}, "equity TTFT p95 <= 15 s"),
        ({"report_p95": 61000}, "report turn p95 <= 60 s"),
        (
            {"equity_ttft": (3000, 14000), "single": (2000, 5000)},
            "concurrent equity TTFT p95 within 1.5x single-user",
        ),
    ],
)
def test_each_missed_target_fails_the_run(kwargs, failing):
    run, base = _stats(**kwargs)
    verdicts = _by_name(report.evaluate(run, base, None))
    assert verdicts[failing] is False


def test_with_faults_injected_latency_is_shown_not_judged():
    run, base = _stats(equity_ttft=(9000, 30000))
    events = [
        {"at_s": 60, "scenario": "slow_llm", "event": "end", "ready_after_s": 0.2},
        {"at_s": 150, "scenario": "db_restart", "event": "restarted", "ready_after_s": 7.0},
    ]
    checks = report.evaluate(run, base, None, events)
    verdicts = _by_name(checks)
    assert verdicts["server ready again within 60 s after every injected fault"] is True
    assert not any(v is False for v in verdicts.values())
    assert not any("TTFT" in n and v is not None for n, v in verdicts.items())


def test_a_server_that_never_came_back_fails_the_chaos_run():
    run, base = _stats()
    events = [{"at_s": 150, "scenario": "db_restart", "event": "restarted", "ready_after_s": None}]
    assert (
        _by_name(report.evaluate(run, base, None, events))[
            "server ready again within 60 s after every injected fault"
        ]
        is False
    )


def test_memory_verdict_needs_a_long_enough_run_and_flat_means_flat():
    short = [(i * 30.0, 500.0) for i in range(20)]
    assert report.memory_trend(short)["assessed"] is False
    flat = [(i * 30.0, 500.0 + (i % 3)) for i in range(121)]  # one hour, jittering
    t = report.memory_trend(flat)
    assert t["assessed"] and t["ok"] and abs(t["mb_per_hour"]) < 5
    leak = [(i * 30.0, 500.0 + i * 2.0) for i in range(121)]  # +240 MB in the hour
    t = report.memory_trend(leak)
    assert t["assessed"] and t["ok"] is False and t["mb_per_hour"] > 200


def test_report_reads_locust_csv_and_writes_files(tmp_path):
    header = "Type,Name,Request Count,Failure Count,Median Response Time,Average Response Time,Min Response Time,Max Response Time,Average Content Size,Requests/s,Failures/s,50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%\n"

    def write(prefix, rows):
        lines = [
            f"{t},{n},{c},{f},{p50},{p50},0,0,0,0,0,{p50},{p50},{p50},{p50},{p95},{p95},{p95},{p95},{p95},{p95},{p95}"
            for t, n, c, f, p50, p95 in rows
        ]
        (tmp_path / f"{prefix}_stats.csv").write_text(
            header + "\n".join(lines + ["TURN,Aggregated,1,0,1,1"]) + "\n"
        )

    write("run", [("TTFT", "equity", 90, 0, 3000, 8000), ("TURN", "equity", 90, 0, 4000, 9000)])
    write("base", [("TTFT", "equity", 10, 0, 2500, 6000), ("TURN", "equity", 10, 0, 3000, 7000)])
    code = report.main(
        [
            "--run",
            str(tmp_path / "run"),
            "--baseline",
            str(tmp_path / "base"),
            "--version",
            "v9",
            "--out",
            str(tmp_path / "rep"),
        ]
    )
    assert code == 0
    assert "# ARIA load report: v9" in (tmp_path / "rep.md").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "rep.json").read_text(encoding="utf-8"))["version"] == "v9"


# ── loadtests/gradio_http.py ─────────────────────────────────────────────────


def test_answer_frames_are_told_from_loading_and_progress_frames():
    f = gradio_http._is_answer_frame
    assert not f(
        '{"msg":"process_generating","output":{"data":[[["role","assistant"],"content","<span class=\\"aria-dot\\">"]]}}'
    )
    assert not f(
        '{"msg":"process_generating","output":{"data":[["replace",[1,"content"],"<div class=\'aria-progress\'>…"]]}}'
    )
    assert f('{"msg":"process_generating","output":{"data":[["replace",[1,"content"],"TCS looks stable."]]}}')
    assert not f('{"msg":"estimation","rank":0}')


class _Resp:
    def __init__(self, lines=(), status=200):
        self._lines, self.status_code = list(lines), status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def json(self):
        return {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _session(monkeypatch, stream_lines, join_status=200):
    s = gradio_http.ChatSession.__new__(gradio_http.ChatSession)
    s.host, s.timeout = "http://x", 5
    s.session_hash, s.types, s.trigger_id = "h", ["state", "textbox", "chatbot", "radio"], 7
    s.dep = {"id": 3, "inputs": [1, 2, 3, 4]}

    class Http:
        def post(self, url, json=None, timeout=None):
            s.sent = json
            return _Resp(status=join_status)

        def get(self, url, params=None, stream=False, timeout=None):
            return _Resp(stream_lines)

    s.http = Http()
    return s


def test_ask_sends_state_as_null_and_times_the_first_answer_frame(monkeypatch):
    lines = [
        'data: {"msg":"estimation"}',
        'data: {"msg":"process_starts"}',
        'data: {"msg":"process_generating","output":{"data":["<span class=\\"aria-dot\\">"]}}',
        'data: {"msg":"process_generating","output":{"data":[["replace",[1,"content"],"Here is what the research shows"]]}}',
        'data: {"msg":"process_completed","success":true,"output":{"data":[]}}',
    ]
    s = _session(monkeypatch, lines)
    turn = s.ask("Analyse TCS")
    assert s.sent["data"] == [None, "Analyse TCS", [], "1D"] and s.sent["fn_index"] == 3
    assert turn.error is None and turn.ttft_s is not None and turn.ttft_s <= turn.total_s


@pytest.mark.parametrize(
    "lines,expect",
    [
        (['data: {"msg":"process_completed","success":true,"output":{"data":[]}}'], "no answer"),
        (
            [
                'data: {"msg":"process_generating","output":{"data":["Something went wrong (ref ab12)"]}}',
                'data: {"msg":"process_completed","success":true}',
            ],
            "error message",
        ),
        (['data: {"msg":"process_completed","success":false}'], "failed"),
        (['data: {"msg":"close_stream"}'], "closed early"),
    ],
)
def test_ask_reports_the_ways_a_turn_can_go_wrong(monkeypatch, lines, expect):
    turn = _session(monkeypatch, lines).ask("q")
    assert turn.error and expect in turn.error


def test_a_degraded_answer_is_a_success_that_is_counted(monkeypatch):
    lines = [
        'data: {"msg":"process_generating","output":{"data":[["replace",[1,"content"],'
        '"TCS price is 3500. Data not available this time: news for TCS (timed out)."]]}}',
        'data: {"msg":"process_completed","success":true}',
    ]
    turn = _session(monkeypatch, lines).ask("q")
    assert turn.error is None and turn.degraded is True


def test_provider_outage_text_counts_as_a_failed_turn(monkeypatch):
    lines = [
        'data: {"msg":"process_generating","output":{"data":["The shared AI service isn\'t available right now."]}}',
        'data: {"msg":"process_completed","success":true}',
    ]
    assert _session(monkeypatch, lines).ask("q").error


def test_a_refused_join_is_an_error_not_a_hang(monkeypatch):
    turn = _session(monkeypatch, [], join_status=500).ask("q")
    assert turn.error and "HTTP 500" in turn.error
