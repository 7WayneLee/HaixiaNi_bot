"""校正核心、hook 與假 agy 的離線測試。"""
import copy
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from haixia import correction
from haixia.transcript import create, validate, validate_corrected
from scripts import correct_transcripts as cli

ROOT = Path(__file__).resolve().parents[1]


def document(count=8, spacing=80, text="麻黄湯主之"):
    engine = {"name": "whisper", "model": "large-v3", "version": "1", "params": {},
              "device": "cpu", "compute_type": "int8", "elapsed_sec": 1.0}
    segments = [{"start": float(i * spacing), "end": float(i * spacing + 8),
                 "text_raw": text, "text": text, "speaker": None,
                 "confidence": None, "low_confidence": False} for i in range(count)]
    return create("影片/02 針灸/甲.rm", count * spacing + 9, engine, segments)


def test_chunks_context_and_line_cap():
    doc = document(10, 50)
    chunks = correction.split_chunks(doc["segments"])
    assert [(c["start_index"], c["end_index"]) for c in chunks] == [(0, 5), (5, 10)]
    assert chunks[0]["after"] == [5]
    assert chunks[1]["before"] == [4]
    assert all(c["end_index"] - c["start_index"] <= 150 for c in correction.split_chunks(document(400, 1)["segments"]))


def test_prompt_rules_terms_hotwords_sections_and_unique_markers():
    doc = document(3, 1)
    doc["segments"][1]["start"] = 0.04
    doc["segments"][2]["start"] = 0.04
    chunk = correction.split_chunks(doc["segments"])[0]
    prompt, labels = correction.build_prompt(doc, chunk, 3)
    assert len(labels) == len(set(labels)) == 3
    assert "人紀・針灸" in prompt and "尾閭" in prompt and "黃耆" in prompt
    assert "足三里" in prompt and "## 前文（只供參考，不要輸出）" in prompt
    assert "## 中醫詞表\n睛明、攢竹" in prompt
    assert "## 要校正的行（共 3 行）" in prompt and "## 後文（只供參考，不要輸出）" in prompt
    assert "每段最多 3 次" in prompt
    assert prompt.index("## 前文") < prompt.index("## 要校正") < prompt.index("## 後文")


def test_parse_alignment_and_thresholds():
    labels = ["744.3", "750.0"]
    assert correction.parse_and_align("說明\n[743.3] 甲\n[750.0] 乙", labels) == ["甲", "乙"]
    assert correction.parse_and_align("[743.3] 甲", labels) == [None, None]
    segments = [{"text": "麻黃湯主之"}] * 20
    labels = [str(i) for i in range(20)]
    good = "\n".join(f"[{i}] 麻黃湯主之" for i in range(20))
    assert correction.evaluate(good, labels, segments)["valid"]
    assert not correction.evaluate("\n".join(good.splitlines()[:19]), labels, segments)["valid"]
    assert correction.evaluate("\n".join(good.splitlines()[:18]), labels, segments)["match_ratio"] == .9
    assert not correction.evaluate("\n".join(good.splitlines()[:18]), labels, segments)["valid"]
    assert not correction.evaluate("\n".join(f"[{i}] 長" for i in range(20)), labels, segments)["valid"]
    too_long = "\n".join(f"[{i}] " + ("長" * 30 if i < 2 else "麻黃湯主之") for i in range(20))
    score = correction.evaluate(too_long, labels, segments)
    assert score["anomalous"] == 2 and not score["valid"] and not score["admissible"]
    assert correction.choose_result([{"evaluation": score}], segments)[0] == "failed"
    blank = "\n".join(f"[{i}] " + ("" if i < 2 else "麻黃湯主之") for i in range(20))
    assert correction.evaluate(blank, labels, segments)["anomalous"] == 2


def test_partial_failed_fallback_and_schema():
    doc = document(20, 1)
    chunks = correction.split_chunks(doc["segments"])
    original = doc["segments"]
    labels = correction.markers(original)
    partial_output = "\n".join(f"[{label}] 麻黃湯主之" for label in labels[:19])
    partial = {"evaluation": correction.evaluate(partial_output, labels, original)}
    status, lines = correction.choose_result([partial], original)
    assert status == "partial" and lines[-1] == ("麻黄湯主之", False)
    failed = {"evaluation": correction.evaluate("\n".join(partial_output.splitlines()[:18]), labels, original)}
    assert correction.choose_result([failed], original)[0] == "failed"
    short = {"evaluation": correction.evaluate("\n".join(f"[{label}] 短" for label in labels), labels, original)}
    assert not short["evaluation"]["admissible"]
    assert correction.choose_result([short, partial], original)[0] == "partial"
    assert correction.choose_result([short], original)[0] == "failed"
    result = {"lines": lines, "start": chunks[0]["start"], "end": chunks[0]["end"],
              "status": status, "attempts": 2, "searches": 1, "elapsed_sec": 5}
    corrected = correction.corrected_document(doc, chunks, [result], "gemini-3.8-flash-high", 5,
                                               correction.prompt_sha256())
    validate_corrected(corrected)
    assert corrected["correction"]["chunks"][0]["fallback_lines"] == 1
    with pytest.raises(ValueError):
        validate(corrected)
    broken = copy.deepcopy(corrected)
    broken["segments"][0]["corrected"] = "true"
    with pytest.raises(ValueError):
        validate_corrected(broken)
    broken = copy.deepcopy(corrected)
    broken["correction"]["chunks"][0]["fallback_lines"] = -1
    with pytest.raises(ValueError):
        validate_corrected(broken)
    broken = copy.deepcopy(corrected)
    broken["segments"][-1]["text"] = "不一致"
    with pytest.raises(ValueError):
        validate_corrected(broken)


@pytest.mark.parametrize("replacement", ["", "長" * 30])
def test_single_anomalous_line_uses_asr(replacement):
    doc = document(20, 1)
    labels = correction.markers(doc["segments"])
    output = "\n".join(f"[{label}] {replacement if index == 0 else '麻黃湯主之'}"
                       for index, label in enumerate(labels))
    score = correction.evaluate(output, labels, doc["segments"])
    assert score["valid"] and score["anomalous_indices"] == [0]
    status, lines = correction.choose_result([{"evaluation": score}], doc["segments"])
    assert status == "ok" and lines[0] == ("麻黄湯主之", False)
    assert all(corrected for _, corrected in lines[1:])


def test_gate_enforces_search_limit(tmp_path):
    gate = tmp_path / "gate.py"
    gate.write_bytes((ROOT / "tools/agy_hook/gate.py").read_bytes())
    env = dict(os.environ, HAIXIA_AGY_MAX_SEARCH="2", HAIXIA_AGY_LABEL="測試")
    def call(name):
        payload = {"conversationId": "case", "toolCall": {"name": name}}
        result = subprocess.run([sys.executable, str(gate)], input=json.dumps(payload),
                                text=True, capture_output=True, env=env, check=True)
        return json.loads(result.stdout)["decision"]
    assert [call("search_web") for _ in range(3)] == ["allow", "allow", "deny"]
    assert all(call(name) == "deny" for name in ("read_url_content", "run_command", "view_file"))


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "agy").symlink_to(ROOT / "tests/fake_agy.py")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HAIXIA_BACKOFF_BASE_SEC", "0.2")
    monkeypatch.setenv("HAIXIA_BACKOFF_MAX_SEC", "0.2")
    input_dir = tmp_path / "input"
    relative = Path("影片/02 針灸/甲.rm.json")
    (input_dir / relative).parent.mkdir(parents=True)
    (input_dir / relative).write_text(json.dumps(document(4, 130), ensure_ascii=False), encoding="utf-8")
    args = ["--in-dir", str(input_dir), "--out-dir", str(tmp_path / "output"),
            "--work-dir", str(tmp_path / "work"), "--jobs", "2"]
    return args, tmp_path / "output" / relative, tmp_path


def test_cli_end_to_end_resume_and_cache_invalidation(fake_env, monkeypatch):
    args, output, tmp = fake_env
    flag = tmp / "calls"
    monkeypatch.setenv("FAKE_AGY_FLAG", str(flag))
    assert cli.main(args) == 0
    first = flag.read_text()
    corrected = validate_corrected(json.loads(output.read_text(encoding="utf-8")))
    assert all(chunk["status"] == "ok" for chunk in corrected["correction"]["chunks"])
    assert corrected["segments"][0]["text"] == "麻黃湯主之"
    assert cli.main(args) == 0 and flag.read_text() == first
    assert cli.main(args + ["--force"]) == 0 and len(flag.read_text()) > len(first)
    second = flag.read_text()
    monkeypatch.setattr(correction, "PROMPT_HEAD", correction.PROMPT_HEAD + "\n新增規則")
    output.unlink()
    assert cli.main(args) == 0 and len(flag.read_text()) > len(second)


def test_cli_quota_pause_without_failure(fake_env, monkeypatch):
    args, output, tmp = fake_env
    monkeypatch.setenv("FAKE_AGY_MODE", "quota_once")
    monkeypatch.setenv("FAKE_AGY_FLAG", str(tmp / "quota.flag"))
    assert cli.main(args + ["--jobs", "1"]) == 0
    doc = validate_corrected(json.loads(output.read_text(encoding="utf-8")))
    assert doc["correction"]["chunks"][0]["attempts"] == 2
    assert all(chunk["status"] == "ok" for chunk in doc["correction"]["chunks"])
    assert "暫停：Antigravity 額度用完或被限速" in (tmp / "work/logs/correct.log").read_text()
    assert json.loads((tmp / "work/status.json").read_text())["chunks_failed"] == 0


def test_cli_missing_line_retries_then_partial(fake_env, monkeypatch):
    args, output, tmp = fake_env
    source_file = tmp / "input" / "影片/02 針灸/甲.rm.json"
    source_file.write_text(json.dumps(document(20, 1), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("FAKE_AGY_COUNTER", str(tmp / "counter"))
    monkeypatch.setenv("FAKE_AGY_MODE", "missing_once")
    assert cli.main(args + ["--retries", "1"]) == 0
    corrected = validate_corrected(json.loads(output.read_text()))
    assert corrected["correction"]["chunks"][0]["status"] == "ok"
    assert corrected["correction"]["chunks"][0]["attempts"] == 2
    monkeypatch.setenv("FAKE_AGY_MODE", "missing")
    assert cli.main(args + ["--retries", "1", "--force"]) == 0
    corrected = validate_corrected(json.loads(output.read_text()))
    chunk = corrected["correction"]["chunks"][0]
    assert chunk["status"] == "partial" and chunk["attempts"] == 2 and chunk["fallback_lines"] == 1
    assert corrected["segments"][-1]["corrected"] is False


def test_cli_rejects_short_attempt_and_anomalous_fallback(fake_env, monkeypatch):
    args, output, tmp = fake_env
    source_file = tmp / "input" / "影片/02 針灸/甲.rm.json"
    source_file.write_text(json.dumps(document(20, 1), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("FAKE_AGY_MODE", "short")
    assert cli.main(args + ["--retries", "0"]) == 1
    corrected = validate_corrected(json.loads(output.read_text()))
    assert corrected["correction"]["chunks"][0]["status"] == "failed"
    assert corrected["correction"]["chunks"][0]["fallback_lines"] == 20
    monkeypatch.setenv("FAKE_AGY_MODE", "long_one")
    assert cli.main(args + ["--retries", "0", "--force"]) == 0
    corrected = validate_corrected(json.loads(output.read_text()))
    assert corrected["correction"]["chunks"][0]["status"] == "ok"
    assert corrected["correction"]["chunks"][0]["fallback_lines"] == 1
    assert corrected["segments"][0]["text"] == corrected["segments"][0]["text_asr"]
    assert corrected["segments"][0]["corrected"] is False


@pytest.mark.parametrize("mode,expected_log", [
    ("empty_once", "沒有可辨識的時間標記行"),
    ("quota_stdout_once", "暫停：Antigravity 額度用完或被限速"),
])
def test_cli_empty_output_and_stdout_quota(fake_env, monkeypatch, mode, expected_log):
    args, output, tmp = fake_env
    monkeypatch.setenv("FAKE_AGY_MODE", mode)
    monkeypatch.setenv("FAKE_AGY_COUNTER", str(tmp / "counter"))
    assert cli.main(args + ["--jobs", "1", "--retries", "1"]) == 0
    corrected = validate_corrected(json.loads(output.read_text()))
    assert corrected["correction"]["chunks"][0]["attempts"] == 2
    assert expected_log in (tmp / "work/logs/correct.log").read_text()


def test_cli_circuit_breaker_and_no_escalation_during_pause(fake_env, monkeypatch):
    args, output, tmp = fake_env
    monkeypatch.setenv("FAKE_AGY_MODE", "error_three_then_normal")
    monkeypatch.setenv("FAKE_AGY_COUNTER", str(tmp / "counter"))
    assert cli.main(args + ["--jobs", "1"]) == 0
    corrected = validate_corrected(json.loads(output.read_text()))
    assert corrected["correction"]["chunks"][0]["attempts"] == 4
    assert "暫停：連續 3 次呼叫失敗" in (tmp / "work/logs/correct.log").read_text()
    source_file = tmp / "input" / "影片/02 針灸/甲.rm.json"
    source_file.write_text(json.dumps(document(8, 130), ensure_ascii=False), encoding="utf-8")
    (tmp / "counter").unlink()
    monkeypatch.setenv("FAKE_AGY_MODE", "quota_first_four_then_normal")
    monkeypatch.setenv("HAIXIA_BACKOFF_BASE_SEC", "1")
    monkeypatch.setenv("HAIXIA_BACKOFF_MAX_SEC", "8")
    assert cli.main(args + ["--jobs", "4", "--force"]) == 0
    log = (tmp / "work/logs/correct.log").read_text()
    assert log.count("暫停：Antigravity 額度用完或被限速") == 1


def test_cli_retry_failed_only_bad_chunk(fake_env, monkeypatch):
    args, output, tmp = fake_env
    calls = tmp / "calls.log"
    monkeypatch.setenv("FAKE_AGY_CALL_LOG", str(calls))
    monkeypatch.setenv("FAKE_AGY_BAD_CHUNK", "2")
    assert cli.main(args + ["--retries", "0"]) == 1
    first = validate_corrected(json.loads(output.read_text()))
    assert [chunk["status"] for chunk in first["correction"]["chunks"]] == ["ok", "failed"]
    assert "仍有失敗段落的檔案：影片/02 針灸/甲.rm" in (tmp / "work/logs/correct.log").read_text()
    cli.cache_path(tmp / "work", first["source"], 1).unlink()
    monkeypatch.delenv("FAKE_AGY_BAD_CHUNK")
    assert cli.main(args + ["--retries", "0", "--retry-failed"]) == 0
    second = validate_corrected(json.loads(output.read_text()))
    assert [chunk["status"] for chunk in second["correction"]["chunks"]] == ["ok", "ok"]
    called = calls.read_text().splitlines()
    assert len(called) == 3 and "#2#" in called[-1]
    cache = cli.cache_path(tmp / "work", first["source"], 2)
    assert len(list(cache.parent.glob("00002.attempt*.json"))) == 2


def test_cli_retry_partial_only(fake_env, monkeypatch):
    args, output, tmp = fake_env
    source_file = tmp / "input" / "影片/02 針灸/甲.rm.json"
    source_file.write_text(json.dumps(document(20, 1), ensure_ascii=False), encoding="utf-8")
    calls = tmp / "calls.log"
    monkeypatch.setenv("FAKE_AGY_CALL_LOG", str(calls))
    monkeypatch.setenv("FAKE_AGY_MODE", "missing")
    assert cli.main(args + ["--retries", "0"]) == 0
    assert json.loads(output.read_text())["correction"]["chunks"][0]["status"] == "partial"
    monkeypatch.setenv("FAKE_AGY_MODE", "normal")
    assert cli.main(args + ["--retry-failed", "--retries", "0"]) == 0
    assert len(calls.read_text().splitlines()) == 2
    assert json.loads(output.read_text())["correction"]["chunks"][0]["status"] == "ok"


def test_cli_empty_transcript(fake_env):
    args, output, tmp = fake_env
    original = document(0)
    source_file = tmp / "input" / (original["source"] + ".json")
    source_file.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    assert cli.main(args) == 0
    corrected = validate_corrected(json.loads(output.read_text()))
    assert corrected["segments"] == [] and corrected["correction"]["chunks"] == []


def test_fake_agy_missing_and_timeout(fake_env, monkeypatch):
    args, output, tmp = fake_env
    monkeypatch.setenv("FAKE_AGY_MODE", "missing")
    assert cli.main(args + ["--retries", "0"]) == 1
    doc = validate_corrected(json.loads(output.read_text()))
    assert any(chunk["status"] == "failed" for chunk in doc["correction"]["chunks"])
    assert all(not segment["corrected"] for segment in doc["segments"])
    monkeypatch.setenv("FAKE_AGY_MODE", "timeout")
    monkeypatch.setenv("HAIXIA_AGY_TIMEOUT_GRACE_SEC", "0.1")
    assert cli.main(args + ["--retries", "0", "--print-timeout", "1", "--force"]) == 1
    assert any("逾時" in path.read_text() for path in (tmp / "work/chunks").rglob("*.attempt*.json"))


def test_cli_ctrl_c(fake_env, monkeypatch):
    args, output, tmp = fake_env
    marker = tmp / "agy-started"
    env = dict(os.environ, FAKE_AGY_MODE="hang_ignore_term", FAKE_AGY_STARTED=str(marker))
    process = subprocess.Popen([sys.executable, str(ROOT / "scripts/correct_transcripts.py"), *args,
                                "--jobs", "1"], env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    status = tmp / "work/status.json"
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert marker.exists() and status.exists()
    child_pid = int(marker.read_text())
    began = time.monotonic()
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=8) == 2
    assert time.monotonic() - began < 7
    saved = json.loads(status.read_text())
    assert saved["state"] == "aborted" and saved["chunks_done"] == 0
    assert not output.exists()
    assert not list((tmp / "work/chunks").rglob("*.json"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
