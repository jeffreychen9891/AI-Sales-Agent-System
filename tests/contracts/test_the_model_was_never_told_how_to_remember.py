"""运维复测第 8 条：模型提了一条 `remember`，而没有人告诉过它怎么写。

## 现场（stage，2026-09-20 13:12:48，同一轮两条判定）

```
pre_execute | allow | M0_AI_CONTROL_ALLOWED          <- send_message，正常发出
pre_execute | deny  | GENERATION_PAYLOAD_INCOMPLETE  <- remember，被拒
```

`detail` 逐字：

> `a remember payload needs ['field_key', 'field_value'] as non-empty strings;
> this one has ['concern_topic']`

⚠️ **客户那一侧完全看不见** —— 同一轮的回复照常发出，只是那次记忆写入丢了。
**这一族比整份拒更安静**：它不改变客户看到的任何东西。

## 两个缺口，而第二个更根本

1. **提示词没教载荷形状** —— 模型猜了 `{'concern_topic': …}`；
2. **提示词根本没收到「哪些字段可以记」** —— 键名也是猜的，
   第二道闸「商家没声明这个字段」会再拒一次。

②在开工前量出来时是这样的：生成步收到的是 `remembered_facts`
（**已经知道的事**，形状 `{ref, value}`），**而没有任何东西告诉它能记哪些**。
第一次记某个字段时，它没有先例可抄。

## 与 ADR-0144 决策 3 是同一件事

**一张模型看不见的闭合词表，就是一张它会去猜的词表。**
`rememberable_fields` 之于记忆，正是 `slots_wanted` 之于槽位。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastagent_v2.runtime import request_shape
from fastagent_v2.worker.inference_model import _GENERATE_SYSTEM, PROMPT_RELEASE

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY_ROOT / "profiles" / "internal-demo-sales" / "profile.json"
PIPELINE = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "pipeline.py"
)

#: **平台会校验的每一种载荷，以及它各自要哪几个键。**
#:
#: ⚠️ 这张表是本文件的核心：它对着 `pipeline.py` 里那几个
#: `_*_payload_refusal`，而**闸要什么，提示词就得教过什么**。
#: 2026-09-21 开工前实测：四种里**只有 `remember` 一个字都没教**。
VALIDATED_PAYLOADS = {
    "send_message": ("content",),
    "send_material": ("ref",),
    "followup": ("content",),
    "remember": ("field_key", "field_value"),
}


def _profile() -> dict:
    return json.loads(PACKAGE.read_text(encoding="utf-8"))


def test_every_payload_the_platform_checks_is_taught_in_the_prompt() -> None:
    """**闸要什么，提示词就得教过什么** —— 这是本文件的第二条腿。

    ADR-0144 之后仓里已经有一条「契约 `required` vs 提示词 `Required keys`」
    的守卫，**而它抓不到这一族**：`typed_payload` 在契约里是
    **有意不约束**的（ADR-0095 §6），校验在平台这一层。

    ⚠️ 它红的时候多半是有人加了一道新的载荷闸而没告诉模型 ——
    **那种缺陷是安静的**：能建、能过合同、能返回 200，只有那一条意图被丢掉，
    而客户看不出任何不同。
    """

    for kind, keys in VALIDATED_PAYLOADS.items():
        for key in keys:
            assert key in _GENERATE_SYSTEM, (
                f"平台按 `{key}` 校验 {kind} 的载荷，而生成步提示词一个字没提它 —— "
                "模型只能猜，而猜错是安静的"
            )


def test_the_gates_still_check_exactly_those_keys() -> None:
    """⚠️ **上面那张表不能是我写下就算数的** —— 它要对着闸的源码。

    没有这一条，闸改了要求（比如多要一个键）而表没跟上时，
    上面那条照样绿 —— **一张只对着自己的表，守的是它自己。**
    """

    source = PIPELINE.read_text(encoding="utf-8")
    remember = source[source.index("def _remember_payload_refusal") :]
    remember = remember[: remember.index("\ndef ")]
    names = re.search(r'for name in \(([^)]*)\)', remember)
    assert names is not None, "`_remember_payload_refusal` 的形状变了"
    required = tuple(re.findall(r'"([a-z_]+)"', names.group(1)))
    assert required == VALIDATED_PAYLOADS["remember"], (
        f"闸现在要的是 {required}，而本文件那张表写的是 "
        f"{VALIDATED_PAYLOADS['remember']} —— 表要跟着闸走"
    )


def test_the_model_is_told_which_fields_it_may_remember() -> None:
    """**第二个缺口：能记哪几件事。**

    `remembered_facts` 是**已经知道的**，`rememberable_fields` 是**允许记的**。
    两者同时在提示词里，而它们不是一回事 —— 本条钉的是后者真的在。
    """

    document = _profile()
    prompt = request_shape.generate_prompt(
        strategy_id="advance_goal",
        strategy_reason_code="TEST",
        rememberable_fields=document["memory_fields"],
    )
    assert "rememberable_fields" in prompt, "那张表没进提示词 —— 键名只能靠猜"
    assert "remembered_facts" in prompt, "已经知道的那一份不该被挤掉"

    sent = {entry["key"]: entry["description"] for entry in prompt["rememberable_fields"]}
    declared = {field["key"]: field["description"] for field in document["memory_fields"]}
    assert sent == declared, f"下发的与配包声明的对不上：{sent} vs {declared}"


def test_the_model_is_told_the_key_must_come_from_that_list() -> None:
    """⚠️ **把表发下去还不够 —— 要说清「只能从里面取」。**

    这一条是写探针时补的：B 支（只删掉那半句、形状与表都留着）
    **原本一条都不红**。而那正是缺口②的教学句 ——
    发了一张表却没说它是闭合的，模型照样会造一个键，
    只是这次它有先例可抄、猜得更像对的。

    措辞可以改，**这件事不能没有**。
    """

    assert "from that list" in _GENERATE_SYSTEM, (
        "提示词发了 `rememberable_fields` 却没说键只能从里面取 —— "
        "一张没说闭合的表，就是一张会被添项的表"
    )


def test_the_field_table_carries_no_ttl() -> None:
    """⚠️ **`ttl_days` 有意不下发。**

    它是平台与商家之间的事（这条记忆能活多久），模型既不决定它也不该知道它。
    **多给一个字段就是多一个可以被写错的东西。**
    """

    prompt = request_shape.generate_prompt(
        strategy_id="advance_goal",
        strategy_reason_code="TEST",
        rememberable_fields=[
            {"key": "industry", "description": "他做哪一行。", "ttl_days": 30}
        ],
    )
    entry = prompt["rememberable_fields"][0]
    assert set(entry) == {"key", "description"}, f"多发了字段：{sorted(entry)}"


def test_every_declared_field_says_what_it_holds() -> None:
    """**六个字段一个不少，而描述是必填的**（合同已经守着，这里钉的是数）。"""

    fields = _profile()["memory_fields"]
    assert len(fields) == 6, f"记忆字段现在是 {len(fields)} 个：{[f['key'] for f in fields]}"
    empty = [f["key"] for f in fields if not str(f.get("description", "")).strip()]
    assert empty == [], f"这些字段没有描述：{empty}"


def test_the_bounds_are_in_the_prompt_and_say_characters() -> None:
    """**128 / 1024，而且是「字符」不是「字节」。**

    ⚠️ 量纲这件事本仓付过钱：E4-1b 的第一版把同一个 1024 当成字节，
    用户 2026-09-12 裁定撤掉那道闸。闸的注释因此逐字写着
    「`length()` 数的是字符不是字节」，**提示词这一侧也要说同一句**。
    """

    assert "128" in _GENERATE_SYSTEM
    assert "1024" in _GENERATE_SYSTEM
    assert "counted in characters" in _GENERATE_SYSTEM, (
        "没写明量纲 —— 模型按字节数就会在中文上把上界算错三倍"
    )


def test_every_side_of_the_model_boundary_takes_the_same_keywords() -> None:
    """⚠️ **生产签名加一个参数，而替身没跟上 —— 这一批栽在这里。**

    2026-09-21 实测：`ModelBackedInference.generate` 加了
    `rememberable_fields`，`InferencePort` 与 `DeterministicMockInference`
    都没跟上。**`tests/contracts` 2,381 条全绿**，而门禁
    **123 failed**，一条都不在 `contracts` 里 ——
    那一族走的是 e2e/integration 的替身，`TypeError` 要到真管线跑起来才抛。

    ⚠️ **端口是 `Protocol`，运行期不校验**：一个少了参数的实现
    照样能构造、能注入、能通过类型检查以外的一切。

    所以三处的关键字集合逐字相等：端口、生产实现、替身。
    """

    import inspect  # noqa: PLC0415

    from fastagent_v2.worker.inference import (  # noqa: PLC0415
        DeterministicMockInference,
        InferencePort,
    )
    from fastagent_v2.worker.inference_model import ModelBackedInference  # noqa: PLC0415

    def keywords(function: object) -> set[str]:
        return {
            name
            for name, parameter in inspect.signature(function).parameters.items()
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        }

    port = keywords(InferencePort.generate)
    production = keywords(ModelBackedInference.generate)
    double = keywords(DeterministicMockInference.generate)

    assert production == port, (
        f"生产实现与端口对不上 —— 只有生产有：{sorted(production - port)}；"
        f"只有端口有：{sorted(port - production)}"
    )
    assert double == port, (
        f"替身与端口对不上 —— 替身缺：{sorted(port - double)}。"
        "**它红的时候 contracts 可能是全绿的**：替身少一个参数要到 e2e 才抛 TypeError"
    )


def test_the_package_stamp_moves_with_its_version() -> None:
    """⚠️ **改了配包的内容就得抬版本号，而这一条是本批自己踩出来的。**

    本批往六个 `memory_fields` 各加了一句描述、跑 `seal_profile_package.py`
    重新封了版 —— `content_hash` 从 `cc9ff5cd` 变成 `3cb83b88`，
    **而版本号还停在 1.3.0，全仓 2,380 条一条都没红。**

    那个方向是安静的：能封版、能加载、能通过每一条现有断言。
    它会在**发布到 stage 的那一刻**才响 —— 发布器的不可变守卫说
    「这个 id 已经属于别的 release」，而读到那句话的人第一反应是自己哪步做错了。
    本仓为撞号付过五次钱。

    **所以这三样钉在一起**：版本、`release_id` 末段、封版戳。
    重封了版 → 戳变 → 本条红 → 只能连着版本一起抬。

    ⚠️ **本条只管「重封了版而没抬号」这一半**，另一半（改了内容**没**重封版）
    由封版检查管 —— 探针 G 实测过：删掉一句描述时本条不红，
    先红的是封版检查。**两条各管一段，不重叠。**

    ## ⚠️ 2026-09-21：它红了，而我用错的方式让它绿了

    ADR-0146 那一批改了配包内容、重封了版，**本条如期变红** ——
    而落地的改法是**把期望值从旧哈希挪到新哈希**，版本号一个字没动。
    **守卫的报错文案指着要做的那件事（「改了内容就把版本一起抬了」），
    而我做的是另一件。** 同一笔里 `PROMPT_RELEASE` v11 → v12 抬了，
    配包这条没有。

    条目当时写的理由是「1.4.0 尚未发布过，支内共用一个号」。**三处都站不住**：
    1.4.0 **不在支内**，它已随 `d62377f6` 进了 `main`；
    而「尚未发布过」这句**仓库验不了** —— `docs/backlog/ideas.md` 里
    作者自己写着「它钉不住『这个版本号还能不能发』，那一格在库里不在仓里」，
    **那正是撞第五次时用过的同一个理由**。

    复核判退，改号到 **1.4.1**。
    **这一段留在这里，是因为下一次想那么改的人会是同一个人。**
    """

    identity = _profile()["identity"]
    assert (
        identity["version"],
        identity["release_id"][-12:],
        identity["content_hash"][:8],
    ) == ("1.4.12", "000100040012", "48c1fc39"), (
        f"三者对不上：{identity['version']} / {identity['release_id'][-12:]} / "
        f"{identity['content_hash'][:8]} —— 改了内容就把版本一起抬了，"
        "再跑 `python scripts/seal_profile_package.py profiles/internal-demo-sales`"
    )


def test_the_release_moved_with_this_teaching() -> None:
    """改了提示词就要抬版本号。

    ⚠️ **名字里带 `this_teaching` 是有意的**：同日落地的「一条说一件事」
    那一批里已经有一条 `test_the_release_moved_with_the_prompt`。
    两条重名时，探针只拿得到函数名，**分不出红的是哪一个**。

    ⚠️ **v10 被跳过，而那是有意的**：它属于在飞的那一支（一条说一件事）。
    **两份不同的提示词正文永远不许共用一个 release 串。**
    """

    assert PROMPT_RELEASE == "m1a-prompt-v17"
