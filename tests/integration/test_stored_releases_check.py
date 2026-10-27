"""那个前置检查**真的会在一行坏 release 上红** —— 而不是一个恒绿的脚本。

## 为什么这份文件存在

`scripts/check_stored_releases.py` 是「跑 `063` 之前先过一遍库里每一行 release」
那件事的执行者。它的处境很特别：**它要检的那台库，门禁够不着** ——
门禁那套栈每次现建，里面的 release 是播种夹具写的，必然是今天的形状。

于是它天然是「一条没有人见过它红的检查」，而本仓对这种东西有明确判断：
**一条绿着的守卫永远不显示它的报错**（`review-window-state` §三）。
更要紧的是**恒绿的东西不是检查** —— 而这个脚本恰恰只在出事那天才第一次跑。

所以这里在**真库**上做三件：干净库上跑一遍脚本本身（退出码 + stdout）；
在一个**不提交的事务**里塞一行坏 release，看那个判定函数认不认得出、
说不说得出是哪一行哪一格；再确认那次回滚真的没留下东西 ——
`profile_releases` 是 append-only，**留下了就删不掉**。
**这是它唯一一次在「失败」状态下被人看见。**
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from psycopg import Connection

from tests.conftest import TENANT_A, _migration_url

pytestmark = [pytest.mark.security]

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_stored_releases.py"

#: 一份**过不了新 schema** 的 `outcome_rules`：`fulfilment` 是个词表外的值。
#: 这正是批 11 定型之前完全合法的写法（那时它是 `{"type": "object"}`），
#: 也就是**存量 release 里真可能躺着的那一种**。
STALE_RULE = {
    "strategy": "advance_goal",
    "party": "ai",
    "creates_commitment": "take_action",
    "fulfilment": "whenever_it_happens",
}


def _run() -> subprocess.CompletedProcess[str]:
    """跑那个脚本本身，**不是把它 import 进来调 `main()`**。

    要验的是「拿着 URL 去跑它的人看到什么」，而那包括退出码与 stdout ——
    import 进来调只验得到返回值。
    """

    env = {
        **_script_env(),
        "FASTAGENT_MIGRATION_DATABASE_URL": _migration_url(),
    }
    return subprocess.run(  # noqa: S603 - 固定 argv，无 shell
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _load_checker() -> Any:
    """把脚本当模块装进来 —— 要问的是它那个判定函数，不是它的退出码。"""

    import importlib.util

    if "check_stored_releases" in sys.modules:
        return sys.modules["check_stored_releases"]
    spec = importlib.util.spec_from_file_location("check_stored_releases", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_stored_releases"] = module
    spec.loader.exec_module(module)
    return module


def _script_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        str(ROOT / part)
        for part in (
            "packages/runtime/src",
            "packages/channels/src",
            "services/api/src",
            "services/worker/src",
            "services/ingress/src",
        )
    )
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def test_a_clean_database_passes(seeded_database: dict[str, Any]) -> None:
    """**先证明它在干净库上是绿的** —— 否则下面那条红说明不了什么。

    一条永远红的检查与一条永远绿的检查一样没用，
    而两者的区别只有把两种情况都跑一遍才看得出来。
    **这一条跑的是脚本本身**（退出码 + stdout），也就是拿着 URL 的人看到的东西。
    """

    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "failed=0" in result.stdout


def test_a_stale_release_is_reported_with_where_it_broke(
    seeded_database: dict[str, Any],
) -> None:
    """**一行过不了新 schema 的 release，它认得出、并指得出是哪一行哪一格。**

    ## 为什么这一条不走子进程，而上面那条走

    `profile_releases` 是 **append-only**：`006` 的触发器
    `BEFORE UPDATE OR DELETE` 无条件抛。**所以一条往里塞测试数据的用例
    清理不掉自己塞的行** —— 第一版就是这样，两条断言都过了而 teardown 炸在
    `RestrictViolation`，全量门禁 `1 error`。

    **那张表的形状是对的**（release 永不改写，这正是它存在的意义），
    所以让路的是用例：塞进一个**不提交的事务**里，当场问那个判定函数，再回滚。
    子进程看不见未提交的数据，而同一个连接看得见 —— 于是脚本被拆成
    「连哪台库、怎么退出」与「什么算过不了」两半，这里问的是后一半。

    **它因此没覆盖退出码那一格**，而那一格由上面那条覆盖。如实写在这里。
    """

    assert seeded_database["tenant_a"] == TENANT_A
    module = _load_checker()

    with Connection.connect(_migration_url()) as connection:
        assert not module.stale_releases(connection), "还没塞之前就有坏行 —— 先查那个"

        row = connection.execute(
            "SELECT profile_id, profile FROM profile_releases WHERE tenant_id = %s LIMIT 1",
            (TENANT_A,),
        ).fetchone()
        assert row is not None, "播种夹具里没有 release —— 换一个取法"
        profile_id, profile = row

        # 照播种夹具那一行改**一处** —— **不自己编一份 profile**：
        # 编的那份多半在别处也不合法，于是它红了而红的理由不是我要测的那个
        # （本仓记过「无效变异给假信号」）。
        stale = copy.deepcopy(profile)
        stale["outcome_rules"] = [STALE_RULE]

        connection.execute(
            """
            INSERT INTO profile_releases(tenant_id, id, profile_id, version, content_hash, profile)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                TENANT_A,
                uuid.uuid4(),
                profile_id,
                f"stale-{uuid.uuid4().hex[:8]}",
                uuid.uuid4().hex + uuid.uuid4().hex,
                json.dumps(stale),
            ),
        )

        found = module.stale_releases(connection)
        connection.rollback()

    assert len(found) == 1, f"没认出来：{found}"
    who, why = found[0]
    assert str(TENANT_A) in who, "没说是哪个租户"
    assert "outcome_rules" in why, "没说是哪一格"

    # **这一行 2026-09-16 改过，而改它的理由值得留着。**
    #
    # 原来写的是 `assert "whenever_it_happens" in why` ——「没说是哪个值不合法」。
    # 那条断言**要求输出里出现被拒的那个值**，而那正是同一天被发现的泄露：
    # `error.message` 在 `maxLength` 上就是整段商家话术，脚本把它打进了部署记录。
    # 修复之后这一条当场红 —— **一条要求泄露的用例**。
    #
    # 换成两样都来自**我们自己 schema** 的东西：关键字名，
    # 以及那一格的合法取值（`enum` 的 `validator_value`）。
    # 「哪个值不合法」这一问，拿着 URL 的人去库里看 —— 那是它该待的地方。
    assert "enum" in why, f"没说是哪一类校验失败：{why}"
    assert "same_turn" in why, f"没说那一格的合法取值是哪些：{why}"
    assert "whenever_it_happens" not in why, (
        f"输出里出现了被拒的那个值：{why} —— 那是商家正文，这个脚本的输出会被贴进部署记录"
    )


def test_the_rollback_really_left_nothing_behind(seeded_database: dict[str, Any]) -> None:
    """**上面那条回滚干净了吗** —— append-only 的表上，没滚掉就永远留在那儿。

    `probe_harness` 那四条失败模式里的第三条（「变异之后崩了，树上留着」）
    在这里的对应物：一条留下垃圾的用例，会让**下一次**跑
    `test_a_clean_database_passes` 红，而那一红指着的是上一次。
    """

    module = _load_checker()
    with Connection.connect(_migration_url()) as connection:
        assert not module.stale_releases(connection), (
            "库里还留着坏行 —— 上一条用例的回滚没生效，"
            "而 profile_releases 是 append-only，删不掉"
        )
