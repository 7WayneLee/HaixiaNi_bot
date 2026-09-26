"""Antigravity 額度用完換帳號接力：暫停上限、單一 worker 試探、resume-now 與狀態欄位的離線測試。"""
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from haixia import correction
from haixia.transcript import create
from scripts import correct_transcripts as cli

ROOT = Path(__file__).resolve().parents[1]
EPOCH = 1_800_000_000.0
QUOTA_FIELDS = ("agy_quota_exhausted_since", "agy_quota_message", "agy_quota_kind", "agy_quota_resets_at")


def document(count=12, spacing=130):
    engine = {"name": "whisper", "model": "large-v3", "version": "1", "params": {},
              "device": "cpu", "compute_type": "int8", "elapsed_sec": 1.0}
    segments = [{"start": float(i * spacing), "end": float(i * spacing + 8),
                 "text_raw": "麻黄湯主之", "text": "麻黄湯主之", "speaker": None,
                 "confidence": None, "low_confidence": False} for i in range(count)]
    return create("影片/02 針灸/甲.rm", count * spacing + 9, engine, segments)


CHUNKS = len(correction.split_chunks(document()["segments"]))


def quota_response(resets="2h31m45s", extra=""):
    short = f"RESOURCE_EXHAUSTED (code 429): Individual quota reached. {extra}Resets in {resets}"
    return {"stderr": "AGY_ERROR: " + json.dumps({"short_error": short, "status": 429}),
            "output": "", "exit_code": 3}


def note_quota(shared, response, started=EPOCH):
    kind, reason = cli.error_kind(response)
    assert kind == "quota"
    message, countdown = cli.quota_details(response)
    return shared.note_error(kind, reason, logging.getLogger("rotation-test"),
                             cli.quota_reset_seconds(response), message, countdown, started)


def saved(path):
    return json.loads((path / "status.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("poll,resets,pause,kind", [
    (600, "2h31m45s", 600, "five_hour"),
    (None, "2h31m45s", 9105 + 120, "five_hour"),
    (600, "52h20m12s", 600, "weekly"),
    (None, "52h20m12s", 1200, "weekly"),
])
def test_pause_limit_and_quota_status_fields(tmp_path, monkeypatch, poll, resets, pause, kind):
    monkeypatch.setattr(cli.time, "time", lambda: EPOCH)
    shared = cli.SharedState(tmp_path, CHUNKS, 1.0, backoff_base=1200, backoff_max=3600)
    shared.quota_poll = poll
    assert note_quota(shared, quota_response(resets, "key sk-ABC123 "))
    assert shared.pause_until == EPOCH + pause
    status = saved(tmp_path)
    assert status["agy_quota_exhausted_since"] == cli.timestamp(EPOCH)
    assert status["agy_quota_kind"] == kind
    assert f"Resets in {resets}" in status["agy_quota_message"]
    assert "sk-***" in status["agy_quota_message"] and "sk-ABC" not in json.dumps(status)
    assert status["agy_quota_resets_at"] == cli.timestamp(EPOCH + cli.reset_countdown("Resets in " + resets))
    assert status["engines"]["agy"]["quota_poll_min"] == (None if poll is None else 10)


def test_reset_countdown_units():
    assert cli.reset_countdown("Resets in 2h31m45s") == 9105
    assert cli.reset_countdown('"Resets in 2d4h."') == 187200
    assert cli.reset_countdown("quota reached") is None
    assert cli.quota_details({"stderr": "RESOURCE_EXHAUSTED", "output": ""}) == (None, None)


def test_probe_success_clears_round_but_straggler_does_not(tmp_path, monkeypatch):
    now = [EPOCH]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    shared = cli.SharedState(tmp_path, CHUNKS, 1.0)
    shared.quota_poll = 600
    log = logging.getLogger("rotation-test")
    assert note_quota(shared, quota_response())
    now[0] += 30
    shared.note_success(EPOCH - 60, log)  # 額度用完之前就送出的呼叫晚到的成功
    assert saved(tmp_path)["agy_quota_exhausted_since"] == cli.timestamp(EPOCH)
    assert shared.pause_until == EPOCH + 600
    now[0] = EPOCH + 601
    assert shared.wait_if_paused() is True
    assert shared.probing and shared.probe_owner == threading.get_ident()
    assert saved(tmp_path)["engines"]["agy"]["probing"] is True
    shared.note_success(now[0], log)
    status = saved(tmp_path)
    assert all(status[key] is None for key in QUOTA_FIELDS)
    assert not shared.probing and shared.probe_owner is None and shared.pause_until == 0


def test_probe_quota_repauses_and_old_call_is_ignored(tmp_path, monkeypatch):
    now = [EPOCH]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    shared = cli.SharedState(tmp_path, CHUNKS, 1.0)
    shared.quota_poll = 600
    assert note_quota(shared, quota_response())
    now[0] = EPOCH + 601
    assert shared.wait_if_paused() is True and shared.probing
    # 試探開始前就送出的呼叫（例如換帳號前的舊呼叫）回 429：不暫停、不結束試探。
    assert note_quota(shared, quota_response(), started=EPOCH - 5)
    assert shared.pause_until == 0 and shared.probing
    # 試探本身回 429：再暫停 N 分鐘，同一輪的開始時間不變。
    assert note_quota(shared, quota_response("2h21m"), started=EPOCH + 601)
    assert shared.pause_until == EPOCH + 601 + 600 and not shared.probing
    assert saved(tmp_path)["agy_quota_exhausted_since"] == cli.timestamp(EPOCH)


def test_without_poll_pause_expiry_does_not_probe(tmp_path, monkeypatch):
    now = [EPOCH]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    shared = cli.SharedState(tmp_path, CHUNKS, 1.0)
    assert note_quota(shared, quota_response("30s"))
    assert shared.pause_until == EPOCH + 150
    now[0] = EPOCH + 151
    assert shared.wait_if_paused() is True
    assert not shared.probing and shared.probe_owner is None


@pytest.fixture
def run_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "agy").symlink_to(ROOT / "tests/fake_agy.py")
    source = tmp_path / "in/影片/02 針灸/甲.rm.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(document(), ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               FAKE_AGY_MODE="account", FAKE_AGY_COUNTER=str(tmp_path / "counter"),
               FAKE_AGY_TIMELINE=str(tmp_path / "timeline"), FAKE_AGY_SLEEP="0.3",
               HAIXIA_BACKOFF_BASE_SEC="0.3", HAIXIA_BACKOFF_MAX_SEC="0.3")
    args = [sys.executable, str(ROOT / "scripts/correct_transcripts.py"),
            "--in-dir", str(tmp_path / "in"), "--out-dir", str(tmp_path / "out"),
            "--work-dir", str(tmp_path / "work"), "--jobs", "4"]
    return args, env, tmp_path


def timeline(tmp):
    """假 agy 每次呼叫的（編號、開始、結束、結果），依編號排序。"""
    path = tmp / "timeline"
    lines = path.read_text().splitlines() if path.exists() else []
    return sorted((int(number), float(start), float(end), outcome)
                  for number, start, end, outcome in (line.split() for line in lines))


def overlaps(first, second):
    return first[1] < second[2] and second[1] < first[2]


def isolated(call, calls):
    return not any(overlaps(call, other) for other in calls if other is not call)


def wait_for(condition, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.05)
    raise AssertionError("等待逾時")


def run(args, env):
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]


def test_poll_probes_with_one_worker_then_all_resume(run_env):
    args, env, tmp = run_env
    env["FAKE_AGY_QUOTA_CALLS"] = "7"
    run(args + ["--quota-poll-min", "0.015"], env)
    calls = timeline(tmp)
    assert [call[3] for call in calls].count("quota") == 7
    assert [call[3] for call in calls].count("ok") == CHUNKS
    # 4 個 worker 同時碰到 429 之後，#5–#7 的試探都失敗、#8 成功；每次都只有一個呼叫在跑。
    probes = [call for call in calls if 5 <= call[0] <= 8]
    assert [call[3] for call in probes] == ["quota", "quota", "quota", "ok"]
    assert all(isolated(call, calls) for call in probes)
    later = [call for call in calls if call[0] > 8]
    assert any(overlaps(first, second) for index, first in enumerate(later) for second in later[index + 1:])
    log = (tmp / "work/logs/correct.log").read_text(encoding="utf-8")
    assert "Antigravity 額度暫停最多 0.015 分鐘就試探一次" in log
    assert log.count("額度試探：先由一個 worker 呼叫一次") >= 4
    assert log.count("額度試探成功") == 1
    status = saved(tmp / "work")
    assert status["chunks_ok"] == CHUNKS and status["state"] == "finished"
    assert all(status[key] is None for key in QUOTA_FIELDS)


@pytest.mark.parametrize("extra", [[], ["--quota-poll-min", "30"]])
def test_resume_now_probes_immediately_and_is_removed(run_env, extra):
    args, env, tmp = run_env
    work = tmp / "work"
    switch = tmp / "switched"
    resume = work / "resume-now"
    env["FAKE_AGY_SWITCH"] = str(switch)

    def paused_and_idle():
        status = saved(work)
        return (status["engines"]["agy"]["state"] == "paused" and status["active_engines"] == []
                and status["agy_quota_exhausted_since"] is not None)

    process = subprocess.Popen(args + extra, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(paused_and_idle)
        before = len(timeline(tmp))
        since = saved(work)["agy_quota_exhausted_since"]
        # 還沒換帳號就 touch：只有一個 worker 試探，失敗後再暫停，同一輪不變。
        resume.touch()
        wait_for(lambda: not resume.exists() and len(timeline(tmp)) == before + 1 and paused_and_idle())
        time.sleep(.5)
        assert len(timeline(tmp)) == before + 1
        assert saved(work)["agy_quota_exhausted_since"] == since
        # 換好帳號再 touch：立刻接上並跑完。
        switch.touch()
        resume.touch()
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert not resume.exists()
    log = (work / "logs/correct.log").read_text(encoding="utf-8")
    assert log.count("收到 resume-now，立即重試") == 2
    calls = timeline(tmp)
    first_ok = min((call for call in calls if call[3] == "ok"), key=lambda call: call[1])
    assert isolated(first_ok, calls)
    status = saved(work)
    assert status["chunks_ok"] == CHUNKS and all(status[key] is None for key in QUOTA_FIELDS)


def test_resume_now_without_quota_pause_is_just_removed(tmp_path):
    shared = cli.SharedState(tmp_path, CHUNKS, 1.0)
    (tmp_path / "resume-now").touch()
    logger = logging.getLogger("rotation-test")
    assert shared.check_resume_now(logger)
    assert not (tmp_path / "resume-now").exists() and not shared.probing
    assert not shared.check_resume_now(logger)


def test_without_poll_all_workers_resume_together(run_env):
    args, env, tmp = run_env
    env.update(FAKE_AGY_QUOTA_CALLS="4", FAKE_AGY_RESETS="")
    run(args, env)
    calls = timeline(tmp)
    ok = [call for call in calls if call[3] == "ok"]
    assert len(ok) == CHUNKS
    assert any(overlaps(first, second) for index, first in enumerate(ok) for second in ok[index + 1:])
    log = (tmp / "work/logs/correct.log").read_text(encoding="utf-8")
    assert "暫停：Antigravity 額度用完或被限速" in log
    assert "額度試探" not in log and "試探一次" not in log
    status = saved(tmp / "work")
    assert status["engines"]["agy"]["quota_poll_min"] is None
    assert all(status[key] is None for key in QUOTA_FIELDS)
