"""`purge_tenant_data` 的表列只许增不许减。

**这条门禁是 2026-09-12 独立审查 P1-1 换来的**，它要防的失败**不是红不红**：

三个并行批次各自用 `CREATE OR REPLACE` **整份重写**同一个函数
（`052` 加 `knowledge_chunk_identity`、`053` 加 `materials`、`054` 加
`contact_memories`），而三份清单互不包含。迁移按**文件名排序**应用
（`scripts/migrate.py`），于是**号最大的那份赢，把别人加的名字静默抹掉**——
**两条是不同文件，`git merge-tree` 给出 0 个冲突标记。**

更难看见的一层：**干净库与已迁移库会给出两种答案。**
CI 永远新建库、按文件号跑，而 Stage / 生产按**到达顺序**跑，后合并的那份赢。
于是同一组提交，在 CI 上覆盖齐全、在 Stage 上少一张表，
**而这一半门禁照不到**。

后果是：**租户要求抹除之后，他的数据还在库里。**

**为什么是这条用例而不是一句约定**：ADR-0125 §三 的口径——
能变成门禁的约束不留在散文里。原来的处置是「合并前记得以 `main` 的当前定义为基重抄」，
那是一条靠人记的规则，而这个缺陷的成因恰恰是有人没记住。
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[2] / "infrastructure" / "migrations"

_DEFINES = re.compile(r"FUNCTION\s+purge_tenant_data", re.IGNORECASE)
_ARRAY = re.compile(r"ordered_tables\s+text\[\]\s*:=\s*ARRAY\[(.*?)\]\s*;", re.S)


def _purge_lists() -> list[tuple[str, set[str]]]:
    """每一条重声明 `purge_tenant_data` 的迁移，按应用顺序给出它的表列。"""

    found: list[tuple[str, set[str]]] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if not _DEFINES.search(text):
            continue
        body = _ARRAY.search(text)
        assert body is not None, (
            f"{path.name} 重声明了 purge_tenant_data，但读不出它的 ordered_tables——"
            "本用例靠这个数组判断覆盖面，读不出就等于没有守卫"
        )
        found.append((path.name, set(re.findall(r"'([a-z_]+)'", body.group(1)))))
    return found


def test_at_least_one_migration_defines_the_purge() -> None:
    """空集合会让下面那条真空通过。"""

    assert _purge_lists(), "没有任何迁移定义 purge_tenant_data，下一条用例证明不了任何事"


def test_no_redefinition_drops_a_table_an_earlier_one_purged() -> None:
    """**重声明只许增不许减。**

    这一条会在本仓三个在飞批次的任意两条落到同一棵树上、而后来者没有
    以前一份为基重抄时变红——**在合并之后的基线变红之前**，
    也就是在「红在没做错事的那位作者的合并后基线上」之前。
    """

    lists = _purge_lists()
    for (earlier_name, earlier), (later_name, later) in zip(lists, lists[1:], strict=False):
        dropped = sorted(earlier - later)
        assert not dropped, (
            f"{later_name} 重声明 purge_tenant_data 时丢掉了 {earlier_name} 已在清除的表："
            f"{dropped}。迁移按文件名排序应用，号大的那份最后生效，"
            "于是这些表在租户抹除时会被静默留下——而 git 不会报冲突，"
            f"因为两者是不同文件。修法：把 {later_name} 的 ordered_tables 改成"
            f"「{earlier_name} 的清单 ∪ 本批新增的表」，不要凭空重抄一份旧的。"
        )


def test_the_last_definition_covers_every_earlier_one() -> None:
    """**最后那份就是真正生效的那份**，它必须覆盖全部历史。

    上一条只比较相邻两份；这一条防的是「A 掉了 B 的、C 又补回 B 的但掉了 A 的」
    这种相邻比较看不出来的组合。
    """

    lists = _purge_lists()
    union: set[str] = set()
    for _, tables in lists:
        union |= tables
    last_name, last = lists[-1]
    missing = sorted(union - last)
    assert not missing, (
        f"{last_name} 是最后生效的那份定义，而它没有覆盖 {missing}——"
        "这些表被某一条更早的迁移放进过清除清单，现在掉了。"
    )
