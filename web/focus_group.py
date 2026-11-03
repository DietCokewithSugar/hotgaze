"""AI 焦点小组 · AI focus group: a simulated focus group on the DeepSeek API.

The browser sends a brief (one research question, optional background, target users
and material) and the server answers each of three steps as a stream of events:

    plan      recruit participants and draft a discussion guide; with screenshots,
              describe every screen; with a heatmap, note what it predicts
    discuss   the moderator asks each guide question, every participant answers in
              turn and hears everyone before them, one probe per question, then a
              closing round; a broken or stopped session resumes where it left off
    report    a structured report written from the transcript

Every prompt lives here, so the endpoints cannot be used as a general-purpose LLM
proxy. Nothing is stored: the browser holds the plan and transcript and sends them
back for the next step.
"""

# The prompts below are prose whose line breaks are part of the prompt; do not wrap them.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from string import Template
from typing import Any, Literal, Protocol

try:
    import httpx
except ImportError:  # pragma: no cover - web/requirements.txt installs it
    httpx = None  # type: ignore[assignment]

Lang = Literal["zh", "en"]
Mode = Literal["text", "flow", "ab", "heatmap"]
Event = tuple[str, dict[str, Any]]

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

MIN_PARTICIPANTS = 3
MAX_PARTICIPANTS = 6
MAX_QUESTIONS = 6
MAX_FLOW_IMAGES = 20
MAX_AB_IMAGES = 10  # per version
MAX_TURNS = 400

TOPIC_MAX = 300
CONTEXT_MAX = 3000
AUDIENCE_MAX = 300
_FIELD_MAX = {
    "name": 20,
    "summary": 80,
    "profile": 700,
    "voice": 140,
    "question": 300,
    "purpose": 200,
    "title": 60,
    "description": 400,
    "objective": 400,
    "element": 60,
    "note": 240,
}
_TURN_TEXT_MAX = 4000

STANCES = ("supportive", "neutral", "skeptical")
_STANCE_WORDS = {
    "supportive": ("support", "positive", "open", "支持", "积极", "愿意", "乐观"),
    "skeptical": ("skeptic", "sceptic", "negative", "doubt", "怀疑", "抵触", "反对", "消极"),
    "neutral": ("neutral", "undecided", "wait", "观望", "中立", "中性", "看情况"),
}

# Discussion settings. Turns run without thinking for speed; planning and the report
# think, which is where structure matters.
_TURN_TEMPERATURE = 1.2
_MODERATOR_TEMPERATURE = 1.0
_PROBE_TEMPERATURE = 0.7
_TURN_MAX_TOKENS = 700
_MODERATOR_MAX_TOKENS = 450
_THINKING_MAX_TOKENS = 32768
_RETRY_DELAYS = (2.0, 5.0)
# Questions that name no screens still show them all when a flow is this short.
_ALL_SHOTS_UP_TO = 6
PING_SECONDS = 15.0


# ── Settings and errors ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    report_model: str = DEFAULT_MODEL

    @classmethod
    def from_env(cls) -> Settings:
        model = os.environ.get("DEEPSEEK_MODEL", "").strip() or DEFAULT_MODEL
        return cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "").strip().rstrip("/")
            or DEFAULT_BASE_URL,
            model=model,
            report_model=os.environ.get("DEEPSEEK_REPORT_MODEL", "").strip() or model,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


class FocusError(Exception):
    """A failure the UI can translate: a stable code, an English message, its values."""

    def __init__(self, code: str, message: str, **params: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.params = params

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "params": self.params}


class _Retryable(Exception):
    def __init__(self, error: FocusError) -> None:
        super().__init__(error.message)
        self.error = error


# ── DeepSeek client ──────────────────────────────────────────────────────────


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, usage: dict[str, Any]) -> None:
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.cache_hit_tokens += int(usage.get("prompt_cache_hit_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        details = usage.get("completion_tokens_details") or {}
        self.reasoning_tokens += int(details.get("reasoning_tokens") or 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


class LLM(Protocol):
    settings: Settings
    usage: Usage

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        thinking: bool = False,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]: ...

    async def aclose(self) -> None: ...


class DeepSeek:
    """Streams chat completions from DeepSeek's OpenAI-format API.

    Yields the answer text as it arrives (reasoning is not shown), adds every call's
    token usage to `usage`, and retries rate limits, overloads and dropped connections
    as long as nothing has been yielded yet.
    """

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        if httpx is None:
            raise FocusError("focus_unavailable", "httpx is not installed on this server.")
        self.settings = settings
        self.usage = Usage()
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            # Reads may idle while the model thinks; DeepSeek sends keep-alive comments.
            timeout=httpx.Timeout(connect=15.0, read=240.0, write=60.0, pool=30.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        thinking: bool = False,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": model or self.settings.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            # Thinking is on by default in the API; turns explicitly switch it off.
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if thinking:
            payload["reasoning_effort"] = "high"
        elif temperature is not None:
            payload["temperature"] = temperature
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens

        attempt = 0
        while True:
            yielded = False
            try:
                async with contextlib.aclosing(self._once(payload)) as chunks:
                    async for text in chunks:
                        yielded = True
                        yield text
                return
            except _Retryable as exc:
                if yielded or attempt >= len(_RETRY_DELAYS):
                    raise exc.error from None
                await asyncio.sleep(_RETRY_DELAYS[attempt])
                attempt += 1

    async def _once(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code != 200:
                    raise _http_error(response.status_code, await response.aread())
                self.usage.calls += 1
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # blank separators and ": keep-alive" comments
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if chunk.get("usage"):
                        self.usage.add(chunk["usage"])
                    for choice in chunk.get("choices") or []:
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            yield content
                        finish = choice.get("finish_reason")
                        if finish == "content_filter":
                            raise FocusError(
                                "focus_filtered",
                                "DeepSeek declined to continue because of its content policy.",
                            )
                        if finish == "insufficient_system_resource":
                            raise _Retryable(
                                FocusError("focus_llm_busy", "DeepSeek is out of capacity.")
                            )
        except httpx.TransportError as exc:  # timeouts, refused and dropped connections
            raise _Retryable(
                FocusError("focus_llm_unreachable", f"Could not reach DeepSeek: {exc!s}")
            ) from exc


def _http_error(status: int, body: bytes) -> Exception:
    text = body.decode("utf-8", "replace")
    try:
        detail = json.loads(text).get("error", {}).get("message") or text
    except (ValueError, AttributeError):
        detail = text
    detail = str(detail)[:300]
    if status == 401:
        return FocusError("focus_llm_auth", "DeepSeek rejected the API key.", reason=detail)
    if status == 402:
        return FocusError("focus_llm_balance", "The DeepSeek account has run out of balance.")
    if status == 429:
        return _Retryable(FocusError("focus_llm_busy", "DeepSeek rate limit reached."))
    if status >= 500:
        return _Retryable(
            FocusError("focus_llm_failed", f"DeepSeek error {status}: {detail}", reason=detail)
        )
    return FocusError(
        "focus_llm_rejected", f"DeepSeek refused the request: {detail}", reason=detail
    )


# ── Inputs ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Brief:
    lang: Lang
    topic: str
    context: str = ""
    audience: str = ""
    participants: int = 5
    mode: Mode = "text"


@dataclass(frozen=True)
class Shot:
    """One screenshot shown to the group. `ref` is 1…20, A1…/B1… or S."""

    ref: str
    data_url: str


@dataclass
class Material:
    mode: Mode
    shots: list[Shot] = field(default_factory=list)
    overlay: str | None = None  # heatmap overlay data URL
    attention: dict[str, Any] | None = None  # HotGaze summary, heatmap mode

    @property
    def refs(self) -> list[str]:
        return [shot.ref for shot in self.shots]


_REF_PATTERNS = {"flow": r"(?:[1-9]|1\d|20)", "ab": r"[AB](?:[1-9]|10)", "heatmap": r"S"}


def plan_refs(mode: Mode, raw_plan: Any) -> list[str]:
    """Screen refs a plan names, for the report step, which gets no screenshots."""
    pattern = _REF_PATTERNS.get(mode)
    if not pattern or not isinstance(raw_plan, dict):
        return []
    refs: list[str] = []
    for item in _items(raw_plan.get("screens")):
        ref = str(item.get("ref", "")).strip().upper()
        if re.fullmatch(pattern, ref) and ref not in refs:
            refs.append(ref)
    return refs


def shot_refs(mode: Mode, count_a: int, count_b: int = 0) -> list[str]:
    if mode == "heatmap":
        return ["S"][:count_a]
    if mode == "ab":
        return [f"A{i}" for i in range(1, count_a + 1)] + [f"B{i}" for i in range(1, count_b + 1)]
    return [str(i) for i in range(1, count_a + 1)]


# ── Prompts ──────────────────────────────────────────────────────────────────

_ZH: dict[str, str] = {
    "moderator": "主持人",
    "none": "（无）",
    "unset": "（未指定）",
    "stance.supportive": "支持，愿意尝试",
    "stance.neutral": "观望，看情况",
    "stance.skeptical": "怀疑，有抵触",
    "plan.system": (
        "你是一位经验丰富的用户研究员，正在为一场焦点小组座谈做准备：招募参与者，并写一份讨论"
        "提纲。你写的内容会直接用来驱动一场由 AI 扮演参与者的模拟座谈，所以人设必须真实、具体、"
        "彼此不同。只输出 JSON。"
    ),
    "plan.user": """## 研究简报
研究问题：${topic}
背景信息：${context}
目标用户：${audience}
参与人数：${count} 位
讨论素材：${material}

## 招募参与者
- 一共 ${count} 位，都是目标用户里真实存在的人。彼此差异要大：年龄、城市、职业、收入、经验和使用习惯都不同。
- 对研究问题的初始立场要覆盖三种：supportive（支持、愿意尝试）、neutral（观望、看情况）、skeptical（怀疑、有抵触），至少一位明确的 skeptical。
- profile 写 80–150 字：生活处境，以及和研究问题直接相关的真实经历（用过什么产品、花过多少钱、踩过什么坑、为什么这么想），要有具体的名称、数字和场景，不要空泛的性格形容词。
- voice 用一句话写说话风格（比如"说话很冲，爱拿数据怼人"、"慢条斯理，爱讲自己年轻时的事"），让每个人一开口就能分辨出来。
- name 用符合当地习惯的真实姓名，不要用"用户 A"。summary 用"年龄 · 城市 · 职业"的格式。

## 讨论提纲
- 写 ${questions} 个问题：从具体经历切入，再到感受、需求和取舍，最后可以请大家权衡或排序。
- 问题要开放、中立，不诱导，一次只问一件事；口语化，像主持人会说的话。
- purpose 用一句话写这一题想了解什么。
${guide_rules}
## 输出格式
只输出一个 JSON 对象，不要任何解释：
{
  "title": "本场座谈的简短标题，不超过 16 个字",
  "objective": "研究目标，1–2 句",
${screens_schema}${attention_schema}  "participants": [
    {"id": "p1", "name": "姓名", "summary": "34 岁 · 杭州 · 互联网运营", "profile": "……", "voice": "……", "stance": "supportive|neutral|skeptical"}
  ],
  "guide": [
    {"id": "q1", "question": "……", "purpose": "……"${guide_schema}}
  ]
}""",
    "plan.material.text": "无，只围绕研究问题讨论。",
    "plan.material.flow": (
        "一个业务的完整流程，共 ${n} 张截图，按顺序编号 1–${n}，附在本消息里。请先逐张仔细看。"
    ),
    "plan.material.ab": (
        "同一场景的两版流程：A 版 ${na} 张（A1–A${na}），B 版 ${nb} 张（B1–B${nb}），"
        "附在本消息里。请先逐张仔细看。"
    ),
    "plan.material.heatmap": (
        "一张页面截图（编号 S），以及 HotGaze 预测的注意力热力图（越红表示预测的视线越集中），"
        "都附在本消息里。热力图的数据：\n${attention}"
    ),
    "plan.rules.flow": (
        "- 问题按流程的阶段组织（比如注册、挑选、支付……），每一题用 screens 标出对应的截图编号；"
        "可以有一题聊整个流程的总体感受（screens 留空）。\n"
        "- 有截图时，先逐张看清楚，再在 screens 里给每张写标题和描述。\n"
    ),
    "plan.rules.ab": (
        '- 先分别聊 A 版和 B 版的具体环节（用 screens 标出编号，如 ["A2", "A3"]），再直接比较'
        "两版；问题里不要暗示哪一版更好。\n"
        "- 先逐张看清楚，再在 screens 里给每张写标题和描述。\n"
    ),
    "plan.rules.heatmap": (
        "- 第 1 题先让大家凭第一印象说注意到了什么、会先点哪里（不提热力图）；在第 2 或第 3 题由"
        "主持人展示热力图（这一题 reveal_heatmap 为 true，其余为 false），追问预测和大家的感受"
        "哪里一致、哪里不一致，以及为什么没注意到某些重要元素。\n"
        "- attention 根据热力图和截图，列出 3–6 个最值得讨论的元素，包括被忽视的重要元素。\n"
    ),
    "plan.schema.screens": (
        '  "screens": [\n'
        '    {"ref": "${ref}", "title": "页面名称，不超过 12 个字", '
        '"description": "这一页是什么、关键元素和文案、用户能做什么，1–2 句"}\n'
        "  ],\n"
    ),
    "plan.schema.attention": (
        '  "attention": [\n'
        '    {"element": "页面元素", "level": "high|medium|low", '
        '"note": "热力图显示的情况，以及它可能意味着什么，1 句"}\n'
        "  ],\n"
    ),
    "session.system": """你在参与一场模拟的焦点小组座谈（一种用户研究方法）。每次你只扮演其中一个角色（主持人或某一位参与者），只写这个角色的一次发言。

## 研究
研究问题：${topic}
研究目标：${objective}
背景信息：${context}
目标用户：${audience}

## 展示给大家的素材
${material}

## 在场的参与者
${roster}

## 座谈规则
- 这是一场真实感很强的线下座谈：大家围坐一桌，按主持人的提问轮流发言，能听到彼此说的每一句话。
- 每位参与者只从自己的人设出发：用自己的处境、经历、习惯和情绪说话，举具体的例子（产品名、金额、时间、场景），不泛泛而谈。
- 观点要有差异。人设决定了你会认同什么、反对什么：不要为了和气而附和，也不要说"大家说得都有道理"。可以直接反驳别人，点名回应（"我不太同意王姐说的……"）。被说服也可以，但要慢，而且要有理由。
- 只能引用别人在座谈里亲口说过的话，不要假装知道别人没讲过的经历。
- 口语化，像在说话而不是写文章：短句、停顿、口头禅都可以；不要列点，不要"首先、其次、最后"，不要总结陈词，不要客套。
- 一般 2–5 句话；没什么想法时可以很短。
- 绝不提到自己是 AI、模型或"模拟"。""",
    "session.material.text": "本场没有展示素材，只围绕研究问题讨论。",
    "session.material.flow": (
        "主持人给大家看了一个业务的完整流程，共 ${n} 张截图（截图附在下一条消息里，编号一致）：\n"
        "${screens}"
    ),
    "session.material.ab": (
        "主持人给大家看了同一场景的两版流程（截图附在下一条消息里，编号一致）。\n"
        "A 版：\n${screens_a}\nB 版：\n${screens_b}"
    ),
    "session.material.heatmap": (
        "主持人给大家看了一张页面截图（附在下一条消息里）：\n${screens}\n"
        "主持人手上还有这张页面的注意力热力图，会在讨论中途展示给大家。"
    ),
    "shots.label": "以下是展示给大家的截图：",
    "shot": "图 ${ref}：${title}",
    "transcript.head": "## 到目前为止的座谈记录",
    "transcript.empty": "（座谈还没开始）",
    "transcript.heatmap": "（主持人展示了注意力热力图，见下图。越红表示预测的视线越集中。）",
    "heatmap.label": "主持人展示的注意力热力图：",
    "turn": """现在轮到「${name}」发言。

你的人设：${name}，${summary}。${profile}
说话风格：${voice}
你对这个话题的初始立场：${stance}。立场可以随讨论慢慢变化，但要有理由。

${situation}

直接写出${name}这一次说的话：不要写名字或冒号，不要引号，不要动作或旁白描写。""",
    "turn.answer": (
        "主持人刚才问：「${question}」\n先听听前面的人说了什么：如果谁的话和你的经历或看法相关"
        "（不管认同还是反对），可以先接一句，再说你自己的。"
    ),
    "turn.first": "主持人刚才问：「${question}」\n你是第一个回答的。",
    "turn.probe": "主持人刚才点名追问了你：「${question}」\n正面回应，说得具体一点。",
    "turn.final": "主持人请每个人做最后的表态：「${question}」\n用一两句话说出你最终的态度。",
    "turn.final.ab": (
        "主持人请每个人做最后的表态：「${question}」\n"
        "用一两句话明确说你更倾向 A 版还是 B 版，以及最关键的一个理由。"
    ),
    "ask": """现在由主持人发言。主持人中立、温和、善于倾听：不发表自己的观点，不评价谁对谁错，不替参与者做总结。

${situation}

只写主持人这一次说的话，口语化，不超过 4 句。问题的意思不要改，可以换成更自然的说法。不要写"主持人："前缀，不要引号。""",
    "ask.first": (
        "这是开场。先用一两句话欢迎大家，用自己的话简单说明今天聊什么（不要照念研究目标），"
        "提醒大家没有对错、说真实想法就好，然后抛出第一个问题：「${question}」"
    ),
    "ask.next": (
        "先用一句话自然地承接上一题的讨论（可以点到一个有意思的分歧或共识，但不要逐个总结），"
        "然后抛出下一个问题：「${question}」"
    ),
    "ask.screens": "这个问题对应截图 ${refs}，提问时请大家看这几张。",
    "ask.reveal": (
        "提问之前，先向大家展示注意力热力图（已经附在这条发言里，大家都能看到），"
        "用两三句话说明它预测的重点：\n${attention}\n然后再提问。"
    ),
    "probe": """主持人刚听完大家对第 ${number} 题的回答。判断要不要追问一次：
- 值得追问：有明显分歧、有人说得笼统没有例子、有人提到了值得深挖的具体经历、或者有人几乎没说什么。
- 不需要追问：大家已经说得充分具体，再问只会重复。
如果追问，点名 1–2 位参与者（用名字称呼），问题要具体、开放，不超过 2 句，口语化。

只输出 JSON：{"probe": true, "targets": ["p2"], "question": "主持人追问的话"}，或者 {"probe": false}。targets 只能从这些 id 里选：${roster}。""",
    "close": """所有问题都聊完了，现在由主持人收尾。先用一句话感谢大家，然后请每个人用一两句话做最后的表态：${ask}

只写主持人这一次说的话，不超过 3 句。不要写前缀，不要引号。""",
    "close.ask": "如果只能改变一件事，你最希望改变的是什么？（可以换成更贴合本次主题的问法）",
    "close.ask.ab": "你更倾向 A 版还是 B 版？最关键的理由是什么？",
    "report.system": (
        "你是一位资深用户研究员，负责把一场焦点小组座谈整理成研究报告。你只依据座谈记录写作，"
        "不编造记录里没有的观点。只输出 JSON。"
    ),
    "report.user": """## 研究信息
研究问题：${topic}
研究目标：${objective}
背景信息：${context}
目标用户：${audience}

## 展示给大家的素材
${material}

## 参与者
${roster}

## 座谈记录
${transcript}

## 写作要求
- 只依据座谈记录。quotes 必须是参与者的原话，可以截取其中一段，但一个字都不能改。
- 区分多数人的共识和个别人的观点：每一条都用 id 写明涉及哪些参与者。evidence 表示证据强度：strong（多人一致且有具体经历）、moderate、weak（只有一人提到或比较笼统）。
- 建议要具体、可执行，related 写它对应的发现序号（从 1 开始）。
- 这是 AI 扮演参与者的模拟座谈：limitations 说明模拟的局限，validate 列出最需要用真实用户验证的 3–5 个问题。
${report_rules}
## 输出格式
只输出一个 JSON 对象：
{
  "headline": "一句话结论，不超过 40 个字",
  "summary": "2–4 句展开",
  "findings": [
    {"title": "……", "detail": "……", "evidence": "strong|moderate|weak", "participants": ["p1", "p3"], "quotes": [{"speaker": "p1", "text": "原话"}]}
  ],
  "pain_points": [{"title": "……", "detail": "……", "participants": ["p2"]}],
  "needs": [{"title": "……", "detail": "……", "participants": ["p1"]}],
  "disagreements": [{"topic": "……", "sides": [{"participants": ["p1"], "view": "……"}, {"participants": ["p4"], "view": "……"}]}],
  "stances": [{"participant": "p1", "stance": "positive|neutral|negative|mixed", "summary": "最终态度，一句话"}],
  "opportunities": [{"title": "……", "detail": "……", "priority": "high|medium|low", "related": [1]}],
${extra_schema}  "validate": ["……"],
  "limitations": "……"
}
findings 写 3–6 条；pain_points、needs 各 2–6 条；disagreements 0–4 条；stances 每位参与者一条；opportunities 3–6 条。""",
    "report.rules.flow": "- flow_issues 按截图编号指出流程里的具体问题，severity 表示严重程度。\n",
    "report.rules.ab": (
        "- ab 汇总偏好：按收尾时的表态，把每位参与者归入 a、b 或 undecided；分别写两版的优点"
        "和问题；verdict 给出结论和下一步建议。flow_issues 按截图编号（A1、B2……）指出具体问题。\n"
    ),
    "report.rules.heatmap": (
        "- attention 对照热力图的预测和参与者的反馈：match 填 aligned（一致）、conflict（矛盾）"
        "或 partial（部分一致）。\n"
    ),
    "report.schema.flow": (
        '  "flow_issues": [{"screens": ["3", "4"], "issue": "……", "severity": "high|medium|low", '
        '"suggestion": "……"}],\n'
    ),
    "report.schema.ab": (
        '  "ab": {"a": ["p1"], "b": ["p2"], "undecided": [], "a_strengths": ["……"], '
        '"a_issues": ["……"], "b_strengths": ["……"], "b_issues": ["……"], "verdict": "……"},\n'
        '  "flow_issues": [{"screens": ["A2", "B2"], "issue": "……", "severity": "high|medium|low", '
        '"suggestion": "……"}],\n'
    ),
    "report.schema.heatmap": (
        '  "attention": [{"element": "……", "predicted": "热力图预测的情况", '
        '"feedback": "参与者怎么说", "match": "aligned|conflict|partial"}],\n'
    ),
    "attention.grid": "九宫格注意力占比（从左上到右下，每行三格）：\n${rows}",
    "attention.row": "${label}：${cells}",
    "attention.rows": "上|中|下",
    "attention.focal": "预测的焦点（按强度排序，坐标是占页面宽、高的比例）：${points}",
    "attention.region": "标出的区域「${name}」分到 ${share} 的注意力（面积占页面 ${area}）",
    "attention.item": "- ${element}（${level}）：${note}",
    "level.high": "注意力高",
    "level.medium": "注意力中等",
    "level.low": "注意力低",
    "json.retry": "上一次的输出不是合法 JSON。请只输出一个合法的 JSON 对象，不要任何其他文字。",
    "silent": "（没有说话）",
}

_EN: dict[str, str] = {
    "moderator": "Moderator",
    "none": "(none)",
    "unset": "(not specified)",
    "stance.supportive": "open to it, willing to try",
    "stance.neutral": "undecided, it depends",
    "stance.skeptical": "skeptical, resistant",
    "plan.system": (
        "You are an experienced UX researcher preparing a focus group: recruiting participants "
        "and writing a discussion guide. What you write drives a simulated session in which AI "
        "plays the participants, so the personas must be real, specific and different from each "
        "other. Output JSON only."
    ),
    "plan.user": """## Research brief
Research question: ${topic}
Background: ${context}
Target users: ${audience}
Group size: ${count}
Material: ${material}

## Recruit participants
- ${count} people, all real members of the target users. Make them very different: age, city, job, income, experience and habits.
- Their starting attitude to the research question must cover supportive (open, willing to try), neutral (undecided) and skeptical (doubtful, resistant), with at least one clearly skeptical person.
- profile: 60–110 words on their situation and the real experience that bears on the question (products used, money spent, things that went wrong, why they think what they think), with concrete names, numbers and scenes, not personality adjectives.
- voice: one sentence on how they talk (e.g. "blunt, throws numbers at people", "slow and chatty, tells stories about the old days") so each one is recognisable from the first line.
- name: a realistic full name for the market in the brief, never "User A". summary: "age · city · job".

## Discussion guide
- Write ${questions} questions: start from concrete experience, move to feelings, needs and trade-offs, and possibly end by asking people to weigh or rank options.
- Open and neutral, never leading, one thing at a time, phrased the way a moderator would say it.
- purpose: one sentence on what the question is meant to learn.
${guide_rules}
## Output format
Output one JSON object and nothing else:
{
  "title": "a short title for the session, at most 6 words",
  "objective": "the research objective, 1–2 sentences",
${screens_schema}${attention_schema}  "participants": [
    {"id": "p1", "name": "Full Name", "summary": "34 · Leeds · operations manager", "profile": "…", "voice": "…", "stance": "supportive|neutral|skeptical"}
  ],
  "guide": [
    {"id": "q1", "question": "…", "purpose": "…"${guide_schema}}
  ]
}""",
    "plan.material.text": "none; the group discusses the research question only.",
    "plan.material.flow": (
        "one complete flow of ${n} screenshots, numbered 1–${n} in order, attached to this "
        "message. Look at each one carefully first."
    ),
    "plan.material.ab": (
        "two versions of the same flow: version A with ${na} screenshots (A1–A${na}) and "
        "version B with ${nb} (B1–B${nb}), attached to this message. Look at each one carefully."
    ),
    "plan.material.heatmap": (
        "one page screenshot (ref S) and its HotGaze attention heatmap (redder means more "
        "predicted attention), both attached to this message. Heatmap data:\n${attention}"
    ),
    "plan.rules.flow": (
        "- Organise the questions by stage of the flow (sign-up, choosing, paying…) and list the "
        "screenshots each one covers in screens; one question may cover the whole flow (empty "
        "screens).\n"
        "- Look at every screenshot first, then give each a title and description in screens.\n"
    ),
    "plan.rules.ab": (
        "- First discuss specific steps of A and of B (refs in screens, e.g. "
        '["A2", "A3"]), then compare the versions directly; never hint which one is better.\n'
        "- Look at every screenshot first, then give each a title and description in screens.\n"
    ),
    "plan.rules.heatmap": (
        "- Question 1 asks for first impressions: what people noticed and what they would click "
        "first, without mentioning the heatmap. In question 2 or 3 the moderator shows the "
        "heatmap (reveal_heatmap true on that question, false elsewhere) and probes where the "
        "prediction matches people's experience, where it does not, and why important "
        "elements went unnoticed.\n"
        "- attention: from the heatmap and the screenshot, list the 3–6 elements most worth "
        "discussing, including important elements that are being ignored.\n"
    ),
    "plan.schema.screens": (
        '  "screens": [\n'
        '    {"ref": "${ref}", "title": "page name, at most 5 words", '
        '"description": "what the page is, key elements and copy, what the user can do; '
        '1–2 sentences"}\n'
        "  ],\n"
    ),
    "plan.schema.attention": (
        '  "attention": [\n'
        '    {"element": "page element", "level": "high|medium|low", '
        '"note": "what the heatmap shows and what it may mean, 1 sentence"}\n'
        "  ],\n"
    ),
    "session.system": """You are taking part in a simulated focus group (a user research method). Each time you play exactly one role, the moderator or one participant, and write that role's next contribution only.

## Research
Research question: ${topic}
Objective: ${objective}
Background: ${context}
Target users: ${audience}

## Material shown to the group
${material}

## Participants in the room
${roster}

## Ground rules
- This is a realistic in-person session: everyone sits around one table, answers the moderator in turn and hears every word the others say.
- Each participant speaks only from their own persona: their situation, history, habits and feelings, with concrete examples (product names, amounts, dates, places), never generalities.
- Views must differ. The persona decides what you agree or disagree with; don't agree to be nice and never say everyone has a point. Push back directly and by name ("I don't buy that, Sarah…"). You may be persuaded, but slowly and for a reason.
- Only refer to what others have actually said in the session; don't pretend to know their background.
- Talk, don't write: short sentences, contractions and hesitations are fine; no bullet points, no "firstly / secondly", no wrap-ups, no politeness padding.
- Usually 2–5 sentences; very short is fine when you have little to add.
- Never mention being an AI, a model or a simulation.""",
    "session.material.text": "No material is shown; the group discusses the research question only.",
    "session.material.flow": (
        "The moderator showed the group one complete flow of ${n} screenshots (attached to the "
        "next message, same numbers):\n${screens}"
    ),
    "session.material.ab": (
        "The moderator showed the group two versions of the same flow (attached to the next "
        "message, same refs).\nVersion A:\n${screens_a}\nVersion B:\n${screens_b}"
    ),
    "session.material.heatmap": (
        "The moderator showed the group one page screenshot (attached to the next message):\n"
        "${screens}\nThe moderator also holds the page's attention heatmap and will show it "
        "partway through the session."
    ),
    "shots.label": "The screenshots shown to the group:",
    "shot": "Screen ${ref}: ${title}",
    "transcript.head": "## Transcript so far",
    "transcript.empty": "(The session has not started yet.)",
    "transcript.heatmap": (
        "(The moderator shows the attention heatmap below. Redder means more predicted attention.)"
    ),
    "heatmap.label": "The attention heatmap the moderator showed:",
    "turn": """It is now ${name}'s turn to speak.

Your persona: ${name}, ${summary}. ${profile}
How you talk: ${voice}
Your starting attitude to the topic: ${stance}. It may shift during the session, slowly and for a reason.

${situation}

Write only what ${name} says this time: no name or colon, no quotation marks, no stage directions.""",
    "turn.answer": (
        'The moderator just asked: "${question}"\nListen to the people before you: if what '
        "someone said touches your experience or views (agreeing or not), react to it briefly, "
        "then give your own answer."
    ),
    "turn.first": 'The moderator just asked: "${question}"\nYou are the first to answer.',
    "turn.probe": (
        'The moderator just put a follow-up to you by name: "${question}"\n'
        "Answer it head-on and be specific."
    ),
    "turn.final": (
        'The moderator asks everyone for a final word: "${question}"\n'
        "Say where you finally stand in one or two sentences."
    ),
    "turn.final.ab": (
        'The moderator asks everyone for a final word: "${question}"\n'
        "In one or two sentences, say clearly whether you lean to version A or version B, and "
        "your single most important reason."
    ),
    "ask": """The moderator speaks now. The moderator is neutral, warm and a good listener: shares no opinions, never judges who is right, never sums up for participants.

${situation}

Write only what the moderator says this time, conversational, at most 4 sentences. Keep the question's meaning; you may phrase it more naturally. No "Moderator:" prefix, no quotation marks.""",
    "ask.first": (
        "This is the opening. Welcome everyone in a sentence or two, say in your own words what "
        "today is about (don't read out the objective), remind them there are no wrong answers "
        'and honest views are what you want, then ask the first question: "${question}"'
    ),
    "ask.next": (
        "Bridge from the last question in one natural sentence (you may point to an interesting "
        "disagreement or agreement, but don't summarise everyone), then ask the next question: "
        '"${question}"'
    ),
    "ask.screens": "This question is about screens ${refs}; ask people to look at them.",
    "ask.reveal": (
        "Before asking, show the group the attention heatmap (attached to this turn, everyone "
        "can see it) and explain in two or three sentences what it predicts:\n${attention}\n"
        "Then ask the question."
    ),
    "probe": """The moderator has just heard everyone's answers to question ${number}. Decide whether to probe once:
- Worth probing: a clear disagreement, a vague answer with no example, a concrete experience worth digging into, or someone who barely spoke.
- Not needed: answers were full and specific; another question would only repeat them.
If you probe, address 1–2 participants by name with a specific, open question of at most 2 sentences.

Output JSON only: {"probe": true, "targets": ["p2"], "question": "what the moderator says"} or {"probe": false}. targets must come from these ids: ${roster}.""",
    "close": """All questions are covered; the moderator now closes. Thank everyone in one sentence, then ask each person for a final word in one or two sentences: ${ask}

Write only what the moderator says, at most 3 sentences. No prefix, no quotation marks.""",
    "close.ask": (
        "If you could change just one thing, what would it be? (rephrase to fit today's topic)"
    ),
    "close.ask.ab": "Do you lean to version A or version B, and what is your main reason?",
    "report.system": (
        "You are a senior UX researcher turning a focus group into a research report. You write "
        "only from the transcript and never invent views that are not in it. Output JSON only."
    ),
    "report.user": """## Research
Research question: ${topic}
Objective: ${objective}
Background: ${context}
Target users: ${audience}

## Material shown to the group
${material}

## Participants
${roster}

## Transcript
${transcript}

## Writing rules
- Use the transcript only. quotes must be participants' exact words: you may cut a passage, but never change a word.
- Separate what most people agreed on from individual views: tag every item with the participant ids involved. evidence is the strength of support: strong (several people, concrete experience), moderate, weak (one person or vague).
- Recommendations must be specific and actionable; related lists the numbers of the findings they answer (from 1).
- AI played the participants: limitations explains the limits of the simulation, and validate lists the 3–5 questions that most need checking with real users.
${report_rules}
## Output format
Output one JSON object:
{
  "headline": "the conclusion in one sentence, at most 20 words",
  "summary": "2–4 sentences",
  "findings": [
    {"title": "…", "detail": "…", "evidence": "strong|moderate|weak", "participants": ["p1", "p3"], "quotes": [{"speaker": "p1", "text": "exact words"}]}
  ],
  "pain_points": [{"title": "…", "detail": "…", "participants": ["p2"]}],
  "needs": [{"title": "…", "detail": "…", "participants": ["p1"]}],
  "disagreements": [{"topic": "…", "sides": [{"participants": ["p1"], "view": "…"}, {"participants": ["p4"], "view": "…"}]}],
  "stances": [{"participant": "p1", "stance": "positive|neutral|negative|mixed", "summary": "final stance in one sentence"}],
  "opportunities": [{"title": "…", "detail": "…", "priority": "high|medium|low", "related": [1]}],
${extra_schema}  "validate": ["…"],
  "limitations": "…"
}
3–6 findings; 2–6 pain_points and needs each; 0–4 disagreements; one stance per participant; 3–6 opportunities.""",
    "report.rules.flow": (
        "- flow_issues names concrete problems in the flow by screenshot ref; severity is how "
        "serious each one is.\n"
    ),
    "report.rules.ab": (
        "- ab sums up preference: put every participant in a, b or undecided by their final "
        "word; list each version's strengths and issues; verdict gives the conclusion and next "
        "step. flow_issues names concrete problems by ref (A1, B2…).\n"
    ),
    "report.rules.heatmap": (
        "- attention compares the heatmap's prediction with what participants said: match is "
        "aligned, conflict or partial.\n"
    ),
    "report.schema.flow": (
        '  "flow_issues": [{"screens": ["3", "4"], "issue": "…", "severity": "high|medium|low", '
        '"suggestion": "…"}],\n'
    ),
    "report.schema.ab": (
        '  "ab": {"a": ["p1"], "b": ["p2"], "undecided": [], "a_strengths": ["…"], '
        '"a_issues": ["…"], "b_strengths": ["…"], "b_issues": ["…"], "verdict": "…"},\n'
        '  "flow_issues": [{"screens": ["A2", "B2"], "issue": "…", "severity": "high|medium|low", '
        '"suggestion": "…"}],\n'
    ),
    "report.schema.heatmap": (
        '  "attention": [{"element": "…", "predicted": "what the heatmap predicts", '
        '"feedback": "what participants said", "match": "aligned|conflict|partial"}],\n'
    ),
    "attention.grid": "Attention share in a 3×3 grid (top-left to bottom-right, three per row):\n${rows}",
    "attention.row": "${label}: ${cells}",
    "attention.rows": "Top|Middle|Bottom",
    "attention.focal": (
        "Predicted focal points, strongest first (as fractions of page width, height): ${points}"
    ),
    "attention.region": 'Marked region "${name}" gets ${share} of attention (covers ${area} of the page)',
    "attention.item": "- ${element} (${level}): ${note}",
    "level.high": "high attention",
    "level.medium": "medium attention",
    "level.low": "low attention",
    "json.retry": (
        "Your last output was not valid JSON. Output exactly one valid JSON object and nothing else."
    ),
    "silent": "(says nothing)",
}

PROMPTS: dict[str, dict[str, str]] = {"zh": _ZH, "en": _EN}


def _t(lang: Lang, key: str, **values: Any) -> str:
    template = PROMPTS[lang][key]
    if not values:
        return template
    return Template(template).safe_substitute({k: str(v) for k, v in values.items()})


# ── Plan ─────────────────────────────────────────────────────────────────────


async def plan_session(llm: LLM, brief: Brief, material: Material) -> AsyncIterator[Event]:
    """Recruit participants and draft the guide (and describe screens, note attention)."""
    yield "status", {"phase": "recruit"}
    raw = await _json_call(
        llm, _plan_messages(brief, material), brief.lang, model=llm.settings.model
    )
    plan = normalize_plan(raw, brief, material)
    yield "plan", {"plan": plan}
    yield "usage", _usage(llm, llm.settings.model)


def _plan_messages(brief: Brief, material: Material) -> list[dict[str, Any]]:
    lang, mode = brief.lang, brief.mode
    if mode == "flow":
        material_text = _t(lang, "plan.material.flow", n=len(material.shots))
    elif mode == "ab":
        count_a = sum(1 for ref in material.refs if ref.startswith("A"))
        material_text = _t(lang, "plan.material.ab", na=count_a, nb=len(material.shots) - count_a)
    elif mode == "heatmap":
        material_text = _t(
            lang, "plan.material.heatmap", attention=attention_text(lang, material.attention)
        )
    else:
        material_text = _t(lang, "plan.material.text")
    first_ref = material.refs[0] if material.refs else "1"
    has_shots = mode in ("flow", "ab", "heatmap")
    guide_schema = {
        "flow": ', "screens": ["1", "2"]',
        "ab": ', "screens": ["A1", "A2"]',
        "heatmap": ', "reveal_heatmap": false',
    }.get(mode, "")
    prompt = _t(
        lang,
        "plan.user",
        topic=brief.topic,
        context=brief.context or _t(lang, "none"),
        audience=brief.audience or _t(lang, "unset"),
        count=brief.participants,
        questions="3–5" if mode != "heatmap" else "3–4",
        material=material_text,
        guide_rules=_t(lang, f"plan.rules.{mode}") if mode != "text" else "",
        screens_schema=_t(lang, "plan.schema.screens", ref=first_ref) if has_shots else "",
        attention_schema=_t(lang, "plan.schema.attention") if mode == "heatmap" else "",
        guide_schema=guide_schema,
    )
    system = {"role": "system", "content": _t(lang, "plan.system")}
    if not material.shots:
        return [system, {"role": "user", "content": prompt}]
    parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for shot in material.shots:
        parts.append(
            {"type": "text", "text": _t(lang, "shot", ref=shot.ref, title="").rstrip("：: ")}
        )
        parts.append(_image_part(shot.data_url, detail="high"))
    if material.overlay:
        parts.append({"type": "text", "text": _t(lang, "heatmap.label")})
        parts.append(_image_part(material.overlay, detail="high"))
    return [system, {"role": "user", "content": parts}]


def normalize_plan(raw: dict[str, Any], brief: Brief, material: Material) -> dict[str, Any]:
    """Make a model's plan safe to show and to run: ids, lengths, refs, one heatmap reveal."""
    lang = brief.lang
    participants = []
    names: set[str] = set()
    for index, item in enumerate(_items(raw.get("participants")), start=1):
        name = _text(item.get("name"), "name") or (
            f"参与者{index}" if lang == "zh" else f"P{index}"
        )
        while name in names:
            name = f"{name}{index}"
        names.add(name)
        participants.append(
            {
                "id": "",
                "name": name,
                "summary": _text(item.get("summary"), "summary"),
                "profile": _text(item.get("profile"), "profile"),
                "voice": _text(item.get("voice"), "voice"),
                "stance": _stance(item.get("stance")),
            }
        )
    participants = participants[: brief.participants]
    if len(participants) < MIN_PARTICIPANTS:
        raise FocusError("focus_bad_output", "The model did not recruit enough participants.")
    for index, person in enumerate(participants, start=1):
        person["id"] = f"p{index}"

    refs = material.refs
    known = set(refs)
    guide = []
    for item in _items(raw.get("guide"))[:MAX_QUESTIONS]:
        question = _text(item.get("question"), "question")
        if not question:
            continue
        entry: dict[str, Any] = {
            "id": "",
            "question": question,
            "purpose": _text(item.get("purpose"), "purpose"),
        }
        if brief.mode in ("flow", "ab"):
            entry["screens"] = _refs(item.get("screens"), known)
        if brief.mode == "heatmap":
            entry["reveal_heatmap"] = bool(item.get("reveal_heatmap"))
        guide.append(entry)
    if not guide:
        raise FocusError("focus_bad_output", "The model did not write a discussion guide.")
    for index, entry in enumerate(guide, start=1):
        entry["id"] = f"q{index}"
    if brief.mode == "heatmap":
        _single_reveal(guide)

    plan: dict[str, Any] = {
        "title": _text(raw.get("title"), "title") or brief.topic[:40],
        "objective": _text(raw.get("objective"), "objective"),
        "participants": participants,
        "guide": guide,
    }
    if refs:
        described = {}
        for item in _items(raw.get("screens")):
            ref = str(item.get("ref", "")).strip().upper()
            if ref in known and ref not in described:
                described[ref] = {
                    "ref": ref,
                    "title": _text(item.get("title"), "title"),
                    "description": _text(item.get("description"), "description"),
                }
        plan["screens"] = [
            described.get(ref) or {"ref": ref, "title": "", "description": ""} for ref in refs
        ]
    if brief.mode == "heatmap":
        plan["attention"] = [
            {
                "element": _text(item.get("element"), "element"),
                "level": _choice(item.get("level"), ("high", "medium", "low"), "medium"),
                "note": _text(item.get("note"), "note"),
            }
            for item in _items(raw.get("attention"))[:8]
            if _text(item.get("element"), "element")
        ]
    return plan


def validate_plan(raw: Any, brief: Brief, material: Material) -> dict[str, Any]:
    """Check a plan the researcher edited in the browser before running it."""
    if not isinstance(raw, dict):
        raise FocusError("focus_invalid_plan", "The plan is missing or not an object.")
    people = _items(raw.get("participants"))
    questions = _items(raw.get("guide"))
    if not MIN_PARTICIPANTS <= len(people) <= MAX_PARTICIPANTS:
        raise FocusError(
            "focus_invalid_plan",
            f"A session needs {MIN_PARTICIPANTS}–{MAX_PARTICIPANTS} participants.",
            min=MIN_PARTICIPANTS,
            max=MAX_PARTICIPANTS,
        )
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise FocusError(
            "focus_invalid_plan", f"A session needs 1–{MAX_QUESTIONS} questions.", max=MAX_QUESTIONS
        )
    if any(not _text(person.get("name"), "name") for person in people):
        raise FocusError("focus_invalid_plan", "Every participant needs a name.")
    if any(not _text(item.get("question"), "question") for item in questions):
        raise FocusError("focus_invalid_plan", "Every question needs text.")
    sized = Brief(brief.lang, brief.topic, brief.context, brief.audience, len(people), brief.mode)
    return normalize_plan(raw, sized, material)


def _single_reveal(guide: list[dict[str, Any]]) -> None:
    flagged = [i for i, entry in enumerate(guide) if entry.get("reveal_heatmap")]
    keep = flagged[0] if flagged else min(1, len(guide) - 1)
    for i, entry in enumerate(guide):
        entry["reveal_heatmap"] = i == keep


# ── Discussion ───────────────────────────────────────────────────────────────


def schedule_order(participants: list[dict[str, Any]], round_index: int) -> list[str]:
    """Speaking order for a round: rotated so nobody always anchors the discussion."""
    ids = [person["id"] for person in participants]
    shift = round_index % len(ids)
    return ids[shift:] + ids[:shift]


class _Session:
    def __init__(
        self,
        llm: LLM,
        brief: Brief,
        plan: dict[str, Any],
        material: Material,
        turns: list[dict[str, Any]],
    ) -> None:
        self.llm = llm
        self.brief = brief
        self.lang = brief.lang
        self.plan = plan
        self.material = material
        self.turns = turns
        self.people = {person["id"]: person for person in plan["participants"]}
        self.system = _session_system(brief, plan, material)

    def name(self, speaker: str) -> str:
        if speaker == "moderator":
            return _t(self.lang, "moderator")
        return str(self.people[speaker]["name"])

    def shots_for(self, question: int) -> list[dict[str, Any]]:
        """The screenshots in front of the group for one question.

        A question that names screens shows those; otherwise every screen when there are
        only a few (their descriptions are always in the system prompt). Sending only
        what is being discussed keeps each request small.
        """
        shots = self.material.shots
        if not shots:
            return []
        guide = self.plan["guide"]
        named = guide[question].get("screens") or [] if question < len(guide) else []
        if self.brief.mode == "heatmap" or (not named and len(shots) <= _ALL_SHOTS_UP_TO):
            named = [shot.ref for shot in shots]
        return _shot_parts(self.lang, self.plan, [s for s in shots if s.ref in named])

    def messages(
        self, instruction: str, question: int, *, show_overlay: bool = False
    ) -> list[dict[str, Any]]:
        """Shared system prompt, screenshots, transcript, then this speaker's instruction.

        Within a question everything before the instruction is identical from one call
        to the next apart from the newest turn, so DeepSeek's prefix cache serves most of
        each prompt. The heatmap appears in the transcript right after the turn that
        showed it; `show_overlay` adds it for the moderator who is about to show it.
        """
        before, after = transcript_segments(self.lang, self.turns, self.name)
        head = _t(self.lang, "transcript.head")
        tail = "---\n" + instruction
        shot_parts = self.shots_for(question)
        if not shot_parts and not self.material.overlay:
            body = before or _t(self.lang, "transcript.empty")
            content: Any = f"{head}\n\n{body}\n\n{tail}"
        else:
            parts = shot_parts
            parts.append(
                {"type": "text", "text": f"{head}\n\n{before or _t(self.lang, 'transcript.empty')}"}
            )
            if self.material.overlay and (after is not None or show_overlay):
                parts.append({"type": "text", "text": _t(self.lang, "heatmap.label")})
                parts.append(_image_part(self.material.overlay))
                if after:
                    parts.append({"type": "text", "text": after})
            parts.append({"type": "text", "text": tail})
            content = parts
        return [{"role": "system", "content": self.system}, {"role": "user", "content": content}]

    async def speak(
        self,
        turn: dict[str, Any],
        instruction: str,
        *,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[Event]:
        """Stream one turn: announce it, pass text through as it arrives, then settle it."""
        yield "turn", {**turn, "text": ""}
        own = self.name(turn["speaker"])
        others = [self.name(s) for s in ("moderator", *self.people) if self.name(s) != own]
        messages = self.messages(
            instruction, turn["question"], show_overlay=turn.get("image") == "heatmap"
        )
        text = ""
        for _ in range(2):  # an empty answer gets one more try
            cleaner = TurnCleaner(own, others)
            async with contextlib.aclosing(
                self.llm.stream(messages, temperature=temperature, max_tokens=max_tokens)
            ) as chunks:
                async for delta in chunks:
                    out = cleaner.feed(delta)
                    if out:
                        yield "delta", {"slot": turn["slot"], "text": out}
                    if cleaner.stopped:
                        break
            rest = cleaner.finish()
            if rest:
                yield "delta", {"slot": turn["slot"], "text": rest}
            text = cleaner.text
            if text:
                break
        settled = {**turn, "text": text or _t(self.lang, "silent")}
        self.turns.append(settled)
        yield "turn_end", settled

    async def decide_probe(self, index: int) -> dict[str, Any]:
        roster = ", ".join(f"{pid}={person['name']}" for pid, person in self.people.items())
        instruction = _t(self.lang, "probe", number=index + 1, roster=roster)
        try:
            raw = await _json_call(
                self.llm,
                self.messages(instruction, index),
                self.lang,
                thinking=False,
                temperature=_PROBE_TEMPERATURE,
                max_tokens=_MODERATOR_MAX_TOKENS,
            )
        except FocusError as exc:
            if exc.code != "focus_bad_output":
                raise
            return {"probe": False}
        targets = [t for t in _refs(raw.get("targets"), set(self.people), upper=False)][:2]
        question = _text(raw.get("question"), "question")
        if not raw.get("probe") or not targets or not question:
            return {"probe": False}
        return {"probe": True, "targets": targets, "question": question}


async def discuss_session(
    llm: LLM,
    brief: Brief,
    plan: dict[str, Any],
    material: Material,
    transcript: list[dict[str, Any]] | None = None,
) -> AsyncIterator[Event]:
    """Run (or resume) the discussion, streaming every turn.

    `transcript` holds the turns already completed, in order; the session continues
    after the last of them. Each turn has a `slot` naming its place in the schedule:
    q1.ask, q1.p2, q1.probe, q1.probe.p4, … close.ask, close.p1.
    """
    session = _Session(llm, brief, plan, material, [])
    lang = brief.lang
    guide = plan["guide"]
    people = plan["participants"]
    pending = iter(list(transcript or []))
    upcoming = next(pending, None)

    def replay(slot: str) -> bool:
        """True when `slot` was already completed; the turn then rejoins the session."""
        nonlocal upcoming
        if upcoming is None:
            return False
        if upcoming.get("slot") != slot:
            raise FocusError(
                "focus_invalid_transcript",
                f"The transcript does not follow the session order at {upcoming.get('slot')!r}.",
            )
        session.turns.append(upcoming)
        upcoming = next(pending, None)
        return True

    def answer(
        slot: str, pid: str, index: int, situation: str, **extra: Any
    ) -> AsyncIterator[Event]:
        turn = {"slot": slot, "kind": "answer", "speaker": pid, "question": index, **extra}
        return session.speak(
            turn,
            _turn_instruction(lang, session.people[pid], situation),
            temperature=_TURN_TEMPERATURE,
            max_tokens=_TURN_MAX_TOKENS,
        )

    def moderate(turn: dict[str, Any], instruction: str) -> AsyncIterator[Event]:
        return session.speak(
            turn,
            instruction,
            temperature=_MODERATOR_TEMPERATURE,
            max_tokens=_MODERATOR_MAX_TOKENS,
        )

    reveal = next((i for i, item in enumerate(guide) if item.get("reveal_heatmap")), None)
    for index, item in enumerate(guide):
        number = index + 1
        question = item["question"]

        if not replay(f"q{number}.ask"):
            situation = _t(lang, "ask.first" if index == 0 else "ask.next", question=question)
            if item.get("screens"):
                situation += "\n" + _t(lang, "ask.screens", refs=_join(lang, item["screens"]))
            turn: dict[str, Any] = {
                "slot": f"q{number}.ask",
                "kind": "ask",
                "speaker": "moderator",
                "question": index,
            }
            if index == reveal and material.overlay:
                notes = _attention_notes(lang, plan) or attention_text(lang, material.attention)
                situation = _t(lang, "ask.reveal", attention=notes) + "\n" + situation
                turn["image"] = "heatmap"
            async for event in moderate(turn, _t(lang, "ask", situation=situation)):
                yield event

        for position, pid in enumerate(schedule_order(people, index)):
            if replay(f"q{number}.{pid}"):
                continue
            key = "turn.first" if position == 0 else "turn.answer"
            async for event in answer(
                f"q{number}.{pid}", pid, index, _t(lang, key, question=question)
            ):
                yield event

        probe_slot = f"q{number}.probe"
        if upcoming is not None and upcoming.get("slot") == probe_slot:
            targets = list(upcoming.get("targets") or [])
            replay(probe_slot)
            probe = session.turns[-1]["text"]
        elif upcoming is not None:
            targets, probe = [], ""  # the session already moved on without a probe
        else:
            decision = await session.decide_probe(index)
            targets, probe = decision.get("targets", []), decision.get("question", "")
            if targets:
                turn = {
                    "slot": probe_slot,
                    "kind": "probe",
                    "speaker": "moderator",
                    "question": index,
                    "targets": targets,
                }
                async for event in moderate(turn, _t(lang, "ask", situation=probe)):
                    yield event
                probe = session.turns[-1]["text"]
        for pid in targets:
            slot = f"q{number}.probe.{pid}"
            if replay(slot):
                continue
            async for event in answer(
                slot, pid, index, _t(lang, "turn.probe", question=probe), probe=True
            ):
                yield event
        yield "usage", _usage(llm, llm.settings.model)

    closing = len(guide)
    if not replay("close.ask"):
        ask = _t(lang, "close.ask.ab" if brief.mode == "ab" else "close.ask")
        turn = {"slot": "close.ask", "kind": "close", "speaker": "moderator", "question": closing}
        async for event in moderate(turn, _t(lang, "close", ask=ask)):
            yield event
    close_question = next(t["text"] for t in session.turns if t["slot"] == "close.ask")
    final_key = "turn.final.ab" if brief.mode == "ab" else "turn.final"
    for pid in schedule_order(people, closing):
        slot = f"close.{pid}"
        if replay(slot):
            continue
        turn = {"slot": slot, "kind": "final", "speaker": pid, "question": closing}
        async for event in session.speak(
            turn,
            _turn_instruction(
                lang, session.people[pid], _t(lang, final_key, question=close_question)
            ),
            temperature=_TURN_TEMPERATURE,
            max_tokens=_TURN_MAX_TOKENS,
        ):
            yield event
    if upcoming is not None:
        raise FocusError(
            "focus_invalid_transcript", "The transcript has turns after the closing round."
        )
    yield "usage", _usage(llm, llm.settings.model)


def _usage(llm: LLM, model: str) -> dict[str, Any]:
    """Token use so far in this request (cumulative), and the model that ran it."""
    return {**llm.usage.as_dict(), "model": model}


def _join(lang: Lang, refs: list[str]) -> str:
    return ("、" if lang == "zh" else ", ").join(refs)


def _attention_notes(lang: Lang, plan: dict[str, Any]) -> str:
    return "\n".join(
        _t(
            lang,
            "attention.item",
            element=note["element"],
            level=_t(lang, f"level.{note['level']}"),
            note=note["note"],
        )
        for note in plan.get("attention") or []
    )


def _turn_instruction(lang: Lang, person: dict[str, Any], situation: str) -> str:
    return _t(
        lang,
        "turn",
        name=person["name"],
        summary=person.get("summary") or "",
        profile=person.get("profile") or "",
        voice=person.get("voice") or _t(lang, "unset"),
        stance=_t(lang, f"stance.{person.get('stance', 'neutral')}"),
        situation=situation,
    )


def _session_system(brief: Brief, plan: dict[str, Any], material: Material) -> str:
    lang = brief.lang
    return _t(
        lang,
        "session.system",
        topic=brief.topic,
        objective=plan.get("objective") or _t(lang, "unset"),
        context=brief.context or _t(lang, "none"),
        audience=brief.audience or _t(lang, "unset"),
        material=material_text(lang, brief.mode, plan),
        roster=_roster(lang, plan, full=False),
    )


def material_text(lang: Lang, mode: Mode, plan: dict[str, Any]) -> str:
    screens = plan.get("screens") or []

    def lines(items: list[dict[str, Any]]) -> str:
        sep = "：" if lang == "zh" else ": "
        return "\n".join(
            f"{s['ref']}. {s.get('title') or ''}{sep if s.get('description') else ''}{s.get('description') or ''}"
            for s in items
        )

    if mode == "flow":
        return _t(lang, "session.material.flow", n=len(screens), screens=lines(screens))
    if mode == "ab":
        return _t(
            lang,
            "session.material.ab",
            screens_a=lines([s for s in screens if s["ref"].startswith("A")]),
            screens_b=lines([s for s in screens if s["ref"].startswith("B")]),
        )
    if mode == "heatmap":
        return _t(lang, "session.material.heatmap", screens=lines(screens))
    return _t(lang, "session.material.text")


def _roster(lang: Lang, plan: dict[str, Any], *, full: bool) -> str:
    opening, closing, sep = ("（", "）", "：") if lang == "zh" else (" (", ")", ": ")
    rows = []
    for person in plan["participants"]:
        row = f"- {person['id']} · {person['name']}{opening}{person.get('summary') or ''}{closing}"
        if full:
            row += f"{sep}{person.get('profile') or ''}"
        rows.append(row)
    return "\n".join(rows)


def _shot_parts(lang: Lang, plan: dict[str, Any], shots: list[Shot]) -> list[dict[str, Any]]:
    if not shots:
        return []
    titles = {s["ref"]: s.get("title") or "" for s in plan.get("screens") or []}
    parts: list[dict[str, Any]] = [{"type": "text", "text": _t(lang, "shots.label")}]
    for shot in shots:
        label = _t(lang, "shot", ref=shot.ref, title=titles.get(shot.ref, "")).rstrip("：: ")
        parts.append({"type": "text", "text": label})
        parts.append(_image_part(shot.data_url))
    return parts


def transcript_segments(
    lang: Lang, turns: list[dict[str, Any]], name: Callable[[str], str]
) -> tuple[str, str | None]:
    """The transcript as text, split after the turn that shows the heatmap.

    Returns (before, after); `after` is None until the heatmap has been shown and an
    empty string right after it.
    """
    before: list[str] = []
    after: list[str] | None = None
    sep = "：" if lang == "zh" else ": "
    for turn in turns:
        line = f"{name(turn['speaker'])}{sep}{turn['text']}"
        if after is None:
            before.append(line)
            if turn.get("image") == "heatmap":
                before.append(_t(lang, "transcript.heatmap"))
                after = []
        else:
            after.append(line)
    return "\n\n".join(before), None if after is None else "\n\n".join(after)


def transcript_text(lang: Lang, turns: list[dict[str, Any]], name: Callable[[str], str]) -> str:
    before, after = transcript_segments(lang, turns, name)
    return "\n\n".join(part for part in (before, after) if part)


class TurnCleaner:
    """Cleans one speaker's streamed text.

    Drops a leading name tag or quotation mark, and stops the turn when the model
    starts writing a line for somebody else ("李四：…"). A short unfinished last line
    is held back until it can no longer become such a tag.
    """

    HOLD = 26
    _OPEN_QUOTES = '“"「『'
    _CLOSE_QUOTES = '”"」』'

    def __init__(self, own: str, others: list[str]) -> None:
        names = sorted({own, *others}, key=len, reverse=True)
        alternatives = "|".join(re.escape(n) for n in names if n)
        others_alt = "|".join(re.escape(n) for n in sorted(set(others), key=len, reverse=True) if n)
        self._head = re.compile(
            rf"^\s*(?:\*\*|【|\[)?\s*(?:{alternatives})\s*(?:\*\*|】|\])?\s*(?:[（(][^）)]{{0,24}}[）)])?\s*[：:]\s*"
        )
        self._foreign = (
            re.compile(rf"\n\s*(?:\*\*|【|\[)?\s*(?:{others_alt})\s*(?:\*\*|】|\])?\s*[：:]")
            if others_alt
            else None
        )
        self.raw = ""
        self.sent = 0
        self.started = False
        self.stopped = False
        self.quoted = False

    def feed(self, delta: str) -> str:
        if self.stopped:
            return ""
        self.raw += delta
        if not self.started:
            if len(self.raw) < self.HOLD and not re.search(r"[\n：:]", self.raw):
                return ""
            self._strip_head()
            if not self.raw:
                return ""
            self.started = True
        return self._release(final=False)

    def finish(self) -> str:
        if not self.started:
            self._strip_head()
            self.started = True
        return self._release(final=True)

    def _strip_head(self) -> None:
        text = self._head.sub("", self.raw, count=1).lstrip()
        stripped = text.lstrip(self._OPEN_QUOTES)
        self.quoted = self.quoted or len(stripped) < len(text)
        self.raw = stripped

    def _release(self, *, final: bool) -> str:
        if self._foreign is not None:
            match = self._foreign.search(self.raw)
            if match:
                self.raw = self.raw[: match.start()]
                self.stopped = True
        end = len(self.raw)
        if not final and not self.stopped:
            newline = self.raw.rfind("\n")
            if newline != -1 and end - newline - 1 < self.HOLD:
                end = newline
        # A closing quote at the very end may belong to the opening one already dropped.
        if self.quoted and self.raw[:end].rstrip()[-1:] in self._CLOSE_QUOTES:
            end = len(self.raw[:end].rstrip()) - 1
        chunk = self.raw[self.sent : end]
        self.sent = max(self.sent, end)
        return chunk.replace("**", "")

    @property
    def text(self) -> str:
        text = self.raw.replace("**", "").strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        if self.quoted and text[-1:] in self._CLOSE_QUOTES:
            text = text[:-1].rstrip()
        return text


# ── Report ───────────────────────────────────────────────────────────────────


async def report_session(
    llm: LLM,
    brief: Brief,
    plan: dict[str, Any],
    material: Material,
    transcript: list[dict[str, Any]],
) -> AsyncIterator[Event]:
    """Write the structured report from the transcript."""
    yield "status", {"phase": "report"}
    raw = await _json_call(
        llm, _report_messages(brief, plan, transcript), brief.lang, model=llm.settings.report_model
    )
    report = normalize_report(raw, brief, plan, transcript)
    yield "report", {"report": report}
    yield "usage", _usage(llm, llm.settings.report_model)


def _report_messages(
    brief: Brief, plan: dict[str, Any], transcript: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    lang, mode = brief.lang, brief.mode
    people = {p["id"]: p["name"] for p in plan["participants"]}

    def name(speaker: str) -> str:
        if speaker == "moderator":
            return _t(lang, "moderator")
        label = people.get(speaker, speaker)
        return f"{label}（{speaker}）" if lang == "zh" else f"{label} ({speaker})"

    material = material_text(lang, mode, plan)
    if mode == "heatmap" and plan.get("attention"):
        material += "\n" + _attention_notes(lang, plan)
    extra = mode if mode in ("flow", "ab", "heatmap") else None
    prompt = _t(
        lang,
        "report.user",
        topic=brief.topic,
        objective=plan.get("objective") or _t(lang, "unset"),
        context=brief.context or _t(lang, "none"),
        audience=brief.audience or _t(lang, "unset"),
        material=material,
        roster=_roster(lang, plan, full=True),
        transcript=transcript_text(lang, transcript, name),
        report_rules=_t(lang, f"report.rules.{extra}") if extra else "",
        extra_schema=_t(lang, f"report.schema.{extra}") if extra else "",
    )
    return [
        {"role": "system", "content": _t(lang, "report.system")},
        {"role": "user", "content": prompt},
    ]


def normalize_report(
    raw: dict[str, Any], brief: Brief, plan: dict[str, Any], transcript: list[dict[str, Any]]
) -> dict[str, Any]:
    """Keep the report to known participants and screens, and quotes to words really said."""
    ids = [p["id"] for p in plan["participants"]]
    known = set(ids)
    refs = {s["ref"] for s in plan.get("screens") or []}
    said: dict[str, str] = {}
    for turn in transcript:
        if turn.get("speaker") in known:
            said[turn["speaker"]] = said.get(turn["speaker"], "") + _squash(turn.get("text", ""))

    def who(value: Any) -> list[str]:
        return _refs(value, known, upper=False)

    def quote(item: dict[str, Any]) -> dict[str, str] | None:
        text = _clip(item.get("text"), 300)
        speaker = str(item.get("speaker", "")).strip()
        squashed = _squash(text)
        if len(squashed) < 4:
            return None
        if speaker in said and squashed in said[speaker]:
            return {"speaker": speaker, "text": text}
        for pid, words in said.items():  # right words, wrong speaker
            if squashed in words:
                return {"speaker": pid, "text": text}
        return None  # not something anyone said

    findings = []
    for item in _items(raw.get("findings"))[:8]:
        title = _clip(item.get("title"), 80)
        if not title:
            continue
        quotes = [q for q in (quote(x) for x in _items(item.get("quotes"))[:4]) if q]
        findings.append(
            {
                "title": title,
                "detail": _clip(item.get("detail"), 600),
                "evidence": _choice(
                    item.get("evidence"), ("strong", "moderate", "weak"), "moderate"
                ),
                "participants": who(item.get("participants")),
                "quotes": quotes,
            }
        )

    def points(key: str) -> list[dict[str, Any]]:
        return [
            {
                "title": _clip(item.get("title"), 80),
                "detail": _clip(item.get("detail"), 400),
                "participants": who(item.get("participants")),
            }
            for item in _items(raw.get(key))[:8]
            if _clip(item.get("title"), 80)
        ]

    stances = []
    seen: set[str] = set()
    for item in _items(raw.get("stances")):
        pid = str(item.get("participant", "")).strip()
        if pid in known and pid not in seen:
            seen.add(pid)
            stances.append(
                {
                    "participant": pid,
                    "stance": _choice(
                        item.get("stance"), ("positive", "neutral", "negative", "mixed"), "neutral"
                    ),
                    "summary": _clip(item.get("summary"), 240),
                }
            )
    stances.sort(key=lambda s: ids.index(s["participant"]))

    report: dict[str, Any] = {
        "headline": _clip(raw.get("headline"), 120),
        "summary": _clip(raw.get("summary"), 900),
        "findings": findings,
        "pain_points": points("pain_points"),
        "needs": points("needs"),
        "disagreements": [
            {
                "topic": _clip(item.get("topic"), 120),
                "sides": [
                    {
                        "participants": who(side.get("participants")),
                        "view": _clip(side.get("view"), 300),
                    }
                    for side in _items(item.get("sides"))[:4]
                    if _clip(side.get("view"), 300)
                ],
            }
            for item in _items(raw.get("disagreements"))[:5]
            if _clip(item.get("topic"), 120)
        ],
        "stances": stances,
        "opportunities": [
            {
                "title": _clip(item.get("title"), 80),
                "detail": _clip(item.get("detail"), 400),
                "priority": _choice(item.get("priority"), ("high", "medium", "low"), "medium"),
                "related": _numbers(item.get("related"), len(findings)),
            }
            for item in _items(raw.get("opportunities"))[:8]
            if _clip(item.get("title"), 80)
        ],
        "validate": [v for v in (_clip(x, 240) for x in _list(raw.get("validate"))[:6]) if v],
        "limitations": _clip(raw.get("limitations"), 700),
    }
    if brief.mode in ("flow", "ab"):
        report["flow_issues"] = [
            {
                "screens": _refs(item.get("screens"), refs),
                "issue": _clip(item.get("issue"), 300),
                "severity": _choice(item.get("severity"), ("high", "medium", "low"), "medium"),
                "suggestion": _clip(item.get("suggestion"), 300),
            }
            for item in _items(raw.get("flow_issues"))[:12]
            if _clip(item.get("issue"), 300)
        ]
    if brief.mode == "ab":
        ab = raw.get("ab") if isinstance(raw.get("ab"), dict) else {}
        placed: set[str] = set()
        groups: dict[str, list[str]] = {}
        for key in ("a", "b", "undecided"):
            groups[key] = [pid for pid in who(ab.get(key)) if pid not in placed]
            placed.update(groups[key])
        report["ab"] = {
            **groups,
            **{
                key: [v for v in (_clip(x, 240) for x in _list(ab.get(key))[:6]) if v]
                for key in ("a_strengths", "a_issues", "b_strengths", "b_issues")
            },
            "verdict": _clip(ab.get("verdict"), 600),
        }
    if brief.mode == "heatmap":
        report["attention"] = [
            {
                "element": _clip(item.get("element"), 80),
                "predicted": _clip(item.get("predicted"), 240),
                "feedback": _clip(item.get("feedback"), 300),
                "match": _choice(item.get("match"), ("aligned", "conflict", "partial"), "partial"),
            }
            for item in _items(raw.get("attention"))[:8]
            if _clip(item.get("element"), 80)
        ]
    if not report["headline"] and not findings:
        raise FocusError("focus_bad_output", "The model did not write a usable report.")
    return report


# ── Transcript validation ────────────────────────────────────────────────────


def validate_transcript(raw: Any, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Turns sent back by the browser: shape and speakers only; order is checked on resume."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_TURNS:
        raise FocusError("focus_invalid_transcript", "The transcript must be a list of turns.")
    speakers = {"moderator", *(p["id"] for p in plan["participants"])}
    turns = []
    for item in raw:
        if not isinstance(item, dict):
            raise FocusError("focus_invalid_transcript", "Every turn must be an object.")
        speaker = str(item.get("speaker", ""))
        kind = str(item.get("kind", ""))
        text = item.get("text")
        if speaker not in speakers or kind not in ("ask", "answer", "probe", "close", "final"):
            raise FocusError("focus_invalid_transcript", "A turn has an unknown speaker or kind.")
        if not isinstance(text, str) or not text.strip():
            raise FocusError("focus_invalid_transcript", "Every turn needs text.")
        turn: dict[str, Any] = {
            "slot": str(item.get("slot", ""))[:40],
            "kind": kind,
            "speaker": speaker,
            "question": int(item["question"]) if str(item.get("question", "")).isdigit() else 0,
            "text": text.strip()[:_TURN_TEXT_MAX],
        }
        if item.get("image") == "heatmap":
            turn["image"] = "heatmap"
        if kind == "probe":
            turn["targets"] = _refs(item.get("targets"), speakers - {"moderator"}, upper=False)[:2]
        if item.get("probe"):
            turn["probe"] = True
        turns.append(turn)
    return turns


# ── Heatmap summary ──────────────────────────────────────────────────────────


def attention_text(lang: Lang, attention: dict[str, Any] | None) -> str:
    """HotGaze's numbers in words the planner and moderator can use."""
    if not attention:
        return _t(lang, "none")
    lines = []
    grid = attention.get("grid") or []
    if len(grid) == 9:
        labels = _t(lang, "attention.rows").split("|")
        rows = [
            _t(
                lang,
                "attention.row",
                label=labels[r],
                cells=" · ".join(_pct(v) for v in grid[r * 3 : r * 3 + 3]),
            )
            for r in range(3)
        ]
        lines.append(_t(lang, "attention.grid", rows="\n".join(rows)))
    focal = attention.get("focal") or []
    if focal:
        points = "  ".join(
            f"{i}. ({_pct(p['x'])}, {_pct(p['y'])})" for i, p in enumerate(focal[:5], start=1)
        )
        lines.append(_t(lang, "attention.focal", points=points))
    for region in attention.get("regions") or []:
        lines.append(
            _t(
                lang,
                "attention.region",
                name=region["name"],
                share=_pct(region["share"]),
                area=_pct(region["area"]),
            )
        )
    return "\n".join(lines) or _t(lang, "none")


def _pct(value: float) -> str:
    return f"{round(float(value) * 100)}%"


# ── Streaming plumbing ───────────────────────────────────────────────────────


async def event_stream(
    events: AsyncIterator[Event],
    *,
    on_close: Callable[[], Awaitable[None]] | None = None,
    ping_seconds: float = PING_SECONDS,
) -> AsyncIterator[str]:
    """Server-sent events, with a comment every few seconds while the model thinks.

    Proxies in front of the app close connections that stay silent for a couple of
    minutes; the pings keep the stream open. Errors become an `error` event, and a
    client that disconnects cancels the work (and the DeepSeek calls) at once.
    """
    queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=256)

    async def pump() -> None:
        try:
            async for event in events:
                await queue.put(event)
            await queue.put(("done", {}))
        except FocusError as exc:
            await queue.put(("error", exc.payload()))
        except Exception as exc:  # noqa: BLE001 - reported to the browser, not swallowed
            await queue.put(
                ("error", FocusError("focus_llm_failed", f"Unexpected error: {exc!s}").payload())
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=ping_seconds)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            name, data = item
            yield f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        if on_close is not None:
            with contextlib.suppress(Exception):
                await on_close()


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _json_call(
    llm: LLM,
    messages: list[dict[str, Any]],
    lang: Lang,
    *,
    model: str | None = None,
    thinking: bool = True,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """One JSON answer; asks once more if the first is not valid JSON."""
    attempt_messages = messages
    for attempt in range(2):
        text = ""
        async with contextlib.aclosing(
            llm.stream(
                attempt_messages,
                model=model,
                thinking=thinking,
                json_mode=True,
                temperature=temperature,
                max_tokens=max_tokens or (_THINKING_MAX_TOKENS if thinking else None),
            )
        ) as chunks:
            async for delta in chunks:
                text += delta
        parsed = parse_json_object(text)
        if parsed is not None:
            return parsed
        if attempt == 0:
            attempt_messages = [
                *messages,
                {"role": "assistant", "content": text.strip()[:2000] or "{}"},
                {"role": "user", "content": _t(lang, "json.retry")},
            ]
    raise FocusError("focus_bad_output", "The model did not return valid JSON.")


def parse_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1]):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _image_part(data_url: str, detail: str = "auto") -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": data_url, "detail": detail}}


def _items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, field_name: str) -> str:
    return _clip(value, _FIELD_MAX[field_name])


def _clip(value: Any, limit: int) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = re.sub(r"[ \t]+", " ", str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _stance(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in STANCES:
        return text
    for stance, words in _STANCE_WORDS.items():
        if any(word in text for word in words):
            return stance
    return "neutral"


def _refs(value: Any, known: set[str], *, upper: bool = True) -> list[str]:
    out: list[str] = []
    for item in _list(value):
        ref = str(item).strip()
        ref = ref.upper() if upper else ref
        if ref in known and ref not in out:
            out.append(ref)
    return out


def _numbers(value: Any, high: int) -> list[int]:
    """Distinct 1-based indexes up to `high`, sorted."""
    found = set()
    for item in _list(value):
        text = str(item).strip()
        if text.isdigit() and 1 <= int(text) <= high:
            found.add(int(text))
    return sorted(found)


def _squash(text: str) -> str:
    """Letters and digits only, lower case: how quotes are matched against the transcript."""
    return "".join(ch.lower() for ch in unicodedata.normalize("NFKC", str(text)) if ch.isalnum())
