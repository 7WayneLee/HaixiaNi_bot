"""Claude 金鑰：/setkey、/key、/usekey、/delkey 與切換按鈕。

只用假 Anthropic client、假 bot 與暫存目錄裡的 .env／金鑰清單，不連網、不讀真的 .env。金鑰都是明顯假的 sk-ant-test-…。
"""

import importlib.util
import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from haixia import answer as core
from haixia import telegram_bot as tg
from test_telegram_bot import (CHAT, USER, STRANGER, FakeAnswerer, FakeBot, FakeCallback, _message, _receive,
                               make_core, run)  # noqa: F401 — make_core 是 fixture

OLD_KEY = "sk-ant-test-" + "old_" * 12 + "OLD1"
NEW_KEY = "sk-ant-test-" + "new_" * 12 + "abcd"
SPARE_KEY = "sk-ant-test-" + "spare_" * 8 + "wxyz"
INVALID_KEY = "sk-ant-test-" + "bad_" * 12 + "!"
ALL_KEYS = (OLD_KEY, NEW_KEY, SPARE_KEY, INVALID_KEY)
ENV_BEFORE = ("# 保留註解\r\nOTHER='不動'\r\n\r\nANTHROPIC_API_KEY=" + OLD_KEY
              + "\r\nANTHROPIC_WORKSPACE_ID=wrkspc_test\r\n# 尾端不換行")
OPUS, SONNET, FLASH = tg.MODELS["opus"], tg.MODELS["sonnet"], tg.MODELS["gemini-flash"]


def env_with(key, workspace="wrkspc_test"):
    """.env 換成 key；workspace=None 時那一行被刪掉（綁定 workspace 的金鑰）。"""
    text = ENV_BEFORE.replace(OLD_KEY, key)
    if workspace is None:
        text = text.replace("\r\nANTHROPIC_WORKSPACE_ID=wrkspc_test", "")
    else:
        text = text.replace("wrkspc_test", workspace)
    return text.encode()


@pytest.fixture(autouse=True)
def debug_logs(caplog):
    caplog.set_level(logging.DEBUG)


@pytest.fixture
def key_env(tmp_path, monkeypatch):
    folder = tmp_path / "repo"
    folder.mkdir()
    path = folder / ".env"
    path.write_bytes(ENV_BEFORE.encode())
    path.chmod(0o600)
    monkeypatch.setattr(core, "ENV_PATH", path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    return SimpleNamespace(folder=folder, env=path, keys=folder / core.KEYS_NAME, before=ENV_BEFORE.encode())


def write_store(key_env, active, keys, mode=0o600):
    key_env.keys.write_text(json.dumps({"active": active, "keys": keys}, ensure_ascii=False))
    key_env.keys.chmod(mode)
    key_env.env.write_bytes(env_with(keys[active]))
    return key_env.keys.read_bytes(), key_env.env.read_bytes()


def saved_raw(key_env):
    return json.loads(key_env.keys.read_text(encoding="utf-8"))


def saved(key_env):
    """清單裡的 {active, keys: {名稱: 金鑰}}；workspace 另用 saved_workspaces 看。"""
    data = saved_raw(key_env)
    return {"active": data["active"],
            "keys": {name: entry["key"] for name, entry in data["keys"].items()}}


def saved_workspaces(key_env):
    return {name: entry["workspace"] for name, entry in saved_raw(key_env)["keys"].items()}


def mode(path):
    return path.stat().st_mode & 0o777


def folder_names(key_env):
    return sorted(item.name for item in key_env.folder.iterdir())


@pytest.fixture
def validation(monkeypatch):
    """假的 anthropic client：problems 依金鑰指定 models.list 要丟的例外。"""
    fake = SimpleNamespace(problems={}, close_problem=None, events=[])

    class Client:
        def __init__(self, key, workspace, **options):
            self.key = key
            fake.events.append(("create", key, workspace, options, threading.get_ident()))
            self.models = self

        def list(self, **options):
            fake.events.append(("list", options, threading.get_ident()))
            if self.key in fake.problems:
                raise fake.problems[self.key]
            return SimpleNamespace(data=[SimpleNamespace(id="測試模型")])

        def close(self):
            fake.events.append(("close", threading.get_ident()))
            if fake.close_problem is not None:
                raise fake.close_problem

    monkeypatch.setattr(core, "make_client", Client)
    fake.verified = lambda: [event[1] for event in fake.events if event[0] == "create"]
    return fake


def status_problem(status):
    import anthropic
    import httpx2

    request = httpx2.Request("GET", "https://example.invalid/v1/models")
    response = httpx2.Response(status, request=request)
    return anthropic.APIStatusError(f"測試錯誤 {NEW_KEY}", response=response, body={"secret": NEW_KEY})


def connection_problem(kind):
    import anthropic
    import httpx2

    request = httpx2.Request("GET", "https://example.invalid/v1/models")
    if kind == "timeout":
        return anthropic.APITimeoutError(request)
    return anthropic.APIConnectionError(message=f"測試斷線 {NEW_KEY}", request=request)


def assert_no_keys(bot, caplog, *callbacks):
    """回覆、按鈕（callback data）、按鈕回應與 log 都不能有完整金鑰；不把金鑰印進失敗訊息。"""
    shown = repr(bot.calls) + caplog.text + repr([callback.answers for callback in callbacks])
    leaked = sum(key in shown for key in ALL_KEYS)
    assert leaked == 0, "回覆、按鈕或 log 出現完整金鑰"


def press(bot_core, bot, data, user_id=USER):
    """走正式的 CallbackQueryHandler 入口。"""
    pytest.importorskip("telegram")
    application = tg.build_application(bot_core, "123456:TEST-TOKEN-NOT-REAL")
    handler = [handler for group in application.handlers.values() for handler in group][1]
    callback = FakeCallback(data)
    update = SimpleNamespace(callback_query=callback, effective_user=SimpleNamespace(id=user_id))
    run(handler.callback(update, SimpleNamespace(bot=bot)))
    return callback


def calls_named(bot, name):
    return [kwargs for kind, kwargs in bot.calls if kind == name]


# ---------- /setkey ----------

def test_setkey_success_preserves_file_history_costs_and_gemini(key_env, validation, tmp_path, caplog):
    created = []
    searcher = SimpleNamespace(store=None, close=lambda: None)

    class Answerer(FakeAnswerer):
        def __init__(self, model, shared):
            super().__init__(model, None)
            self.key = core.resolve_api_key() if model.startswith("claude-") else None
            self.closed_on = []
            created.append(self)
            assert shared is searcher

        def close(self):
            self.closed_on.append(threading.get_ident())

    pool = tg.AnswererPool(lambda: searcher, Answerer)
    bot_core = tg.BotCore({USER}, pool, log_dir=tmp_path / "logs")
    bot = FakeBot()
    try:
        opus = bot_core.executor.submit(pool.get, OPUS).result()
        sonnet = bot_core.executor.submit(pool.get, SONNET).result()
        gemini = bot_core.executor.submit(pool.get, FLASH).result()
        assert opus.key == OLD_KEY
        run(bot_core.handle_message(bot, CHAT, USER, "舊問題"))
        state = bot_core.state(CHAT)
        history = state.conversation
        previous_messages = list(history.messages)
        previous_activity, previous_context = state.last_active, state.context_tokens
        bot_core.log_path.parent.mkdir(exist_ok=True)
        bot_core.log_path.write_text('{"cost_usd": 0.123}\n')
        old_log = bot_core.log_path.read_bytes()
        bot.calls.clear()
        _receive(bot_core, bot, _message(f"/setkey@HaixiaBot   {NEW_KEY}  "))
        assert bot.calls[0] == ("delete", {"chat_id": CHAT, "message_id": 1})
        # 第一次寫入：.env 原有的金鑰是「預設」，只打金鑰就是更新「預設」。
        # 沒給 workspace id＝綁定 workspace 的金鑰，.env 的 ANTHROPIC_WORKSPACE_ID 那一行刪掉。
        assert key_env.env.read_bytes() == env_with(NEW_KEY, None)
        assert saved_workspaces(key_env) == {"預設": None}
        assert saved(key_env) == {"active": "預設", "keys": {"預設": NEW_KEY}}
        assert mode(key_env.env) == mode(key_env.keys) == 0o600
        assert folder_names(key_env) == [".anthropic_keys.json", ".env"]
        assert set(pool.answerers) == {FLASH}
        worker_id = validation.events[0][-1]
        assert worker_id != threading.get_ident()
        assert validation.events == [
            ("create", NEW_KEY, None, {"timeout": 10.0, "max_retries": 0}, worker_id),
            ("list", {"limit": 1}, worker_id), ("close", worker_id)]
        assert opus.closed_on == sonnet.closed_on == [worker_id]
        assert not gemini.closed_on and pool.get(FLASH) is gemini
        assert state.conversation is history and history.messages == previous_messages
        assert (state.last_active, state.context_tokens) == (previous_activity, previous_context)
        assert bot_core.log_path.read_bytes() == old_log
        assert bot.texts() == ["已更新 預設 …abcd，下一題開始生效。舊金鑰記得到 Anthropic Console 撤銷。"]
        assert_no_keys(bot, caplog)
        run(bot_core.handle_message(bot, CHAT, USER, "下一題"))
        replacement = pool.answerers[OPUS]
        assert replacement is not opus and replacement.key == NEW_KEY
        assert replacement.calls[-1]["conversation"] is history
    finally:
        bot_core.close()


def test_setkey_with_name_adds_key_and_switches(make_core, key_env, validation, caplog):
    bot_core, pool = make_core()
    pool.get(OPUS)
    gemini = pool.get(FLASH)
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey 備用-2 {NEW_KEY}"))
    assert bot.texts() == ["已存 備用-2 …abcd 並改用，下一題開始生效。"]
    assert saved(key_env) == {"active": "備用-2", "keys": {"預設": OLD_KEY, "備用-2": NEW_KEY}}
    assert key_env.env.read_bytes() == env_with(NEW_KEY, None) and mode(key_env.keys) == 0o600
    # 從 .env 帶進來的「預設」保留原本的 workspace id
    assert saved_workspaces(key_env) == {"預設": "wrkspc_test", "備用-2": None}
    assert set(pool.answerers) == {FLASH} and pool.get(FLASH) is gemini
    assert validation.verified() == [NEW_KEY]
    assert_no_keys(bot, caplog)


def test_setkey_replaces_named_key(make_core, key_env, validation, caplog):
    write_store(key_env, "預設", {"預設": OLD_KEY, "備用": SPARE_KEY})
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey 備用 {NEW_KEY}"))
    assert bot.texts() == ["已更新 備用 …abcd，下一題開始生效。舊金鑰記得到 Anthropic Console 撤銷。"]
    assert saved(key_env) == {"active": "備用", "keys": {"預設": OLD_KEY, "備用": NEW_KEY}}
    assert key_env.env.read_bytes() == env_with(NEW_KEY, None)
    # 同一把再存一次：不提醒撤銷（沒有被換掉的舊金鑰）。
    _receive(bot_core, bot, _message(f"/setkey 備用 {NEW_KEY}"))
    assert bot.texts()[-1] == "已更新 備用 …abcd，下一題開始生效。"
    assert_no_keys(bot, caplog)


@pytest.mark.parametrize("status", [401, 403, 400, 429, 500, 529])
def test_setkey_validation_status_does_not_write(make_core, key_env, validation, caplog, status):
    bot_core, pool = make_core()
    existing = pool.get(OPUS)
    validation.problems[NEW_KEY] = status_problem(status)
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey {NEW_KEY}"))
    expected = ("金鑰無效或沒有權限，沒有更換。" if status in (401, 403)
                else "暫時無法驗證，沒有更換，稍後再試。")
    assert bot.texts() == [expected]
    assert bot.calls[0][0] == "delete" and validation.events[-1][0] == "close"
    assert key_env.env.read_bytes() == key_env.before and not key_env.keys.exists()
    assert pool.get(OPUS) is existing
    assert f"HTTP {status}" in caplog.text
    assert_no_keys(bot, caplog)


@pytest.mark.parametrize("kind", ["network", "timeout"])
def test_setkey_connection_failure_does_not_write(make_core, key_env, validation, caplog, kind):
    validation.problems[NEW_KEY] = connection_problem(kind)
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey 備用 {NEW_KEY}"))
    assert bot.texts() == ["暫時無法驗證，沒有更換，稍後再試。"]
    assert key_env.env.read_bytes() == key_env.before and not key_env.keys.exists()
    assert bot.calls[0][0] == "delete"
    assert_no_keys(bot, caplog)


@pytest.mark.parametrize("arg, reply", [
    (INVALID_KEY, "金鑰格式不對。"),
    ("sk-ant-test-short", "金鑰格式不對。"),
    ("wrong-" + "x" * 60, "金鑰格式不對。"),
    ("sk-ant-test-" + "x" * 300, "金鑰格式不對。"),
    ("備用 " + INVALID_KEY, "金鑰格式不對。"),
    (NEW_KEY + " 另一項", tg.SETKEY_FORMAT),
    (NEW_KEY + "\n另一項", tg.SETKEY_FORMAT),
    ("名 稱 " + NEW_KEY, tg.SETKEY_FORMAT),
    ("一二三四五六七八九十一二三四五六七八九十多 " + NEW_KEY, tg.NAME_RULE),
    ("壞/名 " + NEW_KEY, tg.NAME_RULE),
    ("sk-ant-x " + NEW_KEY, tg.SETKEY_FORMAT),
], ids=["bad-char", "short", "wrong-prefix", "too-long", "named-bad-key", "trailing-word", "trailing-line",
        "three-parts", "long-name", "slash-name", "key-like-name"])     # 測試名稱不帶金鑰
def test_setkey_bad_input_deletes_without_validation(make_core, key_env, validation, caplog, arg, reply):
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey {arg}"))
    assert bot.texts() == [reply]
    assert key_env.env.read_bytes() == key_env.before and not key_env.keys.exists()
    assert validation.events == [] and bot.calls[0][0] == "delete"
    assert_no_keys(bot, caplog)


@pytest.mark.parametrize("command, reply", [
    ("/setkey   ", tg.SETKEY_USAGE),
    ("/usekey", "用法：/usekey 名稱（/key 看清單）"),
    ("/delkey", "用法：/delkey 名稱"),
])
def test_key_commands_without_arg_only_show_usage(make_core, key_env, validation, command, reply):
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message(command))
    assert bot.texts() == [reply]
    assert [name for name, _ in bot.calls] == ["send"]
    assert key_env.env.read_bytes() == key_env.before and not key_env.keys.exists()
    assert not validation.events
    assert "會自動刪掉含金鑰的訊息" in tg.SETKEY_USAGE


@pytest.mark.parametrize("outcome", ["success", "invalid", "401", "network", "false"])
def test_delete_failure_always_reminds_user(make_core, key_env, validation, caplog, outcome):
    class BadDelete(Exception):
        pass

    class NoDeleteBot(FakeBot):
        async def delete_message(self, **kwargs):
            await super().delete_message(**kwargs)
            if outcome == "false":
                return False
            raise BadDelete(f"測試刪除失敗 {NEW_KEY}")

    key = INVALID_KEY if outcome == "invalid" else NEW_KEY
    if outcome == "401":
        validation.problems[NEW_KEY] = status_problem(401)
    elif outcome == "network":
        validation.problems[NEW_KEY] = connection_problem("network")
    bot_core, _ = make_core()
    bot = NoDeleteBot()
    _receive(bot_core, bot, _message(f"/setkey {key}"))
    assert bot.calls[0][0] == "delete"
    assert bot.texts()[0].endswith("\n請手動刪掉剛才那則訊息。")
    assert ("RuntimeError（HTTP None）" if outcome == "false" else "BadDelete（HTTP None）") in caplog.text
    assert_no_keys(bot, caplog)


def test_setkey_refuses_when_store_is_full(make_core, key_env, validation, caplog):
    keys = {f"k{index}": "sk-ant-test-" + f"{index:02d}_" * 12 for index in range(core.MAX_SAVED_KEYS)}
    before_store, _ = write_store(key_env, "k0", keys)
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey 新的 {NEW_KEY}"))
    assert bot.texts() == ["最多存 10 把，先 /delkey 刪一把。"]
    assert key_env.keys.read_bytes() == before_store and not validation.events
    # 已有的名稱仍可更新。
    _receive(bot_core, bot, _message(f"/setkey k3 {NEW_KEY}"))
    assert bot.texts()[-1].startswith("已更新 k3 …abcd")
    assert len(saved(key_env)["keys"]) == core.MAX_SAVED_KEYS
    assert_no_keys(bot, caplog)


def test_env_write_failure_restores_store_and_leaves_no_temp_files(make_core, key_env, validation, monkeypatch,
                                                                   caplog):
    bot_core, pool = make_core()
    existing = pool.get(OPUS)
    real_replace = os.replace

    def fail_env(source, target):
        if Path(target) == key_env.env:
            assert Path(source).parent == key_env.folder and mode(Path(source)) == 0o600
            raise PermissionError(f"測試無法寫入 {NEW_KEY}")
        return real_replace(source, target)

    monkeypatch.setattr(core.os, "replace", fail_env)
    bot = FakeBot()
    # 清單檔還不存在：失敗後也不留下清單檔。
    _receive(bot_core, bot, _message(f"/setkey {NEW_KEY}"))
    assert bot.texts() == ["寫入金鑰失敗，沒有更換，稍後再試。"]
    assert key_env.env.read_bytes() == key_env.before and folder_names(key_env) == [".env"]
    # 清單檔已存在：還原成原本的內容。
    monkeypatch.setattr(core.os, "replace", real_replace)
    before_store, before_env = write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY})
    monkeypatch.setattr(core.os, "replace", fail_env)
    _receive(bot_core, bot, _message("/usekey 備用"))
    assert bot.texts()[-1] == "寫入失敗，沒有切換，稍後再試。"
    assert key_env.keys.read_bytes() == before_store and key_env.env.read_bytes() == before_env
    assert folder_names(key_env) == [".anthropic_keys.json", ".env"]
    assert pool.get(OPUS) is existing
    assert_no_keys(bot, caplog)


def test_client_close_failure_still_rotates(make_core, key_env, validation, caplog):
    validation.close_problem = RuntimeError(NEW_KEY)
    bot_core, pool = make_core()
    previous = pool.get(OPUS)

    def fail_close():
        raise RuntimeError(OLD_KEY)

    previous.close = fail_close
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey {NEW_KEY}"))
    assert bot.texts()[0].startswith("已更新 預設 …abcd") and OPUS not in pool.answerers
    assert core.resolve_api_key() == NEW_KEY
    assert_no_keys(bot, caplog)


# ---------- /key ----------

def test_key_lists_names_suffixes_and_switch_buttons(make_core, key_env, validation, caplog):
    write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY, "第三": SPARE_KEY})
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message("/key"))
    [(kind, sent)] = bot.calls
    assert kind == "send"
    # 舊格式清單的每把金鑰都沿用 .env 的 workspace id，只顯示後 4 碼
    assert sent["text"] == ("Claude 金鑰：\n✓ 預設 …OLD1 wrkspc_…test（使用中）\n・備用 …abcd wrkspc_…test\n"
                            "・第三 …wxyz wrkspc_…test\n"
                            "點按鈕切換。新增：/setkey 名稱 金鑰")
    buttons = [[(button.text, button.callback_data) for button in row] for row in sent["reply_markup"].inline_keyboard]
    assert buttons == [[("改用 備用", "k:" + tg.key_token("備用"))], [("改用 第三", "k:" + tg.key_token("第三"))]]
    assert all(tg.KEY_CALLBACK.fullmatch(data) for row in buttons for _, data in row)
    assert not validation.events
    assert_no_keys(bot, caplog)


def test_key_first_run_uses_env_key_without_writing(make_core, key_env, validation, caplog):
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message("/key"))
    assert bot.texts() == ["Claude 金鑰：\n✓ 預設 …OLD1 wrkspc_…test（使用中）\n新增：/setkey 名稱 金鑰"]
    assert "reply_markup" not in calls_named(bot, "send")[0]
    assert folder_names(key_env) == [".env"] and not validation.events
    assert_no_keys(bot, caplog)


def test_key_marks_env_key_missing_from_list(make_core, key_env, validation, caplog):
    write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY})
    key_env.env.write_bytes(env_with(SPARE_KEY))         # 有人直接改了 .env
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message("/key"))
    text = bot.texts()[0]
    assert "・預設 …OLD1" in text and "現在用的 …wxyz 不在清單裡。" in text and "使用中" not in text
    assert len(calls_named(bot, "send")[0]["reply_markup"].inline_keyboard) == 2
    # 清單的 active 雖然是「預設」，但實際沒在用，可以刪。
    _receive(bot_core, bot, _message("/delkey 備用"))
    assert bot.texts()[-1] == "已刪掉 備用 …abcd。"
    assert_no_keys(bot, caplog)


# ---------- /usekey 與按鈕 ----------

def test_usekey_command_switches_and_rebuilds_only_claude(make_core, key_env, validation, caplog):
    write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY})
    bot_core, pool = make_core()
    pool.get(OPUS), pool.get(SONNET)
    gemini = pool.get(FLASH)
    bot = FakeBot()
    _receive(bot_core, bot, _message("/usekey 備用"))
    assert bot.texts() == ["已改用 備用 …abcd，下一題開始生效。"]
    assert saved(key_env) == {"active": "備用", "keys": {"預設": OLD_KEY, "備用": NEW_KEY}}
    assert key_env.env.read_bytes() == env_with(NEW_KEY)
    assert mode(key_env.keys) == mode(key_env.env) == 0o600
    assert validation.verified() == [NEW_KEY]
    assert set(pool.answerers) == {FLASH} and pool.get(FLASH) is gemini
    assert not calls_named(bot, "delete")
    assert_no_keys(bot, caplog)


def test_switch_button_reverifies_and_switches(make_core, key_env, validation, caplog):
    write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY}, mode=0o644)
    bot_core, pool = make_core()
    pool.get(OPUS)
    bot = FakeBot()
    callback = press(bot_core, bot, "k:" + tg.key_token("備用"))
    assert callback.answers == [{}]
    edits = calls_named(bot, "edit")
    assert [edit["text"] for edit in edits] == ["驗證中…", "已改用 備用 …abcd，下一題開始生效。"]
    assert all(edit["message_id"] == 101 and "reply_markup" not in edit for edit in edits)
    assert saved(key_env)["active"] == "備用" and key_env.env.read_bytes() == env_with(NEW_KEY)
    assert mode(key_env.keys) == 0o600                      # 原本 644，寫回後一律 600
    assert validation.verified() == [NEW_KEY] and OPUS not in pool.answerers
    assert_no_keys(bot, caplog, callback)


@pytest.mark.parametrize("via", ["command", "button"])
@pytest.mark.parametrize("outcome", ["401", "403", "network", "timeout", "500"])
def test_switch_keeps_current_key_when_verification_fails(make_core, key_env, validation, caplog, via, outcome):
    before_store, before_env = write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY})
    validation.problems[NEW_KEY] = (status_problem(int(outcome)) if outcome.isdigit()
                                    else connection_problem(outcome))
    bot_core, pool = make_core()
    existing = pool.get(OPUS)
    bot = FakeBot()
    callbacks = []
    if via == "command":
        _receive(bot_core, bot, _message("/usekey 備用"))
    else:
        callbacks.append(press(bot_core, bot, "k:" + tg.key_token("備用")))
    expected = ("備用 …abcd 無效或沒有權限，沒有切換。" if outcome in ("401", "403")
                else "暫時無法驗證，沒有切換，稍後再試。")
    assert bot.texts()[-1] == expected
    assert key_env.keys.read_bytes() == before_store and key_env.env.read_bytes() == before_env
    assert pool.get(OPUS) is existing
    assert_no_keys(bot, caplog, *callbacks)


def test_usekey_unknown_current_and_stale_button(make_core, key_env, validation, caplog):
    before_store, before_env = write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY})
    bot_core, pool = make_core()
    existing = pool.get(OPUS)
    bot = FakeBot()
    _receive(bot_core, bot, _message("/usekey 不存在"))
    _receive(bot_core, bot, _message("/usekey 預設"))
    stale = press(bot_core, bot, "k:" + tg.key_token("已刪掉的"))
    broken = press(bot_core, bot, "k:不是雜湊")
    assert bot.texts() == ["沒有「不存在」。/key 看清單。", "已經在用 預設。",
                           "驗證中…", tg.STALE_KEYS, tg.STALE_KEYS]
    assert stale.answers == broken.answers == [{}]
    assert not validation.events and pool.get(OPUS) is existing
    assert key_env.keys.read_bytes() == before_store and key_env.env.read_bytes() == before_env
    assert_no_keys(bot, caplog, stale, broken)


def test_model_buttons_still_route_to_model_menu(make_core, key_env):
    bot_core, _ = make_core()
    bot = FakeBot()
    callback = press(bot_core, bot, "m:s")
    assert callback.answers == [{}]
    assert bot.texts() == ["目前模型：Opus 5.5｜medium（預設）\n選 Sonnet 5.5 的思考程度："]
    callback = press(bot_core, bot, "e:s:medium")
    assert callback.answers == [{}]
    assert bot.texts()[-1] == "已切換到 Sonnet 5.5｜medium，開新對話。"


# ---------- /delkey ----------

def test_delkey_removes_saved_key_without_touching_env_or_claude(make_core, key_env, validation, caplog):
    _, before_env = write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY, "第三": SPARE_KEY})
    bot_core, pool = make_core()
    existing = pool.get(OPUS)
    bot = FakeBot()
    _receive(bot_core, bot, _message("/delkey 備用"))
    assert bot.texts() == ["已刪掉 備用 …abcd。"]
    assert saved(key_env) == {"active": "預設", "keys": {"預設": OLD_KEY, "第三": SPARE_KEY}}
    assert key_env.env.read_bytes() == before_env and mode(key_env.keys) == 0o600
    assert pool.get(OPUS) is existing and not validation.events
    assert_no_keys(bot, caplog)


def test_delkey_refuses_active_and_unknown(make_core, key_env, validation, caplog):
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message("/delkey 預設"))         # 第一次：清單檔還沒建立
    assert folder_names(key_env) == [".env"]
    before_store, before_env = write_store(key_env, "備用", {"預設": OLD_KEY, "備用": NEW_KEY})
    _receive(bot_core, bot, _message("/delkey 備用"))
    _receive(bot_core, bot, _message("/delkey 不存在"))
    assert bot.texts() == ["預設 正在使用，不能刪。先 /usekey 換別把。",
                           "備用 正在使用，不能刪。先 /usekey 換別把。", "沒有「不存在」。/key 看清單。"]
    assert key_env.keys.read_bytes() == before_store and key_env.env.read_bytes() == before_env
    assert_no_keys(bot, caplog)


@pytest.mark.parametrize("command", ["usekey", "delkey"])
def test_name_commands_delete_pasted_keys(make_core, key_env, validation, caplog, command):
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/{command} {NEW_KEY}"))
    _receive(bot_core, bot, _message(f"/{command} 壞/名"))
    assert bot.calls[0] == ("delete", {"chat_id": CHAT, "message_id": 1})
    assert bot.texts() == ["這裡要打名稱，不是金鑰。已刪掉那則訊息。", tg.NAME_RULE]
    assert len(calls_named(bot, "delete")) == 1 and not validation.events
    assert_no_keys(bot, caplog)


# ---------- 環境變數、清單檔、白名單 ----------

def test_environment_override_refuses_changes(make_core, key_env, validation, monkeypatch, caplog):
    """規則：環境變數 ANTHROPIC_API_KEY 蓋過 .env；設了就不從 Telegram 換（否則重啟後又被環境變數蓋回來）。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", SPARE_KEY)
    before_store, before_env = write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY})
    bot_core, pool = make_core()
    existing = pool.get(OPUS)
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey 新的 {NEW_KEY}"))
    _receive(bot_core, bot, _message("/usekey 備用"))
    callback = press(bot_core, bot, "k:" + tg.key_token("備用"))
    _receive(bot_core, bot, _message("/key"))
    texts = bot.texts()
    assert texts[:2] == [tg.ENV_OVERRIDE, tg.ENV_OVERRIDE] and texts[3] == tg.ENV_OVERRIDE
    assert texts[4] == ("Claude 金鑰：\n・預設 …OLD1 wrkspc_…test\n・備用 …abcd wrkspc_…test\n"
                        "現在用環境變數的 …wxyz，不能從這裡換。\n新增：/setkey 名稱 金鑰")
    assert "reply_markup" not in calls_named(bot, "send")[-1]
    assert bot.calls[0][0] == "delete"                     # 含金鑰的訊息照樣刪
    assert key_env.keys.read_bytes() == before_store and key_env.env.read_bytes() == before_env
    assert os.environ["ANTHROPIC_API_KEY"] == SPARE_KEY == core.resolve_api_key()
    assert not validation.events and pool.get(OPUS) is existing
    assert_no_keys(bot, caplog, callback)


def test_first_run_store_comes_from_env_without_writing(key_env, tmp_path):
    store = core.load_key_store()
    assert store == core.KeyStore("預設", {"預設": OLD_KEY}, {"預設": "wrkspc_test"})
    assert folder_names(key_env) == [".env"]
    assert core.keys_path() == key_env.keys
    empty = tmp_path / "empty" / ".env"
    empty.parent.mkdir()
    empty.write_text("OTHER=1\n")
    assert core.load_key_store(empty) == core.KeyStore(None, {})


@pytest.mark.parametrize("content", [b"{", b"\xff", b'{"keys": []}', b'{"keys": {"a": 1}}', b"[]",
                                     b'{"keys": {"a": "  "}}'])
def test_broken_store_is_never_overwritten(make_core, key_env, validation, caplog, content):
    key_env.keys.write_bytes(content)
    bot_core, _ = make_core()
    bot = FakeBot()
    for command in (f"/setkey 備用 {NEW_KEY}", "/usekey 備用", "/delkey 備用", "/key"):
        _receive(bot_core, bot, _message(command))
    assert bot.texts() == ["金鑰清單讀不到，沒有更換。", "金鑰清單讀不到，沒有更換。",
                           "金鑰清單讀不到，沒有刪除。", "金鑰清單讀不到，看 bot 的 log。"]
    assert key_env.keys.read_bytes() == content and key_env.env.read_bytes() == key_env.before
    assert not validation.events and "KeyStoreError" in caplog.text
    assert_no_keys(bot, caplog)


def test_store_with_missing_active_falls_back_to_none(key_env):
    key_env.keys.write_text(json.dumps({"active": "不見了", "keys": {"備用": NEW_KEY}}))
    assert core.load_key_store() == core.KeyStore(None, {"備用": NEW_KEY}, {"備用": "wrkspc_test"})


@pytest.mark.parametrize("name, ok", [("預設", True), ("backup_2", True), ("備用-A", True), ("一" * 20, True),
                                      ("", False), ("一" * 21, False), ("有 空格", False), ("a/b", False),
                                      ("sk-ant-x", False), ("名\n稱", False)])
def test_key_name_rule(name, ok):
    assert core.is_key_name(name) is ok


@pytest.mark.parametrize("command", [f"/setkey {NEW_KEY}", f"/setkey 備用 {NEW_KEY}", "/key", "/usekey 備用",
                                     "/delkey 備用"], ids=["setkey", "setkey-named", "key", "usekey", "delkey"])
def test_key_commands_reject_stranger(make_core, key_env, validation, command):
    bot_core, pool = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message(command, user_id=STRANGER))
    assert bot.calls == [] and not pool.answerers and not validation.events
    assert key_env.env.read_bytes() == key_env.before and not key_env.keys.exists()


def test_key_button_rejects_stranger_but_answers(make_core, key_env, validation):
    before_store, before_env = write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY})
    bot_core, _ = make_core()
    bot = FakeBot()
    callback = press(bot_core, bot, "k:" + tg.key_token("備用"), user_id=STRANGER)
    assert callback.answers == [{}] and bot.calls == [] and not validation.events
    assert key_env.keys.read_bytes() == before_store and key_env.env.read_bytes() == before_env


def test_gitignore_covers_key_files():
    lines = set((core.REPO / ".gitignore").read_text(encoding="utf-8").splitlines())
    assert {".env", ".anthropic_keys.json", ".env.tmp-*", ".anthropic_keys.json.tmp-*"} <= lines
    if shutil.which("git") is None or not (core.REPO / ".git").exists():
        pytest.skip("沒有 git，只檢查 .gitignore 的內容")
    for name in (".env", core.KEYS_NAME, ".env.tmp-abc123", core.KEYS_NAME + ".tmp-abc123"):
        result = subprocess.run(["git", "check-ignore", "-q", "--no-index", name], cwd=core.REPO)
        assert result.returncode == 0, f"{name} 沒有被 .gitignore 擋住"


# ---------- 文案、選單、回覆遮蔽 ----------

def test_key_help_menu_and_errors():
    assert tg.parse_command("/SETKEY@Bot " + NEW_KEY).name == "setkey"
    assert [tg.parse_command(f"/{name} 備用").name for name in ("key", "usekey", "delkey")] == [
        "key", "usekey", "delkey"]
    menu = dict(tg.BOT_COMMANDS)
    assert "key" in menu and not {"setkey", "delkey", "usekey"} & set(menu)
    help_text = tg.help_text(core.DEFAULT_MODEL, 3)
    for line in ("/key 看、換 Claude 金鑰", "/setkey 名稱 金鑰 存金鑰", "/usekey 名稱 改用金鑰", "/delkey 名稱 刪金鑰"):
        assert line in help_text.splitlines()
    assert tg.error_message(core.MissingApiKey("x")) == "沒有 Claude 金鑰，用 /setkey 金鑰 設定。"
    assert tg.error_message(SimpleNamespace(status_code=401)) == "Claude 金鑰被拒（HTTP 401），/key 看金鑰，/setkey 換。"


@pytest.mark.parametrize("source", ["reply", "quote", "external", "caption", "question"])
def test_reply_question_redacts_key_before_truncating(source):
    content = "/setkey " + NEW_KEY
    fields = {}
    question = "請說明"
    if source == "question":
        question += content
    elif source == "quote":
        fields = {"reply_to_message": _message(content), "quote": SimpleNamespace(text=content)}
    elif source == "external":
        fields = {"external_reply": object(), "quote": SimpleNamespace(text=content)}
    else:
        replied = (_message(caption=content) if source == "caption" else _message(content))
        fields = {"reply_to_message": replied}
    result = tg.reply_question(question, SimpleNamespace(**fields), USER)
    assert NEW_KEY not in result and "***" in result
    # 遮蔽在截斷前做，不能留下被截斷的金鑰片段。
    long_content = "前" * (tg.REPLY_LIMIT - 8) + content
    result = tg.reply_question("問", SimpleNamespace(reply_to_message=_message(long_content)), USER)
    assert "sk-ant-" not in result


def test_question_with_key_is_redacted_before_model_and_display(make_core, key_env, validation, caplog):
    bot_core, pool = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"這把 {NEW_KEY} 能用嗎"))
    [call] = pool.all_calls()
    assert NEW_KEY not in call["question"] and "***" in call["question"]
    assert_no_keys(bot, caplog)


# ---------- 程式進入點與 log ----------

def load_entry():
    spec = importlib.util.spec_from_file_location("telegram_entry_keys", core.REPO / "scripts/telegram_bot.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return entry


def test_entry_factory_reloads_key_after_switch(key_env, validation, monkeypatch):
    entry = load_entry()
    monkeypatch.setattr(entry.bot, "load_config", lambda: tg.Config("假 token", frozenset({USER}), 3, OLD_KEY, None))
    monkeypatch.setattr(entry, "setup_logging", lambda *args: None)
    monkeypatch.setattr(entry, "Searcher", lambda *args: SimpleNamespace(store=None, close=lambda: None))

    def make_answerer(*args, api_key, **kwargs):
        return SimpleNamespace(key=api_key, close=lambda: None)

    monkeypatch.setattr(core, "Answerer", make_answerer)

    def build_application(bot_core, token):
        def polling(**kwargs):
            try:
                get = lambda: bot_core.executor.submit(bot_core.pool.get, OPUS).result()  # noqa: E731
                previous = get()
                assert previous.key == OLD_KEY
                bot = FakeBot()
                run(bot_core.handle_message(bot, CHAT, USER, f"/setkey 備用 {NEW_KEY}",
                                            message=SimpleNamespace(message_id=1)))
                current = get()
                assert current is not previous and current.key == NEW_KEY
                run(bot_core.handle_message(bot, CHAT, USER, "/usekey 預設"))
                assert get().key == OLD_KEY
            finally:
                bot_core.close()
        return SimpleNamespace(run_polling=polling)

    monkeypatch.setattr(entry.bot, "build_application", build_application)
    assert entry.main(["--bm25-only"]) == 0


def test_logging_blocks_ptb_and_sdk_debug_after_rotation(caplog):
    entry = load_entry()
    roots = ("telegram", "httpx", "httpcore", "httpx2", "httpcore2", "anthropic", "apscheduler")
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    names = roots + ("telegram.ext.Application", "telegram._bot")
    saved_levels = {name: logger.level for name, logger in logging.root.manager.loggerDict.items()
                    if isinstance(logger, logging.Logger)}
    try:
        logging.getLogger("telegram.ext.Application").setLevel(logging.DEBUG)
        entry.setup_logging(OLD_KEY)
        assert all(logging.getLogger(name).getEffectiveLevel() >= logging.WARNING for name in names)
        assert root.handlers[0].level == logging.INFO
        caplog.clear()
        root.addHandler(caplog.handler)
        for name in names:
            logging.getLogger(name).debug("測試更新 /setkey %s", NEW_KEY)
            logging.getLogger(name).info("測試更新 /setkey %s", NEW_KEY)
        assert not caplog.records
        # 新金鑰不在啟動時的機密清單裡，訊息與例外仍會遮掉。
        record = logging.LogRecord("測試", logging.ERROR, __file__, 1, "測試 %s", (NEW_KEY,), None)
        try:
            raise RuntimeError(NEW_KEY)
        except RuntimeError:
            import sys
            record.exc_info = sys.exc_info()
        tg.RedactFilter(OLD_KEY).filter(record)
        assert NEW_KEY not in logging.Formatter().format(record)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for name, logger in logging.root.manager.loggerDict.items():
            if isinstance(logger, logging.Logger):
                logger.setLevel(saved_levels.get(name, logging.NOTSET))


# ---------- 組織層級金鑰：workspace id ----------

WORKSPACE = "wrkspc_01TestWorkspaceZw9V"
OTHER_WORKSPACE = "wrkspc_02OtherWorkspaceQq7X"


def message_problem(status, message):
    """帶 API 錯誤訊息的假例外（body 跟 Anthropic API 一樣是 {"type": "error", "error": {...}}）。"""
    import anthropic
    import httpx2

    request = httpx2.Request("GET", "https://example.invalid/v1/models")
    response = httpx2.Response(status, request=request)
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return anthropic.APIStatusError(message, response=response, body=body)


NOT_SCOPED = ("This API key is not scoped to a workspace, so this request must include the "
              "anthropic-workspace-id header with the ID of the workspace to use.")


@pytest.mark.parametrize("arg, name", [(f"備用 {NEW_KEY} {WORKSPACE}", "備用"), (f"{NEW_KEY} {WORKSPACE}", "預設")],
                         ids=["named", "default-name"])
def test_setkey_with_workspace_verifies_and_writes_both(make_core, key_env, validation, caplog, arg, name):
    bot_core, pool = make_core()
    pool.get(OPUS)
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey {arg}"))
    assert bot.calls[0][0] == "delete"
    assert [event[2] for event in validation.events if event[0] == "create"] == [WORKSPACE]
    assert key_env.env.read_bytes() == env_with(NEW_KEY, WORKSPACE)
    assert core.resolve_workspace_id() == WORKSPACE and core.resolve_api_key() == NEW_KEY
    assert saved(key_env)["active"] == name and saved(key_env)["keys"][name] == NEW_KEY
    assert saved_workspaces(key_env)[name] == WORKSPACE
    assert saved_raw(key_env)["keys"][name] == {"key": NEW_KEY, "workspace": WORKSPACE}
    assert OPUS not in pool.answerers
    _receive(bot_core, bot, _message("/key"))
    assert f"✓ {name} …abcd wrkspc_…Zw9V（使用中）" in bot.texts()[-1]
    assert WORKSPACE not in caplog.text and WORKSPACE not in repr(bot.calls)
    assert_no_keys(bot, caplog)


def test_setkey_without_workspace_on_org_key_asks_for_it(make_core, key_env, validation, caplog):
    validation.problems[NEW_KEY] = message_problem(400, NOT_SCOPED)
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey 備用 {NEW_KEY}"))
    assert bot.texts() == ["這是組織層級金鑰，要加 workspace ID：/setkey 名稱 金鑰 wrkspc_…"]
    assert key_env.env.read_bytes() == key_env.before and not key_env.keys.exists()
    assert "HTTP 400" in caplog.text and "not scoped" not in caplog.text
    assert_no_keys(bot, caplog)


@pytest.mark.parametrize("status", [400, 403, 404])
def test_setkey_with_rejected_workspace(make_core, key_env, validation, caplog, status):
    validation.problems[NEW_KEY] = message_problem(status, f"Workspace {WORKSPACE} not found or not accessible.")
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey 備用 {NEW_KEY} {WORKSPACE}"))
    assert bot.texts() == ["workspace ID 不對（要 wrkspc_ 開頭，在 Console → Settings → Workspaces）"]
    assert key_env.env.read_bytes() == key_env.before and not key_env.keys.exists()
    assert WORKSPACE not in caplog.text
    assert_no_keys(bot, caplog)


def test_setkey_other_failures_with_workspace_keep_old_replies(make_core, key_env, validation, caplog):
    bot_core, _ = make_core()
    bot = FakeBot()
    validation.problems[NEW_KEY] = message_problem(401, "invalid x-api-key")
    _receive(bot_core, bot, _message(f"/setkey 備用 {NEW_KEY} {WORKSPACE}"))
    validation.problems[NEW_KEY] = message_problem(500, "Internal server error")
    _receive(bot_core, bot, _message(f"/setkey 備用 {NEW_KEY} {WORKSPACE}"))
    assert bot.texts() == ["金鑰無效或沒有權限，沒有更換。", "暫時無法驗證，沒有更換，稍後再試。"]
    assert key_env.env.read_bytes() == key_env.before and not key_env.keys.exists()


@pytest.mark.parametrize("workspace", ["wrkspc_", "wrkspc_ab", "wrkspc_有中文", "wrkspc_a-b-c-d", "WRKSPC_abcdef",
                                       "wrkspc_" + "a" * 81], ids=["empty", "short", "cjk", "dash", "upper", "long"])
def test_setkey_bad_workspace_format_skips_validation(make_core, key_env, validation, caplog, workspace):
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey 備用 {NEW_KEY} {workspace}"))
    assert bot.texts() == [tg.BAD_WORKSPACE]
    assert not validation.events and bot.calls[0][0] == "delete"
    assert key_env.env.read_bytes() == key_env.before and not key_env.keys.exists()


def test_workspace_format_rule():
    assert core.is_workspace_format(WORKSPACE) and core.is_workspace_format("wrkspc_abcd")
    for bad in ("wrkspc_abc", "wrkspc_", "workspace_abcd", "wrkspc_ab cd", None, 3):
        assert not core.is_workspace_format(bad)


def test_usekey_switches_key_and_workspace_together(make_core, key_env, validation, caplog):
    key_env.keys.write_text(json.dumps({"active": "組織", "keys": {
        "組織": {"key": OLD_KEY, "workspace": WORKSPACE}, "綁定": {"key": NEW_KEY, "workspace": None},
        "另一個": {"key": SPARE_KEY, "workspace": OTHER_WORKSPACE}}}, ensure_ascii=False))
    key_env.env.write_bytes(env_with(OLD_KEY, WORKSPACE))
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message("/usekey 組織"))
    assert bot.texts()[-1] == "已經在用 組織。" and not validation.events
    _receive(bot_core, bot, _message("/usekey 綁定"))
    assert bot.texts()[-1] == "已改用 綁定 …abcd，下一題開始生效。"
    assert key_env.env.read_bytes() == env_with(NEW_KEY, None) and core.resolve_workspace_id() is None
    _receive(bot_core, bot, _message("/usekey 另一個"))
    # workspace 那一行先前被刪掉，這次加在檔尾；其他行原樣保留
    env = core.read_env_file(key_env.env)
    assert (env["ANTHROPIC_API_KEY"], env["ANTHROPIC_WORKSPACE_ID"]) == (SPARE_KEY, OTHER_WORKSPACE)
    assert key_env.env.read_bytes() == env_with(SPARE_KEY, None) + f"\r\nANTHROPIC_WORKSPACE_ID={OTHER_WORKSPACE}\r\n".encode()
    _receive(bot_core, bot, _message("/usekey 組織"))
    assert key_env.env.read_bytes() == env_with(OLD_KEY, None) + f"\r\nANTHROPIC_WORKSPACE_ID={WORKSPACE}\r\n".encode()
    assert [event[1:3] for event in validation.events if event[0] == "create"] == [
        (NEW_KEY, None), (SPARE_KEY, OTHER_WORKSPACE), (OLD_KEY, WORKSPACE)]
    # 同一把金鑰但 .env 的 workspace 被改掉：不算「已經在用」，重新寫入
    key_env.env.write_bytes(env_with(OLD_KEY, OTHER_WORKSPACE))
    _receive(bot_core, bot, _message("/usekey 組織"))
    assert bot.texts()[-1] == "已改用 組織 …OLD1，下一題開始生效。"
    assert key_env.env.read_bytes() == env_with(OLD_KEY, WORKSPACE)
    assert WORKSPACE not in caplog.text and OTHER_WORKSPACE not in caplog.text
    assert_no_keys(bot, caplog)


def test_usekey_reports_workspace_problems(make_core, key_env, validation, caplog):
    write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY})
    key_env.env.write_bytes(env_with(OLD_KEY, None))      # 舊清單＋.env 沒有 workspace → 備用也沒有
    bot_core, _ = make_core()
    bot = FakeBot()
    validation.problems[NEW_KEY] = message_problem(400, NOT_SCOPED)
    _receive(bot_core, bot, _message("/usekey 備用"))
    assert bot.texts()[-1] == ("備用 …abcd 是組織層級金鑰，要加 workspace ID：/setkey 備用 金鑰 wrkspc_…，沒有切換。")
    assert key_env.env.read_bytes() == env_with(OLD_KEY, None)


def test_legacy_store_migrates_to_new_format_on_write(make_core, key_env, validation, caplog):
    write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY})
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message(f"/setkey 第三 {SPARE_KEY} {WORKSPACE}"))
    assert saved_raw(key_env) == {"active": "第三", "keys": {
        "預設": {"key": OLD_KEY, "workspace": "wrkspc_test"},
        "備用": {"key": NEW_KEY, "workspace": "wrkspc_test"},
        "第三": {"key": SPARE_KEY, "workspace": WORKSPACE}}}
    assert core.load_key_store().workspaces == {"預設": "wrkspc_test", "備用": "wrkspc_test", "第三": WORKSPACE}


@pytest.mark.parametrize("entry", [{"key": NEW_KEY, "workspace": 3}, {"key": NEW_KEY, "workspace": " "},
                                   {"workspace": WORKSPACE}, {"key": "  "}, [NEW_KEY]])
def test_bad_new_format_entries_are_rejected(key_env, entry):
    key_env.keys.write_text(json.dumps({"active": "a", "keys": {"a": entry}}))
    with pytest.raises(core.KeyStoreError):
        core.load_key_store()


def test_workspace_environment_variable_blocks_switching(make_core, key_env, validation, monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", WORKSPACE)
    write_store(key_env, "預設", {"預設": OLD_KEY, "備用": NEW_KEY})
    bot_core, _ = make_core()
    bot = FakeBot()
    _receive(bot_core, bot, _message("/usekey 備用"))
    _receive(bot_core, bot, _message(f"/setkey 新的 {NEW_KEY} {WORKSPACE}"))
    assert bot.texts() == [tg.ENV_OVERRIDE, tg.ENV_OVERRIDE] and not validation.events
