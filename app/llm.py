from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator

import httpx

from .config import Settings


class LLMError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LLMClient:
    """Minimal OpenAI-compatible client with retries, streaming and a mock mode."""

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model
        self.temperature = settings.llm_temperature
        self.max_tokens = settings.llm_max_tokens
        self.proxy = settings.http_proxy or None
        self.mode = settings.mode
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self._http: httpx.AsyncClient | None = None

    # ── public API ────────────────────────────────────────────────
    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "generic",
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        if self.mode == "mock":
            return await self._mock_chat(purpose, messages)
        return await self._chat_live(
            messages,
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=max_tokens or self.max_tokens,
            json_mode=json_mode,
        )

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "writer",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        if self.mode == "mock":
            text = await self._mock_chat(purpose, messages)
            for i in range(0, len(text), 14):
                await asyncio.sleep(0.008)
                yield text[i : i + 14]
            return
        async for chunk in self._stream_live(
            messages,
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=max_tokens or self.max_tokens,
        ):
            yield chunk

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def usage_dict(self) -> dict[str, int]:
        return dict(self.usage)

    # ── live transport ───────────────────────────────────────────
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(180.0, connect=20.0),
                follow_redirects=True,
                trust_env=not bool(self.proxy),
                proxy=self.proxy,
            )
        return self._http

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def _chat_live(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        last_error = "未知错误"
        for attempt in range(3):
            payload = self._payload(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
                stream=False,
            )
            try:
                resp = await self._client().post(self.endpoint, headers=self._headers(), json=payload)
                if resp.status_code == 400 and json_mode:
                    # Not every OpenAI-compatible provider supports response_format.
                    payload.pop("response_format", None)
                    resp = await self._client().post(self.endpoint, headers=self._headers(), json=payload)
                if resp.status_code in {429, 500, 502, 503, 504}:
                    last_error = self._error_detail(resp)
                    await asyncio.sleep(1.2 * (attempt + 1))
                    continue
                if resp.status_code >= 400:
                    raise LLMError(self._friendly_error(resp.status_code, resp), status=resp.status_code)
                body = resp.json()
                self._record_usage(body.get("usage") or {})
                try:
                    content = body["choices"][0]["message"].get("content") or ""
                except (KeyError, IndexError, TypeError) as exc:
                    raise LLMError("模型返回结构异常，请检查接口是否为 OpenAI 兼容格式") from exc
                if not content.strip():
                    raise LLMError("模型返回了空内容")
                return content
            except LLMError:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(1.2 * (attempt + 1))
            except ValueError as exc:
                last_error = f"响应不是合法 JSON: {exc}"
                await asyncio.sleep(0.8)
        raise LLMError(f"模型请求失败（已重试 3 次）：{last_error}")

    async def _stream_live(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        payload = self._payload(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=False, stream=True
        )
        try:
            async with self._client().stream(
                "POST", self.endpoint, headers=self._headers(), json=payload
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    text = body.decode("utf-8", errors="replace")
                    raise LLMError(self._friendly_error_text(resp.status_code, text), status=resp.status_code)
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except ValueError:
                        continue
                    usage = chunk.get("usage")
                    if usage:
                        self._record_usage(usage)
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
        except LLMError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise LLMError(f"流式请求中断：{type(exc).__name__}: {exc}") from exc

    def _record_usage(self, usage: dict[str, Any]) -> None:
        for key in self.usage:
            try:
                self.usage[key] += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass

    @staticmethod
    def _error_detail(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            message = body.get("error", {}).get("message") or body.get("message") or body
        except ValueError:
            message = resp.text[:300]
        return f"HTTP {resp.status_code}: {message}"

    def _friendly_error(self, status: int, resp: httpx.Response) -> str:
        return self._friendly_error_text(status, resp.text)

    @staticmethod
    def _friendly_error_text(status: int, text: str) -> str:
        hints = {
            401: "API Key 无效或未授权（401）",
            403: "API Key 无权访问该模型（403）",
            404: "接口地址或模型名不存在（404），请检查 Base URL 与模型名",
            429: "模型服务限流（429），请稍后重试",
        }
        hint = hints.get(status, f"模型服务返回 HTTP {status}")
        detail = text.strip().replace("\n", " ")[:240]
        return f"{hint}：{detail}" if detail else hint

    # ── mock mode ────────────────────────────────────────────────
    async def _mock_chat(self, purpose: str, messages: list[dict[str, str]]) -> str:
        await asyncio.sleep(0.18)
        prompt = "\n".join(m.get("content", "") for m in messages)
        user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), prompt)
        query = self._extract_field(prompt, ["研究主题", "用户问题", "原始问题"]) or self._first_line(user)
        if purpose == "planner":
            return self._mock_plan(query)
        if purpose == "researcher":
            question = self._extract_field(prompt, ["研究子问题"]) or query
            return self._mock_note(question, prompt)
        if purpose == "critic":
            return self._mock_critique(query)
        if purpose == "writer":
            return self._mock_report(query, prompt)
        return "这是演示模式返回的模拟内容。"

    @staticmethod
    def _first_line(text: str) -> str:
        for line in text.splitlines():
            line = line.strip()
            if len(line) >= 6:
                return line[:160]
        return "AI Agent 的工程化落地"

    @staticmethod
    def _extract_field(prompt: str, names: list[str]) -> str:
        for name in names:
            match = re.search(rf"{name}[：:]\s*(.+)", prompt)
            if match:
                return match.group(1).strip().strip("「」\"'")[:180]
        return ""

    @staticmethod
    def _mock_plan(query: str) -> str:
        plan = {
            "subquestions": [
                {
                    "question": f"「{query}」涉及的核心概念、现状与主要玩家是什么？",
                    "search_query": f"{query} 现状 定义 综述",
                    "rationale": "先建立事实基线，避免后续分析建立在模糊概念上。",
                },
                {
                    "question": f"支撑「{query}」的关键技术路径与架构是什么？",
                    "search_query": f"{query} 技术架构 实现 方案",
                    "rationale": "识别技术路线差异，是评估可行性与成本的前提。",
                },
                {
                    "question": f"「{query}」有哪些真实落地案例与量化效果？",
                    "search_query": f"{query} 案例 效果 数据 实践",
                    "rationale": "用案例和数据校验宣传口径，区分概念验证与规模化落地。",
                },
                {
                    "question": f"「{query}」的主要风险、争议与演进趋势是什么？",
                    "search_query": f"{query} 风险 挑战 趋势 争议",
                    "rationale": "补齐反方证据，避免报告只呈现单一乐观叙事。",
                },
            ]
        }
        return json.dumps(plan, ensure_ascii=False)

    @staticmethod
    def _mock_note(question: str, prompt: str) -> str:
        ids = list(dict.fromkeys(re.findall(r"\[(S\d+)\]", prompt)))
        chosen = ids[:3]
        focus = question[:80].strip()
        note = {
            "summary": (
                f"围绕该子问题（{focus}），公开材料呈现出三类信息：概念边界仍有分歧，"
                "工程实现集中在任务编排、工具调用、上下文管理与评测闭环，"
                "公开案例则更多出现在知识密集型流程。演示模式未调用真实模型核验事实，"
                "上述判断仅用于展示研究流程与引用机制。"
            ),
            "key_points": [
                f"概念与边界尚未完全统一，不同来源的定义存在差异。{''.join(f'[{i}]' for i in chosen[:2])}",
                f"工程难点在系统编排、工具可靠性与评测闭环，而非单个模型能力。{''.join(f'[{i}]' for i in chosen[:2])}",
                f"规模化收益缺少统一口径的量化证据，需要结合失败案例评估。{''.join(f'[{i}]' for i in chosen[-2:])}",
            ],
            "source_ids": chosen,
        }
        return json.dumps(note, ensure_ascii=False)

    @staticmethod
    def _mock_critique(query: str) -> str:
        critique = {
            "verdict": "现有笔记覆盖了概念、技术与案例，但对可量化指标和失败边界的证据不足。",
            "gaps": [
                "缺少可比的成本、时延或准确率指标",
                "缺少失败案例与适用边界",
            ],
            "followups": [
                {
                    "question": f"「{query}」的量化收益与失败案例有哪些？",
                    "search_query": f"{query} 量化 指标 成本 失败 案例",
                    "rationale": "补充反方证据与可验证指标，降低结论过度乐观的风险。",
                }
            ],
        }
        return json.dumps(critique, ensure_ascii=False)

    @staticmethod
    def _mock_report(query: str, prompt: str) -> str:
        ids = list(dict.fromkeys(re.findall(r"\[(S\d+)\]", prompt)))
        refs = "".join(f"[{sid}]" for sid in ids[:4]) or ""
        return f"""## 摘要

围绕「{query}」，本次研究从概念现状、技术路径、落地案例与风险边界四个维度展开。综合检索到的公开材料，可以给出的核心判断是：该方向已从概念验证进入工程化探索阶段，但距离可复制的规模化收益仍有明显距离 {refs}。

> 演示模式提示：以下内容由内置模拟模型生成，仅用于展示多 Agent 协作、引用与报告生成流程，不代表真实研究结论。

## 核心发现

- **共识正在形成，边界仍模糊。** 多数来源认可其长期价值，但对能力边界、适用场景的定义并不一致，直接比较不同案例的收益容易失真 {refs}。
- **工程难点在系统而非单点。** 真正决定效果的往往不是模型本身，而是任务拆解、工具可靠性、上下文管理与评测闭环的组合设计 {refs}。
- **案例集中在知识密集流程。** 公开实践更多出现在检索、分析与文档处理环节，涉及高风险决策时仍保留人工复核 {refs}。
- **量化证据仍然稀缺。** 成本、时延、准确率的口径不统一，失败案例披露不足，导致 ROI 评估普遍偏乐观 {refs}。

## 详细分析

### 1. 从能力展示走向系统交付

早期项目常用单次演示证明可行性，而生产环境要求可观测、可回滚、可评估。多智能体架构的价值不在于“Agent 数量”，而在于把规划、执行与校验拆分给不同角色，让错误在流程内部被发现，而不是直接暴露给最终用户 {refs}。

### 2. 成本与可靠性的权衡

增加一次模型调用或一轮检索，都会带来延迟与费用。实践中更常见的做法是设置预算上限、限制反思轮次，并对工具调用做缓存与降级。对多数业务而言，稳定的窄场景流程比通用的“全能助手”更容易产生可衡量收益 {refs}。

### 3. 评测决定迭代速度

如果只以主观感受评价输出，系统很难持续改进。可落地的做法包括：为关键结论建立来源校验、抽样人工评分、记录失败类型，并把一次任务的完整事件流留存下来用于复盘 {refs}。

## 风险与不确定性

- 检索结果可能过时或相互矛盾，引用存在不等于结论正确。
- 演示模式未调用真实模型，本报告中的判断仅用于流程展示。
- 真实落地还需评估数据合规、权限隔离与人工复核成本 {refs}。

## 结论

「{query}」值得关注，但更适合以“窄场景 + 强评测 + 人工兜底”的方式推进。下一步应优先补齐可量化指标与失败案例，再决定是否扩大投入 {refs}。
"""
