"""一段素材在模型请求里**真正占掉多少字节**，只有这一个算法。

## 这个模块错过一次，而错的那一版讲得通

**2026-09-11 的第一版把口径定成了 `ensure_ascii=True`**，理由是
`worker/providers.py` 当时就是那样比 `MAX_MODEL_INPUT_BYTES` 的。
那个观察是对的，而**那道闸本身是错的** —— 它量的是一个**没有任何东西发送**的
字符串：`httpx._content.encode_json` 用的是
`ensure_ascii=False, separators=(",", ":")`，**同一份 payload 实际发出 65 字节而旧口径
量成 112，逐字节比对确认**。

生产上已经付过账：一次 plan 调用被拒在「16,400 字节」，
**拒它的是我们自己这道检查，不是供应商** —— 供应商那一份只有一半大
（运维报障 2026-09-11）。

「计划步固定底」那一批把闸改对了（`providers.py` 的 `wire_request`）。
**于是第一版的 `entry_cost` 变成了比闸严 1.66× 的东西** ——
151 段在架素材：转义 50,124 / 线路 30,129。
**方向是过严**：它会拒掉本来装得下的包，正是「封不了版」那一类。

**本模块现在量线路。**

> **留痕而不是抹掉**：第一版的推理是「让账本与闸一致」，那条推理没有错，
> 错在**没有去问那道闸量的是不是真的那个串**。
> 下一个看到两个口径并存的人会重走同一条路。

## 两件当时做对的事，不要连坐

1. **「账本与闸不是同一把尺子」这个发现是对的** —— 只是当时把错的那一边
   当成了标准；
2. **去掉那 1,931 的重复计数是对的**，它与单位无关：那份预留在独立审查
   2026-09-11 P1-1 之后已经被 `_check_stage_budget` 加进每回合的总量里，
   同一份减了两次。

## 为什么是「算」不是「拟合」

一个拟合（当时量到的是 `1.993x + 67`）的斜率取决于中文占比，换个语料就漂。
`entry_cost` 直接把序列化器真正会做的两层算一遍：先
`json.dumps(..., ensure_ascii=False)` 写进 `user_prompt`，
再作为一个**字符串值**进 payload —— **payload 那一层现在也是
`ensure_ascii=False`**，所以中文不再被翻倍。

`tests/contracts/test_prompt_bytes.py` 拿真实请求逐段比对，**残差恒等于 0**。
"""

from __future__ import annotations

import json


def _inner(value: object) -> str:
    """写进 `user_prompt` 那一层，与 `worker/inference_model.py` 的 `generate` 一致。

    **`ensure_ascii=False`，且不传 `separators`** —— 两点都是照抄那边的调用，
    而不是这里的偏好。第二点决定了数组分隔符是 `", "` 而不是 `","`，
    那两个字节正是 `ENTRY_SEPARATOR_BYTES` 的来历。
    """

    return json.dumps(value, ensure_ascii=False)


def _outer_bytes(value: str) -> int:
    """`value` 作为 payload 里一个字符串值时占的字节数，含它自己的两个引号。

    **`ensure_ascii=False`，与 `worker/providers.py` 的 `wire_request` 一致。**
    那一行才是比 `MAX_MODEL_INPUT_BYTES` 的那一行，而它量的是
    **httpx 真正发出去的那个 body**。
    """

    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


#: 空字符串作为 payload 里一个字符串值时的固定开销（两个引号）。
#: 算出来而不是写 2，因为「为什么是 2」比「2」值钱。
_EMPTY_STRING_BYTES = _outer_bytes("")


def entry_cost(ref: str, text: str) -> int:
    """一条 `{"ref": …, "text": …}` 素材加进请求后，请求会多出多少字节。

    量的是 `MAX_MODEL_INPUT_BYTES` 那道闸用的口径，也就是**线路**。

    **正文那部分就是它的 UTF-8 长度**（线路是 UTF-8，中文一个字三字节，
    没有 `\\uXXXX` 那一层）。所以这个函数与 `len(text.encode("utf-8"))`
    的差别**不在正文，在骨架**：`{"ref":…,"text":…}` 的引号、逗号、键名
    与 ref 本身，实测约 `20 + len(ref)`。一段 27 字节的短素材，骨架比正文长。

    > **这一行先前写着「中文大约按两倍计——因为它到了线路上真的就是两倍」。**
    > 那句话是 2026-09-11 那一版留下的，而**它描述的是转义口径**：
    > 线路上中文不是两倍。留痕见本模块顶部的 docstring。
    """

    return _outer_bytes(_inner({"ref": ref, "text": text})) - _EMPTY_STRING_BYTES


def asset_entry_cost(ref: str, label: str) -> int:
    """一条 `{"ref": …, "label": …}` 文件候选加进请求后，请求会多出多少字节。

    **与 `entry_cost` 并排，而不是复用它。** 键名不同（`text` 四个字母 vs
    `label` 五个），所以拿 `entry_cost` 顶替**每一条少算 1 字节** ——
    实测：`asset:price-list:v1` + `price-list` 这一条是 **61**，顶替算成 60。

    一个字节看着可以忽略，而**少算的方向是 fail-open**：封版说得下，
    真实回合超出去。这个仓为 fail-open 的少算付过账（那次是 76 字节，
    兜住它的是安全余量那一格），所以这里不借。

    **推出来的，不是估的**：走的是与 `entry_cost` 逐字相同的那条路
    （真的编码一次，减掉空串基线），所以它跟着编码口径走，
    不需要在口径变的那天被人记起来改。
    """

    return _outer_bytes(_inner({"ref": ref, "label": label})) - _EMPTY_STRING_BYTES


def text_cost(text: str) -> int:
    """一段**裸文本**进到提示词里要占的字节数，同一口径。

    给那些不是 `{ref, text}` 形状的提示词字段用。
    """

    return _outer_bytes(_inner(text)) - _EMPTY_STRING_BYTES


def document_cost(document: object) -> int:
    """一份**嵌套文档**（不是裸文本、也不是 `{ref, text}`）进提示词要占的字节。

    `understanding` 走的就是这条：它整份被塞进 `user_prompt`，而
    `user_prompt` 又作为 payload 里的一个**字符串值**。**所以是两层**，
    与 `entry_cost` 同构，只是内容不是一段素材。

    **这个函数是被审查逼出来的**（独立审查 2026-09-12，T282 批 P1-1）：
    `understanding_bound` 当时自己写了第四把尺子 ——
    `json.dumps(doc, ensure_ascii=False, separators=(",", ":"))` ——
    **两层都不对**：真实那一层不传 `separators`（默认 `", "`），
    而外面那一层每个 `"` 要变成 `\\"`。实测偏差 **1.075× – 1.163×**，
    **方向是 fail-open**，而它让那一批的兑现物用例在说谎：
    bound 完的文档量成 1,431 ≤ 1,580，**真实进请求 1,601**。

    **所以尺子只有这一处**。谁要量「一坨东西进提示词占多少」，调这个，
    不要顺手写一个 `json.dumps`。
    """

    return _outer_bytes(_inner(document)) - _EMPTY_STRING_BYTES


#: 数组里把两段素材连起来的那个分隔符占的字节数。
#:
#: **是 2 不是 1，而这一点是被用例抓出来的**：`inference_model.generate` 调的是
#: `json.dumps(..., ensure_ascii=False)`，**没传 `separators`**，所以数组分隔符
#: 是默认的 `", "`——一个逗号**加一个空格**。`providers.py` 那一层确实传了
#: `separators=(",", ":")`，而那个参数管的是 payload 本身，
#: 管不到已经序列化好的那个字符串里面。
ENTRY_SEPARATOR_BYTES = 2


def budgeted_asset_entry_cost(ref: str, label: str) -> int:
    """预算口径的单条文件候选成本：`asset_entry_cost` 再加上它前面那个分隔符。

    与 `budgeted_entry_cost` 同一个理由 —— 第二条起每条前面都有一个 `,`，
    而封版要算的是「再加一条会多多少」，那个逗号是它的一部分。
    """

    return asset_entry_cost(ref, label) + ENTRY_SEPARATOR_BYTES


def budgeted_entry_cost(ref: str, text: str) -> int:
    """预算口径的单段成本：`entry_cost` 再加上它前面那个分隔符。

    **一个回合 n 段实际只有 n−1 个分隔符，所以整回合这样算会高估恰好 2 字节。**
    高估是安全的那一侧，而换来的是**预算不必知道一个桶里最终会有几段**——
    `_check_stage_budget` 把素材分进 `per_stage` / `topic_bytes` / `always_on`
    几个桶再按回合组合，要精确数分隔符就得给每个桶再挂一个计数器，
    而那是在一条 admission 路径上为 2 字节增加结构。

    `test_prompt_bytes.py` 把「恰好 2 字节」钉住，所以这个近似不会悄悄变大。
    """

    return entry_cost(ref, text) + ENTRY_SEPARATOR_BYTES
