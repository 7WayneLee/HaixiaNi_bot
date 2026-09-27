#!/usr/bin/env python3
"""用 Antigravity、Codex、Claude Code CLI 並行校正逐字稿，支援斷點續跑。"""

import argparse
from collections import deque
import hashlib
import json
import logging
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from haixia.correction import (build_prompt, choose_result, corrected_document, evaluate,
                               now, prompt_sha256, split_chunks)
from haixia.transcript import save_corrected, validate, validate_corrected

ROOT = Path(__file__).resolve().parents[1]
# Codex 額度用完時回「Your workspace is out of credits. Add credits to continue.」（2026-09-26 實測，5 小時額度重設後恢復）。
# Claude Code 訂閱額度用完時回「You've hit your limit · resets …」或「Claude AI usage limit reached」，
# stream-json 另有 status 為 rejected 的 rate_limit_event。
QUOTA = re.compile(r"RESOURCE_EXHAUSTED|\b429\b|quota|rate.?limit|too many requests|usage limit|"
                   r"out of credits|add credits|hit your (?:\w+ ){0,3}limit|(?:session|weekly|opus) limit|"
                   r"額度|限速", re.I)
NETWORK = re.compile(r"connection|network|dns|timed? ?out|unreachable|unavailable|overloaded|socket|"
                     r"ECONN|ENET|連線|網路", re.I)
SECRET = re.compile(r"sk-[A-Za-z0-9*_\-]+")
OUTPUT_LINE = re.compile(r"^\s*\[\d+(?:\.\d+)?\]", re.M)
RESET_TIME = re.compile(r"Resets\s+in\s+(\d+(?:h|m|s)(?:\d+(?:h|m|s))*)(?=$|[\s\"',.!?}\]])", re.I)
RESET_PARTS = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", re.I)
RESET_ANY = re.compile(r"Resets\s+in\s+((?:\d+[dhms])+)", re.I)
WEEKLY_RESET_SEC = 6 * 3600
# 每個 agy 程序的 log 都有一行 applyAuthResult，記錄這次實際登入的帳號。
AUTH_EMAIL = re.compile(r"applyAuthResult:\s*email=([^,\s]*)")
AGY_LOG_KEEP = 500
ENGINE_TOOLS = {"agy": "antigravity-cli", "codex": "codex-cli", "claude": "claude-cli"}
TOOL_ENGINES = {tool: engine for engine, tool in ENGINE_TOOLS.items()}
# 批次的 claude 不能沿用呼叫端（Claude Code、Orca）的 session 與 hook 環境變數。
CLAUDE_ENV_DROP = re.compile(r"^(CLAUDECODE|CLAUDE_PID|CLAUDE_EFFORT|"
                             r"CLAUDE_CODE_(ENTRYPOINT|CHILD_SESSION|EXECPATH|SESSION_\w*|MESSAGING_\w*)|"
                             r"ORCA_AGENT_\w*)$")


def reset_countdown(text):
    """額度錯誤裡最長的重設倒數（秒）；不設上限、可含天數，沒有時回傳 None。"""
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    values = [sum(int(number) * units[unit.lower()]
                  for number, unit in re.findall(r"(\d+)([dhms])", match.group(1), re.I))
              for match in RESET_ANY.finditer(text or "")]
    return max(values, default=None)


def quota_details(result):
    """回傳額度錯誤的 short_error（已遮蔽金鑰；沒有時為 None）與不設上限的重設倒數。"""
    message = None
    match = re.search(r"AGY_ERROR:\s*(\{[^\n]+\})", result["stderr"])
    if match:
        try:
            message = json.loads(match.group(1)).get("short_error")
        except (json.JSONDecodeError, AttributeError):
            pass
    message = redact(message) if isinstance(message, str) and message.strip() else None
    return message, reset_countdown(result["stderr"] + "\n" + result["output"])


def quota_reset_seconds(result):
    """從額度錯誤取得重設倒數；只接受 0 到 6 小時之間的值。"""
    message = result["stderr"] + "\n" + result["output"]
    seconds = []
    for match in RESET_TIME.finditer(message):
        parts = RESET_PARTS.fullmatch(match.group(1))
        if parts and any(value is not None for value in parts.groups()):
            value = sum(int(number or 0) * unit for number, unit in zip(parts.groups(), (3600, 60, 1)))
            if 0 < value <= 6 * 3600:
                seconds.append(value)
    return max(seconds, default=None)


def timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def redact(value):
    """任何外部 CLI 文字落盤之前遮蔽金鑰。"""
    if isinstance(value, str):
        return SECRET.sub("sk-***", value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(json.dumps(redact(data), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def setup_hook(work_dir):
    work_dir = Path(work_dir).resolve()
    if work_dir == ROOT or ROOT in work_dir.parents:
        raise ValueError("--work-dir 必須放在 repo 外面")
    ws = work_dir / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    if any(item.name != ".agents" for item in ws.iterdir()):
        raise ValueError("ws 工作目錄只能放 .agents/")
    hooks = ws / ".agents"
    hooks.mkdir(exist_ok=True)
    for name in ("gate.py", "hooks.json"):
        shutil.copyfile(ROOT / "tools/agy_hook" / name, hooks / name)
    return ws


def check_hook(ws, max_search):
    gate = ws / ".agents/gate.py"
    env = dict(os.environ, HAIXIA_AGY_LABEL="自我測試", HAIXIA_AGY_MAX_SEARCH=str(max_search))
    for tool, expected in (("search_web", "allow" if max_search else "deny"), ("run_command", "deny")):
        payload = {"conversationId": f"selftest-{os.getpid()}-{time.time_ns()}", "toolCall": {"name": tool}}
        result = subprocess.run([sys.executable, str(gate)], input=json.dumps(payload),
                                text=True, capture_output=True, cwd=ws / ".agents", env=env, timeout=10)
        if result.returncode or json.loads(result.stdout).get("decision") != expected:
            raise RuntimeError(f"hook 自我測試失敗：{tool}：{result.stderr}")


def agy_log_path(work_dir, kind):
    """每次 agy 呼叫各用一個 log 檔；檔名以時間開頭，依檔名排序即是先後順序。"""
    directory = Path(work_dir) / "agy-logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{kind}-{threading.get_ident()}.log"


def prune_agy_logs(work_dir):
    """agy-logs 只保留最新 AGY_LOG_KEEP 個檔。"""
    try:
        logs = sorted((Path(work_dir) / "agy-logs").glob("*.log"))
    except OSError:
        return
    for path in logs[:max(0, len(logs) - AGY_LOG_KEEP)]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def agy_account(log_file):
    """從 agy log 的 applyAuthResult 取出實際登入的帳號；讀不到或沒有時回傳 None。"""
    try:
        text = Path(log_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    accounts = [email for email in AUTH_EMAIL.findall(text) if email]
    return accounts[-1] if accounts else None


def same_account(first, second):
    return (first or "").strip().lower() == (second or "").strip().lower()


def check_model(model, log_file):
    """確認 agy 看得到指定模型，並回傳這次登入的帳號（agy models 不耗額度）。"""
    result = subprocess.run(["agy", "--log-file", str(log_file), "models"],
                            capture_output=True, text=True, timeout=60)
    available = {line.split()[0] for line in result.stdout.splitlines() if line.split()}
    if result.returncode or model not in available:
        raise RuntimeError(f"agy models 看不到指定模型：{model}；{result.stderr.strip()}")
    return agy_account(log_file)


def check_account(work_dir):
    """用 agy models（不耗額度）確認目前登入的帳號；讀不到時回傳 None。"""
    log_file = agy_log_path(work_dir, "models")
    try:
        subprocess.run(["agy", "--log-file", str(log_file), "models"],
                       capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass
    account = agy_account(log_file)
    prune_agy_logs(work_dir)
    return account


def search_count(audit, label):
    if not audit.exists():
        return 0
    count = 0
    with audit.open(encoding="utf-8") as source:
        for line in source:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("label") == label and item.get("tool") == "search_web" and item.get("allow"):
                count += 1
    return count


def run_agy(prompt, model, timeout, ws, label, max_search, shared):
    # --log-file 是根層級旗標，要放在 -p 或子命令前面。
    log_file = agy_log_path(shared.work_dir, "call")
    args = ["agy", "--log-file", str(log_file), "-p", prompt, "--model", model, "--output-format", "text",
            "--print-timeout", f"{timeout}s", "--disable-slash-commands"]
    env = dict(os.environ, HAIXIA_AGY_LABEL=label, HAIXIA_AGY_MAX_SEARCH=str(max_search))
    began = time.monotonic()
    process = subprocess.Popen(args, cwd=ws, env=env, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    shared.register_process(process, "agy")
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout + float(os.environ.get("HAIXIA_AGY_TIMEOUT_GRACE_SEC", "30")))
            code = process.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            code = 124
            stderr += "\nAntigravity 呼叫逾時"
    finally:
        shared.unregister_process(process, "agy")
    account = agy_account(log_file)
    prune_agy_logs(shared.work_dir)
    return {"output": stdout, "stderr": stderr, "exit_code": code,
            "elapsed_sec": round(time.monotonic() - began, 2),
            "searches": search_count(ws / ".agents/audit.jsonl", label),
            "agy_account": account, "agy_log": log_file.name}


def run_codex(prompt, model, effort, timeout, ws, shared):
    last = ws / f"last-{threading.get_ident()}-{time.time_ns()}.txt"
    command = ["codex", "exec", "--ignore-user-config", "--ephemeral",
               "--skip-git-repo-check", "-s", "read-only", "--disable", "shell_tool",
               "-m", model, "-c", f'model_reasoning_effort="{effort}"',
               "-c", 'web_search="cached"', "--json", "-o", str(last), prompt]
    began = time.monotonic()
    process = subprocess.Popen(command, cwd=ws, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    shared.register_process(process, "codex")
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout + float(os.environ.get("HAIXIA_AGY_TIMEOUT_GRACE_SEC", "30")))
            code = process.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            code = 124
            stderr += "\nCodex 呼叫逾時"
    finally:
        shared.unregister_process(process, "codex")
    output = last.read_text(encoding="utf-8") if last.exists() else ""
    last.unlink(missing_ok=True)
    searches = 0
    usage = {}
    commands = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
        if event.get("type", "").startswith("item."):
            item_type = (event.get("item") or {}).get("type")
            if item_type == "command_execution":
                commands += 1
        if event.get("type") == "item.completed":
            item_type = (event.get("item") or {}).get("type")
            searches += item_type == "web_search"
    return {"output": output, "stderr": stderr + "\n" + stdout, "exit_code": code,
            "elapsed_sec": round(time.monotonic() - began, 2), "searches": searches,
            "usage": usage, "command_executions": commands}


def setup_claude(work_dir):
    """claude 在空的 <work-dir>/claude-ws 執行；hook 放在 <work-dir>/claude-hook（計數與稽核紀錄也在那裡）。"""
    work_dir = Path(work_dir).resolve()
    if work_dir == ROOT or ROOT in work_dir.parents:
        raise ValueError("--work-dir 必須放在 repo 外面")
    ws = work_dir / "claude-ws"
    ws.mkdir(parents=True, exist_ok=True)
    if any(ws.iterdir()):
        raise ValueError("claude-ws 工作目錄必須是空的")
    hook_dir = work_dir / "claude-hook"
    hook_dir.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "tools/claude_hook/gate.py", hook_dir / "gate.py")
    return ws, hook_dir


def check_claude_hook(hook_dir, max_search):
    """以假工具呼叫確認 hook 放行 WebSearch（max_search 為 0 時拒絕）、拒絕其他工具。"""
    gate = Path(hook_dir) / "gate.py"
    env = dict(os.environ, HAIXIA_CLAUDE_LABEL="自我測試", HAIXIA_CLAUDE_MAX_SEARCH=str(max_search))
    for tool, allowed in (("WebSearch", bool(max_search)), ("Bash", False), ("WebFetch", False)):
        payload = {"session_id": f"selftest-{os.getpid()}-{time.time_ns()}", "hook_event_name": "PreToolUse",
                   "tool_name": tool, "tool_input": {}}
        result = subprocess.run([sys.executable, str(gate)], input=json.dumps(payload), text=True,
                                capture_output=True, cwd=hook_dir, env=env, timeout=10)
        denied = bool(result.stdout.strip()) and json.loads(result.stdout)["hookSpecificOutput"][
            "permissionDecision"] == "deny"
        if result.returncode or denied == allowed:
            raise RuntimeError(f"Claude hook 自我測試失敗：{tool}：{result.stderr}")


def claude_command(prompt, model, effort, hook_dir, budget):
    """只開 WebSearch；不載入使用者的設定檔（hooks、外掛）、MCP、技能，不保存 session。
    --bare 會讓訂閱登入失效（只認 ANTHROPIC_API_KEY），所以不用。
    --max-budget-usd 是依 API 牌價估算的金額（訂閱登入也有），每輪回應結束才檢查，擋得住多輪失控，
    擋不住單一回應一直不結束；那種情況靠 --claude-timeout。"""
    gate = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(hook_dir) / 'gate.py'))}"
    settings = {"autoMemoryEnabled": False,
                "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
                    {"type": "command", "command": gate, "timeout": 10}]}]}}
    return ["claude", "-p", "--model", model, "--effort", effort, "--no-session-persistence",
            "--output-format", "stream-json", "--verbose", "--max-budget-usd", f"{budget:g}",
            "--setting-sources", "", "--settings", json.dumps(settings, ensure_ascii=False),
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--tools", "WebSearch", "--allowedTools", "WebSearch", "--permission-mode", "dontAsk",
            "--disable-slash-commands", "--no-chrome", "--", prompt]


def hook_search_count(audit, label):
    """claude hook 稽核紀錄裡，這次呼叫放行的 WebSearch 次數。"""
    count = 0
    try:
        with Path(audit).open(encoding="utf-8") as source:
            for line in source:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                count += item.get("label") == label and item.get("tool") == "WebSearch" and bool(item.get("allow"))
    except OSError:
        return 0
    return count


def _json_or_none(line):
    try:
        return json.loads(line)
    except ValueError:
        return None


def parse_claude_stream(stdout):
    """解析 claude -p 的 stream-json：最後的回答、錯誤、搜尋次數、用量與 rate_limit_event。"""
    tools, rejected_tools, queries, notes = {}, set(), [], []
    result = limits = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            if line.strip():
                notes.append(line.strip()[-300:])
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        blocks = [block for block in message.get("content") or [] if isinstance(block, dict)] \
            if isinstance(message.get("content"), list) else []
        if kind == "assistant":
            for block in blocks:
                if block.get("type") == "tool_use":
                    tools[block.get("id")] = block.get("name")
                    if block.get("name") == "WebSearch":
                        queries.append((block.get("input") or {}).get("query"))
        elif kind == "user":
            rejected_tools.update(block.get("tool_use_id") for block in blocks
                                  if block.get("type") == "tool_result" and block.get("is_error"))
        elif kind == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
            limits = event["rate_limit_info"]
            if limits.get("status") == "rejected":
                notes.append(f'Claude rate_limit_event rejected：{limits.get("rateLimitType")}，'
                             f'resetsAt={limits.get("resetsAt")}')
        elif kind == "result":
            result = event
    output = ""
    if result is None:
        # 逾時或中斷時留下線索：收到幾個事件、最後幾個事件的種類、已輸出多少字。
        kinds = [event.get("type") for event in map(_json_or_none, stdout.splitlines()) if isinstance(event, dict)]
        text = sum(len(block.get("text") or "") for event in map(_json_or_none, stdout.splitlines())
                   if isinstance(event, dict) and event.get("type") == "assistant"
                   for block in ((event.get("message") or {}).get("content") or []) if isinstance(block, dict))
        notes.append(f"claude 沒有輸出 result 事件（收到 {len(kinds)} 個事件，最後是 {kinds[-5:]}，"
                     f"回答文字 {text} 字）")
    elif result.get("is_error") or result.get("subtype") != "success":
        notes.append(f'claude 回報錯誤（{result.get("subtype")}）：{result.get("result") or ""}')
    else:
        output = result.get("result") or ""
    searches = [tool_id for tool_id, name in tools.items() if name == "WebSearch"]
    usage = {}
    if result is not None:
        tokens = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        models = result.get("modelUsage") if isinstance(result.get("modelUsage"), dict) else {}
        usage = {"num_turns": result.get("num_turns"), "duration_api_ms": result.get("duration_api_ms"),
                 "total_cost_usd": result.get("total_cost_usd"),
                 **{key: tokens.get(key) for key in ("input_tokens", "output_tokens",
                                                     "cache_read_input_tokens", "cache_creation_input_tokens")},
                 "web_search_requests": sum(item.get("webSearchRequests") or 0 for item in models.values()
                                            if isinstance(item, dict))}
    return {"output": output, "notes": notes, "rate_limit": limits, "usage": usage, "queries": queries,
            "searches": sum(tool_id not in rejected_tools for tool_id in searches),
            "searches_denied": sum(tool_id in rejected_tools for tool_id in searches),
            "other_tool_uses": sum(name != "WebSearch" for name in tools.values())}


def run_claude(prompt, model, effort, timeout, ws, hook_dir, label, max_search, shared, budget=1.0):
    env = {key: value for key, value in os.environ.items() if not CLAUDE_ENV_DROP.match(key)}
    env.update(HAIXIA_CLAUDE_LABEL=label, HAIXIA_CLAUDE_MAX_SEARCH=str(max_search))
    began = time.monotonic()
    process = subprocess.Popen(claude_command(prompt, model, effort, hook_dir, budget), cwd=ws, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    shared.register_process(process, "claude")
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout + float(os.environ.get("HAIXIA_AGY_TIMEOUT_GRACE_SEC", "30")))
            code = process.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            code = 124
            stderr += "\nClaude 呼叫逾時"
    finally:
        shared.unregister_process(process, "claude")
    parsed = parse_claude_stream(stdout)
    return {"output": parsed["output"], "stderr": "\n".join([stderr.strip(), *parsed["notes"]]).strip(),
            "exit_code": code, "elapsed_sec": round(time.monotonic() - began, 2),
            "searches": parsed["searches"], "searches_denied": parsed["searches_denied"],
            "hook_searches": hook_search_count(Path(hook_dir) / "audit.jsonl", label),
            "queries": parsed["queries"], "other_tool_uses": parsed["other_tool_uses"],
            "usage": parsed["usage"], "rate_limit": parsed["rate_limit"]}


def error_kind(result):
    if result["exit_code"] == 0 and OUTPUT_LINE.search(result["output"]):
        return None, ""
    stderr = result["stderr"]
    match = re.search(r"AGY_ERROR:\s*(\{[^\n]+\})", stderr)
    detail = stderr
    if match:
        try:
            data = json.loads(match.group(1))
            detail = " ".join(str(value) for value in data.values()) + " " + stderr
        except json.JSONDecodeError:
            pass
    detail = redact((detail + "\n" + result["output"]).strip())
    if "stream disconnected before completion" in detail and "401" in detail:
        return "network", "stream disconnected before completion；" + detail[-260:]
    if QUOTA.search(detail):
        return "quota", detail[-300:]
    if result["exit_code"] == 0:
        return "empty", ("輸出沒有可辨識的時間標記行：" + detail)[-300:]
    if NETWORK.search(detail):
        return "network", detail[-300:]
    return "other", (detail or "no output produced").strip()[-300:]


class SharedState:
    def __init__(self, work_dir, total, audio_total, files_total=0, backoff_base=300, backoff_max=3600):
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.pause_until = 0.0
        self.quota_resets_at = 0.0
        self.pause_level = 0
        self.consecutive_errors = 0
        self.processes = set()
        self.active_calls = {"agy": 0, "codex": 0, "claude": 0}
        self.force_stop = False
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.work_dir = Path(work_dir)
        self.started = time.monotonic()
        self.started_at = now()
        self.last_progress_at = None
        self.counts = {"chunks_total": total, "chunks_done": 0, "chunks_ok": 0,
                       "chunks_partial": 0, "chunks_failed": 0, "files_total": files_total,
                       "files_done": 0,
                       "retries": 0, "searches": 0, "audio_hours_total": audio_total}
        self.audio_hours_done = 0.0
        self.last_error = None
        self.failed_files = set()
        self.engines = {}
        self.agy_done = 0
        self.agy_elapsed = 0.0
        self.agy_reason = None
        self.agy_stopped = False
        # 帳號接力：quota_poll 是額度暫停的上限（秒，None 表示不限）；
        # quota_since 起到 agy 再次成功之前算同一輪額度用完。
        self.quota_poll = None
        self.quota_since = None
        self.quota_message = None
        self.quota_kind = None
        self.quota_reset_estimate = None
        # 試探中只放行一個 worker；fresh_after 之前就開始的呼叫回報的額度錯誤視為舊消息。
        self.probing = False
        self.probe_owner = None
        self.fresh_after = 0.0
        # 帳號管控：所有 agy 程序共用鑰匙圈裡的登入資料，別的 agy 視窗更新登入資料就會換掉帳號。
        # 帳號不符的暫停和額度暫停互相獨立；account_resumed_at 之前就送出的呼叫不再觸發暫停。
        self.expected_arg = None
        self.account = None
        self.account_started = 0.0
        self.account_expected = None
        self.account_mismatch_since = None
        self.account_resumed_at = 0.0
        self.account_poll = 120.0
        self.account_check_at = 0.0
        self.account_check_note = None
        self.status("running")

    def agy_paused(self):
        """agy 是否暫停派送：額度或斷路器暫停中，或帳號不符。"""
        return self.pause_until > time.time() or self.account_mismatch_since is not None

    def status(self, state=None):
        with self.lock:
            if state is None:
                states = (["stopped" if self.agy_stopped else "paused" if self.agy_paused() else "running"]
                          if getattr(self, "agy_enabled", True) else [])
                states += [item.state for item in self.engines.values()]
                state = "paused" if states and all(item != "running" for item in states) else "running"
            engine_data = {}
            if getattr(self, "agy_enabled", True):
                engine_data["agy"] = {"state": "stopped" if self.agy_stopped else
                                      "paused" if self.agy_paused() else "running",
                                      "paused_until": timestamp(self.pause_until) if self.pause_until > time.time() else None,
                                      "reason": redact(self.agy_reason), "chunks_done": self.agy_done,
                                      "average_sec": round(self.agy_elapsed / self.agy_done, 2) if self.agy_done else 0,
                                      "probing": self.probing,
                                      "quota_poll_min": self.quota_poll / 60 if self.quota_poll is not None else None}
            for name, item in self.engines.items():
                engine_data[name] = item.snapshot()
            data = {"pid": os.getpid(), "started_at": self.started_at,
                    "last_progress_at": self.last_progress_at,
                    "quota_resets_at": timestamp(self.quota_resets_at) if self.quota_resets_at else None,
                    "agy_quota_exhausted_since": timestamp(self.quota_since) if self.quota_since else None,
                    "agy_quota_message": redact(self.quota_message), "agy_quota_kind": self.quota_kind,
                    "agy_quota_resets_at": timestamp(self.quota_reset_estimate) if self.quota_reset_estimate else None,
                    "agy_account": self.account, "agy_expected_account": self.account_expected,
                    "agy_account_mismatch": self.account_mismatch_since is not None,
                    "agy_account_mismatch_since": (timestamp(self.account_mismatch_since)
                                                   if self.account_mismatch_since is not None else None),
                    "updated_at": now(), "state": state,
                    "paused_until": None if self.pause_until <= time.time() else
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.pause_until)),
                    **self.counts, "audio_hours_done": round(self.audio_hours_done, 4),
                    "failed_files": sorted(self.failed_files),
                    "consecutive_errors": self.consecutive_errors, "last_error": redact(self.last_error),
                    "engines": engine_data, "codex_mode": getattr(self, "codex_mode", None),
                    "active_engines": [name for name, count in self.active_calls.items() if count > 0]}
            atomic_json(self.work_dir / "status.json", data)

    def wait_if_paused(self, requeue=False, log=None):
        """等 agy 暫停結束（額度／斷路器暫停與帳號不符各自獨立）。
        試探中只放行一個 worker；requeue 為真（雙引擎）時不佔著段等待。"""
        me = threading.get_ident()
        while True:
            with self.lock:
                remaining = self.pause_until - time.time()
                if remaining <= 0:
                    if self.pause_until:
                        self.pause_until = 0.0
                        self.quota_resets_at = 0.0
                        if self.quota_poll is not None and self.quota_since is not None:
                            self.start_probe()
                        self.status()
                    if self.stop.is_set():
                        return False
                    if self.account_mismatch_since is not None:
                        remaining = 1.0
                    elif not self.probing or self.probe_owner == me:
                        return True
                    elif self.probe_owner is None:
                        self.probe_owner = me
                        if log:
                            log.info("額度試探：先由一個 worker 呼叫一次，其餘等結果")
                        return True
                    else:
                        remaining = 1.0
                if requeue:
                    return "requeue"
            if self.stop.wait(min(remaining, 1.0)):
                return False

    def start_probe(self):
        """進入試探；在此之前就開始的呼叫回報的額度錯誤不再採信。呼叫端須持有 lock。"""
        self.probing = True
        self.fresh_after = time.time()

    def release_probe(self):
        with self.lock:
            if self.probe_owner == threading.get_ident():
                self.probe_owner = None

    def limit_quota_pause(self, until):
        return min(until, time.time() + self.quota_poll) if self.quota_poll is not None else until

    def check_resume_now(self, log):
        """<work-dir>/resume-now 存在時立即解除 agy 的額度暫停，改由一個 worker 試探；
        帳號不符時立即檢查一次登入帳號。處理後刪除該檔。"""
        path = self.work_dir / "resume-now"
        if not path.exists():
            return False
        with self.lock:
            enabled = getattr(self, "agy_enabled", True)
            if enabled and self.account_mismatch_since is not None:
                log.warning("收到 resume-now，立即檢查 Antigravity 登入的帳號")
                self.account_check_at = 0.0
            if enabled and self.quota_since is not None:
                log.warning("收到 resume-now，立即重試")
                self.pause_until = 0.0
                self.quota_resets_at = 0.0
                self.start_probe()
                self.status()
            elif not enabled or self.account_mismatch_since is None:
                log.info("收到 resume-now；Antigravity 目前沒有額度暫停或帳號不符，不需處理")
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                log.error("無法刪除 %s：%s", path, error)
        return True

    def expected_account(self):
        """預期帳號：<work-dir>/expected-account 優先（每次都重讀，換帳號不必重啟），其次 --expected-agy-account。"""
        try:
            words = (self.work_dir / "expected-account").read_text(encoding="utf-8").split()
        except OSError:
            words = []
        return words[0] if words else self.expected_arg

    def refresh_expected(self, log):
        """重讀預期帳號，有變動時記一行 log；回傳（預期帳號, 是否變動）。呼叫端須持有 lock。"""
        expected = self.expected_account()
        changed = not same_account(expected, self.account_expected)
        if changed:
            log.warning("預期的 Antigravity 帳號改為 %s", expected or "（未設定，只記錄、不管控）")
            self.account_check_note = None
        self.account_expected = expected
        return expected, changed

    def note_account(self, account, started, log, context):
        """記錄 agy 實際登入的帳號並和預期帳號比對。context 為「呼叫」時只會進入帳號不符的暫停
        （這次的結果照常驗收、採用）；「啟動檢查」「檢查」（agy models）確認帳號符合才解除暫停。"""
        with self.lock:
            expected, _ = self.refresh_expected(log)
            if account is not None and started >= self.account_started:
                self.account, self.account_started = account, started
            mismatch = self.account_mismatch_since is not None
            note = ((expected or "").lower(), (account or "").lower())
            if expected is None or (account is not None and same_account(account, expected)):
                if mismatch and context != "呼叫":
                    self.account_mismatch_since = None
                    self.account_resumed_at = time.time()
                    self.account_check_note = None
                    if expected is None:
                        log.warning("已不再設定預期的 Antigravity 帳號，解除帳號不符的暫停，繼續派送")
                    else:
                        log.warning("Antigravity 帳號已符合預期（%s），解除帳號不符的暫停，繼續派送", account)
            elif account is None:
                if context == "呼叫" or self.account_check_note != note:
                    self.account_check_note = note
                    log.warning("%s：無法從 agy log 解析登入的帳號，這次沒有檢查帳號", context)
            elif not mismatch:
                if started < self.account_resumed_at:
                    log.info("帳號恢復前就送出的 agy 呼叫用的是 %s，不再暫停", account)
                else:
                    self.account_mismatch_since = time.time()
                    self.account_check_at = time.time() + self.account_poll
                    self.account_check_note = note
                    found = ("agy 呼叫實際登入的帳號是 %s，不是預期的 %s（這次的結果照常驗收、採用）"
                             if context == "呼叫" else f"{context}發現目前登入的帳號是 %s，不是預期的 %s")
                    log.warning("暫停 Antigravity：" + found + "。請在 agy 視窗登入預期的帳號；"
                                "如果要改用這個帳號，把它寫進 %s。之後每 %g 分鐘用 agy models 檢查一次，"
                                "touch %s 可立即檢查", account, expected, self.work_dir / "expected-account",
                                self.account_poll / 60, self.work_dir / "resume-now")
            elif context != "呼叫" and self.account_check_note != note:
                self.account_check_note = note
                log.warning("Antigravity 帳號仍不符：預期 %s，目前登入 %s；每 %g 分鐘再檢查",
                            expected, account, self.account_poll / 60)
            self.status()

    def poll_account(self, log):
        """主迴圈呼叫：預期帳號改變時，或帳號不符的暫停中每 account_poll 秒（resume-now 會立即觸發），
        用 agy models（不耗額度）檢查目前登入的帳號。"""
        with self.lock:
            if self.stop.is_set():
                return False
            _, changed = self.refresh_expected(log)
            if not changed and (self.account_mismatch_since is None or time.time() < self.account_check_at):
                return False
            self.account_check_at = time.time() + self.account_poll
        started = time.time()
        self.note_account(check_account(self.work_dir), started, log, "檢查")
        return True

    def note_error(self, kind, reason, log, reset_seconds=None, message=None, countdown=None, started=None):
        with self.lock:
            if self.probe_owner == threading.get_ident():
                self.probe_owner = None
            if kind == "quota" and started is not None and started < self.fresh_after:
                log.warning("試探開始前就送出的呼叫回報額度用完（可能是換帳號前的舊呼叫），不暫停，直接重試")
                return True
            self.last_error = redact(reason)
            self.agy_reason = redact(reason)
            if kind == "quota":
                noted = time.time()
                if self.quota_since is None:
                    self.quota_since = noted
                self.quota_message = redact(message or reason)
                self.quota_kind = "weekly" if countdown is not None and countdown > WEEKLY_RESET_SEC else "five_hour"
                self.quota_reset_estimate = noted + countdown if countdown else None
                self.probing = False
            if kind == "quota" and reset_seconds is not None:
                reset_at = time.time() + reset_seconds
                if reset_at > self.quota_resets_at:
                    self.quota_resets_at = reset_at
                    self.pause_until = self.limit_quota_pause(reset_at + 120)
                    self.pause_level = 0
                    reset_clock = time.strftime("%H:%M", time.localtime(reset_at))
                    retry_clock = time.strftime("%H:%M", time.localtime(self.pause_until))
                    log.warning("暫停：Antigravity 額度用完，預計 %s 重設，%s 再試", reset_clock, retry_clock)
                self.status("paused")
                return True
            if self.pause_until > time.time():
                self.status("paused")
                return True
            if kind != "quota":
                self.consecutive_errors += 1
            if kind == "quota" or self.consecutive_errors >= 3:
                delay = min(self.backoff_max, self.backoff_base * 2 ** self.pause_level)
                self.pause_level += 1
                self.pause_until = time.time() + delay
                if kind == "quota":
                    self.pause_until = self.limit_quota_pause(self.pause_until)
                self.quota_resets_at = 0.0
                clock = time.strftime("%H:%M", time.localtime(self.pause_until))
                cause = ("Antigravity 額度用完或被限速" if kind == "quota" else
                         f"連續 {self.consecutive_errors} 次呼叫失敗")
                log.warning("暫停：%s（%s），%s 再試", cause, reason, clock)
                self.status("paused")
                return True
            self.status()
            return False

    def note_success(self, started=None, log=None):
        with self.lock:
            self.consecutive_errors = 0
            self.pause_level = 0
            self.agy_reason = None
            if self.probe_owner == threading.get_ident():
                self.probe_owner = None
            # 額度用完之前就送出的呼叫晚到的成功，不代表額度已恢復。
            if self.quota_since is None or (started is not None and started < self.quota_since):
                return
            if self.probing:
                self.probing = False
                self.pause_until = 0.0
                self.quota_resets_at = 0.0
                if log:
                    log.info("額度試探成功：Antigravity 恢復，所有 worker 繼續")
            self.quota_since = self.quota_message = self.quota_kind = self.quota_reset_estimate = None
            self.status()

    def register_process(self, process, engine="agy"):
        with self.lock:
            self.processes.add(process)
            self.active_calls[engine] += 1
            self.status()
            stopped = self.stop.is_set()
            force = self.force_stop
        if stopped:
            try:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass

    def unregister_process(self, process, engine="agy"):
        with self.lock:
            self.processes.discard(process)
            self.active_calls[engine] = max(0, self.active_calls[engine] - 1)
            self.status()

    def stop_processes(self):
        """先終止所有進行中的程序群組，五秒後強制清理。"""
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while any(process.poll() is None for process in processes) and time.monotonic() < deadline:
            time.sleep(0.05)
        with self.lock:
            self.force_stop = True
            processes = list(self.processes)
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def completed(self, result, chunk, audio_hours, log):
        with self.lock:
            self.counts["chunks_done"] += 1
            self.counts[f'chunks_{result["status"]}'] += 1
            self.counts["searches"] += result["searches"]
            self.audio_hours_done += audio_hours
            engine = TOOL_ENGINES.get(result.get("engine", "antigravity-cli"))
            if engine == "agy":
                self.agy_done += 1
                self.agy_elapsed += result["elapsed_sec"]
            elif engine in self.engines:
                # 沿用既有校正版的段可能來自這次沒有啟用的引擎，就不計入引擎統計。
                self.engines[engine].done += 1
                self.engines[engine].elapsed += result["elapsed_sec"]
            self.last_progress_at = now()
            self.status()
            log.info("%s 第 %d 段：%d 行，%.1f 秒，搜尋 %d 次，%s", result["source"],
                     result["chunk_number"], chunk["end_index"] - chunk["start_index"],
                     result["elapsed_sec"], result["searches"],
                     "失敗" if result["status"] == "failed" else result["status"])

    def progress(self, log):
        with self.lock:
            hours = max((time.monotonic() - self.started) / 3600, 1e-9)
            speed = self.audio_hours_done / hours
            remaining = (self.counts["audio_hours_total"] - self.audio_hours_done) / speed if speed else 0
            average = self.counts["searches"] / self.counts["chunks_done"] if self.counts["chunks_done"] else 0
            log.info("進度：%d/%d 段，音訊 %.2f 小時，速度 %.2f 音訊小時/實際小時，預估剩餘 %.1f 小時；失敗 %d，重試 %d，平均搜尋 %.1f 次",
                     self.counts["chunks_done"], self.counts["chunks_total"], self.audio_hours_done,
                     speed, remaining, self.counts["chunks_failed"], self.counts["retries"], average)


class CodexState:
    label = "Codex"

    def __init__(self, shared, log, weekly_max, session_max, orca_bin="orca"):
        self.shared = shared
        self.log = log
        self.weekly_max = weekly_max
        self.session_max = session_max
        self.orca_bin = orca_bin
        self.state = "running"
        self.pause_until = 0.0
        self.reason = None
        self.done = 0
        self.elapsed = 0.0
        self.session_percent = None
        self.weekly_percent = None
        self.session_reset = None
        self.checked_at = 0.0
        self.errors = 0
        self.pause_level = 0

    def snapshot(self):
        return {"state": self.state, "paused_until": timestamp(self.pause_until) if self.state == "paused" else None,
                "reason": redact(self.reason), "chunks_done": self.done,
                "average_sec": round(self.elapsed / self.done, 2) if self.done else 0,
                "session_used_percent": self.session_percent, "weekly_used_percent": self.weekly_percent}

    def pause(self, until, reason):
        with self.shared.lock:
            if until > self.pause_until:
                self.pause_until = until
            self.state = "paused"
            self.reason = redact(reason)
            self.log.warning("%s 暫停：%s；預計 %s 再試", self.label, self.reason,
                             time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.pause_until)))
            self.shared.status()

    def check_quota(self, force=False):
        with self.shared.lock:
            if self.state == "stopped":
                return False
            if not force and self.checked_at and time.time() - self.checked_at < 60:
                return True
            self.checked_at = time.time()
            try:
                result = subprocess.run([self.orca_bin, "account", "list", "--json"],
                                        capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise ValueError(result.stderr or result.stdout)
                limits = json.loads(result.stdout)["result"]["rateLimits"]["codex"]
                session, weekly = limits["session"], limits["weekly"]
                session_used, weekly_used = session["usedPercent"], weekly["usedPercent"]
                reset = session["resetsAt"] / 1000
                if (not isinstance(session_used, (int, float)) or isinstance(session_used, bool) or
                        not isinstance(weekly_used, (int, float)) or isinstance(weekly_used, bool) or
                        not isinstance(reset, (int, float)) or reset <= 0):
                    raise ValueError("額度資料不完整")
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
                self.pause(time.time() + 900, f"無法讀取 Codex 額度：{redact(str(error))}")
                return False
            self.session_percent = session_used
            self.weekly_percent = weekly_used
            self.session_reset = reset
            if weekly_used >= self.weekly_max:
                self.state = "stopped"
                self.reason = f"Codex 週額度 {weekly_used:g}% 已達上限 {self.weekly_max:g}%"
                self.log.info(self.reason + "；本次執行不再使用 Codex")
                self.shared.status()
                return False
            if session_used >= self.session_max:
                self.pause(max(time.time() + 1, reset + 120),
                           f"Codex session 額度 {session_used:g}% 已達上限 {self.session_max:g}%")
                return False
            self.shared.status()
            return True

    def ready(self):
        while not self.shared.stop.is_set():
            with self.shared.lock:
                if self.state == "stopped":
                    return False
                remaining = self.pause_until - time.time()
                if self.state == "paused" and remaining <= 0:
                    self.state = "running"
                    self.pause_until = 0
                    self.reason = None
                    self.checked_at = 0
                    self.shared.status()
            if remaining > 0:
                self.shared.stop.wait(min(remaining, 1.0))
                continue
            if self.check_quota():
                return True
        return False

    def note_error(self, kind, reason):
        with self.shared.lock:
            self.reason = redact(reason)
            if kind == "quota":
                if self.check_quota() and self.session_reset and self.session_reset > time.time():
                    self.pause(self.session_reset + 120, "Codex 回報額度用完")
                elif self.state != "stopped" and self.state != "paused":
                    delay = min(self.shared.backoff_max, self.shared.backoff_base * 2 ** self.pause_level)
                    self.pause_level += 1
                    self.pause(time.time() + delay, "Codex 額度錯誤；重設時間不明")
                return True
            if self.state == "paused":
                return True
            self.errors += 1
            if self.errors >= 3:
                delay = min(self.shared.backoff_max, self.shared.backoff_base * 2 ** self.pause_level)
                self.pause_level += 1
                self.pause(time.time() + delay, f"連續 {self.errors} 次呼叫失敗（{self.reason}）")
                return True
            self.shared.status()
            return False

    def note_success(self):
        with self.shared.lock:
            self.errors = 0
            self.pause_level = 0
            self.reason = None

    def backoff(self):
        """額度或斷路器的指數退避秒數；呼叫端須持有 lock。"""
        delay = min(self.shared.backoff_max, self.shared.backoff_base * 2 ** self.pause_level)
        self.pause_level += 1
        return delay


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class ClaudeState(CodexState):
    """Claude Code 的訂閱額度。來源有兩個：Orca 的 claude session／weekly（每次呼叫前、最多每 60 秒讀一次），
    以及每次呼叫 stream-json 的 rate_limit_event（比 Orca 即時）。同一個額度週期內用量只增不減，
    所以取重設時間較晚的週期，同一週期取較大值。週額度上限 100 表示不設限。"""
    label = "Claude"

    def __init__(self, shared, log, weekly_max, session_max, orca_bin="orca"):
        super().__init__(shared, log, weekly_max, session_max, orca_bin)
        self.weekly_reset = None

    def snapshot(self):
        data = super().snapshot()
        data["session_resets_at"] = timestamp(self.session_reset) if self.session_reset else None
        data["weekly_resets_at"] = timestamp(self.weekly_reset) if self.weekly_reset else None
        return data

    def merge(self, session_used, session_reset, weekly_used=None, weekly_reset=None):
        """併入一筆用量；呼叫端須持有 lock。"""
        def pick(old_used, old_reset, used, reset):
            if old_reset is None or reset > old_reset + 60:
                return used, reset
            if reset >= old_reset - 60:
                return max(old_used, used), max(old_reset, reset)
            return old_used, old_reset
        self.session_percent, self.session_reset = pick(self.session_percent, self.session_reset,
                                                        session_used, session_reset)
        if weekly_used is not None:
            self.weekly_percent, self.weekly_reset = pick(self.weekly_percent, self.weekly_reset,
                                                          weekly_used, weekly_reset)

    def evaluate(self):
        """依目前的用量決定能否取段；呼叫端須持有 lock。"""
        if self.weekly_max < 100 and self.weekly_percent is not None and self.weekly_percent >= self.weekly_max:
            self.state = "stopped"
            self.reason = f"Claude 週額度 {self.weekly_percent:g}% 已達上限 {self.weekly_max:g}%"
            self.log.info(self.reason + "；本次執行不再使用 Claude")
            self.shared.status()
            return False
        if self.session_percent is not None and self.session_percent >= self.session_max:
            # 重設時間已過但資料還沒更新時，一分鐘後再讀。
            self.pause(max(time.time() + 60, (self.session_reset or 0) + 120),
                       f"Claude session 額度 {self.session_percent:g}% 已達上限 {self.session_max:g}%")
            return False
        return True

    def check_quota(self, force=False):
        with self.shared.lock:
            if self.state == "stopped":
                return False
            if not force and self.checked_at and time.time() - self.checked_at < 60:
                return self.evaluate()
            self.checked_at = time.time()
            try:
                result = subprocess.run([self.orca_bin, "account", "list", "--json"],
                                        capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise ValueError(result.stderr or result.stdout)
                limits = json.loads(result.stdout)["result"]["rateLimits"]["claude"]
                session = limits["session"]
                session_used, reset = session["usedPercent"], session["resetsAt"]
                if not is_number(session_used) or not is_number(reset) or reset <= 0:
                    raise ValueError("額度資料不完整")
                weekly = limits.get("weekly") if isinstance(limits.get("weekly"), dict) else {}
                weekly_used, weekly_reset = weekly.get("usedPercent"), weekly.get("resetsAt")
                if not is_number(weekly_used) or not is_number(weekly_reset):
                    if self.weekly_max < 100:
                        raise ValueError("週額度資料不完整")
                    weekly_used = weekly_reset = None
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
                self.pause(time.time() + 900, f"無法讀取 Claude 額度：{redact(str(error))}")
                return False
            self.merge(session_used, reset / 1000, weekly_used,
                       weekly_reset / 1000 if weekly_reset is not None else None)
            ready = self.evaluate()
            self.shared.status()
            return ready

    def note_limits(self, info):
        """併入這次呼叫的 rate_limit_event（utilization 是 0 到 1 的比例）；達上限就暫停。"""
        windows = info.get("unifiedWindows") if isinstance(info, dict) else None
        if not isinstance(windows, dict):
            return

        def window(name):
            item = windows.get(name)
            if isinstance(item, dict) and is_number(item.get("utilization")) and is_number(item.get("resetsAt")):
                return round(item["utilization"] * 100, 1), float(item["resetsAt"])
            return None, None

        session_used, reset = window("five_hour")
        if session_used is None:
            return
        with self.shared.lock:
            if self.state == "stopped":
                return
            self.merge(session_used, reset, *window("seven_day"))
            if self.state == "running":
                self.evaluate()
            self.shared.status()

    def note_error(self, kind, reason, limits=None):
        if kind != "quota":
            return super().note_error(kind, reason)
        with self.shared.lock:
            self.reason = redact(reason)
            rejected = isinstance(limits, dict) and limits.get("status") == "rejected"
            reset = limits.get("resetsAt") if rejected else None
            if is_number(reset) and time.time() < reset <= time.time() + WEEKLY_RESET_SEC:
                self.pause(reset + 120, f'Claude 回報額度用完（{limits.get("rateLimitType")}）')
            elif is_number(reset) and reset > time.time():
                # 週額度用完：使用者可能用重設券，所以定期重試（最長每 60 分鐘），不等到重設時間。
                clock = time.strftime("%m/%d %H:%M", time.localtime(reset))
                self.pause(time.time() + self.backoff(),
                           f'Claude 週額度用完（{limits.get("rateLimitType")}，預計 {clock} 重設）；定期重試')
            elif self.check_quota(force=True) and self.session_reset and self.session_reset > time.time():
                self.pause(self.session_reset + 120, "Claude 回報額度用完")
            elif self.state not in {"stopped", "paused"}:
                self.pause(time.time() + self.backoff(), "Claude 額度錯誤；重設時間不明")
            return True


def cache_path(work_dir, source, number):
    ident = hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]
    return Path(work_dir) / "chunks" / ident / f"{number:05d}.json"


def engine_model(args, engine):
    return {"agy": args.model, "codex": args.codex_model, "claude": args.claude_model}[engine]


def engine_effort(args, engine):
    return {"agy": None, "codex": args.codex_effort, "claude": args.claude_effort}[engine]


def primary_model(args, engines):
    """校正版整份的 model 欄位：有 agy 用 agy 的模型，其次 codex，再其次 claude。"""
    return engine_model(args, next(name for name in ENGINE_TOOLS if name in engines))


def reuse_existing(source, document, number, chunk, chunk_count, existing, args, digest):
    """快取遺失時，從同一版校正版取回已完成的段。"""
    if existing is None or args.force:
        return None
    correction = existing["correction"]
    expected_model = primary_model(args, args.engines.split(","))
    if (correction["prompt_sha256"] != digest or correction["model"] != expected_model or
            correction["max_search"] != args.max_search or
            len(correction["chunks"]) != chunk_count):
        return None
    prior = correction["chunks"][number - 1]
    if prior["status"] != "ok" or prior["start"] != chunk["start"] or prior["end"] != chunk["end"]:
        return None
    start, end = chunk["start_index"], chunk["end_index"]
    old_segments = existing["segments"][start:end]
    new_segments = document["segments"][start:end]
    if len(old_segments) != len(new_segments) or any(
            old["text_asr"] != new["text"] or old["start"] != new["start"] or old["end"] != new["end"]
            for old, new in zip(old_segments, new_segments)):
        return None
    return {"source": source, "chunk_number": number, **prior,
            "lines": [(item["text"], item["corrected"]) for item in old_segments]}


def correct_chunk(job, args, ws, shared, log, digest, engine="agy"):
    """ws 是該引擎的工作目錄；claude 另外需要 shared.claude_hook（hook 目錄）。"""
    source, document, number, chunk, existing = job
    if shared.stop.is_set():
        return None
    prompt, labels = build_prompt(document, chunk, args.max_search)
    original = document["segments"][chunk["start_index"]:chunk["end_index"]]
    cache = cache_path(args.work_dir, source, number)
    model = engine_model(args, engine)
    effort = engine_effort(args, engine)
    key_material = prompt + "\0" + model + "\0" + digest
    if effort:
        key_material += "\0" + effort
    key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    if cache.exists() and not args.force:
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if saved.get("key") == key and not (args.retry_failed and saved["result"]["status"] in {"partial", "failed"}):
                return saved["result"]
        except (ValueError, KeyError):
            pass
    if existing is not None and not args.force:
        return existing
    attempts = []
    elapsed = searches = 0
    failures = 0
    sequence = 0
    previous_attempts = [int(path.stem.rsplit(".attempt", 1)[1])
                         for path in cache.parent.glob(f"{cache.stem}.attempt*.json")
                         if path.stem.rsplit(".attempt", 1)[-1].isdigit()]
    first_attempt_number = max(previous_attempts, default=0)
    control = shared if engine == "agy" else shared.engines[engine]
    # 多引擎時，暫停中的引擎把段放回佇列，讓其他引擎接手。
    multi = len(shared.engines) + getattr(shared, "agy_enabled", False) > 1
    transient_retries = 0
    while failures <= args.retries and not shared.stop.is_set():
        if engine == "agy" and shared.engines and shared.agy_paused():
            return "requeue"
        # 暫停時間已過的引擎由 ready() 恢復，不能在這裡放回佇列，否則永遠輪不到它恢復。
        if engine != "agy" and multi and (control.state == "stopped" or
                                          control.state == "paused" and control.pause_until > time.time()):
            return "requeue"
        if engine != "agy" and not control.check_quota():
            if multi:
                return "requeue"
            if not control.ready():
                return None
        ready = (control.wait_if_paused(bool(shared.engines), log) if engine == "agy" else
                 control.ready())
        if ready == "requeue":
            return "requeue"
        if not ready:
            return None
        sequence += 1
        label = f"{source}#{number}#{time.time_ns()}-{sequence}"
        started = time.time()
        try:
            if engine == "agy":
                response = run_agy(prompt, model, args.print_timeout, ws, label, args.max_search, shared)
            elif engine == "codex":
                response = run_codex(prompt, model, effort, args.print_timeout, ws, shared)
            else:
                response = run_claude(prompt, model, effort, args.claude_timeout, ws, shared.claude_hook,
                                      label, args.max_search, shared, args.claude_max_budget_usd)
        except OSError as error:
            response = {"output": "", "stderr": str(error), "exit_code": 127,
                        "elapsed_sec": 0.0, "searches": 0, "usage": {}}
        if shared.stop.is_set():
            return None
        if engine == "agy":
            shared.note_account(response.get("agy_account"), started, log, "呼叫")
        if engine == "claude":
            control.note_limits(response.get("rate_limit"))
        response["evaluation"] = evaluate(response["output"], labels, original)
        response["label"] = label
        if response.get("command_executions"):
            log.warning("Codex 出現 %d 次 command_execution 事件：%s 第 %d 段",
                        response["command_executions"], source, number)
        if response.get("other_tool_uses"):
            log.warning("Claude 呼叫了 WebSearch 以外的工具 %d 次（hook 會拒絕）：%s 第 %d 段",
                        response["other_tool_uses"], source, number)
        atomic_json(cache.with_name(f"{cache.stem}.attempt{first_attempt_number + sequence}.json"),
                    {"key": key, **response})
        elapsed += response["elapsed_sec"]
        searches += response["searches"]
        kind, reason = error_kind(response)
        if kind:
            log.warning("%s 第 %d 段第 %d 次呼叫錯誤（%s）：%s", source, number, sequence, kind, reason)
            if engine == "agy":
                message, countdown = quota_details(response) if kind == "quota" else (None, None)
                paused = shared.note_error(kind, reason, log,
                                           quota_reset_seconds(response) if kind == "quota" else None,
                                           message, countdown, started)
            elif engine == "claude":
                paused = control.note_error(kind, reason, response.get("rate_limit"))
            else:
                paused = control.note_error(kind, reason)
            if kind == "quota" and multi:
                return "requeue"
            if paused:
                continue
            if (engine == "codex" and kind == "network" and "stream disconnected before completion" in reason
                    and transient_retries < 3):
                transient_retries += 1
                continue
            failures += 1
        else:
            if engine == "agy":
                shared.note_success(started, log)
            else:
                control.note_success()
            attempts.append(response)
            if response["evaluation"]["valid"]:
                break
            failures += 1
        if failures <= args.retries:
            with shared.lock:
                shared.counts["retries"] += 1
                shared.status()
    if shared.stop.is_set() and failures <= args.retries and not any(
            item["evaluation"]["valid"] for item in attempts):
        return None
    status, lines = choose_result(attempts, original)
    result = {"source": source, "chunk_number": number, "start": chunk["start"],
              "end": chunk["end"], "status": status, "attempts": sequence,
              "searches": searches, "elapsed_sec": elapsed, "lines": lines,
              "fallback_lines": sum(not corrected for _, corrected in lines),
              "engine": ENGINE_TOOLS[engine], "model": model, "effort": effort}
    atomic_json(cache, {"key": key, "result": result})
    return result


def log_setup(work_dir):
    log = logging.getLogger("haixia.correct")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in (logging.StreamHandler(sys.stdout),
                    logging.FileHandler(Path(work_dir) / "logs/correct.log", encoding="utf-8")):
        handler.setFormatter(formatter)
        log.addHandler(handler)
    return log


def collect(args, log):
    selected = []
    if args.files:
        for line in args.files.read_text(encoding="utf-8").splitlines():
            relative = Path(line.strip())
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if relative.is_absolute() or ".." in relative.parts:
                log.error("略過不安全的檔案路徑：%s", line)
                continue
            selected.append(relative)
    else:
        selected = sorted(item.relative_to(args.in_dir) for item in args.in_dir.rglob("*.json"))
    documents = {}
    existing_docs = {}
    failed_files = set()
    total_hours = selected_hours = 0.0
    for relative in selected:
        source = relative.as_posix().removesuffix(".json")
        if args.include and not any(source.startswith(prefix) for prefix in args.include):
            continue
        if not relative.name.endswith(".json"):
            log.error("略過非 JSON 檔：%s", relative)
            continue
        try:
            document = validate(json.loads((args.in_dir / relative).read_text(encoding="utf-8")))
            if document["source"] != source:
                raise ValueError("source 與相對檔名不一致")
        except (OSError, ValueError) as error:
            log.error("壞檔，略過：%s：%s", relative, error)
            continue
        selected_hours += document["duration_sec"] / 3600
        output = args.out_dir / relative
        existing = None
        if output.exists() and not args.force:
            try:
                existing = validate_corrected(json.loads(output.read_text(encoding="utf-8")))
                if existing["source"] != source:
                    raise ValueError("校正版 source 與相對檔名不一致")
                if any(chunk["status"] == "failed" for chunk in existing["correction"]["chunks"]):
                    failed_files.add(source)
            except (OSError, ValueError) as error:
                action = "將重做" if args.retry_failed else "仍跳過；可用 --retry-failed 或 --force 重做"
                log.warning("既有校正版無法驗證，%s：%s：%s", action, relative, error)
                if not args.retry_failed:
                    if args.limit_hours is not None and selected_hours >= args.limit_hours:
                        break
                    continue
            if existing is not None and (not args.retry_failed or all(
                    chunk["status"] == "ok" for chunk in existing["correction"]["chunks"])):
                if args.limit_hours is not None and selected_hours >= args.limit_hours:
                    break
                continue
        documents[source] = (relative, document, split_chunks(document["segments"]))
        if existing is not None:
            existing_docs[source] = existing
        total_hours += document["duration_sec"] / 3600
        if args.limit_hours is not None and selected_hours >= args.limit_hours:
            break
    return documents, total_hours, existing_docs, failed_files


def main(argv=None):
    parser = argparse.ArgumentParser(description="用 Antigravity / Codex / Claude Code CLI 批次校正逐字稿")
    for name in ("in-dir", "out-dir", "work-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--engines", default="agy")
    parser.add_argument("--agy-jobs", "--jobs", dest="agy_jobs", type=int, default=4)
    parser.add_argument("--codex-jobs", type=int, default=4)
    parser.add_argument("--codex-model", default="gpt-6-sol")
    parser.add_argument("--codex-effort", default="medium")
    parser.add_argument("--codex-mode", choices=("relay", "parallel"), default="relay",
                        help="Codex 與 Claude 共用：relay 只在 Antigravity 暫停或沒啟用時取段；parallel 隨時取段")
    parser.add_argument("--codex-weekly-max", type=float, default=80)
    parser.add_argument("--codex-session-max", type=float, default=85)
    parser.add_argument("--claude-jobs", type=int, default=4)
    parser.add_argument("--claude-model", default="claude-opus-5-5")
    parser.add_argument("--claude-effort", default="medium")
    parser.add_argument("--claude-session-max", type=float, default=80)
    parser.add_argument("--claude-weekly-max", type=float, default=100, help="100 表示不設限")
    parser.add_argument("--claude-timeout", type=int, default=300,
                        help="Claude 每次呼叫的逾時秒數；正常一段 10 到 70 秒，失控的呼叫會一直耗額度")
    parser.add_argument("--claude-max-budget-usd", type=float, default=1.0,
                        help="Claude 每次呼叫的估算金額上限（依 API 牌價；正常一段約 0.1 到 0.25 美元）")
    parser.add_argument("--model", default="gemini-3.8-flash-high")
    parser.add_argument("--max-search", type=int, default=5)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--print-timeout", type=int, default=900)
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--files", type=Path)
    parser.add_argument("--limit-hours", type=float)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--quota-poll-min", type=float,
                        help="Antigravity 額度暫停最多幾分鐘就由一個 worker 試探；不給則等到重設時間")
    parser.add_argument("--expected-agy-account", metavar="EMAIL",
                        help="預期 agy 登入的帳號，不符就暫停 Antigravity；<work-dir>/expected-account 檔優先，每次檢查都重讀")
    args = parser.parse_args(argv)
    args.expected_agy_account = (args.expected_agy_account or "").strip() or None
    engines = args.engines.split(",")
    if not engines or len(set(engines)) != len(engines) or any(engine not in ENGINE_TOOLS for engine in engines):
        parser.error("--engines 是 agy、codex、claude 以逗號分隔的組合，例如 agy,claude")
    if (args.agy_jobs < 1 or args.codex_jobs < 1 or args.claude_jobs < 1 or args.max_search < 0 or
            args.retries < 0 or args.print_timeout < 1 or args.claude_timeout < 1 or
            not args.claude_max_budget_usd > 0 or (args.limit_hours is not None and args.limit_hours <= 0) or
            not 0 <= args.codex_weekly_max <= 100 or not 0 <= args.codex_session_max <= 100 or
            not 0 <= args.claude_weekly_max <= 100 or not 0 <= args.claude_session_max <= 100 or
            (args.quota_poll_min is not None and not args.quota_poll_min > 0)):
        parser.error("jobs、print-timeout、limit-hours、quota-poll-min 必須為正數；max-search、retries 不可為負數")
    args.in_dir, args.out_dir, args.work_dir = (item.resolve() for item in (args.in_dir, args.out_dir, args.work_dir))
    (args.work_dir / "logs").mkdir(parents=True, exist_ok=True)
    log = log_setup(args.work_dir)
    ws = setup_hook(args.work_dir) if "agy" in engines else None
    codex_ws = args.work_dir / "codex-ws"
    if "codex" in engines:
        if args.work_dir == ROOT or ROOT in args.work_dir.parents:
            raise ValueError("--work-dir 必須放在 repo 外面")
        codex_ws.mkdir(parents=True, exist_ok=True)
        if any(codex_ws.iterdir()):
            raise ValueError("codex-ws 工作目錄必須是空的")
    claude_ws, claude_hook = setup_claude(args.work_dir) if "claude" in engines else (None, None)
    documents, hours, existing_docs, failed_files = collect(args, log)
    digest = prompt_sha256()
    jobs = [(source, document, number, chunk,
             reuse_existing(source, document, number, chunk, len(chunks), existing_docs.get(source), args, digest))
            for source, (_, document, chunks) in documents.items()
            for number, chunk in enumerate(chunks, 1)]
    log.info("開始：%d 檔，%.2f 音訊小時，%d 段，engines=%s，codex-mode=%s",
             len(documents), hours, len(jobs), args.engines, args.codex_mode)
    if "claude" in engines:
        log.info("Claude：model=%s，effort=%s，session 上限 %g%%，週上限 %s，逾時 %d 秒，每次呼叫上限 %g 美元",
                 args.claude_model, args.claude_effort, args.claude_session_max,
                 "不設限" if args.claude_weekly_max >= 100 else f"{args.claude_weekly_max:g}%",
                 args.claude_timeout, args.claude_max_budget_usd)
    if args.dry_run:
        for source, document, number, chunk, _existing in jobs:
            prompt, _ = build_prompt(document, chunk, args.max_search)
            path = args.work_dir / "prompts" / hashlib.sha256(source.encode()).hexdigest()[:20] / f"{number:05d}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prompt + "\n", encoding="utf-8")
            log.info("提示詞：%s 第 %d 段 → %s", source, number, path)
        return 0
    empty_sources = [source for source, (_, _, chunks) in documents.items() if not chunks]
    if not jobs:
        shared = SharedState(args.work_dir, 0, hours, len(documents))
        shared.agy_enabled = "agy" in engines
        shared.codex_mode = args.codex_mode
        shared.agy_stopped = True
        shared.failed_files = failed_files.copy()
        for source in empty_sources:
            relative, original, chunks = documents[source]
            save_corrected(corrected_document(original, chunks, [], primary_model(args, engines),
                                              args.max_search, digest), args.out_dir / relative)
            shared.counts["files_done"] += 1
            shared.audio_hours_done += original["duration_sec"] / 3600
        shared.status("finished")
        log.info("摘要：0 段，輸出 %d 個空白逐字稿", len(empty_sources))
        if failed_files:
            log.error("仍有失敗段落的檔案：%s", "、".join(sorted(failed_files)))
        return 1 if failed_files else 0
    if "agy" in engines:
        account_started = time.time()
        startup_account = check_model(args.model, agy_log_path(args.work_dir, "models"))
        prune_agy_logs(args.work_dir)
        check_hook(ws, args.max_search)
    if "claude" in engines:
        check_claude_hook(claude_hook, args.max_search)
    base = float(os.environ.get("HAIXIA_BACKOFF_BASE_SEC", "300"))
    maximum = float(os.environ.get("HAIXIA_BACKOFF_MAX_SEC", "3600"))
    shared = SharedState(args.work_dir, len(jobs), hours, len(documents), base, maximum)
    shared.agy_enabled = "agy" in engines
    shared.codex_mode = args.codex_mode
    if "agy" in engines:
        shared.expected_arg = args.expected_agy_account
        shared.account_poll = float(os.environ.get("HAIXIA_ACCOUNT_POLL_SEC", "120"))
        with shared.lock:
            shared.account_expected = shared.expected_account()
        if shared.account_expected:
            log.info("預期的 Antigravity 帳號：%s；帳號不符就暫停 Antigravity。要換預期帳號就改 %s（不必重啟）",
                     shared.account_expected, args.work_dir / "expected-account")
        else:
            log.info("沒有設定預期的 Antigravity 帳號：只記錄每次呼叫實際登入的帳號，不管控")
        shared.note_account(startup_account, account_started, log, "啟動檢查")
    if args.quota_poll_min is not None:
        shared.quota_poll = args.quota_poll_min * 60
        if "agy" in engines:
            log.info("Antigravity 額度暫停最多 %g 分鐘就試探一次；換好帳號後 touch %s 可立即重試",
                     args.quota_poll_min, args.work_dir / "resume-now")
    if "codex" in engines:
        shared.engines["codex"] = CodexState(shared, log, args.codex_weekly_max,
                                               args.codex_session_max)
    if "claude" in engines:
        shared.engines["claude"] = ClaudeState(shared, log, args.claude_weekly_max,
                                                 args.claude_session_max)
        shared.claude_hook = claude_hook
    shared.status()
    shared.failed_files = failed_files.copy()
    for source in empty_sources:
        relative, original, chunks = documents[source]
        save_corrected(corrected_document(original, chunks, [], primary_model(args, engines),
                                          args.max_search, digest), args.out_dir / relative)
        shared.counts["files_done"] += 1
        shared.audio_hours_done += original["duration_sec"] / 3600
    if empty_sources:
        shared.status()
    by_file = {source: {} for source in documents}
    interrupted = False
    waiting = deque(jobs)
    condition = threading.Condition(shared.lock)
    results = queue.Queue()
    inflight = 0

    def record(job, result, engine):
        source, document, number, chunk, _existing = job
        if isinstance(result, Exception):
            error = result
            log.error("失敗：%s 第 %d 段：%s", source, number, redact(str(error)))
            shared.last_error = redact(str(error))
            status, lines = choose_result([], document["segments"][chunk["start_index"]:chunk["end_index"]])
            result = {"source": source, "chunk_number": number, "start": chunk["start"],
                      "end": chunk["end"], "status": status, "attempts": 0,
                      "searches": 0, "elapsed_sec": 0.0, "lines": lines,
                      "fallback_lines": len(lines), "engine": ENGINE_TOOLS[engine],
                      "model": engine_model(args, engine), "effort": engine_effort(args, engine)}
        if result is None:
            return
        by_file[source][number] = result
        file_chunks = documents[source][2]
        audio_start = (document["clip"]["start"] if document["clip"] else 0.0) if number == 1 else chunk["start"]
        audio_end = (file_chunks[number]["start"] if number < len(file_chunks) else
                     (document["clip"]["start"] if document["clip"] else 0.0) + document["duration_sec"])
        shared.completed(result, chunk, max(0, audio_end - audio_start) / 3600, log)
        if len(by_file[source]) == len(documents[source][2]):
            relative, original, chunks = documents[source]
            corrected = corrected_document(original, chunks,
                                           [by_file[source][i] for i in range(1, len(chunks) + 1)],
                                           primary_model(args, engines), args.max_search, digest)
            save_corrected(corrected, args.out_dir / relative)
            if any(item["status"] == "failed" for item in by_file[source].values()):
                failed_files.add(source)
            else:
                failed_files.discard(source)
            with shared.lock:
                shared.counts["files_done"] += 1
                shared.failed_files = failed_files.copy()
                shared.status()

    def worker(engine):
        nonlocal inflight
        control = shared if engine == "agy" else shared.engines[engine]
        engine_ws = {"agy": ws, "codex": codex_ws, "claude": claude_ws}[engine]
        while not shared.stop.is_set():
            with condition:
                if not waiting and inflight == 0:
                    return
                if engine == "agy":
                    available = (not shared.agy_paused() and
                                 not (shared.probing and shared.probe_owner is not None))
                else:
                    if control.state == "stopped":
                        return
                    available = (control.pause_until <= time.time() and
                                 (args.codex_mode == "parallel" or not shared.agy_enabled or
                                  shared.agy_paused()))
                if not waiting or not available:
                    condition.wait(.2)
                    continue
            if engine != "agy" and not control.check_quota():
                continue
            with condition:
                if not waiting or (engine != "agy" and args.codex_mode == "relay" and
                                   shared.agy_enabled and not shared.agy_paused()):
                    continue
                job = waiting.popleft()
                inflight += 1
            try:
                result = correct_chunk(job, args, engine_ws, shared, log, digest, engine)
            except Exception as error:
                result = error
            finally:
                if engine == "agy":
                    shared.release_probe()
            with condition:
                inflight -= 1
                if result == "requeue":
                    waiting.appendleft(job)
                else:
                    results.put((job, result, engine))
                condition.notify_all()

    jobs_per_engine = {"agy": args.agy_jobs, "codex": args.codex_jobs, "claude": args.claude_jobs}
    threads = [threading.Thread(target=worker, args=(engine,), daemon=True)
               for engine in engines for _ in range(jobs_per_engine[engine])]
    try:
        for thread in threads:
            thread.start()
        next_progress = time.monotonic() + 300
        while any(thread.is_alive() for thread in threads) or not results.empty():
            try:
                job, result, engine = results.get(timeout=.2)
                record(job, result, engine)
            except queue.Empty:
                pass
            if shared.agy_enabled:
                shared.check_resume_now(log)
                shared.poll_account(log)
            if time.monotonic() >= next_progress:
                shared.progress(log)
                next_progress = time.monotonic() + 300
    except KeyboardInterrupt:
        interrupted = True
        shared.stop.set()
        log.warning("收到 Ctrl-C，停止派新段並終止進行中的 CLI 呼叫")
        shared.stop_processes()
    finally:
        for thread in threads:
            thread.join()
    while not results.empty():
        record(*results.get())
    shared.progress(log)
    for control in shared.engines.values():
        if control.state == "running":
            control.state = "stopped"
            control.reason = "執行結束"
    shared.agy_stopped = True
    shared.status("aborted" if interrupted else "finished")
    log.info("摘要：完成 %d/%d 段，ok %d，partial %d，失敗 %d，輸出 %d 檔",
             shared.counts["chunks_done"], shared.counts["chunks_total"],
             shared.counts["chunks_ok"], shared.counts["chunks_partial"],
             shared.counts["chunks_failed"], shared.counts["files_done"])
    if failed_files:
        log.error("仍有失敗段落的檔案：%s", "、".join(sorted(failed_files)))
    return 2 if interrupted else 1 if failed_files or shared.counts["chunks_done"] < len(jobs) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError) as error:
        sys.exit(f"校正失敗：{error}")
