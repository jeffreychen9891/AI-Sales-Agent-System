"""决策轨迹渲染的那几条判据。

**为什么这一份值得存在**：这个渲染器是作品集里**读者第一个真的跑起来的东西**
（`python scripts/demo_trace.py demo/scenarios/*.json`，不需要数据库也不需要 key）。
它渲染错了不会有任何东西报错 —— 它只会安静地少显示一步、
或者把「有意不说话」显示成「说了一句空的」。

而那一轮**恰恰是整个演示的看点**：第 3 轮是静默轮次，
系统先读这次静默再决定做什么。把它渲染平了，演示就白做了。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = ROOT / "demo" / "scenarios" / "carrier-rental-walkthrough.json"


def _load() -> Any:
    """按路径导入 —— `scripts/` 不是包，与 `test_export_portfolio.py` 同一个办法。"""

    if "demo_trace" in sys.modules:
        return sys.modules["demo_trace"]
    path = ROOT / "scripts" / "demo_trace.py"
    spec = importlib.util.spec_from_file_location("demo_trace", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_trace"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def trace_module() -> Any:
    return _load()


@pytest.fixture(scope="module")
def scenario() -> dict[str, Any]:
    return json.loads(SCENARIO.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def trace(trace_module: Any, scenario: dict[str, Any]) -> Any:
    return trace_module.trace_from_scenario(scenario)


# ------------------------------------------------------------ 表与表对齐


def test_every_titled_step_has_a_builder_and_the_reverse(trace_module: Any) -> None:
    """标题表与渲染表**按等值判**，不是包含判。

    两个方向的失败模式不同，而**都是安静的**：
    * 标题表里有、渲染表里没有 -> `KeyError`（这个还算响）；
    * 渲染表里有、标题表里没有 -> **那一步永远不显示**，
      而「不显示」与「这一轮没有这一步」在输出上一模一样。

    所以这里写等值。新增一步时两张表一起改，或者让这条红。
    """

    titled = {key for key, _ in trace_module.STEP_TITLES}
    built = set(trace_module._LINE_BUILDERS)
    assert titled == built


def test_the_silence_step_is_rendered_before_understanding(trace_module: Any) -> None:
    """`read_silence` 必须排在 `understand` 前面。

    顺序是**语义**，不是审美：那一步的全部意思是
    「**先**读这次静默，**再**决定做什么」。倒过来显示就讲反了。
    """

    order = [key for key, _ in trace_module.STEP_TITLES]
    assert order.index("read_silence") < order.index("understand")
    assert order.index("understand") < order.index("plan") < order.index("generate")


# ------------------------------------------------------------ 这份场景


def test_the_committed_scenario_renders_every_turn(trace: Any) -> None:
    """今天这份场景渲染出四轮，序号 1..4 —— 这是上面那些判据的正面。

    ⚠️ 这条红的时候先看场景文件是不是改了轮数，再改这个数。
    """

    assert [turn.ordinal for turn in trace.turns] == [1, 2, 3, 4]
    assert trace.scenario_id == "carrier-rental-walkthrough"
    assert "synthetic-two-axis-desk" in trace.profile


def test_the_silence_turn_is_the_only_four_step_turn(trace: Any) -> None:
    """只有静默那一轮有四步，别的三轮都是三步。

    这一条同时钉住两件事：静默那一步**真的被渲染了**，
    而且它**没有**被错加到别的轮次上。
    """

    steps_per_turn = {turn.ordinal: [step.key for step in turn.steps] for turn in trace.turns}
    assert steps_per_turn[3][0] == "read_silence"
    assert len(steps_per_turn[3]) == 4
    for ordinal in (1, 2, 4):
        assert "read_silence" not in steps_per_turn[ordinal]
        assert len(steps_per_turn[ordinal]) == 3


def test_the_silence_reading_carries_its_reason(trace: Any) -> None:
    """静默那一步的三行都不许是空的。

    「读到的 / 决定做 / 为什么」缺任何一行，这一轮就退化成
    「系统说了句话」—— 而它要展示的是**它为什么这么说**。
    """

    silence = next(step for step in trace.turns[2].steps if step.key == "read_silence")
    labels = {line.label: line.value for line in silence.lines}
    assert labels["决定做"] == "ask_again"
    assert labels["读到的"].strip()
    assert labels["为什么"].strip()


def test_the_generation_constraints_reach_the_trace(trace: Any) -> None:
    """`generation_constraints` 必须出现在轨迹里。

    它是**闭合约束词表**，而这份轨迹的价值一半在这里：
    读者看得到「模型被允许做什么」，而不只是它说了什么。
    """

    plan = next(step for step in trace.turns[0].steps if step.key == "plan")
    labels = {line.label: line.value for line in plan.lines}
    assert labels["生成约束"] == "one_question"
    assert labels["选的策略"] == "ask_slot"
    assert labels["理由码"] == "RENTAL_PERIOD_NOT_KNOWN"


# ------------------------------------------- 有意的沉默 vs 说了一句空的


def test_a_turn_with_no_segments_reads_as_a_deliberate_silence(
    trace_module: Any,
) -> None:
    """没有可见段落 -> `None`，渲染成「有意不说话」。"""

    assert trace_module._reply_of({"customer_visible_segments": []}) is None
    assert trace_module._reply_of({}) is None


def test_an_empty_string_segment_is_not_speech(trace_module: Any) -> None:
    """**一句空字符串不算说话。**

    ⚠️ 这一条分开的是两件处置方向相反的事：
    * **有意的沉默** —— 一个可以被判对错的决定，演示要显眼地标出来；
    * **一段空内容** —— 缺陷。

    把后者渲染成前者，等于用「这是设计」盖住一个 bug。
    """

    document = {"customer_visible_segments": [{"type": "text", "content_or_ref": ""}]}
    assert trace_module._reply_of(document) is None


def test_the_text_render_marks_the_silence_explicitly(trace_module: Any) -> None:
    """终端那份要把沉默**写出来**，不是留一行空白。

    一行空白与「渲染器坏了」看起来一样。
    """

    trace = trace_module.trace_from_scenario(
        {
            "scenario_id": "s",
            "title": "t",
            "profile": {},
            "provenance": "p",
            "turns": [
                {
                    "ordinal": 1,
                    "label": "沉默",
                    "steps": {"generate": {"customer_visible_segments": []}},
                }
            ],
        }
    )
    assert "有意不说话" in trace_module.render_text(trace)


# ------------------------------------------------------------ 两份渲染


def test_both_renderers_read_the_same_trace(trace_module: Any, trace: Any) -> None:
    """文本与 HTML 读的是**同一个 `Trace`**。

    这一条钉的不是格式，是「同一件事不要有两份实现」——
    这个仓为那个形状赔过不止一次。判据：每一轮对客户说的那句话
    在两份输出里都要出现。
    """

    text = trace_module.render_text(trace)
    page = trace_module.render_html(trace)
    spoken = [turn.reply for turn in trace.turns if turn.reply]
    assert spoken, "这份场景里应当有人说过话，否则这条用例钉不住东西"
    for reply in spoken:
        assert reply in text
        assert reply in page


def test_the_html_escapes_scenario_text(trace_module: Any) -> None:
    """场景文件里的文本进 HTML 之前必须转义。

    场景文件是**输入**（下一份可能来自别人），而这份 HTML 会被人在浏览器里打开。
    不转义就是一个现成的注入点，而它**安静** —— 页面照样渲染得出来。
    """

    trace = trace_module.trace_from_scenario(
        {
            "scenario_id": "s",
            "title": "<script>alert(1)</script>",
            "profile": {},
            "provenance": "p",
            "turns": [
                {
                    "ordinal": 1,
                    "label": "x",
                    "steps": {
                        "generate": {
                            "customer_visible_segments": [
                                {"type": "text", "content_or_ref": "<img src=x onerror=1>"}
                            ]
                        }
                    },
                }
            ],
        }
    )
    page = trace_module.render_html(trace)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "<img src=x onerror=1>" not in page


def test_the_html_needs_no_external_resource(trace_module: Any, trace: Any) -> None:
    """HTML 不许引用任何外部资源。

    一份要靠 CDN 才能看的演示，在断网的会议室里就是一张白页 ——
    而这个仓的整条卖点就是「不需要联网」。
    """

    page = trace_module.render_html(trace)
    for forbidden in ("http://", "https://", "<script", "<link", "@import"):
        assert forbidden not in page, f"外部资源或脚本漏进了 HTML：{forbidden}"
