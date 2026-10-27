"""发布与回滚，以及 T279 / T281 两条登记到期时换来的两道闸。

**这两条到期条件都写死在第 5 批**，理由也都写死了：

* **T281** —— 发布是唯一能廉价检查「这一版都向量化过了吗」的时刻，
  因为那时整批 chunk 都在手上；
* **T279** —— 第 5 批是第一次有代码同时看得见两个 release 的 chunk。

所以这个文件里最要紧的不是「发布会把状态改对」，
**是那两道闸真的会拒**，而且拒的是它们各自该拒的那一件。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastagent_v2.runtime.settings import Settings
from psycopg import Connection, errors, sql
from psycopg.rows import dict_row

from tests.conftest import TENANT_A

pytestmark = [pytest.mark.security]

DIM = 2048


def _app_url() -> str:
    return Settings.from_env().database_url


def _insert(connection: Connection, table: str, row: dict[str, Any]) -> None:
    connection.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(name) for name in row),
            sql.SQL(", ").join(sql.Placeholder() for _ in row),
        ),
        tuple(row.values()),
    )


def _vector(seed: float = 0.01) -> str:
    return "[" + ",".join([str(seed)] * DIM) + "]"


@pytest.fixture()
def db(seeded_database: dict[str, object]) -> Iterator[Connection]:
    assert seeded_database["tenant_a"] == TENANT_A
    # **`dict_row`，与生产一致**：`publish_knowledge` 按列名取，
    # 而一个用元组行的替身会让它在这里炸、在真实运行时不炸 ——
    # 那是「用例与生产走的不是同一条路」的一个小样本。
    with Connection.connect(_app_url(), row_factory=dict_row) as connection:
        connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(TENANT_A),))
        yield connection


def _make_release(
    connection: Connection,
    *,
    version: int,
    status: str = "draft",
    chunks: int = 2,
    embed: bool = True,
    slug_prefix: str = "kb",
    body_suffix: str = "",
) -> uuid.UUID:
    release_id = uuid.uuid4()
    _insert(
        connection,
        "knowledge_releases",
        {
            "tenant_id": TENANT_A,
            "id": release_id,
            "version": version,
            "status": "draft",
            "embedding_model": "probe-embedding",
            "embedding_dimension": DIM,
            "distance_metric": "cosine",
            "normalization": "l2",
            "truncation_policy": "reject-over-limit",
            "authoring_spec": "probe",
            "content_hash": f"{version:064x}",
        },
    )
    for ordinal in range(chunks):
        slug = f"{slug_prefix}-{ordinal:03d}"
        body = f"{slug} 的正文。{body_suffix}"
        digest = _hash(body)
        connection.execute(
            "INSERT INTO knowledge_chunk_identity(tenant_id, slug, chunk_version, text_hash)"
            " VALUES (%s, %s, 1, %s) ON CONFLICT DO NOTHING",
            (TENANT_A, slug, digest),
        )
        _insert(
            connection,
            "knowledge_chunks",
            {
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
                **({"embedding": _vector()} if embed else {}),
            },
        )
    if status != "draft":
        connection.execute(
            "UPDATE knowledge_releases SET status = %s, published_at = now() "
            " WHERE tenant_id = %s AND id = %s",
            (status, TENANT_A, release_id),
        )
    return release_id


def _hash(body: str) -> str:
    import hashlib

    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _publish(connection: Connection, version: int) -> dict[str, object]:
    from scripts.publish_knowledge import publish

    return publish(connection, tenant_id=TENANT_A, version=version)


# ---------------------------------------------------------------------------
# T281：发布之前必须都向量化过
# ---------------------------------------------------------------------------


def test_a_release_with_an_unembedded_chunk_cannot_be_published(db: Connection) -> None:
    """**失败方向是安静的，这是这条闸的全部理由。**

    一个只嵌了一半的 release 发出去之后，检索少召回，而
    `retrieval_evidence.outcome` 记的是 `'empty'` —— **和「真的没有匹配上」
    长得一模一样**。裁定六特意把「检索坏了」和「模型没想到要查」拆开，
    而这会造出第三种混进 `'empty'` 里的原因。
    """

    _make_release(db, version=1, chunks=3, embed=False)
    with pytest.raises(errors.CheckViolation, match="without an embedding"), db.transaction():
        _publish(db, 1)


def test_the_refusal_says_how_many_are_missing(db: Connection) -> None:
    """「3 条里有 3 条没向量」和「有 1 条」是两种不同的处置。

    前者是没跑向量化，后者是跑了但断在中间 —— 而一句
    「this release is not ready」两种都读不出来。
    """

    _make_release(db, version=1, chunks=3, embed=False)
    with pytest.raises(errors.CheckViolation) as caught, db.transaction():
        _publish(db, 1)
    assert "3 of 3" in str(caught.value)


def test_an_empty_release_cannot_be_published(db: Connection) -> None:
    """一个 chunk 都没有的版本，检索照常跑、照常返回零条。

    **与「只嵌了一半」同源**：库里看着健康，而它答不出任何东西。
    """

    _make_release(db, version=1, chunks=0)
    with pytest.raises(errors.CheckViolation, match="no chunks"), db.transaction():
        _publish(db, 1)


def test_a_fully_embedded_release_publishes(db: Connection) -> None:
    """上面三条的反面。

    没有它，一条「拒绝一切」的触发器会让那三条全绿 ——
    而它们要证明的是这道闸在**选择**。
    """

    _make_release(db, version=1, chunks=2, embed=True)
    assert _publish(db, 1)["version"] == 1
    assert db.execute(
        "SELECT status FROM knowledge_releases WHERE tenant_id = %s AND version = 1",
        (TENANT_A,),
    ).fetchone()["status"] == "published"


# ---------------------------------------------------------------------------
# T279：同一个 slug:vN，到哪个 release 都是同一段正文
# ---------------------------------------------------------------------------


def test_the_same_slug_and_version_cannot_carry_two_different_bodies(db: Connection) -> None:
    """**这一条是 T279 换来的那道闸。**

    `RetrievalEvidence` 要答得出「当时引的是哪一版正文」，而那句话的全部价值
    建立在「`kb-012:v1` 到哪儿都是同一段话」上。一旦不成立，
    `v<N>` 就退化成一个建议 —— **而失败是安静的**：没有任何东西会红，
    只有半年后有人翻旧会话时发现引文对不上。
    """

    _make_release(db, version=1, chunks=1, slug_prefix="dup")
    with pytest.raises(errors.ForeignKeyViolation), db.transaction():
        # 同一个 slug:v1，另一段正文 —— 身份表里那一行的 hash 对不上。
        _make_release(db, version=2, chunks=1, slug_prefix="dup", body_suffix="改了一个字。")


def test_the_same_slug_and_version_with_the_same_body_is_fine_across_releases(
    db: Connection,
) -> None:
    """**反面，而且它是常态**：一批内容导进第二个 release，身份行本来就该复用。

    没有这一条，上面那条约束读起来像「一个 slug 只能出现在一个 release 里」，
    而那会让「改一条、其余照旧」这件事整个做不了。
    """

    _make_release(db, version=1, chunks=2, slug_prefix="shared")
    _make_release(db, version=2, chunks=2, slug_prefix="shared")
    assert db.execute(
        "SELECT count(*) AS total FROM knowledge_chunk_identity WHERE tenant_id = %s",
        (TENANT_A,),
    ).fetchone()["total"] == 2, "身份行该被复用，而不是每个 release 一份"


def test_a_new_chunk_version_may_carry_new_words(db: Connection) -> None:
    """`v2` 是**另一条身份**，所以它当然可以是另一段话。

    这是规格 §七 的正面：正文改了就版本 +1。
    如果这条红了，说明那道闸把「改内容」也一起拦掉了 ——
    **那会让整个版本机制没有出口。**
    """

    _make_release(db, version=1, chunks=1, slug_prefix="evolving")
    body = "evolving-000 的正文。第二版。"
    db.execute(
        "INSERT INTO knowledge_chunk_identity(tenant_id, slug, chunk_version, text_hash)"
        " VALUES (%s, 'evolving-000', 2, %s)",
        (TENANT_A, _hash(body)),
    )
    release_id = _make_release(db, version=2, chunks=0)
    _insert(
        db,
        "knowledge_chunks",
        {
            "tenant_id": TENANT_A,
            "id": uuid.uuid4(),
            "release_id": release_id,
            "document_ref": "docs/probe.md",
            "document_version": "1",
            "ordinal": 0,
            "ref": "knowledge:evolving-000:v2",
            "slug": "evolving-000",
            "chunk_version": 2,
            "question": "探针问题",
            "content_type": "实测数据",
            "confidence": "已实测",
            "body": body,
            "text_hash": _hash(body),
            "source_spans": json.dumps([{"file": "docs/probe.md", "section": "probe"}]),
            "embedding": _vector(),
        },
    )


# ---------------------------------------------------------------------------
# 发布与回滚的状态机
# ---------------------------------------------------------------------------


def test_publishing_supersedes_the_previous_one(db: Connection) -> None:
    _make_release(db, version=1)
    _publish(db, 1)
    _make_release(db, version=2)
    _publish(db, 2)

    rows = {
        row["version"]: row["status"]
        for row in db.execute(
            "SELECT version, status FROM knowledge_releases WHERE tenant_id = %s", (TENANT_A,)
        ).fetchall()
    }
    assert rows == {1: "superseded", 2: "published"}


def test_rolling_back_records_what_it_displaced_and_publishing_does_not(db: Connection) -> None:
    """`rolled_back_from` 只在回滚时填，而这不对称是有意的。

    回滚之后旧的那一版只是「又变成 published 了」——
    **没有任何东西说得出它是往回走的一步**，而那正是这一列存在的理由
    （`050` 的列注释）。往前发布不需要它：版本号自己就在往上走。
    """

    _make_release(db, version=1)
    _publish(db, 1)
    second = _make_release(db, version=2)
    _publish(db, 2)

    result = _publish(db, 1)
    assert result["rolled_back"] is True

    rolled_from = db.execute(
        "SELECT rolled_back_from FROM knowledge_releases WHERE tenant_id = %s AND version = 1",
        (TENANT_A,),
    ).fetchone()["rolled_back_from"]
    assert rolled_from == second, "回滚没记下它顶掉了哪一版"

    forward = db.execute(
        "SELECT rolled_back_from FROM knowledge_releases WHERE tenant_id = %s AND version = 2",
        (TENANT_A,),
    ).fetchone()["rolled_back_from"]
    assert forward is None, "往前发布不该填这一列"


def test_only_one_release_is_published_at_any_time(db: Connection) -> None:
    """**这条不变量不是脚本守的，是那条部分唯一索引守的。**

    两版同时 published 不是一个降级状态，是一个没有答案的问题 ——
    而检索层会拿它碰到的第一条当答案，不同的日子可能不同。
    """

    _make_release(db, version=1)
    _publish(db, 1)
    _make_release(db, version=2)
    _publish(db, 2)

    assert db.execute(
        "SELECT count(*) AS total FROM knowledge_releases"
        " WHERE tenant_id = %s AND status = 'published'",
        (TENANT_A,),
    ).fetchone()["total"] == 1

    # **绕过脚本直接写第二条，数据库照样拒。**
    #
    # `superseded_at` 也要一起清 —— 不清的话先撞上的是 `050` 那条
    # 「`status = 'superseded'` 与 `superseded_at` 同生同灭」的 CHECK，
    # **而那样这条用例就变成在测另一道闸了**：它会红，但红的理由与它声称的
    # 不是同一件事，而那正是本仓反复付钱的那个形状。
    with pytest.raises(errors.UniqueViolation), db.transaction():
        db.execute(
            "UPDATE knowledge_releases SET status = 'published', superseded_at = NULL"
            " WHERE tenant_id = %s AND version = 1",
            (TENANT_A,),
        )


def test_publishing_the_current_one_again_is_refused_readably(db: Connection) -> None:
    from scripts.publish_knowledge import PublishRefused

    _make_release(db, version=1)
    _publish(db, 1)
    with pytest.raises(PublishRefused, match="已经是当前那一版"):
        _publish(db, 1)


def test_a_version_that_does_not_exist_is_refused_readably(db: Connection) -> None:
    from scripts.publish_knowledge import PublishRefused

    with pytest.raises(PublishRefused, match="没有 v7"):
        _publish(db, 7)


def test_a_rolled_back_release_has_its_superseded_at_cleared(db: Connection) -> None:
    """`superseded_at` 是状态不是日志 —— 日志是 `rolled_back_from`。

    `050` 有一条 CHECK 把 `status = 'superseded'` 与这一列绑在一起，
    所以不清掉它，回滚那一步会被数据库拒。**这条用例证明脚本清了。**
    """

    _make_release(db, version=1)
    _publish(db, 1)
    _make_release(db, version=2)
    _publish(db, 2)
    _publish(db, 1)

    superseded_at = db.execute(
        "SELECT superseded_at FROM knowledge_releases WHERE tenant_id = %s AND version = 1",
        (TENANT_A,),
    ).fetchone()["superseded_at"]
    assert superseded_at is None


def test_inserting_an_already_published_release_is_always_refused(db: Connection) -> None:
    """**「一步生出一个已发布的版本」不被允许，而这是有意的。**

    `knowledge_chunks` 对 `knowledge_releases` 有外键，所以插入那一刻必然 0 chunk
    —— 触发器的 `BEFORE INSERT` 分支因此**恒拒**。独立审查 2026-09-12 P0-2 读作缺陷；
    量完之后它是这条状态机想要的形状：一版 release 的生命是
    **draft → 灌 chunk → 向量化 → publish**，而「一步生出已发布的版本」
    在库里的样子恰好是「一个空的已发布版本」——**T281 要拦的正是这个**。

    **这条用例是为了让下一个人不把它当成意外再打开一次。**
    代价本批付过：`main` 上 36 条 fixture 走的是那条产品里不存在的捷径，
    已全部改成走真实的那条路。
    """

    with pytest.raises(errors.CheckViolation, match="no chunks"), db.transaction():
        _insert(
            db,
            "knowledge_releases",
            {
                "tenant_id": TENANT_A,
                "id": uuid.uuid4(),
                "version": 99,
                "status": "published",
                "embedding_model": "probe-embedding",
                "embedding_dimension": DIM,
                "distance_metric": "cosine",
                "normalization": "l2",
                "truncation_policy": "reject-over-limit",
                "authoring_spec": "probe",
                "content_hash": "f" * 64,
                "published_at": "2026-09-12T00:00:00Z",
            },
        )


def test_the_text_hash_must_be_the_hash_of_the_body(db: Connection) -> None:
    """**P2-1 换来的那道闸。**

    身份表锁的是 `(tenant, slug, version) → 一个 text_hash`，**不是 → 一段正文**。
    没有这条 CHECK，「同一个 slug:vN 到哪儿都是同一段话」只保证到 hash 那一层，
    **而写错 hash 的写入方照样能把两段正文挂在同一条身份上** ——
    审查方拿 `test_knowledge_rls_isolation.py` 里那段 `"c" * 64` 当的就是现成反例。
    """

    release_id = _make_release(db, version=1, chunks=0)
    with pytest.raises(errors.CheckViolation, match="text_hash_matches_body"), db.transaction():
        db.execute(
            "INSERT INTO knowledge_chunk_identity(tenant_id, slug, chunk_version, text_hash)"
            " VALUES (%s, 'kb-liar', 1, %s) ON CONFLICT DO NOTHING",
            (TENANT_A, "a" * 64),
        )
        _insert(
            db,
            "knowledge_chunks",
            {
                "tenant_id": TENANT_A,
                "id": uuid.uuid4(),
                "release_id": release_id,
                "document_ref": "docs/probe.md",
                "document_version": "1",
                "ordinal": 90,
                "ref": "knowledge:kb-liar:v1",
                "slug": "kb-liar",
                "chunk_version": 1,
                "question": "探针问题",
                "content_type": "实测数据",
                "confidence": "已实测",
                "body": "这段正文与那个 hash 无关。",
                "text_hash": "a" * 64,
                "source_spans": json.dumps([{"file": "docs/probe.md", "section": "probe"}]),
            },
        )
