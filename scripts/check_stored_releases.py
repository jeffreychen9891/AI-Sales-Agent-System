"""每一行已经存库的 `profile_releases`，还过得了今天的 `merchant-profile` 契约吗。

## 它是干什么用的：**一次收紧的前置，不是一道门禁**

批 11（2026-09-16）把 `merchant-profile.outcome_rules` 从 `{"type": "object"}`
定型成带三个词表的结构。**那是一次对已存数据的追溯收紧** ——
判据是本仓自己立的那条机械问法（批 2 §四）：

```sh
grep -rn 'validate_contract("merchant-profile"' --include=*.py services/ packages/
```

现读**三处**，而其中两处在**读路径**上：

| 处 | 什么时候跑 |
|---|---|
| `services/worker/src/fastagent_v2/worker/pipeline.py:2008` | **每一个入站回合**验一次库里那一行 |
| `services/api/src/fastagent_v2/profiles/resolver.py:115` | 每一次装载验一次 |
| `services/api/src/fastagent_v2/profiles/registry.py:1462` | 封版（写入侧，当场生效） |

**所以一行过不了新 schema 的 release，不是一次封版被拒，
是那个租户的每一条消息 500。** 而它在本仓的历史里已经是第三次了
（批 2 的 `slots` · D2 的 `information_slots` · 本批的 `outcome_rules`），
前两次也都只写了封版那一侧。

**跑 `063` 之前跑这个**：`063` 一落地，台账开始写、读路径开始跑，
上面那一问就从纸面变成线上。这也是复核窗口把它排在 11d 之前的理由。

## 它为什么不进 `ci.sh`

门禁那套栈是**每次现建的**，里面的 release 是播种夹具写进去的那几行 ——
**它们必然是今天的形状**，所以在门禁里跑这个恒绿，恒绿的东西不是检查。
要检的是**真实环境里躺着的那些行**，而那台库门禁够不着。

它因此是一个**要有人拿着 URL 去跑**的脚本：

```sh
FASTAGENT_MIGRATION_DATABASE_URL=... python scripts/check_stored_releases.py
```

退出码：`0` = 每一行都过；`1` = 有行过不了（逐行打出 tenant / profile_id /
version 与**第一条**错**的形状**（不是错的内容）—— 一份不合规的 release 可能同时有几十条子错，
而它们指的是同一件事，打全会把真正要看的那几行冲掉；**不打 profile 正文**）；
`2` = 连不上、没有那张表、或者那张表是空的。

**不打正文**是有意的：release 里有商家的话术，而这个脚本的输出多半会被
贴进部署记录（`AGENTS.md` 那条「脱敏边界」同源）。要看正文去库里看。

**而第一版没做到这件事，是跑它的人发现的（2026-09-16）**：那一版打的是
`error.message[:160]`，而 jsonschema 在 `maxLength` 上的消息体**就是那个
超长的值本身**，于是两行 FAIL 各打出了约 160 字商家 goal 原文 ——
**一句写在 docstring 里的承诺，和一行做相反事情的代码**。
现在走 `describe()` 的正向白名单（关键字 + schema 侧的约束 + 值的类型长度），
钉住它的是 `tests/contracts/test_stale_release_report_is_redacted.py`。
"""

from __future__ import annotations

import io
import os
import sys

from fastagent_v2.runtime.contracts import describe_validation_error, load_schema
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from psycopg import Connection


def stale_releases(connection: Connection) -> list[tuple[str, str]]:
    """库里过不了今天契约的那些 release，`[(哪一行, 第一条错), ...]`。

    **与「连库」分开是被 `profile_releases` 的形状逼出来的**：那张表是
    append-only（`006` 的触发器 `BEFORE UPDATE OR DELETE` 无条件抛），
    所以一条往里塞测试数据的用例**清理不掉自己塞的行**。

    拆开之后，用例可以在**一个不提交的事务**里塞进去、当场问这个函数、再回滚 ——
    子进程看不见未提交的数据，而这个函数看得见。
    **这不是为测试让路**：`main()` 负责「连哪台库、怎么退出」，
    这个函数负责「什么算过不了」，两者本来就是两件事。
    """

    rows = connection.execute(
        """
        SELECT tenant_id, profile_id, version, profile
          FROM profile_releases
         ORDER BY tenant_id, profile_id, version
        """
    ).fetchall()

    validator = Draft202012Validator(load_schema("merchant-profile"))
    bad: list[tuple[str, str]] = []
    for tenant_id, profile_id, version, profile in rows:
        errors = sorted(validator.iter_errors(profile), key=lambda error: list(error.path))
        if not errors:
            continue
        error = errors[0]
        where = "/".join(str(part) for part in error.path) or "(顶层)"
        bad.append((f"{tenant_id} {profile_id}@{version}", f"{where}: {describe(error)}"))
    return bad


#: 这些 jsonschema 关键字的 `validator_value` 来自**我们自己的 schema**，
#: 打出来不泄露任何商家内容；而 `enum` 那一类的取值表同样是 schema 里的。
#: **`instance` 与 `message` 一个都不在这里**，理由见 `describe`。
def describe(error: ValidationError) -> str:
    """一条校验失败的**不含正文**描述。

    **实现搬去了 `runtime.contracts.describe_validation_error`**（2026-09-16）——
    第二个调用方出现了（`profiles/resolver.py` 的 `releases()`），
    而两处各写一遍迟早只改一处。那段「它被一次真实泄露逼出来」的记述跟着搬了过去。

    **搬家时输出多了一样**：前缀 `error.json_path`（出事的位置，如
    `$.target_frames[0].goal`）。**位置不含值** —— 白名单那张表因此多一行。
    这份脚本的输出格式随之变化，那是有意的：它原来说得出「哪一条约束」，
    说不出「哪一格」。

    这个薄壳留着，是因为本文件的两处调用点和一条用例都按这个名字读它。
    """

    return describe_validation_error(error)


def count_releases(connection: Connection) -> int:
    row = connection.execute("SELECT count(*) FROM profile_releases").fetchone()
    return int(row[0]) if row else 0


def main() -> int:
    url = os.getenv("FASTAGENT_MIGRATION_DATABASE_URL", "").strip()
    if not url:
        print("FASTAGENT_MIGRATION_DATABASE_URL 没设 —— 这个脚本要连的是**将要跑迁移的那台库**")
        return 2

    with Connection.connect(url) as connection:
        exists = connection.execute(
            "SELECT to_regclass('public.profile_releases') IS NOT NULL"
        ).fetchone()
        if not exists or not exists[0]:
            print("这台库里没有 profile_releases —— 迁移还没跑过？")
            return 2
        total = count_releases(connection)
        bad_rows = stale_releases(connection)

    if not total:
        # **空库是一个答案，不是一次通过。** 一个连错了库、或者连上了一台
        # 还没播种的库，同样会走到这里 —— 那时「零行全过」读起来像「没问题」。
        print("profile_releases 一行都没有 —— 确认连的是不是那台库")
        return 2

    for who, why in bad_rows:
        print(f"FAIL {who}  {why}")

    print(f"checked={total} failed={len(bad_rows)}")
    if bad_rows:
        print(
            "上面这些 release 过不了今天的 merchant-profile 契约。"
            " **它们不是封版会被拒，是读路径上 500**"
            "（worker/pipeline.py 每个入站回合验一次、profiles/resolver.py 每次装载验一次）。"
            " 跑迁移之前先决定怎么办：改那几行，还是把那次收紧退回去。"
        )
    return 1 if bad_rows else 0


if __name__ == "__main__":
    # 与 `check_adr_references.py` / `check_backlog_references.py` 逐字同源：
    # Windows 控制台是 cp1252，而这个脚本每一行输出都是中文 ——
    # 没有这一句它会以 `UnicodeEncodeError` 结束，**退出码非零而一个字都没报出来**。
    # 这个脚本尤其不能这样：它是**跑迁移之前**那个人手里唯一的判据。
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
