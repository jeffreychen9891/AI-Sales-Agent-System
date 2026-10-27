"""`material:` 与 `knowledge:` 引用的三段语法，**只有这一个定义点**。

**它先前没有规格。** `material:demo-opening:v1` 这个形状在 `profiles/` 下的真实包里
出现 **169 次、164 个不同值**（`content_materials` 160 + `materials.json` 键 151
+ `tone_material_ref` 8 + `knowledge_bindings` 1；`materials.json` 的键无一例外
已经出现在某个帧的 `content_materials` 里，所以它对去重后的计数没有贡献），
而**没有任何 schema 约束过它的三段各是什么**：`merchant-profile.schema.json` 对
`content_materials` 的裸引用只说 `{"type": "string", "minLength": 1}`，
对 `knowledge_bindings` 与 `tone_material_ref` 都只说 `{"type": "string"}`。

**今天没有一处代码解析它**——`channel.customer_text` 这类读者把它当成
**一整个字典键**用（`materials.get(str(segment["content_or_ref"]))`）。
**所以「没有规格」今天还不疼**，疼的是下一步：向量化要按 slug 归组、
证据要把 ref 拆回 slug 与版本去记「当时引的是哪一版」。

**一个没有规格的标识符，会在第一个需要拆它的人手里被拆成他以为的样子**——
而第二个人会拆成另一个样子。这一份规格是在**第一个拆它的人出现之前**写的，
这是它最便宜的时刻。

## 三段各是什么

    <kind>:<slug>:v<N>

* **`kind`** —— 闭合词表 `REFERENCE_KINDS`。**两类不可混用**：素材是商家逐字
  审过的话术，知识是整理出来的资料，**冲突时素材赢**
  （`docs/plans/knowledge-base-schedule-2026-09-11.md` §5.2.2「裁定四」）。
  **这个出处先前写的是「ADR-0127 那一格的裁定四」，是错的**——ADR-0127 只裁了两件
  （release 独立生命周期、会话不 pin），全文没有「裁定四」也没有「素材优先」，
  而全仓唯一叫「裁定四」的是 ADR-0070 §五，讲的是 tenant-less 资源归类。
  留痕是因为**一个写错的 ADR 编号会被下一个引用它的人继续传下去**；
* **`slug`** —— 小写字母数字，连字符分隔，**永不复用**。一个 slug 用过之后，
  即使那条内容被删也不给别的内容。**理由**：回复里的引用会留在会话记录里，
  复用 slug 等于让历史引用悄悄指向别的内容——**而那是安静的**；
* **`v<N>`** —— 这一条内容**自己的修订号**，正文改一个字就 +1。
  **它不是 release 的版本号**：一次发布可能只改了几十条里的一条，
  而证据要答得出「当时引的是哪一版正文」。

完整的人工整理规格：`docs/plans/knowledge-chunk-authoring-spec-2026-09-11.md` §四。

## `policy:` 同形，**但它不在这个词表里**

仓里还跑着第三族同形标识符：`policy_bindings.*` 与 `confidence_gate_policy_ref`
下的 13 个 `policy:<slug>:v1`，三段形状与本语法逐字一致，
而 `REFERENCE_KINDS` 里没有 `policy`——`parse_reference("policy:authorization:v1")`
今天会抛 `ValueError`。**这是有意的，写在这里免得下一个人当成漏项补进去：**

本词表管的是**内容引用**——**指向一个会进提示词、并且模型要在候选里选中它、
再写回生成结果的东西**。`policy:` 指的是**一份策略文档**，它绑定的是机器的行为
而不是要说的话，**模型从头到尾看不见它**。而 `REFERENCE_KINDS` 的入会条件
（见下面那条注释）是「**它和素材冲突时谁赢**」——这一问对 `policy:` 根本不成立：
策略和素材不是两个可以互相顶替的来源，它们在管线上是两层。
**一个答不出入会问题的类别，加进闭合词表就等于把词表打开了。**

**`asset` 为什么在里面而 `policy` 不在——分界线是「模型看不看得见」。**
2026-09-12 加 `asset` 时，这条分界线才第一次被写准，**在那之前它写的是
「指向一段会进提示词的话」**，而 asset 指的是一个文件不是一段话，
照那句字面读会得出「asset 不该进」的结论——**E3 的 ADR 初稿就是这么读的，
读错了**（ADR-0130 §二.2 初稿；该条**尚未合入 `main`**，已在它自己那一批里
改正原稿并留痕，**不是点名修订——点名修订是给已经在图纸上的东西用的**）。

真正的判据是：**`asset:price-list:v1` 会进提示词的候选清单、会被模型选中、
会出现在生成结果里，与 `material:` 逐字同一条路径**；
而 `policy:authorization:v1` 三样一样都不会。
**这才是 `policy:` 出局的理由，「它不是一段话」不是。**

真要让 `policy:` 也有语法，正确的落点是**另一个词表**（或一个显式的
「同形但另一族」的解析入口），不是往这一个里塞。

## 形状统一，**被指的东西随位置不同**

同一个三段形状在两个位置上指的不是同一类东西，**这一条要说破**：

* 帧的 `content_materials` 与 `materials.json` 的键里，
  第二段是**一段话术**、第三段是**它自己的修订号**；
* `knowledge_bindings` 里，RFC 写的是「collection release ref」——
  第二段是**一个集合**、第三段是**那个集合的 release 版本**；
* 帧的 `content_assets` 里（E3-2b 起），第二段是**一份文件**、
  第三段是**那份文件自己的版本号**——**版本在 `materials` 表里，不在 profile 里**
  （ADR-0130 §二.2：profile 声明 slug 不声明版本，发送时解析当前那一版）。
  **所以这一段今天在 profile 里写 `v1` 只是语法占位，读它的人要知道
  真正决定发哪一版的不是它。**

**语法不区分这两者，而这是故意的**：两边要回答的问题一样
（指的是哪一个、哪一版），而让它们长得不一样的唯一好处是
“读的人能一眼分出来”——而第一段已经干了这件事。

## 为什么是「返回拒绝理由」而不是「返回布尔」

**沿用 `credentials_mode_refusal` 的形状**（ADR-0113 §五，该条自己写着
「规则被提成纯函数，**这是本条最该被继承的部分**」）。
一个布尔只能让调用方说「不合法」，而**作者要的是「哪一段不合法」**——
第二段有大写字母和第三段写成 `v0`，是两件要分别去改的事。

## 为什么语法不写进 JSON Schema

**写进去就是两个定义点。** schema 管形状（是不是字符串、空不空），
这里管规则（三段各是什么、为什么）。**一条带理由的规则放在能解释自己的函数里**，
比放在一个 `pattern` 里更可能被下一个人读懂——而 `pattern` 与本函数一旦漂开，
**漂开的那一刻没有任何东西会红**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 闭合词表。**加一类不是加一个字符串**：加进来的那一类要同时回答
#: 「它和素材冲突时谁赢」，否则生成步拿到两种来源不知道听谁的。
#:
#: 三类各自的答案，逐条写下来——**这就是入会凭证**：
#:
#: * ``material`` —— 商家逐字审过的话术。**冲突时它赢**；
#: * ``knowledge`` —— 整理出来的资料，检索出来引用作答。**输给素材**
#:   （知识库那一格的裁定四，`docs/plans/knowledge-base-schedule-2026-09-11.md` §5.2.2）；
#: * ``asset`` —— **要发给客户的那个文件**（E3 资料，ADR-0130）。
#:   **它和素材不争同一个位置，所以「谁赢」这一问在它身上的答案是「不必裁」**：
#:   素材决定**说什么话**，asset 决定**这一条回复要不要附一个文件**。
#:   一条回复可以两者都有、也可以只有一个。
#:   **但「不必裁」是一个答案，不是答不出**——见下面 ``policy:`` 那节的分界。
REFERENCE_KINDS = frozenset({"material", "knowledge", "asset"})

#: slug：小写字母数字，连字符分隔，不以连字符开头或结尾、不连续两个连字符。
#: **实测四个面上的 164 个不同引用全部符合**，没有一个用到大写或其他字符。
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: 版本：`v` 加正整数，**不含前导零**。`v01` 与 `v1` 指同一条内容而字符串不等，
#: 而引用是被当作字典键用的——两种写法就是两条内容。
_VERSION = re.compile(r"^v([1-9][0-9]*)$")


@dataclass(frozen=True, slots=True)
class Reference:
    """一个解析好的引用。`raw` 留着，因为它才是字典里的那个键。"""

    kind: str
    slug: str
    version: int
    raw: str


def reference_refusal(reference: object, *, expected_kind: str | None = None) -> str | None:
    """这个引用为什么不合法，合法则 `None`。

    `expected_kind` 是调用方知道自己在读哪一类时传的——
    `knowledge_bindings` 里出现一个 `material:` 引用是个真实的错误，
    而不传这个参数就看不出来。
    """

    # **调用方的那一半也会写错，而写错的表现是这一面变成「拒绝一切」。**
    # `expected_kind="knowlege"` 时下面那条比较永不为真，拒绝理由念起来还挺像话
    # （「这里只接受 'knowlege' 引用」），于是一个面静静地拒掉它本该放行的所有值。
    # 今天三个调用点都对，所以这是韧性而不是缺陷——但它是 `assert` 花得起的那种。
    assert expected_kind is None or expected_kind in REFERENCE_KINDS, (
        f"expected_kind={expected_kind!r} 不是已知类别；"
        f"只有 {sorted(REFERENCE_KINDS)}"
    )

    if not isinstance(reference, str):
        return f"引用必须是字符串，这里是 {type(reference).__name__}"
    if not reference:
        return "引用是空字符串"

    parts = reference.split(":")
    if len(parts) != 3:
        return (
            f"引用要三段 `<kind>:<slug>:v<N>`，这里有 {len(parts)} 段"
            + ("（是不是漏了版本段？）" if len(parts) == 2 else "")
        )

    kind, slug, version = parts
    if kind not in REFERENCE_KINDS:
        return f"第一段 {kind!r} 不是已知类别，只有 {sorted(REFERENCE_KINDS)}"
    if expected_kind is not None and kind != expected_kind:
        return f"这里只接受 {expected_kind!r} 引用，而第一段是 {kind!r}"
    if not _SLUG.match(slug):
        return (
            f"第二段 {slug!r} 不是合法 slug：只能是小写字母数字，"
            "用单个连字符分隔，且不以连字符开头或结尾"
        )
    if not _VERSION.match(version):
        return f"第三段 {version!r} 不是合法版本：要写成 `v1`、`v2`……（正整数，无前导零）"
    return None


def parse_reference(reference: str, *, expected_kind: str | None = None) -> Reference:
    """解析一个引用；不合法就抛 `ValueError`，理由与 `reference_refusal` 同一句。

    **两个函数共用同一条规则**——这个走的就是 `reference_refusal`，
    不另判一遍。
    """

    refusal = reference_refusal(reference, expected_kind=expected_kind)
    if refusal is not None:
        raise ValueError(f"{reference!r}：{refusal}")
    kind, slug, version = reference.split(":")
    return Reference(kind=kind, slug=slug, version=int(version[1:]), raw=reference)
