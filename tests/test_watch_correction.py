"""校正監視程式只用假狀態、假 log 與假 Orca 的離線測試。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts import watch_correction as watch


@pytest.fixture
def monitor(tmp_path):
    current = [datetime(2026, 9, 26, 8, 0, tzinfo=timezone(timedelta(hours=8)))]
    free = [10 * 1024**3]
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "correct.log").write_text("既有 log\n", encoding="utf-8")
    calls = tmp_path / "orca-calls.jsonl"
    orca = tmp_path / "fake-orca"
    orca.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                    f"with open({str(calls)!r}, 'a', encoding='utf-8') as out:\n"
                    "    out.write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n",
                    encoding="utf-8")
    orca.chmod(0o755)
    stamp = current[0].isoformat()
    status = {"pid": 42, "state": "running", "started_at": stamp,
              "updated_at": stamp, "last_progress_at": stamp, "paused_until": None,
              "quota_resets_at": None, "chunks_done": 2, "chunks_total": 10,
              "chunks_ok": 2, "chunks_partial": 0, "chunks_failed": 0,
              "audio_hours_done": 0.2, "audio_hours_total": 1.0}

    def save():
        (tmp_path / "status.json").write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")

    save()
    watcher = watch.CorrectionWatcher(tmp_path, notify_run="run_fake", orca_bin=str(orca),
                                      daily_status_hour=-1, now_fn=lambda: current[0],
                                      pid_alive_fn=lambda pid: pid == 42,
                                      free_bytes_fn=lambda path: free[0])
    return watcher, status, save, current, free, calls


def sent(calls):
    return [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()] if calls.exists() else []


def test_engine_stops_weekly_info_other_alert(monitor, capsys):
    watcher, status, save, _current, _free, calls = monitor
    status["engines"] = {"codex": {"state": "stopped", "reason": "Codex 週額度 85% 已達上限"}}
    save()
    watcher.check_once()
    assert "Codex 週額度停止" in capsys.readouterr().out
    assert not sent(calls)
    status["engines"]["agy"] = {"state": "stopped", "reason": "CLI 損壞"}
    save()
    watcher.check_once()
    assert "agy 引擎停止" in capsys.readouterr().out
    assert len(sent(calls)) == 1


AGY_QUOTA = ('AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429): Individual quota reached. '
             'Resets in 2h16m17s."}')


@pytest.mark.parametrize("state,subject", [("finished", "校正執行結束"), ("aborted", "校正已手動中止")])
def test_terminal_state_skips_engine_stop_alerts(monitor, state, subject, capsys):
    watcher, status, save, _current, _free, calls = monitor
    status.update(state=state, pid=0,
                  engines={"agy": {"state": "stopped", "reason": AGY_QUOTA},
                           "codex": {"state": "stopped", "reason": "CLI 損壞"}})
    save()
    assert watcher.check_once()
    output = capsys.readouterr().out
    assert "ALERT" not in output and "引擎停止" not in output
    assert [call[7] for call in sent(calls)] == [subject]


@pytest.mark.parametrize("reason", [AGY_QUOTA, "RESOURCE_EXHAUSTED", "HTTP 429 Too Many Requests",
                                    "Quota exceeded for this model", "Antigravity 額度用完"])
def test_engine_stop_for_quota_is_info_only(monitor, reason, capsys):
    watcher, status, save, _current, _free, calls = monitor
    status["engines"] = {"agy": {"state": "stopped", "reason": reason}}
    save()
    watcher.check_once()
    output = capsys.readouterr().out
    assert "INFO" in output and "agy 引擎因額度停止" in output
    assert "ALERT" not in output and not sent(calls)


@pytest.mark.parametrize("reason", ["CLI 損壞", "連線逾時 5 次", "PID 4290 無回應"])
def test_engine_stop_for_other_errors_still_alerts(monitor, reason, capsys):
    watcher, status, save, _current, _free, calls = monitor
    status["engines"] = {"agy": {"state": "stopped", "reason": reason}}
    save()
    watcher.check_once()
    assert f"ALERT 2026-09-26 08:00:00 agy 引擎停止：{reason}" in capsys.readouterr().out
    assert [call[5:8] for call in sent(calls)] == [["status", "--subject", "校正監視警報：agy 引擎停止"]]


@pytest.mark.parametrize("kind", ["程式意外結束", "卡住", "暫停逾時", "新的失敗段", "斷路器暫停", "磁碟空間不足"])
def test_every_alert_uses_fake_orca(monitor, kind, capsys):
    watcher, status, save, current, free, calls = monitor
    assert not watcher.check_once()
    if kind == "程式意外結束":
        status["pid"] = 43
    elif kind == "卡住":
        old = (current[0] - timedelta(minutes=46)).isoformat()
        status.update(updated_at=old, last_progress_at=old)
    elif kind == "暫停逾時":
        status.update(state="paused", paused_until=(current[0] - timedelta(minutes=31)).isoformat())
    elif kind == "新的失敗段":
        status["chunks_failed"] = 3
    elif kind == "斷路器暫停":
        with watcher.correct_log.open("a", encoding="utf-8") as output:
            output.write("2026-09-26 暫停：連續 3 次呼叫失敗，稍後再試\n")
    else:
        free[0] = 4 * 1024**3
    save()
    assert not watcher.check_once()
    assert f"ALERT 2026-09-26 08:00:00 {kind}：" in capsys.readouterr().out
    assert f"{kind}：" in watcher.watch_log.read_text(encoding="utf-8")
    calls_data = sent(calls)
    assert len(calls_data) == 1
    assert calls_data[0][:5] == ["orchestration", "send", "--to", "run:run_fake", "--type"]
    assert calls_data[0][5] == "status" and calls_data[0][-1] == "--json"
    assert calls_data[0][6:8] == ["--subject", f"校正監視警報：{kind}"]


def test_alert_uses_status_type_with_alert_subject(monitor):
    watcher, status, save, current, _free, calls = monitor
    watcher.daily_status_hour = 8
    status["pid"] = 43
    save()
    watcher.check_once()
    alert, daily = sent(calls)
    assert alert[4:8] == ["--type", "status", "--subject", "校正監視警報：程式意外結束"]
    assert "escalation" not in alert
    assert daily[4:8] == ["--type", "status", "--subject", "校正每日進度"]


def test_alert_dedup_resend_and_clear(monitor):
    watcher, status, save, current, _, calls = monitor
    watcher.check_once()
    status["pid"] = 43
    save()
    watcher.check_once()
    current[0] += timedelta(hours=5)
    status.update(updated_at=current[0].isoformat(), last_progress_at=current[0].isoformat())
    save()
    watcher.check_once()
    assert len(sent(calls)) == 1
    current[0] += timedelta(hours=1, minutes=1)
    status.update(updated_at=current[0].isoformat(), last_progress_at=current[0].isoformat())
    save()
    watcher.check_once()
    assert len(sent(calls)) == 2
    status["pid"] = 42
    save()
    watcher.check_once()
    status["pid"] = 43
    save()
    watcher.check_once()
    assert len(sent(calls)) == 3


def test_failed_chunks_need_three_more_and_dedup(monitor):
    watcher, status, save, current, _, calls = monitor
    watcher.check_once()
    for failed in (3, 4, 7):
        status["chunks_failed"] = failed
        save()
        watcher.check_once()
    assert len(sent(calls)) == 1
    watcher.check_once()  # 失敗數未增加，狀況解除。
    status["chunks_failed"] = 10
    save()
    watcher.check_once()
    assert len(sent(calls)) == 2


def test_quota_pause_info_daily_summary_and_read_only(monitor, capsys):
    watcher, status, save, current, _, calls = monitor
    status.update(state="paused", paused_until=(current[0] + timedelta(hours=3)).isoformat(),
                  quota_resets_at=(current[0] + timedelta(hours=2, minutes=58)).isoformat())
    save()
    status_before = (watcher.work_dir / "status.json").read_bytes()
    log_before = watcher.correct_log.read_bytes()
    watcher.daily_status_hour = 9
    watcher.check_once()
    current[0] = current[0].replace(hour=9)
    watcher.check_once()
    watcher.check_once()
    current[0] += timedelta(days=1)
    status["paused_until"] = (current[0] + timedelta(hours=3)).isoformat()
    save()
    watcher.check_once()
    output = capsys.readouterr().out
    assert output.count("INFO") == 1 and "額度暫停" in output
    assert "ALERT" not in output
    assert output.count("校正每日進度") == 2
    assert all("status" in call for call in sent(calls))
    assert log_before == watcher.correct_log.read_bytes()
    assert status_before != (watcher.work_dir / "status.json").read_bytes()  # 測試資料自行更新。


@pytest.mark.parametrize("state,subject", [("finished", "校正執行結束"), ("aborted", "校正已手動中止")])
def test_terminal_status_sends_once_and_exits(monitor, state, subject, capsys):
    watcher, status, save, _, _, calls = monitor
    status.update(state=state, pid=0, chunks_ok=6, chunks_partial=2, chunks_failed=2,
                  failed_files=["影片/甲.rm", "影片/乙.rm"])
    save()
    assert watcher.check_once()
    output = capsys.readouterr().out
    assert subject in output and "ALERT" not in output
    assert "ok 6、partial 2、failed 2" in output
    if state == "finished":
        assert "影片/甲.rm" in output and "影片/乙.rm" in output
    assert len(sent(calls)) == 1 and "status" in sent(calls)[0]


def test_weekly_quota_asks_to_switch_account(monitor, capsys):
    watcher, status, save, current, _free, calls = monitor
    weekly = 'AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 52h20m12s."}'
    status.update(state="paused", paused_until=(current[0] + timedelta(minutes=40)).isoformat(),
                  engines={"agy": {"state": "paused", "reason": weekly}})
    save()
    watcher.check_once()
    output = capsys.readouterr().out
    assert "ALERT" in output and "Antigravity 週額度用完" in output and "約 52 小時" in output
    assert len(sent(calls)) == 1

    def later(hours):  # 程式每小時重試一次、再次暫停
        current[0] += timedelta(hours=hours)
        status["paused_until"] = (current[0] + timedelta(minutes=40)).isoformat()
        save()

    later(1)
    watcher.check_once()
    assert len(sent(calls)) == 1
    later(6)
    watcher.check_once()
    assert len(sent(calls)) == 2
    status["engines"]["agy"]["reason"] = weekly.replace("52h20m12s", "30m23s")
    later(7)
    watcher.check_once()
    assert len(sent(calls)) == 2


def test_long_reset_hours_parses_units():
    assert watch.long_reset_hours("Resets in 2h31m45s") is None
    assert round(watch.long_reset_hours("Resets in 52h20m12s")) == 52
    assert watch.long_reset_hours("Resets in 2d4h") == 52
    assert watch.long_reset_hours("") is None


def test_quota_round_alerts_once_per_round(monitor, capsys):
    watcher, status, save, current, _free, calls = monitor
    five = 'RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 2h31m45s'
    since = (current[0] - timedelta(minutes=1)).isoformat()
    status.update(state="paused", paused_until=(current[0] + timedelta(minutes=9)).isoformat(),
                  quota_resets_at=(current[0] + timedelta(hours=2, minutes=31)).isoformat(),
                  agy_quota_exhausted_since=since, agy_quota_message=five, agy_quota_kind="five_hour",
                  agy_quota_resets_at=(current[0] + timedelta(hours=2, minutes=31)).isoformat(),
                  engines={"agy": {"state": "paused", "reason": five, "quota_poll_min": 10}})
    save()
    watcher.check_once()
    output = capsys.readouterr().out
    assert "ALERT" in output and "Antigravity 額度用完，請切換 Gemini 帳號" in output
    assert "5 小時額度用完" in output and "預計 09/26 10:31 重設（約 2.5 小時後）" in output
    assert "/logout" in output and str(watcher.work_dir / "resume-now") in output
    assert "最多 10 分鐘" in output and "額度暫停" not in output
    assert len(sent(calls)) == 1 and sent(calls)[0][5] == "status"
    assert "校正監視警報：Antigravity 額度用完，請切換 Gemini 帳號" in sent(calls)[0]

    # 同一輪：試探中、再次暫停、訊息更新都不重送。
    for state in ("running", "paused"):
        current[0] += timedelta(minutes=10)
        status.update(state=state, updated_at=current[0].isoformat(),
                      agy_quota_message=five.replace("2h31m45s", "2h21m"),
                      paused_until=(current[0] + timedelta(minutes=10)).isoformat() if state == "paused" else None)
        save()
        watcher.check_once()
    assert len(sent(calls)) == 1

    # 換帳號後 agy 成功，欄位清空；不通知。
    current[0] += timedelta(minutes=5)
    status.update(state="running", paused_until=None, quota_resets_at=None, updated_at=current[0].isoformat(),
                  last_progress_at=current[0].isoformat(), agy_quota_exhausted_since=None,
                  agy_quota_message=None, agy_quota_kind=None, agy_quota_resets_at=None,
                  engines={"agy": {"state": "running", "reason": None, "quota_poll_min": 10}})
    save()
    watcher.check_once()
    assert len(sent(calls)) == 1

    # 下一輪（週額度）即使在 6 小時內也再通知一次，文字不同。
    weekly = five.replace("2h31m45s", "52h20m12s")
    current[0] += timedelta(hours=1)
    status.update(state="paused", paused_until=(current[0] + timedelta(minutes=10)).isoformat(),
                  updated_at=current[0].isoformat(), last_progress_at=current[0].isoformat(),
                  agy_quota_exhausted_since=current[0].isoformat(), agy_quota_message=weekly,
                  agy_quota_kind="weekly", agy_quota_resets_at=(current[0] + timedelta(hours=52)).isoformat(),
                  engines={"agy": {"state": "paused", "reason": weekly, "quota_poll_min": 10}})
    save()
    capsys.readouterr()
    watcher.check_once()
    output = capsys.readouterr().out
    assert len(sent(calls)) == 2
    assert "週額度用完" in output and "約 52.0 小時後" in output and "5 小時額度" not in output
    assert output.count("ALERT") == 1 and "錯誤訊息顯示約" not in output  # 舊版週額度警報不再另外送
    watcher.check_once()
    assert len(sent(calls)) == 2


def test_quota_round_without_poll_or_reset_time(monitor, capsys):
    watcher, status, save, current, _free, calls = monitor
    status.update(state="paused", agy_quota_exhausted_since=current[0].isoformat(),
                  agy_quota_message="RESOURCE_EXHAUSTED", agy_quota_kind="five_hour",
                  agy_quota_resets_at=None, engines={"agy": {"state": "paused", "quota_poll_min": None}})
    save()
    watcher.check_once()
    output = capsys.readouterr().out
    assert "重設時間不明" in output and "沒有設 --quota-poll-min" in output
    assert len(sent(calls)) == 1


def test_codex_quota_pause_does_not_ask_to_switch_account(monitor, capsys):
    watcher, status, save, current, _free, calls = monitor
    status.update(state="paused", agy_quota_exhausted_since=None, agy_quota_message=None,
                  agy_quota_kind=None, agy_quota_resets_at=None,
                  engines={"codex": {"state": "paused", "reason": "Codex session 額度 90% 已達上限 85%",
                                     "paused_until": (current[0] + timedelta(hours=2)).isoformat()}})
    save()
    watcher.check_once()
    status["engines"]["codex"].update(state="stopped", reason="Codex 週額度 85% 已達上限 80%")
    save()
    watcher.check_once()
    output = capsys.readouterr().out
    assert "ALERT" not in output and "Codex 週額度停止" in output
    assert not sent(calls)
