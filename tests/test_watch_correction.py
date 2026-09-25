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
    assert calls_data[0][5] == "escalation" and calls_data[0][-1] == "--json"


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
