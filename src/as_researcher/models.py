"""Live OpenAI-compatible model construction and an explicit offline test double."""

import asyncio
import json
import os
import time
from urllib.parse import urlsplit

import httpx
from agentscope.credential import OpenAICredential
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatModelBase, ChatResponse, OpenAIChatModel

from .model_diagnostics import model_error_details, protect_retry_logging
from .source_inputs import SOURCE_CONTEXT_MARKER

SCENARIOS = (
    "normal",
    "duplicate",
    "empty",
    "search_error",
    "invalid_citation",
    "injection",
    "no_tool",
    "tool_chain",
)


class ObservedOpenAIChatModel(OpenAIChatModel):
    """Retain bounded provider metadata lost by AgentScope 2.0.8's parser.

    This override is deliberately tied to the pinned non-streaming adapter.
    Never persist raw responses, headers, tool arguments or reasoning content.
    """

    def __init__(self, **kwargs):
        if kwargs.get("stream", True):
            raise ValueError("ObservedOpenAIChatModel requires stream=False")
        protect_retry_logging()
        super().__init__(**kwargs)
        self.provider_responses: list[dict] = []
        self.request_events: list[dict] = []

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        event = {
            "request_index": len(self.request_events) + 1,
            "status": "running",
            "message_count": len(messages),
            "tool_schema_count": len(tools or []),
            "tool_choice": getattr(tool_choice, "mode", None),
        }
        self.request_events.append(event)
        started = time.perf_counter()
        try:
            response = await super()._call_api(model_name, messages, tools, tool_choice, **kwargs)
            event["status"] = "responded"
            return response
        except asyncio.CancelledError as exc:
            event.update(status="cancelled", **model_error_details(exc))
            raise
        except Exception as exc:
            event.update(status="failed", **model_error_details(exc))
            raise
        finally:
            event["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)

    def _parse_completion_response(self, start_datetime, response, audio_format="wav"):
        parsed = super()._parse_completion_response(start_datetime, response, audio_format)
        choice = response.choices[0] if response.choices else None
        raw_reason = choice.finish_reason if choice else None
        reason = (
            raw_reason
            if raw_reason in {"stop", "tool_calls", "length", "content_filter", "function_call"}
            else "unknown"
        )
        has_tools = bool(choice and choice.message.tool_calls)
        complete = (reason == "stop" and not has_tools) or (reason == "tool_calls" and has_tools)
        usage = response.usage

        def count(obj, key):
            value = getattr(obj, key, None)
            return value if type(value) is int and value >= 0 else None

        details = getattr(usage, "completion_tokens_details", None)
        event = {
            "response_index": len(self.provider_responses) + 1,
            "finish_reason": reason,
            "input_tokens": count(usage, "prompt_tokens"),
            "output_tokens": count(usage, "completion_tokens"),
            "reasoning_tokens": count(details, "reasoning_tokens"),
            "has_tool_calls": has_tools,
            "tool_calls_suppressed": has_tools and not complete,
            "incomplete": not complete,
        }
        self.provider_responses.append(event)
        parsed.metadata["provider_response"] = dict(event)
        if not complete:
            # An incomplete tool argument must never reach Toolkit. End this
            # iteration with visible text only; run_research marks it incomplete.
            parsed.content = [block for block in parsed.content if isinstance(block, TextBlock)]
            if not parsed.content:
                parsed.content = [TextBlock(text="模型响应未完整结束，未取得可保存的回答正文。")]
        return parsed


def thinking_parameters(mode: str, base_url: str, model: str) -> dict:
    """Only opt in to the verified DeepSeek switch; never guess for a proxy.

    Verified 2026-09-28: https://api-docs.deepseek.com/guides/thinking_mode/
    provider-default omits the parameter; it does not mean thinking is disabled.
    """
    if mode == "provider-default":
        return {}
    if mode != "disabled":
        raise ValueError("Unsupported thinking mode")
    try:
        url = urlsplit(base_url)
        supported = (
            url.scheme == "https"
            and url.hostname == "api.deepseek.com"
            and url.port in (None, 443)
            and not (url.username or url.password or url.query or url.fragment)
            and url.path.rstrip("/") in ("", "/v1")
            and model in {"deepseek-flash", "deepseek-v4-pro"}
        )
    except ValueError:
        supported = False
    if not supported:
        raise ValueError(
            "Explicit thinking control requires the verified DeepSeek official endpoint/model; "
            "use provider-default for other providers or proxies"
        )
    return {"thinking": {"type": "disabled"}}


def live_model(
    max_retries: int = 0, max_tokens: int = 1800, thinking_mode: str = "provider-default"
) -> ObservedOpenAIChatModel:
    if type(max_tokens) is not int or not 64 <= max_tokens <= 4096:
        raise ValueError("Model max_tokens must be between 64 and 4096")
    # Accept the user's existing LLM_* convention without rewriting .env.
    aliases = {
        "MODEL_API_KEY": "LLM_API_KEY",
        "MODEL_BASE_URL": "LLM_BASE_URL",
        "MODEL_NAME": "LLM_MODEL",
    }
    settings = {key: os.environ.get(key) or os.environ.get(alias) for key, alias in aliases.items()}
    required = ("MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME")
    missing = [key for key in required if not settings[key]]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))
    options = thinking_parameters(thinking_mode, settings["MODEL_BASE_URL"], settings["MODEL_NAME"])
    model = ObservedOpenAIChatModel(
        credential=OpenAICredential(
            api_key=settings["MODEL_API_KEY"],
            base_url=settings["MODEL_BASE_URL"],
        ),
        model=settings["MODEL_NAME"],
        stream=False,
        max_retries=max_retries,
        parameters=OpenAIChatModel.Parameters(parallel_tool_calls=False),
        extra_body={"max_tokens": max_tokens, **options},
        client_kwargs={
            "timeout": 45.0,
            "max_retries": 0,
            "http_client": httpx.AsyncClient(timeout=45.0),
        },
    )
    model.thinking_mode = thinking_mode
    return model


class ScriptedModel(ChatModelBase):
    """Drive the real Agent/Toolkit loop without a network or an LLM.

    This verifies integration and policies, not intelligence, prompt-injection
    resistance, real retrieval quality, or real token/cost savings.
    """

    def __init__(self, scenario: str = "normal"):
        super().__init__(
            credential=OpenAICredential(api_key="offline-placeholder"),
            model="offline-scripted-fixture",
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
        )
        self.scenario = scenario
        self.calls = 0
        self.formatter = OpenAIChatFormatter()

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        supplied = []
        page_results = []
        for message in messages:
            for block in message.content:
                if isinstance(block, TextBlock) and SOURCE_CONTEXT_MARKER in block.text:
                    supplied = json.loads(block.text.rsplit(SOURCE_CONTEXT_MARKER, 1)[1])[
                        "provided_sources"
                    ]
                if isinstance(block, ToolResultBlock) and block.name == "read_page":
                    output = block.output
                    text = (
                        output
                        if isinstance(output, str)
                        else "".join(item.text for item in output if isinstance(item, TextBlock))
                    )
                    try:
                        page_results.append(json.loads(text))
                    except ValueError:
                        page_results.append({"error": "fixture_tool_error"})
        if supplied:
            if not page_results:
                return ChatResponse(
                    content=[
                        ToolCallBlock(
                            id="fixture-supplied-page",
                            name="read_page",
                            input=json.dumps({"source_id": supplied[0]["id"]}),
                        )
                    ],
                    is_last=True,
                )
            page = page_results[-1]
            answer = (
                f"## 结论\n指定来源的固定正文读取成功。[{page['id']}@page]\n"
                "## 局限\n这是预置模型和正文的流程演示，不代表真实研究质量。"
                if isinstance(page, dict)
                and page.get("evidence_kind") == "page_excerpt"
                and "error" not in page
                else "## 结论\n证据不足：指定来源未取得可用正文。"
            )
            return ChatResponse(content=[TextBlock(text=answer)], is_last=True)
        observations = []
        sources = {}
        for message in messages:
            for block in message.content:
                if isinstance(block, ToolResultBlock) and block.name == "search_web":
                    output = block.output
                    text = (
                        output
                        if isinstance(output, str)
                        else "".join(item.text for item in output if isinstance(item, TextBlock))
                    )
                    payload = json.loads(text)
                    observations.append(payload)
                    sources.update({s["id"]: s for s in payload.get("sources", [])})
        queries = ["agent memory design", "agent memory evaluation"]
        if self.scenario == "duplicate":
            queries[1] = "  Agent   Memory DESIGN  "
        if (
            self.scenario == "tool_chain"
            and sources
            and not any(
                isinstance(b, ToolResultBlock) and b.name == "read_page"
                for m in messages
                for b in m.content
            )
        ):
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id=f"fixture-read-{self.calls}",
                        name="read_page",
                        input=json.dumps({"source_id": next(iter(sources))}),
                    )
                ],
                is_last=True,
            )
        should_finish = (
            self.scenario == "no_tool"
            or len(observations) >= len(queries)
            or (observations and observations[-1].get("stop"))
            or (tool_choice is not None and tool_choice.mode == "none")
        )
        if should_finish:
            if sources:
                statements = []
                for sid, source in sources.items():
                    claim = (
                        "评测需分别关注检索相关性与证据支持。"
                        if source["url"].endswith("/evaluation")
                        else "Agent 可以从记忆中检索相关记录。"
                    )
                    statements.append(f"- {claim}[{sid}@snippet]")
                answer = (
                    "## 结论\n这是使用预置证据的离线流程演示。\n\n## 证据与比较\n"
                    + "\n".join(statements)
                    + "\n\n## 局限与待验证问题\n"
                    "证据来自预置样例，不能用于证明真实模型的回答质量。"
                )
            else:
                answer = "## 结论\n证据不足。本次未获得可使用的外部证据。"
            if self.scenario == "invalid_citation":
                answer += "\n这句话携带一个故意构造的错误引用 [SRC-00000000]。"
            return ChatResponse(content=[TextBlock(text=answer)], is_last=True)
        return ChatResponse(
            content=[
                ToolCallBlock(
                    id=f"fixture-call-{self.calls}",
                    name="search_web",
                    input=json.dumps(
                        {
                            "query": queries[len(observations)],
                            "knowledge_gap": "找到初始证据"
                            if not observations
                            else "补充评测方法的证据",
                        }
                    ),
                )
            ],
            is_last=True,
        )
