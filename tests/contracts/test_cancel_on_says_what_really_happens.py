"""`cancel_on` 那一格说的，与平台真做的，**今天一致 —— 因为它说的是「我不读你」**。

## 这份文件钉的是一次「承认」，不是一次修复

用户 2026-09-16 裁定（T358）：**承认平台的行为，把声明改成它真的是什么**。
量出来的事实是：

| 值 | 执行者 | 读声明吗 |
|---|---|---|
| `customer_message` | `CUSTOMER_MESSAGE_NEW_GENERATION` | ❌ 无条件 |
| `frame_exit` | `TARGET_FRAME_EXITED` | ❌ 无条件 |
| `human_takeover` | `CONVERSATION_TAKEN_OVER` | ❌ 无条件 |
| `profile_repin` | `PROFILE_RELEASE_REPINNED` | ❌ 无条件 |
| `commitment_fulfilled` | **一个都没有** | — |

**所以「零消费方」这句话要说准**：零消费方的是**那张声明**；
而五个条件里四个各有一处无条件的执行者。两件事的处置完全不同，
而 T358 第一版把它们说成了一件 —— 这份文件的存在有一半是为了不再说错。

## 它检查三件

1. **那四个原因码还在**（各自那一处取消还活着）；
2. **`commitment_fulfilled` 仍然没有执行者，而且没人把承诺置成 `fulfilled`**
   —— 一根绊线：兑现那一步哪天落地，它会红并指着 T358 说「把那条催单接上」；
3. **契约那一格真的说了这件事** —— 否则下一个配包作者会以为自己写的会生效。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "packages" / "contracts" / "schemas" / "merchant-profile.schema.json"

#: `(原因码, 哪个文件写它, 它对应 cancel_on 的哪个值)`。
#:
#: **一张分类表**：新增一处取消而不进这张表，最后一条会红并指着原因码说是谁。
UNCONDITIONAL_CANCELLATIONS = [
    (
        "CUSTOMER_MESSAGE_NEW_GENERATION",
        "services/worker/src/fastagent_v2/worker/pipeline.py",
        "customer_message",
    ),
    (
        "TARGET_FRAME_EXITED",
        "services/worker/src/fastagent_v2/worker/pipeline.py",
        "frame_exit",
    ),
    (
        "CONVERSATION_TAKEN_OVER",
        "services/api/src/fastagent_v2/api/app.py",
        "human_takeover",
    ),
    (
        "PROFILE_RELEASE_REPINNED",
        "services/api/src/fastagent_v2/profiles/binding.py",
        "profile_repin",
    ),
]

#: 那个今天什么都没有的值。
WITHOUT_AN_EXECUTOR = "commitment_fulfilled"


def _cancel_on() -> dict:
    document = json.loads(SCHEMA.read_text(encoding="utf-8"))
    rule = document["properties"]["target_frames"]["items"]["properties"]["time_rules"]["items"]
    return rule["properties"]["cancel_on"]


def test_the_four_unconditional_cancellations_are_still_there() -> None:
    """四处取消各自还在自己那份文件里。

    **判据是原因码写在哪个文件**，不是「全仓有这个词」——
    原因码搬了家意味着那条取消的时机变了，而那正是要被看见的事。
    """

    for reason, where, _ in UNCONDITIONAL_CANCELLATIONS:
        source = (ROOT / where).read_text(encoding="utf-8")
        # **判据是那条 UPDATE 里写着它**，不是「这个文件提到过它」。
        # 反向探针实测：`CONVERSATION_TAKEN_OVER` 在 `app.py` 里出现 **3 次**
        # （另外两处是审计与读查询），所以只写 `reason in source` 的第一版，
        # 在那条取消被改名之后**照绿**。
        statement = f"invalidation_reason = '{reason}'"
        assert statement in source, (
            f"{where} 里没有 {statement} 了 —— 那条取消要么没了，要么改了名。"
            " 两种都要去核 T358 那张表与契约里 `cancel_on` 的描述。"
        )


def test_the_declared_vocabulary_and_the_executors_still_line_up() -> None:
    """词表里的五个值 = 四个有执行者的 + 一个没有的。

    **集合相等，不是包含**：词表里多一个值而没人管它，正是 T358 登记的形状；
    少一个值则意味着某处取消对应的那个声明没了。
    """

    declared = set(_cancel_on()["items"]["enum"])
    accounted = {value for _, _, value in UNCONDITIONAL_CANCELLATIONS} | {WITHOUT_AN_EXECUTOR}

    assert declared == accounted, (
        f"`cancel_on` 的词表是 {sorted(declared)}，而这份文件记着的是 {sorted(accounted)}。"
        " 新增的那个值要么进 UNCONDITIONAL_CANCELLATIONS（有执行者，写明是哪一处），"
        " 要么说明它今天没有执行者 —— 后者是又一条 T358。"
    )


def test_nothing_fulfils_a_commitment_yet_so_that_value_still_does_nothing() -> None:
    """**绊线**：没有任何东西把承诺置成 `fulfilled`，所以那个值今天不可能生效。

    `synthetic-carrier` 的两条承诺规则**声明了它** —— 而声明不会让那条催单
    在承诺兑现时停下来。

    **它红的时候是好事**：兑现那一步落地了。那一刻要做两件 ——
    给 `commitment_fulfilled` 接上取消（`scheduled_events.commitment_id` 在 11d
    已经接了外键，join 得到），并把 T358 与契约里那一格的描述改掉。
    """

    # **逐行判，不逐文件判。** 反向探针实测：第一版写的是
    # 「这个文件里有 `'fulfilled'` 而没有 `NOT IN`」—— 而 `commitment_ledger.py`
    # 里本来就有一句 `NOT IN ('fulfilled', ...)` 的读过滤，
    # **于是整份文件被跳过，往它里面加一个真的写入方也照绿**。
    # 一个文件级的启发式，挡住的恰好是最该被看见的那份文件。
    writers = []
    for root in ("packages", "services"):
        for path in (ROOT / root).rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "'fulfilled'" not in line or "NOT IN" in line:
                    continue
                # 写入侧的形状：`SET status = 'fulfilled'` / `status = 'fulfilled'`。
                if "status" in line and "=" in line:
                    writers.append(f"{path.relative_to(ROOT)}:{number}")

    assert not writers, (
        f"有人开始把承诺置成 fulfilled 了：{writers}。"
        " **去给 `commitment_fulfilled` 接上取消** —— 在那之前，"
        " 一条已经兑现的承诺仍然会在到期时催客户一次。并把 T358 与契约那一格改掉。"
    )


def test_the_contract_says_the_platform_does_not_read_this_field() -> None:
    """契约那一格必须自己说清这件事。

    **这一条不是文案检查**：配包作者读的就是这一格。它不说，
    作者就会以为自己写的 `["frame_exit"]` 意味着「客户回消息时不取消」——
    而实际会取消。

    判据落在**内容**上（四个原因码 + 那个没有执行者的值），
    不落在「有没有 description 这个键」—— 后者换句话就看不见了。
    """

    described = _cancel_on().get("description") or ""
    assert described, "`cancel_on` 又没有描述了 —— T358 的处置就是补它"

    for reason, _, _ in UNCONDITIONAL_CANCELLATIONS:
        assert reason in described, f"描述里没点名 {reason}，读的人查不到是哪一处在取消"
    assert WITHOUT_AN_EXECUTOR in described
    assert "T358" in described, "描述里要留得下登记号，否则读到它的人不知道去哪看后续"
