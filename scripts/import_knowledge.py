"""把人工整理好的 chunk 收进库，并把它们向量化。

**这是知识库第 4 批的两件事**（`docs/plans/knowledge-base-schedule-2026-09-11.md` §七）：
接收 chunk、向量化。**发布是第 5 批**，所以本脚本建出来的 release 是 `draft` ——
它不会被任何检索读到，因为检索读的是 `status = 'published'` 的那一版（裁定七）。

## 它为什么是一个脚本而不是一条 route

**裁定二：所有内容准备由人工处理。** 整理的人交出一份 JSON，
有人把它导进来 —— 这中间没有一个需要 HTTP 的环节，
而一条 route 要带上鉴权、配额、审计与一份 OpenAPI，
**为一件今天由人跑一次的事**。第二版接自动化时它会长出一条 route，
到那时改的是「谁调它」，不是这里的规则。

## 它连的是 `fastagent_app`，**不是迁移角色**

`050` 一条 `GRANT` 都没发（「a grant that precedes its caller is a permission
nothing is testing」）。**`051` 随本脚本一起发出第一条** —— 调用方就是它。

**第一版连的是迁移角色，那是错的，而错法记在 `051` 的注释里**：
`POSTGRES_USER` 就是 `fastagent_migrator`，它是**超级用户**，
而超级用户**整个绕过** RLS —— `FORCE` 解的是属主的豁免，解不了这一个。
连着它导入，隔离一次都不会被走到。

`fastagent_app` 是 `NOSUPERUSER`，所以它受策略约束：
**本脚本每导一行都在走那条策略**，而
`tests/integration/test_knowledge_rls_isolation.py` 把跨租户那几条钉住 ——
**那是第 3 批自审 §六.1 欠下的那一条，到这里才真的还上。**

## 向量化：**不重嵌没改过的 chunk**

实测（`evidence/knowledge/embedding-live-client-probe-2026-09-11.md`）：
**同一段文本嵌两次，向量不一样**，最大元素差 3.66e-03。

所以「重嵌一遍反正一样」是错的 —— **它会无理由地改变检索行为**。
本脚本按 `text_hash` 复用：同一租户下已经有同样 `text_hash` 且已向量化的 chunk 时，
**复制那个向量，不再调模型**。这既省 N 次往返，也让「正文没变 ⇒ 检索行为不变」成立。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/runtime/src"))
sys.path.insert(0, str(ROOT / "services/worker/src"))

import httpx  # noqa: E402
from fastagent_v2.runtime.knowledge_authoring import chunk_refusals  # noqa: E402
from fastagent_v2.worker.embedding import (  # noqa: E402
    EmbeddingBinding,
    EmbeddingClient,
)
from fastagent_v2.worker.model_policy import active_release  # noqa: E402
from fastagent_v2.worker.providers import HttpxChatTransport  # noqa: E402
from psycopg import Connection  # noqa: E402
from psycopg.errors import ForeignKeyViolation  # noqa: E402
from psycopg.rows import DictRow, dict_row  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

#: 一次嵌入调用的期限。比聊天那几个角色宽：它不在客户等着的那条路上。
EMBED_TIMEOUT_SECONDS = 30.0


class BodyChangedWithoutNewVersion(RuntimeError):
    """同一个 `<slug>:v<N>` 换了一段正文 —— 整理规格 §七 不允许（T279）。"""


class AlreadyImported(RuntimeError):
    """这一批内容已经在库里了。

    **不是错误，是一句答复** —— 所以 `main` 把它印出来并返回 0 之外的码，
    而不是让它变成一个栈回溯。

    **带上那一版的 id 与状态**，因为 `--embed` 要拿它去把没嵌完的补上
    （独立审查 2026-09-12，P2-2）。
    """

    def __init__(self, message: str, *, release_id: UUID, status: str) -> None:
        super().__init__(message)
        self.release_id = release_id
        self.status = status


def text_hash(body: str) -> str:
    """正文的 sha256。

    **它是本批唯一能用来判同的东西** —— 向量不行（不确定），
    而 `(slug, version)` 只是一个名字，T279 记着它今天拦不住两份不同正文。
    """

    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _content_digest(entries: list[dict[str, Any]]) -> str:
    """这一版内容的指纹，**覆盖整条 chunk 的全部字段**。

    **先前它只覆盖 `id` / `version` / `body`**（独立审查 2026-09-12，P2-3）。
    实测：把一条的 `question` 换掉、`source` 的 `section` 改到另一节，
    **指纹逐字节相同** —— 于是这类修订今天进不了库，
    而它们完全合法：整理规格 §七 只要求**正文**改动才 `v+1`，
    改问题、补来源、修正 `confidence` 都不该 +version。

    更糟的是给整理的人看的那句拒绝语（「内容一个字都没变」）**是错的**，
    而它会把人引向「那就改 version 吧」—— **正好污染 T279 盯的那条不变量。**

    `knowledge_releases.content_hash` 是**这一版内容的身份**，
    而来源改了就是另一版内容。

    用 `json.dumps(sort_keys=True)`：字段顺序变了不该算新内容，
    而字段的**值**变了应该算。
    """

    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: str(e["id"])):
        digest.update(
            json.dumps(entry, sort_keys=True, ensure_ascii=False).encode("utf-8")
        )
        digest.update(b"\x00")
    return digest.hexdigest()


def validate(entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    """整批校验，**返回全部问题而不是第一条**。"""

    return {
        str(entry.get("id")): [str(r) for r in chunk_refusals(entry)]
        for entry in entries
        if chunk_refusals(entry)
    }


def _binding() -> EmbeddingBinding:
    """从**活着的那一版** model policy 里取 `embedding` 块。

    **先前这里写死 `release-2026.09.02-5.json`**（独立审查 2026-09-12，P2-1）。
    今天 `-5` 恰好就是 active，所以当时没有错——**而 `-6` 落地的那天，
    导入会继续拿一块已经 superseded 的绑定去嵌入**，并把它的 `model_id`
    写进 `knowledge_releases.embedding_model`。失败方向与 P1-2 同族，一样安静。

    `active_release()` 是「哪一版是活的」的唯一答案：它扫全部 `release-*.json`、
    认 `status == "active"`，**0 个或多个都报错**——那两种情况本来就该停下，
    而不是由这里挑一个。
    """

    return EmbeddingBinding.from_release(active_release().document)


def create_release(
    connection: Connection[DictRow], *, tenant_id: UUID, entries: list[dict[str, Any]], spec: str
) -> UUID:
    """建一个 `draft` release，并把这一批 chunk 全写进去。

    **`content_hash` 取的是正文，不是向量** —— 向量不确定（见模块 docstring），
    拿它做内容指纹的话「同一批内容导两次」会得到两个不同的 hash。
    """

    binding = _binding()
    digest = _content_digest(entries)

    # **同一批内容导第二次，说清楚而不是抛一个约束名。**
    # `UNIQUE (tenant_id, content_hash)` 是 `050` 有意留的（那里的注释写着
    # 为什么，以及第 5 批撞上时正确做法是回滚不是重发）。**端到端实测撞到了它**，
    # 而当时的表现是一个 psycopg 的 UniqueViolation 栈回溯 ——
    # 一个导内容的人读不出「我已经导过了」。
    existing = connection.execute(
        "SELECT id, version, status FROM knowledge_releases "
        " WHERE tenant_id = %s AND content_hash = %s",
        (tenant_id, digest),
    ).fetchone()
    if existing is not None:
        raise AlreadyImported(
            f"这一批内容已经是 release v{existing['version']}（{existing['status']}）。"
            "**正文与其余字段都没变**，所以没有新版本可发 —— "
            "要改内容就改 JSON 再导；要把旧的那一版重新置为当前，那是第 5 批的回滚。",
            release_id=UUID(str(existing["id"])),
            status=str(existing["status"]),
        )

    next_row = connection.execute(
        "SELECT coalesce(max(version), 0) + 1 AS next FROM knowledge_releases "
        "WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchone()
    assert next_row is not None  # `coalesce(max(...))` always returns one row
    version = next_row["next"]
    release_id = uuid4()
    connection.execute(
        """
        INSERT INTO knowledge_releases(
            tenant_id, id, version, status, embedding_model, embedding_dimension,
            distance_metric, normalization, truncation_policy, authoring_spec, content_hash)
        VALUES (%s, %s, %s, 'draft', %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tenant_id,
            release_id,
            int(version),
            binding.model_id,
            binding.dimension,
            binding.distance_metric,
            binding.normalization,
            binding.truncation_policy,
            spec,
            digest,
        ),
    )

    for ordinal, entry in enumerate(entries):
        try:
            sources = list(entry["source"])
            digest_of_body = text_hash(str(entry["body"]))
            # **身份行先写**（`052`）。它对每个 `(slug, version)` 只允许一行、
            # 只带一个 `text_hash`，而 chunk 那条四列外键要求对得上 ——
            # 所以「同一个 `<slug>:v<N>` 换了一段正文」在这里就被拒，
            # 而不是等到有人发现引文对不上（T279）。
            #
            # `ON CONFLICT DO NOTHING`：同一批内容导进不同 release 是正常的，
            # 身份行本来就该复用。**冲突留给外键去拒** —— 它的报错点名的是
            # 那一行 chunk，而这里 `DO UPDATE` 会把冲突悄悄改掉。
            connection.execute(
                "INSERT INTO knowledge_chunk_identity(tenant_id, slug, chunk_version, text_hash)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (tenant_id, str(entry["id"]), int(entry["version"]), digest_of_body),
            )
            connection.execute(
                """
                INSERT INTO knowledge_chunks(
                    tenant_id, id, release_id, document_ref, document_version, ordinal,
                    ref, slug, chunk_version, question, content_type, confidence, body,
                    text_hash, source_spans)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    uuid4(),
                    release_id,
                    # `document/version` —— 今天没有 `KnowledgeDocument` 表（裁定二），
                    # 值由整理规格给：第一个来源的文件，以及这一批的批次号。
                    str(sources[0]["file"]),
                    str(entry.get("batch") or spec),
                    ordinal,
                    f"knowledge:{entry['id']}:v{entry['version']}",
                    str(entry["id"]),
                    int(entry["version"]),
                    str(entry["question"]),
                    str(entry["type"]),
                    str(entry["confidence"]),
                    str(entry["body"]),
                    digest_of_body,
                    Jsonb(sources),
                ),
            )
        except ForeignKeyViolation as clash:
            # **按约束名判，不按子串判**（复审 2026-09-12，N-2）。
            # `if "identity" not in str(clash)` 是子串匹配，**锚点写错与真的
            # 没命中在输出里长得一样**，而没有任何用例走过它。
            if getattr(clash.diag, "constraint_name", None) != "knowledge_chunks_identity_fkey":
                raise
            # **把那条外键的拒绝翻译成一句人话**（独立审查 2026-09-12，P2-4）。
            #
            # 最可能的失误恰恰是**改了正文忘了 `v+1`**，而数据库给的是一段
            # `knowledge_chunks_identity_fkey` 的 psycopg 栈。
            # `publish_knowledge.py` 自己立的规矩是「只负责把数据库的拒绝
            # 翻译成一句人话」——同一批里的对侧当时没照做。
            raise BodyChangedWithoutNewVersion(
                f"`{entry['id']}:v{entry['version']}` 的正文与库里那一版对不上。"
                "**同一个编号的同一版，到哪个 release 都必须是同一段正文**"
                "（整理规格 §七 / T279）—— 改了正文就把 `version` +1，不要沿用旧号。"
            ) from clash
    return release_id


@contextmanager
def _client_or(
    client: EmbeddingClient | None, binding: EmbeddingBinding
) -> Iterator[EmbeddingClient]:
    """用传进来的那个 client，没传就自己建一个真的。

    **加这个参数是为了让这个脚本可测**（独立审查 2026-09-12，P1-1）：
    本批最重的两条判据（只重嵌改过的、整批拒不是跳过坏的几条）先前只活在
    一个零用例、零门禁的脚本里，唯一的证据是一次**不能重跑**的端到端实跑。
    """

    if client is not None:
        yield client
        return
    with httpx.Client() as http:
        yield EmbeddingClient(transport=HttpxChatTransport(http), binding=binding)


def vectorise(
    connection: Connection[DictRow],
    *,
    tenant_id: UUID,
    release_id: UUID,
    client: EmbeddingClient | None = None,
) -> dict[str, int]:
    """给这一版里还没有向量的 chunk 补上向量。

    **先找同 `text_hash` 的旧向量，找到就复制** —— 见模块 docstring：
    重嵌一条没改过的 chunk 会无理由地改变检索行为，而那不是省一次调用的问题。
    """

    pending = connection.execute(
        "SELECT id, ref, body, text_hash FROM knowledge_chunks "
        " WHERE tenant_id = %s AND release_id = %s AND embedding IS NULL"
        " ORDER BY ordinal",
        (tenant_id, release_id),
    ).fetchall()

    counts = {"reused": 0, "embedded": 0}
    if not pending:
        return counts

    binding = _binding()
    with _client_or(client, binding) as embedder:
        for row in pending:
            # **复用的候选必须来自同一个模型**（独立审查 2026-09-12，P1-2）。
            #
            # 先前这条查询是 `WHERE tenant_id AND text_hash AND embedding IS NOT NULL`
            # ——**不限 release、不限模型**。于是：换模型 → 改一条正文 → 导入，
            # 新 release 里 1 条是模型 B、54 条是复用来的模型 A，
            # 而 `embedding_model` 列写着 B。**两个向量空间混在一次检索里，
            # 没有任何东西会红。**
            #
            # `embedding_dimension = 2048` 那条 CHECK 拦不住它：
            # **同维度换模型是最常见的换法**，而 `EmbeddingDimensionMismatch`
            # 只在真发了请求时才可能抛，**复用路径根本不发请求**。
            #
            # 而 `knowledge_releases.embedding_model` 这一列存在的全部理由
            # （RFC `:728`：换模型就新建 release 重建索引）正是让这件事可查。
            existing = connection.execute(
                "SELECT c.embedding FROM knowledge_chunks c"
                "  JOIN knowledge_releases r"
                "    ON r.tenant_id = c.tenant_id AND r.id = c.release_id"
                " WHERE c.tenant_id = %s AND c.text_hash = %s"
                "   AND c.embedding IS NOT NULL AND r.embedding_model = %s"
                " LIMIT 1",
                (tenant_id, row["text_hash"], binding.model_id),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    "UPDATE knowledge_chunks SET embedding = %s "
                    " WHERE tenant_id = %s AND id = %s",
                    (existing["embedding"], tenant_id, row["id"]),
                )
                counts["reused"] += 1
                continue

            embedding = embedder.embed_one(
                str(row["body"]), timeout_seconds=EMBED_TIMEOUT_SECONDS
            )
            connection.execute(
                "UPDATE knowledge_chunks SET embedding = %s "
                " WHERE tenant_id = %s AND id = %s",
                ("[" + ",".join(repr(v) for v in embedding.vector) + "]", tenant_id, row["id"]),
            )
            counts["embedded"] += 1
            print(f"  embedded {row['ref']}  ({embedding.prompt_tokens} tokens)")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="整理好的 chunk JSON")
    parser.add_argument("--tenant", required=True, help="租户 uuid")
    parser.add_argument(
        "--embed",
        action="store_true",
        help=(
            "导入之后立刻向量化（要凭据）。"
            "**同一份 JSON 第二次跑 --embed**：若那一版还是 draft，"
            "就去把没嵌完的那些补上——中途失败之后的恢复路径就是它。"
        ),
    )
    parser.add_argument(
        "--check-only", action="store_true", help="只校验，不写库"
    )
    args = parser.parse_args()

    document = json.loads(args.source.read_text(encoding="utf-8"))
    entries = document["entries"]
    spec = str(document.get("meta", {}).get("batch") or args.source.stem)

    problems = validate(entries)
    if problems:
        print(f"{len(problems)} / {len(entries)} 条不合规，**整批不导入**：\n")
        for chunk_id, reasons in sorted(problems.items()):
            print(f"  {chunk_id}")
            for reason in reasons:
                print(f"      {reason}")
        # **整批拒，不是跳过坏的那几条。** 一批内容是一次发布的单位，
        # 而「导进去了一部分」会让下一个人以为这一版是完整的。
        return 1
    print(f"{len(entries)} 条全部合规。")
    if args.check_only:
        return 0

    url = os.getenv("FASTAGENT_DATABASE_URL", "").strip()
    if not url:
        print("FASTAGENT_DATABASE_URL 没有设置", file=sys.stderr)
        return 2

    tenant_id = UUID(args.tenant)
    # **`dict_row`，不是默认的元组** —— 下面几处按列名取。
    # 第一版用的是默认 row factory，于是 `row["text_hash"]` 在真实导入时炸了；
    # 单元用例没碰到，因为它们没有连接。
    with Connection.connect(url, row_factory=dict_row) as connection:
        # **`fastagent_app` 受策略约束** —— 不设这个就一行都写不进去，
        # 而那正是隔离在生效的样子。
        connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_id),))
        try:
            release_id = create_release(
                connection, tenant_id=tenant_id, entries=entries, spec=spec
            )
        except BodyChangedWithoutNewVersion as clash:
            # **按 `AlreadyImported` 的形状收口**（复审 N-2）：先前这个异常
            # 全仓零捕获，所以那句人话**到不了操作者手里** —— CLI 仍然打一段栈，
            # 只是最后一行换成了中文。
            print(clash)
            return 5
        except AlreadyImported as already:
            # **`--embed` 要能把没嵌完的那一版补上**（独立审查 P2-2）。
            #
            # 先前帮助文本写着「不给就只导入，第二次再跑 `--embed`」——
            # **那条路走不通**：第二次跑同一份 JSON 在第一条语句就撞上
            # `content_hash` 已存在，`vectorise` 一次都不会被调到。
            #
            # **同一条也是中途失败之后的唯一恢复路径**：55 次真实调用在一个事务里，
            # 中途失败整批回滚，而回滚之后没有任何行可供复用——**花掉的钱要重花**。
            if not (args.embed and already.status == "draft"):
                print(already)
                return 3
            print(already)
            print("**但它还是 draft，而你给了 --embed** —— 去补没嵌完的那些。")
            counts = vectorise(
                connection, tenant_id=tenant_id, release_id=already.release_id
            )
            connection.commit()
            print(f"向量化完成：新嵌 {counts['embedded']} 条，复用 {counts['reused']} 条")
            return 0
        print(f"draft release {release_id}（{len(entries)} 条 chunk，向量待补）")
        if args.embed:
            counts = vectorise(connection, tenant_id=tenant_id, release_id=release_id)
            print(f"向量化完成：新嵌 {counts['embedded']} 条，复用 {counts['reused']} 条")
        connection.commit()
    print("**它是 draft** —— 发布是第 5 批的事，检索读不到它（裁定七）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
