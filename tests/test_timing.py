import json

from src.timing import (
    SPAN_ACK,
    SPAN_FIRST_EVENT,
    SPAN_ORDER,
    SPAN_RESULT,
    SPAN_SPAWN,
    SPAN_TOTAL,
    TIMINGS_FILENAME,
    JobTimings,
    debug_timing_enabled,
)


class FakeClock:
    """Deterministic monotonic stand-in so span assertions are exact."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_start_stop_records_elapsed_milliseconds():
    clock = FakeClock()
    timings = JobTimings("job-1", clock=clock)

    timings.start(SPAN_ACK)
    clock.advance(0.25)
    recorded = timings.stop(SPAN_ACK)

    assert recorded == 250.0
    assert timings.spans_ms[SPAN_ACK] == 250.0


def test_stop_without_start_returns_none_and_records_nothing():
    timings = JobTimings()
    assert timings.stop(SPAN_ACK) is None
    assert SPAN_ACK not in timings.spans_ms


def test_timer_context_manager_records_even_when_body_raises():
    clock = FakeClock()
    timings = JobTimings(clock=clock)

    try:
        with timings.timer(SPAN_RESULT):
            clock.advance(1.5)
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert timings.spans_ms[SPAN_RESULT] == 1500.0


def test_record_ms_clamps_negatives_and_ignores_garbage():
    timings = JobTimings()
    timings.record_ms(SPAN_SPAWN, -5)
    timings.record_ms(SPAN_TOTAL, "nope")

    assert timings.spans_ms[SPAN_SPAWN] == 0.0
    assert SPAN_TOTAL not in timings.spans_ms


def test_since_origin_ms_measures_from_construction():
    clock = FakeClock()
    timings = JobTimings(clock=clock)
    clock.advance(2.0)
    assert timings.since_origin_ms() == 2000.0


def test_absorb_result_event_derives_cli_startup_overhead():
    timings = JobTimings("job-2")
    timings.record_ms(SPAN_SPAWN, 20)
    timings.record_ms(SPAN_FIRST_EVENT, 1400)
    timings.record_ms(SPAN_RESULT, 1600)

    timings.absorb_result_event(
        {"type": "result", "duration_ms": 1583, "duration_api_ms": 1200, "num_turns": 2}
    )

    assert timings.meta["cli_wall_ms"] == 3020.0
    # 3020 CLI wall - 1583 self-reported = the ~1.4s startup cost issue #7 quotes.
    assert timings.meta["cli_startup_overhead_ms"] == 1437.0
    assert timings.meta["num_turns"] == 2


def test_absorb_result_event_ignores_non_dict_and_bool_values():
    timings = JobTimings()
    timings.record_ms(SPAN_SPAWN, 10)
    timings.absorb_result_event("not an event")
    timings.absorb_result_event({"duration_ms": True})

    assert "duration_ms" not in timings.meta


def test_set_meta_skips_none_values():
    timings = JobTimings()
    timings.set_meta(warm=False, model=None)

    assert timings.meta == {"warm": False}


def test_snapshot_orders_canonical_spans_and_keeps_extras():
    timings = JobTimings("job-3")
    timings.record_ms(SPAN_TOTAL, 4000)
    timings.record_ms(SPAN_ACK, 100)
    timings.record_ms("t_custom", 5)

    snapshot = timings.snapshot()
    keys = list(snapshot["spans_ms"])

    assert keys == [SPAN_ACK, SPAN_TOTAL, "t_custom"]
    assert keys[:2] == [name for name in SPAN_ORDER if name in snapshot["spans_ms"]]
    assert snapshot["job_id"] == "job-3"


def test_format_line_includes_spans_overhead_and_warm_flag():
    timings = JobTimings()
    timings.record_ms(SPAN_SPAWN, 20)
    timings.record_ms(SPAN_FIRST_EVENT, 1400)
    timings.record_ms(SPAN_RESULT, 1600)
    timings.set_meta(warm=True)
    timings.absorb_result_event({"duration_ms": 1583})

    line = timings.format_line()

    assert "t_spawn 20ms" in line
    assert "cli_startup 1437ms" in line
    assert line.endswith("warm")


def test_format_line_without_any_spans():
    assert JobTimings().format_line() == "계측 없음"


def test_write_persists_snapshot_next_to_the_job(tmp_path):
    timings = JobTimings("job-4")
    timings.record_ms(SPAN_ACK, 120)

    path = timings.write(tmp_path)

    assert path == tmp_path / TIMINGS_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["job_id"] == "job-4"
    assert payload["spans_ms"][SPAN_ACK] == 120.0


def test_write_returns_none_when_directory_is_missing(tmp_path):
    assert JobTimings().write(tmp_path / "nope") is None


def test_debug_timing_enabled_reads_truthy_values():
    assert debug_timing_enabled({"DEBUG_TIMING": "1"}) is True
    assert debug_timing_enabled({"DEBUG_TIMING": "TRUE"}) is True
    assert debug_timing_enabled({"DEBUG_TIMING": "0"}) is False
    assert debug_timing_enabled({}) is False


# Retry accounting: main.py re-runs the CLI for one Discord message when a
# resumed session has gone missing, so issue #7's numbers must not silently
# report only the second run.


def test_start_attempt_archives_cli_spans_and_keeps_message_spans():
    timings = JobTimings("job-5")
    timings.record_ms(SPAN_ACK, 120)
    timings.record_ms(SPAN_SPAWN, 20)
    timings.record_ms(SPAN_FIRST_EVENT, 1400)
    timings.record_ms(SPAN_RESULT, 900)
    timings.set_meta(warm=True)
    timings.absorb_result_event({"duration_ms": 800, "num_turns": 1})

    timings.start_attempt(reason="stale_session_retry")

    # The abandoned attempt's numbers moved aside...
    assert SPAN_SPAWN not in timings.spans_ms
    assert SPAN_RESULT not in timings.spans_ms
    assert "duration_ms" not in timings.meta
    assert "warm" not in timings.meta
    # ...but the spans that belong to the message, not the CLI run, stayed.
    assert timings.spans_ms[SPAN_ACK] == 120.0

    archived = timings.attempts[0]
    assert archived["spans_ms"][SPAN_FIRST_EVENT] == 1400.0
    assert archived["meta"]["duration_ms"] == 800
    assert archived["meta"]["warm"] is True
    assert archived["reason"] == "stale_session_retry"


def test_start_attempt_is_a_noop_before_the_first_run():
    timings = JobTimings()
    timings.record_ms(SPAN_ACK, 50)

    timings.start_attempt()

    assert timings.attempts == []
    assert timings.spans_ms[SPAN_ACK] == 50.0


def test_second_attempt_records_its_own_numbers_independently():
    timings = JobTimings("job-6")
    timings.record_ms(SPAN_SPAWN, 20)
    timings.record_ms(SPAN_FIRST_EVENT, 1500)
    timings.start_attempt(reason="stale_session_retry")

    timings.record_ms(SPAN_SPAWN, 5)
    timings.record_ms(SPAN_FIRST_EVENT, 8)
    timings.set_meta(warm=False)

    snapshot = timings.snapshot()

    assert snapshot["spans_ms"][SPAN_FIRST_EVENT] == 8.0
    assert snapshot["attempts"][0]["spans_ms"][SPAN_FIRST_EVENT] == 1500.0
    assert snapshot["meta"]["attempts"] == 2


def test_snapshot_omits_attempts_key_when_there_was_no_retry():
    timings = JobTimings()
    timings.record_ms(SPAN_SPAWN, 10)

    snapshot = timings.snapshot()

    assert "attempts" not in snapshot
    assert "attempts" not in snapshot["meta"]


def test_format_line_flags_a_retried_job():
    timings = JobTimings()
    timings.record_ms(SPAN_SPAWN, 20)
    timings.start_attempt()
    timings.record_ms(SPAN_SPAWN, 5)

    assert "시도 2회" in timings.format_line()


def test_write_persists_archived_attempts(tmp_path):
    timings = JobTimings("job-7")
    timings.record_ms(SPAN_RESULT, 2000)
    timings.start_attempt(reason="stale_session_retry")
    timings.record_ms(SPAN_RESULT, 1000)
    timings.record_ms(SPAN_TOTAL, 3500)

    path = timings.write(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    # t_total (3500) exceeds the live attempt's spans (1000); the archived
    # attempt is what accounts for the difference.
    assert payload["spans_ms"][SPAN_TOTAL] == 3500.0
    assert payload["attempts"][0]["spans_ms"][SPAN_RESULT] == 2000.0


def test_cli_startup_is_omitted_when_a_wall_span_is_missing():
    # A harness that records spawn+first_event but never sees a result event
    # must not be told the CLI started up in negative time.
    timings = JobTimings()
    timings.record_ms(SPAN_SPAWN, 20)
    timings.record_ms(SPAN_FIRST_EVENT, 6)
    timings.absorb_result_event({"duration_ms": 1400})

    meta = timings.snapshot()["meta"]

    assert "cli_startup_overhead_ms" not in meta
    assert meta["cli_wall_ms"] == 26.0
    assert "cli_startup" not in timings.format_line()


def test_cli_startup_appears_once_the_missing_span_arrives():
    timings = JobTimings()
    timings.record_ms(SPAN_SPAWN, 20)
    timings.record_ms(SPAN_FIRST_EVENT, 6)
    timings.absorb_result_event({"duration_ms": 1400})
    assert "cli_startup_overhead_ms" not in timings.snapshot()["meta"]

    timings.record_ms(SPAN_RESULT, 1500)

    assert timings.snapshot()["meta"]["cli_startup_overhead_ms"] == 126.0
