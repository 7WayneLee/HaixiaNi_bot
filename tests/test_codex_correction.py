"""Codex 離線呼叫、額度與雙引擎排程。"""
import json
import os
from pathlib import Path

import pytest

from haixia.transcript import create, validate_corrected
from scripts import correct_transcripts as cli

ROOT = Path(__file__).resolve().parents[1]


def document(count=4, spacing=130):
    engine = {"name": "whisper", "model": "large-v3", "version": "1", "params": {},
              "device": "cpu", "compute_type": "int8", "elapsed_sec": 1.0}
    segments = [{"start": float(i * spacing), "end": float(i * spacing + 8),
                 "text_raw": "麻黄湯主之", "text": "麻黄湯主之", "speaker": None,
                 "confidence": None, "low_confidence": False} for i in range(count)]
    return create("影片/02 針灸/甲.rm", count * spacing + 9, engine, segments)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("codex", "fake_codex.py"), ("orca", "fake_orca_account.py"),
                         ("agy", "fake_agy.py")):
        target = ROOT / "tests" / script
        target.chmod(0o755)
        (bin_dir / name).symlink_to(target)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HAIXIA_BACKOFF_BASE_SEC", "0.2")
    monkeypatch.setenv("HAIXIA_BACKOFF_MAX_SEC", "0.2")
    source = tmp_path / "in/影片/02 針灸/甲.rm.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(document(), ensure_ascii=False))
    args = ["--in-dir", str(tmp_path / "in"), "--out-dir", str(tmp_path / "out"),
            "--work-dir", str(tmp_path / "work"), "--engines", "codex",
            "--codex-jobs", "1"]
    return args, tmp_path / "out/影片/02 針灸/甲.rm.json", tmp_path


@pytest.mark.parametrize("mode,attempts", [("normal", 1), ("missing_once", 2),
                                           ("disconnect_once", 2), ("usage_limit_once", 2)])
def test_codex_attempts(fake, monkeypatch, mode, attempts):
    args, output, tmp = fake
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    monkeypatch.setenv("FAKE_CODEX_COUNTER", str(tmp / "counter"))
    monkeypatch.setenv("FAKE_ORCA_RESET_OFFSET", "-121")
    assert cli.main(args) == 0
    doc = validate_corrected(json.loads(output.read_text()))
    assert all(item["engine"] == "codex-cli" and item["model"] == "gpt-6-sol"
               and item["effort"] == "medium" and item["status"] == "ok"
               for item in doc["correction"]["chunks"])
    assert doc["correction"]["chunks"][0]["attempts"] == attempts
    assert doc["correction"]["tool"] == "codex-cli"
    attempt = next((tmp / "work/chunks").rglob("*.attempt1.json"))
    if mode not in {"disconnect_once", "usage_limit_once"}:
        assert json.loads(attempt.read_text())["usage"]["input_tokens"] == 100
    if mode == "disconnect_once":
        for path in (tmp / "work/chunks").rglob("*.attempt*.json"):
            assert "sk-ABC" not in path.read_text()
        assert "sk-ABC" not in (tmp / "work/logs/correct.log").read_text()
        assert "sk-ABC" not in (tmp / "work/status.json").read_text()


def test_codex_timeout(fake, monkeypatch):
    args, output, tmp = fake
    monkeypatch.setenv("FAKE_CODEX_MODE", "timeout")
    monkeypatch.setenv("HAIXIA_AGY_TIMEOUT_GRACE_SEC", "0.1")
    assert cli.main(args + ["--retries", "0", "--print-timeout", "1"]) == 1
    assert validate_corrected(json.loads(output.read_text()))["correction"]["chunks"][0]["status"] == "failed"


@pytest.mark.parametrize("session,weekly,expected", [(90, 10, "paused"), (10, 90, "stopped")])
def test_codex_quota_controls(fake, monkeypatch, session, weekly, expected):
    _args, _output, tmp = fake
    monkeypatch.setenv("FAKE_ORCA_SESSION", str(session))
    monkeypatch.setenv("FAKE_ORCA_WEEKLY", str(weekly))
    shared = cli.SharedState(tmp / "quota", 1, 1.0)
    state = cli.CodexState(shared, __import__("logging").getLogger("fake"), 80, 85)
    shared.agy_enabled = False
    shared.engines["codex"] = state
    assert not state.check_quota()
    assert state.state == expected
    saved = json.loads((tmp / "quota/status.json").read_text())
    assert saved["engines"]["codex"]["weekly_used_percent"] == weekly


def test_missing_quota_pauses_fifteen_minutes(fake, monkeypatch):
    _args, _output, tmp = fake
    monkeypatch.setenv("FAKE_ORCA_MISSING", "1")
    shared = cli.SharedState(tmp / "quota", 1, 1.0)
    state = cli.CodexState(shared, __import__("logging").getLogger("fake"), 80, 85)
    shared.engines["codex"] = state
    before = cli.time.time()
    assert not state.check_quota()
    assert state.state == "paused" and state.pause_until >= before + 900


def test_legacy_validation_and_redaction(fake):
    args, output, tmp = fake
    assert cli.main(args) == 0
    doc = json.loads(output.read_text())
    for chunk in doc["correction"]["chunks"]:
        for field in ("engine", "model", "effort"):
            del chunk[field]
    doc["correction"]["tool"] = "antigravity-cli"
    validate_corrected(doc)
    assert cli.redact("Incorrect API key provided: sk-ABC*123") == "Incorrect API key provided: sk-***"


def test_relay_requeues_quota_chunk_and_agy_resumes(fake, monkeypatch):
    args, output, tmp = fake
    monkeypatch.setenv("FAKE_AGY_MODE", "quota_once")
    monkeypatch.setenv("FAKE_AGY_FLAG", str(tmp / "agy.flag"))
    monkeypatch.setenv("FAKE_CODEX_MODE", "sleep")
    monkeypatch.setenv("HAIXIA_BACKOFF_BASE_SEC", "0.3")
    monkeypatch.setenv("HAIXIA_BACKOFF_MAX_SEC", "0.3")
    both = args[:]
    both[both.index("codex") + 0] = "agy,codex"
    assert cli.main(both + ["--agy-jobs", "1"]) == 0
    doc = validate_corrected(json.loads(output.read_text()))
    assert {chunk["engine"] for chunk in doc["correction"]["chunks"]} == {"antigravity-cli", "codex-cli"}
    assert doc["correction"]["tool"] == "mixed"
    assert len(doc["correction"]["chunks"]) == 2
    status = json.loads((tmp / "work/status.json").read_text())
    assert status["codex_mode"] == "relay" and status["chunks_done"] == 2


def test_parallel_workers_both_take_chunks(fake, monkeypatch):
    args, output, tmp = fake
    monkeypatch.setenv("FAKE_AGY_MODE", "sleep")
    both = args[:]
    both[both.index("codex")] = "agy,codex"
    assert cli.main(both + ["--agy-jobs", "1", "--codex-mode", "parallel"]) == 0
    doc = validate_corrected(json.loads(output.read_text()))
    assert {chunk["engine"] for chunk in doc["correction"]["chunks"]} == {"antigravity-cli", "codex-cli"}
    assert json.loads((tmp / "work/status.json").read_text())["chunks_done"] == 2


def test_codex_quota_pause_does_not_stop_agy(fake, monkeypatch):
    args, output, tmp = fake
    monkeypatch.setenv("FAKE_ORCA_SESSION", "90")
    both = args[:]
    both[both.index("codex")] = "agy,codex"
    assert cli.main(both + ["--agy-jobs", "1", "--codex-mode", "parallel"]) == 0
    doc = validate_corrected(json.loads(output.read_text()))
    assert all(chunk["engine"] == "antigravity-cli" for chunk in doc["correction"]["chunks"])
    assert json.loads((tmp / "work/status.json").read_text())["engines"]["codex"]["state"] == "paused"
