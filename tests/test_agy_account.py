"""記錄 agy 實際登入的帳號、帳號不符就暫停 Antigravity 的離線測試（只用假帳號）。"""
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from haixia import correction
from haixia.transcript import create, validate_corrected
from scripts import correct_transcripts as cli

ROOT = Path(__file__).resolve().parents[1]
EPOCH = 1_800_000_000.0
A, B, C = "a@example.com", "b@example.com", "c@example.com"


def document(count=8, spacing=130):
    engine = {"name": "whisper", "model": "large-v3", "version": "1", "params": {},
              "device": "cpu", "compute_type": "int8", "elapsed_sec": 1.0}
    segments = [{"start": float(i * spacing), "end": float(i * spacing + 8),
                 "text_raw": "麻黄湯主之", "text": "麻黄湯主之", "speaker": None,
                 "confidence": None, "low_confidence": False} for i in range(count)]
    return create("影片/02 針灸/甲.rm", count * spacing + 9, engine, segments)


CHUNKS = len(correction.split_chunks(document()["segments"]))
SOURCE = document()["source"]


def saved(work):
    return json.loads((work / "status.json").read_text(encoding="utf-8"))


def count(path):
    return int(path.read_text() or "0") if path.exists() else 0


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


def auth_line(email):
    return (f"I0926 17:42:37.562708       1 server_oauth.go:196] applyAuthResult: "
            f"email={email}, authMethod=consumer, quotaProject=\n")


def test_parse_account_from_log(tmp_path):
    log = tmp_path / "agy.log"
    assert cli.agy_account(log) is None  # 檔案不存在
    log.write_text("I0926 keyring.go:64] keyringAuth: loaded token\n")
    assert cli.agy_account(log) is None
    log.write_text(auth_line(A) + "其他行\n" + auth_line(C))
    assert cli.agy_account(log) == C  # 同一個 log 有多行時取最後一行
    log.write_text(auth_line(A) + auth_line(""))
    assert cli.agy_account(log) == A


def test_prune_keeps_newest_500(tmp_path):
    logs = tmp_path / "agy-logs"
    logs.mkdir()
    for index in range(505):
        (logs / f"20260926-120000-{index:06d}-call-1.log").write_text("x")
    (logs / "other.txt").write_text("不是 log")
    cli.prune_agy_logs(tmp_path)
    names = sorted(path.name for path in logs.glob("*.log"))
    assert len(names) == cli.AGY_LOG_KEEP == 500
    assert names[0] == "20260926-120000-000005-call-1.log"
    assert (logs / "other.txt").exists()


def test_note_account_rules(tmp_path, monkeypatch):
    now = [EPOCH]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    log = logging.getLogger("account-test")
    shared = cli.SharedState(tmp_path, 4, 1.0)
    # 沒有設定預期帳號：只記錄。
    shared.note_account(A, EPOCH, log, "呼叫")
    status = saved(tmp_path)
    assert status["agy_account"] == A and status["agy_expected_account"] is None
    assert status["agy_account_mismatch"] is False and status["agy_account_mismatch_since"] is None
    # 大小寫不同視為同一個帳號；解析不到帳號不暫停，也不蓋掉最後看到的帳號。
    shared.expected_arg = "A@Example.com"
    shared.note_account(A, EPOCH + 1, log, "呼叫")
    shared.note_account(None, EPOCH + 2, log, "呼叫")
    assert not shared.agy_paused() and saved(tmp_path)["agy_account"] == A
    # 不符：暫停，但呼叫的結果不會解除暫停，只有 agy models 的檢查可以。
    now[0] = EPOCH + 10
    shared.note_account(B, EPOCH + 3, log, "呼叫")
    assert shared.agy_paused() and shared.pause_until == 0
    status = saved(tmp_path)
    assert status["agy_account_mismatch"] is True and status["agy_account"] == B
    assert status["agy_account_mismatch_since"] == cli.timestamp(EPOCH + 10)
    assert status["engines"]["agy"]["state"] == "paused" and status["state"] == "paused"
    assert status["paused_until"] is None
    shared.note_account(A, EPOCH + 4, log, "呼叫")
    assert shared.agy_paused()
    now[0] = EPOCH + 20
    shared.note_account(A, EPOCH + 20, log, "檢查")
    assert not shared.agy_paused() and shared.account_resumed_at == EPOCH + 20
    # 恢復前就送出的呼叫晚到、用的是舊帳號：不再暫停，也不蓋掉目前的帳號。
    shared.note_account(B, EPOCH + 15, log, "呼叫")
    assert not shared.agy_paused() and saved(tmp_path)["agy_account"] == A
    # 恢復後才送出的呼叫又用錯帳號：再次暫停，新的一輪。
    now[0] = EPOCH + 30
    shared.note_account(B, EPOCH + 25, log, "呼叫")
    assert saved(tmp_path)["agy_account_mismatch_since"] == cli.timestamp(EPOCH + 30)
    # 預期帳號被拿掉：檢查時解除暫停。
    shared.expected_arg = None
    shared.note_account(None, EPOCH + 31, log, "檢查")
    assert not shared.agy_paused() and saved(tmp_path)["agy_expected_account"] is None


def test_expected_account_file_wins_and_is_reread(tmp_path):
    shared = cli.SharedState(tmp_path, 1, 1.0)
    shared.expected_arg = B
    assert shared.expected_account() == B
    (tmp_path / "expected-account").write_text(f"  {C}\n")
    assert shared.expected_account() == C
    (tmp_path / "expected-account").write_text(A)
    assert shared.expected_account() == A
    (tmp_path / "expected-account").write_text("\n")
    assert shared.expected_account() == B


def test_quota_pause_and_mismatch_are_independent(tmp_path, monkeypatch):
    now = [EPOCH]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    log = logging.getLogger("account-test")
    shared = cli.SharedState(tmp_path, 4, 1.0)
    shared.quota_poll = 600
    shared.expected_arg = C
    shared.note_account(A, EPOCH, log, "呼叫")
    assert shared.note_error("quota", "RESOURCE_EXHAUSTED Resets in 2h", log, 7200, None, 7200, EPOCH)
    assert shared.pause_until == EPOCH + 600
    # 帳號對了，但額度暫停還沒到期：仍然暫停。
    shared.note_account(C, EPOCH + 1, log, "檢查")
    assert shared.account_mismatch_since is None and shared.agy_paused()
    # 額度暫停到期後照原本的試探規則恢復。
    now[0] = EPOCH + 601
    assert shared.wait_if_paused() is True and shared.probing
    # 反過來：額度正常但帳號不符，wait_if_paused 不放行（雙引擎時放回佇列）。
    shared.note_success(now[0], log)
    now[0] = EPOCH + 700
    shared.note_account(A, EPOCH + 700, log, "呼叫")
    assert shared.wait_if_paused(requeue=True) == "requeue"


@pytest.fixture
def run_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("agy", "fake_agy.py"), ("codex", "fake_codex.py"),
                         ("orca", "fake_orca_account.py")):
        (ROOT / "tests" / script).chmod(0o755)
        (bin_dir / name).symlink_to(ROOT / "tests" / script)
    source = tmp_path / "in/影片/02 針灸/甲.rm.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(document(), ensure_ascii=False), encoding="utf-8")
    account = tmp_path / "keychain"
    account.write_text(C + "\n")
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               FAKE_AGY_COUNTER=str(tmp_path / "counter"), FAKE_AGY_ACCOUNT_FILE=str(account),
               FAKE_AGY_MODELS_COUNTER=str(tmp_path / "models"),
               HAIXIA_BACKOFF_BASE_SEC="0.3", HAIXIA_BACKOFF_MAX_SEC="0.3",
               HAIXIA_ACCOUNT_POLL_SEC="3600")
    args = [sys.executable, str(ROOT / "scripts/correct_transcripts.py"),
            "--in-dir", str(tmp_path / "in"), "--out-dir", str(tmp_path / "out"),
            "--work-dir", str(tmp_path / "work"), "--jobs", "1"]
    return args, env, tmp_path


def start(args, env):
    return subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def finish(process):
    try:
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def attempt_account(work, number):
    path = cli.cache_path(work, SOURCE, number).with_name(f"{number:05d}.attempt1.json")
    return json.loads(path.read_text(encoding="utf-8"))["agy_account"]


def output_chunks(tmp):
    return validate_corrected(json.loads((tmp / "out/影片/02 針灸/甲.rm.json").read_text()))["correction"]["chunks"]


def test_without_expected_only_records(run_env):
    args, env, tmp = run_env
    env.update(FAKE_AGY_FLIP_AT="2", FAKE_AGY_FLIP_TO=A)
    finish(start(args, env))
    work = tmp / "work"
    status = saved(work)
    assert status["state"] == "finished" and status["chunks_ok"] == CHUNKS
    assert status["agy_account"] == A and status["agy_expected_account"] is None
    assert status["agy_account_mismatch"] is False
    assert [attempt_account(work, number) for number in range(1, CHUNKS + 1)] == [C] + [A] * (CHUNKS - 1)
    logs = sorted((work / "agy-logs").glob("*.log"))
    assert len(logs) == CHUNKS + 1
    assert sum("-models-" in path.name for path in logs) == 1
    assert all("applyAuthResult" in path.read_text() for path in logs)
    text = (work / "logs/correct.log").read_text(encoding="utf-8")
    assert "沒有設定預期的 Antigravity 帳號" in text and "暫停 Antigravity" not in text


def test_unparseable_account_does_not_fail_chunk(run_env):
    args, env, tmp = run_env
    (tmp / "keychain").write_text("")
    finish(start(args + ["--expected-agy-account", C], env))
    work = tmp / "work"
    status = saved(work)
    assert status["chunks_ok"] == CHUNKS and status["agy_account"] is None
    assert status["agy_account_mismatch"] is False
    assert attempt_account(work, 1) is None
    assert "無法從 agy log 解析登入的帳號" in (work / "logs/correct.log").read_text(encoding="utf-8")


def test_mismatch_pauses_and_models_poll_resumes(run_env):
    args, env, tmp = run_env
    env.update(FAKE_AGY_FLIP_AT="2", FAKE_AGY_FLIP_TO=A, HAIXIA_ACCOUNT_POLL_SEC="0.3")
    work = tmp / "work"
    process = start(args + ["--expected-agy-account", C], env)
    try:
        wait_for(lambda: saved(work)["agy_account_mismatch"])
        models = count(tmp / "models")
        time.sleep(1.5)
        # 暫停期間不再派新的 agy -p 呼叫，但每隔一段時間用 agy models 檢查帳號。
        assert count(tmp / "counter") == 2
        assert count(tmp / "models") >= models + 2
        status = saved(work)
        assert status["agy_account"] == A and status["agy_expected_account"] == C
        assert status["agy_account_mismatch_since"] and status["engines"]["agy"]["state"] == "paused"
        assert status["state"] == "paused" and status["paused_until"] is None
        # 用錯帳號的那一段結果照常驗收、採用，不重跑。
        chunk = json.loads(cli.cache_path(work, SOURCE, 2).read_text(encoding="utf-8"))["result"]
        assert chunk["status"] == "ok" and chunk["attempts"] == 1
        assert attempt_account(work, 2) == A
        (tmp / "keychain").write_text(C + "\n")  # 使用者在 agy 視窗登入回預期的帳號
    finally:
        finish(process)
    status = saved(work)
    assert status["state"] == "finished" and status["chunks_ok"] == CHUNKS
    assert status["agy_account"] == C and status["agy_account_mismatch"] is False
    assert status["agy_account_mismatch_since"] is None
    assert count(tmp / "counter") == CHUNKS
    assert [chunk["attempts"] for chunk in output_chunks(tmp)] == [1] * CHUNKS
    text = (work / "logs/correct.log").read_text(encoding="utf-8")
    assert f"暫停 Antigravity：agy 呼叫實際登入的帳號是 {A}，不是預期的 {C}（這次的結果照常驗收、採用）" in text
    assert text.count(f"Antigravity 帳號已符合預期（{C}），解除帳號不符的暫停") == 1
    assert "帳號仍不符" not in text  # 同樣的結果不重複記


@pytest.mark.parametrize("trigger", ["resume-now", "expected-account"])
def test_resume_now_or_new_expected_account_checks_immediately(run_env, trigger):
    args, env, tmp = run_env
    env.update(FAKE_AGY_FLIP_AT="2", FAKE_AGY_FLIP_TO=A)
    work = tmp / "work"
    process = start(args + ["--expected-agy-account", C], env)
    try:
        wait_for(lambda: saved(work)["agy_account_mismatch"])
        models = count(tmp / "models")
        time.sleep(.5)
        assert count(tmp / "models") == models and count(tmp / "counter") == 2  # 2 分鐘內不會自己檢查
        if trigger == "resume-now":
            (tmp / "keychain").write_text(C + "\n")
            (work / "resume-now").touch()
        else:
            (work / "expected-account").write_text(A + "\n")  # 指揮改用實際的帳號
    finally:
        finish(process)
    status = saved(work)
    assert status["chunks_ok"] == CHUNKS and status["agy_account_mismatch"] is False
    expected = C if trigger == "resume-now" else A
    assert status["agy_expected_account"] == expected
    text = (work / "logs/correct.log").read_text(encoding="utf-8")
    assert f"Antigravity 帳號已符合預期（{expected}）" in text
    if trigger == "resume-now":
        assert "收到 resume-now，立即檢查 Antigravity 登入的帳號" in text
        assert not (work / "resume-now").exists()
    else:
        assert f"預期的 Antigravity 帳號改為 {A}" in text


def test_startup_mismatch_pauses_before_any_call(run_env):
    args, env, tmp = run_env
    work = tmp / "work"
    work.mkdir()
    (work / "expected-account").write_text(C + "\n")  # 檔案優先於 --expected-agy-account
    (tmp / "keychain").write_text(A + "\n")
    process = start(args + ["--expected-agy-account", B], env)
    try:
        wait_for(lambda: saved(work)["agy_account_mismatch"])
        time.sleep(.5)
        assert count(tmp / "counter") == 0
        status = saved(work)
        assert status["agy_expected_account"] == C and status["agy_account"] == A
        (tmp / "keychain").write_text(C + "\n")
        (work / "resume-now").touch()
    finally:
        finish(process)
    text = (work / "logs/correct.log").read_text(encoding="utf-8")
    assert f"預期的 Antigravity 帳號：{C}" in text
    assert f"暫停 Antigravity：啟動檢查發現目前登入的帳號是 {A}，不是預期的 {C}" in text
    assert saved(work)["chunks_ok"] == CHUNKS and count(tmp / "counter") == CHUNKS


def test_relay_codex_takes_over_during_mismatch(run_env):
    args, env, tmp = run_env
    (tmp / "keychain").write_text(A + "\n")
    finish(start(args + ["--engines", "agy,codex", "--agy-jobs", "1", "--codex-jobs", "1",
                         "--expected-agy-account", C], env))
    assert count(tmp / "counter") == 0
    assert {chunk["engine"] for chunk in output_chunks(tmp)} == {"codex-cli"}
    status = saved(tmp / "work")
    assert status["chunks_ok"] == CHUNKS and status["agy_account_mismatch"] is True


def test_run_prunes_agy_logs(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "agy").symlink_to(ROOT / "tests/fake_agy.py")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(cli, "AGY_LOG_KEEP", 2)
    source = tmp_path / "in/影片/02 針灸/甲.rm.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(document(), ensure_ascii=False), encoding="utf-8")
    assert cli.main(["--in-dir", str(tmp_path / "in"), "--out-dir", str(tmp_path / "out"),
                     "--work-dir", str(tmp_path / "work"), "--jobs", "1"]) == 0
    logs = sorted((tmp_path / "work/agy-logs").glob("*.log"))
    assert len(logs) == 2 and all("-call-" in path.name for path in logs)
