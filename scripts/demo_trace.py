#!/usr/bin/env python
"""把一通对话的**决策轨迹**渲染出来 —— 每一轮「读到什么 / 决定做什么 / 为什么」。

## 为什么它从场景文件读，而不是只从数据库读

一个作品集演示最怕的不是不够花哨，是**读者跑不起来**。
完整链路要 PostgreSQL + 两个 Redis + 迁移 + 种子数据；
而这份轨迹要展示的东西 —— **模型每一轮的决策文档** ——
`demo/scenarios/*.json` 里**逐字就是那些文档**
（`ScriptedInference` 回放的就是它，并且按
`packages/contracts/inference/*.schema.json` 的模型侧形状校验过）。

所以默认那条路**零依赖**：

    python scripts/demo_trace.py demo/scenarios/carrier-rental-walkthrough.json

⚠️ **而这件事必须说清楚，否则这份输出会被误读**：
场景那条路渲染的是「**模型侧产出**」，**不是一次真实链路的产物**。
它不经过 ingress、不写 inbox / intent / outbox、不记成本账。
真链路那条路要起一套栈（见 `infrastructure/demo/compose.yml`），
而**表头会写明自己是哪一条** —— 一份分不清自己来历的轨迹，
读者会当成它没有声称的那件事。
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 每一步在轨迹里的标题。**表驱动而不是写死在渲染函数里** ——
#: 场景文件里多一步（`read_silence` 就是这么来的）时，
#: 要么进这张表要么不显示，而「不显示」在输出上与「这一轮没有这一步」同形。
STEP_TITLES: tuple[tuple[str, str], ...] = (
    ("read_silence", "读这次静默"),
    ("understand", "理解"),
    ("plan", "规划"),
    ("generate", "生成"),
)


@dataclass(frozen=True, slots=True)
class Line:
    """轨迹里的一行：一个标签和它的值。"""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class Step:
    key: str
    title: str
    lines: list[Line] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Turn:
    ordinal: int
    label: str
    steps: list[Step]
    reply: str | None
    usage: list[Line]


@dataclass(frozen=True, slots=True)
class Trace:
    source: str
    scenario_id: str
    title: str
    profile: str
    provenance: str
    turns: list[Turn]


def _join(values: list[str]) -> str:
    return "、".join(values) if values else "（无）"


def _join_any(value: object) -> str:
    if isinstance(value, list):
        return _join([str(item) for item in value])
    return "（无）"


def _rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _confidence(document: dict[str, Any]) -> str:
    block = document.get("confidence")
    if not isinstance(block, dict):
        return "（无）"
    calibrated = block.get("calibrated_score")
    shown = "未标定" if calibrated is None else str(calibrated)
    return (
        f"raw {block.get('raw_score')} / 标定 {shown}"
        f" / 依据 {_join_any(block.get('factor_codes'))}"
    )


def _read_silence_lines(document: dict[str, Any]) -> list[Line]:
    action = document.get("action")
    action = action if isinstance(action, dict) else {}
    return [
        Line("读到的", str(document.get("reading", ""))),
        Line("决定做", str(action.get("do", ""))),
        Line("为什么", str(action.get("because", ""))),
    ]


def _understand_lines(document: dict[str, Any]) -> list[Line]:
    return [
        Line("这一轮读到", str(document.get("turn_summary", ""))),
        Line(
            "意图",
            _join(
                [
                    f"{item.get('intent_label')}（{item.get('confidence')}）"
                    for item in _rows(document.get("intents"))
                ]
            ),
        ),
        Line(
            "槽位填上",
            _join(
                [
                    f"{item.get('slot')} = {item.get('value')}"
                    for item in _rows(document.get("slot_updates"))
                ]
            ),
        ),
        Line(
            "还不知道",
            _join(
                [
                    f"{item.get('field')}（{item.get('reason')}）"
                    for item in _rows(document.get("uncertainties"))
                ]
            ),
        ),
        Line(
            "情绪 / 风险",
            f"{document.get('sentiment')} / {_join_any(document.get('risk_markers'))}",
        ),
    ]


def _plan_lines(document: dict[str, Any]) -> list[Line]:
    operation = document.get("target_stack_operation")
    operation = operation if isinstance(operation, dict) else {}
    return [
        Line("选的策略", str(document.get("selected_strategy_id", ""))),
        Line("理由码", str(document.get("strategy_reason_code", ""))),
        Line("要问的槽位", _join_any(document.get("required_slot_requests"))),
        # **这一行是承重的**：生成受这些约束，而约束是**闭合词表**，不是动作表
        # （`CLAUDE.md` 第三节记过一次把「要检索」塞进这个词表的事 ——
        # schema 过得去，而读词表的人会读到一个不是约束的约束）。
        Line("生成约束", _join_any(document.get("generation_constraints"))),
        Line("目标栈", str(operation.get("op", ""))),
        Line("把握", _confidence(document)),
    ]


def _generate_lines(document: dict[str, Any]) -> list[Line]:
    return [
        Line("按策略", str(document.get("strategy_id", ""))),
        Line("对客断言", _join_any(document.get("claims"))),
        Line("安全标记", _join_any(document.get("safety_markers"))),
        Line("把握", _confidence(document)),
    ]


_LINE_BUILDERS: dict[str, Callable[[dict[str, Any]], list[Line]]] = {
    "read_silence": _read_silence_lines,
    "understand": _understand_lines,
    "plan": _plan_lines,
    "generate": _generate_lines,
}


def _reply_of(document: dict[str, Any]) -> str | None:
    """这一轮对客户说的那句话，没说就是 `None`。

    **「没说」与「说了一句空的」要分开**：前者是一次**有意的沉默**
    （这个系统把它当成一个可以被判对错的决定，并且有一轮专门录了它），
    后者是缺陷。把两者渲染成同一个样子，演示就把那一轮的看点抹掉了。
    """

    spoken = [
        str(segment.get("content_or_ref", ""))
        for segment in _rows(document.get("customer_visible_segments"))
        if str(segment.get("content_or_ref", ""))
    ]
    return "\n".join(spoken) if spoken else None


def _usage_lines(document: dict[str, Any]) -> list[Line]:
    lines: list[Line] = []
    for step, _ in STEP_TITLES:
        block = document.get(step)
        if not isinstance(block, dict):
            continue
        lines.append(
            Line(
                step,
                f"入 {block.get('input_tokens')} tok"
                f" / 出 {block.get('output_tokens')} tok"
                f" / {block.get('latency_ms')} ms",
            )
        )
    return lines


def trace_from_scenario(scenario: dict[str, Any]) -> Trace:
    """场景文件 -> 轨迹。**纯函数**，所以它测得起来，也所以它不需要数据库。"""

    profile = scenario.get("profile")
    profile = profile if isinstance(profile, dict) else {}
    turns: list[Turn] = []
    for raw in _rows(scenario.get("turns")):
        documents = raw.get("steps")
        documents = documents if isinstance(documents, dict) else {}
        steps: list[Step] = []
        for key, title in STEP_TITLES:
            document = documents.get(key)
            if not isinstance(document, dict):
                continue
            steps.append(Step(key=key, title=title, lines=_LINE_BUILDERS[key](document)))
        generated = documents.get("generate")
        usage = documents.get("usage")
        turns.append(
            Turn(
                ordinal=int(raw.get("ordinal", 0)),
                label=str(raw.get("label", "")),
                steps=steps,
                reply=_reply_of(generated) if isinstance(generated, dict) else None,
                usage=_usage_lines(usage) if isinstance(usage, dict) else [],
            )
        )
    return Trace(
        source="场景文件（模型侧产出，未经真实链路）",
        scenario_id=str(scenario.get("scenario_id", "")),
        title=str(scenario.get("title", "")),
        profile=(
            f"{profile.get('profile_id')} v{profile.get('version')}"
            f"（帧 {profile.get('frame')}）"
        ),
        provenance=str(scenario.get("provenance", "")),
        turns=turns,
    )


def render_text(trace: Trace) -> str:
    out: list[str] = [
        f"决策轨迹：{trace.title}",
        f"场景   : {trace.scenario_id}",
        f"配包   : {trace.profile}",
        f"来源   : {trace.source}",
        "",
    ]
    for turn in trace.turns:
        out.append("=" * 72)
        out.append(f"第 {turn.ordinal} 轮 · {turn.label}")
        for step in turn.steps:
            out.append(f"  [{step.title}]")
            for line in step.lines:
                out.append(f"    {line.label:<13}{line.value}")
        out.append("  [对客户说]")
        out.append(f"    {turn.reply}" if turn.reply else "    （这一轮有意不说话）")
        if turn.usage:
            out.append("  [用量]")
            for line in turn.usage:
                out.append(f"    {line.label:<13}{line.value}")
    return "\n".join(out) + "\n"


#: HTML 那份的样式。**单文件、不引用任何外部资源** ——
#: 一份要靠 CDN 才能看的演示，在断网的会议室里就是一张白页。
_STYLE = """
:root{--bg:#fbfbfd;--fg:#1a1a1f;--mut:#6b6b76;--card:#fff;--line:#e4e4ea;
--accent:#2f5fd0;--say:#0b6b4f;--quiet:#8a6d1f}
@media(prefers-color-scheme:dark){:root{--bg:#141418;--fg:#ececf1;--mut:#9a9aa6;
--card:#1d1d23;--line:#2e2e37;--accent:#8fb0ff;--say:#6ddfae;--quiet:#e3c76a}}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1rem;background:var(--bg);color:var(--fg);
font:15px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:52rem;margin:0 auto}
h1{font-size:1.4rem;margin:0 0 .4rem}
.meta{color:var(--mut);font-size:.84rem;margin-bottom:.3rem}
.note{border-left:3px solid var(--quiet);padding:.6rem .9rem;margin:1.2rem 0;
background:var(--card);color:var(--mut);font-size:.84rem;border-radius:0 6px 6px 0}
.turn{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:1rem 1.2rem;margin:1rem 0}
.turn>h2{font-size:1rem;margin:0 0 .8rem;color:var(--accent)}
.step{margin:.7rem 0}
.step>h3{font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;
color:var(--mut);margin:0 0 .3rem}
dl{display:grid;grid-template-columns:7.5rem 1fr;gap:.2rem .8rem;margin:0}
dt{color:var(--mut);font-size:.86rem}
dd{margin:0;overflow-wrap:anywhere}
.say{border-left:3px solid var(--say);padding:.5rem .9rem;margin:.8rem 0 0;font-size:.96rem}
.say.quiet{border-left-color:var(--quiet);color:var(--mut)}
"""


def render_html(trace: Trace) -> str:
    """自带样式的单文件 HTML。

    它和 `render_text` 共存的理由：终端那份用来 `grep` 和贴进交接件，
    这份用来给人看。**两份读的是同一个 `Trace`**，所以它们不会各自漂 ——
    这个仓为「同一件事两份实现」赔过不止一次。
    """

    def esc(value: str) -> str:
        return html.escape(value, quote=True)

    parts: list[str] = [
        "<!doctype html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{esc(trace.title)}</title>",
        f"<style>{_STYLE}</style></head><body><div class=wrap>",
        f"<h1>{esc(trace.title)}</h1>",
        f"<div class=meta>场景 {esc(trace.scenario_id)} · 配包 {esc(trace.profile)}</div>",
        f"<div class=meta>来源 {esc(trace.source)}</div>",
        f"<div class=note>{esc(trace.provenance)}</div>",
    ]
    for turn in trace.turns:
        parts.append(f"<section class=turn><h2>第 {turn.ordinal} 轮 · {esc(turn.label)}</h2>")
        for step in turn.steps:
            parts.append(f"<div class=step><h3>{esc(step.title)}</h3><dl>")
            for line in step.lines:
                parts.append(f"<dt>{esc(line.label)}</dt><dd>{esc(line.value)}</dd>")
            parts.append("</dl></div>")
        if turn.reply:
            parts.append(f"<div class=say>{esc(turn.reply)}</div>")
        else:
            parts.append('<div class="say quiet">这一轮有意不说话</div>')
        if turn.usage:
            parts.append("<div class=step><h3>用量</h3><dl>")
            for line in turn.usage:
                parts.append(f"<dt>{esc(line.label)}</dt><dd>{esc(line.value)}</dd>")
            parts.append("</dl></div>")
        parts.append("</section>")
    parts.append("</div></body></html>")
    return "\n".join(parts) + "\n"


def main() -> int:
    # Windows 控制台默认不是 UTF-8（cp1252 / cp936），中文会在这里半截死掉 ——
    # 而这是 README 让读者跑的**第一条**命令，死状是一个 UnicodeEncodeError
    # 回溯，读起来像项目坏了。`demo_drive.py` 早就有这道闸，这一条漏了。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="渲染一通对话的决策轨迹")
    parser.add_argument("scenario", type=Path, help="demo/scenarios/*.json")
    parser.add_argument("--html", type=Path, default=None, help="同时写一份 HTML")
    arguments = parser.parse_args()

    scenario = json.loads(arguments.scenario.read_text(encoding="utf-8"))
    trace = trace_from_scenario(scenario)
    sys.stdout.write(render_text(trace))
    if arguments.html is not None:
        arguments.html.write_text(render_html(trace), encoding="utf-8")
        sys.stdout.write(f"\nHTML 写到：{arguments.html}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
