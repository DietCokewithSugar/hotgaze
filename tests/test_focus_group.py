"""Tests for the AI focus group: web/focus_group.py and its endpoints in web/app.py.

DeepSeek is never called: a scripted model answers by role, and the HTTP client is
exercised against httpx's mock transport. Skipped unless the web dependencies are
installed: pip install -r web/requirements.txt httpx2
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import re
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from PIL import Image

_WEB = Path(__file__).parent.parent / "web"

PLAN = {
    "title": "理财痛点",
    "objective": "了解普通人理财时最头疼的环节。",
    "participants": [
        {
            "id": "x9",
            "name": "王丽",
            "summary": "34 岁 · 杭州 · 互联网运营",
            "profile": "2021 年追高买了白酒基金，亏了三成。",
            "voice": "说话直，爱吐槽。",
            "stance": "怀疑",
        },
        {
            "name": "陈子豪",
            "summary": "26 岁 · 深圳 · 程序员",
            "profile": "每月定投指数基金。",
            "voice": "爱讲数据。",
            "stance": "supportive",
        },
        {
            "name": "刘建国",
            "summary": "52 岁 · 济南 · 国企中层",
            "profile": "只买大额存单和国债。",
            "voice": "慢条斯理。",
            "stance": "neutral",
        },
    ],
    "guide": [
        {"question": "大家平时怎么打理闲钱？", "purpose": "热身", "screens": ["1", "9"]},
        {"question": "理财最让你头疼的是什么？", "purpose": "痛点", "reveal_heatmap": True},
    ],
    "screens": [
        {"ref": "1", "title": "首页", "description": "基金推荐列表。"},
        {"ref": "2", "title": "详情", "description": "收益曲线和买入按钮。"},
    ],
    "attention": [{"element": "买入按钮", "level": "low", "note": "几乎没有被注意到。"}],
}

REPORT = {
    "headline": "大家缺的不是产品，是看得懂和信得过。",
    "summary": "多数人被术语劝退。",
    "findings": [
        {
            "title": "看不懂比亏钱更劝退",
            "detail": "详情页术语太多。",
            "evidence": "strong",
            "participants": ["p1", "p2", "p7"],
            "quotes": [
                {"speaker": "p1", "text": "王丽的发言"},
                {"speaker": "p3", "text": "陈子豪的发言"},  # right words, wrong speaker
                {"speaker": "p2", "text": "这句话没人说过"},
            ],
        }
    ],
    "stances": [
        {"participant": "p3", "stance": "negative", "summary": "还是存款踏实。"},
        {"participant": "p1", "stance": "mixed", "summary": "想学但怕亏。"},
        {"participant": "p1", "stance": "positive", "summary": "重复的条目"},
    ],
    "opportunities": [{"title": "把术语翻译成人话", "priority": "high", "related": [1, "1", 5]}],
    "flow_issues": [{"screens": ["2", "7"], "issue": "按钮不显眼", "severity": "high"}],
    "ab": {"a": ["p1", "p2"], "b": ["p2", "p3"], "a_strengths": ["清楚"]},
    "attention": [{"element": "买入按钮", "match": "conflict", "feedback": "没看到"}],
    "validate": ["真实用户是否同样看不懂？"],
    "limitations": "AI 扮演的参与者。",
}


@pytest.fixture(scope="module")
def fg() -> ModuleType:
    pytest.importorskip("httpx")
    if str(_WEB) not in sys.path:
        sys.path.insert(0, str(_WEB))
    import focus_group

    return focus_group


@pytest.fixture(scope="module")
def web(fg: ModuleType) -> ModuleType:
    pytest.importorskip("fastapi")
    pytest.importorskip("python_multipart")
    if importlib.util.find_spec("httpx2") is None:  # older Starlette test clients use httpx
        pytest.importorskip("httpx")
    spec = importlib.util.spec_from_file_location("hotgaze_web_app_focus", _WEB / "app.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Scripted:
    """Answers like DeepSeek would, by role: plan, report, probe decision or a turn."""

    def __init__(
        self,
        fg: ModuleType,
        plan: dict[str, Any] | None = None,
        report: dict[str, Any] | None = None,
        probe: dict[str, Any] | None = None,
        say: Callable[[str], str] | None = None,
    ) -> None:
        self.fg = fg
        self.settings = fg.Settings("test-key", model="m-chat", report_model="m-report")
        self.usage = fg.Usage()
        self.plan = PLAN if plan is None else plan
        self.report = REPORT if report is None else report
        self.probe = probe if probe is not None else {"probe": False}
        self.say = say or (lambda speaker: f"{speaker}的发言")
        self.requests: list[dict[str, Any]] = []
        self.closed = False

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
        self.requests.append(
            {"messages": messages, "model": model, "thinking": thinking, "json_mode": json_mode}
        )
        self.usage.calls += 1
        system = messages[0]["content"]
        fg = self.fg
        if system in (fg._t("zh", "plan.system"), fg._t("en", "plan.system")):
            text = json.dumps(self.plan, ensure_ascii=False)
        elif system in (fg._t("zh", "report.system"), fg._t("en", "report.system")):
            text = json.dumps(self.report, ensure_ascii=False)
        elif json_mode:
            text = json.dumps(self.probe, ensure_ascii=False)
        else:
            text = self.say(_speaker(messages))
        for start in range(0, len(text), 4):
            yield text[start : start + 4]

    async def aclose(self) -> None:
        self.closed = True


def _instruction(messages: list[dict[str, Any]]) -> str:
    content = messages[-1]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(part["text"] for part in content if part["type"] == "text")


def _speaker(messages: list[dict[str, Any]]) -> str:
    text = _instruction(messages)
    match = re.search(r"现在轮到「(.+?)」发言|It is now (.+?)'s turn", text)
    return (match.group(1) or match.group(2)) if match else "主持人"


def _images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    content = messages[-1]["content"]
    return [] if isinstance(content, str) else [p for p in content if p["type"] == "image_url"]


async def _collect(events: AsyncIterator[tuple[str, dict[str, Any]]]) -> list[tuple[str, Any]]:
    return [event async for event in events]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _png(size: tuple[int, int] = (32, 24), color: tuple[int, int, int] = (40, 90, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _brief(fg: ModuleType, mode: str = "text", lang: str = "zh", count: int = 3) -> Any:
    return fg.Brief(lang, "理财用户的痛点", "", "", count, mode)


def _material(fg: ModuleType, mode: str = "text", count: int = 0) -> Any:
    refs = fg.shot_refs(mode, count)
    return fg.Material(mode, [fg.Shot(ref, f"data:image/png;base64,{ref}") for ref in refs])


def _plan(fg: ModuleType, mode: str = "text", material: Any = None) -> dict[str, Any]:
    material = material or _material(fg, mode, 2 if mode == "flow" else 0)
    plan: dict[str, Any] = fg.normalize_plan(PLAN, _brief(fg, mode), material)
    return plan


# ── Engine ───────────────────────────────────────────────────────────────────


class TestPlan:
    def test_normalizes_ids_stances_and_refs(self, fg: ModuleType) -> None:
        plan = _plan(fg, "flow")
        assert [p["id"] for p in plan["participants"]] == ["p1", "p2", "p3"]
        assert [p["stance"] for p in plan["participants"]] == ["skeptical", "supportive", "neutral"]
        assert [q["id"] for q in plan["guide"]] == ["q1", "q2"]
        assert plan["guide"][0]["screens"] == ["1"]  # unknown screen 9 dropped
        assert "reveal_heatmap" not in plan["guide"][1]  # not heatmap mode
        assert [s["ref"] for s in plan["screens"]] == ["1", "2"]

    def test_heatmap_mode_keeps_exactly_one_reveal(self, fg: ModuleType) -> None:
        raw = json.loads(json.dumps(PLAN))
        for item in raw["guide"]:
            item["reveal_heatmap"] = False
        plan = fg.normalize_plan(raw, _brief(fg, "heatmap"), _material(fg, "heatmap", 1))
        assert [q["reveal_heatmap"] for q in plan["guide"]] == [False, True]
        assert plan["attention"][0]["element"] == "买入按钮"
        assert plan["screens"] == [{"ref": "S", "title": "", "description": ""}]

    def test_too_few_participants_is_bad_output(self, fg: ModuleType) -> None:
        raw = {**PLAN, "participants": PLAN["participants"][:2]}
        with pytest.raises(fg.FocusError) as caught:
            fg.normalize_plan(raw, _brief(fg), _material(fg))
        assert caught.value.code == "focus_bad_output"

    def test_extra_participants_are_trimmed_to_the_brief(self, fg: ModuleType) -> None:
        raw = {**PLAN, "participants": PLAN["participants"] * 3}
        plan = fg.normalize_plan(raw, _brief(fg, count=4), _material(fg))
        assert len(plan["participants"]) == 4
        assert len({p["name"] for p in plan["participants"]}) == 4  # duplicate names made unique

    def test_edited_plan_must_have_names_and_questions(self, fg: ModuleType) -> None:
        brief, material = _brief(fg), _material(fg)
        edited = json.loads(json.dumps(_plan(fg)))
        edited["guide"][0]["question"] = "  "
        with pytest.raises(fg.FocusError) as caught:
            fg.validate_plan(edited, brief, material)
        assert caught.value.code == "focus_invalid_plan"
        edited = json.loads(json.dumps(_plan(fg)))
        edited["participants"] = edited["participants"][:2]
        with pytest.raises(fg.FocusError):
            fg.validate_plan(edited, brief, material)

    def test_plan_session_asks_for_json_with_thinking(self, fg: ModuleType) -> None:
        llm = Scripted(fg)
        events = _run(_collect(fg.plan_session(llm, _brief(fg), _material(fg))))
        assert [name for name, _ in events] == ["status", "plan", "usage"]
        request = llm.requests[0]
        assert request["thinking"] and request["json_mode"] and request["model"] == "m-chat"
        assert "理财用户的痛点" in _instruction(request["messages"])

    def test_plan_with_screenshots_sends_them_in_high_detail(self, fg: ModuleType) -> None:
        llm = Scripted(fg)
        _run(_collect(fg.plan_session(llm, _brief(fg, "flow"), _material(fg, "flow", 2))))
        images = _images(llm.requests[0]["messages"])
        assert len(images) == 2
        assert all(part["image_url"]["detail"] == "high" for part in images)

    def test_invalid_json_is_retried_once(self, fg: ModuleType) -> None:
        class Broken(Scripted):
            async def stream(self, messages: list[dict[str, Any]], **kw: Any) -> AsyncIterator[str]:
                self.requests.append({"messages": messages, **kw})
                yield "not json" if len(self.requests) == 1 else json.dumps(PLAN)

        llm = Broken(fg)
        events = _run(_collect(fg.plan_session(llm, _brief(fg), _material(fg))))
        assert "plan" in [name for name, _ in events]
        assert len(llm.requests) == 2
        assert llm.requests[1]["messages"][-1]["content"] == fg._t("zh", "json.retry")


class TestDiscussion:
    def test_order_probes_and_closing(self, fg: ModuleType) -> None:
        llm = Scripted(fg, probe={"probe": True, "targets": ["p2", "p8"], "question": "具体说说？"})
        plan = _plan(fg)
        events = _run(_collect(fg.discuss_session(llm, _brief(fg), plan, _material(fg))))
        turns = [data for name, data in events if name == "turn_end"]
        assert [t["slot"] for t in turns] == [
            "q1.ask", "q1.p1", "q1.p2", "q1.p3", "q1.probe", "q1.probe.p2",
            "q2.ask", "q2.p2", "q2.p3", "q2.p1", "q2.probe", "q2.probe.p2",
            "close.ask", "close.p3", "close.p1", "close.p2",
        ]  # fmt: skip
        assert turns[4]["targets"] == ["p2"]  # unknown target dropped
        assert turns[1]["text"] == "王丽的发言"
        assert events[-1][0] == "usage"
        turn_requests = [r for r in llm.requests if not r["json_mode"]]
        assert all(not r["thinking"] for r in llm.requests)
        # Every speaker sees the shared system prompt and the transcript so far.
        assert len({r["messages"][0]["content"] for r in turn_requests}) == 1
        last = _instruction(turn_requests[-1]["messages"])
        assert "陈子豪：陈子豪的发言" in last and "现在轮到「陈子豪」发言" in last

    def test_streams_deltas_that_add_up_to_the_turn(self, fg: ModuleType) -> None:
        llm = Scripted(fg, say=lambda who: f"{who}：“我说个实话，这事挺难的。”\n刘建国：我插一句")
        events = _run(_collect(fg.discuss_session(llm, _brief(fg), _plan(fg), _material(fg))))
        streamed = "".join(d["text"] for n, d in events if n == "delta" and d["slot"] == "q1.p1")
        settled = next(d for n, d in events if n == "turn_end" and d["slot"] == "q1.p1")
        assert settled["text"] == streamed == "我说个实话，这事挺难的。"

    def test_resumes_after_the_last_completed_turn(self, fg: ModuleType) -> None:
        llm = Scripted(fg, probe={"probe": True, "targets": ["p3"], "question": "为什么？"})
        brief, plan, material = _brief(fg), _plan(fg), _material(fg)
        full = _run(_collect(fg.discuss_session(llm, brief, plan, material)))
        turns = [d for n, d in full if n == "turn_end"]
        for cut in (1, 4, 5, 6, len(turns) - 1):
            again = _run(_collect(fg.discuss_session(llm, brief, plan, material, turns[:cut])))
            rest = [d["slot"] for n, d in again if n == "turn_end"]
            assert [t["slot"] for t in turns[:cut]] + rest == [t["slot"] for t in turns], cut

    def test_a_skipped_probe_is_not_asked_again_on_resume(self, fg: ModuleType) -> None:
        llm = Scripted(fg)  # never probes
        brief, plan, material = _brief(fg), _plan(fg), _material(fg)
        turns = [
            d for n, d in _run(_collect(fg.discuss_session(llm, brief, plan, material)))
            if n == "turn_end"
        ]  # fmt: skip
        assert "q1.probe" not in [t["slot"] for t in turns]
        llm.requests.clear()
        _run(_collect(fg.discuss_session(llm, brief, plan, material, turns[:6])))
        assert sum(r["json_mode"] for r in llm.requests) == 1  # only question 2's decision

    def test_rejects_a_transcript_out_of_order(self, fg: ModuleType) -> None:
        llm = Scripted(fg)
        brief, plan, material = _brief(fg), _plan(fg), _material(fg)
        turns = [
            d for n, d in _run(_collect(fg.discuss_session(llm, brief, plan, material)))
            if n == "turn_end"
        ]  # fmt: skip
        with pytest.raises(fg.FocusError) as caught:
            _run(_collect(fg.discuss_session(llm, brief, plan, material, turns[1:3])))
        assert caught.value.code == "focus_invalid_transcript"

    def test_each_question_shows_its_own_screens(self, fg: ModuleType) -> None:
        material = _material(fg, "flow", 8)
        raw = json.loads(json.dumps(PLAN))
        raw["guide"][0]["screens"] = ["2", "3"]
        raw["guide"][1]["screens"] = []
        plan = fg.normalize_plan(raw, _brief(fg, "flow"), material)
        llm = Scripted(fg)
        _run(_collect(fg.discuss_session(llm, _brief(fg, "flow"), plan, material)))
        q1 = [r for r in llm.requests if "大家平时怎么打理闲钱" in _instruction(r["messages"])]
        assert {len(_images(r["messages"])) for r in q1} == {2}
        # Question 2 names no screens and the flow has 8 (> 6): descriptions only.
        q2 = [r for r in llm.requests if "最让你头疼" in _instruction(r["messages"])]
        assert q2 and {len(_images(r["messages"])) for r in q2} == {0}
        assert "2. 详情：收益曲线和买入按钮。" in llm.requests[0]["messages"][0]["content"]

    def test_heatmap_is_revealed_at_its_question_and_stays_visible(self, fg: ModuleType) -> None:
        material = _material(fg, "heatmap", 1)
        material.overlay = "data:image/jpeg;base64,OVERLAY"
        brief = _brief(fg, "heatmap")
        plan = fg.normalize_plan(PLAN, brief, material)
        llm = Scripted(fg)
        events = _run(_collect(fg.discuss_session(llm, brief, plan, material)))
        reveal = next(d for n, d in events if n == "turn_end" and d.get("image") == "heatmap")
        assert reveal["slot"] == "q2.ask"
        overlay_seen = [
            any(p["image_url"]["url"].endswith("OVERLAY") for p in _images(r["messages"]))
            for r in llm.requests
        ]
        first = overlay_seen.index(True)
        assert "买入按钮" in _instruction(llm.requests[first]["messages"])  # the moderator's notes
        assert all(overlay_seen[first:]) and not any(overlay_seen[:first])

    def test_ab_closing_asks_for_a_preference(self, fg: ModuleType) -> None:
        llm = Scripted(fg)
        material = fg.Material("ab", [fg.Shot("A1", "a"), fg.Shot("B1", "b")])
        plan = fg.normalize_plan(PLAN, _brief(fg, "ab"), material)
        _run(_collect(fg.discuss_session(llm, _brief(fg, "ab"), plan, material)))
        finals = [r for r in llm.requests if "最后的表态" in _instruction(r["messages"])]
        assert finals and all("A 版还是 B 版" in _instruction(r["messages"]) for r in finals)

    def test_english_session_uses_english_prompts(self, fg: ModuleType) -> None:
        llm = Scripted(fg)
        brief = _brief(fg, lang="en")
        plan = fg.normalize_plan(PLAN, brief, _material(fg))
        _run(_collect(fg.discuss_session(llm, brief, plan, _material(fg))))
        assert llm.requests[0]["messages"][0]["content"].startswith("You are taking part")
        assert "It is now 王丽's turn" in _instruction(llm.requests[1]["messages"])


class TestTurnCleaner:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("王丽：说实话我不信。", "说实话我不信。"),
            ("**王丽**：说实话我不信。", "说实话我不信。"),
            ("【王丽】：说实话", "说实话"),
            ("王丽（34岁）：说实话", "说实话"),
            ("“说实话我不信。”", "说实话我不信。"),
            ("说实话：我不信。", "说实话：我不信。"),
            ("第一句。\n\n\n\n第二句。", "第一句。\n\n第二句。"),
            ("我先说。\n陈子豪：我来补充", "我先说。"),
            ("我先说。\n主持人：好的", "我先说。"),
        ],
    )
    def test_cleans_one_speakers_text(self, fg: ModuleType, raw: str, expected: str) -> None:
        cleaner = fg.TurnCleaner("王丽", ["陈子豪", "主持人"])
        streamed = "".join(cleaner.feed(raw[i : i + 3]) for i in range(0, len(raw), 3))
        streamed += cleaner.finish()
        assert cleaner.text == expected
        assert streamed.strip() == expected.strip() or "\n\n\n" in raw


class TestReport:
    def _turns(self, fg: ModuleType) -> list[dict[str, Any]]:
        llm = Scripted(fg)
        events = _run(_collect(fg.discuss_session(llm, _brief(fg), _plan(fg), _material(fg))))
        return [d for n, d in events if n == "turn_end"]

    def test_keeps_only_real_quotes_and_known_people(self, fg: ModuleType) -> None:
        plan, turns = _plan(fg), self._turns(fg)
        report = fg.normalize_report(REPORT, _brief(fg), plan, turns)
        finding = report["findings"][0]
        assert finding["participants"] == ["p1", "p2"]
        assert finding["quotes"] == [
            {"speaker": "p1", "text": "王丽的发言"},
            {"speaker": "p2", "text": "陈子豪的发言"},  # re-attributed to who said it
        ]
        assert [(s["participant"], s["stance"]) for s in report["stances"]] == [
            ("p1", "mixed"),
            ("p3", "negative"),
        ]
        assert report["opportunities"][0]["related"] == [1]
        assert "flow_issues" not in report and "ab" not in report and "attention" not in report

    def test_mode_sections(self, fg: ModuleType) -> None:
        material = _material(fg, "flow", 2)
        plan = fg.normalize_plan(PLAN, _brief(fg, "flow"), material)
        flow = fg.normalize_report(REPORT, _brief(fg, "flow"), plan, self._turns(fg))
        assert flow["flow_issues"] == [
            {"screens": ["2"], "issue": "按钮不显眼", "severity": "high", "suggestion": ""}
        ]
        ab = fg.normalize_report(REPORT, _brief(fg, "ab"), _plan(fg), self._turns(fg))
        assert (ab["ab"]["a"], ab["ab"]["b"]) == (["p1", "p2"], ["p3"])  # p2 counted once
        heat = fg.normalize_report(REPORT, _brief(fg, "heatmap"), _plan(fg), self._turns(fg))
        assert heat["attention"][0]["match"] == "conflict"

    def test_report_uses_the_report_model_with_thinking(self, fg: ModuleType) -> None:
        llm = Scripted(fg)
        events = _run(
            _collect(fg.report_session(llm, _brief(fg), _plan(fg), _material(fg), self._turns(fg)))
        )
        assert [n for n, _ in events] == ["status", "report", "usage"]
        assert llm.requests[0]["model"] == "m-report" and llm.requests[0]["thinking"]
        prompt = _instruction(llm.requests[0]["messages"])
        assert "王丽（p1）：王丽的发言" in prompt and "2021 年追高" in prompt

    def test_empty_report_is_bad_output(self, fg: ModuleType) -> None:
        with pytest.raises(fg.FocusError) as caught:
            fg.normalize_report({"findings": []}, _brief(fg), _plan(fg), [])
        assert caught.value.code == "focus_bad_output"


class TestTranscriptValidation:
    def test_shape_and_speakers(self, fg: ModuleType) -> None:
        plan = _plan(fg)
        good = [_turn("q1.ask", "moderator", "嗨")]
        assert fg.validate_transcript(good, plan)[0]["text"] == "嗨"
        assert fg.validate_transcript(None, plan) == []
        for bad in ([{"kind": "ask", "speaker": "p9", "text": "x"}], [{"kind": "x"}], {"a": 1}):
            with pytest.raises(fg.FocusError):
                fg.validate_transcript(bad, plan)


class TestAttentionText:
    def test_grid_focal_points_and_regions(self, fg: ModuleType) -> None:
        text = fg.attention_text(
            "zh",
            {
                "grid": [0.2, 0.1, 0.05, 0.15, 0.2, 0.1, 0.1, 0.05, 0.05],
                "focal": [{"x": 0.12, "y": 0.08, "value": 1.0}],
                "regions": [{"name": "开户按钮", "share": 0.06, "area": 0.03}],
            },
        )
        assert "上：20% · 10% · 5%" in text
        assert "1. (12%, 8%)" in text
        assert "「开户按钮」分到 6% 的注意力（面积占页面 3%）" in text
        assert fg.attention_text("en", None) == "(none)"


class TestEventStream:
    def test_events_pings_and_done(self, fg: ModuleType) -> None:
        async def events() -> AsyncIterator[tuple[str, dict[str, Any]]]:
            yield "status", {"phase": "recruit"}
            await asyncio.sleep(0.05)
            yield "plan", {"plan": {"title": "理财"}}

        closed: list[bool] = []

        async def close() -> None:
            closed.append(True)

        chunks = _run(_collect_text(fg.event_stream(events(), on_close=close, ping_seconds=0.01)))
        assert chunks[0] == 'event: status\ndata: {"phase": "recruit"}\n\n'
        assert ": ping\n\n" in chunks
        assert 'event: plan\ndata: {"plan": {"title": "理财"}}\n\n' in chunks
        assert chunks[-1] == "event: done\ndata: {}\n\n"
        assert closed == [True]

    def test_errors_end_the_stream_without_done(self, fg: ModuleType) -> None:
        async def events() -> AsyncIterator[tuple[str, dict[str, Any]]]:
            yield "status", {}
            raise fg.FocusError("focus_llm_balance", "No balance.")

        chunks = _run(_collect_text(fg.event_stream(events())))
        assert chunks[-1].startswith("event: error\n")
        assert json.loads(chunks[-1].split("data: ", 1)[1])["code"] == "focus_llm_balance"
        assert not any(chunk.startswith("event: done") for chunk in chunks)


async def _collect_text(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


# ── DeepSeek client ──────────────────────────────────────────────────────────


def _sse(*chunks: dict[str, Any], comment: bool = True) -> bytes:
    lines = [": keep-alive\n\n"] if comment else []
    lines += [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


def _delta(text: str, finish: str | None = None, usage: dict[str, Any] | None = None) -> dict:
    chunk: dict[str, Any] = {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}
    if usage:
        chunk["usage"] = usage
    return chunk


class TestDeepSeekClient:
    def _client(self, fg: ModuleType, handler: Callable[[Any], Any]) -> Any:
        import httpx

        settings = fg.Settings("sk-test", base_url="https://deepseek.test")
        return fg.DeepSeek(settings, transport=httpx.MockTransport(handler))

    def _stream(self, client: Any, **kw: Any) -> list[str]:
        async def run() -> list[str]:
            try:
                return [
                    text async for text in client.stream([{"role": "user", "content": "hi"}], **kw)
                ]
            finally:
                await client.aclose()

        return _run(run())

    def test_streams_text_and_counts_usage(self, fg: ModuleType) -> None:
        import httpx

        sent: list[dict[str, Any]] = []

        def handler(request: Any) -> Any:
            sent.append(json.loads(request.content))
            assert request.headers["authorization"] == "Bearer sk-test"
            assert request.url.path == "/chat/completions"
            usage = {
                "prompt_tokens": 120,
                "prompt_cache_hit_tokens": 100,
                "completion_tokens": 7,
                "completion_tokens_details": {"reasoning_tokens": 3},
            }
            reasoning = {"choices": [{"delta": {"reasoning_content": "thinking…"}}]}
            body = _sse(reasoning, _delta("你好"), _delta("，世界", "stop", usage))
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        client = self._client(fg, handler)
        assert self._stream(client, temperature=1.2, max_tokens=50) == ["你好", "，世界"]
        payload = sent[0]
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["temperature"] == 1.2 and payload["max_tokens"] == 50
        assert payload["stream"] and payload["stream_options"] == {"include_usage": True}
        assert "response_format" not in payload and "reasoning_effort" not in payload
        assert client.usage.as_dict() == {
            "calls": 1,
            "prompt_tokens": 120,
            "cache_hit_tokens": 100,
            "completion_tokens": 7,
            "reasoning_tokens": 3,
        }

    def test_thinking_json_request(self, fg: ModuleType) -> None:
        import httpx

        sent: list[dict[str, Any]] = []

        def handler(request: Any) -> Any:
            sent.append(json.loads(request.content))
            return httpx.Response(200, content=_sse(_delta("{}", "stop")))

        self._stream(self._client(fg, handler), thinking=True, json_mode=True, temperature=1.0)
        assert sent[0]["thinking"] == {"type": "enabled"}
        assert sent[0]["reasoning_effort"] == "high"
        assert sent[0]["response_format"] == {"type": "json_object"}
        assert "temperature" not in sent[0]  # no effect in thinking mode

    @pytest.mark.parametrize(
        ("status", "code"),
        [(401, "focus_llm_auth"), (402, "focus_llm_balance"), (400, "focus_llm_rejected")],
    )
    def test_maps_errors(self, fg: ModuleType, status: int, code: str) -> None:
        import httpx

        def handler(request: Any) -> Any:
            return httpx.Response(status, json={"error": {"message": "Model Not Exist"}})

        with pytest.raises(fg.FocusError) as caught:
            self._stream(self._client(fg, handler))
        assert caught.value.code == code

    def test_retries_rate_limits_then_succeeds(
        self, fg: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        monkeypatch.setattr(fg, "_RETRY_DELAYS", (0.0, 0.0))
        calls: list[int] = []

        def handler(request: Any) -> Any:
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(429 if len(calls) == 1 else 503, json={})
            return httpx.Response(200, content=_sse(_delta("好", "stop")))

        assert self._stream(self._client(fg, handler)) == ["好"]
        assert len(calls) == 3

    def test_gives_up_after_retries(self, fg: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        monkeypatch.setattr(fg, "_RETRY_DELAYS", (0.0, 0.0))

        def handler(request: Any) -> Any:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(fg.FocusError) as caught:
            self._stream(self._client(fg, handler))
        assert caught.value.code == "focus_llm_unreachable"

    def test_content_filter_is_reported(self, fg: ModuleType) -> None:
        import httpx

        def handler(request: Any) -> Any:
            return httpx.Response(200, content=_sse(_delta("半句", "content_filter")))

        with pytest.raises(fg.FocusError) as caught:
            self._stream(self._client(fg, handler))
        assert caught.value.code == "focus_filtered"

    def test_settings_from_environment(
        self, fg: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_REPORT_MODEL"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://proxy.test/")
        settings = fg.Settings.from_env()
        assert not settings.configured
        assert (settings.model, settings.report_model) == ("deepseek-flash", "deepseek-flash")
        assert settings.base_url == "https://proxy.test"
        monkeypatch.setenv("DEEPSEEK_API_KEY", " sk-1 ")
        monkeypatch.setenv("DEEPSEEK_REPORT_MODEL", "deepseek-v4-pro")
        settings = fg.Settings.from_env()
        assert settings.configured and settings.api_key == "sk-1"
        assert settings.report_model == "deepseek-v4-pro"


# ── Endpoints ────────────────────────────────────────────────────────────────


def _events(response: Any) -> list[tuple[str, Any]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = []
    for block in response.text.split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith(":")]
        if not lines:
            continue
        name = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        events.append((name, data))
    return events


@pytest.fixture
def scripted(web: ModuleType, fg: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[Scripted]:
    """Configure the focus group and record every scripted model the app creates."""
    made: list[Scripted] = []

    def factory(settings: Any) -> Scripted:
        made.append(Scripted(fg, probe={"probe": True, "targets": ["p1"], "question": "比如？"}))
        return made[-1]

    monkeypatch.setattr(web, "FOCUS", fg.Settings("test-key"))
    monkeypatch.setattr(web, "_focus_client", factory)
    return made


@pytest.fixture
def client(web: ModuleType) -> Any:
    from fastapi.testclient import TestClient

    return TestClient(web.app)


def _form(**extra: Any) -> dict[str, Any]:
    return {"topic": "理财用户的痛点", "lang": "zh", **extra}


def _shot(name: str) -> tuple[str, tuple[str, bytes, str]]:
    return ("images", (name, _png(), "image/png"))


def _turn(slot: str, speaker: str, text: str) -> dict[str, Any]:
    kind = "ask" if speaker == "moderator" else "answer"
    return {"slot": slot, "kind": kind, "speaker": speaker, "question": 0, "text": text}


class TestEndpoints:
    def test_unconfigured_server_says_so(
        self, web: ModuleType, fg: ModuleType, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(web, "FOCUS", fg.Settings(""))
        assert client.get("/api/info").json()["tools"]["focus_group"] is False
        response = client.post("/api/focus-group/plan", data=_form())
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "focus_unavailable"

    def test_route_and_info(self, client: Any, scripted: list[Scripted]) -> None:
        assert client.get("/focus-group").status_code == 200
        info = client.get("/api/info").json()
        assert info["tools"]["focus_group"] is True
        assert info["focus_group"]["participants"] == [3, 6]
        assert info["focus_group"]["max_images"] == 20

    @pytest.mark.parametrize(
        ("data", "files", "code"),
        [
            ({"topic": "  "}, None, "focus_topic_required"),
            ({"topic": "x" * 301}, None, "focus_text_too_long"),
            ({"topic": "x", "mode": "flow"}, None, "focus_images_missing"),
            ({"topic": "x", "mode": "ab"}, [_shot("a.png")], "focus_images_missing_ab"),
            (
                {"topic": "x", "mode": "heatmap"},
                [_shot("a.png"), _shot("b.png")],
                "focus_images_too_many",
            ),
            (
                {"topic": "x", "mode": "flow"},
                [("images", ("a.txt", b"hello", "text/plain"))],
                "unreadable_image",
            ),
        ],
    )
    def test_plan_validates_before_streaming(
        self, client: Any, scripted: list[Scripted], data: Any, files: Any, code: str
    ) -> None:
        response = client.post("/api/focus-group/plan", data=data, files=files)
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == code
        assert scripted == []  # nothing reached the model

    def test_plan_text_mode(self, client: Any, scripted: list[Scripted]) -> None:
        events = _events(client.post("/api/focus-group/plan", data=_form(participants="3")))
        assert [name for name, _ in events] == ["status", "plan", "usage", "done"]
        plan = events[1][1]["plan"]
        assert [p["id"] for p in plan["participants"]] == ["p1", "p2", "p3"]
        assert scripted[0].closed

    def test_plan_flow_mode_sends_screens_in_order(
        self, client: Any, scripted: list[Scripted]
    ) -> None:
        files = [
            ("images", ("01.png", _png(color=(255, 0, 0)), "image/png")),
            ("images", ("02.jpg", _jpeg((3000, 1500)), "image/jpeg")),
        ]
        events = _events(client.post("/api/focus-group/plan", data=_form(mode="flow"), files=files))
        assert [s["ref"] for s in events[1][1]["plan"]["screens"]] == ["1", "2"]
        images = _images(scripted[0].requests[0]["messages"])
        assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
        resized = _decode(images[1]["image_url"]["url"])
        assert max(resized.size) == 2048  # the plan sees screens at up to 2048 px

    def test_plan_heatmap_mode_runs_hotgaze_first(
        self, client: Any, scripted: list[Scripted]
    ) -> None:
        landing = Path(__file__).parent / "fixtures" / "landing.png"
        files = [("images", ("landing.png", landing.read_bytes(), "image/png"))]
        data = _form(mode="heatmap", region="cta:250,200,200,35")
        events = _events(client.post("/api/focus-group/plan", data=data, files=files))
        names = [name for name, _ in events]
        assert names == ["status", "attention", "status", "plan", "usage", "done"]
        summary = events[1][1]["summary"]
        assert summary["size"] == [800, 600]
        assert abs(sum(summary["grid"]) - 1) < 0.01
        assert summary["regions"][0]["name"] == "cta"
        assert 0 <= summary["focal"][0]["x"] <= 1
        assert events[1][1]["overlay"].startswith("data:image/jpeg;base64,")
        request = scripted[0].requests[0]["messages"]
        assert len(_images(request)) == 2  # the page and its heatmap
        assert "cta" in _instruction(request)

    def test_discuss_streams_turns(
        self, client: Any, scripted: list[Scripted], fg: ModuleType
    ) -> None:
        plan = _plan(fg)
        events = _events(
            client.post(
                "/api/focus-group/discuss",
                data=_form(plan=json.dumps(plan, ensure_ascii=False)),
            )
        )
        names = [name for name, _ in events]
        assert names[0] == "turn" and names[-2:] == ["usage", "done"]
        slots = [d["slot"] for n, d in events if n == "turn_end"]
        assert slots[:6] == ["q1.ask", "q1.p1", "q1.p2", "q1.p3", "q1.probe", "q1.probe.p1"]
        assert slots[-1] == "close.p2"

    def test_discuss_resumes_and_checks_the_transcript(
        self, client: Any, scripted: list[Scripted], fg: ModuleType
    ) -> None:
        plan = json.dumps(_plan(fg), ensure_ascii=False)
        done = [_turn("q1.ask", "moderator", "开始")]
        events = _events(
            client.post(
                "/api/focus-group/discuss",
                data=_form(plan=plan, transcript=json.dumps(done, ensure_ascii=False)),
            )
        )
        assert [d["slot"] for n, d in events if n == "turn_end"][0] == "q1.p1"
        wrong = [{**done[0], "slot": "q2.ask"}]
        events = _events(
            client.post(
                "/api/focus-group/discuss", data=_form(plan=plan, transcript=json.dumps(wrong))
            )
        )
        assert events[-1] == (
            "error",
            {
                "code": "focus_invalid_transcript",
                "message": "The transcript does not follow the session order at 'q2.ask'.",
                "params": {},
            },
        )
        bad = client.post("/api/focus-group/discuss", data=_form(plan=plan, transcript="[1]"))
        assert bad.status_code == 400

    def test_discuss_heatmap_mode_reveals_the_overlay(
        self, client: Any, scripted: list[Scripted], fg: ModuleType
    ) -> None:
        material = _material(fg, "heatmap", 1)
        plan = fg.normalize_plan(PLAN, _brief(fg, "heatmap"), material)
        files = [
            ("images", ("page.png", _png(), "image/png")),
            ("overlay", ("overlay.jpg", _jpeg((32, 24)), "image/jpeg")),
        ]
        events = _events(
            client.post(
                "/api/focus-group/discuss",
                data=_form(mode="heatmap", plan=json.dumps(plan, ensure_ascii=False)),
                files=files,
            )
        )
        reveal = [d for n, d in events if n == "turn_end" and d.get("image") == "heatmap"]
        assert [d["slot"] for d in reveal] == ["q2.ask"]

    def test_report(self, client: Any, scripted: list[Scripted], fg: ModuleType) -> None:
        plan = _plan(fg, "flow")
        turns = [
            _turn("q1.ask", "moderator", "开始"),
            _turn("q1.p1", "p1", "王丽的发言"),
        ]
        data = _form(
            mode="flow",
            plan=json.dumps(plan, ensure_ascii=False),
            transcript=json.dumps(turns, ensure_ascii=False),
        )
        events = _events(client.post("/api/focus-group/report", data=data))
        assert [name for name, _ in events] == ["status", "report", "usage", "done"]
        report = events[1][1]["report"]
        assert report["findings"][0]["quotes"] == [{"speaker": "p1", "text": "王丽的发言"}]
        assert report["flow_issues"][0]["screens"] == ["2"]  # refs known from the plan alone

    def test_report_needs_a_discussion(
        self, client: Any, scripted: list[Scripted], fg: ModuleType
    ) -> None:
        data = _form(plan=json.dumps(_plan(fg), ensure_ascii=False), transcript="[]")
        response = client.post("/api/focus-group/report", data=data)
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "focus_transcript_empty"


class TestSamples:
    """The sample sessions the page shows must be runs the pipeline could have produced."""

    class _NeverCalled:
        usage: Any = None

        async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            raise AssertionError("the sample transcript stops before the closing round")
            yield ""  # pragma: no cover

        async def aclose(self) -> None:
            pass

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_sample_follows_the_pipeline(self, fg: ModuleType, lang: str) -> None:
        path = _WEB / "static" / "samples" / f"focus-group-wealth.{lang}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["format"] == "focal-focus-group" and data["lang"] == lang
        assert data["generator"]  # the page labels the sample from this
        mode = data["brief"]["mode"]
        brief = fg.Brief(lang, data["brief"]["topic"], "", "", 5, mode)
        material = fg.Material(mode, [fg.Shot(r, "") for r in fg.plan_refs(mode, data["plan"])])
        plan = fg.validate_plan(data["plan"], brief, material)
        assert plan["participants"] == data["plan"]["participants"]
        assert plan["guide"] == data["plan"]["guide"]
        turns = fg.validate_transcript(data["transcript"], plan)
        never = self._NeverCalled()
        never.usage = fg.Usage()
        never.settings = fg.Settings("x")  # type: ignore[attr-defined]
        events = _run(_collect(fg.discuss_session(never, brief, plan, material, turns)))
        assert events[-1][0] == "usage"  # replayed to the end without a new turn
        report = fg.normalize_report(data["report"], brief, plan, turns)
        for raw, kept in zip(data["report"]["findings"], report["findings"], strict=True):
            assert kept["quotes"] == raw["quotes"], "every quote must be the speaker's exact words"


class TestTranslations:
    def test_every_focus_error_code_has_a_translation(
        self, web: ModuleType, fg: ModuleType
    ) -> None:
        strings = (_WEB / "static" / "js" / "i18n.js").read_text()
        engine = Path(fg.__file__).read_text()
        app = Path(web.__file__).read_text()
        codes = set(re.findall(r'FocusError\(\s*"([a-z_]+)"', engine))
        codes |= set(re.findall(r'_error\(\s*\d+,\s*(?:code if \w+ else )?"(focus_[a-z_]+)"', app))
        codes |= set(re.findall(r'_focus_parse\([^)]*"(focus_[a-z_]+)"\)', app))
        codes |= set(re.findall(r'code = "(focus_[a-z_]+)" if', app))
        assert {"focus_llm_balance", "focus_unavailable", "focus_images_missing_ab"} <= codes
        for code in sorted(codes):
            assert strings.count(f'"errors.{code}":') == 2, code


def _jpeg(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 120, 40)).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _decode(data_url: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))
