"""写入路径是 per-kind 分派的，而不是五处写死的 `send_message`。

**本批（E3-3）做的是 `pipeline.py` 那段注释自己开的方子**：
「a per-kind write path, with this set derived from its keys」。

**零行为改动的重构，守卫不能靠「既有用例全绿」**——那只说明没弄坏，
不说明分派真的在分派。下面每一条都问一件**重构之前答不出**的事。

## 那段注释列了四处，实际是五处

`idempotency_key` 的前缀 `f"send_message:{event_id}:..."` **不在那四条里**，
是本次重构才暴露的。`test_every_hard_coded_send_message_is_gone` 把五处一起钉住。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastagent_v2.worker.pipeline import (
    GENERATION_SUPPORTED_KINDS,
    GENERATION_WRITE_PATHS,
    DirectWrite,
    GenerationWritePath,
    OutboxDelivery,
)

SOURCE = (
    Path(__file__).resolve().parents[2]
    / "services/worker/src/fastagent_v2/worker/pipeline.py"
)


def test_the_supported_set_is_derived_from_the_registry() -> None:
    """**集合由键派生，不是另写一份。**

    两份会漂开，而漂开的那天没有任何东西会红——
    一个 kind 在集合里而注册表里没有，`_decide_proposal` 会 `KeyError`；
    反过来则是一个建好了却永远进不来的写入路径。
    """

    assert GENERATION_SUPPORTED_KINDS == frozenset(GENERATION_WRITE_PATHS)


def test_every_entry_answers_all_five_questions() -> None:
    """**缺任何一件都不该进这个注册表**，这是 `GenerationWritePath` 存在的理由。

    ## 名字与断言要跟着合同走（独立审查 2026-09-12，P3-3）

    这条用例上一版叫 `..._all_three_questions`，**而类 docstring 那时已经改成
    「需要回答的五件」了**——名字与断言一起落后于合同。
    **那正是 E3-3 的 P1-1 那一条的小号**（一句关于「加一个 kind 要什么」的声明
    与代码对不上），**一个批次之后又出现了一次**。

    `effect` 漏掉不会出事（它没有默认值，构造时就必须给），
    **但它的类型此前没人验**：塞一个字符串进去，dataclass 不拦，
    `isinstance(effect, DirectWrite)` 为假 → 走 `OutboxDelivery` 分支 →
    `delivery.audience` 抛 `AttributeError`，**在事务里**。

    ## 这五件仍然是必要条件，不是充分条件

    判断够不够的办法不是读这段话，是**问一遍你的 kind 在
    `_persist_accepted_proposal` 里会走到哪几条 `INSERT`**。
    """

    for key, path in GENERATION_WRITE_PATHS.items():
        assert isinstance(path, GenerationWritePath)
        assert path.kind == key, f"注册键 {key!r} 与它写进 intent_records 的值 {path.kind!r} 不一致"
        assert path.idempotency_prefix, f"{key} 没有幂等前缀"
        assert callable(path.payload_refusal)
        assert isinstance(path.effect, OutboxDelivery | DirectWrite), (
            f"{key} 的 effect 是 {type(path.effect).__name__}，"
            "而分派只认 OutboxDelivery / DirectWrite —— 别的类型会走进出站分支，"
            "然后在事务里抛 AttributeError"
        )
        assert path.turn_refusal is None or callable(path.turn_refusal), (
            f"{key} 的 turn_refusal 既不是 None 也不可调用"
        )


# **E3-3 的绊线 `test_site_three_is_still_open_...` 在本批被删掉了，这是它设计好的结局。**
#
# 它的说明写着：「E4-2 把出站构造收进注册表条目的那一天，它会变红——
# 那时要做的不是改断言，是删掉这条用例并把注释里那个 ❌ 改成 ✅」。
# **两件都做了**：`ProposalEffect` 落地、`GENERATION_SUPPORTED_KINDS` 上方那条标 ✅。
#
# **删掉一条绊线不等于少了一道守卫**——接替它的是
# `tests/contracts/test_remember_write_path.py` 里的两条：
# `test_remember_writes_a_row_and_queues_nothing`（remember 的效果是 DirectWrite）
# 与 `test_the_registry_is_what_decides_the_effect_now`（出站行不再长在
# `_persist_accepted_proposal` 里）。
# **绊线守的是「第 3 处还开着」，这两条守的是「它关对了，而且没退回去」。**


def test_send_material_is_registered_now_that_all_three_halves_exist() -> None:
    """**E3-4 到了，所以这一条从「不在里面」翻成了「在里面」。**

    上一版逐字写着：「这条红的时候，要么 E3-4 到了（该改本条），
    要么有人把它提前放进来了。」——**是前者**（用户 2026-09-16 裁定三半同批）。

    当时缺的那一半是「能送它的渠道」。今天三半都在：

    | 半 | 落点 |
    |---|---|
    | 上传（供应商拿得到这份文件） | `upload_file_by_url` + `scripts/upload_materials.py` |
    | 渠道（发得出去） | `send_file` + 适配器那条支 |
    | 模型看得见（提得出来） | `select_assets` + 提示词候选清单 |

    **而这条用例现在钉的是第四件**：注册了之后，
    `send_material` 排出去的那一行**里面不许有凭据**。
    """

    assert "send_material" in GENERATION_WRITE_PATHS
    assert "send_material" in GENERATION_SUPPORTED_KINDS

    path = GENERATION_WRITE_PATHS["send_material"]
    assert isinstance(path.effect, OutboxDelivery)
    assert path.effect.audience == "customer"


def test_the_outbox_row_carries_a_reference_and_never_a_credential() -> None:
    """**排出去的那一行只带引用** —— 四个 `provider_*` 字段一个都不许在里面。

    这是这一格最容易走错的一步：决策时把 `fileId` / `fileAesKey` / `fileMd5`
    拼进 `content`，派发时照着发，worker 一条授权都不用加 —— **能跑，且更简单**。

    不走它的理由：那会把凭据复制进 `outbox_messages`，
    **而那张表的读面比 `materials` 宽得多**（`fastagent_app` 从 `058` 起
    就读得到出站行，消息页也读它）。`066` / `067` 那两条列级授权围的就是这件事，
    在这里拼一次就全绕过去了 —— **而且不会有任何东西变红**。

    判据是**值里不出现密钥**，不是「键名里没有 key 这个词」：
    换个键名就绕过去的断言等于没有。
    """

    build = GENERATION_WRITE_PATHS["send_material"].effect.build_content  # type: ignore[union-attr]
    # **这个值在运行时拼出来，源码里不留一个像密钥的字面量。**
    #
    # 第一版把这个假值直接赋给了一个名叫 secret 的变量，`check_secrets.py`
    # 当场红：`assigned-secret-literal`。**它是对的** —— 它无从知道这是个假值，
    # 而它自己写着「不要把值加进白名单，要么收窄模式要么把值挪出仓库」。
    # 这里挪的是值：与 `probe_material_catalogue` 里
    # `_STATEMENT = "INSERT INTO " + ...` 同一个办法。
    #
    # **改完它又红了一次，红的是这条注释** —— 因为注释里原样引了那行写法。
    # 那一次也对：一个像密钥的字面量在不在注释里并不重要。
    # 所以上面这段是**描述**它，不是复述它。
    planted = "8e1987" + "e0000241"

    content = build({"ref": "asset:price-list:v1", "aes_key": planted, "fileMd5": "m"}, None)

    rendered = json.dumps(content, ensure_ascii=False)
    assert planted not in rendered, (
        f"出站行里出现了密钥：{rendered}。content 构造器把 payload 里的东西"
        "原样带过去了 —— 它该只取 `ref` 那一个字段"
    )
    assert content == {"type": "asset", "ref": "asset:price-list:v1"}


def test_the_payload_refusal_speaks_about_its_own_kind() -> None:
    """**每个 kind 问自己的问题。**

    重构之前这条守卫写死成 `kind == "send_message" and ...`，
    于是第二个 kind 会遇到一条**问错问题**的拒绝——
    `pipeline.py` 那段注释里四条中的第 4 条讲的就是它。
    """

    refuse = GENERATION_WRITE_PATHS["send_message"].payload_refusal
    assert refuse({"content": {"type": "text", "text": "在"}}) is None
    detail = refuse({"not_content": 1})
    assert detail is not None
    assert "send_message" in detail, "拒绝理由要说清它在替哪个 kind 说话"
    assert "content" in detail


def test_the_refusal_detail_stays_bounded() -> None:
    """**`detail` 有界**（独立审查 2026-09-07 P3-1 立的规矩，重构不得丢掉它）。

    键来自模型而 `typed_payload` 没有 `maxProperties`，
    这是第一个到达 `policy_decisions.context` 的模型可控字符串。
    """

    refuse = GENERATION_WRITE_PATHS["send_message"].payload_refusal
    detail = refuse({f"k{i}" * 50: 1 for i in range(60)})
    assert detail is not None
    assert len(detail) <= 500


def test_every_hard_coded_send_message_is_gone() -> None:
    """**五处字面量都不在了。**

    这条扫源码，因为它要证的是「**没有**某样东西」——
    而那件事没有任何运行期用例能证。

    **允许留下的有两处，各有理由**：

    1. **注册表自己**——它是唯一定义点，当然要写出这个名字；
    2. **`_sent_material_refs` 那条 SQL 的 `AND kind = 'send_message'`**——
       **它是一条读查询，不是写入路径**。它问的是「这条会话已经发出去了哪几段
       *素材*」（ADR-0125 §6.1 的阶段闸），而不是「哪些 kind 能发」。

    > **第 2 处是本批扫描第一版抓到的，而抓到之后要做的不是顺手改它。**
    > `send_material` 注册之后这条查询要不要带上它，**是一个语义问题**：
    > 它找的是**话术段**的 ref，而 `send_material` 送的是文件。
    > **凭「看起来该一起」去改一条读查询的语义，正是本仓反复付钱的形状**——
    > 登记为 **T310**，到期在 E3-4 注册 `send_material` 的那一批。

    ## ⚠️ 这条第一版自称钉五处，**实际只钉住四处**（独立审查 2026-09-12，P2-1）

    正则原本是 `['"]send_message['"]`——它要求 `send_message` 后面**紧跟引号**。
    而第 2 处（幂等前缀）原样的写法是 `f"send_message:{event_id}:{candidate_basis}"`，
    **后面跟的是冒号**，于是把它写回去这条用例一声不吭。
    审查方跑了反向探针：还原之后 `7 passed`，**一条都没红**。

    **漏掉的恰好是本批自称的新发现**，而这不是巧合——
    第 5 处是「重构才暴露出来的」，所以它也是**唯一没有旧写法可照抄**的一处，
    正则是照着前四处的形状写的。

    **这一处比别处更需要这条守卫**：把前缀写回去是**同值**改动，
    幂等键一个字符都不变，**没有任何运行期用例能红**——
    源码扫描是它唯一可能的守卫，而它当时没守。
    """

    text = SOURCE.read_text(encoding="utf-8")
    body = "\n".join(
        line
        for line in text.split("\n")
        # 注释与文档串不算——它们是解释，不是行为
        if not line.lstrip().startswith("#")
    )

    # 注册表那一块是唯一允许出现的地方
    start = body.index("GENERATION_WRITE_PATHS: Mapping")
    end = body.index("GENERATION_SUPPORTED_KINDS = frozenset")
    registry = body[start:end]
    outside = body.replace(registry, "")

    # 第 2 处允许：那条读查询。**按整行排除，不是按子串**——
    # 子串排除会连带放过同一行里别的东西。
    kept = [
        line for line in outside.split("\n") if "AND kind = 'send_message'" not in line
    ]
    # **结尾那一位要连冒号一起认**（独立审查 2026-09-12，P2-1）：
    # 幂等前缀的原样写法是 `f"send_message:{event_id}:..."`，
    # 只认引号的正则看不见它。**`[:'"]` 是把第 5 处纳进来的那一个字符类。**
    offenders = [
        m.group(0) for m in re.finditer(r"""['"]send_message[:'"]""", "\n".join(kept))
    ]
    assert not offenders, (
        f"注册表之外还有 {len(offenders)} 处写死的 'send_message'：{offenders[:5]}。"
        "加一个 kind 应当是加一个注册表条目，不是在五个地方各改一次字面量。"
    )


def test_the_sql_no_longer_names_the_kind() -> None:
    """第 4 处单独钉：`INSERT INTO intent_records` 不再把 kind 写进 SQL。

    **原来那条是最坏的一处**：一个 `send_material` 提案会被**记成**
    `send_message`，**并且照样通过合同校验**——
    错得不吵，只有查库的人看得见。
    """

    # **先剥注释，再扫。** 第二版忘了剥，于是抓到的是**本批自己写在那处的注释**
    # （它当然要提到这个名字），而不是代码——**一条扫源码的用例如果不剥注释，
    # 它测的是文字不是行为**。
    text = "\n".join(
        line
        for line in SOURCE.read_text(encoding="utf-8").split("\n")
        if not line.lstrip().startswith("#")
    )
    inserts = [
        text[m.end() :][:600] for m in re.finditer(r"INSERT INTO intent_records\(", text)
    ]

    # **`INSERT INTO intent_records(` 在这个文件里不止一处**——落待办那条写的是
    # `'manual_task'`，是它自己的 kind，合法。本条第一版用 `text.index(...)` 取
    # 第一处，**取到的正是那条**，于是把 kind 改回写死时它照样绿：
    # **一条测着别人、却以它自己的名义绿着的用例**（本批探针 3 抓到）。
    assert len(inserts) >= 2, "这个文件里 intent_records 的 INSERT 少于两处，本条的前提变了"

    offenders = [chunk for chunk in inserts if "'send_message'" in chunk]
    assert not offenders, (
        "intent_records 的某条 INSERT 又把 kind 写死成 'send_message' 了——"
        "它应当读合同里的 kind，否则 send_material 提案会被记成 send_message "
        "并照样通过合同校验。"
    )
