"""`memory_fields` 的三道封版检查（E4-1b，ADR-0131 M2）。

**平台在记忆上只拥有三样**：禁止项、上限、生命周期。清单本身是商家的。
这一族用例要证明的正是这个分界——**而不是"平台检查了字段是否合理"**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.profiles.registry import (
    MEMORY_FIELDS_KEY,
    MEMORY_FORBIDDEN_SUBSTRINGS,
    ProfilePackageError,
    load_package,
)

from tests.contracts.test_reference_grammar import (  # noqa: F401
    _package_with,
    scratch_repository,
)

REPO = Path(__file__).resolve().parents[2]


def _with_memory_fields(
    directory: Path, entries: list[dict[str, Any]], *, can_remember: bool = True
) -> Path:
    """一份声明了 `memory_fields` 的包。

    **默认同时允许 `remember`**（2026-09-18 起封版要求）：
    声明了要记而没有任何打法能记，那份声明一条都不会被写进去 ——
    商家以为它生效了。`can_remember=False` 是给那条用例用的反例。
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        document[MEMORY_FIELDS_KEY] = entries
        if can_remember:
            body = next(iter(document["strategies"].values()))
            kinds = body.setdefault("allowed_intent_kinds", [])
            if "remember" not in kinds:
                kinds.append("remember")

    return _package_with(directory, mutate)


def test_a_merchant_vocabulary_publishes(scratch_repository: Path) -> None:  # noqa: F811
    """**安全带，也是这一族的主张本身。**

    三个行业毫不相干的字段名必须都能封版——平台**不**判断清单是否合理。
    没有这一条，下面三条可以靠"拒绝一切 memory_fields"一起变绿。
    """

    path = _with_memory_fields(
        scratch_repository / "ok",
        [
            {"key": "ring_size", "description": "What ring_size holds."},
            {"key": "frequent_cities", "description": "What frequent_cities holds."},
            {"key": "child_grade", "description": "What child_grade holds."},
        ],
    )
    load_package(path)


def test_an_absent_declaration_publishes(scratch_repository: Path) -> None:  # noqa: F811
    """不记任何东西的商家不必声明这个键——它是可选的，在架包一个都不能被拒。"""

    path = _package_with(scratch_repository / "absent", lambda d, m: None)
    load_package(path)


def test_a_forbidden_category_is_refused(scratch_repository: Path) -> None:  # noqa: F811
    """禁止项绊线真的在拒。"""

    path = _with_memory_fields(
        scratch_repository / "forbidden", [
            {"key": "id_card_no", "description": "What id_card_no holds."},
        ]
    )
    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)
    message = str(caught.value)
    assert "id_card" in message
    assert "tripwire" in message, "拒绝理由必须说清它是绊线不是合规保证"


def test_the_tripwire_is_documented_as_bypassable(scratch_repository: Path) -> None:  # noqa: F811
    """**这一条断言的是本批最容易被误读的那件事。**

    绊线匹配的是商家自己取的名字。一个改叫 `zjhm`（证件号码的拼音首字母）的
    声明**会通过**——这不是缺陷，是这道闸能力的边界。

    **把它写成一条会红的用例，是为了让"将来有人以为平台保证了合规"这件事
    必须先改掉一条明确写着相反意思的断言。**
    """

    path = _with_memory_fields(scratch_repository / "bypass", [
        {"key": "zjhm", "description": "What zjhm holds."},
    ])
    load_package(path)  # 通过，且这是有意的

    source = (
        REPO / "services/api/src/fastagent_v2/profiles/registry.py"
    ).read_text(encoding="utf-8")
    assert "tripwire, not a compliance guarantee" in source, (
        "这道闸的边界必须写在它旁边；删掉那句话之前先想清楚"
    )


def test_a_duplicate_key_is_refused(scratch_repository: Path) -> None:  # noqa: F811
    """JSON Schema 表达不了"数组里对象的某个字段互不相同"。"""

    path = _with_memory_fields(
        scratch_repository / "dup", [
            {"key": "ring_size", "description": "What ring_size holds."},
            {"key": "ring_size", "description": "What ring_size holds."},
        ]
    )
    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)
    assert "twice" in str(caught.value)


def test_the_count_ceiling_is_eight_and_it_comes_from_the_share(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """**上限是 8，而 8 是从「记忆在提示词里占多少份额」反推的。**

    ## 这条用例换掉了哪一条

    上一版叫 `test_there_is_no_count_ceiling_and_that_is_a_ruling`，
    钉的是「三十二条声明今天必须能封版」。**它自己写着到期条件**：

    > 哪天它红了，要么有人把上限加回来了，**要么 E4-5 到了**
    > —— 两种情况都先读 T300。

    **E4-5 到了**（记忆值进理解步与计划步，2026-09-18），
    而 T300 的到期条件写死了这一批要同时定三件：
    份额、由份额反推的条数、schema `maxItems`。**三者只能一起定。**

    ## 算式

    | | |
    |---|---|
    | 份额 | `memory_prompt.REMEMBERED_BYTES` = **1,200** 字节／每个提示词 |
    | 一条真实记忆 | 值 ≤40 汉字 = 120 + 键约 16 + JSON 那一圈 24 ≈ **160** 字节 |
    | 1,200 ÷ 160 | **7.5 → 8** |

    ## ⚠️ 它刻意不是可证最坏值

    被撤掉那道闸正是按最坏算的（1024 字符中文 = 6,148 字节 → 上限 **3**），
    而用户 2026-09-12 裁定：**「那是一次会改变产品形状的收窄，不是一次修正。」**
    所以这里按真实一条记忆的量级算，**而超出份额的那些不再安静消失** ——
    `memories_for_prompt` 把装不下的回成一个 `memories_not_shown` 计数。

    ## 它真的走一次 `load_package`

    上一版的第一版**不做它 docstring 声称的事**（独立复审 2026-09-12 N-1）：
    body 只有一句关于 `prompt_bytes` 的断言，而复审方把
    `if len(fields) > 4: raise` 加回 `_check_memory_fields`，**八条用例全绿**。
    **两个方向都要走一次真的封版**，所以下面是 8 条能过、9 条被拒。
    """

    from fastagent_v2.runtime.memory_prompt import REMEMBERED_BYTES  # noqa: PLC0415

    assert REMEMBERED_BYTES == 1_200, "份额变了 —— 上限那个 8 要跟着重算，见 T300"

    fits = [{"key": f"field_{index}", "description": "fixture field"} for index in range(8)]
    load_package(_with_memory_fields(scratch_repository / "eight", fits))

    one_too_many = [{"key": f"field_{index}", "description": "fixture field"} for index in range(9)]
    path = _with_memory_fields(scratch_repository / "nine", one_too_many)
    # **判据是那一对，不是那句话**：8 条过、9 条拒 —— 上限就在这两次之间。
    # 合同违例的消息不点名是哪一条规则（它只说「不满足 merchant-profile 合同」），
    # 所以拿消息当判据会把「别的地方写错了」也读成「上限生效了」。
    with pytest.raises(Exception, match="merchant-profile contract"):
        load_package(path)


def test_the_ruling_rests_on_a_measured_fact() -> None:
    """上一条钉的是行为；这一条钉的是它**依据的那个事实**。

    两件分开，因为它们会因不同的原因变假：上一条红=有人加回了上限；
    本条红=**被撤掉那道闸的前提变了**，那时 T300 的算术要重做。

    ---

    **2026-09-12：本条红过一次，而红得对 —— 它钉的是一个随口径而变的常数。**

    原来的断言是 `text_cost("中" * 1024) > 4 * 1024`，也就是「中文至少 4 字节
    一个字」。那是**转义口径**下的事实（`\\uXXXX`，6 字节一个字，1024 字 → 6,148）。
    「字节口径回正」那一批把 `text_cost` 改回**线路**口径
    （UTF-8，中文 3 字节一个字，1024 字 → 3,076），于是 `3076 > 4096` 为假。

    **两批各自的门禁都不可能抓到它**：那一批树上没有本文件，本文件所在的树上
    没有那个改动。**只有合并后的基线会红** —— 与 `purge_tenant_data` 那一类
    「git 不报冲突而合起来红」同族。

    **而 T300 的裁定没有翻。** 改的是数，不是结论 —— 理由见下面第三条断言：
    被撤掉那道闸**放行**的最坏情形仍然远超整个请求预算。

    | 取值（1024 字符） | 转义口径（旧） | **线路口径（今天）** | 真实门槛 |
    |---|---:|---:|---|
    | ascii | 1,028 | 1,028 | n≥16 |
    | **中文** | ~~6,148~~ | **3,076** | **n≥6** |
    | emoji | ~~12,292~~ | 4,100 | n≥4 |

    **所以这一条改成钉「前提」而不是钉「一个字节数」** ——
    前提是「`length()` 数字符，所以 1024 这个上限根本不约束字节」，
    而它的后果是「那道按字节读 1024 的闸只在 n≥17 时拒，
    而 n≥6 就已经装不下」。**后者不随口径翻转**，前者才是 T300 的依据。
    """

    from fastagent_v2.profiles.registry import MAX_MODEL_INPUT_BYTES
    from fastagent_v2.runtime.prompt_bytes import text_cost

    characters = 1024  # = 054 的 CHECK (length(field_value) BETWEEN 1 AND 1024)

    # 一、**字符不是字节**，而这正是被撤掉那道闸读错的地方。
    #     线路口径下中文 3 字节一个字；这一条只要求「明显超过 1 字节」，
    #     **不钉具体倍数** —— 倍数随口径变，而「超过」不变。
    one_chinese_value = text_cost("中" * characters)
    assert one_chinese_value > 2 * characters, (
        f"中文取值 {one_chinese_value} 字节 / {characters} 字符 —— "
        "单位换算没了，那 1024 的字符上限就真的能当字节上限读，重读 T300"
    )

    # 二、把今天的读数写出来，免得下一个人重新量。
    #     **这一条用区间而不是等号**：`text_cost` 的骨架开销将来可能变几字节，
    #     而那不该让本用例红。
    assert 3 * characters <= one_chinese_value <= 3 * characters + 64, (
        f"中文每字的线路成本不再是 3 字节（实测 {one_chinese_value}）—— "
        "T300 那张表与下面那条门槛都要重算"
    )

    # 三、**本条真正的依据**：被撤掉那道闸**放行**的最坏情形装不下。
    #     那道闸把 1024 当字节读，于是只在 n≥17 时拒 —— 也就是说
    #     **16 条中文声明它是放行的**，而 16 条中文声明是整个请求预算的三倍。
    #     这一条在转义口径下也成立（那时是六倍），**所以它不随口径翻转**。
    worst_case_the_old_gate_allowed = 16 * one_chinese_value
    assert worst_case_the_old_gate_allowed > MAX_MODEL_INPUT_BYTES, (
        f"16 条中文声明 {worst_case_the_old_gate_allowed} 字节已经装得进 "
        f"{MAX_MODEL_INPUT_BYTES} —— 那道按字节读 1024 的闸不再是无效的，"
        "T300「闸已撤」这个处置要重看"
    )


def test_the_migration_ceiling_is_pinned_once_the_migration_is_here() -> None:
    """**条件式：`054` 不在树上就什么也不证，在了就真的钉住。**

    **原来的写法是 `assert not migration.exists()`**，它在 E4-1 合进 `main` 的
    那一刻必红——**而红的是 `main` 的合并后基线，账记在另一批头上**，
    还会按熔断把下一批升档（独立审查 2026-09-12 P1-3）。

    作者当时的意图是对的（不要一条真空通过的等式），**但这个形状被这个形状支配**：
    `054` 没到时两者等价（都什么也不证），到了则**自动变成真的等式**，
    而 `054` 若改了那个数它照样红。**现在这条在每一个分支上都不劣于它。**

    **钉的是「字符」不是「字节」**——`length()` 数字符，这正是被撤掉那道闸
    读错的地方（T300）。
    """

    migration = REPO / "infrastructure/migrations/054_contact_memories.sql"
    if not migration.exists():
        pytest.skip("054 尚未与本批落到同一棵树上；到了就会真的检")

    text = migration.read_text(encoding="utf-8")
    assert "CHECK (length(field_value) BETWEEN 1 AND 1024)" in text, (
        "054 里那条取值上限变了，而 T300 记的算术以它为底数——两处要一起改"
    )
    assert "octet_length" not in text.split("field_value")[1][:200], (
        "054 改用了 octet_length：单位从字符变成了字节，T300 的算术要重做"
    )


def test_the_forbidden_list_is_the_platforms_not_the_merchants() -> None:
    """**ADR-0125 §五 的反面，要说破。**

    §五 禁止把**商家的流程分类**（topic / 阶段）写进平台代码。
    本表是**平台自己的禁止边界**，ADR-0131 M2 逐字把它判给平台。
    这一条钉住四类都在，免得有人"顺手精简"。
    """

    joined = " ".join(MEMORY_FORBIDDEN_SUBSTRINGS)
    for category in ("id_card", "payment", "health", "religion"):
        assert category in joined, f"ADR-0131 M2 点名的四类里 {category!r} 不在表里"


def test_declaring_what_to_remember_without_anyone_able_to_remember_is_refused(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """**声明了要记，就得有哪个打法真的能记**（2026-09-18）。

    一份声明了 `memory_fields` 而没有任何打法允许 `remember` 的配包，
    **封得了版、跑得了对话，而那份声明一条都不会被写进去** ——
    商家以为「我要记这几件事」已经生效了，而写入路径永远不会被授权。

    **两头都在、中间没接**，而在这一道之前没有任何东西会红。

    它是补演示配包那六个字段时发现的：**我差点只改配包，不问这一句。**
    """

    path = _with_memory_fields(
        scratch_repository / "nobody-can-remember",
        [{"key": "ring_size", "description": "What ring_size holds."}],
        can_remember=False,
    )

    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)

    assert "no strategy lists 'remember'" in str(caught.value)


def test_the_shipped_demo_package_declares_and_can_remember() -> None:
    """**在架那份演示包两头都在** —— 这一条钉的是配包本身，不是闸。

    闸只管「说得通」；这一条管「演示这个商家真的用上了它」。
    顺序就是优先级：装不下时按它截断，而截掉的会被数出来。
    """

    import json  # noqa: PLC0415

    document = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "profiles"
            / "internal-demo-sales"
            / "profile.json"
        ).read_text(encoding="utf-8")
    )

    keys = [str(entry["key"]) for entry in document[MEMORY_FIELDS_KEY]]
    assert keys == [
        "industry",
        "pain_point",
        "team_size",
        "current_tool",
        "decision_role",
        "timeline",
    ], "六个字段或它们的顺序变了 —— 顺序就是这个商家的优先级"

    remembering = [
        name
        for name, body in document["strategies"].items()
        if "remember" in (body.get("allowed_intent_kinds") or [])
    ]
    assert sorted(remembering) == ["advance_goal", "answer_question", "ask_slot"], (
        f"能记的打法变了：{sorted(remembering)}"
    )
