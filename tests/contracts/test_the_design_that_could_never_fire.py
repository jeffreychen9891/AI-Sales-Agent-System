"""设计写了但永远触发不了的三处，修掉并钉住（适配审查批，2026-09-24）。

一次「以代码为主」的适配审查（代码 × 配包 × 资料库 × profile）找出三个同族缺陷，
**形状全是缺陷 19 那种**：机制两头都建了、中间是空的，而空是静默的。

**K4** · demo 五帧全配 `ai_commitment_due` time_rule，而 AI 承诺唯一入账路是
`outcome_rules`（demo **0 条**，且策略清一色 `no_new_commitment`）——
per-row 锚 `commitment_due_at` 永无行可挂。修法：删五条死规则 +
封版检「声明这种规则的包必须有 registrar」。

**K1** · `knowledge_bindings` 封版检语法、投影回显，而检索按
「租户 published release」检，绑定**运行期零读者**；carrier 绑的实体全仓 0 处。
修法：接开关语义（`[]` = 不检索）；demo/third 补真绑定（值解引用留 T505）。

**K3** · carrier `offer_material` 声明 `must_quote_material`，而素材文本只来自
`materials.json`，carrier 没有 —— 引用闸 `if materials and not quoted` **恒短路**。
修法：carrier 补六段词 + 封版检「允许引用型策略的帧必须有词」。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastagent_v2.profiles.registry import (
    ProfilePackageError,
    _check_commitment_timer_rules_have_a_registrar,
    _check_must_quote_strategies_have_words,
)
from fastagent_v2.worker.pipeline import InboxProcessor
from fastagent_v2.worker.platform_frames import materials_for_frame

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _profile(name: str) -> dict[str, Any]:
    return json.loads(
        (REPOSITORY_ROOT / "profiles" / name / "profile.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# K4 · 承诺到期规则要有入账人
# ---------------------------------------------------------------------------


def test_a_commitment_timer_without_a_registrar_is_refused() -> None:
    """帧声明 `ai_commitment_due` 而全包无 `party: ai` + 同名 fulfilment 的规则 = 拒。"""

    document = {
        "target_frames": [
            {
                "id": "a",
                "time_rules": [{"id": "a-due", "emit_kind": "ai_commitment_due"}],
            }
        ],
        "outcome_rules": [],
    }

    with pytest.raises(ProfilePackageError, match="registers no way"):
        _check_commitment_timer_rules_have_a_registrar(document, Path("p.json"))


def test_a_registrar_seals_and_the_shipped_packages_pass() -> None:
    """反侧 + 存量：有 registrar 的封得住；三个在架包全过（demo 已删净）。"""

    document = {
        "target_frames": [
            {
                "id": "a",
                "time_rules": [{"id": "a-due", "emit_kind": "ai_commitment_due"}],
            }
        ],
        "outcome_rules": [
            {"strategy": "advance_goal", "party": "ai", "fulfilment": "ai_commitment_due"}
        ],
    }
    _check_commitment_timer_rules_have_a_registrar(document, Path("p.json"))
    for name in ("internal-demo-sales", "synthetic-carrier", "synthetic-third"):
        _check_commitment_timer_rules_have_a_registrar(_profile(name), Path(name))


def test_demo_no_longer_declares_the_dead_timer_rules() -> None:
    """五条从发布起一次都没可能响过的规则删净；`customer_promise_due` 原样在。

    客户侧承诺不经 `outcome_rules`（理解步照抄客户的话入账），
    所以那一半的规则**活着**，不许一起删。
    """

    demo = _profile("internal-demo-sales")
    assert demo["identity"]["version"] == "1.4.12"
    kinds = [
        rule["emit_kind"]
        for frame in demo["target_frames"]
        for rule in frame.get("time_rules") or []
    ]
    assert "ai_commitment_due" not in kinds
    assert kinds.count("customer_promise_due") == 5


# ---------------------------------------------------------------------------
# K1 · 检索终于读绑定（开关语义）
# ---------------------------------------------------------------------------


class _RecordingRetriever:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, *, context: Any, query: str) -> str:
        self.calls.append(query)
        return "hit"


def _retrieve(profile: dict[str, Any], retriever: _RecordingRetriever) -> Any:
    processor = SimpleNamespace(_retriever=retriever)
    plan = SimpleNamespace(document={"knowledge_query": "运费怎么算"})
    return InboxProcessor._retrieve_if_requested(
        processor, plan=plan, context=None, profile=profile
    )


def test_a_profile_with_no_bindings_never_retrieves() -> None:
    """`knowledge_bindings: []` = 商家说「我没有参考资料」——计划步写了 query 也不检。

    **修复前这个键运行期零读者**（`registry.py` 自认），检索按租户当前
    published release 检：声明为空的包照样查租户库，声明本身没有任何效果。
    """

    retriever = _RecordingRetriever()
    assert _retrieve({"knowledge_bindings": []}, retriever) is None
    assert retriever.calls == []


def test_a_bound_profile_still_retrieves() -> None:
    """反侧：绑了就照旧走检索器 —— 开关只关「没绑」那一侧。"""

    retriever = _RecordingRetriever()
    result = _retrieve(
        {"knowledge_bindings": ["knowledge:demo-internal-corpus:v1"]}, retriever
    )
    assert result == "hit"
    assert retriever.calls == ["运费怎么算"]


def test_demo_now_declares_its_corpus_binding() -> None:
    """demo 租户在用 kb 语料（kb-v1..v3 的 meta 写明「内部演示租户」），
    开关落地那一刻它必须补上声明 —— 否则接线当天 demo 的检索静默关掉。"""

    assert _profile("internal-demo-sales")["knowledge_bindings"] == [
        "knowledge:demo-internal-corpus:v1"
    ]


def test_every_package_whose_tenant_retrieves_declares_a_binding() -> None:
    """开关落地那天，**每个真的在检索的租户，它的包都得把话说出来**。

    demo 的租户在用 kb 语料（kb-v1..v3 的 meta 写明「内部演示租户」）；
    third 的租户在 e2e 里真的发布语料并走完检索三跳
    （`test_knowledge_exit_criteria.py`，检索器按生产装配）——
    这两个包接线前都写着 `[]`，那句「没有参考资料」是假的，
    只是从来没人听。carrier 从 KB-5 起就绑着（值解引用见 T505）。
    `[]` 的关闭语义由上面 `test_a_profile_with_no_bindings_never_retrieves` 钉。
    """

    assert _profile("internal-demo-sales")["knowledge_bindings"] == [
        "knowledge:demo-internal-corpus:v1"
    ]
    assert _profile("synthetic-third")["knowledge_bindings"] == [
        "knowledge:service-desk-faq:v1"
    ]
    assert _profile("synthetic-carrier")["knowledge_bindings"] == [
        "knowledge:carrier-terms:v1"
    ]


# ---------------------------------------------------------------------------
# K3 · 引用型策略的帧必须有词
# ---------------------------------------------------------------------------


def test_a_must_quote_strategy_with_wordless_frames_is_refused() -> None:
    """允许 `must_quote_material` 策略的帧全部素材无词 = 拒（carrier 修复前的形状）。"""

    document = {
        "strategies": {
            "offer_material": {"generation_constraints": ["must_quote_material"]}
        },
        "target_frames": [
            {
                "id": "rental",
                "allowed_strategy_set": ["offer_material"],
                "content_materials": ["material:x-terms:v1"],
            }
        ],
    }

    with pytest.raises(ProfilePackageError, match="no declared material has words"):
        _check_must_quote_strategies_have_words(document, Path("p.json"))


def test_words_supplied_seals_and_a_non_quoting_frame_needs_none() -> None:
    """两个反侧：供了词的封得住；不允许引用型策略的帧无词也不拦（third 的形状）。"""

    document = {
        "strategies": {
            "offer_material": {"generation_constraints": ["must_quote_material"]}
        },
        "content_material_texts": {"material:x-terms:v1": "按天计费，归还当天不计。"},
        "target_frames": [
            {
                "id": "rental",
                "allowed_strategy_set": ["offer_material"],
                "content_materials": ["material:x-terms:v1"],
            }
        ],
    }
    _check_must_quote_strategies_have_words(document, Path("p.json"))

    wordless_but_silent = {
        "strategies": {"acknowledge": {"generation_constraints": []}},
        "target_frames": [
            {
                "id": "welcome",
                "allowed_strategy_set": ["acknowledge"],
                "content_materials": ["material:y:v1"],
            }
        ],
    }
    _check_must_quote_strategies_have_words(wordless_but_silent, Path("p.json"))
    _check_must_quote_strategies_have_words(_profile("synthetic-third"), Path("third"))


def test_carrier_now_supplies_words_and_the_quote_gate_has_teeth() -> None:
    """carrier 六段词落地：封版过，且 rental 帧真的解析出词 ——
    `MATERIAL_QUOTE_REQUIRED` 的 `if materials` 在 carrier 上第一次可能为真。"""

    carrier = _profile("synthetic-carrier")
    assert carrier["identity"]["version"] == "1.0.3"

    # 封版时这个检跑在 `_merge_material_texts` 之后（`load_package` 的顺序），
    # 所以这里也先并——**拿裸 profile.json 去跑它会红，而那个红是对的**：
    # 词在 sibling 文件里，没并进来就是「无词」。
    texts = json.loads(
        (REPOSITORY_ROOT / "profiles" / "synthetic-carrier" / "materials.json").read_text(
            encoding="utf-8"
        )
    )["materials"]
    merged = dict(carrier)
    merged["content_material_texts"] = texts
    _check_must_quote_strategies_have_words(merged, Path("carrier"))
    rental = next(f for f in carrier["target_frames"] if f["id"] == "rental")
    resolved = materials_for_frame(merged, rental)
    assert set(resolved) == {
        "material:carrier-rental-terms:v1",
        "material:carrier-deposit-terms:v1",
        "material:carrier-return-reminder:v1",
    }
    assert all(text.strip() for text in resolved.values())
