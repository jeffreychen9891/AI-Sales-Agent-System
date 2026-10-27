"""同一组输入，有素材 vs 无素材，生成结果差多少。

用户点名要的那个对照。**它要真调模型**，所以需要
`FASTAGENT_MODEL_API_KEY_DEEPSEEK` 在环境里；脚本本身不打印凭据。

跑法（从仓库根目录，用仓库 .venv 的 python）：

    .venv/Scripts/python.exe scripts/probe_material_quoting.py
    .venv/Scripts/python.exe scripts/probe_material_quoting.py --only 2

`--tree` 默认是本脚本所在的检出，只有要量**另一棵树**时才给。
**它在 `scripts/` 里而不是某个临时目录**，因为它是唯一一个发现
「草稿引用了、payload 一段都没有」的东西——而那个缺陷已经进过 `main`
（见 `docs/reviews/generation-materials-correction-2026-09-10*.md`）。
下一个人要能一条命令跑起来。

对照的构造：**同一个 plan、同一个 understanding、同一个 envelope、同一个 profile**，
唯一的差别是 `generate()` 的 `materials` 参数——一次是本回合选中的素材，
一次是空 mapping（也就是素材那一批之前每一回合的状态）。

三组输入（开场 / 被问到花钱 / 240 秒沉默跟进），每组两次调用。六次调用，都是真钱。

**第二版加了三件**（用户读第一版结果时问出来的）：

1. **逐字比对**：被引用的每一段，`materials.json` 原文 vs 实际发出的文本；
2. **出站那份文档也数一遍**：`customer_visible_segments` 是草稿，
   `typed_payload.content` 才是 `_persist_accepted_proposal` 拿去建行的那份（T265）。
   第一版只数了草稿，那是这个 harness 自己的洞；
3. **疑似改写扫描**：模型不引用、自己写一段近似的话——
   **平台今天认不出来（T268）**，这里的相似度是脚本算的，不是平台的守卫。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load_paths(tree: Path) -> None:
    for source in (
        "packages/runtime/src",
        "packages/channels/src",
        "services/api/src",
        "services/worker/src",
    ):
        sys.path.insert(0, str(tree / source))


#: 本帧声明的四条，以及它们在选取层里的可用性。
_DECLARED: list[tuple[str, dict[str, Any]]] = [
    ("material:demo-opening:v1", {"availability": "proactive"}),
    ("material:demo-spend-ceiling:v1", {"availability": "on_request", "topics": ["花钱"]}),
    ("material:demo-dont-know:v1", {"availability": "on_request"}),
    (
        "material:demo-followup-silence-meta:v1",
        {"availability": "timer", "emit_kinds": ["silence_check"]},
    ),
]


def _profile(materials: dict[str, str], *, structured: bool) -> dict[str, Any]:
    """内部演示那份 profile 的形状，取它 introduce 帧的真实素材与真实分桶。

    **`structured` 决定 `content_materials` 写成对象还是裸字符串。** 对象形只有
    选取层那一批认得；素材那一批的 `materials_for_frame` 会把一个 dict `str()`
    掉、于是一条都匹配不上——**这就是第一版在没有选取层的树上量到「0 段」的原因，
    而那个 0 与模型无关**。脚本要能在两种检出上都说真话。
    """

    return {
        "target_frames": [
            {
                "id": "introduce",
                "goal": "你是这套系统自己的销售，对面是公司其他部门的同事。",
                "information_slots": [{"name": "concern_topic", "required": True}],
                "material_topic_slot": "concern_topic",
                "allowed_strategy_set": ["advance_goal"],
                "exit_conditions": [],
                "time_rules": [
                    {"id": "introduce-silence_check", "emit_kind": "silence_check"},
                ],
                "content_materials": [
                    {"ref": ref, **extra} if structured else ref
                    for ref, extra in _DECLARED
                ],
            }
        ],
        "strategies": {
            "advance_goal": {
                "allowed_intent_kinds": ["send_message"],
                "generation_constraints": [],
            }
        },
        "content_material_texts": materials,
    }


_CASES: list[tuple[str, str | None, dict[str, Any]]] = [
    (
        "开场（对方发了第一条）",
        None,
        {"event_id": "00000000-0000-4000-8000-000000000001", "slot_updates": []},
    ),
    (
        "被问到花钱",
        None,
        {
            "event_id": "00000000-0000-4000-8000-000000000002",
            "slot_updates": [{"slot": "concern_topic", "value": "花钱"}],
        },
    ),
    (
        "240 秒沉默跟进（定时器回合）",
        "silence_check",
        {"event_id": "00000000-0000-4000-8000-000000000003", "slot_updates": []},
    ),
]


def _refs(segments: list[Any]) -> list[str]:
    return [
        str(segment["content_or_ref"])
        for segment in segments
        if isinstance(segment, dict) and segment.get("type") == "material_ref"
    ]


def _render(segments: list[Any], declared: dict[str, str]) -> str:
    """渲染成实际会发出的文本——与 `channel.customer_text` 同一个替换、同一个 trim。"""

    parts = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        value = str(segment.get("content_or_ref"))
        if segment.get("type") == "material_ref":
            parts.append(declared.get(value, "<未知 ref>").strip())
        else:
            parts.append(value.strip())
    return "\n".join(parts)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _payload_segments(document: dict[str, Any]) -> list[Any]:
    """出站那份文档的段。

    **草稿不是发出去的那份。** `_persist_accepted_proposal` 从一条 `send_message`
    意图的 `typed_payload.content` 建出站行，不是从 `customer_visible_segments`
    ——那正是 T265 那一问。第一版这个脚本只数了草稿。
    """

    found: list[Any] = []
    for intent in document.get("proposed_intents", []):
        content = (intent.get("typed_payload") or {}).get("content")
        if isinstance(content, dict) and isinstance(content.get("segments"), list):
            found.extend(content["segments"])
    return found


def _sendable(content: Any, declared: dict[str, str]) -> str | None:
    """出站行里真正会装的那句话，用平台自己的读者算。"""

    from fastagent_v2.worker.channel import customer_text

    return customer_text(content, materials=declared)


def _report_arm(arm: str, document: dict[str, Any], declared: dict[str, str]) -> str:
    segments = document["customer_visible_segments"]
    quoted = _refs(segments)
    payload = _payload_segments(document)
    rendered = _render(segments, declared)

    print(f"\n  —— {arm} ——")
    print(f"  草稿 customer_visible_segments 引用了 {len(quoted)} 段：{quoted}")
    print(f"  出站 typed_payload.content    引用了 {len(_refs(payload))} 段：{_refs(payload)}")
    # **出站那份文档原样打印。** 数不清楚就分不出三种情况：
    # payload 里全是 text（模型自己写的）/ payload 用了另一种 content 形状 /
    # proposed_intents 根本是空的。后果完全不同，而发出去的是这份。
    print(f"  proposed_intents 共 {len(document.get('proposed_intents', []))} 条：")
    for intent in document.get("proposed_intents", []):
        content = (intent.get("typed_payload") or {}).get("content")
        # **不要叫 `rendered`。** 它曾经就叫这个，于是遮住了上面那份渲染结果，
        # 「渲染后」那一行与两臂相似度都变成了在比 payload 的 JSON。
        # 判据那几行没受影响，但一个会打印假行的探针，正是本批在修的那类东西。
        payload_json = json.dumps(content, ensure_ascii=False)[:300]
        print(f"    kind={intent.get('kind')!r} content={payload_json}")
        print(f"    -> customer_text 会发出的：{_sendable(content, declared)!r}")
    print("  模型原样返回的段：")
    for segment in segments:
        value = str(segment["content_or_ref"])
        shown = value if segment["type"] == "material_ref" else value[:44] + "…"
        print(f"    - {segment['type']:<13} {shown}")
    print("  渲染后：")
    print("    " + rendered.replace("\n", "\n    "))

    for ref in quoted:
        original = declared.get(ref)
        if original is None:
            print(f"  x 逐字 {ref}：**不在本回合可用集里**（平台会整条拒发）")
            continue
        delivered = original.strip()
        verdict = "逐字一致" if delivered == original else "仅首尾空白被 trim"
        print(
            f"  v 逐字 {ref}：{verdict}"
            f"（sha256/12 原文 {_digest(original)} -> 出站 {_digest(delivered)}）"
        )

    for segment in segments:
        if segment["type"] != "text":
            continue
        text = str(segment["content_or_ref"])
        best_ref, best = "", 0.0
        for ref, original in declared.items():
            score = difflib.SequenceMatcher(None, text, original).ratio()
            if score > best:
                best_ref, best = ref, score
        if best >= 0.5:
            print(
                f"  ! 疑似改写：一段 text 与 {best_ref} 相似度 {best:.2f}"
                f"——**平台今天认不出来，见 T268**"
            )
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tree",
        type=Path,
        default=ROOT,
        help="仓库检出目录，默认本脚本所在的那一棵",
    )
    parser.add_argument("--materials", type=Path, default=None)
    parser.add_argument(
        "--only",
        type=int,
        default=None,
        help="只跑第几组（1/2/3），用来把确认一件事的成本从六次调用降到两次",
    )
    arguments = parser.parse_args()

    tree: Path = arguments.tree
    _load_paths(tree)

    if not os.environ.get("FASTAGENT_MODEL_API_KEY_DEEPSEEK"):
        print("FASTAGENT_MODEL_API_KEY_DEEPSEEK 没设，跑不了。", file=sys.stderr)
        return 2

    # **选取层可能不在这棵树上。** 它是另一批（`material_selection`），而本脚本要能
    # 在任何检出上跑——量「payload 有没有带上 ref」不需要分桶。缺它时退回
    # 「本帧声明的全部素材」，并在输出里说清楚，免得有人把无桶的读数当成有桶的。
    import importlib

    from fastagent_v2.worker.inference import inference_for_provider
    from fastagent_v2.worker.platform_frames import materials_for_frame

    try:
        material_selection: Any | None = importlib.import_module(
            "fastagent_v2.worker.material_selection"
        )
    except ImportError:
        material_selection = None

    source = arguments.materials or (tree / "tests/fixtures/internal-demo-materials.json")
    texts = json.loads(source.read_text(encoding="utf-8"))["materials"]
    profile = _profile(texts, structured=material_selection is not None)
    definition = profile["target_frames"][0]
    declared = materials_for_frame(profile, definition)

    # **缺素材就把那几条摘掉，不要退出。** 夹具是快照，不同的检出上未必齐
    # （素材那一批带 11 段，选取层那一批刷成 29 段）。本脚本要量的是
    # 「payload 有没有带上 ref」，那件事不需要素材齐全——但读数里必须写明少了什么，
    # 否则「选中 2 段」看起来像分桶的结果，其实是夹具缺了两段。
    def _ref_of(item: Any) -> str:
        return str(item if isinstance(item, str) else item["ref"])

    missing = [
        _ref_of(item) for item in definition["content_materials"] if _ref_of(item) not in declared
    ]
    if missing:
        print(f"⚠ 这棵树的素材夹具缺 {len(missing)} 条，已从 profile 里摘掉：{missing}")
        definition["content_materials"] = [
            item for item in definition["content_materials"] if _ref_of(item) in declared
        ]
    if not declared:
        print("素材夹具里一条都没有，量不了。", file=sys.stderr)
        return 2

    os.chdir(tree)
    # **用生产那个工厂建**，不自己组装。第一版手搓 provider client 搓错了（漏 transport）
    # ——一个自己组装边界的探针，量的是自己的组装，不是平台的。
    inference = inference_for_provider("deepseek")

    plan = {
        "selected_strategy_id": "advance_goal",
        "strategy_reason_code": "ADVANCE",
        "generation_constraints": [],
        "candidate_intents": [{"kind": "send_message", "idempotency_basis": "contrast"}],
        "required_slot_requests": [],
    }

    cases = _CASES if arguments.only is None else [_CASES[arguments.only - 1]]
    for label, emit_kind, understanding in cases:
        if material_selection is None:
            offered = dict(declared)
        else:
            offered = material_selection.select_materials(
                definition=definition,
                declared=declared,
                emit_kind=emit_kind,
                understanding=understanding,
            )
        envelope = {"event_type": "timer" if emit_kind else "customer_message"}

        print("=" * 72)
        print(f"■ {label}")
        note = "" if material_selection is not None else "（**这棵树没有选取层，未分桶**）"
        print(f"  本回合选中 {len(offered)} 段{note}：{list(offered)}")

        results: dict[str, str] = {}
        for arm, materials in (("有素材", offered), ("无素材", {})):
            document = inference.generate(
                plan=plan,
                understanding=understanding,
                envelope=envelope,
                materials=materials,
            )
            results[arm] = _report_arm(arm, document, declared)

        ratio = difflib.SequenceMatcher(None, results["有素材"], results["无素材"]).ratio()
        print(f"\n  两臂逐字相同：{results['有素材'] == results['无素材']}；相似度 {ratio:.2f}")

    print("=" * 72)
    print("读法：")
    print("1. 『引用了 N 段』是模型自报的 ref。无素材那一臂只可能是 0——手上没 ref 可指。")
    print("2. 『逐字』那几行是结构性的，不是运气：material_ref 段只有一个字符串字段，")
    print("   模型放进去的是 ref 不是正文，所以要么等于某个可用 ref（平台取商家原文），")
    print("   要么不等于（GENERATION_MATERIAL_REF_UNKNOWN 整条拒发）。没有第三个分支。")
    print("3. 真正要看的是『! 疑似改写』：模型不引用、自己写一段近似的话。")
    print("   **平台今天认不出来（T268）**，那个相似度是本脚本算的，不是平台的守卫。")
    print("4. 草稿与出站两行若对不上，是 T265 那一问——发出去的是出站那份。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
