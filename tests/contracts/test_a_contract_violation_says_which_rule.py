"""契约不过的时候，那句拒绝要说出**是哪一条规则、在哪个位置**。

## 它防的那件事 2026-09-22 在 stage 上发生过

同一条客户消息、**同一份输入**（每次都是 3,211 input / 461 output tokens），
理解步跑了 4 次挂了 2 次，而审计里全部的信息只有这一句：

    UNDERSTAND_VALUE output does not satisfy the understanding-result contract

**说不出是哪一条规则不过。** 运维为缩小范围花了一个多小时，
最后仍然没缩到某一条
（`docs/handoffs/ops-to-author-the-memory-write-takes-the-turn-with-it-2026-09-22.md`
缺陷 2）。而 `validator` 与 `absolute_path` **一直就在那个异常里**，
`from error` 还把它挂着 —— 没有任何地方把它们记下来。

## 三条各守一半

1. `test_the_refusal_names_the_rule_and_the_place` —— 说得出；
2. `test_the_refusal_still_carries_none_of_the_model_s_words` —— **而且不说别的**。
   这两条是一对：只有第一条，下一个人把 `error.message` 整个拼进去就"更详细"了，
   而 jsonschema 在 `maxLength` / `enum` / `pattern` 上的 message **就是那个值本身**
   （`describe_validation_error` 的 docstring 记着那次把商家话术打进部署记录的事）；
3. `test_every_broken_rule_is_listed_not_just_the_first` —— **报全部，不是第一条**。
   `validator.validate()` 只抛 jsonschema 认为最匹配的那一条，而
   `AGENTS.md` 把「jsonschema 的第一条 violation」点名列在
   「任何『报第一个』的工具」那一族里。一个间歇性失败的供应商，
   不该让人「修一条再跑一次」地试。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastagent_v2.runtime.contracts import inference_contract_violations
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.inference_model import _violations_for_operator

#: 客户说的话，**一个字都不许出现在拒绝消息里**。
#: 取一句像真话的，而不是 `"x"` —— 一个单字符的值在任何截断下都不会露出来，
#: 那种夹具会让这条用例为了错的理由绿（`AGENTS.md`：夹具不能恰好等于错误答案）。
CUSTOMER_WORDS = "我们是连锁餐饮，12家店，客服就2个人"


def _understanding(**overrides: Any) -> dict[str, Any]:
    """一份除了被点名的那处之外都合法的理解结果。

    ⚠️ **底稿取自平台自己的确定性替身**（`DeterministicMockInference.understand`），
    不是手写的。第一版是手写的，少了 `intents` / `sentiment` /
    `customer_commitments` / `uncertainties` 四个必填键 —— 于是"合法"那一条当场红，
    而上面那几条**在一份本来就坏的底稿上也会绿**，为的是别的理由。
    `AGENTS.md`「怎么算满足」的第 2 档就是这个：**拿对方真正会产生那个结构的
    代码路径去驱动**，而不是自己凭空写一份。
    """

    document = dict(
        DeterministicMockInference().understand(
            envelope={
                "event_id": "00000000-0000-4000-8000-000000000000",
                "event_type": "customer_message",
                "content": {"text": CUSTOMER_WORDS},
            },
            profile={},
            aggregate={},
        )
    )
    document.update(overrides)
    return document


def test_the_refusal_names_the_rule_and_the_place() -> None:
    """**哪一条规则、在哪个位置** —— 两样都要，而且都来自我们自己的 schema。

    `turn_summary` 取一个超长的值：`maxLength` 是一条**平台自己写下的**约束，
    所以「约束是什么」可以照打，而「实际是什么」只打形状。
    """

    document = _understanding(turn_summary=CUSTOMER_WORDS * 200)
    violations = inference_contract_violations("understanding-result", document)

    assert violations, "文档是坏的，而这里一条违例都没列出来"
    line = "\n".join(violations)
    assert "turn_summary" in line, f"没说是哪个位置：{line}"
    assert "maxLength" in line, f"没说是哪一条规则：{line}"


def test_the_refusal_still_carries_none_of_the_model_s_words() -> None:
    """说得更清楚**不等于**说得更多。

    这一条与上一条是一对。单独留上一条，下一个人会把 `error.message` 拼进去 ——
    而在 `maxLength` / `enum` / `pattern` 上，那个 message **就是那个值本身**。
    """

    document = _understanding(turn_summary=CUSTOMER_WORDS * 200)
    line = "\n".join(inference_contract_violations("understanding-result", document))

    assert CUSTOMER_WORDS not in line, f"客户原话漏进了拒绝消息：{line[:200]}"
    # 整句不在，半句也不许在 —— 截断救不了这一类（留下的是正文，掉的是尾巴）。
    assert "连锁餐饮" not in line, f"客户原话的一部分漏了出来：{line[:200]}"


def test_every_broken_rule_is_listed_not_just_the_first() -> None:
    """两处坏，就要报两处。

    **这一条是「报第一个」那一族的守卫**：`validate()` 抛一条，而这里问的是全部。
    两处故意选在不同的 `json_path` 上，否则 jsonschema 可能把它们合成一条，
    那样这条用例会**因为一个与它无关的原因**通过。
    """

    document = _understanding(
        turn_summary=CUSTOMER_WORDS * 200,  # maxLength
        risk_markers=["这个词不在那七个里"],  # enum
    )
    violations = inference_contract_violations("understanding-result", document)
    places = {violation.split(":")[0] for violation in violations}

    assert len(violations) >= 2, f"只报了 {len(violations)} 条：{violations}"
    assert any("turn_summary" in place for place in places), places
    assert any("risk_markers" in place for place in places), places


def test_a_clean_document_has_no_violations() -> None:
    """反侧：合法的文档不许被列出任何一条。

    少了这条，一个「什么都报一遍」的实现也能满足上面三条
    —— 而那是另一种坏法（`AGENTS.md`：放宽一条判据时正反两侧都要验）。
    """

    assert inference_contract_violations("understanding-result", _understanding()) == []


@pytest.mark.parametrize(
    ("count", "expected_tail"),
    [(6, ""), (7, "(+1 more, 7 in total)")],
)
def test_the_cap_says_how_many_it_did_not_show(count: int, expected_tail: str) -> None:
    """有上限就把丢掉的数说出来 —— **静默截断读起来像「就这些」**。

    参数取在边界**两侧**（6 = 上限本身，7 = 刚超一条），
    而不是取 3 和 100：一个写成 `> _MAX + 1` 的差一错在后者下照样绿。
    """

    line = _violations_for_operator([f"a/{index}: pattern" for index in range(count)])

    assert ("more" in line) == bool(expected_tail), line
    if expected_tail:
        assert line.endswith(expected_tail), line


def test_an_empty_list_does_not_read_as_passing() -> None:
    """`validate()` 抛了而重验一条都没有 —— 那不是「没有违例」。

    写成「没有违例」会让读的人以为它通过了。这一条钉住那句话不是那个意思。
    """

    line = _violations_for_operator([])

    assert "no violation" in line
    assert "changed between the two passes" in line
