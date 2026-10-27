"""一条记忆还算不算数 —— **生命周期那一样，T299 关闭。**

## 这个模块补的是三样里的第三样

ADR-0131 M2 把记忆这件事切成两半：**字段清单是商家的，而平台拥有三样** ——
禁止项、上限、生命周期。前两样早就有归属：

| 平台拥有的 | 归谁执行 | 什么时候 |
|---|---|---|
| 禁止项（按 key 名）| `profiles/registry.py::_check_memory_fields` | 封版时 |
| 禁止项（按取值）| `runtime/memory_values.py::forbidden_value_reason` | 写入时 |
| 上限（能声明几个字段）| `merchant-profile.schema.json` 的 `maxItems: 8` | 封版时 |
| **生命周期** | **本模块** | **读取时** |

`ttl_days` 这个键 **2026-09-12 就在 schema 里了，而在本批之前它全仓两处出现、
两处都在 schema 文件自己身上**（一处是它的 description，一处是它的类型声明）——
**零消费方**。它自己的 description 逐字写着这件事：

> omitting it means no expiry is declared — **nothing expires a memory today**

本批之后那句话为假。**description 已改，并留了「上一版写的是什么」** ——
本仓最贵的那一类错是「声明了但不存在」，而它的反面是
「文档说某事不存在，而某一批让它存在了，却没人回去改那句话」。

## 两个数，以及它们为什么只能一起定

**用户 2026-09-20 裁定：默认「永不过期」，上限 365 天。**

T299 登记时写的是「**上界与默认值只能一起定**」，而那句话是对的 ——
它们答的是同一个问题的两半：

* **上限 365** 回答「商家最多能让一条记忆活多久」；
* **默认永不过期** 回答「商家什么都不说时，平台替他决定了什么」。

只定上限不定默认，等于让「省略」这件事继续没有含义；
只定默认不定上限，等于让商家写 `ttl_days: 36500` 也算数。

⚠️ **「默认永不过期」是一次有意的取舍，不是没想**：它让本批
**不改变任何一份现存配包的行为** —— 今天在架的配包一个 `ttl_days` 都没写
（`profiles/` 现读），所以本批落地当天，线上没有任何一条记忆会因此消失。
**过期这件事从商家主动声明的那一刻才开始发生。**

## 为什么是读时过滤，不是扫地机

**`ttl_days` 是商家当前配包的属性，不是那一行的属性。**

`054_contact_memories.sql` 的触发器逐字写着
`contact_memories rows cannot be un-tombstoned` —— tombstone **不可逆**。
而配包是天天在改的东西：商家把某个字段的 `ttl_days` 从 30 调回 365，
**那几条记忆应该回来**。把一个可改的配置烧进一个不可改的行状态，
换来的是一次写错 `ttl_days` 就永久丢数据。

**用户 2026-09-20 裁定：读时过滤。**

所以本模块**不写库、不删行、不改状态** —— 它只回答一个问题：
**「按商家今天的声明，这条记忆还算不算数」**。
行照旧躺在表里，`status` 照旧是 `active`；变的只是谁把它读回去。

⚠️ **由此而来的一件，必须一起做**：控制面屏幕上那条记忆仍然显示为 `active`，
而模型已经不用它了 —— **「这条还在用」与「它已经过期了」在屏幕上长得一样**。
本仓为这个形状付过一次钱（`manual_tasks.expires_at` 全仓零写者而读模型照样显示它，
`test_manual_task_owner_sla_expiry.py` 把它钉成了一个事实）。
所以控制面的读模型**同一批**多一个算出来的 `expired` 位，
`tests/contracts/test_a_remembered_fact_has_a_lifetime.py` 逐个消费方钉住。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

#: 商家能给一条记忆声明的最长寿命，**天**。
#:
#: **用户 2026-09-20 裁定 365。** 它与「省略 = 永不过期」是同一次裁定的两半，
#: 理由见本模块开头 —— 两个数只能一起定。
#:
#: ⚠️ **改这个数要同时改 schema 里那个 `maximum`**，两处是一个数
#: （`merchant-profile.schema.json` 的 `memory_fields.items.ttl_days`）。
#: `test_a_remembered_fact_has_a_lifetime.py::test_the_cap_is_one_number_in_two_places`
#: 把两处钉成相等 —— 写死在这里而 schema 忘了改，那一条会红。
MEMORY_TTL_DAYS_MAX = 365

#: profile 里那个数组的键名，以及每条里那两个键。
#: **与 `pipeline.py` / `app.py` 里的同名常量是同一个字面量**，
#: 而本模块不去 import 它们：那两处分属两个 service，互相不 import。
MEMORY_FIELDS_KEY = "memory_fields"
_FIELD_KEY = "key"
_TTL_DAYS_KEY = "ttl_days"


def ttl_days_by_field(profile: Mapping[str, Any]) -> Mapping[str, int]:
    """这份 profile 给哪几个字段声明了寿命，各是几天。

    **只收声明过 `ttl_days` 的那几个** —— 没声明的字段根本不在返回的映射里，
    而那正是「省略 = 永不过期」在代码里的样子：
    `is_expired` 查不到这个键，就答「没过期」。

    ⚠️ **不要把没声明的填成一个默认天数** ——
    那会把「商家没说」和「商家说了很久」变成同一件事，
    而这两件在产品上不是一回事：前者是「平台不替你决定」，
    后者是「商家决定了一个很长的期限」。

    **上限在这里不拦** —— schema 的 `maximum` 在封版时就拦掉了，
    一份过得了闸的配包不会带着 366 走到这里。本函数照单全收，
    是因为**一个在读取路径上偷偷改小的值，会让「屏幕上写 400 天、
    实际按 365 天算」成为一件没有任何东西会说的事**。
    """

    declared = profile.get(MEMORY_FIELDS_KEY) or []
    lifetimes: dict[str, int] = {}
    for entry in declared:
        if not isinstance(entry, Mapping):
            continue
        ttl = entry.get(_TTL_DAYS_KEY)
        if not isinstance(ttl, int) or isinstance(ttl, bool):
            # `bool` 是 `int` 的子类 —— `ttl_days: true` 会被 schema 拦掉，
            # 而这一层不该因为上游拦过就假设它拦过。
            continue
        key = entry.get(_FIELD_KEY)
        if not isinstance(key, str) or not key:
            continue
        lifetimes[key] = ttl
    return lifetimes


def is_expired(
    *,
    field_key: str,
    created_at: datetime | None,
    now: datetime,
    lifetimes: Mapping[str, int],
) -> bool:
    """按商家今天的声明，这条记忆还算不算数。

    **`created_at` 是那条 active 行自己的时间，而它就是这条事实的年龄** ——
    `054` 的行不可变，改一个字段的值走的是「旧行 `superseded` + 新行」
    （`app.py` 那条 `UPDATE … SET status = 'superseded'` 紧跟一条 INSERT），
    所以 active 那一行的 `created_at` 正是「这个值是什么时候写的」。
    ⚠️ **不是 contact 的创建时间，也不是第一次记住这个字段的时间** ——
    商家更新了一次，这条事实就重新算年龄，而那是对的：
    「他上个月说他要 A」与「他今天说他要 A」不是同一条事实的新旧两版，
    是两条事实。

    **`created_at` 为 `None` 时答「没过期」** —— 那一列是 `NOT NULL DEFAULT now()`，
    所以 `None` 只可能来自一个没取这一列的查询。
    **那种情况下答「过期了」会静悄悄吞掉记忆**，而答「没过期」至多是漏过滤一次；
    两种错的代价不对称，所以这里选后者。
    ⚠️ 真正该防的是「有个调用点忘了取 `created_at`」，
    而那条由 `test_every_reader_asks_for_created_at` 按 AST 钉住，不靠这里。
    """

    ttl = lifetimes.get(field_key)
    if ttl is None:
        # 商家没给这个字段声明寿命 = 永不过期（用户 2026-09-20 裁定的那一半）。
        return False
    if created_at is None:
        return False
    if created_at.tzinfo is None:
        # `timestamptz` 读回来本该带时区；不带的当 UTC，
        # 而不是拿本机时区去比 —— 那会让同一条记忆在两台机器上过期时间不同。
        created_at = created_at.replace(tzinfo=UTC)
    return now - created_at > timedelta(days=ttl)


def live_memories(
    rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    lifetimes: Mapping[str, int],
) -> tuple[Mapping[str, Any], ...]:
    """把过期的那几条滤掉，**顺序不动**。

    引擎那两处共用它：一处把记忆喂进提示词，一处判「这个字段的槽位占了没」。
    **两处都要过滤，而这不是对称的好看** ——
    只滤前者的话，一条过期的记忆仍然占着槽位，新的 `remember` 写不进去，
    **而没有任何东西会变红**：模型看不到那条旧事实，也写不成新的，
    于是那个字段从此空着。
    """

    return tuple(
        row
        for row in rows
        if not is_expired(
            field_key=str(row.get("field_key") or ""),
            created_at=row.get("created_at"),
            now=now,
            lifetimes=lifetimes,
        )
    )
