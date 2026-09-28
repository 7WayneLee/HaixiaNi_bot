"""重做模式（--redo-engine）的離線測試：選檔、安全檢查、取代規則、進度與中斷。"""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from haixia import correction
from haixia.transcript import create, save_corrected, validate_corrected
from scripts import correct_transcripts as cli

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gemini-3.8-flash-high"
CLAUDE = {"engine": "claude-cli", "model": "claude-opus-5-5", "effort": "medium"}
CODEX = {"engine": "codex-cli", "model": "gpt-6-sol", "effort": "medium"}
AGY = {"engine": "antigravity-cli", "model": MODEL, "effort": None}


def asr(source, count=8, spacing=130):
    """每兩個 segment 切成一段（130 秒間隔、四分鐘一段），count=8 就是 4 段。"""
    engine = {"name": "whisper", "model": "large-v3", "version": "1", "params": {},
              "device": "cpu", "compute_type": "int8", "elapsed_sec": 1.0}
    segments = [{"start": float(i * spacing), "end": float(i * spacing + 8),
                 "text_raw": "麻黄湯主之", "text": "麻黄湯主之", "speaker": None,
                 "confidence": None, "low_confidence": False} for i in range(count)]
    return create(source, count * spacing + 9, engine, segments)


def corrected(document, engines, replaced=None, max_search=5):
    """依 engines（每段一個引擎設定）做出既有的校正版；文字標上引擎名，方便辨認有沒有被改。"""
    chunks = correction.split_chunks(document["segments"])
    results = []
    for number, (chunk, engine) in enumerate(zip(chunks, engines), 1):
        count = chunk["end_index"] - chunk["start_index"]
        result = {"start": chunk["start"], "end": chunk["end"], "status": "ok", "attempts": 1,
                  "searches": number, "elapsed_sec": 10.0 + number,
                  "lines": [(f'{engine["engine"]}版{number}', True)] * count, **engine}
        if replaced and number in replaced:
            result["replaced"] = replaced[number]
        results.append(result)
    return correction.corrected_document(document, chunks, results, MODEL, max_search, correction.prompt_sha256())


@pytest.fixture
def env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("agy", "fake_agy.py"), ("codex", "fake_codex.py"),
                         ("claude", "fake_claude.py"), ("orca", "fake_orca_account.py")):
        target = ROOT / "tests" / script
        target.chmod(0o755)
        (bin_dir / name).symlink_to(target)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HAIXIA_BACKOFF_BASE_SEC", "0.2")
    monkeypatch.setenv("HAIXIA_BACKOFF_MAX_SEC", "0.2")
    monkeypatch.setenv("FAKE_AGY_CALL_LOG", str(tmp_path / "agy.calls"))
    args = ["--in-dir", str(tmp_path / "in"), "--out-dir", str(tmp_path / "out"),
            "--work-dir", str(tmp_path / "work"), "--engines", "agy", "--jobs", "1",
            "--redo-engine", "claude-cli"]
    return args, tmp_path


def put(tmp, source, engines=None, replaced=None, document=None, max_search=5):
    """寫入 ASR 原檔；有 engines 時一併寫入既有校正版。回傳校正版路徑。"""
    document = document or asr(source)
    path = tmp / "in" / f"{source}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    output = tmp / "out" / f"{source}.json"
    if engines is not None:
        save_corrected(corrected(document, engines, replaced, max_search), output)
    return output


def agy_calls(tmp):
    """fake agy 收到的 (source, 段號)。"""
    path = tmp / "agy.calls"
    labels = path.read_text().splitlines() if path.exists() else []
    return sorted((label.split("#")[0], int(label.split("#")[1])) for label in labels)


def load(path):
    return validate_corrected(json.loads(path.read_text(encoding="utf-8")))


def log_text(tmp):
    return (tmp / "work/logs/correct.log").read_text(encoding="utf-8")


def test_redo_replaces_only_target_chunks_and_records_replaced(env):
    args, tmp = env
    old_replaced = {"engine": "antigravity-cli", "model": MODEL, "effort": None, "searches": 2, "elapsed_sec": 7.5}
    target = put(tmp, "影片/02 針灸/甲.rm", [AGY, CLAUDE, CODEX, CLAUDE], replaced={3: old_replaced, 4: old_replaced})
    only_agy = put(tmp, "影片/02 針灸/乙.rm", [AGY, AGY, CODEX, AGY])
    put(tmp, "影片/02 針灸/丙.rm")
    before = load(target)
    untouched = only_agy.read_bytes()
    assert cli.main(args) == 0
    assert agy_calls(tmp) == [("影片/02 針灸/甲.rm", 2), ("影片/02 針灸/甲.rm", 4)]
    assert only_agy.read_bytes() == untouched
    assert not (tmp / "out/影片/02 針灸/丙.rm.json").exists()
    after = load(target)
    old_chunks, new_chunks = before["correction"]["chunks"], after["correction"]["chunks"]
    # 非目標段：metadata（含既有的 replaced）與文字原封不動。
    for number in (1, 3):
        assert new_chunks[number - 1] == old_chunks[number - 1]
    assert new_chunks[2]["replaced"] == old_replaced
    chunks = correction.split_chunks(after["segments"])
    for number in (1, 3):
        span = slice(chunks[number - 1]["start_index"], chunks[number - 1]["end_index"])
        assert after["segments"][span] == before["segments"][span]
    # 目標段：換成 Antigravity 的結果，replaced 記下被取代的 Claude 版（不是更早的那一版）。
    for number in (2, 4):
        item = new_chunks[number - 1]
        assert item["engine"] == "antigravity-cli" and item["model"] == MODEL and item["status"] == "ok"
        assert item["replaced"] == {"engine": "claude-cli", "model": "claude-opus-5-5", "effort": "medium",
                                    "searches": number, "elapsed_sec": 10.0 + number}
        span = slice(chunks[number - 1]["start_index"], chunks[number - 1]["end_index"])
        assert all(segment["text"] == "麻黃湯主之" for segment in after["segments"][span])
    assert after["correction"]["tool"] == "mixed"
    assert after["correction"]["model"] == MODEL
    assert after["correction"]["created_at"] >= before["correction"]["created_at"]
    assert "重做：取代 2 段、保留原版 0 段、未完成 0 段" in log_text(tmp)
    # 再跑一次：已經沒有 Claude 段，不再呼叫。
    assert cli.main(args) == 0 and len(agy_calls(tmp)) == 2
    # 全部換成 Antigravity 時 tool 重算成 antigravity-cli；被取代過的段再被重做，replaced 換成新的前一版。
    assert cli.main(args[:-1] + ["codex-cli", "--engines", "agy"]) == 0
    final = load(target)
    assert final["correction"]["tool"] == "antigravity-cli"
    assert final["correction"]["chunks"][2]["replaced"]["engine"] == "codex-cli"
    assert final["correction"]["chunks"][1] == new_chunks[1]


def test_status_counts_only_target_chunks(env):
    args, tmp = env
    put(tmp, "影片/02 針灸/甲.rm", [AGY, CLAUDE, AGY, CLAUDE])
    put(tmp, "影片/02 針灸/乙.rm", [CLAUDE, AGY, AGY, AGY])
    put(tmp, "影片/02 針灸/丙.rm", [AGY, AGY, AGY, AGY])
    assert cli.main(args) == 0
    status = json.loads((tmp / "work/status.json").read_text())
    document = asr("影片/02 針灸/甲.rm")
    chunks = correction.split_chunks(document["segments"])
    hours = sum(cli.chunk_audio_hours(document, chunks, number) for number in (1, 2, 4))
    assert status["chunks_total"] == status["chunks_done"] == status["chunks_ok"] == 3
    assert status["files_total"] == status["files_done"] == 2
    assert status["audio_hours_total"] == pytest.approx(hours)
    assert status["audio_hours_done"] == pytest.approx(hours, abs=1e-4)
    assert status["state"] == "finished"


def fake_result(status):
    def run(job, args, ws, shared, log, digest, engine="agy"):
        source, document, number, chunk, existing = job
        assert existing is None
        if status == "exception":
            raise RuntimeError("意外錯誤")
        count = chunk["end_index"] - chunk["start_index"]
        return {"source": source, "chunk_number": number, "start": chunk["start"], "end": chunk["end"],
                "status": status, "attempts": 3, "searches": 1, "elapsed_sec": 1.0,
                "lines": [("新版", status != "failed")] * count, "fallback_lines": 0,
                "engine": "antigravity-cli", "model": MODEL, "effort": None}
    return run


@pytest.mark.parametrize("status", ["partial", "failed", "exception"])
def test_non_ok_redo_keeps_original(env, monkeypatch, status):
    args, tmp = env
    output = put(tmp, "影片/02 針灸/甲.rm", [AGY, CLAUDE, AGY, AGY])
    before = output.read_text(encoding="utf-8")
    monkeypatch.setattr(cli, "correct_chunk", fake_result(status))
    assert cli.main(args) == 1
    old, new = json.loads(before), load(output)
    assert new["segments"] == old["segments"]
    assert new["correction"]["chunks"] == old["correction"]["chunks"]
    assert "replaced" not in new["correction"]["chunks"][1]
    assert "保留原本 claude-cli 的版本" in log_text(tmp)
    assert "重做：取代 0 段、保留原版 1 段、未完成 0 段" in log_text(tmp)


def test_real_failed_redo_keeps_original(env, monkeypatch):
    args, tmp = env
    output = put(tmp, "影片/02 針灸/甲.rm", [CLAUDE, AGY, AGY, AGY])
    before = json.loads(output.read_text(encoding="utf-8"))
    monkeypatch.setenv("FAKE_AGY_MODE", "missing")
    assert cli.main(args + ["--retries", "0"]) == 1
    assert agy_calls(tmp) == [("影片/02 針灸/甲.rm", 1)]
    after = load(output)
    assert after["segments"] == before["segments"]
    assert after["correction"]["chunks"] == before["correction"]["chunks"]
    status = json.loads((tmp / "work/status.json").read_text())
    assert status["chunks_failed"] == 1 and status["chunks_total"] == 1


@pytest.mark.parametrize("change", ["asr_text", "asr_time", "boundary", "prompt", "max_search"])
def test_mismatch_skips_whole_file(env, change):
    args, tmp = env
    document = asr("影片/02 針灸/甲.rm")
    output = put(tmp, "影片/02 針灸/甲.rm", [AGY, CLAUDE, AGY, CLAUDE], document=document)
    other = put(tmp, "影片/02 針灸/乙.rm", [AGY, CLAUDE, AGY, AGY])
    changed = copy.deepcopy(document)
    if change == "asr_text":
        changed["segments"][5]["text"] = "桂枝湯主之"
    elif change == "asr_time":
        changed["segments"][5]["end"] += 1
    elif change == "boundary":
        # 多一個 segment 讓重新切段的段數與起訖跟校正版不同。
        changed["segments"].append(dict(changed["segments"][-1], start=1050.0, end=1052.0))
        changed["duration_sec"] = 1060.0
    if change in {"asr_text", "asr_time", "boundary"}:
        (tmp / "in/影片/02 針灸/甲.rm.json").write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    else:
        saved = json.loads(output.read_text(encoding="utf-8"))
        if change == "prompt":
            saved["correction"]["prompt_sha256"] = "0" * 64
        else:
            saved["correction"]["max_search"] = 3
        output.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    before = output.read_bytes()
    assert cli.main(args) == 1
    assert output.read_bytes() == before
    assert agy_calls(tmp) == [("影片/02 針灸/乙.rm", 2)]
    assert "重做安全檢查不符，整檔略過" in log_text(tmp)


@pytest.mark.parametrize("extra,message", [
    (["--engines", "agy,claude"], "--engines 不能包含要重做的引擎"),
    (["--force"], "--force"),
    (["--retry-failed"], "--retry-failed"),
    (["--limit-hours", "1"], "--limit-hours"),
    (["--redo-limit", "0"], "--redo-limit"),
])
def test_argument_errors(env, capsys, extra, message):
    args, _tmp = env
    with pytest.raises(SystemExit):
        cli.main(args + extra)
    assert message in capsys.readouterr().err


def test_redo_limit_requires_redo_engine(env, capsys):
    args, _tmp = env
    with pytest.raises(SystemExit):
        cli.main(args[:-2] + ["--redo-limit", "2"])
    assert "--redo-limit" in capsys.readouterr().err


def test_redo_limit_and_include(env):
    args, tmp = env
    first = put(tmp, "影片/02 針灸/甲.rm", [CLAUDE, CLAUDE, AGY, CLAUDE])
    second = put(tmp, "影片/03 本草/乙.rm", [CLAUDE, AGY, AGY, AGY])
    untouched = second.read_bytes()
    assert cli.main(args + ["--redo-limit", "2"]) == 0
    assert agy_calls(tmp) == [("影片/02 針灸/甲.rm", 1), ("影片/02 針灸/甲.rm", 2)]
    assert [item["engine"] for item in load(first)["correction"]["chunks"]] == [
        "antigravity-cli", "antigravity-cli", "antigravity-cli", "claude-cli"]
    assert second.read_bytes() == untouched
    assert json.loads((tmp / "work/status.json").read_text())["chunks_total"] == 2
    assert cli.main(args + ["--include", "影片/03"]) == 0
    assert agy_calls(tmp)[-1] == ("影片/03 本草/乙.rm", 1) and len(agy_calls(tmp)) == 3
    assert load(first)["correction"]["chunks"][3]["engine"] == "claude-cli"
    assert cli.main(args) == 0
    assert len(agy_calls(tmp)) == 4 and load(first)["correction"]["chunks"][3]["engine"] == "antigravity-cli"


def test_interrupted_file_is_not_written(env, monkeypatch):
    args, tmp = env
    output = put(tmp, "影片/02 針灸/甲.rm", [CLAUDE, AGY, CLAUDE, AGY])
    before = output.read_bytes()
    real = fake_result("ok")

    def stop_on_second(job, args, ws, shared, log, digest, engine="agy"):
        if job[2] == 3:
            shared.stop.set()
            return None
        return real(job, args, ws, shared, log, digest, engine)

    monkeypatch.setattr(cli, "correct_chunk", stop_on_second)
    assert cli.main(args) == 1
    assert output.read_bytes() == before
    assert "重做：取代 1 段、保留原版 0 段、未完成 1 段" in log_text(tmp)
    monkeypatch.setattr(cli, "correct_chunk", real)
    assert cli.main(args) == 0
    assert [item["engine"] for item in load(output)["correction"]["chunks"]] == ["antigravity-cli"] * 4


def test_dry_run_writes_only_target_prompts(env):
    args, tmp = env
    output = put(tmp, "影片/02 針灸/甲.rm", [AGY, CLAUDE, AGY, CLAUDE])
    before = output.read_bytes()
    assert cli.main(args + ["--dry-run"]) == 0
    prompts = sorted(path.name for path in (tmp / "work/prompts").rglob("*.txt"))
    assert prompts == ["00002.txt", "00004.txt"]
    assert "重做：1 檔、2 段、" in log_text(tmp)
    assert agy_calls(tmp) == [] and output.read_bytes() == before


def test_cached_target_engine_result_is_not_reused(env):
    args, tmp = env
    source = "影片/02 針灸/甲.rm"
    output = put(tmp, source, [CLAUDE, AGY, AGY, AGY])
    document = asr(source)
    chunk = correction.split_chunks(document["segments"])[0]
    prompt, _ = correction.build_prompt(document, chunk, 5)
    key = hashlib.sha256((prompt + "\0" + MODEL + "\0" + correction.prompt_sha256()).encode()).hexdigest()
    lines = [("快取版", True)] * (chunk["end_index"] - chunk["start_index"])
    cli.atomic_json(cli.cache_path(tmp / "work", source, 1), {"key": key, "result": {
        "source": source, "chunk_number": 1, "start": chunk["start"], "end": chunk["end"], "status": "ok",
        "attempts": 1, "searches": 0, "elapsed_sec": 1.0, "lines": lines, "fallback_lines": 0, **CLAUDE}})
    assert cli.main(args) == 0
    assert agy_calls(tmp) == [(source, 1)]
    assert load(output)["segments"][0]["text"] == "麻黃湯主之"


def test_cached_partial_is_redone_in_redo_mode(env, monkeypatch):
    args, tmp = env
    source = "影片/02 針灸/甲.rm"
    output = put(tmp, source, [CLAUDE, AGY, AGY, AGY])
    monkeypatch.setenv("FAKE_AGY_MODE", "missing")
    assert cli.main(args + ["--retries", "0"]) == 1
    assert load(output)["correction"]["chunks"][0]["engine"] == "claude-cli"
    monkeypatch.delenv("FAKE_AGY_MODE")
    assert cli.main(args) == 0
    assert len(agy_calls(tmp)) == 2
    assert load(output)["correction"]["chunks"][0]["replaced"]["engine"] == "claude-cli"


def test_corrected_document_keeps_replaced_and_validation():
    document = asr("影片/02 針灸/甲.rm")
    replaced = {"engine": "claude-cli", "model": "claude-opus-5-5", "effort": "medium",
                "searches": 0, "elapsed_sec": 30.5}
    doc = corrected(document, [AGY, AGY, AGY, AGY], replaced={2: replaced})
    assert doc["correction"]["chunks"][1]["replaced"] == replaced
    assert "replaced" not in doc["correction"]["chunks"][0]
    validate_corrected(doc)
    for bad in ({**replaced, "engine": "whisper"}, {**replaced, "model": ""}, {**replaced, "effort": 3},
                {**replaced, "searches": -1}, {**replaced, "searches": 1.5}, {**replaced, "elapsed_sec": -1},
                {**replaced, "extra": 1}, "claude-cli", None):
        broken = copy.deepcopy(doc)
        broken["correction"]["chunks"][1]["replaced"] = bad
        with pytest.raises(ValueError):
            validate_corrected(broken)
    legacy = copy.deepcopy(doc)
    for field in ("engine", "model", "effort"):
        del legacy["correction"]["chunks"][1][field]
    with pytest.raises(ValueError):
        validate_corrected(legacy)
