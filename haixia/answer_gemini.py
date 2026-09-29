"""Vertex AI Gemini 問答：共用 Claude 版的工具、Answer 與 JSONL 紀錄。"""

import time
from pathlib import Path

from google import genai
from google.genai import types

from haixia import answer as core
from haixia.search import Searcher

PROJECT = "vmdemo1-507014"
LOCATION = "global"
MODELS = ("gemini-3.8-flash", "gemini-3.1-pro-preview")
THINKING_LEVELS = ("default", "low", "medium", "high")
MAX_OUTPUT_TOKENS = 8192  # 思考 token 也佔這個上限
REFUSAL_TEXT = ("抱歉，這一題被 Gemini 的安全機制擋下，沒有產生回答（{reason}）。"
                "這可能是誤判：可以換個說法再問；如果之後的問題也一直被擋，請開新對話。")
BLOCKED_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "RECITATION", "BLOCKLIST", "SPII",
                   "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION"}


def function_declarations():
    """直接由共用工具定義轉換，保留 kind=classic 等參數。"""
    return [types.FunctionDeclaration(name=item["name"], description=item["description"],
                                      parameters_json_schema=item["input_schema"])
            for item in core.TOOLS]


def usage_from_metadata(metadata):
    """prompt 已含快取 token；候選、思考和工具提示依官方欄位分別計。"""
    def count(name):
        return int(getattr(metadata, name, 0) or 0)

    prompt = count("prompt_token_count")
    cached = count("cached_content_token_count")
    candidate = count("candidates_token_count")
    thoughts = count("thoughts_token_count")
    tool_prompt = count("tool_use_prompt_token_count")
    return {"input_tokens": max(0, prompt - cached) + tool_prompt,
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0,
            "output_tokens": candidate + thoughts,
            "candidate_tokens": candidate, "thoughts_tokens": thoughts,
            "tool_use_prompt_tokens": tool_prompt}


def finish_reason(value):
    return getattr(value, "value", value) or "UNKNOWN"


def make_client():
    return genai.Client(
        vertexai=True, project=PROJECT, location=LOCATION,
        http_options=types.HttpOptions(
            api_version="v1", timeout=120_000,
            retry_options=types.HttpRetryOptions(attempts=3, initial_delay=1.0, max_delay=8.0,
                                                 http_status_codes=[408, 429, 500, 502, 503, 504])))


class Answerer:
    def __init__(self, index_dir, model=MODELS[0], *, client=None, embedder="vertex",
                 system_prompt=None, max_tool_rounds=core.MAX_TOOL_ROUNDS,
                 max_output_tokens=MAX_OUTPUT_TOKENS, thinking_level="default",
                 log_dir=core.DEFAULT_LOG_DIR, prices=None, clock=time.monotonic, searcher=None):
        if model not in MODELS:
            raise ValueError(f"不支援的 Gemini 模型：{model}")
        if thinking_level not in THINKING_LEVELS:
            raise ValueError(f"Gemini 思考程度只能是 {'、'.join(THINKING_LEVELS)}")
        self.prices = core.PRICES if prices is None else prices
        if model not in self.prices:
            raise ValueError(f"價格表沒有 {model}，請用 --prices 提供")
        self.model = model
        self.thinking_level = thinking_level
        self.max_tool_rounds = max_tool_rounds
        self.max_output_tokens = max_output_tokens
        self.log_dir = None if log_dir is None else Path(log_dir)
        self.clock = clock
        self.system = (system_prompt if system_prompt is not None
                       else core.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")).strip()
        self.tool = types.Tool(function_declarations=function_declarations())
        self.owns_searcher = searcher is None
        if searcher is None:
            if embedder == "vertex":
                embedder = core.default_embedder()
            searcher = Searcher(index_dir, embedder)
        self.searcher = searcher
        self.client = client if client is not None else make_client()

    def request_config(self, allow_tools=True):
        config = types.GenerateContentConfig(
            system_instruction=self.system, max_output_tokens=self.max_output_tokens,
            tools=[self.tool],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                mode="AUTO" if allow_tools else "NONE")),
        )
        if self.thinking_level != "default":
            config.thinking_config = types.ThinkingConfig(thinking_level=self.thinking_level.upper())
        return config

    def ask(self, conversation, question, on_tool=None):
        started = self.clock()
        answer = core.Answer(text="", stop_reason="", model=self.model)
        messages = conversation.messages
        messages.append(types.Content(role="user", parts=[types.Part.from_text(text=question)]))
        conversation.questions += 1
        error = None
        try:
            self._loop(messages, answer, on_tool, conversation)
        except Exception as problem:
            error = problem
            raise
        finally:
            answer.elapsed_sec = round(self.clock() - started, 3)
            core.log_answer(self.log_dir, self.model, self.thinking_level, False, question, answer, error)
        return answer

    def _loop(self, messages, answer, on_tool, conversation):
        allow_tools = True
        has_tool_results = False
        retried_blank = False
        while True:
            response = self.client.models.generate_content(
                model=self.model, contents=messages, config=self.request_config(allow_tools))
            answer.requests += 1
            usage = usage_from_metadata(getattr(response, "usage_metadata", None))
            answer.context_tokens = (usage["input_tokens"] + usage["cache_read_input_tokens"])
            for name, value in usage.items():
                answer.usage[name] = answer.usage.get(name, 0) + value
            if (self.model == "gemini-3.1-pro-preview"
                    and int(getattr(getattr(response, "usage_metadata", None), "prompt_token_count", 0) or 0) > 200_000
                    and "提示超過 20 萬 token，Pro 價格估算可能偏低" not in answer.notes):
                answer.notes.append("提示超過 20 萬 token，Pro 價格估算可能偏低")
            price = self.prices[self.model]
            answer.cost_usd += core.usage_cost(usage, price)
            candidate = (getattr(response, "candidates", None) or [None])[0]
            reason = finish_reason(getattr(candidate, "finish_reason", None))
            if candidate is None:
                reason = finish_reason(getattr(getattr(response, "prompt_feedback", None), "block_reason", None))
            answer.stop_reason = reason
            if reason in BLOCKED_REASONS:
                answer.refused = True
                answer.text = REFUSAL_TEXT.format(reason=reason)
                return
            content = getattr(candidate, "content", None)
            if content is None:
                if has_tool_results and not retried_blank:
                    retried_blank = True
                    allow_tools = False
                    answer.notes.append("工具結果後沒有文字，已要求 Gemini 補答一次")
                    messages.append(types.Content(role="user", parts=[types.Part.from_text(
                        text="請根據上面已有的工具結果，依系統指示的答案格式直接作答。")]))
                    continue
                answer.text = "（這次沒有產生文字回答，請再問一次或換個說法。）"
                return
            # 完整物件原樣追加；Part 裡的 thought_signature 不拆、不重建。
            messages.append(content)
            parts = content.parts or []
            calls = [part.function_call for part in parts if part.function_call is not None]
            if calls and allow_tools and reason == "STOP":
                answer.rounds += 1
                results = []
                for call in calls:
                    name = call.name or ""
                    message, failed = core.execute_tool(
                        self.searcher, name, call.args or {}, answer.rounds, answer.tool_calls, on_tool,
                        conversation)
                    payload = {"error" if failed else "result": message}
                    results.append(types.Part(function_response=types.FunctionResponse(
                        name=name, id=call.id, response=payload)))
                if answer.rounds >= self.max_tool_rounds:
                    allow_tools = False
                    answer.notes.append(f"工具呼叫達到上限 {self.max_tool_rounds} 輪，要求直接作答")
                messages.append(types.Content(role="tool", parts=results))
                has_tool_results = True
                continue
            if calls:
                messages.append(types.Content(role="tool", parts=[types.Part(
                    function_response=types.FunctionResponse(
                        name=call.name or "", id=call.id,
                        response={"error": "工具呼叫未完成，沒有執行。"})) for call in calls]))
            answer.text = "".join(part.text for part in parts if part.text and not part.thought).strip()
            if reason == "MAX_TOKENS":
                answer.notes.append("回答超過 max_output_tokens 被截斷")
            if not answer.text:
                if has_tool_results and not retried_blank:
                    retried_blank = True
                    allow_tools = False
                    answer.notes.append("工具結果後沒有文字，已要求 Gemini 補答一次")
                    messages.append(types.Content(role="user", parts=[types.Part.from_text(
                        text="請根據上面已有的工具結果，依系統指示的答案格式直接作答。")]))
                    continue
                answer.text = "（這次沒有產生文字回答，請再問一次或換個說法。）"
            return

    def close(self):
        if self.owns_searcher:
            self.searcher.close()
        close = getattr(self.client, "close", None)
        if close:
            close()
