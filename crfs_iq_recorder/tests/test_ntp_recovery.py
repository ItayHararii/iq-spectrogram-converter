"""NTP refresh on a wrong sensor clock (ntp_recovery), against a simulated Node. No network."""

from __future__ import annotations

import base64
import copy
import json
import threading
from datetime import datetime, timezone

import pytest

from crfs_iq_recorder import ntp_recovery as nr
from crfs_iq_recorder.sensor_clock import PcClock

# GET /api/values/network/ntp.json from the Node 100-18 (host 2.25-4823), 9 Nov 2026.
NTP_PAGE = {
    "pageErrors": [{"message": "Release not covered by Software Support and Update Service", "type": "warning"}],
    "values": {"00-source": "Manual", "01-selected-server": "198.51.100.250", "02-preferred-servers": ["1.il.pool.ntp.org", "1.il.pool.ntp.org"]},
    "widgets": [
        {"editable": False, "id": "00-source", "name": "Source", "required": False, "type": "string"},
        {"editable": False, "id": "01-selected-server", "name": "Selected Server", "required": False, "type": "string"},
        {"id": "02-preferred-servers", "multiple": True, "name": "Preferred Servers", "required": False, "type": "string"},
    ],
}
SERVERS = "02-preferred-servers"
ORIGINAL = ["1.il.pool.ntp.org", "1.il.pool.ntp.org"]
TEMPORARY = [*ORIGINAL, "1.il.pool.ntp.org"]
EXACT_BODY = b'{"00-source":"Manual","01-selected-server":"198.51.100.250","02-preferred-servers":["1.il.pool.ntp.org","1.il.pool.ntp.org"]}'
TEMP_BODY = b'{"00-source":"Manual","01-selected-server":"198.51.100.250","02-preferred-servers":["1.il.pool.ntp.org","1.il.pool.ntp.org","1.il.pool.ntp.org"]}'
KEY = "192.0.2.33"
START = datetime(2026, 11, 9, 9, 29, 0, tzinfo=timezone.utc).timestamp()
TRUSTED = PcClock(True, "SNTP 1.il.pool.ntp.org", 0.05, "test")


class FakeTime:
    def __init__(self) -> None:
        self.t = START

    def now(self) -> float:
        return self.t

    def wait(self, seconds: float) -> bool:
        self.t += seconds
        return False


class FakeNode:
    """Status page, NTP page and the page's Submit. Like the real Node, a Save restarts chrony (which
    steps the clock ``fixes_after_s`` later) only when it changes the server list; the same list does nothing."""

    origin = "http://192.0.2.33"

    def __init__(self, clock: FakeTime, offset_s: float, *, page=None, time_source=None, puts=(), fixes_after_s: float | None = 6.0,
                 on_put=None) -> None:
        self.clock = clock
        self.offset_s = offset_s
        self.page = copy.deepcopy(page if page is not None else NTP_PAGE)
        self.time_source = time_source
        self.puts = list(puts)  # one (status, error, applied) per PUT; then 200 and applied
        self.fixes_after_s = fixes_after_s
        self.fixed_at: float | None = None
        self.page_readable = True
        self.on_put = on_put
        self.calls: list[tuple] = []

    def servers(self):
        return self.page["values"].get(SERVERS)

    def _sensor_offset(self) -> float:
        if self.fixed_at is not None and self.clock.t >= self.fixed_at:
            return 0.3
        return self.offset_s

    def get(self, path: str) -> nr.WebReply:
        self.calls.append(("GET", path))
        if path == "/api/values/status.json":
            offset = self._sensor_offset()
            stamp = datetime.fromtimestamp(self.clock.t + offset, tz=timezone.utc)
            # The real Node reports "No accurate source" as soon as its clock is stepped away from NTP time.
            source = self.time_source or ("NTP" if abs(offset) < 5 else "No accurate source")
            values = {"02-system-time": stamp.strftime("%Y-%m-%d %H:%M:%S+0000"), "03-time-source": source}
            return nr.WebReply(200, {}, json.dumps({"values": values}))
        if path == nr.NTP_PAGE_PATH:
            if not self.page_readable:
                return nr.WebReply(error="Timed out (the sensor did not answer).")
            return nr.WebReply(200, {}, json.dumps(self.page))
        return nr.WebReply(404, {}, "")

    def put_json(self, path: str, body: bytes, *, page: str) -> nr.WebReply:
        self.calls.append(("PUT", path, body, page))
        if self.on_put is not None:
            self.on_put(self, body)
        status, error, applied = self.puts.pop(0) if self.puts else (200, "", True)
        if applied:
            sent = json.loads(body)[SERVERS]
            if sent != self.servers():
                self.page["values"][SERVERS] = sent
                stepped = self.fixed_at is not None and self.clock.t >= self.fixed_at
                if self.fixes_after_s is not None and not stepped:
                    self.fixed_at = self.clock.t + self.fixes_after_s
        if error:
            return nr.WebReply(error=error)
        return nr.WebReply(status, {}, json.dumps(self.page) if 200 <= status < 300 else "")

    def saves(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "PUT"]


def _run(node: FakeNode, clock: FakeTime, store: nr.RecoveryStore, *, pc=TRUSTED, notes=None, wait=None, pair_idle=None):
    return nr.recover_clock(
        node, store, KEY, reason="test", pc_check=lambda _servers: pc,
        now=clock.now, monotonic=clock.now, wait=wait or clock.wait,
        notify=(notes.append if notes is not None else None), pair_idle=pair_idle,
    )


@pytest.fixture
def store(tmp_path):
    return nr.RecoveryStore(tmp_path / "sensor_clock_recovery.json")


def test_the_save_body_is_the_page_values_exactly_as_its_submit_sends_them():
    page = nr.parse_ntp_page(json.dumps(NTP_PAGE))
    assert page is not None
    assert page.save_body() == EXACT_BODY  # widget order, read-only values included, duplicates kept, compact
    assert page.editable_ids() == [SERVERS] and page.server_list_id() == SERVERS
    assert page.configured_servers() == ORIGINAL
    assert page.all_servers() == ["1.il.pool.ntp.org", "198.51.100.250"]
    assert page.body_with(SERVERS, TEMPORARY) == TEMP_BODY  # only the list differs
    # A value without a widget is not part of the page's state, so it is not sent.
    extra = dict(NTP_PAGE, values={**NTP_PAGE["values"], "99-hidden": 1})
    assert nr.parse_ntp_page(json.dumps(extra)).save_body() == EXACT_BODY
    assert nr.parse_ntp_page("not json") is None and nr.parse_ntp_page(json.dumps({"values": {}})) is None


@pytest.mark.parametrize(
    "original, temporary",
    [
        (["1.il.pool.ntp.org", "1.il.pool.ntp.org"], ["1.il.pool.ntp.org", "1.il.pool.ntp.org", "1.il.pool.ntp.org"]),
        (["1.il.pool.ntp.org"], ["1.il.pool.ntp.org", "1.il.pool.ntp.org"]),
        (["a.example", "b.example", "c.example"], ["a.example", "b.example", "c.example", "a.example"]),
        (["", "b.example"], ["", "b.example", "b.example"]),
        ([], None),
        (["  "], None),
    ],
)
def test_the_temporary_list_is_the_original_plus_one_more_copy_of_its_first_server(original, temporary):
    assert nr.temporary_servers(original) == temporary
    if temporary is not None:
        assert temporary[: len(original)] == original and set(temporary) == set(original)


def test_the_put_on_the_wire_matches_the_browser_request():
    import requests

    sent: list[requests.PreparedRequest] = []

    class Session:
        def request(self, method, url, *, headers, data, auth, timeout, allow_redirects):
            sent.append(requests.Request(method, url, headers=headers, data=data, auth=auth).prepare())
            sent[-1].timeout = timeout
            response = requests.Response()
            response.status_code = 200
            response._content = b""
            return response

        def close(self):
            pass

    web = nr.SensorWeb("http://192.0.2.33", "admin", "s3cret-test")
    web._session = Session()
    reply = web.put_json(nr.NTP_PAGE_PATH, EXACT_BODY, page=nr.NTP_PAGE)
    assert reply.status == 200 and not reply.error
    request = sent[0]
    assert request.method == "PUT" and request.url == "http://192.0.2.33/api/values/network/ntp.json"
    assert request.body == EXACT_BODY
    assert request.headers["Content-Type"] == "application/json; charset=utf-8"
    assert request.headers["Accept"] == "*/*"
    assert request.headers["Origin"] == "http://192.0.2.33" and request.headers["Referer"] == "http://192.0.2.33/network/ntp"
    assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"admin:s3cret-test").decode()
    assert request.timeout == nr.SAVE_TIMEOUT_S
    web.get("/api/values/status.json")
    assert sent[1].method == "GET" and sent[1].body is None and sent[1].headers["Accept"] == "*/*"


def test_a_correct_clock_is_never_saved(store):
    clock = FakeTime()
    node = FakeNode(clock, 0.4)
    result = _run(node, clock, store)
    assert result.outcome == nr.OK and not node.saves() and not result.saved
    assert node.calls == [("GET", "/api/values/status.json")]
    assert nr.result_message(result) is None


def test_a_wrong_clock_is_refreshed_by_a_change_and_restore_pair_and_verified(store):
    clock = FakeTime()
    node = FakeNode(clock, -600.0, fixes_after_s=6.0)
    notes: list[str] = []
    result = _run(node, clock, store, notes=notes)
    assert result.outcome == nr.SYNCED, result.to_dict()
    assert notes == [nr.REFRESHING_TEXT]
    saves = node.saves()
    assert [s[2] for s in saves] == [TEMP_BODY, EXACT_BODY]
    assert all(s[1] == nr.NTP_PAGE_PATH and s[3] == "/network/ntp" for s in saves)
    # The settings end exactly as read, verified on the page before anything else.
    assert node.servers() == ORIGINAL and result.restored is True
    assert [s["kind"] for s in result.saves] == ["temporary", "restore"]
    assert result.read_backs[-1]["servers"] == ORIGINAL and result.read_backs[-1]["matches_original"] is True
    assert result.original_servers == ORIGINAL and result.temporary_servers == TEMPORARY
    # Verified by two good readings after the restore, not by an HTTP answer.
    good = [p for p in result.polls if p["verdict"] == "correct"]
    assert len(good) == 2 and result.after.offset_s == pytest.approx(0.3, abs=1.0)
    assert result.timing["first_correct_after_restore_save_s"] == pytest.approx(6.0)
    assert nr.result_message(result) == ("Sensor clock synchronized.", "info")
    assert store.entry(KEY) == {} and store.pending_restore(KEY) == {}  # the episode is over
    assert all("Authorization" not in s["headers"] for s in result.saves)
    assert result.to_dict()["saves"][0]["body"] == TEMP_BODY.decode()


def test_a_pair_that_does_not_fix_the_clock_says_so_and_never_claims_success(store):
    clock = FakeTime()
    node = FakeNode(clock, -14 * 365 * 86400.0, fixes_after_s=None)
    started = clock.t
    result = _run(node, clock, store)
    assert result.outcome == nr.STILL_WRONG and len(node.saves()) == 2 and node.servers() == ORIGINAL
    assert result.restored is True
    assert clock.t - started <= nr.SYNC_TIMEOUT_S + nr.SYNC_POLL_S  # bounded
    text, level = nr.result_message(result)
    assert level == "error" and "synchronized" not in text
    assert text.startswith("Sensor clock still wrong after NTP refresh (reads 20") and "misdated" in text
    # The next refresh waits RETRY_GAP_S.
    allowed, at = store.may_save(KEY, clock.t)
    assert not allowed and at == pytest.approx(store.entry(KEY)["last_save"] + nr.RETRY_GAP_S)


@pytest.mark.parametrize(
    "status, error",
    [(400, ""), (500, ""), (0, "Connection refused (the sensor did not accept the connection).")],
)
def test_a_refused_temporary_save_changes_nothing_and_needs_no_restore(store, status, error):
    clock = FakeTime()
    node = FakeNode(clock, -600.0, puts=[(status, error, False)])
    result = _run(node, clock, store)
    assert result.outcome == nr.SAVE_FAILED and [s[2] for s in node.saves()] == [TEMP_BODY]
    assert node.servers() == ORIGINAL and result.restored is None
    assert result.read_backs[-1]["after"] == "temporary refused" and result.read_backs[-1]["matches_original"] is True
    assert nr.result_message(result) == ("Could not refresh NTP on the sensor. See details.", "error")
    assert store.entry(KEY)["saves"] == 1 and store.pending_restore(KEY) == {}
    assert result.after is None and not result.polls[1:]


def test_a_refused_temporary_save_that_changed_the_list_anyway_is_put_back(store):
    clock = FakeTime()
    node = FakeNode(clock, -600.0, puts=[(0, "Read timed out.", True)])
    result = _run(node, clock, store)
    assert result.outcome == nr.SAVE_FAILED and [s[2] for s in node.saves()] == [TEMP_BODY, EXACT_BODY]
    assert node.servers() == ORIGINAL and result.restored is True and store.pending_restore(KEY) == {}


def test_a_failed_restore_is_retried_until_the_list_reads_back(store):
    clock = FakeTime()
    node = FakeNode(clock, -600.0, puts=[(200, "", True), (500, "", False), (0, "Connection reset.", False)])
    result = _run(node, clock, store)
    assert result.outcome == nr.SYNCED and result.restored is True and node.servers() == ORIGINAL
    assert [s["kind"] for s in result.saves] == ["temporary", "restore", "restore retry 1", "restore retry 2"]
    times = [s["t"] for s in result.saves]
    assert times[2] - times[1] == pytest.approx(nr.RESTORE_RETRY_DELAYS_S[0]) and times[3] - times[2] == pytest.approx(nr.RESTORE_RETRY_DELAYS_S[1])
    assert store.entry(KEY) == {} and store.pending_restore(KEY) == {}


def test_a_restore_that_never_reads_back_is_reported_and_finished_at_the_next_start(tmp_path):
    path = tmp_path / "sensor_clock_recovery.json"
    clock = FakeTime()
    # Each restore is answered 200 but the page keeps the temporary list (the read-back differs).
    node = FakeNode(clock, -600.0, puts=[(200, "", True)] + [(200, "", False)] * 4)
    result = _run(node, clock, nr.RecoveryStore(path))
    assert result.outcome == nr.RESTORE_FAILED and len(node.saves()) == 5 and node.servers() == TEMPORARY
    assert result.restored is False and not any(r["matches_original"] for r in result.read_backs)
    # chrony stepped the clock after the temporary list, yet nothing claims success while the list differs.
    text, level = nr.result_message(result)
    assert (text, level) == ("Sensor NTP server list not restored. See details.", "error")
    assert json.dumps(ORIGINAL) in result.detail and "synchronized" not in text
    assert len(result.polls) == 1  # no clock verdict after an unverified restore
    pending = nr.RecoveryStore(path).pending_restore(KEY)
    assert pending["original"] == ORIGINAL and pending["temporary"] == TEMPORARY and pending["field"] == SERVERS
    # The next start (a new store on the same file) puts the list back before anything else.
    node.calls.clear()
    again = _run(node, clock, nr.RecoveryStore(path))
    assert node.servers() == ORIGINAL and [c[2] for c in node.saves()] == [EXACT_BODY]
    assert again.pending["action"] == "restored" and again.outcome == nr.OK
    assert nr.restore_message(again) == ("Sensor NTP server list restored.", "info")
    assert nr.RecoveryStore(path).pending_restore(KEY) == {}


@pytest.mark.parametrize(
    "found, action",
    [(["pool.ntp.org"], "the page shows another list; nothing written"), (ORIGINAL, "already the original list")],
)
def test_a_pending_restore_never_writes_over_a_list_it_did_not_leave(store, found, action):
    clock = FakeTime()
    store.note_pending_restore(KEY, field_id=SERVERS, original=ORIGINAL, temporary=TEMPORARY, now=clock.t)
    node = FakeNode(clock, 0.2, page={**NTP_PAGE, "values": {**NTP_PAGE["values"], SERVERS: found}})
    result = _run(node, clock, store)
    assert not node.saves() and node.servers() == found
    assert result.pending["action"] == action and result.pending["found"] == found
    assert store.pending_restore(KEY) == {} and result.outcome == nr.OK and nr.restore_message(result) is None


def test_a_pending_restore_waits_while_the_page_cannot_be_read(store):
    clock = FakeTime()
    store.note_pending_restore(KEY, field_id=SERVERS, original=ORIGINAL, temporary=TEMPORARY, now=clock.t)
    node = FakeNode(clock, -600.0)
    node.page_readable = False
    result = _run(node, clock, store)
    assert result.outcome == nr.PAGE_UNREADABLE and not node.saves() and store.pending_restore(KEY)["original"] == ORIGINAL


def test_the_restore_record_is_written_before_the_first_save_and_the_pair_is_marked_busy(store):
    clock = FakeTime()
    pair_idle = threading.Event()
    seen: list[tuple] = []
    node = FakeNode(clock, -600.0, on_put=lambda _n, body: seen.append((body, store.pending_restore(KEY), pair_idle.is_set())))
    result = _run(node, clock, store, pair_idle=pair_idle)
    assert result.outcome == nr.SYNCED
    assert seen[0][0] == TEMP_BODY and seen[0][1]["original"] == ORIGINAL and seen[0][1]["temporary"] == TEMPORARY
    assert [idle for _b, _p, idle in seen] == [False, False] and pair_idle.is_set()


def test_nothing_is_sent_when_the_restore_record_cannot_be_written(store, monkeypatch):
    clock = FakeTime()
    node = FakeNode(clock, -600.0)
    monkeypatch.setattr(store, "note_pending_restore", lambda *_a, **_k: False)
    result = _run(node, clock, store)
    assert result.outcome == nr.SAVE_FAILED and not node.saves() and "nothing was sent" in result.detail


def test_a_stop_during_the_refresh_still_tries_the_restore_once(tmp_path):
    path = tmp_path / "sensor_clock_recovery.json"
    clock = FakeTime()
    stop = threading.Event()

    def wait(seconds):
        return True if stop.is_set() else clock.wait(seconds)

    node = FakeNode(clock, -600.0, puts=[(200, "", True), (500, "", False)], on_put=lambda _n, _b: stop.set())
    result = _run(node, clock, nr.RecoveryStore(path), wait=wait)
    assert result.outcome == nr.CANCELLED and [s["kind"] for s in result.saves] == ["temporary", "restore"]
    assert result.restored is False and nr.result_message(result) is None
    assert nr.RecoveryStore(path).pending_restore(KEY)["temporary"] == TEMPORARY  # finished at the next start
    node.on_put = None
    again = _run(node, clock, nr.RecoveryStore(path))
    assert again.pending["action"] == "restored" and node.servers() == ORIGINAL


def test_a_stop_before_the_pair_sends_nothing_and_a_stop_after_it_keeps_the_restored_list(store):
    clock = FakeTime()
    node = FakeNode(clock, -600.0)
    result = _run(node, clock, store, wait=lambda _s: True)
    assert result.outcome == nr.CANCELLED and not node.saves() and nr.result_message(result) is None
    polling = _run(node, clock, store, wait=lambda s: s == nr.SYNC_POLL_S)
    assert polling.outcome == nr.CANCELLED and len(node.saves()) == 2 and node.servers() == ORIGINAL
    assert polling.restored is True and store.pending_restore(KEY) == {}


def test_ntp_that_is_off_is_never_switched_on_and_a_page_without_a_server_list_is_not_touched(store):
    page = {**NTP_PAGE, "values": {**NTP_PAGE["values"], SERVERS: "1.il.pool.ntp.org"},
            "widgets": [*NTP_PAGE["widgets"][:2], {"id": SERVERS, "name": "Preferred Server", "type": "string"}]}
    clock = FakeTime()
    node = FakeNode(clock, -600.0, page=page)
    result = _run(node, clock, store)
    assert result.outcome == nr.PAGE_UNREADABLE and not node.saves() and "list of preferred servers" in result.detail


@pytest.mark.parametrize(
    "page, time_source",
    [
        ({**NTP_PAGE, "values": {**NTP_PAGE["values"], "02-preferred-servers": []}}, "NTP"),
        (NTP_PAGE, "GPS"),
        ({**NTP_PAGE, "values": {**NTP_PAGE["values"], "00-source": "Disabled"}}, "NTP"),
        ({"values": {**NTP_PAGE["values"], "03-enabled": False},
          "widgets": [*NTP_PAGE["widgets"], {"id": "03-enabled", "type": "boolean", "name": "Enabled"}]}, "NTP"),
    ],
)
def test_ntp_that_is_off_is_never_switched_on(store, page, time_source):
    clock = FakeTime()
    node = FakeNode(clock, -600.0, page=page, time_source=time_source)
    result = _run(node, clock, store)
    assert result.outcome == nr.NTP_IS_OFF and not node.saves(), result.ntp
    assert nr.result_message(result) == ("Sensor clock incorrect - NTP is off on the sensor.", "error")


def test_an_unsynchronised_time_source_is_not_mistaken_for_ntp_off(store):
    page = nr.parse_ntp_page(json.dumps(NTP_PAGE))
    for source in ("NTP", "No accurate source", ""):
        assert nr.ntp_state(page, source)[0] == nr.NTP_ON, source
    for source in ("GPS", "GNSS", "PTP"):
        assert nr.ntp_state(page, source)[0] == nr.NTP_OFF, source
    # Seen on the real Node right after its clock was set back 600 s.
    clock = FakeTime()
    node = FakeNode(clock, -600.0, time_source="No accurate source")
    result = _run(node, clock, store)
    assert result.before.time_source == "No accurate source" and len(node.saves()) == 2


def test_an_unverified_computer_clock_leaves_the_sensor_alone(store):
    clock = FakeTime()
    node = FakeNode(clock, -600.0)
    result = _run(node, clock, store, pc=PcClock(False, "Windows time service", detail="no network time source (Local CMOS Clock)"))
    assert result.outcome == nr.PC_UNTRUSTED and not node.saves()
    assert "not verified" in nr.result_message(result)[0]


def test_the_clock_is_measured_again_right_before_the_save(store):
    clock = FakeTime()
    node = FakeNode(clock, -600.0)

    def pc_check(_servers):
        node.offset_s = 0.2  # chrony stepped the clock meanwhile
        return TRUSTED

    result = nr.recover_clock(node, store, "k", pc_check=pc_check, now=clock.now, monotonic=clock.now, wait=clock.wait)
    assert result.outcome == nr.OK and not node.saves()


def test_a_pair_counts_as_one_refresh_for_the_retry_limit_and_cooldown_across_restarts(tmp_path):
    path = tmp_path / "sensor_clock_recovery.json"
    clock = FakeTime()
    node = FakeNode(clock, -600.0, fixes_after_s=None)
    first = _run(node, clock, nr.RecoveryStore(path))
    assert first.outcome == nr.STILL_WRONG and len(node.saves()) == 2
    assert nr.RecoveryStore(path).entry(KEY)["saves"] == 1
    # A restart (new store object on the same file) inside the retry gap: no save.
    soon = _run(node, clock, nr.RecoveryStore(path))
    assert soon.outcome == nr.COOLDOWN and len(node.saves()) == 2
    clock.t += nr.RETRY_GAP_S
    second = _run(node, clock, nr.RecoveryStore(path))
    assert second.outcome == nr.STILL_WRONG and len(node.saves()) == 4
    # MAX_SAVES_PER_EPISODE refreshes reached: COOLDOWN_S without a save, across restarts.
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["sensors"][KEY]["cooldown_until"] == pytest.approx(clock.t + nr.COOLDOWN_S, abs=1)
    for step in (nr.RETRY_GAP_S, nr.COOLDOWN_S / 2):
        clock.t += step
        held = _run(node, clock, nr.RecoveryStore(path))
        assert held.outcome == nr.COOLDOWN and len(node.saves()) == 4
    text, _level = nr.result_message(held)
    assert text.startswith("Sensor clock still wrong (is 10 min behind). Next NTP refresh after ")
    clock.t += nr.COOLDOWN_S
    third = _run(node, clock, nr.RecoveryStore(path))
    assert third.outcome == nr.STILL_WRONG and len(node.saves()) == 6 and node.servers() == ORIGINAL
    # The clock is right again: the episode is forgotten.
    node.offset_s = 0.1
    assert _run(node, clock, nr.RecoveryStore(path)).outcome == nr.OK
    assert nr.RecoveryStore(path).entry(KEY) == {}


def test_a_save_whose_result_was_never_written_still_counts(tmp_path):
    store = nr.RecoveryStore(tmp_path / "s.json")
    store.note_save("k", START)
    store.note_save("k", START + nr.RETRY_GAP_S)
    allowed, until = store.may_save("k", START + nr.RETRY_GAP_S + 10)
    assert not allowed and until == pytest.approx(START + nr.RETRY_GAP_S + nr.COOLDOWN_S)


def test_success_is_never_reported_while_the_list_is_not_restored():
    assert nr.result_message(nr.RecoveryResult(nr.SYNCED, restored=False)) is None
    assert nr.result_message(nr.RecoveryResult(nr.SYNCED, restored=None)) is None
    assert nr.result_message(nr.RecoveryResult(nr.SYNCED, restored=True)) == ("Sensor clock synchronized.", "info")


def test_drift_below_the_threshold_is_only_reported(store):
    clock = FakeTime()
    node = FakeNode(clock, 30.0)
    result = _run(node, clock, store)
    assert result.outcome == nr.DRIFT and not node.saves()
    assert nr.result_message(result) == ("Sensor clock is 30.0 s ahead of the computer.", "info")


def test_a_programming_error_never_escapes(store):
    class Broken:
        origin = "x"

        def get(self, path):
            if path == nr.NTP_PAGE_PATH:
                return nr.WebReply(200, {}, json.dumps(NTP_PAGE))
            return nr.WebReply(200, {}, json.dumps({"values": {"02-system-time": "2012-01-02 11:41:53+0000"}}))

        def put_json(self, *_a, **_k):
            raise AssertionError("must not be reached")

    pair_idle = threading.Event()
    result = nr.recover_clock(Broken(), store, "k", pc_check=lambda _s: 1 / 0, pair_idle=pair_idle)
    assert result.outcome == nr.UNREADABLE and "ZeroDivisionError" in result.detail and pair_idle.is_set()
