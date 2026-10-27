"""「承诺台账今天不存在」是一句会过期的话 —— 这一份是替它重核的那个人。

**这条用例的来处是 E1**（2026-09-13）：`conversation-engine.md` §4.2 的一条修订标注
写着「`ProactivityPolicy` 至今没有实现」，而它此后落地了 ——
**本仓第一条比原文还旧的修订标注**。整套注释纪律的可信度建立在
「标注永远比原文新」上，所以 `AGENTS.md`「图纸优先级」加了**第 6 条**：
凡断言「某物今天不存在」，要么带当时的坐标与计数，**要么带一条会替你重核的用例**。

`api.app.COMMITMENT_LEDGER_AVAILABLE = False` 就是这样一句断言。
它今天决定两处读模型显示「未启用」而不是光秃秃的 `0`
（用户 2026-09-13：那个 0 是一个**会误导人的真值**）。
台账落地的那一天，**这一份会红，并且指着那个常量说「改我」** ——
而不是让两块屏幕继续说「未启用」。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastagent_v2.api.commitments_display import (
    COMMITMENT_LEDGER_AVAILABLE,
    COMMITMENT_LEDGER_DISABLED_NOTE,
    commitment_display,
)

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "infrastructure" / "migrations"

#: 建承诺台账的那条迁移长什么样。**按表名匹配，不按文件名** ——
#: 文件叫什么是作者的自由，而 `CREATE TABLE commitments` 是那件事本身。
_LEDGER_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(commitments|commitment_records)\b",
    re.IGNORECASE,
)


def _ledger_migrations() -> list[str]:
    return [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if _LEDGER_TABLE.search(path.read_text(encoding="utf-8"))
    ]


def test_the_claim_that_the_ledger_does_not_exist_is_still_true() -> None:
    """**这一条是那句断言的重核者。**

    它不证明台账该不该建，只证明**那个常量与迁移目录说的是同一件事**。
    两者一旦分家，屏幕上那句「未启用」就变成了下一条「比原文还旧的标注」。
    """

    built = _ledger_migrations()

    if built:
        assert COMMITMENT_LEDGER_AVAILABLE, (
            f"迁移 {built} 建了承诺台账，而 `COMMITMENT_LEDGER_AVAILABLE` 还是 False —— "
            "两处读模型仍在对着一张真表说「未启用」。改那个常量，并把 `commitment_display` "
            "的 enabled 分支接上真实来源"
        )
    else:
        assert not COMMITMENT_LEDGER_AVAILABLE, (
            "常量说台账在了，而迁移目录里没有任何一条建 `commitments` 表 —— "
            "那两处会显示一个没有来源的计数"
        )


def test_a_zero_says_which_kind_of_zero_it_is() -> None:
    """**「显示」与「显示成什么」是两件事** —— 而 2026-09-16 起这个 0 换了一种意思。

    图纸那半句（`control-plane-parity.md:171`、ADR-0054 / ADR-0076 §四.1）要的是
    「必须显式显示『未登记任何承诺』，不得省略」—— **至今一个字没被动过**：
    `count` 照显。改的一直是它旁边那句话说的是不是真话。

    | 时期 | `status` | 那句话的意思 |
    |---|---|---|
    | ~2026-09-13 | —— | `"no commitment has been recorded"` —— **读起来像「我们找过了，没有」** |
    | 2026-09-13 起 | `not_enabled` | 「这个能力还没有启用」 —— 真相是「没有地方可找」 |
    | **2026-09-16 起（批 11）** | `enabled` | **现在真的找过了** ——
      表在、写入方在、读出方在 |

    **第三格是本批的兑现物**，而它成立的条件是三件同时为真：
    迁移建了表、`post_understanding` 那道闸在写、`commitment_refs` 从真表来。
    少任何一件，这个 `enabled` 就变回一句证不了的话 —— 那三件各自的守卫见
    `test_commitment_ledger.py`。
    """

    shown = commitment_display([])

    assert shown["count"] == 0, "字段照显、不省略 —— 图纸那半句没被动"
    assert shown["status"] == "enabled", (
        "台账已经落地（表 + 写入方 + 读出方同批到位），这一格不该再说「未启用」"
    )
    assert "no commitment has been recorded" != shown["note"], (
        "旧措辞读起来像「我们找过了，没有」—— 而在台账落地之前那是假话。"
        "现在它是真话了，**而那句英文仍然不该回来**：措辞只有一处，跟着常量走"
    )
    assert COMMITMENT_LEDGER_DISABLED_NOTE not in str(shown["note"]), (
        "「未启用」那句话不该再出现在启用之后的投影里"
    )


def test_it_is_one_sentence_written_once() -> None:
    """两处显示（详情页与接管期投影）走**同一个函数**。

    `AGENTS.md`「每处都要写成同一个形状」那一节：一件要求每处一致的事，
    不该有第二个写法。先前那句话在 `app.py` 里写了两遍、措辞还不一样。
    """

    api = ROOT / "services/api/src/fastagent_v2/api"
    app_source = (api / "app.py").read_text(encoding="utf-8")
    module_source = (api / "commitments_display.py").read_text(encoding="utf-8")

    assert module_source.count("def commitment_display(") == 1, "定义只该有一处"
    assert app_source.count("def commitment_display(") == 0, "`app.py` 不该再有第二份定义"
    assert app_source.count("commitment_display(") == 2, (
        "两处显示（详情页与接管期投影）各调一次；多了或少了都说明有一处自己又写了一遍"
    )
    assert app_source.count("no commitment has been recorded") == 0, (
        "那句旧措辞该只活在 `commitments_display.py` 的 enabled 分支里"
    )
