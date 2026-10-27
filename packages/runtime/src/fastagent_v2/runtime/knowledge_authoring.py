"""人工整理规格的「可机检五条」，**这里是它们的归属**。

`docs/plans/knowledge-chunk-authoring-spec-2026-09-11.md` §一 列了五条
「全部能被脚本判定，所以它们是『拒绝』而不是『建议』」。
`050_knowledge_base.sql` 的头部逐条写了谁守，其中两条写着
**「归属第 4 批的导入器」**——本模块就是那个归属。

| # | 规则 | 谁守 |
|---|---|---|
| 1 | 正文 ≤ 900 字节（**线路**） | **本模块**（数据库那条 CHECK 是有意偏弱的下界） |
| 2 | 自足（五个禁用词） | **本模块守全部五个**；数据库守其中三个 |
| 3 | 四个字段齐全 | 数据库（`NOT NULL` + 长度下界）；本模块给出可读的理由 |
| 4 | 至少一个 source，带 `file` 与 `section` | 数据库（`source_spans` 那条 CHECK）；本模块同上 |
| 5 | ref 三段合法 | **`runtime.references.reference_refusal`**，本模块**调它不重写** |

## 为什么是「返回拒绝理由」而不是布尔

**沿用 `credentials_mode_refusal` 的形状**（ADR-0113 §五，该条自己写着
「规则被提成纯函数，**这是本条最该被继承的部分**」），
`reference_refusal` 也是这个形状。一个布尔只能让调用方说「这条不合法」，
而整理内容的人要的是**「哪一条、哪一处、差多少」**——
「超了 37 字节」和「第 3 个来源缺 `section`」是两件要分别去改的事。

## 第 1 条：**量真正会进提示词的那个串**

规格 §2.3 写死了这条，理由也写死了：

> `861` 是 `900 − 39` 的结果，而 `39` 取决于 ref 的长度。
> **ref 一旦超过 `kb-999`，骨架就变成 40 字节，861 就不对了。**
> 所以校验器**直接量线路形状**，不要去比 861。

**本仓为同型的错付过一次**：`providers.py` 先前按 `ensure_ascii=True` 量请求，
而传输真正发送的是 `ensure_ascii=False` 的串——同一份 payload，
线路 6,132 / 度量 9,690，于是自己拒掉了一个本来发得出去的请求。
**量就量真正要发的那一个。**

## 第 2 条：**五个词在这里全守，数据库只守三个**

`上文` / `如上` 是子串（「以上文档说明……」含前者，「如上海分公司」含后者），
而 PostgreSQL 这一侧没有分词器，**一条正则会拒掉合法正文**——
拒错一条的代价是发不了版，所以迁移只守另外三个。

**本模块守全部五个**，因为它在导入时跑、失败是「这条内容退回去改」，
而不是「整批发不出去」；**而且这里能给出命中的位置**，
让整理的人一眼看见是哪个词。

**它仍然可能误伤**（「以上文档」）。所以这两个词的拒绝理由里点明了这一点，
并给了绕法：改一个说法。**误伤时看得见原因，比拒得准更重要。**
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fastagent_v2.runtime.references import reference_refusal

#: 线路上限，用户裁定 2026-09-11（裁定八）。配 top-2 与当时那 1,931 预留。
#:
#: **它是「线路」口径**：`ensure_ascii=False`、紧凑分隔符，
#: 与 `worker/inference_model.py` 的 `generate` 写 `user_prompt` 那一层一致。
CHUNK_WIRE_LIMIT = 900

#: 规格 §一 第 2 条的五个禁用词。
#:
#: **前三个是四字定语**，拿不出一个把它们当子串含进去的正常词；
#: **后两个是子串**，见模块 docstring。分开列是因为两组的拒绝理由不同。
UNAMBIGUOUS_BACK_REFERENCES = ("如前所述", "见上一节", "前面提到")
AMBIGUOUS_BACK_REFERENCES = ("上文", "如上")

#: 规格 §一 第 3 条要求非空的四个字段。
REQUIRED_FIELDS = ("question", "type", "confidence", "source")


def wire_bytes(ref: str, body: str) -> int:
    """这条 chunk 进提示词时真正占的线路字节数。

    **形状与 `content_materials` 同形**（`{"ref": …, "text": …}`），
    因为它进的是同一个 `user_prompt`。
    """

    wire = json.dumps(
        {"ref": ref, "text": body},
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return len(wire.encode("utf-8"))


def over_wire_limit(ref: str, body: str) -> int:
    """超出多少字节；`0` 表示合法。"""

    return max(0, wire_bytes(ref, body) - CHUNK_WIRE_LIMIT)


@dataclass(frozen=True, slots=True)
class Refusal:
    """一条拒绝：规格的第几条、哪个字段、为什么。

    `rule` 是规格 §一 那张表的行号，**不是随手编的分类**——
    整理的人拿着它能直接翻到规格里那一条。
    """

    rule: int
    field: str
    reason: str

    def __str__(self) -> str:
        return f"[规格 §一 第 {self.rule} 条 · {self.field}] {self.reason}"


def chunk_refusals(entry: Mapping[str, object]) -> tuple[Refusal, ...]:
    """一条整理好的 chunk 不合规的**全部**理由，合规则空元组。

    **返回全部而不是第一条**：整理的人改一遍就想全改完，
    而「改完再跑一次又红一条」是三次往返变一次的那种差别。
    """

    found: list[Refusal] = []

    # --- 第 3 条：四个字段齐全 -------------------------------------------
    for field in REQUIRED_FIELDS:
        value = entry.get(field)
        empty = value is None or (isinstance(value, str) and not value.strip())
        if empty or (field == "source" and not value):
            found.append(
                Refusal(3, field, f"`{field}` 缺失或为空；四个字段都要有值")
            )

    body = entry.get("body")
    if not isinstance(body, str) or not body.strip():
        found.append(Refusal(3, "body", "`body` 缺失或为空"))
        # 正文没有，第 1、2 条无从谈起；先把能说的说完再返回。
        body = ""

    # --- 第 5 条：ref 三段合法（调唯一定义点，不重写） --------------------
    ref = _ref_of(entry)
    refusal = reference_refusal(ref, expected_kind="knowledge")
    if refusal is not None:
        found.append(Refusal(5, "ref", f"{ref!r}——{refusal}"))

    # --- 第 1 条：线路上限 ------------------------------------------------
    if body and refusal is None:
        # **ref 不合法时不量**：`over_wire_limit` 会把那个坏 ref 也算进去，
        # 报出来的「超了 N 字节」是对着一个不存在的 ref 算的，改起来会跑偏。
        over = over_wire_limit(ref, body)
        if over:
            found.append(
                Refusal(
                    1,
                    "body",
                    f"进提示词时是 {wire_bytes(ref, body):,} 字节，"
                    f"超出上限 {CHUNK_WIRE_LIMIT:,} **{over} 字节**。"
                    "按问题拆，不要按长度砍（规格 §5.2）",
                )
            )

    # --- 第 2 条：自足 -----------------------------------------------------
    for word in UNAMBIGUOUS_BACK_REFERENCES:
        if word in body:
            found.append(
                Refusal(
                    2,
                    "body",
                    f"正文出现「{word}」。检索按条取，取到一条就要能用——"
                    "它指着的那一段不会跟着来",
                )
            )
    for word in AMBIGUOUS_BACK_REFERENCES:
        if word in body:
            found.append(
                Refusal(
                    2,
                    "body",
                    f"正文出现「{word}」。检索按条取，它指着的那一段不会跟着来。"
                    f"**如果这是误伤**（例如「以上文档」里的「上文」、"
                    "「如上海」里的「如上」），换个说法即可——"
                    "这两个词是子串，这里分不出来，所以宁可误伤",
                )
            )

    # --- 第 4 条：来源 -----------------------------------------------------
    found.extend(_source_refusals(entry.get("source")))
    return tuple(found)


def _ref_of(entry: Mapping[str, object]) -> str:
    """`knowledge:<slug>:v<N>`，从 `id` 与 `version` 拼出来。

    **拼而不是读一个现成字段**：整理的 JSON 里没有 `ref`，
    它是 `id` 与 `version` 的函数（规格 §四），
    而让作者手写一遍等于给它第二个定义点。
    """

    slug = str(entry.get("id") or "")
    version = entry.get("version")
    return f"knowledge:{slug}:v{version}"


def _source_refusals(source: object) -> tuple[Refusal, ...]:
    if source is None or (isinstance(source, Sequence) and not source):
        # 第 3 条已经报过「缺失或为空」，这里不重复。
        return ()
    if not isinstance(source, Sequence) or isinstance(source, (str, bytes)):
        return (Refusal(4, "source", "`source` 要是一个数组"),)

    found: list[Refusal] = []
    for index, span in enumerate(source):
        where = f"source[{index}]"
        if not isinstance(span, Mapping):
            found.append(Refusal(4, where, "每一个来源都要是一个对象"))
            continue
        for key in ("file", "section"):
            value = span.get(key)
            if not isinstance(value, str) or not value.strip():
                found.append(
                    Refusal(
                        4,
                        where,
                        f"缺 `{key}`。`file` 是仓内路径、`section` 是能在该文件里"
                        "搜到的字符串——拿着两者去仓里搜要能落到一个地方（规格 §六）",
                    )
                )
    return tuple(found)
