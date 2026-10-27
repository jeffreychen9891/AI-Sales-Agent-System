"""`055` / T296：已发布的 release 里，chunk 不能再被改动。

**这条闸存在是因为 `052` 的发布闸只查一次。** 那一刻查「chunk 都向量化过了
吗」很便宜，而它此后一直成立的全部依据是「发布之后 chunk 不会再变」——
第 5 批把那句话写进了注释，**而当时没有任何东西守着它**。

失败的样子与 T281 是同一种：检索少召回，`retrieval_evidence.outcome` 记的
仍然是 `'empty'`，**和「真的没有匹配上」长得一模一样**。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastagent_v2.runtime.settings import Settings
from psycopg import Connection, errors
from psycopg.rows import dict_row

from tests.conftest import TENANT_A
from tests.integration.test_knowledge_publish import _hash, _make_release, _publish, _vector

pytestmark = [pytest.mark.security]


@pytest.fixture()
def db(seeded_database: dict[str, object]) -> Iterator[Connection]:
    assert seeded_database["tenant_a"] == TENANT_A
    with Connection.connect(
        Settings.from_env().database_url, row_factory=dict_row
    ) as connection:
        connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(TENANT_A),))
        yield connection


@pytest.fixture()
def owner(seeded_database: dict[str, object]) -> Iterator[Connection]:
    """migrator 连接。**它是超级用户，不受 RLS 也不受列级 GRANT 约束** ——
    正因如此，它是「触发器还守不守得住」这个问题唯一问得出口的地方。
    """

    from tests.conftest import _migration_url

    with Connection.connect(_migration_url(), row_factory=dict_row) as connection:
        yield connection


def test_a_published_release_cannot_be_given_another_chunk(db: Connection) -> None:
    """**这一条是列级 GRANT 管不到的那半边** —— 它是 INSERT，不是 UPDATE。

    追加一条**没有向量**的 chunk，正是 `052` 的发布闸要拦的东西，
    而那道闸已经跑过了、不会再跑。
    """

    release_id = _make_release(db, version=1)
    _publish(db, 1)

    with pytest.raises(errors.RaiseException) as caught, db.transaction():
        _insert_chunk(db, release_id, ordinal=99, slug="kb-late", embed=False)

    assert "只能在 draft release 上写" in str(caught.value)
    assert "published" in str(caught.value)


def test_a_published_chunk_cannot_have_its_vector_taken_away(db: Connection) -> None:
    """置空 `embedding` 是**列级 GRANT 允许**的那一列，所以只有触发器拦得住。

    两条闸各管一半，这条用例是「它们不是重复」的那个证据。
    """

    release_id = _make_release(db, version=1)
    _publish(db, 1)

    with pytest.raises(errors.RaiseException), db.transaction():
        db.execute(
            "UPDATE knowledge_chunks SET embedding = NULL"
            " WHERE tenant_id = %s AND release_id = %s",
            (TENANT_A, release_id),
        )


def test_a_superseded_release_is_just_as_locked(db: Connection) -> None:
    """**反面之一：不是「只锁 published」。**

    回滚会让一版变回 `published`（裁定七），所以一版 `superseded` 的 release
    **随时可能重新成为当前那一版** —— 它的 chunk 在这期间被改掉，
    回滚之后就悄悄生效了。
    """

    _make_release(db, version=1)
    second = _make_release(db, version=2, slug_prefix="kb2")
    _publish(db, 1)
    _publish(db, 2)  # v1 变成 superseded，v2 published

    first_chunks = db.execute(
        "SELECT c.id FROM knowledge_chunks c JOIN knowledge_releases r"
        "   ON r.tenant_id = c.tenant_id AND r.id = c.release_id"
        " WHERE c.tenant_id = %s AND r.version = 1",
        (TENANT_A,),
    ).fetchall()
    assert first_chunks, "前提没建起来"

    with pytest.raises(errors.RaiseException) as caught, db.transaction():
        db.execute(
            "UPDATE knowledge_chunks SET embedding = %s WHERE tenant_id = %s AND id = %s",
            (_vector(0.99), TENANT_A, first_chunks[0]["id"]),
        )
    assert "superseded" in str(caught.value)
    assert second is not None


def test_a_draft_release_is_still_fully_writable(db: Connection) -> None:
    """**反面之二，而它是这四条里最要紧的一条。**

    没有它，一条「拒绝一切写入」的触发器会让上面三条全绿 ——
    而导入路径（插 chunk → 补向量）当场就走不通了。
    """

    release_id = _make_release(db, version=1, embed=False)

    _insert_chunk(db, release_id, ordinal=50, slug="kb-more", embed=False)
    db.execute(
        "UPDATE knowledge_chunks SET embedding = %s"
        " WHERE tenant_id = %s AND release_id = %s",
        (_vector(0.5), TENANT_A, release_id),
    )
    remaining = db.execute(
        "SELECT count(*) AS n FROM knowledge_chunks"
        " WHERE tenant_id = %s AND release_id = %s AND embedding IS NULL",
        (TENANT_A, release_id),
    ).fetchone()["n"]
    assert remaining == 0, "draft 期间补向量走不通，导入路径就断了"

    # 而它确实还发得出去 —— 发布闸看到的是「全都嵌过了」。
    _publish(db, 1)


def test_the_body_of_a_draft_chunk_cannot_be_edited_in_place(db: Connection) -> None:
    """**列级 GRANT 管的那一半，触发器管不到。**

    draft 期间触发器是放行的，而正文仍然不该被 UPDATE 改：
    改内容的办法是重新导入（整理规格 §七：正文改了就 `chunk_version` +1）。

    先前只有 `text_hash` 那条 CHECK 拦着，**而连 hash 一起改就不会**——
    所以这条用例把两列一起改，那正是绕过 CHECK 的那条路。
    """

    release_id = _make_release(db, version=1)
    new_body = "换掉的正文。"

    with pytest.raises(errors.InsufficientPrivilege), db.transaction():
        db.execute(
            "UPDATE knowledge_chunks SET body = %s, text_hash = %s"
            " WHERE tenant_id = %s AND release_id = %s",
            (new_body, _hash(new_body), TENANT_A, release_id),
        )


def _insert_chunk(
    connection: Connection, release_id: uuid.UUID, *, ordinal: int, slug: str, embed: bool
) -> None:
    body = f"{slug} 的正文。"
    digest = _hash(body)
    connection.execute(
        "INSERT INTO knowledge_chunk_identity(tenant_id, slug, chunk_version, text_hash)"
        " VALUES (%s, %s, 1, %s) ON CONFLICT DO NOTHING",
        (TENANT_A, slug, digest),
    )
    columns: dict[str, Any] = {
        "tenant_id": TENANT_A,
        "id": uuid.uuid4(),
        "release_id": release_id,
        "document_ref": "docs/probe.md",
        "document_version": "1",
        "ordinal": ordinal,
        "ref": f"knowledge:{slug}:v1",
        "slug": slug,
        "chunk_version": 1,
        "question": "探针问题",
        "content_type": "实测数据",
        "confidence": "已实测",
        "body": body,
        "text_hash": digest,
        "source_spans": json.dumps([{"file": "docs/probe.md", "section": "probe"}]),
    }
    if embed:
        columns["embedding"] = _vector()
    names = ", ".join(columns)
    holders = ", ".join(["%s"] * len(columns))
    connection.execute(
        f"INSERT INTO knowledge_chunks({names}) VALUES ({holders})",  # noqa: S608
        tuple(columns.values()),
    )

def test_a_temp_table_cannot_shadow_the_release_lookup(db: Connection) -> None:
    """**独立审查 2026-09-12 P1-1：这道闸曾经能被一条临时表关掉，一声不响。**

    `pg_temp` 在没被显式写进 `search_path` 时是**隐式最先搜索**的，
    而建临时表只要数据库的 `TEMP` 权限 —— `PUBLIC` 默认就有。
    第一版的函数体写的是不限定 schema 的 `FROM knowledge_releases`
    且没有 `SET search_path`，于是触发器查到的是那张空表，
    落进当时那条「找不到就放行」。**实测通过**：一条没有向量的 chunk
    进了一个已发布的 release。

    **而第一版给那条 fail-open 写的理由是「外键会拒它」——在这条路上恰好相反**：
    外键按 OID 绑真表，`public` 里那一行存在，外键照过。

    **这条用例有一个很容易踩的坑**（审查方点名）：临时表是**会话级**的。
    建表的连接与执行 INSERT 的连接**必须是同一个**，否则用例会因为
    「遮蔽根本没生效」而绿，**读起来和「修好了」一模一样**。
    这里用的是同一个 `db`，下面那条阳性对照就是用来证明这一点的。
    """

    release_id = _make_release(db, version=1)
    _publish(db, 1)
    db.commit()

    # **阳性对照先跑**：没有临时表时它是拒的。没有这一步，
    # 下面的 REFUSED 可能来自任何别的原因。
    with pytest.raises(errors.RaiseException), db.transaction():
        _insert_chunk(db, release_id, ordinal=90, slug="kb-control", embed=False)

    db.execute("CREATE TEMP TABLE knowledge_releases (tenant_id uuid, id uuid, status text)")
    try:
        shadowed = db.execute(
            "SELECT count(*) AS n FROM knowledge_releases"
        ).fetchone()["n"]
        assert shadowed == 0, "临时表没有遮住 —— 这条用例此刻什么都没在测"

        with pytest.raises(errors.RaiseException), db.transaction():
            _insert_chunk(db, release_id, ordinal=99, slug="kb-late", embed=False)
    finally:
        db.execute("DROP TABLE IF EXISTS pg_temp.knowledge_releases")

    left = db.execute(
        "SELECT count(*) AS n FROM public.knowledge_chunks"
        " WHERE tenant_id = %s AND release_id = %s AND embedding IS NULL",
        (TENANT_A, release_id),
    ).fetchone()["n"]
    assert left == 0, "已发布的 release 里多出了没有向量的 chunk"


def test_a_published_chunk_cannot_be_moved_into_a_draft_release(
    owner: Connection, db: Connection
) -> None:
    """**独立审查 2026-09-12 P2-1：只看 `NEW` 的话，把 chunk 搬走是过得去的。**

    一个 2 条 chunk 的已发布 release 会变成 1 条 —— **失败方向与本批要防的
    一模一样**，只是从「多了一条坏的」变成「少了一条好的」。

    **这条走 owner 连接，而那不是取巧**：`fastagent_app` 被列级 GRANT 挡在
    `release_id` 之外（下一条用例证明），所以这条路**今天只有 owner / migrator
    走得到 —— 也就是将来的任何一条迁移**。触发器在本批里的职责正是
    「GRANT 那一半变了之后它还守得住」。
    """

    published = _make_release(db, version=1)
    _publish(db, 1)
    draft = _make_release(db, version=2, slug_prefix="kb2")
    db.commit()

    before = owner.execute(
        "SELECT count(*) AS n FROM knowledge_chunks WHERE tenant_id = %s AND release_id = %s",
        (TENANT_A, published),
    ).fetchone()["n"]
    assert before == 2, "前提没建起来"

    with pytest.raises(errors.RaiseException) as caught, owner.transaction():
        owner.execute(
            "UPDATE knowledge_chunks SET release_id = %s"
            " WHERE tenant_id = %s AND release_id = %s AND ordinal = 0",
            (draft, TENANT_A, published),
        )
    assert "搬走" in str(caught.value)

    after = owner.execute(
        "SELECT count(*) AS n FROM knowledge_chunks WHERE tenant_id = %s AND release_id = %s",
        (TENANT_A, published),
    ).fetchone()["n"]
    assert after == before


def test_the_app_role_cannot_even_name_release_id_in_an_update(db: Connection) -> None:
    """上一条为什么走 owner：**应用角色连提这一列的资格都没有**。

    两条闸各管一半的又一个断面 —— 列级 GRANT 在这里先于触发器生效。
    """

    published = _make_release(db, version=1)
    _publish(db, 1)

    with pytest.raises(errors.InsufficientPrivilege), db.transaction():
        db.execute(
            "UPDATE knowledge_chunks SET release_id = %s WHERE tenant_id = %s",
            (published, TENANT_A),
        )

@pytest.mark.parametrize("status", ["published", "superseded"])
@pytest.mark.parametrize("operation", ["insert", "update"])
def test_neither_write_reaches_a_release_that_is_not_draft(
    db: Connection, status: str, operation: str
) -> None:
    """**四格，而先前只有三格有用例**（独立审查 2026-09-12，P3-3）。

    本批 §二 的全部论证是「INSERT 与 UPDATE 由两条不同的闸管」——
    **按那张表，`superseded` 的 INSERT 是独立的一格**，而
    `test_a_superseded_release_is_just_as_locked` 只走了 UPDATE 那一半。

    审查方实测那一格**不是缺口**（它确实拒），所以这是覆盖面的洞、
    不是行为的洞 —— 而覆盖面的洞正是下一个人改坏了不会被告知的那种。
    """

    target = _make_release(db, version=1)
    _publish(db, 1)
    if status == "superseded":
        _make_release(db, version=2, slug_prefix="kb2")
        _publish(db, 2)  # v1 转 superseded

    with pytest.raises(errors.RaiseException) as caught, db.transaction():
        if operation == "insert":
            _insert_chunk(db, target, ordinal=80, slug=f"kb-{status}-late", embed=False)
        else:
            db.execute(
                "UPDATE knowledge_chunks SET embedding = NULL"
                " WHERE tenant_id = %s AND release_id = %s",
                (TENANT_A, target),
            )

    assert status in str(caught.value), "拒绝理由要说出它是什么状态"
