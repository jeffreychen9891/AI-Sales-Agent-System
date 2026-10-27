"""每一个会写成本行的角色，`model_invocation_costs.role` 的 CHECK 都得认它。

## 它补的是什么（运维缺陷 12，2026-09-23，阻塞）

ADR-0149 给推理加了第四种角色 `SILENCE_CAPABILITY` —— 三处都写了它
（`request_shape` 定义、`inference_model` 传它、`model_policy.REALTIME_TIERS` 列它），
**而没有配套迁移把它加进那张表的 CHECK**。

stage 实测：

```
ERROR:  new row for relation "model_invocation_costs"
        violates check constraint "model_invocation_costs_role_check"
03:14:38  timer | failed | CheckViolation | attempt=5
03:16:37  timer | failed | CheckViolation | attempt=5
```

成本行写在回合事务里 → **整轮回滚** → 重试五次进死信。

## ⚠️ 而模型调用发生在写成本行之前

每一次重试都真的发出了一次请求、被供应商计了费，然后那一行成本被回滚掉。
运维本次实测 **10 次隔离 ≈ 10 次付了钱而账上没有行**。

`model_invocation_costs` 是对账的唯一来源，所以这不是「少了一行日志」——
它让「这个租户这个月花了多少」偏低，**而且偏低得账面自洽**。

## 这条路走过一次

`059` 给 `EMBEDDING` 做过同样的事，它的注释里写着同一句话。
**这次漏了，而漏掉它的代价与上次逐字相同** —— 所以这一条钉的不是
`SILENCE_CAPABILITY` 这一个名字，是**「加一个角色」与「让台账认它」不许分家**。

## 判据是「release 声明了它」，不是「今天有没有人调它」

⚠️ **这一条是写这份用例时被它自己抓出来改的。** 第一版按「实时分档」判，
而 `VISION` 当场把它顶红 —— `VISION` 今天**零调用方**，却在 CHECK 里。

那不是 CHECK 写错了。`cost.py` 逐字写着：
「`COMMITMENT_CAPABILITY` 与 `VISION` 今天没有调用方，但它们**声明了**
绑定与价目……**少算一个角色，兜出来的上界就是假的**。」

## 集合相等，不是包含

包含只挡得住「CHECK 少了一个」，挡不住「CHECK 多了一个连 release 都没声明的」——
而后者同样是坏的：一个词表里的值如果连声明都没有，读它的人会以为那条路存在。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastagent_v2.worker.model_policy import REALTIME_TIERS

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPOSITORY_ROOT / "infrastructure" / "migrations"

#: 不在 `REALTIME_TIERS` 里、而这张表仍然要认的角色。
#:
#: **判据是「release 声明了它」，不是「今天有没有人调它」** ——
#: `cost.py` 那段逐字写着：「`COMMITMENT_CAPABILITY` 与 `VISION` 今天没有
#: 调用方，但它们**声明了**绑定与价目。计数闸兜的是『未结算的钱』，
#: 而未结算的行不问是谁产生的 —— **少算一个角色，兜出来的上界就是假的**。」
#:
#: * `EMBEDDING` —— 检索层（`059`），不是实时分档、没有结构化输出契约，
#:   而它**确实**每回合最多写一行；
#: * `VISION` —— 今天零调用方，而 release 声明了它的绑定与价目。
#:
#: **列在这里而不是把断言松成「包含」**：松了就挡不住另一个方向 ——
#: 「CHECK 里多了一个连 release 都没声明的值」，
#: 那会让读它的人以为那条路存在。
NON_REALTIME_COST_ROLES = frozenset({"EMBEDDING", "VISION"})


def _check_constraint_roles() -> frozenset[str]:
    """CHECK 里现在允许哪几个 role —— **从最后一条动过它的迁移里读**。

    不在这里抄一份清单：抄一份就是同一条规则两个定义点，
    而那正是这条缺陷的形状。
    """

    touching = [
        path
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if "model_invocation_costs_role_check" in path.read_text(encoding="utf-8")
    ]
    assert touching, "没有任何一条迁移动过这个约束 —— 这条用例找错了名字"
    latest = touching[-1].read_text(encoding="utf-8")
    block = latest.split("ADD CONSTRAINT model_invocation_costs_role_check", 1)[1]
    body = block.split("CHECK (role IN (", 1)[1].split("))", 1)[0]
    return frozenset(re.findall(r"'([A-Z_]+)'", body))


def test_the_check_constraint_accepts_every_role_that_writes_a_cost_row() -> None:
    """**等值集合。** 少一个 = 整轮回滚；多一个 = 一条没人走的路看起来存在。"""

    allowed = _check_constraint_roles()
    writing = frozenset(REALTIME_TIERS) | NON_REALTIME_COST_ROLES

    assert allowed == writing, (
        f"只在 CHECK 里 {sorted(allowed - writing)}；"
        f"只在写入侧 {sorted(writing - allowed)}。"
        "\n**加一个角色要同时加一条迁移** —— 少了那一条，"
        "这个角色每跑一次就是一次整轮回滚，而模型的钱已经花掉了。"
    )


def test_the_silence_role_is_the_one_this_batch_added() -> None:
    """钉住本批补的那个名字，并说明它为什么会写这张表。

    ⚠️ **它不在 `REQUEST_PATH_ROLES` 里**（那一串是「一次客户事件请求里
    共享同一个信封的角色」），而它确实实时、确实有输出契约、确实写成本行 ——
    `COMMITMENT_CAPABILITY` 是同形的先例。
    上面那条等值断言的判据是**「release 声明了它」**，
    而不是「在不在请求路径上」，也不是「今天有没有人调它」。
    """

    assert "SILENCE_CAPABILITY" in _check_constraint_roles()
    assert "SILENCE_CAPABILITY" in REALTIME_TIERS


def test_the_migration_that_added_it_records_its_own_version() -> None:
    """⚠️ 这条是 `test_migration_versions` 的重跑者，而它在这里有自己的理由。

    本批那条迁移**只改一个约束**，跑第二遍是幂等的 ——
    所以「忘了记版本」在本地不会有任何症状，只会让它每次部署都重跑一遍。
    """

    latest = sorted(MIGRATIONS.glob("*.sql"))[-1].read_text(encoding="utf-8")
    assert "INSERT INTO schema_migrations(version)" in latest
