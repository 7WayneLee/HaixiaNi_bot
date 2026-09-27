"""Claude Code CLI 引擎的離線測試：呼叫與隔離、搜尋上限、額度管控、接力與格式驗證。"""
import json
import logging
import os
import threading
from pathlib import Path

import pytest

from haixia.transcript import validate_corrected
from scripts import correct_transcripts as cli
from tests.test_codex_correction import document

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("fake")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("claude", "fake_claude.py"), ("codex", "fake_codex.py"),
                         ("orca", "fake_orca_account.py"), ("agy", "fake_agy.py")):
        target = ROOT / "tests" / script
        target.chmod(0o755)
        (bin_dir / name).symlink_to(target)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HAIXIA_BACKOFF_BASE_SEC", "0.2")
    monkeypatch.setenv("HAIXIA_BACKOFF_MAX_SEC", "0.2")
    monkeypatch.setenv("FAKE_CLAUDE_CALL_LOG", str(tmp_path / "claude.calls"))
    source = tmp_path / "in/影片/02 針灸/甲.rm.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(document(), ensure_ascii=False))
    args = ["--in-dir", str(tmp_path / "in"), "--out-dir", str(tmp_path / "out"),
            "--work-dir", str(tmp_path / "work"), "--engines", "claude", "--claude-jobs", "1"]
    return args, tmp_path / "out/影片/02 針灸/甲.rm.json", tmp_path


def calls(tmp):
    path = tmp / "claude.calls"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def attempts(tmp):
    return [json.loads(path.read_text()) for path in sorted((tmp / "work/chunks").rglob("*.attempt*.json"))]


def with_engines(args, engines):
    changed = args[:]
    changed[changed.index("claude")] = engines
    return changed


def claude_state(tmp, weekly_max=100, session_max=80):
    shared = cli.SharedState(tmp / "quota", 1, 1.0)
    shared.agy_enabled = False
    state = cli.ClaudeState(shared, LOG, weekly_max, session_max)
    shared.engines["claude"] = state
    return shared, state


@pytest.mark.parametrize("mode,tries", [("normal", 1), ("missing_once", 2), ("usage_limit_once", 2)])
def test_claude_attempts(fake, monkeypatch, mode, tries):
    args, output, tmp = fake
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    monkeypatch.setenv("FAKE_CLAUDE_COUNTER", str(tmp / "counter"))
    # 重設時間都在過去：額度錯誤改用指數退避（測試裡是 0.2 秒），不會真的等到重設。
    monkeypatch.setenv("FAKE_ORCA_CLAUDE_RESET_OFFSET", "-121")
    monkeypatch.setenv("FAKE_CLAUDE_RESET_OFFSET", "-100")
    assert cli.main(args) == 0
    doc = validate_corrected(json.loads(output.read_text()))
    assert doc["correction"]["tool"] == "claude-cli"
    assert doc["correction"]["model"] == "claude-opus-5-5"
    assert all(item["engine"] == "claude-cli" and item["model"] == "claude-opus-5-5"
               and item["effort"] == "medium" and item["status"] == "ok"
               for item in doc["correction"]["chunks"])
    assert doc["correction"]["chunks"][0]["attempts"] == tries
    assert all(segment["text"] == "麻黃湯主之" for segment in doc["segments"])
    first = attempts(tmp)[0]
    if mode == "usage_limit_once":
        assert cli.error_kind(first)[0] == "quota"
        assert "rejected" in first["stderr"] and first["rate_limit"]["status"] == "rejected"
        log = (tmp / "work/logs/correct.log").read_text()
        assert "呼叫錯誤（quota）" in log and "Claude 暫停：Claude 額度錯誤" in log
        status = json.loads((tmp / "work/status.json").read_text())
        assert status["chunks_failed"] == 0 and status["retries"] == 0
    else:
        assert first["usage"]["input_tokens"] == 100 and first["usage"]["web_search_requests"] == 1
        assert first["searches"] == first["hook_searches"] == 1 and first["queries"] == ["查詢 0"]


def test_claude_timeout(fake, monkeypatch):
    args, output, tmp = fake
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "timeout")
    monkeypatch.setenv("HAIXIA_AGY_TIMEOUT_GRACE_SEC", "0.1")
    assert cli.main(args + ["--retries", "0", "--claude-timeout", "1"]) == 1
    chunks = validate_corrected(json.loads(output.read_text()))["correction"]["chunks"]
    assert chunks[0]["status"] == "failed" and chunks[0]["engine"] == "claude-cli"
    stderr = attempts(tmp)[0]["stderr"]
    assert "Claude 呼叫逾時" in stderr and "收到 1 個事件，最後是 ['system']" in stderr


def test_claude_command_isolation(fake, monkeypatch):
    args, _output, tmp = fake
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "outer-session")
    monkeypatch.setenv("ORCA_AGENT_HOOK_PORT", "1234")
    assert cli.main(args) == 0
    call = calls(tmp)[0]
    argv = call["argv"]
    assert argv[:2] == ["-p", "--model"] and argv[2] == "claude-opus-5-5"
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--max-budget-usd") + 1] == "1"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--tools") + 1] == "WebSearch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands", "--no-chrome"):
        assert flag in argv
    assert "--bare" not in argv and "--dangerously-skip-permissions" not in argv
    assert argv[-2] == "--" and argv[-1].startswith("你是倪海廈課程逐字稿")
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["autoMemoryEnabled"] is False
    assert str(tmp / "work/claude-hook/gate.py") in settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert Path(call["cwd"]).resolve() == (tmp / "work/claude-ws").resolve()
    assert not any((tmp / "work/claude-ws").iterdir())
    assert call["stdin_devnull"]
    assert "CLAUDECODE" not in call["env"] and "CLAUDE_CODE_SESSION_ID" not in call["env"]
    assert not any(name.startswith("ORCA_AGENT") for name in call["env"])
    assert {"HAIXIA_CLAUDE_LABEL", "HAIXIA_CLAUDE_MAX_SEARCH"} <= set(call["env"])


def test_claude_prompt_is_the_shared_prompt(fake):
    """claude 收到的提示詞就是 agy、codex 共用的 build_prompt，prompt_sha256 不變。"""
    args, output, tmp = fake
    assert cli.main(args) == 0
    assert json.loads(output.read_text())["correction"]["prompt_sha256"] == cli.prompt_sha256()
    prompts = [call["argv"][-1] for call in calls(tmp)]
    source = json.loads((tmp / "in/影片/02 針灸/甲.rm.json").read_text())
    chunks = cli.split_chunks(source["segments"])
    assert sorted(prompts) == sorted(cli.build_prompt(source, chunk, 5)[0] for chunk in chunks)


def test_claude_search_limit_and_count(fake, monkeypatch):
    args, output, tmp = fake
    monkeypatch.setenv("FAKE_CLAUDE_SEARCHES", "7")
    assert cli.main(args) == 0
    for attempt in attempts(tmp):
        assert attempt["searches"] == attempt["hook_searches"] == 5
        assert attempt["searches_denied"] == 2 and attempt["usage"]["web_search_requests"] == 5
        assert len(attempt["queries"]) == 7
    doc = validate_corrected(json.loads(output.read_text()))
    assert [chunk["searches"] for chunk in doc["correction"]["chunks"]] == [5, 5]
    audit = [json.loads(line) for line in (tmp / "work/claude-hook/audit.jsonl").read_text().splitlines()]
    assert sum(item["allow"] for item in audit if item["label"] != "自我測試") == 10
    assert json.loads((tmp / "work/status.json").read_text())["searches"] == 10


def test_claude_other_tools_are_denied(fake, monkeypatch):
    args, _output, tmp = fake
    monkeypatch.setenv("FAKE_CLAUDE_TOOL", "Bash")
    assert cli.main(args + ["--max-search", "0"]) == 0
    attempt = attempts(tmp)[0]
    assert attempt["other_tool_uses"] == 1 and attempt["searches"] == 0 and attempt["searches_denied"] == 1
    assert "WebSearch 以外的工具" in (tmp / "work/logs/correct.log").read_text()
    audit = [json.loads(line) for line in (tmp / "work/claude-hook/audit.jsonl").read_text().splitlines()]
    assert not any(item["allow"] for item in audit)


def test_budget_exceeded_is_retried_not_quota():
    stream = "\n".join(json.dumps(event) for event in (
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": None,
         "total_cost_usd": 1.2}))
    parsed = cli.parse_claude_stream(stream)
    response = {"output": parsed["output"], "stderr": "\n".join(parsed["notes"]), "exit_code": 1}
    assert parsed["output"] == "" and "error_max_budget_usd" in response["stderr"]
    assert cli.error_kind(response)[0] == "other"


def test_claude_error_texts():
    assert cli.error_kind({"output": "", "stderr": "You've hit your limit · resets 2pm", "exit_code": 1})[0] == "quota"
    assert cli.error_kind({"output": "", "stderr": "Claude AI usage limit reached|1790488800",
                           "exit_code": 1})[0] == "quota"
    assert cli.error_kind({"output": "", "stderr": "API Error: 529 Overloaded", "exit_code": 1})[0] == "network"
    assert cli.error_kind({"output": "", "stderr": "Not logged in · Please run /login", "exit_code": 1})[0] == "other"


@pytest.mark.parametrize("session,weekly,weekly_max,expected", [
    (80, 10, 100, "paused"), (79, 10, 100, "running"),
    (10, 100, 100, "running"), (10, 95, 90, "stopped")])
def test_claude_quota_controls(fake, monkeypatch, session, weekly, weekly_max, expected):
    _args, _output, tmp = fake
    monkeypatch.setenv("FAKE_ORCA_CLAUDE_SESSION", str(session))
    monkeypatch.setenv("FAKE_ORCA_CLAUDE_WEEKLY", str(weekly))
    monkeypatch.setenv("FAKE_ORCA_CLAUDE_RESET_OFFSET", "600")
    _shared, state = claude_state(tmp, weekly_max)
    before = cli.time.time()
    assert state.check_quota() == (expected == "running")
    assert state.state == expected
    if expected == "paused":
        assert before + 720 <= state.pause_until <= cli.time.time() + 721
        assert state.reason == "Claude session 額度 80% 已達上限 80%"
    saved = json.loads((tmp / "quota/status.json").read_text())["engines"]["claude"]
    assert saved["session_used_percent"] == session and saved["weekly_used_percent"] == weekly
    assert saved["state"] == expected and saved["session_resets_at"]


def test_claude_quota_checked_at_most_every_minute(fake, monkeypatch):
    _args, _output, tmp = fake
    monkeypatch.setenv("FAKE_ORCA_CALLS", str(tmp / "orca.calls"))
    _shared, state = claude_state(tmp)
    assert state.check_quota() and state.check_quota() and state.check_quota()
    assert len((tmp / "orca.calls").read_text().split()) == 1


def test_claude_missing_quota_pauses_fifteen_minutes(fake, monkeypatch):
    _args, _output, tmp = fake
    monkeypatch.setenv("FAKE_ORCA_MISSING", "1")
    _shared, state = claude_state(tmp)
    before = cli.time.time()
    assert not state.check_quota()
    assert state.state == "paused" and state.pause_until >= before + 900
    assert state.reason.startswith("無法讀取 Claude 額度")


def test_stream_limits_pause_and_merge(fake):
    _args, _output, tmp = fake
    _shared, state = claude_state(tmp)
    assert state.check_quota()
    reset = cli.time.time() + 1800
    info = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.5, "resetsAt": reset},
                                                    "seven_day": {"utilization": 0.7, "resetsAt": reset + 86400}}}
    state.note_limits(info)
    assert state.state == "running" and state.session_percent == 50 and state.weekly_percent == 70
    # 同一週期較舊的 Orca 讀數（較低）不會蓋掉較新的 stream 讀數。
    state.merge(40, reset)
    assert state.session_percent == 50
    info["unifiedWindows"]["five_hour"]["utilization"] = 0.81
    state.note_limits(info)
    assert state.state == "paused" and reset + 119 <= state.pause_until <= reset + 121
    assert "81%" in state.reason
    # 新週期的讀數取代舊週期。
    state.merge(3, reset + 5 * 3600)
    assert state.session_percent == 3


def test_rejected_limits_pause_until_reset_or_back_off(fake):
    _args, _output, tmp = fake
    _shared, state = claude_state(tmp)
    reset = cli.time.time() + 3600
    assert state.note_error("quota", "hit your limit", {"status": "rejected", "rateLimitType": "five_hour",
                                                        "resetsAt": reset})
    assert state.state == "paused" and state.pause_until == reset + 120
    _shared, weekly = claude_state(tmp / "weekly")
    before = cli.time.time()
    assert weekly.note_error("quota", "hit your limit", {"status": "rejected", "rateLimitType": "seven_day",
                                                         "resetsAt": before + 3 * 86400})
    # SharedState 預設退避 300 秒起跳；週額度用完不停用 Claude（使用者可能用重設券）。
    assert weekly.state == "paused" and before + 299 <= weekly.pause_until <= cli.time.time() + 301
    assert "週額度" in weekly.reason


def test_relay_claude_takes_over_and_stops_when_agy_resumes(fake, monkeypatch):
    args, output, tmp = fake
    monkeypatch.setenv("FAKE_AGY_MODE", "quota_once")
    monkeypatch.setenv("FAKE_AGY_FLAG", str(tmp / "agy.flag"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    monkeypatch.setenv("HAIXIA_BACKOFF_BASE_SEC", "0.3")
    monkeypatch.setenv("HAIXIA_BACKOFF_MAX_SEC", "0.3")
    assert cli.main(with_engines(args, "agy,claude") + ["--agy-jobs", "1"]) == 0
    doc = validate_corrected(json.loads(output.read_text()))
    assert [chunk["engine"] for chunk in doc["correction"]["chunks"]] == ["claude-cli", "antigravity-cli"]
    assert doc["correction"]["tool"] == "mixed" and doc["correction"]["model"] == "gemini-3.8-flash-high"
    assert len(calls(tmp)) == 1
    status = json.loads((tmp / "work/status.json").read_text())
    assert status["engines"]["claude"]["chunks_done"] == 1 and status["engines"]["agy"]["chunks_done"] == 1


def test_relay_codex_and_claude_both_take_chunks(fake, monkeypatch):
    args, output, tmp = fake
    monkeypatch.setenv("FAKE_AGY_MODE", "quota")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    monkeypatch.setenv("FAKE_CODEX_MODE", "sleep")
    monkeypatch.setenv("FAKE_ORCA_RESET_OFFSET", "-121")
    assert cli.main(with_engines(args, "agy,codex,claude") + ["--agy-jobs", "1", "--codex-jobs", "1"]) == 0
    doc = validate_corrected(json.loads(output.read_text()))
    assert {chunk["engine"] for chunk in doc["correction"]["chunks"]} == {"codex-cli", "claude-cli"}
    assert doc["correction"]["tool"] == "mixed"


def test_relay_claude_resumes_after_its_pause(fake, monkeypatch):
    """暫停時間過了之後，接手的引擎要能恢復；以前會一直把段放回佇列，永遠輪不到恢復。"""
    args, output, tmp = fake
    monkeypatch.setenv("FAKE_AGY_MODE", "quota")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "usage_limit_once")
    monkeypatch.setenv("FAKE_CLAUDE_COUNTER", str(tmp / "counter"))
    monkeypatch.setenv("FAKE_ORCA_CLAUDE_RESET_OFFSET", "-121")
    monkeypatch.setenv("FAKE_CLAUDE_RESET_OFFSET", "-100")
    outcome = []
    runner = threading.Thread(target=lambda: outcome.append(
        cli.main(with_engines(args, "agy,claude") + ["--agy-jobs", "1"])), daemon=True)
    runner.start()
    runner.join(60)
    assert not runner.is_alive() and outcome == [0]
    doc = validate_corrected(json.loads(output.read_text()))
    assert all(chunk["engine"] == "claude-cli" and chunk["status"] == "ok" for chunk in doc["correction"]["chunks"])
    assert len(calls(tmp)) == 3


def test_validation_accepts_claude_and_legacy(fake):
    args, output, _tmp = fake
    assert cli.main(args) == 0
    doc = json.loads(output.read_text())
    validate_corrected(doc)
    mixed = json.loads(json.dumps(doc))
    mixed["correction"]["chunks"][0]["engine"] = "antigravity-cli"
    mixed["correction"]["tool"] = "mixed"
    validate_corrected(mixed)
    wrong = json.loads(json.dumps(doc))
    wrong["correction"]["chunks"][0]["engine"] = "other-cli"
    with pytest.raises(ValueError):
        validate_corrected(wrong)
    for chunk in doc["correction"]["chunks"]:
        for field in ("engine", "model", "effort"):
            del chunk[field]
    doc["correction"]["tool"] = "antigravity-cli"
    validate_corrected(doc)


def test_status_records_claude_percentages(fake, monkeypatch):
    args, _output, tmp = fake
    monkeypatch.setenv("FAKE_CLAUDE_FIVE_HOUR", "0.123")
    monkeypatch.setenv("FAKE_CLAUDE_SEVEN_DAY", "0.456")
    assert cli.main(args) == 0
    claude = json.loads((tmp / "work/status.json").read_text())["engines"]["claude"]
    assert claude["session_used_percent"] == 12.3 and claude["weekly_used_percent"] == 45.6
    assert claude["chunks_done"] == 2 and claude["state"] == "stopped" and claude["reason"] == "執行結束"
