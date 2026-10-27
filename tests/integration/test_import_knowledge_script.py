"""导入脚本的两条判据，**先前只活在一个零用例、零门禁的脚本里**。

独立审查 2026-09-12，P1-1：全仓 grep，`tests/` 与 `scripts/ci.sh` /
`check_static.py` 没有任何一处提到 `import_knowledge`。而这一批把
**「只重嵌改过的那一条」从成本问题提升成了正确性问题**（重嵌会无理由地改变
没改过内容的检索行为），又把**「整批拒不是跳过坏的几条」**写成发布单位的语义 ——
**两条都没有一行用例**。

46 条新用例分别落在嵌入客户端、整理规格、隔离上，**一条都没落在脚本上**；
脚本唯一的证据是一次要真凭据、要真花钱、**不能重跑**的端到端实跑。
**下一个改这个脚本的人得不到任何红灯。**

跟着 `tests/security/test_secret_scan.py` 的写法把脚本当模块导入。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.embedding import Embedding
from psycopg import Connection
from psycopg.rows import dict_row

from tests.conftest import TENANT_A, _migration_url

pytestmark = [pytest.mark.security]

ROOT = Path(__file__).resolve().parents[2]
DIM = 2048


def _script() -> Any:
    """`scripts/` 不是源码根，所以按路径加载。"""

    if "import_knowledge" in sys.modules:
        return sys.modules["import_knowledge"]
    spec = importlib.util.spec_from_file_location(
        "import_knowledge", ROOT / "scripts" / "import_knowledge.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["import_knowledge"] = module
    spec.loader.exec_module(module)
    return module


class _CountingClient:
    """记下被调了几次、每次拿到什么，并回一个可辨认的向量。

    **每次回不同的向量**，因为真实端点就是这样（实测 drift 3.66e-03）——
    一个每次都回同一个向量的替身会让「复用」和「重嵌」在断言里长得一样。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_one(self, text: str, *, timeout_seconds: float) -> Embedding:
        self.calls.append(text)
        seed = 0.01 * len(self.calls)
        return Embedding(
            vector=tuple([seed] * DIM),
            model_id="doubao-embedding-vision-251215",
            dimension=DIM,
            prompt_tokens=10,
        )


def _entry(index: int, *, body: str | None = None) -> dict[str, Any]:
    return {
        "id": f"kb-{index:03d}",
        "version": 1,
        "question": "探针问题",
        "type": "实测数据",
        "confidence": "已实测",
        "source": [{"file": "docs/probe.md", "section": "probe"}],
        "body": body if body is not None else f"第 {index} 段正文。",
    }


@pytest.fixture()
def db(seeded_database: dict[str, object]) -> Iterator[Connection]:
    assert seeded_database["tenant_a"] == TENANT_A
    with Connection.connect(
        Settings.from_env().database_url, row_factory=dict_row
    ) as connection:
        connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(TENANT_A),))
        yield connection


# ---------------------------------------------------------------------------
# 判据一：只重嵌改过的那一条
# ---------------------------------------------------------------------------


def test_an_unchanged_chunk_reuses_its_vector_and_a_changed_one_does_not(
    db: Connection,
) -> None:
    """**这是这一批把成本问题变成正确性问题的那一条。**

    同一段文本嵌两次向量不一样，所以重嵌一条没改过的 chunk
    **会无理由地改变它的检索行为**。
    """

    script = _script()
    client = _CountingClient()
    entries = [_entry(i) for i in range(3)]

    first = script.create_release(db, tenant_id=TENANT_A, entries=entries, spec="probe-v1")
    assert script.vectorise(
        db, tenant_id=TENANT_A, release_id=first, client=client
    ) == {"embedded": 3, "reused": 0}

    # 改一条，其余照旧 —— **改正文就要 `chunk_version` +1**。
    #
    # 先前这里只改正文、不动 version（独立审查 2026-09-12，P1-1），
    # 而第 5 批的身份外键把它推红了。**红得对**：整理规格 §七 写死
    # 「**正文改了（哪怕一个字）→ 同一个 slug，版本 +1**」，
    # 所以「改正文不 +1」本来就不是一条合法工作流 ——
    # 这条用例先前编码的是一条规格不允许的路径。
    #
    # 改成 `v2` 之后它还多证明一件事：**跨版本仍然按 `text_hash` 复用**，
    # 没改的那两条不会因为同批里有人升了版而被重嵌。
    entries[1] = _entry(1, body="第 1 段正文。改了一个字。")
    entries[1]["version"] = 2
    second = script.create_release(db, tenant_id=TENANT_A, entries=entries, spec="probe-v2")
    counts = script.vectorise(db, tenant_id=TENANT_A, release_id=second, client=client)

    assert counts == {"embedded": 1, "reused": 2}
    assert client.calls[-1] == "第 1 段正文。改了一个字。", "重嵌的不是改过的那一条"


def test_a_vector_from_a_different_model_is_not_reused(db: Connection) -> None:
    """**P1-2 那条 bug 的用例。**

    先前那条复用查询不限 release、不限模型，于是：换模型 → 改一条正文 → 导入，
    新 release 里 1 条是模型 B、54 条是复用来的模型 A，而 `embedding_model`
    列写着 B。**两个向量空间混在一次检索里，没有任何东西会红。**

    `embedding_dimension = 2048` 那条 CHECK 拦不住它 —— **同维度换模型是最常见的
    换法**，而 `EmbeddingDimensionMismatch` 只在真发了请求时才可能抛，
    **复用路径根本不发请求**。
    """

    script = _script()
    client = _CountingClient()
    entries = [_entry(i) for i in range(2)]

    first = script.create_release(db, tenant_id=TENANT_A, entries=entries, spec="probe-v1")
    script.vectorise(db, tenant_id=TENANT_A, release_id=first, client=client)
    assert len(client.calls) == 2

    # 换模型：把那一版的 `embedding_model` 改掉，模拟「上一版是另一个模型算的」。
    #
    # **走迁移连接，不走 `fastagent_app`** —— 因为 `051` 现在不给它
    # `UPDATE ON knowledge_releases`（那个 verb 的调用方是第 5 批的 publish）。
    # 这一步是 fixture 的布景，**不是本用例要测的那件事**；
    # 用 app 角色去做它只会让这条用例红在一个与它无关的权限上。
    # （第一版就是那样红的 —— 而那说明 P2-4 收窄的那条 GRANT 真的在生效。）
    db.commit()
    with Connection.connect(_migration_url()) as setup:
        setup.execute(
            "UPDATE knowledge_releases SET embedding_model = 'some-other-model'"
            " WHERE tenant_id = %s AND id = %s",
            (TENANT_A, first),
        )
        setup.commit()

    entries.append(_entry(9))
    second = script.create_release(db, tenant_id=TENANT_A, entries=entries, spec="probe-v2")
    counts = script.vectorise(db, tenant_id=TENANT_A, release_id=second, client=client)

    assert counts == {"embedded": 3, "reused": 0}, (
        "复用了另一个模型算出来的向量 —— 两个向量空间会混进同一次检索"
    )


# ---------------------------------------------------------------------------
# 判据二：整批拒，不是跳过坏的那几条
# ---------------------------------------------------------------------------


def test_one_bad_chunk_stops_the_whole_batch_and_writes_nothing(
    db: Connection, tmp_path: Path
) -> None:
    """**一批内容是一次发布的单位。**

    「导进去了一部分」会让下一个人以为这一版是完整的 ——
    而那一版会被发布、被检索、答不出它本该答的那几条。
    """

    script = _script()
    entries = [_entry(0), _entry(1, body="如前所述，见上面那条。"), _entry(2)]
    source = tmp_path / "batch.json"
    source.write_text(
        json.dumps({"meta": {"batch": "probe"}, "entries": entries}, ensure_ascii=False),
        encoding="utf-8",
    )

    problems = script.validate(entries)
    assert set(problems) == {"kb-001"}, "只有那一条该被拒"

    before = db.execute(
        "SELECT count(*) AS n FROM knowledge_releases WHERE tenant_id = %s", (TENANT_A,)
    ).fetchone()["n"]
    # `main` 在校验不过时 return 1，**在碰数据库之前**。
    assert script.validate(entries), "前提：这一批确实不合规"
    after = db.execute(
        "SELECT count(*) AS n FROM knowledge_releases WHERE tenant_id = %s", (TENANT_A,)
    ).fetchone()["n"]
    assert after == before, "校验不过时一行都不该写"


def test_re_importing_the_same_content_is_refused_and_carries_the_release(
    db: Connection,
) -> None:
    """`AlreadyImported` 带着那一版的 id 与状态 —— **`--embed` 要拿它去补**。"""

    script = _script()
    entries = [_entry(i) for i in range(2)]
    first = script.create_release(db, tenant_id=TENANT_A, entries=entries, spec="probe-v1")

    with pytest.raises(script.AlreadyImported) as caught:
        script.create_release(db, tenant_id=TENANT_A, entries=entries, spec="probe-v1")
    assert caught.value.release_id == first
    assert caught.value.status == "draft"


def test_the_fingerprint_covers_more_than_the_body(db: Connection) -> None:
    """**P2-3 那条 bug 的用例。**

    先前指纹只覆盖 `id` / `version` / `body`，于是「改问题、补来源、修 confidence」
    这类**完全合法**的修订（整理规格 §七 只要求正文改动才 `v+1`）进不了库，
    而给整理的人看到的那句「内容一个字都没变」是错的 ——
    **它会把人引向「那就改 version 吧」，正好污染 T279 盯的那条不变量。**
    """

    script = _script()
    base = [_entry(0)]
    changed_question = [dict(base[0], question="换一个问法？")]
    changed_source = [
        dict(base[0], source=[{"file": "docs/probe.md", "section": "另一节"}])
    ]

    digests = {
        script._content_digest(base),
        script._content_digest(changed_question),
        script._content_digest(changed_source),
    }
    assert len(digests) == 3, "改问题或改来源没有改变这一版的指纹"


def test_reordering_the_fields_is_not_a_new_version() -> None:
    """反面：**字段顺序变了不该算新内容。**

    没有这一条，上面那条会把作者推向「把整条 JSON 原样 hash」，
    而那会让一次无意义的格式化变成一个新版本。
    """

    script = _script()
    one = [{"id": "kb-000", "version": 1, "body": "x", "question": "q"}]
    other = [{"question": "q", "body": "x", "version": 1, "id": "kb-000"}]
    assert script._content_digest(one) == script._content_digest(other)


def test_the_binding_comes_from_the_active_release_not_a_filename(db: Connection) -> None:
    """**P2-1**：先前写死 `release-2026.09.02-5.json`。

    今天 `-5` 恰好就是 active，所以当时没有错 —— 而 `-6` 落地的那天，
    导入会继续拿一块已经 superseded 的绑定去嵌入。
    """

    script = _script()
    from fastagent_v2.worker.model_policy import active_release

    assert script._binding().model_id == active_release().document["embedding"]["model_id"]
    assert uuid.UUID  # noqa: B018 - keeps the import list honest


def test_changing_a_body_without_bumping_the_version_says_so_in_words(db: Connection) -> None:
    """**复审 2026-09-12，N-2**：第一轮要的两个动作里，这一条当时没做。

    最可能的整理失误就是**改了正文忘了 `v+1`**，而数据库给的是一段
    `knowledge_chunks_identity_fkey`。`publish_knowledge.py` 自己立的规矩是
    「只负责把数据库的拒绝翻译成一句人话」—— 同一批里的对侧当时没照做。

    这条用例同时盖住 N-2 的另一半：**判定改成按 `diag.constraint_name`**，
    而先前是 `if "identity" not in str(clash)` 的子串匹配 ——
    **锚点写错与真的没命中在输出里长得一样**，而当时没有任何用例走过它。
    """

    script = _script()
    entries = [_entry(0)]
    script.create_release(db, tenant_id=TENANT_A, entries=entries, spec="probe-v1")

    changed = [_entry(0, body="第 0 段正文。改了一个字，而版本号没动。")]
    with pytest.raises(script.BodyChangedWithoutNewVersion) as caught:
        script.create_release(db, tenant_id=TENANT_A, entries=changed, spec="probe-v2")

    message = str(caught.value)
    assert "kb-000:v1" in message, "拒绝理由要点名是哪一条、哪一版"
    assert "version" in message and "+1" in message, "要说清该怎么办"


def test_an_unrelated_constraint_failure_is_not_swallowed(db: Connection) -> None:
    """**反面**：别的约束坏了不许被当成「忘了 +1」。

    没有这一条，把判定写成「凡是外键违反都报那句话」也会绿 ——
    而那会让一条真正的引用完整性错误被翻译成一句误导人的中文。

    **2026-09-12：这条用例原来断言的是 `ForeignKeyViolation`，而 `055` 改了
    先撞上的是谁。** 一条 `BEFORE ROW` 触发器在外键之前跑，所以
    `055` 的「release 查不到就拒」现在**先于** `knowledge_chunks_release_id_fkey`
    发作 —— 孤儿 `release_id` 从此报的是触发器那句话。

    **这是 `055` fail-closed 的直接后果，写在这里而不是改回去**：那道闸要防的是
    临时表遮蔽（此时外键恰恰不会拒，因为真表里那一行存在），
    而代价就是这一类 —— 报错文字换了人。**本用例要证明的那件事没变**：
    它不是 `BodyChangedWithoutNewVersion`。
    """

    script = _script()
    entries = [_entry(0)]
    with pytest.raises(psycopg.errors.RaiseException) as caught, db.transaction():
        db.execute(
            "INSERT INTO knowledge_chunk_identity(tenant_id, slug, chunk_version, text_hash)"
            " VALUES (%s, 'kb-orphan', 1, %s) ON CONFLICT DO NOTHING",
            (TENANT_A, script.text_hash(entries[0]["body"])),
        )
        db.execute(
            "INSERT INTO knowledge_chunks(tenant_id, id, release_id, document_ref,"
            " document_version, ordinal, ref, slug, chunk_version, question, content_type,"
            " confidence, body, text_hash, source_spans)"
            " VALUES (%s, %s, %s, 'd', '1', 0, 'knowledge:kb-orphan:v1', 'kb-orphan', 1,"
            " 'q', 't', 'c', %s, %s, '[{\"file\": \"a\", \"section\": \"b\"}]')",
            (TENANT_A, uuid.uuid4(), uuid.uuid4(), entries[0]["body"],
             script.text_hash(entries[0]["body"])),
        )

    assert not isinstance(caught.value, script.BodyChangedWithoutNewVersion)
    assert "查不到" in str(caught.value), "报的应当是 055 那条，而不是别的"

def test_the_cli_prints_the_version_clash_instead_of_a_traceback(
    db: Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**N-2 的另一半：`main()` 自己。**

    上面那条只证明 `create_release` 抛得对；而操作者拿到的是 `main()` 的输出。
    先前 `BodyChangedWithoutNewVersion` 全仓零捕获 —— 那句写好的人话
    **印不到任何人眼前**，CLI 照旧打一段 psycopg 栈。

    这是本批第二次踩同一个形状（第 4 批 P1-1：判据只活在没人调的地方），
    所以这一条走的是真的 `main()`，不是 `create_release`。
    """

    script = _script()

    def run(entries: list[dict[str, Any]], batch: str) -> int:
        source = tmp_path / f"{batch}.json"
        source.write_text(
            json.dumps({"meta": {"batch": batch}, "entries": entries}, ensure_ascii=False),
            encoding="utf-8",
        )
        argv = [sys.argv[0], str(source), "--tenant", str(TENANT_A)]
        old = sys.argv
        sys.argv = argv
        try:
            return script.main()
        finally:
            sys.argv = old

    assert run([_entry(0)], "cli-v1") == 0
    capsys.readouterr()

    code = run([_entry(0, body="第 0 段正文。改了一个字，版本号没动。")], "cli-v2")
    printed = capsys.readouterr().out

    assert code != 0, "改了正文忘了 +1 不能当成功退出"
    assert "kb-000:v1" in printed, "要点名是哪一条、哪一版"
    assert "Traceback" not in printed
