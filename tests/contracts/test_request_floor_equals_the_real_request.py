"""admission 算的固定底，必须**等于**那条请求真正要付的字节。

## 它为什么存在：三把尺子，互不相等，方向全是 fail-open

2026-09-15 实测（基线 `f55a46b1`，在架八帧，plan 一侧）：

| | `internal-demo-sales/introduce` | `synthetic-carrier/rental` |
|---|---:|---:|
| `scripts/check_material_dimensions.py` 报的 | 9,654 | 8,344 |
| `_check_request_floors` 算的 | 10,179 | 9,195 |
| **真实请求（回合部分全空）** | **10,939** | **10,035** |

**闸少算 639–840，报告器再少 102–851。** 少算的方向是 fail-open ——
**一份"刚好过闸"的包，真实请求已经超了**，而超了不是变贵，
是那一回合的调用在本地就被拒、回落保守计划、**客户收不到东西**。

三笔成因写在 `runtime/request_shape.py` 的 docstring 里；**其中第 3 笔与前两笔
性质不同**：前两笔是算法错（单层对两层、`separators` 方向反了），
第 3 笔是 **`user_prompt` 自己那一圈键名不属于任何一格，于是没有人算它** ——
**成本付了，全仓无人记账。**

## 判据是**等值**，不是 `<=`

`AGENTS.md`「计数类断言写等值，不要写上界」：
**`floor <= real` 会被"少算"满足，而少算正是这个病本身。**
这一条如果写成上界，2026-09-15 之前的那一版**一行不改就能通过**。

## 与 `test_the_plan_floor_counts_what_the_prompt_sends` 的分工

那一份管**结构**（形状只有一个定义点，谁都不许再拼一份局部副本），
本份管**字节**（那一个定义点算出来的数，等于真实请求）。
**两份缺一不可**：只有本份时，有人重新拼一份副本而那一刻两边碰巧相等，它照样绿；
只有那一份时，`REQUEST_OVERHEAD_BYTES` 漂了它看不见。

**唯一定义点让两边永远一致 —— 包括一致地错。**
所以「两份缺一不可」**有证据，不是设计时的谨慎**：探针 R3
（`scripts/probe_request_floor.py`）从那份唯一的组装里删掉一张策略表，
**闸与「真实请求」都少那个键**（两边调的是同一个函数），
**于是字节那份仍然全绿** —— 红的只有结构那份。
一般化成一条挂在 `AGENTS.md`「单一写入点 + 绊线」下：
**唯一定义点要配一条不依赖它的检查。**
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.profiles.registry import (
    MAX_MODEL_INPUT_BYTES,
    REQUEST_OVERHEAD_BYTES,
    request_floor_bytes,
)
from fastagent_v2.runtime import request_shape
from fastagent_v2.runtime.topic_vocabulary import allowed_slot_values
from fastagent_v2.worker import inference_model
from fastagent_v2.worker.model_policy import active_release
from fastagent_v2.worker.providers import _WIRE_JSON, request_payload

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "profiles"

#: 角色 → 它的 system prompt。三个常量就是生产发出去的那三份。
SYSTEM_PROMPTS = {
    request_shape.UNDERSTAND_VALUE: inference_model._UNDERSTAND_SYSTEM,
    request_shape.PLAN_CAPABILITY: inference_model._PLAN_SYSTEM,
    request_shape.GENERATE_VALUE: inference_model._GENERATE_SYSTEM,
    #: 第四个（ADR-0149）。**加进这张表就等于把它接进下面两条用例** ——
    #: 开销现量现比、固定底与真实请求逐帧等值。
    #:
    #: ⚠️ **它是补上的**：第四份请求写完之后跑全量 2,418 条**一条都没红**，
    #: 因为这张表和 `request_floor_bytes` 都只认三个角色。一个会真的发出去的
    #: 新请求形状，没有任何东西检它装不装得下 —— 而 `registry.py` 那段注释
    #: 自己记着同型的上一次（「先前这道闸只看两种」）。
    request_shape.SILENCE_CAPABILITY: inference_model._SILENCE_SYSTEM,
}


def _wire(role: str, user_prompt: str) -> int:
    """一条请求在**线路上**的字节数，由生产那个构造点现造现量。

    **不抄 payload 的形状** —— `providers.request_payload` 是唯一定义点，
    而 `_WIRE_JSON` 是它比 `MAX_MODEL_INPUT_BYTES` 时用的那套参数
    （已由 `test_the_measured_request_is_the_body_httpx_will_send` 与 httpx
    自己的编码器逐字节钉住）。
    """

    payload = request_payload(
        active_release().role(role),
        system_prompt=SYSTEM_PROMPTS[role],
        user_prompt=user_prompt,
    )
    return len(json.dumps(payload, **_WIRE_JSON).encode("utf-8"))


def _packages() -> list[tuple[str, dict[str, Any]]]:
    out = []
    for path in sorted(PACKAGE_ROOT.iterdir()):
        document = path / "profile.json"
        if document.exists():
            out.append((path.name, json.loads(document.read_text(encoding="utf-8"))))
    return out


def _frames() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    return [
        (f"{name}/{frame.get('id')}", frame, document)
        for name, document in _packages()
        for frame in document.get("target_frames") or []
    ]


def _empty_turn_prompts(
    frame: dict[str, Any], document: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """三种请求的 `user_prompt`，**回合那半全空** —— 固定底的定义。

    走的是 `request_shape`，与 admission 和生产同一个函数。
    """

    slots = list(frame.get("information_slots") or [])
    goal = str(frame.get("goal") or "")
    return {
        request_shape.UNDERSTAND_VALUE: request_shape.understand_prompt(
            goal=goal,
            information_slots=slots,
            # 话题槽的合法取值进理解步的 payload，**所以固定底要付它**
            # （2026-09-15「裁 B」）。推导与生产、admission 同一个函数。
            allowed_slot_values=allowed_slot_values(frame),
        ),
        request_shape.PLAN_CAPABILITY: request_shape.plan_prompt(
            goal=goal,
            exit_conditions=list(frame.get("exit_conditions") or []),
            information_slots=slots,
            allowed_strategies=list(frame.get("allowed_strategy_set") or []),
            strategies=document.get("strategies") or {},
            event_kinds=[
                rule.get("emit_kind") for rule in frame.get("time_rules") or []
            ],
        ),
        request_shape.GENERATE_VALUE: request_shape.generate_prompt(),
    }


@pytest.mark.parametrize(
    "label,frame,document", _frames(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_the_floor_equals_the_request_it_claims_to_measure(
    label: str, frame: dict[str, Any], document: dict[str, Any]
) -> None:
    """逐帧 × 三个角色：**固定底 == 回合全空时那条请求的线路字节。**"""

    floors = request_floor_bytes(frame, document=document)
    prompts = _empty_turn_prompts(frame, document)

    for role, prompt in prompts.items():
        measured = _wire(role, json.dumps(prompt, ensure_ascii=False))
        assert floors[role] == measured, (
            f"{label} {role}：admission 报 {floors[role]:,}，"
            f"而那条请求实际 {measured:,}（差 {measured - floors[role]:+,}）。"
            "**少算的方向是 fail-open** —— 刚好过闸的包，真实请求已经超了。"
            "成因见 runtime/request_shape.py 的 docstring；"
            "改了 system prompt 或响应 schema 就要重测 REQUEST_OVERHEAD_BYTES。"
        )


def test_the_overhead_constants_are_measured_from_the_real_payload() -> None:
    """`REQUEST_OVERHEAD_BYTES` 三个数，逐个从生产构造点现量现比。

    **它们是跨包写两遍的那一半**（`services/api` 不从 `services/worker` 导入），
    与 `MATERIAL_BYTES_BUDGET` 同一个理由、同一种守法。
    """

    for role in SYSTEM_PROMPTS:
        assert REQUEST_OVERHEAD_BYTES[role] == _wire(role, ""), (
            f"{role} 的请求开销变了 —— 多半是 system prompt 或响应 schema 改了。"
            "这个数是 admission 固定底的加数，改了它而这里没跟着改，"
            "封版时算出来的余量就是错的"
        )


def test_the_addition_is_an_identity_not_an_approximation() -> None:
    """`真实请求 = 开销 + document_cost(文档)` —— 拿会咬人的文档验残差。

    引号、反斜杠、中文、控制字符 —— 四种都能让"两层转义"这件事出错，
    而**出错的方向都是少算**。残差必须恒为 0，不是"接近 0"。
    """

    from fastagent_v2.runtime.prompt_bytes import document_cost

    nasty: list[dict[str, Any]] = [
        {},
        {"quoted": '"he said"'},
        {"back": "a\\b"},
        {"cjk": "运费与包邮"},
        {"control": "line\nnext\ttab"},
        {"mixed": ['"运"', "a\\\"b", {"deep": "文\n"}]},
    ]
    for role in SYSTEM_PROMPTS:
        base = _wire(role, "")
        for document in nasty:
            measured = _wire(role, json.dumps(document, ensure_ascii=False))
            assert measured == base + document_cost(document), (
                f"{role} 上 {document!r} 的残差不是 0 —— "
                "那个加法不再是恒等式，固定底就不能这样算"
            )


def test_every_shipped_frame_still_has_room_after_the_correction() -> None:
    """修完之后固定底变大、admission 变严 —— **在架的包有没有被拒。**

    **这一条是那次修正的方向性后果**，写成用例而不是写在自审里：
    下一个把 goal 写长、或往提示词加字的人，会在这里先看到。
    """

    for label, frame, document in _frames():
        for role, floor in request_floor_bytes(frame, document=document).items():
            assert floor < MAX_MODEL_INPUT_BYTES, (
                f"{label} {role} 的固定底 {floor:,} 已经装不下（上限 "
                f"{MAX_MODEL_INPUT_BYTES:,}）——这个包封不了版"
            )
