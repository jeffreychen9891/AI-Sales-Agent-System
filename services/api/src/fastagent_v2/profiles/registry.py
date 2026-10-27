"""Discovery and validation of trusted profile packages.

A "trusted package" is a `profile.json` under a registry root that is inside the
repository -- version controlled, reviewed, and reachable by the birth gates.
The registry never reads a profile from the database, from a request body, or
from an operator-supplied path outside the repository: those are the shapes that
let free-form prompt text become authoritative configuration, which RFC §9.1
forbids.

That sentence used to be a claim in this docstring with nothing behind it. The
independent review (2026-09-02, P1-1) showed the CLI's `--root` accepted any
path, and that a package outside `relative_to` silently fell back to recording
its absolute path as `source_path` -- so an out-of-tree profile carrying the
merchant-specific words the birth gate forbids could be published, and would be
recorded with a `source_path` that reads like a repository-relative one. Both
holes are now closed by `REPOSITORY_ROOT` below and by `load_package` refusing,
rather than falling back, when a package sits outside `relative_to`.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from fastagent_v2.profiles.protocol import (
    ProfileNotFound,
    ProfilePackage,
    ProfilePackageError,
)
from fastagent_v2.runtime import request_shape
from fastagent_v2.runtime.contracts import (
    DEPLOYMENT_ROOT,
    PLATFORM_FRAME_ID_PREFIX,
    is_platform_frame,
    validate_contract,
)
from fastagent_v2.runtime.prompt_bytes import (
    budgeted_asset_entry_cost,
    budgeted_entry_cost,
    document_cost,
)
from fastagent_v2.runtime.references import reference_refusal
from fastagent_v2.runtime.topic_vocabulary import allowed_slot_values
from jsonschema.exceptions import SchemaError, ValidationError

#: Filename a profile package is published from. One profile per directory: the
#: directory name is the package, so a package cannot quietly grow a second
#: profile that no manifest mentions.
PACKAGE_FILENAME = "profile.json"

#: Sibling file carrying the **text** of the materials a profile declares.
#:
#: `target_frames[].content_materials` has always held *references*
#: (`material:demo-opening:v1`). Nothing held the words. The generation step was
#: therefore writing every customer-visible sentence itself -- the profile could
#: name a material and the model would still compose its own version of it, which
#: is precisely what「素材只能由 profile 声明、模型不得现场编造」forbids.
#:
#: **Why a sibling file and not a key inside `profile.json`.** The material text
#: is the part a merchant edits most and reviews as prose; `profile.json` is
#: structure. Keeping them in one file means every wording fix rewrites the
#: document a reviewer reads for machinery. Keeping them in two files and
#: **folding the sibling into the same `content_hash`** costs nothing and keeps
#: both properties: prose is editable on its own, and a package whose words
#: drifted from its hash is still a package nobody reviewed.
#:
#: **It is optional.** A package without this file behaves exactly as it did
#: before: no texts, and the generation step is told about no materials. The two
#: synthetic packages shipped today are in that state on purpose -- see
#: `_load_material_texts` for what is required once a package opts in.
MATERIALS_FILENAME = "materials.json"

#: Where the loaded material texts are placed in the profile document.
#:
#: **Merged into the document rather than carried beside it** so that everything
#: downstream -- `canonical_hash`, the `profile` column of `profile_releases`,
#: and `prepared.profile` in the worker -- gets the texts by already doing what
#: it already does. A parallel field would have to be threaded through the
#: publisher, the binding and the pipeline, and the first place it was forgotten
#: would be a conversation where the model quietly went back to composing.
MATERIAL_TEXTS_KEY = "content_material_texts"

#: profile 顶层声明的记忆字段（E4，ADR-0131 M2）。
MEMORY_FIELDS_KEY = "memory_fields"

#: **这里原本有一个 `MEMORY_VALUE_MAX_BYTES = 1024`，和一道「可证装不下即拒」的闸。
#: 两者都被撤掉了（用户 2026-09-12 裁定，独立审查 P1-1）。**
#:
#: 它抄的是 `054_contact_memories.sql` 的
#: `CHECK (length(field_value) BETWEEN 1 AND 1024)`——**而 `length()` 数的是字符，
#: 不是字节**（`050` 在同一件事上刻意用的是 `octet_length`）。
#: 用仓里唯一的字节权威 `runtime.prompt_bytes.text_cost` 实测 1024 字符的一条取值：
#:
#:     ascii  1,028 字节  -> 最多 15 条
#:     中文   6,148 字节  -> 最多  2 条
#:     emoji 12,292 字节  -> 最多  1 条
#:
#: **而那道闸只在 n≥17 时拒**：一份 16 条中文声明的可证最坏是 98,368 字节，
#: **是整个请求预算的六倍，它封得了版。**
#:
#: **这正是 `prompt_bytes.py` 的模块 docstring 为之而写的那个错**（素材预算曾按
#: 「1.0 × 正文字节」算，低估约两倍，「闸以为装得下，真实装不下」），
#: 而本批把同一个错又做了一遍、并且更远一步——**字符，不是 UTF-8 字节**。
#:
#: **为什么不是改用 `text_cost` 重算**（审查方给的另一条路）：那样中文门槛会落到
#: **n≥3**，几乎拒掉一切真实声明——**那是一次会改变产品形状的收窄，不是一次修正**。
#: 用户裁定：**承认这道闸今天给不出有意义的上界，把条数上限连同「记忆在提示词里
#: 占多少份额」一起推给 E4-5**，登记 T300。
#:
#: **留下的两道闸不受影响**：禁止项绊线与重复 key，两者都与字节无关。

#: 平台不许记的四类（ADR-0131 M2）。**这是一条绊线，不是一份合规保证**——
#: 理由写在 `_check_memory_fields` 的 docstring 里，不在这里重复。
#:
#: **它与 ADR-0125 §五「代码里不写任何分类词表」不同族**，这一点要说破：
#: §五 管的是**商家的流程分类**（topic / 阶段），平台写进代码就是替商家定义流程；
#: 而本表是**平台自己的禁止边界**，ADR-0131 M2 逐字把它判给平台。
#: **一个是「不该由平台定义的」，一个是「只能由平台定义的」。**
MEMORY_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "id_card", "idcard", "identity_card", "id_number", "passport",
    "card_number", "bank", "payment", "credit",
    "health", "medical", "illness", "diagnosis",
    "religion", "faith",
)

#: Frame-level key naming the slot an `on_request` material's topics are matched
#: against. Spelled here and in `worker/material_selection.py` because the worker
#: does not import from `services/api`; the two are pinned equal by a test.
MATERIAL_TOPIC_SLOT_KEY = "material_topic_slot"

#: 这个帧的 topic 槽最多同时点名几类，用来给字节预算留准备金。
#:
#: **不声明就按全部 topic 桶算**——缺省必须落在安全那一侧。声明一个更小的数
#: 等于 profile 作者承担「goal 真的把累积封在这个数以内」这件事，
#: 而那一致性今天没有东西守着（登记 T274）。用户 2026-09-11 裁定演示包封顶 3 类。
MATERIAL_TOPIC_CONCURRENCY_KEY = "material_topic_concurrency"

#: Frame-level key declaring the ordered stages. Spelled here and in
#: `worker/material_selection.py`; a test pins the two equal.
MATERIAL_STAGES_KEY = "material_stages"

#: 帧声明的**文件**资料（E3，ADR-0130）。与 `content_materials` 共用
#: `material_topic_slot` / `material_stages` 两道闸，**不另造一对**——
#: 它们本来就是 ADR-0125 §二 定的同两道闸。
CONTENT_ASSETS_KEY = "content_assets"

# **`PER_MATERIAL_OVERHEAD_BYTES` 已删除，留痕说明它错在哪。**
#
# 它是「98,133 ÷ 301 段减去正文」反推的，用法是
# `PER_MATERIAL_OVERHEAD_BYTES + len(text.encode("utf-8"))`——
# 而卡 `MAX_MODEL_INPUT_BYTES` 的那道闸量的是**线路**（`ensure_ascii=False`，
# httpx 真正发出去的那个 body）。**这一行先前写的是 `ensure_ascii=True`**，
# 那是 2026-09-11 那一版的错口径，见 `runtime/prompt_bytes.py` 顶部。
# **两把尺子不同**：197 把骨架高估约 2.9 倍（真实约 67），
# 而 `1.0 ×` 把中文正文低估约 2 倍。两个错在正文 131 字节处抵消，
# 而语料中位数是 125，所以它长期看起来是对的。
#
# **低估的那一半是活的洞**：用最长那段填满 6,132 的预算是 10 段、账本 6,120，
# 真实 8,770，而余量只有 8,063——封得了版，每个真实回合都
# `ProviderInputTooLarge`。
#
# 现在用 `runtime.prompt_bytes.entry_cost`，**它是算出来的不是拟合的**
# （实测残差恒为 0）。整段推导见那个模块的 docstring。

#: 一个回合能塞给模型的素材字节数上限，与
#: `worker.material_selection.MATERIAL_BYTES_BUDGET` **写两遍、由用例钉死相等**。
#:
#: 写两遍而不是导入：`services/api` 不从 `services/worker` 导入——那条边界这一批
#: 已经为 `MATERIAL_TEXTS_KEY` 与 `MATERIAL_TOPIC_SLOT_KEY` 各写过一次，
#: 这里也一样。**第一版把它 import 过来了，被
#: `test_the_registry_trusts_the_deployment_it_runs_in_not_the_tree_it_was_built_from`
#: 抓住**——registry 要能在只装了 api 的部署里加载。
# **值取 `main` 的 7,063**，理由与去重计数写在
# `worker/material_selection.py` 那份 docstring 的「2026-09-12」一节：
# 6,470 是 `8,401 − 1,931` 算的，而那 1,931 已经证明是重复计数。
# **两处写两遍、由用例钉死相等**，所以这里跟着那边走。
# **2026-09-18（批 E）32,169 -> 29,186**：生成步的 `user_prompt` 多了两样 ——
# 上一轮那句话、以及**已经讲过哪几段**（用户当场更正：「上一句不一定是关键，
# 之前的内容才是」）。空请求基线 +683，两个变长项各扣一行（-1,200 / -1,100）。
# **安全余量一个字没动。** 推导式的正文在 `worker/material_selection.py`。
# **2026-09-19（批 G）29,186 -> 28,868**：空请求基线 +318（生成步系统提示多了
# 「许了就把 X 放进下一条 `send_message`」那四行）。**推导式一个字没动，
# 安全余量一个字没动。**
# 2026-09-20（T200 / E4 第 4 批）27,385 -> 24,923：滚动摘要进了三个提示词。
# 两笔：空请求基线 +62（三处各两个空键），变长那一项扣一行 2,400
# （`material_selection.SUMMARY_BYTES_RESERVED` —— ADR-0122 §三 那条继承项
# 的兑现物）。**安全余量一个字没动。**
# **2026-09-21（一条说一件事）24,923 -> 24,572**：空请求基线 +351
#（生成步系统提示多了「每条 60 字上限、讲不完续到计划列的下一条」那几行）。
# **推导式一个字没动，安全余量一个字没动** —— 与批 E / 批 G / T363 同一种记法。
# **2026-09-21（教模型怎么记）24,572 -> 23,984**：空请求基线 +588
#（生成步系统提示 +559，外加 `user_prompt` 多一个空的 `rememberable_fields`
# 键 +29）。**推导式一个字没动，安全余量一个字没动。**
#
# ⚠️ **这一笔是 rebase 之后整笔重量的**：同日先落地的「一条说一件事」
# 已经把基线推到 24,572，两笔叠加后的差值只能实测，不能相加旧数。
#: **2026-09-23（ADR-0151）23,984 -> 23,573（-411）**：生成步提示词换掉了
#: 「每条至多 60 字」那一段，改成「没有字数限制，长答案自己分段」。
#: **新增的那 411 字节从这个池子里扣**，不让安全余量兜 ——
#: 批 E 立的规矩，`test_no_unfounded_facts_about_the_customer` 钉着它。
#: ⚠️ **这一条先前写的是「-> 23,639（-345）」，而这个常量从来没有取过 23,639。**
#: 那批自审里估的是 -345，落地时门禁量到的是 -411，**只改了值没回来改这句话**。
#: 2026-09-23（运维缺陷 18）核对本条时发现，一并改正 —— 一条写错了数的账，
#: 与没记账一样：下一个人按它去推下一笔，会推出一个不存在的起点。
#: **2026-09-23（运维缺陷 15 / 16）23,573 -> 22,579（-994）**：补记。
#: 那一批落地时没在这里留行。**994 是生成步那一笔**（不许开口就回执、
#: 不许复述、不许说回头再讲）；同批理解步的 +149 **不进这个池子**，
#: 理由见下一条。
#: **2026-09-23（运维缺陷 18 / v16）一个字不动。**
#: 本批只改了 `_UNDERSTAND_SYSTEM`（实测 +166），而**这个池子是 `generate` 的**：
#: 钉它的那条恒等式（`test_the_empty_request_baseline_is_measured_from_the_real_payload`）
#: 量的是 `request_shape.generate_prompt` 造出来的空请求基线，
#: **理解步的 system prompt 不在那份 payload 里**。
#: ⚠️ **所以「改了提示词就从池子里扣」不是一条按提示词计的规矩，是按 payload 计的** ——
#: 照字面执行会把一笔不在这个池子里的开销扣在这里，池子凭空缩一截而没有任何东西变红。
#: **2026-09-23（运维缺陷 19）22,579 -> 21,214（-1,365）**：三笔一次记清 ——
#: 生成步 system prompt +337（`pending_question` 那条规则）、
#: 空请求基线 +28（`user_prompt` 多一个空键）、
#: 变长项新扣一行 1,000（`PENDING_QUESTION_BYTES_RESERVED`，推导式加了一项）。
#: **安全余量一个字没动**；计划步同批的 +264 不在这个池子里（它是 plan 的固定底）。
#: 2026-09-24（缺陷 21）-1：生成步词长参照那一句 +1 字节，照旧从池子出。
MATERIAL_BYTES_BUDGET = 21213

#: 一个模型请求的硬上限，与 `worker.cost.MAX_MODEL_INPUT_BYTES` 写两遍、用例钉死。
MAX_MODEL_INPUT_BYTES = 49_000

#: 三个平台提示词各自的**线路**字节数，与 `worker.inference_model` 的三个常量
#: 写两遍、用例钉死相等（同上那条边界理由）。
#:
#: **为什么 admission 要知道它们**（运维报障 2026-09-11）：`MAX_MODEL_INPUT_BYTES`
#: 是**三种请求共用的一个上限**，而在此之前 admission 只建模了其中一种
#: （generate）的其中一项（`content_materials`）。understand 与 plan 的请求里
#: **各带着整段 `goal`**——**每回合付两次**——而没有任何东西在检。
#: 于是包封得过版、部署得上去，然后某一回合的计划调用在本地被拒、回落保守计划、
#: 不发消息，而每一道门禁都是绿的。
#:
#: **这是同一个洞的第三次发作**：某一处建了闸，另一处没有，两处共用同一个上限。
#: 前两次是「帧不声明阶段就整条闸跳过」与「topic 桶整个不在 admission 里」。
#: **6c（2026-09-12）把 GENERATE_VALUE 抬了 136**：generate 提示词多了一行
#: 「knowledge_chunks 是参考不是可引的话术」（ADR-0135）。
#: **那 136 吃的是 ADR-0132 §八 那张表的余量**（每次 generate 都付的固定底），
#: 于是余量 188 → ADR-0135 的额度 1,800 → 1,826 拿掉 **26** → 本批的接线固定底
#: 再拿 **162**（提示词一行 136 + 空的 `knowledge_chunks` 键在**线路上** 26）
#: → **剩 0**。
#:
#: **先前这里写的是「拿掉 50 → 剩 2」，那两个数都错**（独立审查 2026-09-12 P2-1）：
#: `50 = 26 + 24` 用的是空键的**原始**字节 24，而它进线路要付 **26**
#: （外面那一层把 `user_prompt` 当字符串值再转义一次，两个引号各 +1；
#: 实测：空请求带这个键 **8,174**、不带 **8,148**）。**这一处是全仓唯一说过「剩 2」的地方**，
#: 而 ADR-0135 §三 把「余量 = 0 / 池子满了」写成了留给下一个人的那句话 ——
#: 一句「剩 2」正好把它作废。
#: **这张表从此没有富余**：下一个往任何提示词里加字的人，先重推它（§八 第 2 条原话）。
#: **2026-09-13（裁定一 ②）：`PLAN_CAPABILITY` 2,703 → 3,115，实测 +412。**
#: plan 的 system prompt 加了一句指令 —— 「`required_slot_requests` 必须点名
#: `required_slots_by_strategy` 里那些仍然空着的槽位」。
#:
#: **这不是「改常数」，是实测值跟着被测对象走**（用户 2026-09-13）：
#: 这张表与三个 system prompt 的相等由
#: `test_profile_materials_are_trusted::test_the_platform_prompt_table_matches_the_prompts`
#: 现量现比，所以**改了提示词而这里没跟着改，门禁当场红** ——
#: 实测过：反向探针里往 `_PLAN_SYSTEM` 加一行，红的正是那一条。
#:
#: **为什么这一句进 system prompt 而不是 `user_prompt`**：它是**恒定**的规则
#: （每回合一模一样），而 system prompt 正是 prompt caching 的稳定前缀。
#: 同一批的 `required_slots_by_strategy`（**每回合不同的数据**）进的是
#: `user_prompt` —— 两者分开放，依据是同一条判据：**恒定的进前缀，逐轮的不进。**
#:
#: 加完之后在架三份配包的 plan 固定底：最坏 10,174 / 16,384（62%，
#: `internal-demo-sales` 的 `introduce` 帧），八个帧全部有余量。
#: **2026-09-16（E3-4）：`GENERATE_VALUE` 4,589 → 4,816，实测 +227。**
#: 生成步的系统提示词多了三行：`content_assets` 是文件不是话术、
#: 要发就单独提一个 `send_material`、它的 ref 不进 segment。
#: **这个数与下面那个 32,169 是同一笔改动的两半** —— 提示词 +227，
#: 空的 `content_assets` 键在线路上 +24，合计 +251 从素材预算里出。
PLATFORM_PROMPT_BYTES = {
    # 2026-09-20（T200 的后半）2,910 -> 3,167（+257）：**理解步的提示词终于
    # 把 `turn_summary` 告诉模型了**。契约 T200 那批就把它设成必填，而提示词
    # 的 `Required keys` 从没列过它、同一段还写着 `Do not add properties.` ——
    # stage 上部署后第一条真实客户消息因此被整份判死
    # （`PROVIDER_MALFORMED_OUTPUT`，2026-09-20T10:52:04Z，零条出站）。
    # 经过与那条会红的守卫见 `test_the_prompt_asks_for_every_key_the_contract_requires`。
    # 2026-09-21（ADR-0147）+1,258：理解步把 `interruption.relation` 那六个词
    # **各按后果教了一句**。在此之前它只有一行枚举，六个词一个解释都没有 ——
    # 而每一个都会安静地改变平台接下来能做什么（`correction` 解锁覆盖槽位、
    # `exit`/`human_request` 是会留痕的、`side_question` 是唯一能开子帧的）。
    # 2026-09-23（运维缺陷 15 / 16）：理解步 +149（`turn_summary` 改成只写
    # 客户那一半）、生成步 +994（不许开口就回执、不许复述、不许说回头再讲）。
    # ⚠️ **计划步那一格不是本批改的** —— 它是 ADR-0151 留下的漂移：
    # 那一批只改了 `REQUEST_OVERHEAD_BYTES`（有闸在报），
    # **而这张表没有任何闸在量它**，于是它安静地错了一整批。
    # 本批一并结清，并给它补了一条会红的守卫。
    # 2026-09-23（运维缺陷 18）4,574 -> 4,740（+166）：理解步 `turn_summary`
    # 那一条 bullet 多了三件 —— 他以为我们讲过不等于我们讲过、摘要用他写的
    # 那种语言、以及**把那条规则自己的示例换掉**（示例原来逐字写着
    # `we quoted it`，正是同一条 bullet 明令禁止的那句）。
    # 2026-09-24（运维缺陷 22）4,740 -> 4,462（-278）：删掉 `evidence_span_hash`
    # 的示例两行与那句 64 位 hex 规则 —— LLM 结构上算不了哈希，值恒为编造。
    "UNDERSTAND_VALUE": 4462,
    # 2026-09-19（批 G）+202：一轮可以带一条以上 `send_message`、按列出的顺序发；
    # 回复要说「我先把 X 讲清楚」时，**再列一条把 X 本身发出去**。
    # 投递侧本来就有序（`ORDER BY created_at, ordinal, id`，T109 / 迁移 044），
    # **缺的一直是没有任何一句话告诉模型可以发第二条**。
    # 2026-09-19（批 J / T367）+196：多了一句讲 `unkept_promises` ——
    # 「它非空时这是重跑，把兑现那一条列出来」。
    # 2026-09-21（一条说一件事）+175：「一条讲不完就列第二个 send_message 候选」。
    # **候选在计划步产生**，而生成步多提一个提案是整份拒 ——
    # 规则必须写在产它的那一层，不能只写在写字的那一层。
    # 2026-09-21（ADR-0146）+854：计划步多了「`frames_entered` 是什么、
    # 进过的帧别再 push、他再问起就在当前帧用自己的话简答」那一段。
    # **在此之前它只看得见 `stack_depth` 一个数**，而一个 `brief_answer`
    # 的帧再 push 会被拒、落到可能静默的兜底上 —— 客户就那样被无视了。
    # 2026-09-23（ADR-0151）+28：计划步那段「一轮可以带一条以上 send_message、
    # 装不下就列第二条」换成了「**至多列一条** —— 几个气泡由文本决定，
    # 而计划的那一刻文本还不存在」。**净 +28，是三个数里唯一动的那一个**
    # （生成步去掉 60 字上限、换成分段那段，字节上正好抵掉）。
    # 2026-09-23（运维缺陷 19）计划步 4,570 -> 4,834（+264）、生成步
    # 8,305 -> 8,642（+337）：两步各多一条讲 `pending_question` 的规则。
    # **两步都要教是量出来的**：缺陷现场是生成步在同一条消息的第 ③ 段
    # 把第 ① 段刚用过答案的问题又问了一遍 —— 只教计划步拦不住写字的手；
    # 而在此之前 `pending_question` 进了 plan 的 user_prompt，
    # **两个 system prompt 里 `pending` 零命中** —— 模型收着一个没人教过的键
    # （T200 那一族：契约给了键，提示词没提）。
    "PLAN_CAPABILITY": 4834,
    # 2026-09-18（批 E）+626：多了两句 —— 跟进要从 `materials_already_sent`
    # 里那几件事接起来（**用户当场更正：上一句不一定是关键，之前的内容才是**），
    # 而 `last_outbound` 只管语气与别复读。
    # 2026-09-19（批 G）+318：许了就要在这一轮兑现 —— 把 X 放进计划列的**下一条**
    # `send_message`，**是另一条消息，不是塞进同一条**（用户当天原话：
    # 「许了 紧跟下一条来讲完 不用在一条里面完成显得很奇怪」）。
    # 2026-09-19（T363）+230：生成步系统提示多了三行讲 `remembered_facts`
    # ——「与 knowledge_chunks 同一条规矩：用自己的话写，引它的 ref」。
    # 2026-09-21（一条说一件事）+351：每条 60 字**上限**（不是目标），
    # 讲不完续到计划列的下一条，外加「不许超过计划列的条数」那半句。
    # 那半句不是客套：超了就是 `GENERATION_DUPLICATED_INTENT`，整份拒。
    # 2026-09-21（教模型怎么记，rebase 到 `5fe784a5` 之后）6,341 -> 6,900：
    # `remember` 的载荷形状、键的来源与两个上界（+559）。
    # **在此之前 `field_key` 在整个文件里 0 命中**，而那道闸一直在按它拒。
    # 2026-09-23（ADR-0151）+345：生成步那条「每条至多 60 字」换成了
    # 「**没有字数限制** —— 长答案自己分段，每段各成一个气泡，
    # 平台按序发、每段等送到再停一会儿」，外加素材引用段自成一个气泡那一句。
    # ⚠️ **作者当时口头说「净零」，是错的** —— 这条守卫量出来是 +345。
    # 它逮的就是「常量没跟着改」，而这一次连带逮住了一句没量就说的话。
    # 2026-09-24（缺陷 21）+1：商家词长参照 17-86/41 -> 17-115/45（bridge 素材补实后重量）。
    "GENERATE_VALUE": 8643,
}

#: 每个角色的**响应 schema** 在请求里占的线路字节，与 `model_output_schema` 的
#: 产出写两遍、用例钉死相等。
#:
#: **它是固定底的一部分，先前只隐含在「不带素材的 generate 请求实测 7,983」
#: 那个数里**（用户 2026-09-11 指定并进本批）。三个角色的 binding 都是
#: `openai-responses-v1`，那一支把 schema 放进 payload——**所以它真的在请求里**，
#: 这一条读 `api_compatibility` 核过。不纳入的话，schema 改大会悄悄吃掉余量，
#: 下次又是撞了才知道。
#:
#: **plan-result 是三者里最大的一个（4,740），比它的 system prompt 还大近一倍。**
#:
#: **6a（2026-09-12）把 plan 这一对都抬了**：`knowledge_query` 让 system prompt
#: **+370**（说明段 340 + 示例行 30）、响应 schema **+456**，
#: plan 的固定底 6,617 → **7,443 / 16,384（45.43%）**。
#: **2026-09-13 再抬一次：7,443 → 7,855 / 16,384（47.94%）** ——
#: 裁定一 ② 给 plan 的 system prompt 加了那句必填槽位指令，+412。
#: 这个数与 `PLATFORM_PROMPT_BYTES` 一样是**现量现比**的
#: （`test_plan_knowledge_query.py` 里那条
#: `test_what_the_field_costs_per_plan_turn_is_measured_not_estimated`），
#: 所以改了提示词而它没跟着改，门禁当场红 —— 实测：本批就是这么被抓到的。
#: **这四个数是处置独立审查 P1-1 之后重测的**：那次处置去掉了字段的两个长度界
#: （长度界在契约里意味着模型把查询写长一点就让整份计划作废、客户这一轮没有回复）、
#: 把 description 收短、并重写了提示词那一段。上一版的 +364 / +430 / 7,411 作废。
#: 两个数由 `test_profile_materials_are_trusted` 里那条「两边写两遍、钉死相等」的
#: 用例**现量现比**，所以这里写的是测出来的结果，不是估的。
#: **要是哪天 plan 的固定底变成挡路的那一项**，先砍的是 schema 里
#: `knowledge_query` 的 description（那 430 里的大头是给人读的两句话），
#: 不是砍字段。
#: **2026-09-15：`UNDERSTAND_VALUE` 2,592 -> 2,754（+162）。**
#: 槽位值那条 `maxLength: 400` 连同一句 description 进了 `understanding-result`，
#: 而**响应 schema 每次调用都发** —— 所以一句写在响应 schema 里的 description
#: 是**按次付费的**。第一版那句写得详细（含四组实测数），实测 **+912 字节/次**；
#: 砍到只留「临时值 + 到期条件 + 依据在哪」之后是 +162。
#: **依据与那四组数搬去了 `docs/reviews/stage-bucket-capacity-2026-09-15.md`** ——
#: 文档不进请求，schema 进。
#: **这一笔是那条守卫逼出来的**：改了响应 schema 而两个常量没跟着改，
#: `test_the_overhead_constants_are_measured_from_the_real_payload` 当场红。
#:
#: ## 判据：**进 payload 的文字，每一个字都是每次调用都付一遍的**
#:
#: 响应 schema 与 system prompt 都随每一次调用发出去，**写在它们里面的注释是按次付费的**。
#: 给人读的话应当落在**不进请求的地方**（本注释就是其中之一：`registry.py` 不进 payload）。
#:
#: **这条判据自己给出过最好的一次示范**（2026-09-15，同一天连撞两次）：
#:   1. 第一版把四组实测数写进那条 `description`，实测 **+912 字节/次**；
#:   2. 砍到「临时值 + 到期条件 + 依据在哪」是 **+162**；
#:   3. **然后我把「文字是按次付费的」这句判据本身也写进了那条 description** ——
#:      **再 +206**。那句话在按次付费地解释「文字在按次付费」，**它违反了它自己**。
#:
#: **所以它落在这里，而 schema 里只留那一句最短的。**
#: 三次都是同一条守卫逮的 —— 它逮的是「常量没跟着改」，
#: **而顺带逼出来的是「这段字该写在哪」。**
#:
#: **这条判据自己也适用于它自己** —— 所以它写在这里（注释，不进 payload），
#: 不写在 `description` 里。**照办的形状是两句**：
#: **解释性的文字放注释；`description` 只留最短那一句 + 一个指向注释的坐标。**
#: （第 3 次那 206 字节就是没照办的代价：一句解释「文字在按次付费」的话，
#: 自己在按次付费。）
PLATFORM_RESPONSE_SCHEMA_BYTES = {
    # 2026-09-20（T200）3,002 -> 3,300（+298）：多了必填的 `turn_summary`。
    # 与下面 `REQUEST_OVERHEAD_BYTES` 那一笔是同一个 +298 —— 响应 schema
    # 整份进请求，所以它涨多少，那条请求就涨多少。
    # 2026-09-23（运维缺陷 16）3,300 -> 3,354（+54）：`turn_summary` 的
    # description 改成「只写他那一半，回复那一刻还不存在」。
    # ⚠️ **这 54 字节是必付的**（schema 整份进请求），而它换掉的是一句
    # **结构上不可能答对的要求** —— 原文让理解步写「what we did」。
    # ⚠️ **成因的留痕写在 `inference_model.py` 的 v15 那段，不写在这里** ——
    # 第一版把它写进 description，实测让这个数涨到 **3,786（+486）**，
    # 而那是每回合都付的。`registry.py` 为同一件事记过三次同样的账。
    # 2026-09-24（运维缺陷 22）3,354 -> 3,267（-87）：facts 少了一个必填键。
    "UNDERSTAND_VALUE": 3267,
    "PLAN_CAPABILITY": 4740,
    "GENERATE_VALUE": 3104,
}

#: 一条请求里**与 `user_prompt` 无关**的那些字节：system prompt、响应 schema、
#: 以及供应商 payload 自己的骨架（`model` / `temperature` / `stream` …）。
#:
#: **定义精确到可验**：它就是 `providers.request_payload(binding, system_prompt=<该角色的
#: system prompt>, user_prompt="")` 序列化之后的字节数。于是
#:
#: ```
#: 真实请求 = REQUEST_OVERHEAD_BYTES[role] + document_cost(那份 user_prompt 文档)
#: ```
#:
#: **是恒等式而不是近似**：payload 把 `user_prompt` 放进去时是一个**字符串值**，
#: 而 `document_cost` 量的正是「一份文档作为那样一个字符串值要占多少」。
#: 实测五种含引号 / 反斜杠 / 中文 / 控制字符的文档 × 三个角色，**残差恒为 0**。
#:
#: **它替掉的是「平台提示词 + 响应 schema」这个两项之和**（那两张表留着，
#: 因为它们说清了钱花在哪，而且各自有用例钉着）—— 两项之和漏掉了**供应商骨架**，
#: 而 `_check_request_floors` 又漏掉了 `user_prompt` 自己那一圈键名。
#: **两笔都在这一个数里，而这个数是从生产那个构造点量出来的。**
#:
#: **跨包写两遍的理由与 `MATERIAL_BYTES_BUDGET` 同源**：`services/api` 不从
#: `services/worker` 导入，所以这里写值、那边现量、由
#: `test_request_floor_equals_the_real_request` 钉死相等。
#: **改了任何一个 system prompt 或响应 schema，这三个数就要跟着改，否则那条用例当场红。**
REQUEST_OVERHEAD_BYTES = {
    # 2026-09-20（T200 / E4 第 4 批）6,133 -> 6,431（+298）：理解步的响应
    # schema 多了必填的 `turn_summary`（滚动摘要那一句）。**+298 是实测的**
    # —— `test_the_overhead_constants_are_measured_from_the_real_payload`
    # 当场把它报了出来，而第一版那段 description 写得长，实测是 **+1,504**；
    # 理由挪进 `runtime/summary_prompt.py` 与迁移 071 之后才降到 +298。
    # **合同里的 description 在线路上要花钱**，这是本仓第一次量到它。
    #
    # 2026-09-20（T200 的后半）6,431 -> 6,688（+257）：**理解步的 system
    # prompt 终于把 `turn_summary` 告诉模型了**。上面那 +298 只动了响应
    # schema —— 契约要它，而提示词从没提过它，同一段还写着
    # `Do not add properties.`，于是模型被明确告知不要产它，校验器再因为
    # 它缺席把整份判死。**stage 上部署后第一条真实客户消息就死在这里。**
    # +257 里：`Required keys` 那一行 +14、一条规则 +170、示例一行 +73。
    # ⚠️ **第一版写得长，实测 +477** —— 与上面那笔 +1,504 同一个教训，
    # 同一条规矩：**解释性的文字放注释，提示词里只留照做所必需的那几句。**
    # 2026-09-21（ADR-0147）6,688 -> 7,946（+1,258），与上面那张表同一笔。
    # 2026-09-23（运维缺陷 18）8,149 -> 8,315（+166），与上面那张表同一笔。
    # 2026-09-24（运维缺陷 22）8,315 -> 7,950（-365）：提示词 -278 + 响应 schema -87。
    "UNDERSTAND_VALUE": 7950,
    # 2026-09-19（批 G）8,073 -> 8,275（+202），与上面那张表同一笔：
    # 计划步系统提示多了「一轮可以带一条以上 send_message」那三行。
    # 2026-09-19（批 J / T367）8,275 -> 8,471（+196）：同上那一句，
    # 外加 `user_prompt` 里一个空的 `unkept_promises` 键（线路上已含在 196 里）。
    # 2026-09-21（一条说一件事）8,471 -> 8,646（+175），与上面那张表同一笔。
    # 2026-09-21（ADR-0146）8,646 -> 9,500（+854），与上面那张表同一笔：
    # 计划步多了「`frames_entered` 是什么、进过的帧别再 push、
    # 他再问起就在当前帧用自己的话简答」那一段。
    # 2026-09-23（ADR-0151）+28：计划步那段「一轮可以带一条以上 send_message、
    # 装不下就列第二条」换成了「**至多列一条** —— 几个气泡由文本决定，
    # 而计划的那一刻文本还不存在」。**净 +28，是三个数里唯一动的那一个**
    # （生成步去掉 60 字上限、换成分段那段，字节上正好抵掉）。
    # 2026-09-23（运维缺陷 19）9,528 -> 9,792（+264），与上面那张表同一笔。
    "PLAN_CAPABILITY": 9792,
    # 2026-09-18（批 E）8,139 -> 8,765（+626），与上面那张表同一笔：
    # 生成步系统提示多了两句。
    # ⚠️ **这个数是「空 `user_prompt` 的整条请求」，不是某一帧的真实请求** ——
    # 我第一版填的是后者（9,090），于是 admission 把帧那部分算了两遍、
    # 报出 9,415。`test_the_floor_equals_the_request_it_claims_to_measure`
    # 当场红并报出「差 -325」。**定义精确到可验，就是为了这种时候**。
    # ⚠️ 这个数是 **admission 固定底的加数** —— 少算的方向是 fail-open，
    # 刚好过闸的包真实请求就已经超了。
    # 2026-09-19（批 G）8,765 -> 9,083（+318），同上：生成步系统提示多了
    # 「许了就把 X 放进下一条 send_message」那四行。
    # 2026-09-19（T363）9,083 -> 9,313（+230）：同上那三行。
    # ⚠️ **与空请求基线那 +283 不是同一个增量**：这一项量的是
    # 「system prompt + 响应 schema」，`user_prompt` 里那两个空键不进它
    # ——`request_shape` 那一侧由各自的 payload 自己带。
    # 2026-09-21（一条说一件事）9,313 -> 9,664（+351），与上面那张表同一笔。
    # 2026-09-21（教模型怎么记）9,664 -> 10,223（+559 提示词；那个空键
    # 不进这张表，它进的是空请求基线）。
    # 2026-09-23（ADR-0151）+345：生成步那条「每条至多 60 字」换成了
    # 「**没有字数限制** —— 长答案自己分段，每段各成一个气泡，
    # 平台按序发、每段等送到再停一会儿」，外加素材引用段自成一个气泡那一句。
    # ⚠️ **作者当时口头说「净零」，是错的** —— 这条守卫量出来是 +345。
    # 它逮的就是「常量没跟着改」，而这一次连带逮住了一句没量就说的话。
    # 2026-09-23（运维缺陷 19）11,628 -> 11,965（+337），与上面那张表同一笔。
    "GENERATE_VALUE": 11966,
    # 2026-09-22（ADR-0149）**新增第四个角色**，实测 **7,959**。
    #
    # **比理解步只多 13 字节**（7,946）—— 系统提示词更短，而响应 schema 更简单
    # （`reading` + `action` 两个键，对上理解步那九个）。两笔一增一减，
    # 净差 13。**这个数是量出来的**：
    # `request_payload(active_release().role("SILENCE_CAPABILITY"), …)` 现造现量，
    # 与 `test_the_overhead_constants_are_measured_from_the_real_payload` 同一条路。
    #
    # ⚠️ **它只在主动轮次付**，所以它不落在每一个回合上 —— 与上面三个不同。
    "SILENCE_CAPABILITY": 7959,
}

#: What a trusted profile package must live inside.
#:
#: **Not** `Path(__file__).resolve().parents[5]`, which is what this was until
#: 2026-09-04. That counted five directories up from this module, which is the
#: repository root when the code is read out of the source tree and `/usr/local`
#: when it has been `pip install`ed into `site-packages` -- so in the image the
#: trust root pointed at a directory holding no profiles, `_require_inside_repository`
#: refused every package, and the two routes built on it answered 500 and
#: "your package is not trusted" respectively. The tests could not see it: the
#: gate imports from the source tree, where the wrong derivation gives the right
#: answer (independent review 2026-09-04, P1-1).
#:
#: `DEPLOYMENT_ROOT` is where `CONTRACT_ROOT` already comes from, so the schemas
#: and the packages now move together or not at all. The original comment
#: worried that a working directory is operator-supplied and therefore untrusted
#: input -- but the operator supplying it is the one starting the process, who
#: can equally choose the image, the mount and the arguments. What this bound is
#: really for is a *path argument* pointing outside the deployment, and that it
#: still does.
TRUSTED_ROOT = DEPLOYMENT_ROOT


def _require_inside_repository(candidate: Path, *, what: str) -> Path:
    """Resolve `candidate` and refuse it if it escapes the deployment."""

    resolved = candidate.resolve()
    if resolved != TRUSTED_ROOT and TRUSTED_ROOT not in resolved.parents:
        raise ProfilePackageError(
            f"{what} is outside the deployment and is therefore not trusted: "
            f"{resolved} (deployment root: {TRUSTED_ROOT})"
        )
    return resolved


def canonical_hash(document: dict[str, Any]) -> str:
    """Content hash of a profile: sha256 over the document minus its own hash.

    `identity.content_hash` is removed before hashing -- a document cannot
    contain a hash of itself. Everything else is included, `identity.release_id`
    among it: two releases that differ only in release id are different
    releases.

    The serialisation is pinned here and nowhere else: sorted keys, no
    whitespace, `ensure_ascii=False` over UTF-8. The non-ASCII choice matters
    even though today's profiles are ASCII -- profiles carry customer-facing
    Chinese material, and backslash-u escaping is a second canonicalisation
    waiting to disagree with this one.
    """

    body = deepcopy(document)
    identity = body.get("identity")
    if isinstance(identity, dict):
        identity.pop("content_hash", None)
    serialised = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


def _merge_material_texts(document: dict[str, Any], path: Path) -> None:
    """Fold `materials.json` into the document, or leave it alone if there is none.

    Mutates `document` in place, and does so **before** the contract check and
    the hash check so that both cover the words as well as the structure.
    """

    if MATERIAL_TEXTS_KEY in document:
        # The texts come from the sibling file or not at all. Allowing both
        # sources would create a document whose words depend on which one the
        # loader happened to apply last, and the schema cannot tell them apart:
        # by the time it runs, a merged key and a hand-written one look the same.
        raise ProfilePackageError(
            f"profile package {path} declares {MATERIAL_TEXTS_KEY!r} itself. "
            f"Material text belongs in the sibling {MATERIALS_FILENAME!r}, which is "
            "merged here and covered by the same content hash."
        )

    materials_path = path.parent / MATERIALS_FILENAME
    if not materials_path.is_file():
        return

    try:
        raw = materials_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ProfilePackageError(
            f"profile materials file is unreadable: {materials_path}"
        ) from error
    try:
        sidecar = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ProfilePackageError(
            f"profile materials file is not valid JSON: {materials_path}"
        ) from error
    if not isinstance(sidecar, dict):
        raise ProfilePackageError(
            f"profile materials file is not a JSON object: {materials_path}"
        )

    materials = sidecar.get("materials")
    if not isinstance(materials, dict):
        raise ProfilePackageError(
            f"profile materials file {materials_path} has no `materials` object; "
            f"it holds {sorted(sidecar)[:20]}"
        )
    for ref, text in materials.items():
        # Checked here rather than left to the schema because the message can
        # name the offending ref, and a materials file is edited by whoever
        # writes the words -- not necessarily by someone who reads JSON Schema
        # errors.
        if not isinstance(text, str) or not text.strip():
            raise ProfilePackageError(
                f"profile materials file {materials_path}: material {ref!r} has no text. "
                "A declared material with nothing in it is a material the model would "
                "have to invent."
            )
    document[MATERIAL_TEXTS_KEY] = dict(materials)


def _check_memory_fields(document: dict[str, Any], path: Path) -> None:
    """`memory_fields` 的三件，封版时检（E4-1b，ADR-0131 M2）。

    **平台在记忆这件事上只拥有三样**——禁止项、上限、生命周期。
    字段清单本身是商家的——**不同行业要记的事没有交集，平台永远猜不全下一个**——
    所以这里**不检查清单的内容是否"合理"**，只检查那三样。

    **本段原本举了三个行业当例子，被 `check_core_is_merchant_neutral` 拒了**
    （门禁点名 `registry.py` 里出现商家特定词）。**那次拒绝是对的，留痕在此**：
    这道守卫与本函数讲的是同一条原则的两面——
    **平台不拥有商家的词表**，而「举例说明平台不该有词表」本身也会把词表写进来。
    具体的行业例子在 `ADR-0131 M2`，那是图纸不是核心代码。

    ## 一、禁止项是一条**绊线**，不是一份合规保证

    **这一段必须被读到，否则这道闸会被当成它不是的东西。**

    匹配的是**商家自己取的 key 名字**。一个写 `id_card_no` 的商家会被挡住；
    一个写 `zjhm`、`ref_b`、`customer_extra_2` 的**不会**。
    **所以它挡得住"没多想"，挡不住"换个名字"。**

    **这不是缺陷，是这道闸能力的边界，而边界要写在它旁边**：
    真正按内容的拒绝只能发生在**写入路径**上（值长什么样，而不是列叫什么），
    那是 E4-2 / E4-3 的事，登记为到期项。
    **在那之前，任何人不得把这道闸读成"平台保证不会记敏感信息"。**

    ## 二、**这里没有条数上限**，而那是一次裁定不是遗漏

    本批原本有一道「可证装不下即拒」的闸，**单位是错的**（把字符当字节），
    详见本模块顶部 `MEMORY_VALUE_MAX_BYTES` 原址的那段留痕。

    **用户 2026-09-12 裁定：撤掉它，条数上限连同「记忆在提示词里占多少份额」
    一起推给 E4-5**——因为按真实线路口径，**一份三条中文声明就已经超过整个请求
    预算**，而那说明真正缺的是份额，不是一个条数上限。**登记 T300。**

    **schema 的 `maxItems: 32` 今天仍然允许一个远超预算的声明**，这一点一并登记，
    不在本批降——降到几要有判据，而那个判据是 E4-5 的实测。

    ## 三、重复 key

    JSON Schema 表达不了"数组里对象的某个字段互不相同"。两条同名声明
    会让"这个商家要记什么"有两个答案，而写入路径会按其中一个办。
    """

    fields = document.get(MEMORY_FIELDS_KEY) or []
    if not fields:
        return

    seen: set[str] = set()
    for entry in fields:
        # schema 已要求 `key` 存在且限定了形状，而 `validate_contract` 在本函数
        # 之前跑（`load_package`），**所以经由唯一调用方走不到 `None`**。
        # 留着是因为**下一个直接调本函数的人不一定先 validate**——
        # 同 `_check_reference_grammar` 对缺键那一条的处置，理由逐字相同。
        key = str((entry or {}).get("key"))

        if key in seen:
            raise ProfilePackageError(
                f"{path}: {MEMORY_FIELDS_KEY} declares {key!r} twice. Two declarations "
                "make 'what does this merchant remember' a question with two answers, "
                "and the write path would follow whichever it read first."
            )
        seen.add(key)

        hit = next((w for w in MEMORY_FORBIDDEN_SUBSTRINGS if w in key), None)
        if hit is not None:
            raise ProfilePackageError(
                f"{path}: {MEMORY_FIELDS_KEY} declares {key!r}, whose name contains "
                f"{hit!r}. ADR-0131 M2 keeps identity numbers, payment details, health "
                "and religion out of what the platform remembers. **This check reads the "
                "name the merchant chose, so it stops a careless one and not a renamed "
                "one** — it is a tripwire, not a compliance guarantee; content-level "
                "refusal belongs to the write path (E4-2 / E4-3)."
            )

    # **四、声明了要记，就得有哪个打法真的能记**（2026-09-18）。
    #
    # 一份声明了 `memory_fields` 而没有任何打法允许 `remember` 的配包，
    # **封得了版、跑得了对话，而那份声明一条都不会被写进去** ——
    # 商家以为「我要记这六件事」已经生效了，而写入路径永远不会被授权。
    #
    # **两头都在、中间没接**，而在这一行之前没有任何东西会红。
    # 它与上面三条同族：平台不拥有商家的词表，**而平台守「这份声明说得通」**。
    if not any(
        "remember" in ((body or {}).get("allowed_intent_kinds") or [])
        for body in (document.get("strategies") or {}).values()
    ):
        raise ProfilePackageError(
            f"{path}: {MEMORY_FIELDS_KEY} declares {len(fields)} field(s) to remember, "
            "but no strategy lists 'remember' in its allowed_intent_kinds. The "
            "declaration would never be written to: the merchant believes these facts "
            "are being kept while the write path is never authorised to keep one. "
            "Either allow 'remember' on the strategies that carry the conversation "
            f"forward, or drop {MEMORY_FIELDS_KEY}."
        )



def _check_reference_grammar(document: dict[str, Any], path: Path) -> None:
    """三段引用语法，封版时检。

    **规则只有一个定义点**：`runtime.references.reference_refusal`。
    这里不重新判一遍，只负责把包里五个面上的每一个引用送过去，
    并在拒绝时说清楚是哪个面的哪一条。

    **为什么在封版时检，而不是在发送路径上**：与本文件其余几道闸同一条理由——
    一个引用写错了，发送路径上的表现是 `GENERATION_MATERIAL_REF_UNKNOWN`
    （素材那一侧）或者**什么都不发生**（`knowledge_bindings` 今天零读者），
    而那时消息已经在路上、或者已经安静地少了一半内容。
    **一个不可能工作的包不该发得出去。**

    **2026-09-12 起是五个面**：E3 的 `content_assets` 加进来，
    `expected_kind="asset"`。**这一面与其余四个的不同之处要说破**：
    它**只有对象形态、没有裸串形态**（schema 不接受裸串），
    所以这里直接读 `item["ref"]` 而不做 `isinstance(item, str)` 分支——
    **那个分支在这一面上不可达，写出来会让下一个人以为裸串是合法的**。
    理由在 ADR-0130 §二.1(b)：裸串对素材读作「不设闸、每回合都给」，
    而对文件那就是一次不设闸的投送。

    **P1-1 那条教训在本面上同样适用，而且方向相反**：那次是
    **证据覆盖了 4 个面而守卫只访问 3 个**；本批新增的这一面**今天在架包里一个值都没有**，
    所以「在架包全过」这句话对它**什么也没证明**。本面的证据只能来自新写的用例，
    见 `tests/contracts/test_reference_grammar.py` 里点名 `content_assets` 的那两条。

    **五个面都检，因为它们是五条独立的入口**：
    帧声明的（`content_materials`）、**帧的回归确认语气**
    （`return_confirmation.tone_material_ref`）、**帧声明的文件资料**
    （`content_assets`，2026-09-12 加）、正文文件的键（`materials.json`）、
    以及知识绑定（`knowledge_bindings`）。先前任何一个面写错都没有东西看得见。

    **第四个面是独立审查（2026-09-11，P1-1）量出来的，本函数第一版漏了它**，
    而漏的方式值得写下来：自审拿「164 个在架引用一个都不被拒」当证据，
    **那个 164 是四个面的并集**——`content_materials` 160 + `tone_material_ref` 8
    + `knowledge_bindings` 1 = 169 次出现、去重 164——**而本函数只访问其中三个面，
    那三个面的并集只有 161**。证据覆盖的范围比守卫实际访问的范围大一个面，
    于是「全部通过」这句话是真的，却不是为它声称的理由真的。
    审查方的反向探针证实：`tone_material_ref` 放进
    `'MATERIAL::Bad::vONE::extra'` 照收，同一个值放进 `content_materials` 被拒。
    **这就是 `AGENTS.md` 那条「锚点没命中不是通过」在证据一侧的同型。**

    **空串跳过，不拒。** schema 把 `tone_material_ref` 列为 `required`，
    所以一个「没有语气素材」的帧仍然必须带这个键，而仓里已经在用 `""` 表示这件事
    （`worker/platform_frames.py:102`，平台自己的 `NO_GOAL_FRAME_DEFINITION`）。
    `reference_refusal("")` 会拒（「引用是空字符串」），**所以这一面必须在送进去
    之前就跳过**，这条不对称是有意的：拒掉 `""` 等于逼每个 `policy: "forbidden"`
    的帧去编一个不存在的引用，那是一条**会拒掉在架包**的 admission 变更，
    而本批的全部前提是不拒掉任何在架包。在架的 8 个 tone 值零空串、语法全合法，
    作者与审查方各量过一遍。

    **而缺键仍然拒**（`tone_material_ref` 不在、或不是字符串）。schema 把
    `return_confirmation` 与其中的 `tone_material_ref` 都列进了 `required`，
    且 `validate_contract` 在本函数之前跑（`load_package` 第 732 行对第 835 行），
    所以这条路经由 `load_package` 走不到——**和 `039_contacts.sql` 那条长度约束
    同一个位置**：来自唯一调用方时不可达，仍然值得留着，因为下一个直接调本函数的人
    不一定先 validate。
    """

    for frame in document.get("target_frames", []) or []:
        frame_id = str(frame.get("id"))
        for item in frame.get("content_materials", []) or []:
            reference = item if isinstance(item, str) else (item or {}).get("ref")
            refusal = reference_refusal(reference, expected_kind="material")
            if refusal is not None:
                raise ProfilePackageError(
                    f"{path}: 帧 {frame_id!r} 的 content_materials 里 {reference!r} "
                    f"不是合法引用——{refusal}。"
                    "三段的含义见 `runtime/references.py`。"
                )

        # `tone_material_ref` 2026-09-24 整键收掉（用户裁定）：回归确认那句话
        # 由模型按上下文自己写，与静默唤醒同一条哲学（「情况穷尽不完，让模型
        # 发挥」）。此前它三包悬空（指向不存在的素材）且运行期零消费 ——
        # 这里只检语法不检存在，双重空活到了今天。`policy` 那半保留且活着。

        for item in frame.get(CONTENT_ASSETS_KEY, []) or []:
            reference = (item or {}).get("ref")
            refusal = reference_refusal(reference, expected_kind="asset")
            if refusal is not None:
                raise ProfilePackageError(
                    f"{path}: 帧 {frame_id!r} 的 {CONTENT_ASSETS_KEY} 里 {reference!r} "
                    f"不是合法引用——{refusal}。"
                    "三段的含义见 `runtime/references.py`。"
                )

    for reference in document.get(MATERIAL_TEXTS_KEY, {}) or {}:
        refusal = reference_refusal(reference, expected_kind="material")
        if refusal is not None:
            raise ProfilePackageError(
                f"{path}: {MATERIALS_FILENAME} 里的键 {reference!r} 不是合法引用"
                f"——{refusal}。"
            )

    for reference in document.get("knowledge_bindings", []) or []:
        refusal = reference_refusal(reference, expected_kind="knowledge")
        if refusal is not None:
            raise ProfilePackageError(
                f"{path}: knowledge_bindings 里 {reference!r} 不是合法引用——{refusal}。"
            )


def _check_every_allowed_strategy_is_declared(document: dict[str, Any], path: Path) -> None:
    """帧允许的每一个策略，`strategies` 里都得真的有它（运维缺陷 13，2026-09-23）。

    ## 它补的是 `_check_strategy_required_slots` 自己点名却说「不是本条」的那一条

    那条检查的 docstring 逐字：

    > 一个策略可以被多个帧允许，而槽位是**帧**的属性 …… 那个帧本来就不该允许它，
    > **而那是另一条检查（`allowed_strategy_set` 的语义），不是本条。**

    **那条「另一条检查」在 2026-09-23 之前不存在。**

    ## 而它漏掉的东西，与它自己描述的症状逐字相同

    同一段 docstring 写着：

    > 在帧里永远不存在，于是永远为空，于是**那条策略从此每一轮都被拒** ——
    > 而拒绝的理由码……**指向模型，不指向那个拼错的字**。
    > 「永久被拒 + 错误信息指向错误的原因」是最坏的组合。

    stage 实测（2026-09-23）：演示包三个帧的 `allowed_strategy_set` 里都有
    `offer_material`，而 `strategies` 里**根本没有它**。计划步要发资料时只能选它
    → `STRATEGY_NOT_IN_PROFILE_REGISTRY` → 回退到 `ask_clarification`
    → 客户收到的是一句多余的确认，**而文件一份都没发**。

    整通对话 `send_material` 意图 **0 个**、非文本出站 **0 行** ——
    **「发资料」这条功能从上线起一次都没生效过。**

    ## ⚠️ 为什么那条检查自己抓不到

    它的循环是 `for name, body in strategies.items()` ——
    **一个只出现在 `allowed_strategy_set`、从未出现在 `strategies` 的名字，
    它一次都不会访问到。** 遍历方向决定了它看不见这一类。

    ## 契约对它没有任何约束

    `allowed_strategy_set` 是 `{"type": "array", "items": {"type": "string"}}` ——
    **任何字符串都过**。封版期不判，就没有任何地方会判。

    ## 判据同时覆盖 `time_rule_strategy_set`

    两张表都点策略名，而时间规则那张漏一个的后果一样：
    那条定时规则到点之后选不中任何打法。
    """

    strategies = {str(name) for name in (document.get("strategies") or {})}
    offences: list[str] = []
    for frame in document.get("target_frames") or []:
        frame_id = str(frame.get("id"))
        for field in ("allowed_strategy_set", "time_rule_strategy_set"):
            unknown = sorted(
                {str(name) for name in frame.get(field) or []} - strategies
            )
            if unknown:
                offences.append(f"{frame_id}.{field}: {unknown}")

    if offences:
        raise ProfilePackageError(
            f"profile package {path} allows strategies that the package never "
            f"declares: {offences}. A strategy the frame allows but `strategies` "
            "does not define cannot be selected -- the plan step is refused with "
            "STRATEGY_NOT_IN_PROFILE_REGISTRY and falls back, every single turn. "
            "Measured on stage 2026-09-23: `offer_material` was allowed by three "
            "frames and declared by none, so sending a material never worked once "
            "since the package went live, and the reason code pointed at the model. "
            "Declare it in `strategies`, or take it out of the frame."
        )


def _check_every_delivery_filled_slot_is_declared(
    document: dict[str, Any], path: Path
) -> None:
    """`filled_by_delivery` 点的那条素材，必须在**同一个帧**里真的声明过（运维缺陷 18）。

    ## 这一格是什么

    一个槽可以声明「我由平台的投递记录填」（`merchant-profile.schema.json` 的
    `information_slots[].filled_by_delivery`）。值是一条素材 ref；那条素材真的
    发出去之后，`target_stack.record_delivered_slots` 把槽写成那条素材的原文。

    ## 不检会怎样

    ref 写错一个字，或者写了一条**本帧发不出去**的素材（它只在别的帧的
    `content_materials` 里），结果是：

    * 那条素材永远不会在这一帧被选中，于是 `delivered` 里永远没有它；
    * 于是这个槽永远填不上；
    * 而 `price_quoted` 这类槽正是 `exit_conditions` 的判据 ——
      **这一帧从此出不去**。

    **包封得出、对话跑得动、没有任何东西变红**，只是那个目标永远完不成。
    `CLAUDE.md` 第三节那一类：传了个合法值，安静地给出一个看起来正常的结果。

    ## 判据是「本帧声明了它」，不是「包里某处有它」

    `materials_for_frame` 要求帧与包**两边都声明**才把词交给模型，而
    `record_delivered_slots` 读的就是它。所以「包里别处有这条素材」不够 ——
    那个 ref 在这一帧仍然解析不出原文，槽仍然填不上。

    ## 只对 `type: "string"` 有意义

    写进槽的是素材原文。声明在一个 `boolean` / `money` / `date` 槽上，
    写进去的值与它自己声明的类型对不上 —— 而**没有任何一处在运行期校验槽值的类型**
    （`apply_slot_updates` 不校验，契约里 `slots` 是 `additionalProperties: true`）。
    所以这一条只能在封版期判。
    """

    offences: list[str] = []
    for frame in document.get("target_frames") or []:
        frame_id = str(frame.get("id"))
        declared = {
            str(item.get("ref"))
            for item in (frame.get("content_materials") or [])
            if isinstance(item, dict)
        }
        for slot in frame.get("information_slots") or []:
            reference = str(slot.get("filled_by_delivery") or "").strip()
            if not reference:
                continue
            name = str(slot.get("name"))
            if reference not in declared:
                offences.append(
                    f"{frame_id}.{name}: filled_by_delivery={reference!r} "
                    f"not in this frame's content_materials {sorted(declared)}"
                )
            if str(slot.get("type")) != "string":
                offences.append(
                    f"{frame_id}.{name}: filled_by_delivery on a "
                    f"{slot.get('type')!r} slot -- the platform writes the "
                    "material's own text, which is a string"
                )

    if offences:
        raise ProfilePackageError(
            f"profile package {path} has slots filled by a delivery that cannot "
            f"happen in their own frame: {offences}. The platform fills such a "
            "slot only when that material is delivered *from that frame*, so a "
            "ref the frame never declares leaves the slot permanently empty -- "
            "and a required slot that can never be filled is an exit condition "
            "that can never be met. Nothing goes red: the package seals, the "
            "conversation runs, the goal just never completes."
        )


def _check_some_strategy_declares_a_required_slot(
    document: dict[str, Any], path: Path
) -> None:
    """一份包里**至少要有一个策略**声明 `required_slots`（运维缺陷 19，2026-09-23）。

    ## 全空不是「没配置」，是两条机制同时静默失效

    stage 实测：出事的演示包**每一个策略**的 `required_slots` 都是 `[]`
    （synthetic-third 同样全空；carrier 有两条非空，本条对它不触发），于是：

    * `required_slots_by_strategy` 那张表恒空 → 模型的
      `required_slot_requests` 恒 `[]` → 修复前的 `_record_pending_question`
      第一行恒早退 —— **`pending_question` 结构性地永远是 `None`**，
      同一个问题被原样问了两遍而没有任何东西变红；
    * `STRATEGY_SKIPS_REQUIRED_SLOT` 的触发条件永远不成立 ——
      而它是 `LOOP_ESCALATION_REASON_CODES` 的一员，
      **一条永不触发的闸，它的升级路径也是死的**。

    与缺陷 13（`allowed_strategy_set` 引用未声明的策略）同族：
    **配包里一个看起来无害的空值，让一整条机制静默地不存在。**

    ## 判据是「全空」，不是「每个都要非空」

    `wait` / `acknowledge` 这类策略声明必填槽位没有意义 ——
    要求逐个非空会逼配包作者编造。这条只拒**一个都没有**的包：
    那时上面两条机制对这份包整体失效，而没有任何东西会红。

    ## ⚠️ 声明错帧会造成死锁，那由 `_check_required_slots_exist_in_every_allowed_frame` 挡

    两条分开、且它排在 `_check_strategy_required_slots` 之后 ——
    「一个帧都没有」的要由那条老检查拒（报它的错），死锁检查只接
    「某帧有某帧没有」那一种。第一版把两半写在一个函数里、排在老检查之前，
    **当场截胡了老检查的报错**，钉着那条报错的用例红。
    """

    strategies = document.get("strategies") or {}
    declared = {
        name: [str(slot) for slot in (body or {}).get("required_slots") or []]
        for name, body in strategies.items()
    }
    if strategies and not any(declared.values()):
        raise ProfilePackageError(
            f"profile package {path} declares required_slots on no strategy at "
            "all. With every list empty, required_slots_by_strategy is empty "
            "every turn, the model's required_slot_requests is always [], and "
            "both pending_question's slot annotation and the "
            "STRATEGY_SKIPS_REQUIRED_SLOT gate (plus its loop-escalation path) "
            "silently never fire -- measured on stage 2026-09-23, where the "
            "same question was asked twice in one conversation and nothing "
            "went red. Declare required_slots on at least one strategy whose "
            "goal needs information."
        )


def _check_strategy_required_slots(document: dict[str, Any], path: Path) -> None:
    """每个策略声明的 `required_slots`，都必须在**允许它的某个帧**里真的存在。

    **为什么在封版期拒，而不是在运行期报**（ADR-0137 裁定一 §4.1，用户 2026-09-13
    取「紧」）：这个字段是 `STRATEGY_SKIPS_REQUIRED_SLOT` 那道 `post_plan` 闸的数据源，
    而闸的判据是「这些槽位还空着吗」。一个**拼错的槽位名**（`item_categroy`）
    在帧里永远不存在，于是永远为空，于是**那条策略从此每一轮都被拒** ——
    而拒绝的理由码说的是「跳过了必填槽位」，**指向模型，不指向那个拼错的字**。

    「永久被拒 + 错误信息指向错误的原因」是最坏的组合：症状是某条打法从此再也不生效，
    而日志里全是模型的问题。**取紧的代价是商家改一个字重新封版；
    取松的代价是那个配包带着一条永远不生效的策略上线，而没有人会去看警告。**

    **判据是「允许它的某个帧」，不是「所有帧」**：一个策略可以被多个帧允许，
    而槽位是**帧**的属性 —— `propose_quote` 在 `rental` 帧里要 `rental_period`，
    在一个不含该槽位的帧里被允许时，那个帧本来就不该允许它，
    而那是另一条检查（`allowed_strategy_set` 的语义），不是本条。
    所以本条只问：**这个槽位名在任何一个允许该策略的帧里出现过吗。**

    **一个帧都不允许它的策略，本条跳过不判**：那种策略今天选不中
    （`validate_plan` 按帧的 `allowed_strategy_set` 准入），
    它的 `required_slots` 是一份没人会读的声明 —— 报它会把人引向一个
    与本条无关的问题（「这个策略为什么没帧允许」），那是零调用方那两问的事。
    """

    strategies = document.get("strategies") or {}
    frames = document.get("target_frames") or []

    slots_by_strategy: dict[str, set[str]] = {}
    for frame in frames:
        names = {str(slot.get("name")) for slot in frame.get("information_slots", [])}
        for strategy in frame.get("allowed_strategy_set", []):
            slots_by_strategy.setdefault(str(strategy), set()).update(names)

    offences: list[str] = []
    for name, body in strategies.items():
        declared = [str(slot) for slot in (body or {}).get("required_slots", [])]
        if not declared:
            continue
        reachable = slots_by_strategy.get(str(name))
        if reachable is None:
            # 没有任何帧允许它 —— 见 docstring 末段，本条不判。
            continue
        unknown = sorted(set(declared) - reachable)
        if unknown:
            offences.append(f"{name}: {unknown}")

    if offences:
        raise ProfilePackageError(
            f"profile package {path} declares strategy required_slots that no frame "
            f"allowing that strategy defines: {offences}. "
            "A slot name that cannot be filled is never filled, so the post_plan "
            "required-slot gate would refuse that strategy on every single turn -- "
            "and its reason code points at the model, not at the typo. "
            "Fix the name, or add the slot to the frame's information_slots."
        )


def _enforced_codes() -> list[str]:
    """平台今天**会执行**的那些约束码，排序返回。

    **`is_enforced()` 的生产消费方就是这里**（回查 C-1，2026-09-14）。
    它自称「给读代码的人一个比 `in` 更明确的问法」，而封版那条判据要的是
    **集合运算**（`unknown_constraints` 做差集）—— 单点判定接不进去。
    **而报错文案要的恰好是逐码判定**：读到它的人下一步要判的正是
    「我写的那个码会不会被执行」。
    """

    from fastagent_v2.runtime.generation_constraints import (  # noqa: PLC0415
        KNOWN_CONSTRAINTS,
        is_enforced,
    )

    return sorted(code for code in KNOWN_CONSTRAINTS if is_enforced(code))


def _advisory_codes() -> list[str]:
    """只是嘱咐的那些码 —— 平台认识它们，而没有校验器。

    **与上面那个是一对，而它们合起来等于 `KNOWN_CONSTRAINTS`** ——
    那条等式由 `tests/contracts/test_constraint_vocabulary.py` 钉着
    （两张档不重叠、并集等于 `KNOWN_CONSTRAINTS`）。
    """

    from fastagent_v2.runtime.generation_constraints import (  # noqa: PLC0415
        KNOWN_CONSTRAINTS,
        is_enforced,
    )

    return sorted(code for code in KNOWN_CONSTRAINTS if not is_enforced(code))


def _check_required_slots_exist_in_every_allowed_frame(
    document: dict[str, Any], path: Path
) -> None:
    """声明的必填槽要在**允许该策略的每一个帧**里存在，否则那帧里它恒被拒。

    `_check_strategy_required_slots` 只要求「某个帧里存在」——于是
    「A、B 两帧都允许，槽只在 A」能封版，而运行期那道闸读的是**当前帧**的
    槽位表（`inference_validation`）：在 B 帧里该策略每一轮都被拒，
    点名那个槽又会被 `SLOT_NOT_IN_FRAME` 拒 —— 死锁。
    **包封得出、对话跑得动，只是那条策略在那个帧永远选不中**，
    本仓最贵那类错的形状（运维缺陷 19 这一批顺手钉上）。

    ⚠️ **排在 `_check_strategy_required_slots` 之后**：「一个帧都没有」的
    先由它拒（报它的错），本条只接「某帧有某帧没有」那一种。
    """

    declared = {
        name: [str(slot) for slot in (body or {}).get("required_slots") or []]
        for name, body in (document.get("strategies") or {}).items()
    }
    frames = {
        str(frame.get("id")): {
            str(slot.get("name")) for slot in frame.get("information_slots") or []
        }
        for frame in document.get("target_frames") or []
    }
    offences: list[str] = []
    for frame in document.get("target_frames") or []:
        frame_id = str(frame.get("id"))
        for name in frame.get("allowed_strategy_set") or []:
            for slot in declared.get(str(name), []):
                if slot not in frames[frame_id]:
                    offences.append(f"{frame_id} allows {name} but lacks slot {slot!r}")
    if offences:
        raise ProfilePackageError(
            f"profile package {path} declares required_slots a frame cannot "
            f"satisfy: {offences}. The runtime gate reads the *current* frame's "
            "slots, so in that frame the strategy is refused every turn "
            "(naming the slot is refused too, by SLOT_NOT_IN_FRAME) -- the "
            "package seals, the conversation runs, and that strategy is just "
            "never selectable there."
        )


def _check_exit_conditions_point_at_this_frames_slots(
    document: dict[str, Any], path: Path
) -> None:
    """出口谓词点名的槽必须在**本帧**的 `information_slots`（2026-09-24，建执行批）。

    complete 那道闸读的是**当前帧**的 `slots`（`_unmet_exit_conditions`）——
    点一个本帧没有的槽，条件恒不满足，**这一帧永远 complete 不了**：
    包封得出、对话跑得动、没有任何东西变红。`advance_when` /
    `required_slots` 两道死锁检查的同族第三条。
    谓词的**形状**由 schema 钉（闭合两种：present / equals），这里只查跨引用。
    """

    offences: list[str] = []
    for frame in document.get("target_frames") or []:
        names = {str(s.get("name")) for s in frame.get("information_slots") or []}
        for condition in frame.get("exit_conditions") or []:
            slot = str(condition.get("slot"))
            if slot not in names:
                offences.append(f"{frame.get('id')}.{slot}")
    if offences:
        raise ProfilePackageError(
            f"profile package {path} has exit conditions pointing at slots the "
            f"frame does not declare: {offences}. The complete gate reads the "
            "current frame's slots, so such a condition can never be met -- "
            "the frame can never complete, and nothing goes red."
        )


def _check_advance_when_is_wired(document: dict[str, Any], path: Path) -> None:
    """`advance_when` 的每一条都要指得到真东西（批三，2026-09-24 顺手补）。

    ## 在此之前它零封版校验

    `git grep advance_when services/api/` 在本条之前是 **0 命中** ——
    平台推进的规则表（ADR-0145/0146）封版期一个字不查。四种写错法
    都能封出包、跑得动、没有任何东西变红：

    | 写错 | 运行期的样子 |
    |---|---|
    | `push` 指了不存在的帧 | `advance_into_frame` 那一刻 KeyError，**在真实回合里炸** |
    | `push` 指了平台帧 / 自己 | 运行期 `continue` —— **一条永远不触发的死规则** |
    | `delivered` 的 ref 包里没人声明 | 永远不会被投递 → 条件恒假 → 死规则 |
    | `slot` 不在**本帧**的槽位表 | `slots.get()` 恒 `None` → 条件恒假 → 死规则 |

    死规则正是 `entry_conditions` 那族的形状（商家写了话没人听），
    而 `advance_when` 是**顶替了它的机制** —— 顶替者自己不能再长出同一种洞。

    ## 判据与运行期逐字同侧

    * `push` 读 `frame_definitions` 的键集（运行期 `advance_into_frame` 同一张表）；
    * `delivered` 的 ref 要在**包里某个帧**的 `content_materials` 出现 ——
      投递记录（`pipeline._delivered_materials`）是全会话的，不限本帧；
    * `slot` 要在**声明这条规则的那个帧**的 `information_slots` ——
      `frame_to_advance_into` 读的是 `current["slots"]`，当前帧的。
    """

    frames = {str(frame.get("id")) for frame in document.get("target_frames") or []}
    declared_refs = {
        str(item.get("ref"))
        for frame in document.get("target_frames") or []
        for item in (frame.get("content_materials") or [])
        if isinstance(item, dict)
    } | {
        str(item)
        for frame in document.get("target_frames") or []
        for item in (frame.get("content_materials") or [])
        if isinstance(item, str)
    }
    offences: list[str] = []
    for frame in document.get("target_frames") or []:
        frame_id = str(frame.get("id"))
        slots = {str(slot.get("name")) for slot in frame.get("information_slots") or []}
        for rule in frame.get("advance_when") or []:
            target = str(rule.get("push"))
            if target not in frames:
                offences.append(f"{frame_id}: push={target!r} is not a frame")
            elif target == frame_id:
                offences.append(f"{frame_id}: push points at itself (runtime skips it)")
            if "delivered" in rule and str(rule["delivered"]) not in declared_refs:
                offences.append(
                    f"{frame_id}: delivered={rule['delivered']!r} is declared by no "
                    "frame's content_materials, so it can never be delivered"
                )
            if "slot" in rule and str(rule["slot"]) not in slots:
                offences.append(
                    f"{frame_id}: slot={rule['slot']!r} is not one of this frame's "
                    "information_slots (the runtime reads the current frame's slots)"
                )
    if offences:
        raise ProfilePackageError(
            f"profile package {path} has advance_when rules that can never fire "
            f"or that crash when they do: {offences}. A rule pointing at nothing "
            "is a dead rule -- the package seals, the conversation runs, and the "
            "platform just never advances (or raises KeyError mid-turn)."
        )


def _check_time_rules_do_not_gate_on_business_events(
    document: dict[str, Any], path: Path
) -> None:
    """`when.business_event_type` 今天没有输入 —— 声明它的规则封版即拒（批三扫描）。

    运行期早已写明（`time_rules.py` 的 `_when_allows`）：
    「`business_event_type` has no input here … **refuses rather than admits**」——
    带这个门控的规则**恒不触发**。但那是运行期的 refuse，封版不拦：
    配包作者写一条「发货后 2 小时跟进」，包封得出、规则静默恒死 ——
    与 `advance_when` 那四种死规则、`entry_conditions` 那 11 条谓词同族。

    **整条链三层都空**：ingress 的 envelope 把 `business_event` 写死 `None`
    （`adapter.py`）、运行期恒 `False`、没有任何配包声明过词表。
    入口 / 词表声明 / 消费三件登记在 **T502**，建成之前这里明确拒绝并告知，
    而不是让商家的规则静默不存在。
    """

    offences: list[str] = []
    for frame in document.get("target_frames") or []:
        for rule in frame.get("time_rules") or []:
            when = rule.get("when") or {}
            if isinstance(when, dict) and when.get("business_event_type") is not None:
                offences.append(f"{frame.get('id')}.{rule.get('id')}")
    if offences:
        raise ProfilePackageError(
            f"profile package {path} gates time rules on business_event_type, "
            f"which has no input yet: {offences}. The platform has no business "
            "event ingress today (the envelope field is hard-coded None), so "
            "such a rule would never fire -- silently. Registered as T502; "
            "remove the gate or wait for that batch."
        )


def _check_commitment_timer_rules_have_a_registrar(
    document: dict[str, Any], path: Path
) -> None:
    """`ai_commitment_due` 规则要求包里真的有人登记 AI 承诺 —— 否则封版即拒。

    AI 侧承诺的**唯一**入账路是 `outcome_rules`
    （`commitment_ledger.ai_rules_for`：`party == "ai"` 按打法筛）；
    到期定时器挂在 **per-row 锚** `commitment_due_at` 上，没有承诺行就没有行可挂。
    于是「帧声明了 `ai_commitment_due` 的 time_rule，而全包没有一条
    `party: ai` 且 `fulfilment: ai_commitment_due` 的 outcome_rule」
    是一条**恒不触发的规则**：包封得出、规则静默恒死 ——
    与 `business_event_type` 那条同族（2026-09-24 适配审查，K4：
    demo 五帧全配了它而 `outcome_rules` 为 0 条，且策略清一色
    `no_new_commitment`，五条规则从发布那天起一次都没可能响过）。

    **只检 AI 那一侧。** `customer_promise_due` 不经 `outcome_rules` ——
    客户承诺由理解步照抄客户的话入账（`admit_customer_commitments`），
    没有 registrar 也活得成。

    **按帧不按包地放行则不做**：carrier 的 consultation 帧没配
    `ai_commitment_due` 规则而 `advance_goal` 在那里也登记承诺 ——
    「哪些帧要追到期」是商家的选择；这里只拒「全包无人入账」那种全死。
    """

    declares = [
        f"{frame.get('id')}.{rule.get('id')}"
        for frame in document.get("target_frames") or []
        for rule in frame.get("time_rules") or []
        if rule.get("emit_kind") == "ai_commitment_due"
    ]
    if not declares:
        return
    registrars = [
        rule
        for rule in document.get("outcome_rules") or []
        if isinstance(rule, Mapping)
        and str(rule.get("party")) == "ai"
        and str(rule.get("fulfilment")) == "ai_commitment_due"
    ]
    if not registrars:
        raise ProfilePackageError(
            f"profile package {path} declares ai_commitment_due time rules "
            f"({declares}) but registers no way for an AI commitment to exist: "
            "outcome_rules has no entry with party 'ai' and fulfilment "
            "'ai_commitment_due'. The due timer hangs on the per-row anchor "
            "commitment_due_at, so without a registrar the rule can never "
            "fire -- silently. Add the outcome rule or drop the time rules."
        )


def _check_must_quote_strategies_have_words(document: dict[str, Any], path: Path) -> None:
    """`must_quote_material` 要求那一帧真的有词可引 —— 否则封版即拒。

    引用闸（`inference_validation` 的 `MATERIAL_QUOTE_REQUIRED`）判的是
    `if materials and not quoted`：`materials` 是本轮**声明且供了词**的交集
    （`platform_frames.materials_for_frame` ∩ 本轮可用）。一个声明了
    `must_quote_material` 的策略，被一个**全帧素材无词**的帧允许，
    那条约束**恒短路**：模型永远引不了素材、闸也永远不拦 ——
    包封得出、约束静默不存在（2026-09-24 适配审查 K3：carrier 的
    `offer_material` 就是这个形状，六段引用全无词）。

    **判「无词」不判「无声明」**：帧声明了引用而 `materials.json` 没供词
    的方向已由 `_check_material_coverage` 拒（有素材文件时）；这里补的是
    **根本没有素材文件**（或有文件而这一帧全部落空）的那一支 ——
    coverage 那条对没有 `materials.json` 的包整个不跑，正是 carrier 钻过的缝。
    """

    strategies = document.get("strategies") or {}
    quoting = {
        name
        for name, spec in strategies.items()
        if isinstance(spec, Mapping)
        and "must_quote_material" in (spec.get("generation_constraints") or [])
    }
    if not quoting:
        return
    texts = document.get(MATERIAL_TEXTS_KEY) or {}
    offences: list[str] = []
    for frame in document.get("target_frames") or []:
        allowed = set(frame.get("allowed_strategy_set") or []) | set(
            frame.get("time_rule_strategy_set") or []
        )
        uses = sorted(quoting & allowed)
        if not uses:
            continue
        declared = {
            str(item if isinstance(item, str) else item.get("ref"))
            for item in frame.get("content_materials") or []
        }
        worded = [
            ref
            for ref in declared
            if isinstance(texts.get(ref), str) and str(texts.get(ref)).strip()
        ]
        if not worded:
            offences.append(f"{frame.get('id')} allows {uses}")
    if offences:
        raise ProfilePackageError(
            f"profile package {path} allows strategies that must quote a material "
            f"in frames where no declared material has words: {offences}. The "
            "quote gate reads the declared-and-supplied intersection, so with no "
            "words the constraint never fires -- silently. Supply the words in "
            f"{MATERIALS_FILENAME} or stop allowing the strategy there."
        )


def _check_generation_constraints_are_known(document: dict[str, Any], path: Path) -> None:
    """配包为每条策略声明的 `generation_constraints`，**平台必须认识那个码**。

    **为什么在封版期拒**（T333，与 `_check_strategy_required_slots` 同族同理由）：
    这个字段今天是**自由字符串** —— schema 那一侧是
    `{"type":"array","items":{"type":"string"}}`，**没有 `enum`**。
    于是**拼错一个码与发明一个码完全不可分辨**：`no_new_commitmnet`
    过 schema、过封版、进提示词、进那份固定底的字节账，
    **然后被静默忽略** —— 而写配包的人以为自己加了一道约束。

    **它与 §4.1 那道闸方向相反、形状一样**：那一条防「拼错一个字导致某条打法
    **永远被拒**」，本条防「拼错一个字导致某条约束**永远不生效**」。
    一个的症状是「这条打法再也不工作」，**另一个没有症状** ——
    所以本条更需要在封版期问出来：**封版期是唯一还能问「你是不是想写另一个词」
    的时刻**，而运行期没有任何东西会因此变红。

    **判据用的是两张档的并集**（`ENFORCED_CONSTRAINTS | ADVISORY_CONSTRAINTS`），
    不是「有执行者的那五个」：嘱咐档的码是平台有意留下的
    （进提示词、没有校验器），**配包写它们是对的**。
    分档之前做不了这条检查 —— 那时唯一能做的差集会红在 6 处有意的嘱咐上。

    **本条不看平台帧**（`platform_frames.py` 自带的那份
    `generation_constraints: ["no_new_commitment"]`）：那是平台自己写的常量，
    不经过封版，而 `test_constraint_vocabulary.py` 已经盯着代码侧那一对。

    ## 它是**唯一执行者**，而「在 schema 里加 `enum`」评估过并被否掉了

    **想动这里的人第一反应会是「schema 层拦掉更早更干净」** ——
    那句话本身是对的，**而它在这个仓里的代价是另一回事**，所以写在这里：

    * **裁定（用户 2026-09-13）**：**不加 `enum`**，本函数是唯一执行者；
    * **理由是作用域，不是成本**：`merchant-profile` 这份合同在**三处**执行，
      其中 `pipeline.py:1917` **每一轮对话都跑** —— 所以 `enum` 不是一道新闸，
      是一次**对已存数据的追溯收紧**。库里一版声明了 `enum` 之外某码的 release，
      会从「那条约束没生效」变成「**那个租户每一轮对话失败**」，
      而 `profile_releases` 是 append-only，改一个字要重发一版并重绑；
    * **完整评估**：`docs/plans/generation-constraints-enum-evaluation-2026-09-13.md`；
      **什么条件下该重评**：`docs/backlog/ideas.md` 的 **T338**（两条触发点）。

    **本函数覆盖到哪里，也一并写清**：`publish_release` 的入参类型是
    `ProfilePackage`，而生产代码里**只有 `load_package` 的末尾构造它**
    —— 所以**任何一次发布都经过本函数**。唯一的漏洞是**直接
    `INSERT` 进 `profile_releases`**（今天只有测试夹具走那条路），
    而那个漏洞对旁边八道 `_check_*` 全都成立，不是本函数特有的。
    """

    from fastagent_v2.runtime.generation_constraints import (  # noqa: PLC0415
        unknown_constraints,
    )

    strategies = document.get("strategies") or {}

    offences: list[str] = []
    for name, body in strategies.items():
        unknown = unknown_constraints((body or {}).get("generation_constraints"))
        if unknown:
            offences.append(f"{name}: {list(unknown)}")

    if offences:
        raise ProfilePackageError(
            f"profile package {path} declares generation_constraints the platform does "
            f"not know: {offences}.\n"
            f"Enforced by validate_generation: {_enforced_codes()}.\n"
            f"Advisory (they reach the model through the plan prompt and have no "
            f"validator, on purpose): {_advisory_codes()}.\n"
            "A code outside that set is either a typo or an invention, and neither has "
            "a symptom at runtime: it passes schema, passes sealing, reaches the prompt, "
            "and is then silently ignored -- while whoever wrote the profile believes a "
            "constraint is in force.\n"
            "Fix the spelling, or -- if the constraint is real and new -- give it an "
            "executor branch in _validate_constraints and add it to "
            "ENFORCED_CONSTRAINTS. Adding it to ADVISORY_CONSTRAINTS is not the fix "
            "unless it genuinely cannot be machine-checked today."
        )


def _check_material_coverage(document: dict[str, Any], path: Path) -> None:
    """Declared refs and supplied texts must be the same set, once a package opts in.

    **Both directions are refused, and neither is pedantry.**

    * A ref declared by a frame with no text is the defect this whole mechanism
      exists to remove: the generation step would be handed a name and no words,
      and the model would fill the gap the only way it can -- by composing.
    * A text nobody references is the zero-caller case `AGENTS.md` asks about. It
      reads as "this material is in play" to anyone editing the file, while no
      frame can ever put it in a prompt.

    **Only once the package supplies a materials file.** A package without one
    keeps working exactly as before -- that is what makes this mechanism
    adoptable one package at a time rather than a flag day for every profile
    already published.
    """

    texts = document.get(MATERIAL_TEXTS_KEY)
    if texts is None:
        return

    declared: set[str] = set()
    for frame in document.get("target_frames", []):
        for item in frame.get("content_materials", []):
            declared.add(str(item if isinstance(item, str) else item.get("ref")))

    supplied = set(texts)
    missing = sorted(declared - supplied)
    if missing:
        raise ProfilePackageError(
            f"profile package {path} declares content materials with no text in "
            f"{MATERIALS_FILENAME}: {missing}. A frame that names a material the "
            "generation step cannot read is a frame whose words the model writes itself."
        )
    orphaned = sorted(supplied - declared)
    if orphaned:
        raise ProfilePackageError(
            f"profile package {path}: {MATERIALS_FILENAME} carries text for materials no "
            f"frame declares: {orphaned}. Add them to a frame's `content_materials` or "
            "remove them -- an unreferenced material can never reach a prompt, and "
            "leaving it here reads as though it could."
        )


def request_floor_bytes(
    frame: Mapping[str, Any], *, document: Mapping[str, Any]
) -> dict[str, int]:
    """每一种请求的**固定底**：回合部分全空时，那条请求真正要付的字节数。

    **它是这个仓里唯一一处算固定底的地方。** `scripts/check_material_dimensions.py`
    调它，`_check_request_floors` 调它，用例拿它与**真实请求**逐帧比等值。

    ## 先前有三把尺子，而三把互不相等（2026-09-15 实测，在架八帧）

    | | `internal-demo-sales/introduce` |
    |---|---:|
    | 报告器报的 | 9,654 |
    | 这道闸算的 | 10,179 |
    | **真实请求** | **10,939** |

    **方向全是 fail-open** —— 一份"刚好过闸"的包，真实请求已经超了。
    三笔差额的成因写在 `runtime.request_shape` 的 docstring 里，
    **其中第 3 笔与前两笔性质不同**：前两笔是算法错（单层对两层、
    `separators` 方向反了），第 3 笔是**「`user_prompt` 那一圈骨架不属于任何一格，
    于是没有人算它」—— 成本付了，全仓无人记账。**

    ## 今天的算法，以及它为什么是可证的

    ```
    固定底 = REQUEST_OVERHEAD_BYTES[role] + document_cost(回合部分全空的 user_prompt)
    ```

    **这个加法是恒等式，不是近似**：payload 把 `user_prompt` 放进去时是一个
    **字符串值**，而 `document_cost` 量的正是「一份文档作为那样一个字符串值要占多少」。
    实测（五种含引号 / 反斜杠 / 中文 / 控制字符的文档 × 三个角色）**残差恒为 0**，
    由 `test_request_floor_equals_the_real_request` 现量现比。

    **`user_prompt` 的形状来自 `runtime.request_shape`，与生产同一个函数** ——
    这是本次修法的全部要点：先前两处各拼各的，差出来的就是那段没人记账的骨架。
    """

    strategies = document.get("strategies") or {}
    slots = list(frame.get("information_slots") or [])
    allowed = list(frame.get("allowed_strategy_set") or [])
    goal = str(frame.get("goal") or "")
    prompts = {
        request_shape.UNDERSTAND_VALUE: request_shape.understand_prompt(
            goal=goal,
            information_slots=slots,
            # **与生产同一个推导** —— 固定底要付的就是生产要付的那几个字节。
            allowed_slot_values=allowed_slot_values(frame),
        ),
        request_shape.PLAN_CAPABILITY: request_shape.plan_prompt(
            goal=goal,
            exit_conditions=list(frame.get("exit_conditions") or []),
            information_slots=slots,
            allowed_strategies=allowed,
            strategies=strategies,
            event_kinds=[
                rule.get("emit_kind")
                for rule in frame.get("time_rules") or []
                if isinstance(rule, Mapping)
            ],
        ),
        # **生成步没有配包那一半** —— 它的 `user_prompt` 每一项都来自计划结果
        # 或这一回合，所以它的固定底与帧无关。列在这里是因为
        # **三种请求共用同一个 `MAX_MODEL_INPUT_BYTES`**，而先前这道闸只看两种。
        request_shape.GENERATE_VALUE: request_shape.generate_prompt(),
        # **第四种（ADR-0149），只在主动轮次发**。它的固定底 = 理解步的固定底
        # 加三个键（两个数 + 一个空字典），因为上下文那一半是**委托给
        # `understand_prompt` 的同一份**。
        #
        # ⚠️ **列在这里是补的**：第四份请求写完之后跑全量 2,418 条一条没红 ——
        # 一个会真的发出去的请求形状，没有任何东西检它装不装得下。
        # 上面那段注释记着同型的上一次（「先前这道闸只看两种」），**这是第三次**。
        request_shape.SILENCE_CAPABILITY: request_shape.silence_prompt(
            goal=goal,
            information_slots=slots,
            allowed_slot_values=allowed_slot_values(frame),
        ),
    }
    return {
        role: REQUEST_OVERHEAD_BYTES[role] + document_cost(prompt)
        for role, prompt in prompts.items()
    }


def _check_request_floors(
    frame: dict[str, Any], *, document: dict[str, Any], path: Path, frame_id: str
) -> None:
    """每一种模型请求的**固定底**，都要给这一回合的可变部分留下足够余量。

    **`MAX_MODEL_INPUT_BYTES` 是三种请求共用的一个上限**，而在此之前 admission
    只建模了其中一种（generate）的其中一项（`content_materials`）。
    understand 与 plan 的请求**各带着整段 `goal`**——每回合付两次——没有人检。

    **固定底由 `request_floor_bytes` 算**（唯一定义点，见那个函数）：
    回合部分全空时那条请求真正要付的字节 —— 供应商骨架 + system prompt + 响应 schema
    + `user_prompt` 自己那一圈键名 + 这个帧贡献的部分
    （goal / exit_conditions / required_slots / 槽位表 / 三张策略表）。
    留给回合的是 `understanding` 与**累积的槽位**，而这两样 admission 看不见。

    **2026-09-15 之前这里少算 639–840 字节，方向 fail-open** ——
    成因与修法见 `request_floor_bytes` 与 `runtime.request_shape` 的 docstring。

    **这条闸只断言可证的那一半：固定底本身装不下的，拒。**

    第一版拿 `MATERIAL_BYTES_BUDGET`（6,470）当三种请求共同的回合余量，理由是
    「平台已经为一个回合的可变内容定过一次价」。**数据当场否掉了它**：把响应
    schema 纳进来之后 plan 的固定底是 11,050，加 6,470 就是 17,520——
    **在架的、今天跑得好好的那个包会被拒**。6,470 是给**素材**定的价，
    而 understand / plan 的可变部分是 `understanding` 与累积的槽位，是另一样东西。

    **所以这里不发明那个准备金。** 它要多大，取决于真实的 `understanding` 有多大——
    那是一条要用数据回答的问题（用户 2026-09-11：先给数再定上界），
    **登记在 T275**。在它落地之前，这条闸能诚实断言的就是「固定底本身装不下」，
    而每个帧的余量由 `scripts/check_material_dimensions.py` 报出来，看得见。

    **要写明这条闸今天拦不住什么**：运维那次的形状是「固定底装得下，加上那一轮的
    understanding 就装不下」——**本条拦不住它**。拦住它的是 T275 的上界，不是这里。
    """

    over = {
        role: floor
        for role, floor in request_floor_bytes(frame, document=document).items()
        if floor >= MAX_MODEL_INPUT_BYTES
    }
    if over:
        detail = ", ".join(f"{role} {floor:,} 字节" for role, floor in sorted(over.items()))
        raise ProfilePackageError(
            f"{path}: frame {frame_id!r} 的这些请求，**光固定底就装不下**"
            f"（上限 {MAX_MODEL_INPUT_BYTES:,} 字节）：{detail}。"
            "固定底 = 回合部分全空时那条请求真正要付的字节（平台提示词 + 响应 schema + "
            "供应商骨架 + `user_prompt` 自己那一圈键名 + 这个帧贡献的部分）；"
            "**三种请求共用同一个上限**，而 `goal` 在 understand 与 plan 里各带一遍。"
            "超了不是变贵，是那一回合的调用在本地就被拒、回落保守计划、不发消息。"
            "两条路：把 `goal` 里只给生成步用的部分挪出去，或者把它写短。"
        )


def _check_stage_budget(
    frame: dict[str, Any], *, texts: dict[str, Any], path: Path, frame_id: str
) -> None:
    """每个阶段的主动素材加起来，要塞得进一个回合。

    **这条存在是因为「写下来的约束不等于成立的约束」**——这一轮反复踩的就是它。
    上限本身是从 `cost.MAX_MODEL_INPUT_BYTES` 反推的实测值
    （`worker.material_selection.MATERIAL_BYTES_BUDGET`），超了不是变贵，
    是 `ProviderInputTooLarge`：**那一回合发不出任何东西**。

    在 admission 拒，作者当场知道；不拒，就要等到某个真实会话走到那个阶段、
    模型一句话都没能说出口，而每一道门禁都是绿的。

    **只数正文与 ref**，不数提示词框架——框架那部分已经在 budget 反推时扣掉了。

    ---

    **2026-09-11：这条闸对「什么都没声明」的输入是失效的，而那正是它要拦的形状。**

    原来开头写着 `stages = frame.get(...) or (); if not stages: return`——
    **帧一个阶段都不声明，整条闸直接跳过**。它不是孤例，是三道闸的同一个空集条件：

    | 闸 | 条件 | 帧没声明阶段时 |
    |---|---|---|
    | 拒裸 ref（`_check_material_availability`） | `if stage_names:` | 不触发 |
    | proactive 必须点名阶段（同上） | `if stage_names and …` | 不触发 |
    | 本函数的字节预算 | `if not stages: return` | 不触发 |

    而选取层对**没有 `availability` 的声明**（裸 ref 就归一化成这个）走的是
    `_is_offered` 的无限制分支——**每个回合无条件全给**。三道闸全空 + 选取层全给，
    合起来就是：**一个不声明任何维度的包，可以带任意多素材封版、部署、然后每一个
    回合都被 `ProviderInputTooLarge` 拒掉。**

    **这不是推演，是已经发生过的事**（用户 2026-09-11 指出的根因）：内部演示包的
    `introduce` 帧 **296 条裸 ref、96,654 字节**，预算 6,132——**超 15.8 倍**，
    而当时 `cost.MAX_MODEL_INPUT_BYTES` 是 16 KB（**2026-09-15 起是 49,000**；
    这段记的是那次事故当时的数，96,654 对新上限同样超两倍不止）。
    包全绿封版、全绿部署，**客户一条消息都没收到**，每一道门禁都是绿的。

    **修法（用户裁定）：`per_stage` 为空时退回按全量算一次，超了拒。**

    **同形的上一次就在隔壁**：`_is_offered` 的 proactive 分支原来默认
    「不声明 stages = 不受阶段限制」，那次改成了 fail-closed——
    **改了默认，没改这条的空集行为。** 一条只在「有声明」时才生效的闸，
    对「什么都没声明」的输入等于不存在。
    """

    stages = frame.get(MATERIAL_STAGES_KEY) or ()
    per_stage: dict[str, int] = {
        # **`Mapping` 不是 `dict`**（独立审查 2026-09-11，P3-1）：同一批代码里
        # 可达性那条检查用的是 `Mapping`，两处一旦分叉，一个 Mapping-非-dict 的
        # 阶段会让 `per_stage` 变空，于是整个帧被悄悄改判到「一个阶段都没声明」
        # 那条路上——正是本批在修的那一类「换条路就没人管了」。
        str(s.get("name")): 0
        for s in stages
        if isinstance(s, Mapping)
    }
    # **每个回合都给的那些，要计进每一个阶段**（ADR-0125 §6.2）。
    # 不设闸的 `on_request` 兜底是用户亲自裁定保留的，理由是它的失败方向是
    # 「说不知道」而不是「编造」——但它照样占那一个回合的字节，而
    # `ProviderInputTooLarge` 不认识「这一段是有意不设闸的」。
    always_on = 0
    # **不受任何维度约束、因而每个回合都到场的那些。** 判据抄的是选取层
    # `_is_offered` 的两条无限制分支，不是另立一套：`availability` 缺席
    # （裸 ref 归一化后就是这个），以及 proactive 但一个阶段都没点名。
    # 帧声明了阶段时这两种都已被 `_check_material_availability` 拒掉，
    # 所以这个桶在那条路上恒为 0——`test_a_staged_frame_puts_nothing_in_the_
    # whole_bucket` 钉住这一点，因为它是「两条路不会互相重复计数」的理由。
    every_turn = 0
    topic_bytes: dict[str, int] = defaultdict(int)
    timer_bytes: dict[str, int] = defaultdict(int)
    for item in frame.get("content_materials", []) or []:
        if isinstance(item, str):
            # **归一化，不是跳过**（2026-09-11）。原来这里 `continue`，注释写着
            # 「没声明阶段时不会走到这个函数」——那句话在上面那个提前 return
            # 存在时是真的，而它正是缺陷本身。`material_selection.declarations()`
            # 把 `"X"` 归一化成的就是 `{"ref": "X"}`，两种拼法是同一条声明；
            # 这里按拼法跳过，等于让 296 条素材在预算上读到 0 字节。
            item = {"ref": item}
        elif not isinstance(item, Mapping):
            continue
        ref = str(item.get("ref"))
        cost = budgeted_entry_cost(ref, str(texts.get(ref, "")))
        availability = item.get("availability")
        # **不叫 `stages`**：函数开头那个 `stages` 是「这个帧声明了哪些阶段」，
        # 这个是「这段素材属于哪些阶段」，语义相反。第一版同名覆盖了外层，
        # 今天无害（外层此后不再被读），而下一次在循环后加一行读 `stages`
        # 就会静默拿到最后一段素材的值（独立复审 2026-09-11，P3-4）。
        item_stages = item.get("stages") or ()
        if availability is None:
            every_turn += cost
        elif availability == "proactive" and item_stages:
            for name in item_stages:
                if str(name) in per_stage:
                    per_stage[str(name)] += cost
        elif availability == "proactive":
            every_turn += cost
        elif availability == "on_request" and not (item.get("topics") or ()):
            always_on += cost
        elif availability == "on_request":
            for name in item.get("topics") or ():
                topic_bytes[str(name)] += cost
        elif availability == "timer":
            for name in item.get("emit_kinds") or ():
                timer_bytes[str(name)] += cost

    # ------------------------------------------------------------------
    # **文件候选也占字节，而在这之前 admission 一个都没算**（E3-4 补丁，09-17）
    # ------------------------------------------------------------------
    #
    # 一份包声明 asset 之后，每个能提它的回合都会多出几条
    # `{"ref": …, "label": …}` —— 而这个函数此前只数 `content_materials`。
    # 症状与那三次已记在案的发作**逐字相同**：封得了版、部署得上去，
    # 然后某一个真实回合的 generate 请求超限，**而每一道门禁都是绿的**。
    #
    # **文件没有「每回合都到场」那两个桶。** `availability` 必填，
    # 且三档各自强制自己那道闸（`on_request` 要 topics、`proactive` 要 stages、
    # `timer` 要 emit_kinds）—— 所以不存在话术那边的裸 ref 与
    # 「proactive 但没点名阶段」两种无闸声明，`every_turn` / `always_on` 都不加。
    #
    # **标签的取法与 `worker.material_selection.select_assets` 写两遍**
    # （跨包不 import，与 `MATERIAL_BYTES_BUDGET` 同一条边界），
    # 由 `tests/contracts/test_asset_bytes_are_reserved.py` 钉死相等。
    for asset in frame.get(CONTENT_ASSETS_KEY, []) or []:
        if not isinstance(asset, Mapping):
            continue
        asset_ref = str(asset.get("ref") or "")
        if not asset_ref:
            continue
        asset_label = asset_ref.split(":")[1] if asset_ref.count(":") >= 2 else asset_ref
        asset_cost = budgeted_asset_entry_cost(asset_ref, asset_label)
        asset_availability = str(asset.get("availability") or "")
        if asset_availability == "on_request":
            for name in asset.get("topics") or ():
                topic_bytes[str(name)] += asset_cost
        elif asset_availability == "proactive":
            for name in asset.get("stages") or ():
                # `in per_stage` 那道守卫照抄话术那边：键是帧声明过的阶段名。
                if str(name) in per_stage:
                    per_stage[str(name)] += asset_cost
        elif asset_availability == "timer":
            for name in asset.get("emit_kinds") or ():
                timer_bytes[str(name)] += asset_cost
        # **没有 else。** 不认得的 availability 在 `_check_asset_availability`
        # 就被拒了。在这里再兜一次会**吞掉**它 —— 一个拼错的 availability
        # 会变成「算 0 字节」而不是「封不了版」。

    # **topic 侧要进准备金**（独立审查 2026-09-11，P1-1）。先前它整个不在 admission
    # 里，于是「一个回合塞不塞得下」这条不变量只在 proactive 轴上成立。
    #
    # **实测本批正在封的那个包就会越线**：`introduce` 最大的阶段桶 2,902 字节在限，
    # 而槽里累积到 6 类时一个回合是 **6,973 字节 / 预算 6,132**，8 类是 8,432（1.38 倍）。
    # 匹配是**包含式**，槽里写过的词此后每回合继续命中，所以「累积」不是偶发。
    #
    # **上界由 profile 声明，缺省取最坏情形。** `material_topic_concurrency` 说
    # 「这个帧的槽最多同时点名几类」；不声明就按**全部 topic 桶**算——
    # 缺省必须是安全的那一侧，否则这条闸又会变成「只在有声明时才生效」，
    # 而那正是本批在修的那一类缺陷。
    #
    # **用户 2026-09-11 裁定封顶到 3 类**（另外两条路：准备金按全部 7 桶算要把
    # 19 个桶全部重切、主动段从 116 砍到约 57；或者在选取层截断，那是 worker 的
    # 新机制且失败方向是「模型手上没素材就自己编」）。
    #
    # **要写明的残留**：「goal 那句话真的把累积封在 3 类以内」今天**没有东西守着**——
    # 声明把它从脚本里的一个常量变成了 profile 里的一个字段，可审、可点名，
    # 但它仍然是一句散文与一个数字的一致性。登记在 T274。
    # **1,931 撤销之后，这里越线不再有缓冲**（独立审查 2026-09-11 P2-2）。
    # T274 写着「goal 那句话真的把累积封在 3 类以内，今天没有东西守着」，
    # 而匹配是包含式、会累积。实测本包 `introduce`：7 个桶，前六大
    # 1,174 / 953 / 920 / 868 / 803 / 730，声明 3 类取前三 = 3,047 ——
    # **第 4 类命中 +868，第 5 类再 +803**。
    #
    # 先前这一类越线由 `MATERIAL_BYTES_BUDGET` 与真实余量之间那 1,931 吸收；
    # 字节口径那批把它撤了（它是重复计数，撤得对），**现在只剩 1,000 安全余量，
    # 而那 1,000 已经被每回合的 `understanding` 占满**（T282）。
    # **它是预算口径的参数，不是运行期上限**（用户 2026-09-14 裁 (乙)）。
    # 运行期不数命中了几组（`_is_offered` 逐条问 `any(topic in topic_text)`），
    # 而槽是累积的 —— 所以声明 3 的帧在后面的回合可以提供全部 7 组。
    # **要把它变成真上限，先得答 T346 那一问**（同时给七组是好事还是坏事），
    # 否则那个 3 是从预算参数里借来的数，不是一个产品判断。
    #
    # **而「缺省按全部组算」那条安全读法今天在演示包上装不下** ——
    # 用本闸量过：`introduce` 的 `01-开场` 5,653 / 5,630，
    # 削掉之后最紧的阶段只剩 **29 字节**。
    # **那 29 不是余量问题，是信号**：今天的「全过」是靠一个假的预留数撑出来的
    # （按 3 组算 1,825，真实最坏 3,674）。**解法是重开预算链（T345），不是削包。**
    declared_concurrency = frame.get(MATERIAL_TOPIC_CONCURRENCY_KEY)
    concurrency = len(topic_bytes)
    if isinstance(declared_concurrency, int) and not isinstance(declared_concurrency, bool):
        concurrency = max(0, min(concurrency, declared_concurrency))
    reserve = sum(sorted(topic_bytes.values(), reverse=True)[:concurrency]) + always_on

    # **定时器回合也是一个回合**（独立审查 2026-09-11，P2-1）。`select_materials`
    # 在 `emit_kind` 非空时只给该 kind 的定时器素材，此后走的是同一个 `generate`
    # 与同一个 `MAX_MODEL_INPUT_BYTES`。先前两条路都不数它，而一条用例把这件事
    # 钉成了「正确行为」——那条用例答的是「它在不在活回合露面」，
    # 而预算问的是「有没有哪一种回合装不下」。
    over_timer = {n: b for n, b in timer_bytes.items() if b > MATERIAL_BYTES_BUDGET}
    if over_timer:
        detail = ", ".join(f"{n} {b:,} 字节" for n, b in sorted(over_timer.items()))
        raise ProfilePackageError(
            f"{path}: frame {frame_id!r} 的这些定时器回合装不下"
            f"（上限 {MATERIAL_BYTES_BUDGET:,} 字节）：{detail}。"
            "定时器回合走的是同一个模型请求，超了同样是 ProviderInputTooLarge。"
        )

    if not per_stage:
        # **帧一个阶段都没声明：没有「每个阶段」可数，就数「一个回合」。**
        # 这条路上到场的是不受维度约束的那些，加上 `reserve`——**兜底段与
        # 按 topic 设闸的那些都在里面**（P1-1：topic 侧先前整个不在 admission）。
        # 定时器素材不进这个总量，它按 `emit_kind` 自成一个回合，上面已经单独查过。
        total = every_turn + reserve
        if total > MATERIAL_BYTES_BUDGET:
            raise ProfilePackageError(
                f"{path}: frame {frame_id!r} 一个阶段都没声明，于是它的素材"
                f"**每个回合全给**，加起来 {total:,} 字节，塞不进一个回合"
                f"（上限 {MATERIAL_BYTES_BUDGET:,} 字节）。"
                "超了不是变贵，是那一回合的请求会被 ProviderInputTooLarge 拒掉，"
                "模型一句话都说不出口。"
                f"两条路：给这个帧声明 `{MATERIAL_STAGES_KEY}` 并让每段素材点名自己的阶段，"
                "或者把素材减到一个回合装得下。"
            )
        return
    for name in per_stage:
        # **每个阶段都要带上准备金**：一个回合是「这个阶段的主动素材 + 客户已经
        # 点名的那几类 + 不设闸的兜底」。先前只加 `always_on`，于是 topic 那一侧
        # 的字节在任何一条路上都没被数过。
        per_stage[name] += reserve
    over = {n: b for n, b in per_stage.items() if b > MATERIAL_BYTES_BUDGET}
    if over:
        detail = ", ".join(f"{n} {b:,} 字节" for n, b in sorted(over.items()))
        raise ProfilePackageError(
            f"{path}: frame {frame_id!r} 的这些阶段，主动素材加上 topic 准备金"
            f"（{reserve:,} 字节，按最多同时 {concurrency} 类算）"
            f"塞不进一个回合（上限 {MATERIAL_BYTES_BUDGET:,} 字节）：{detail}。"
            "超了不是变贵，是那一回合的请求会被 ProviderInputTooLarge 拒掉，"
            "模型一句话都说不出口。"
        )


def _check_material_availability(document: dict[str, Any], path: Path) -> None:
    """The availability rules JSON Schema cannot state, refused at admission.

    **Two families live here, and the name only covers the first**
    (independent review 2026-09-12 P3-4). The material rules below all say
    *this material can never be selected*. The **asset** rules added with E3-2b
    say the opposite thing: the file **could** be selected, and must not be
    without something gating it. Reading the asset refusals through the summary
    in the next paragraph inverts their reason.

    Each one describes a material that **can never be selected**, which is the
    same defect in a different spelling each time (the count is deliberately not
    written down -- it has been wrong twice already): the profile says a material is in play and no
    turn will ever offer it. Found at publish time it is a typo; found at
    runtime it is a conversation where the model had nothing to quote and wrote
    its own words instead, which is the failure this whole mechanism exists to
    remove -- and it would be invisible, because a message did go out.

    Refused for the same reason the dedupe collision above is refused: a profile
    that cannot work should not be publishable.
    """

    for frame in document.get("target_frames", []):
        frame_id = str(frame.get("id"))
        slot_names = {str(slot.get("name")) for slot in frame.get("information_slots", [])}
        topic_slot = frame.get(MATERIAL_TOPIC_SLOT_KEY)
        emitted = {str(rule.get("emit_kind")) for rule in frame.get("time_rules", [])}
        stage_names = {
            str(stage.get("name")) for stage in frame.get(MATERIAL_STAGES_KEY, [])
        }
        declared_refs = {
            str(item if isinstance(item, str) else item.get("ref"))
            for item in frame.get("content_materials", [])
        }

        # ---- 文件资料的闸，**没有兜底**（ADR-0130 §二.1(b)，裁定六 §2）----
        #
        # **这一段是把一句裁定变成门禁，而不是留在散文里**（ADR-0125 §三 的口径）。
        #
        # 素材那一侧有一条**有意的**不设闸兜底：topic 闸的失败方向是「该答的那段
        # 被藏起来，模型转而自己写」，所以留一条不设闸的 `on_request` 更安全。
        # **文件这一侧那个推理反过来**：不设闸意味着「发一个没人管它该不该发的
        # 文件」，失败方向从「模型自己写」变成「发错东西给客户，且撤不回」——
        # 本格既不做撤回也不做发前人工确认，**这些闸就是仅有的防线**。
        #
        # **每一档都必须点名它自己那道闸**（独立审查 2026-09-12 P1-1 / P2-1）：
        #
        # | `availability` | 必须点名 | 不许带 |
        # |---|---|---|
        # | `on_request` | `topics` | `stages` / `emit_kinds` |
        # | `proactive`  | `stages` | `topics` / `emit_kinds` |
        # | `timer`      | `emit_kinds` | `topics` / `stages` |
        #
        # **`proactive` 那一档的门槛取「必须点名 `stages`」，不取素材那侧的写法。**
        # 素材那侧是「帧声明了阶段时才要求点名」，安全的原因是**字节预算会接住
        # 漏网的那一种**（`_check_stage_budget`：一个阶段都不声明就每回合全给，
        # 加起来塞不进一个回合）。
        #
        # **这里原本写着「而资料不占提示词字节，预算接不住它」——
        # 那句话 2026-09-17 起不再成立**：E3-4 把 `content_assets` 送进了生成步的
        # 提示词，随后那一批让 `_check_stage_budget` 也数它们。
        # **而结论没变，只是理由换了一条**：预算按`stages`分桶，
        # 一个不点名阶段的 proactive 资料**一个桶都进不去**，
        # 于是预算恰恰接不住它——要求点名 `stages` 仍然是这里唯一的闸。
        # ADR-0130 §二.1(b) 把「没有兜底」写成了裁定。代价是想主动发文件的商家必须先给这个帧切阶段，
        # 这正是「没有能关住它的维度，不是把它放行的理由」（ADR-0125 §四）。
        #
        # **写错字段也要拒**：`proactive` + `topics` 的作者以为自己设了 topic 闸，
        # 而选取语义里 `proactive` 分支从头到尾不看 `topics`
        # （`material_selection.py`）——那个文件会每个回合都递出去，而包是绿的。
        for item in frame.get(CONTENT_ASSETS_KEY, []) or []:
            asset_ref = str((item or {}).get("ref"))
            asset_availability = (item or {}).get("availability")
            asset_topics = [str(t) for t in ((item or {}).get("topics") or ())]
            asset_emit_kinds = {str(k) for k in ((item or {}).get("emit_kinds") or ())}
            asset_stages = {str(x) for x in ((item or {}).get("stages") or ())}

            required, forbidden = {
                "on_request": ("topics", ("stages", "emit_kinds")),
                "proactive": ("stages", ("topics", "emit_kinds")),
                "timer": ("emit_kinds", ("topics", "stages")),
            }[str(asset_availability)]

            present = {
                "topics": asset_topics,
                "stages": asset_stages,
                "emit_kinds": asset_emit_kinds,
            }
            if not present[required]:
                raise ProfilePackageError(
                    f"{path}: frame {frame_id!r} asset {asset_ref!r} is "
                    f"{str(asset_availability)!r} and names no `{required}`. Every asset "
                    "must carry the gate its availability is selected by — ADR-0130 "
                    "§二.1(b) refuses an ungated file offer, because this cell ships "
                    "neither recall nor send-time human confirmation."
                )
            for extra in forbidden:
                if present[extra]:
                    raise ProfilePackageError(
                        f"{path}: frame {frame_id!r} asset {asset_ref!r} is "
                        f"{str(asset_availability)!r} and also declares `{extra}`, which "
                        f"nothing reads on that path — the author would believe a gate is "
                        "set that is never consulted, and the file would go out every "
                        "turn it is eligible for."
                    )

            if asset_topics and not topic_slot:
                raise ProfilePackageError(
                    f"{path}: frame {frame_id!r} asset {asset_ref!r} declares `topics` but "
                    f"the frame has no {MATERIAL_TOPIC_SLOT_KEY}, so nothing can ever "
                    "match them and the asset would never be offered."
                )
            if asset_topics and topic_slot:
                # **话题词表是从话术推出来的，而资料写得进一个不在里面的值。**
                #
                # `topic_vocabulary` 读的是 `content_materials` 的 topics
                # （`runtime/topic_vocabulary.py`），而它同时是模型被告知的
                # 「这个槽只收这些值」——两者同源是「裁 B」要的那件事。
                #
                # 所以一个**只被资料用过**的话题：这个函数此前放行、
                # 模型永远填不进那个值、**那份资料一次都不会被递出去** ——
                # 封得了版、部署得上去、永远不触发，**而没有任何东西会红**。
                # 2026-09-17 写内部演示租户的资料草案时发现的。
                vocabulary = allowed_slot_values(frame).get(str(topic_slot), [])
                if not vocabulary:
                    raise ProfilePackageError(
                        f"{path}: frame {frame_id!r} asset {asset_ref!r} declares `topics`, "
                        f"and the frame declares {MATERIAL_TOPIC_SLOT_KEY} "
                        f"{str(topic_slot)!r} -- but **no material of this frame carries "
                        "`topics`**, so the slot has no vocabulary and the model can never "
                        "put any value in it. The asset would never be offered. "
                        "Give at least one material a `topics` list first: the vocabulary "
                        "is derived from the wording, not declared separately."
                    )
                unreachable = sorted(set(asset_topics) - set(vocabulary))
                if unreachable:
                    raise ProfilePackageError(
                        f"{path}: frame {frame_id!r} asset {asset_ref!r} waits on topics the "
                        f"slot {str(topic_slot)!r} can never take: {unreachable}. "
                        f"The vocabulary is derived from this frame's materials and is "
                        f"{sorted(vocabulary)}. **An asset gated on a value outside it seals, "
                        "deploys, and is never offered once** -- so this is refused here "
                        "rather than discovered by the file not arriving."
                    )

            asset_unreachable = sorted(asset_emit_kinds - emitted)
            if asset_unreachable:
                raise ProfilePackageError(
                    f"{path}: frame {frame_id!r} asset {asset_ref!r} waits on emit kinds "
                    f"no time rule of this frame emits: {asset_unreachable}."
                )

            asset_unknown_stages = sorted(asset_stages - stage_names)
            if asset_unknown_stages:
                raise ProfilePackageError(
                    f"{path}: frame {frame_id!r} asset {asset_ref!r} waits on stages this "
                    f"frame does not declare: {asset_unknown_stages}."
                )

        for stage in frame.get(MATERIAL_STAGES_KEY, []):
            unknown_after = sorted(
                {str(ref) for ref in (stage.get("after") or ())} - declared_refs
            )
            if unknown_after:
                raise ProfilePackageError(
                    f"{path}: frame {frame_id!r} stage {str(stage.get('name'))!r} waits on "
                    f"materials this frame does not declare: {unknown_after}. The stage is "
                    "reached when those refs have been sent, and a ref no frame declares is "
                    "never sent, so the stage would never be reached."
                )
        # **同一族的第二条：被后面的阶段永久盖住的阶段**（2026-09-11）。
        #
        # `material_selection.current_stage()` 取的是**最后一个** `after` 被满足
        # 的阶段。于是「阶段 i 可达」等价于「存在一个已发集合 D，使 i 满足而它
        # 后面的都不满足」——取 `D = after_i` 就够，因为 `after` 是单调的：
        # **阶段 i 可达 ⟺ 它后面没有任何阶段 j 的 `after` 是 after_i 的子集。**
        #
        # **这不是理论上的形状，是刚刚差点发生的那一种**：把一个阶段按预算切成
        # 「刚接触-1入口 / -2渠道 / -3摸底 / -4跟进」四个并列桶、`after` 都写成
        # 空，读起来像「四个都从开场可达」，实测 `current_stage` 恒返回
        # **刚接触-4**，前三桶**一个回合都到不了**——挂在它们上面的素材可达段数
        # 是 0，而每一道门禁都是绿的。
        #
        # 拒在这里，作者当场知道；不拒，就是一批素材静静地永远不出现。
        stage_list = [s for s in frame.get(MATERIAL_STAGES_KEY, []) if isinstance(s, Mapping)]
        for index, stage in enumerate(stage_list):
            after = {str(ref) for ref in (stage.get("after") or ())}
            shadowed_by = [
                str(later.get("name"))
                for later in stage_list[index + 1 :]
                if {str(ref) for ref in (later.get("after") or ())} <= after
            ]
            if shadowed_by:
                raise ProfilePackageError(
                    f"{path}: frame {frame_id!r} stage {str(stage.get('name'))!r} 永远到不了："
                    f"排在它后面的 {shadowed_by} 的 `after` 是它的子集，而阶段推导取的是"
                    "**最后一个**满足的阶段，所以任何能满足它的会话同时也满足了后面那个。"
                    "挂在这个阶段上的素材一个回合都不会出现。"
                    "按预算切出来的并列子桶就是这个形状——把它们用 `after` 串起来，"
                    "或者合并成一个阶段。"
                )
        for item in frame.get("content_materials", []):
            # **判据是「这条声明有没有 `availability`」，不是它写成哪种拼法**
            # （独立复审 2026-09-11，P1-3）。`material_selection.declarations()`
            # 把 `"X"` 归一化成的**正是** `{"ref": "X"}`，那个模块的 docstring
            # 自己写着「A bare string and `{"ref": …}` are the same declaration」。
            # 第一版只拒了字符串那一种拼法，于是同一条声明换个写法就过——
            # 实测 40 KB 素材写成 `{"ref": X}` 通过 admission、预算读到 0 字节。
            # **上一轮「两件要一起做才闭合」的判断是对的，只是闭合的对象选错了。**
            if not (isinstance(item, Mapping) and item.get("availability")):
                if stage_names:
                    # **裸 ref 是 fail-closed 闸的一条现成绕法**（独立审查
                    # 2026-09-11，P1-1；ADR-0125 §6.2）。那条规则只作用于对象形，
                    # 于是同一段素材写成裸字符串就被整段跳过：选取层走无限制分支
                    # **每个回合都给**（连定时器回合也给），预算检查也读不到它。
                    # 实测一段 40 KB 的裸 ref 通过 admission、预算读到 0 字节。
                    #
                    # **这是一次契约收窄**：今天合法的形状会开始被拒。所以它是
                    # 裁定不是修补，写在 ADR-0125 §6.2 里。
                    ref = str(item if isinstance(item, str) else item.get("ref"))
                    raise ProfilePackageError(
                        f"{path}: frame {frame_id!r} declares stages, so every material must "
                        f"declare an `availability`; {ref!r} declares none, which skips the "
                        "stage gate and the byte budget entirely. Write it as "
                        f'{{"ref": {ref!r}, "availability": …, "stages": [...]}}.'
                    )
                continue
            ref = str(item.get("ref"))
            availability = item.get("availability")
            topics = item.get("topics") or ()
            emit_kinds = item.get("emit_kinds") or ()

            if topics and availability != "on_request":
                raise ProfilePackageError(
                    f"{path}: material {ref!r} in frame {frame_id!r} declares topics but is "
                    f"{availability!r}, not 'on_request'. Only an on_request material is "
                    "matched against a topic; anywhere else the topics would be decoration."
                )
            if topics and not topic_slot:
                raise ProfilePackageError(
                    f"{path}: material {ref!r} in frame {frame_id!r} declares topics, but the "
                    f"frame declares no {MATERIAL_TOPIC_SLOT_KEY!r}. There is nothing to match "
                    "them against, so the material could never be offered."
                )
            if emit_kinds and availability != "timer":
                raise ProfilePackageError(
                    f"{path}: material {ref!r} in frame {frame_id!r} declares emit_kinds but is "
                    f"{availability!r}, not 'timer'. Only a timer material is matched against "
                    "the kind of the event that fired."
                )
            if availability == "timer" and not emit_kinds:
                raise ProfilePackageError(
                    f"{path}: material {ref!r} in frame {frame_id!r} is 'timer' but names no "
                    "emit_kinds. A timer material is offered only on a turn whose event kind it "
                    "names, so one that names none is offered on no turn at all."
                )
            stages = item.get("stages") or ()
            # **拒的是 `on_request`，不再是「不等于 proactive」**（ADR-0149）。
            #
            # ⚠️ 原文写的是「Only the proactive bucket is stage filtered」，
            # **而那句话 2026-09-22 之后不成立**：阶段过滤从 `proactive` 那一支
            # 提到了 `_is_offered` 顶部，对每一档都生效。旧判据会把
            # 一个完全合理的 `any` + `stages` 拒掉。
            #
            # **`on_request` 仍然拒，而理由一个字没变**：话题档是「客户提起它
            # 就给」，那与对话走到哪一步无关 —— 给它挂阶段会让一段该答的话
            # 因为阶段不对而不出现，**而客户问了却答不上来是更坏的那一侧**。
            if stages and availability == "on_request":
                raise ProfilePackageError(
                    f"{path}: material {ref!r} in frame {frame_id!r} declares stages but is "
                    "'on_request'. A reactive material is offered when the customer raises its "
                    "topic, whatever stage the conversation is at -- gating it by stage would "
                    "hide the answer to a question he actually asked."
                )
            # **`any` 与 `proactive` 同样要点名阶段**（ADR-0149）：两者都受阶段过滤，
            # 而「不声明 stages = 不受限」正是下面这段实测逼出来的那个洞。
            if stage_names and availability in ("proactive", "any") and not stages:
                # **fail closed，而且这条是实测逼出来的。** 先前的默认是
                # 「不声明 stages 就是不受阶段限制」——对「整帧都没声明阶段」的
                # profile 是对的，对「帧声明了、这一段没声明」就是悄悄逃逸。
                #
                # 301 段那一批量出来：主动桶 164 段里只有 30 段带阶段维度，
                # **134 段没有**（≈ 43,684 字节）。按旧默认，一个回合会把这 134 段
                # 无条件给出去，而余量只有 8,063 字节——**阶段闸等于没关**。
                #
                # 拒在这里，作者就必须为每一段做决定：派个阶段，或者根本别声明它。
                # 「没有能关住它的维度」不是把它放行的理由。
                raise ProfilePackageError(
                    f"{path}: frame {frame_id!r} declares stages, so proactive material "
                    f"{ref!r} must name the stages it belongs to. A proactive material with "
                    "no stage escapes the stage gate entirely, which is the gate not working "
                    "rather than the material being unrestricted."
                )
            unknown_stages = sorted({str(name) for name in stages} - stage_names)
            if unknown_stages:
                raise ProfilePackageError(
                    f"{path}: material {ref!r} in frame {frame_id!r} names stages the frame "
                    f"does not declare: {unknown_stages} (the frame declares "
                    f"{sorted(stage_names)}). No turn would ever be in one, so the material "
                    "is unreachable."
                )
            unknown = sorted({str(kind) for kind in emit_kinds} - emitted)
            if unknown:
                raise ProfilePackageError(
                    f"{path}: material {ref!r} in frame {frame_id!r} names event kinds no time "
                    f"rule of that frame emits: {unknown} (the frame emits {sorted(emitted)}). "
                    "No timer will ever carry that kind, so the material is unreachable."
                )

        _check_stage_budget(
            frame,
            texts=document.get(MATERIAL_TEXTS_KEY) or {},
            path=path,
            frame_id=frame_id,
        )
        _check_request_floors(frame, document=document, path=path, frame_id=frame_id)

        if topic_slot and str(topic_slot) not in slot_names:
            raise ProfilePackageError(
                f"{path}: frame {frame_id!r} names {MATERIAL_TOPIC_SLOT_KEY} "
                f"{str(topic_slot)!r}, which is not one of its information slots "
                f"{sorted(slot_names)}. The value would always be empty, so every material "
                "gated on a topic would be permanently invisible."
            )


def load_package(path: Path, *, relative_to: Path | None = None) -> ProfilePackage:
    """Read, validate and hash-check one profile package.

    Raises `ProfilePackageError` for anything that would make the package
    unpublishable; it never returns a partially validated package.
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ProfilePackageError(f"profile package is unreadable: {path}") from error
    try:
        document = cast(dict[str, Any], json.loads(raw))
    except json.JSONDecodeError as error:
        raise ProfilePackageError(f"profile package is not valid JSON: {path}") from error
    if not isinstance(document, dict):
        raise ProfilePackageError(f"profile package is not a JSON object: {path}")

    # **Before the contract check**, so the schema validates the document the
    # rest of the platform will actually see, and **before the hash check**, so
    # the words are covered by `content_hash` exactly like the structure is.
    _merge_material_texts(document, path)

    try:
        validate_contract("merchant-profile", document)
    except (ValidationError, SchemaError) as error:
        raise ProfilePackageError(
            f"profile package does not satisfy the merchant-profile contract: {path}"
        ) from error
    except OSError as error:
        # `validate_contract` locates the schema through `Path.cwd()`. Run the CLI
        # from anywhere but the application root and this is a missing schema
        # file, not a bad profile. Reporting it as "your profile is invalid" sends
        # the operator to debug the wrong artefact, so it is re-raised as itself
        # with the cause named.
        raise ProfilePackageError(
            f"cannot validate {path}: the merchant-profile schema could not be read "
            f"({type(error).__name__}). Run this from the application root."
        ) from error

    # **Two rules on one frame that would render the same dedupe key are
    # refused here** (independent re-review 2026-09-09, P1-1). The same reason
    # as the `closing_frame_ref` check below: JSON Schema can pin the template,
    # it cannot say "and no two rules in this frame may render it alike".
    #
    # `dedupe_template` is pinned to `{emit_kind}:{target_frame_id}` and the
    # frame id is the same for every rule of a frame, so **two rules of one
    # frame with the same `emit_kind` always collide** -- and the collision does
    # not raise, it is *absorbed* by
    # `(tenant_id, conversation_id, aggregate_generation, dedupe_key)`: the
    # second rule silently never produces a timer.
    #
    # **Refusing at admission is the point.** The producer refuses too, but it
    # runs inside the dispatcher's accounting transaction, after the message has
    # left -- and an exception there rolls a completed delivery back into
    # `SENDING`, where the next claim sends it to the customer again. A profile
    # that can never work should not be publishable; it must not be a thing that
    # detonates on the send path.
    # **A rule id is a lookup key across the whole document, so it has to be
    # unique across the whole document** (B4-4d independent review, P2-1). B4-4d
    # gave `time_rules[].strategy` its first production reader, and the timer
    # carries only `payload_ref = {"rule_id": …}`: `strategy_for_rule` finds the
    # rule by scanning **every** frame. Two frames sharing an id therefore means
    # the first one found binds the other frame's timer -- and that is not
    # fail-closed. If the firing frame's `time_rule_strategy_set` happens to
    # contain the wrong rule's strategy, the plan passes the validator and the
    # customer receives a play the rule that armed the timer never declared.
    #
    # **Refused at admission for the same reason as the dedupe collision below**:
    # the alternative is a check on the send path, where the failure is a
    # message already gone. The shipped profiles prefix ids with the frame
    # (`welcome-silence_check`), but that is a convention, and the schema asks
    # only for `minLength: 1`.
    # ----------------------------------------------------------------------
    # **A profile may not declare a platform frame** (B4-7, ADR-0099). The
    # platform owns the `platform:` namespace: `frame_definitions` resolves
    # those ids for every conversation whether the profile mentions them or not,
    # and it writes the platform entry last, so a profile frame carrying such an
    # id is a definition that can never be reached -- the profile would be
    # describing behaviour the tenant then does not get.
    #
    # **Refused at admission for the same reason as the two checks below**: the
    # alternative is finding out on the turn a live conversation's root goal
    # completes, which is where ADR-0031 already put one bad surprise.
    for frame in document.get("target_frames", []):
        frame_id = str(frame.get("id"))
        if is_platform_frame(frame_id):
            raise ProfilePackageError(
                f"{path}: target frame id {frame_id!r} uses the reserved "
                f"{PLATFORM_FRAME_ID_PREFIX!r} namespace, which belongs to the platform. "
                "A profile cannot declare, shadow or redefine a platform frame."
            )

    # ----------------------------------------------------------------------
    # **The paragraph below belongs to the `rule_ids` loop**, not to the
    # platform-namespace check above it (independent review 2026-09-10, P3-4).
    # The new check was inserted between that comment and the loop it explains,
    # so it read as the new check's rationale.
    rule_ids: dict[str, str] = {}
    for frame in document.get("target_frames", []):
        kinds: dict[str, str] = {}
        for rule in frame.get("time_rules", []):
            rule_id = str(rule.get("id"))
            frame_id = str(frame.get("id"))
            if rule_id in rule_ids:
                raise ProfilePackageError(
                    f"{path}: time rule id {rule_id!r} is declared twice "
                    f"(frames {rule_ids[rule_id]!r} and {frame_id!r}). Rule ids are how a "
                    "pending timer finds the rule that armed it, so a repeat would bind one "
                    "frame's declared strategy to the other frame's timer."
                )
            rule_ids[rule_id] = frame_id
            emit_kind = str(rule.get("emit_kind"))
            if emit_kind in kinds:
                raise ProfilePackageError(
                    f"{path}: frame {frame_id!r} declares two time rules of kind "
                    f"{emit_kind!r} ({kinds[emit_kind]!r} and {rule_id!r}). "
                    "They render the same dedupe key, so the second would never produce a "
                    "scheduled event."
                )
            kinds[emit_kind] = rule_id

    # Run after the contract check, which is what guarantees `target_frames` is
    # a list of objects; the merge above had to happen before it.
    # **排在覆盖检查之前**：覆盖检查比的是两个集合相不相等，
    # 而一个写错的引用会让它报「有素材没文本」——**那句话是对的但指错了地方**，
    # 作者会去补一段本来就存在、只是键写错了的文本。
    _check_reference_grammar(document, path)
    _check_memory_fields(document, path)
    # **先判「这个名字存在吗」，再判「它要的槽位够不够」** ——
    # 反过来的话，一个根本没声明的策略会在下一条里走到
    # 「没有任何帧允许它 → 跳过不判」那一支，两条都不报，
    # 而它正是 2026-09-23 那次静默失效的形状。
    _check_every_allowed_strategy_is_declared(document, path)
    _check_every_delivery_filled_slot_is_declared(document, path)
    _check_some_strategy_declares_a_required_slot(document, path)
    _check_strategy_required_slots(document, path)
    _check_required_slots_exist_in_every_allowed_frame(document, path)
    _check_exit_conditions_point_at_this_frames_slots(document, path)
    _check_advance_when_is_wired(document, path)
    _check_time_rules_do_not_gate_on_business_events(document, path)
    _check_commitment_timer_rules_have_a_registrar(document, path)
    _check_must_quote_strategies_have_words(document, path)
    _check_generation_constraints_are_known(document, path)
    _check_material_coverage(document, path)
    _check_material_availability(document, path)

    closing_reference = document.get("closing_frame_ref")
    if closing_reference is not None:
        # JSON Schema can say "a string"; it cannot say "one of the ids in this
        # same document". A dangling reference would not fail until a live
        # conversation completed its root goal and the pipeline tried to enter a
        # frame the profile does not define -- the worst possible moment to find
        # out (ADR-0031).
        frames = document["target_frames"]
        frame_ids = {str(frame["id"]) for frame in frames}
        if str(closing_reference) not in frame_ids:
            raise ProfilePackageError(
                f"profile package {path} declares closing_frame_ref "
                f"{closing_reference!r}, which is not one of its target frames "
                f"{sorted(frame_ids)}"
            )
        if frames and str(closing_reference) == str(frames[0]["id"]):
            # `should_enter_closing_frame` refuses to push a closing frame when
            # the stack is already in one, and the root frame *is* the first
            # definition -- so a self-referential reference turns the whole
            # feature off with no signal at all. Refusing here makes the
            # misconfiguration loud instead of invisible (independent review
            # 2026-09-03, P3-3).
            raise ProfilePackageError(
                f"profile package {path} declares closing_frame_ref "
                f"{closing_reference!r}, which is its own root target frame. A "
                f"closing frame cannot close itself, so this would silently "
                f"disable the closing turn."
            )

    declared = str(document["identity"]["content_hash"])
    calculated = canonical_hash(document)
    if declared != calculated:
        # Deliberately not "recompute and continue": the declared hash is what a
        # reviewer signed off on and what evidence elsewhere refers to. A package
        # whose bytes drifted from its hash is a package nobody reviewed.
        # **Says which files the hash covers when a materials file was merged.**
        # The stamped hash is taken over the *merged* document, so editing a
        # single sentence in `materials.json` invalidates a hash that lives in
        # `profile.json` -- correct, and baffling if the message only names
        # `profile.json`. The words are part of what a reviewer signed off on;
        # that is the whole reason they are inside the hash.
        covers = (
            f" (the hash covers {PACKAGE_FILENAME} together with {MATERIALS_FILENAME}; "
            "editing either one means restamping)"
            if MATERIAL_TEXTS_KEY in document
            else ""
        )
        raise ProfilePackageError(
            f"profile package content hash does not match its content: {path} "
            f"declares {declared} but hashes to {calculated}{covers}"
        )

    source = path
    if relative_to is not None:
        try:
            source = path.resolve().relative_to(relative_to.resolve())
        except ValueError as error:
            # Not a fallback. `source_path` is provenance -- the migration header
            # calls it the answer to "which trusted package did this come from".
            # Recording an absolute out-of-tree path here would answer it wrongly
            # in a format that reads like a repository-relative path.
            raise ProfilePackageError(
                f"profile package {path} is not under the provenance root "
                f"{relative_to}; refusing to record a source_path that would "
                f"look repository-relative but is not"
            ) from error
    return ProfilePackage(
        document=document,
        content_hash=calculated,
        source_path=source.as_posix(),
    )


@dataclass(frozen=True, slots=True)
class ProfileRegistry:
    """The set of trusted profile packages under one root directory."""

    root: Path
    #: Path that `source_path` is recorded relative to. Defaults to the root's
    #: parent so the recorded provenance reads `profiles/<name>/profile.json`.
    relative_to: Path | None = None

    def _relative_to(self) -> Path:
        return self.relative_to if self.relative_to is not None else self.root.parent

    def _trusted_root(self) -> Path:
        return _require_inside_repository(self.root, what="profile registry root")

    def packages(self) -> tuple[ProfilePackage, ...]:
        """Every valid package under the root, ordered by directory name.

        Identity collisions are refused rather than resolved: two packages that
        claim the same profile_id/version, or the same release id, make "which
        one did we publish" unanswerable.

        There is no separate identical-content check. It would be unreachable:
        the content hash covers `identity`, so two packages that hash alike are
        the same document and collide on profile_id/version first.
        """

        found: list[ProfilePackage] = []
        by_version: dict[tuple[str, str], str] = {}
        by_release: dict[str, str] = {}
        for package in self._discover():
            version_key = (package.profile_id, package.version)
            if version_key in by_version:
                raise ProfilePackageError(
                    f"two profile packages claim {package.profile_id} {package.version}: "
                    f"{by_version[version_key]} and {package.source_path}"
                )
            release_key = str(package.release_id)
            if release_key in by_release:
                raise ProfilePackageError(
                    f"two profile packages claim release id {release_key}: "
                    f"{by_release[release_key]} and {package.source_path}"
                )
            by_version[version_key] = package.source_path
            by_release[release_key] = package.source_path
            found.append(package)
        return tuple(found)

    def _discover(self) -> Iterator[ProfilePackage]:
        root = self._trusted_root()
        if not root.is_dir():
            raise ProfilePackageError(f"profile registry root does not exist: {self.root}")
        for path in sorted(root.glob(f"*/{PACKAGE_FILENAME}")):
            yield load_package(path, relative_to=self._relative_to())

    def package(self, *, profile_id: str, version: str) -> ProfilePackage:
        """The package with this exact identity.

        Raises `ProfileNotFound` when nothing matches. It does not fall back to
        a default profile, the newest version, or the only package present --
        see `ProfileNotFound`.
        """

        for package in self.packages():
            if package.profile_id == profile_id and package.version == version:
                return package
        available = ", ".join(
            f"{item.profile_id} {item.version}" for item in self.packages()
        )
        raise ProfileNotFound(
            f"no trusted profile package for {profile_id} {version}; "
            f"registry holds: {available or '<none>'}"
        )
