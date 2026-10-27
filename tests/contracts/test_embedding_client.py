"""嵌入客户端的形状，**逐条对着那次实测写**。

来源：`evidence/knowledge/embedding-dimension-2026-09-11.md`（用户当场提供凭据、
真实调用、只打印向量长度与元素类型）。本文件里每一个「响应长什么样」的断言，
都能在那份记录里找到对应的原始输出。

**为什么这值得一整个文件**：这个端点与 OpenAI 的 embeddings API **三处不同**，
而三处都是「照着 OpenAI 写会跑起来、然后悄悄给出错东西」的那一类：

* 端点不同（`/embeddings` 对这个模型是 400）；
* 请求的 `input` 元素是对象不是字符串；
* **响应的 `data` 是对象不是数组**，而 `object` 字段偏偏写着 `"list"`。

最后一条最阴：一个照着 `object` 的字面意思写 `data[0]` 的实现，
在这里会 `KeyError`——那还算好的。**真正危险的是第四条**，见
`test_two_texts_in_one_call_is_refused_rather_than_merged`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.worker.embedding import (
    EmbeddingBinding,
    EmbeddingClient,
    EmbeddingDimensionMismatch,
)
from fastagent_v2.worker.providers import (
    ProviderCredentialMissing,
    ProviderHttpError,
    ProviderMalformedOutput,
    TransportResponse,
)

ROOT = Path(__file__).resolve().parents[2]
RELEASE = json.loads(
    (ROOT / "packages/contracts/model-policy/release-2026.09.02-5.json").read_text(
        encoding="utf-8"
    )
)


@dataclass
class _RecordingTransport:
    """记下它被怎么调的，并回一个可配置的响应。

    **不是 mock 框架**：本文件要断言的恰恰是 URL、payload 与响应解读三件事，
    而一个记录型替身让它们成为可读的断言而不是一串 `assert_called_with`。
    """

    body: dict[str, Any]
    status_code: int = 200
    calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        return TransportResponse(status_code=self.status_code, body=self.body)


def _binding() -> EmbeddingBinding:
    return EmbeddingBinding.from_release(RELEASE)


def _ok_body(dimension: int = 2048) -> dict[str, Any]:
    """实测那次 200 响应的形状。

    `object` 说 `"list"` 而 `data` 是一个对象 —— **两者并存是原样照抄的**，
    不是这里写错了。
    """

    return {
        "created": 1757000000,
        "id": "0217570000000000000000000000000000000000000000000000",
        "model": "doubao-embedding-vision-251215",
        "object": "list",
        "data": {"embedding": [0.01] * dimension, "object": "embedding"},
        "usage": {
            "prompt_tokens": 26,
            "prompt_tokens_details": {"image_tokens": 0, "text_tokens": 26},
            "total_tokens": 26,
        },
    }


@pytest.fixture(autouse=True)
def _credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FASTAGENT_MODEL_API_KEY_VOLCENGINE_ARK", "probe-key-not-a-real-one")


# ---------------------------------------------------------------------------
# 它读的是 release 里那块先前零读者的 `embedding`
# ---------------------------------------------------------------------------


def test_the_binding_comes_from_the_shipped_release_not_a_constant() -> None:
    """**本模块是 `release-*.json` 里 `embedding` 那一块的第一个读者。**

    它先前躺在那里没人看，而它记的四项（维度 / 距离 / 归一化 / 截断策略）
    正是 RFC `:728` 要求 embedding release 冻结的那四项，
    也正是 `knowledge_releases` 的四个列。**不要在代码里另写一份。**
    """

    binding = _binding()
    assert binding.model_id == "doubao-embedding-vision-251215"
    assert binding.dimension == 2048
    assert binding.distance_metric == "cosine"
    assert binding.normalization == "l2"
    assert binding.truncation_policy == "reject-over-limit"
    assert binding.api_base.endswith("/api/v3")
    assert not binding.api_base.endswith("/")


def test_the_declared_dimension_matches_the_column_the_chunks_live_in() -> None:
    """`knowledge_chunks.embedding` 是 `vector(2048)`，定宽。

    两处对不上时，失败会发生在写库那一刻、报的是一个 pgvector 的错 ——
    而真正的原因在一个 JSON 文件里。这条把它们钉在一起。
    """

    migration = (ROOT / "infrastructure/migrations/050_knowledge_base.sql").read_text(
        encoding="utf-8"
    )
    assert f"vector({_binding().dimension})" in migration


# ---------------------------------------------------------------------------
# 请求：端点、形状
# ---------------------------------------------------------------------------


def test_it_calls_the_multimodal_endpoint_because_the_plain_one_refuses_this_model() -> None:
    """`/embeddings` 对这个模型是 **HTTP 400**（实测）。

    错误消息点名的是 `model` 而不是 url——所以一个打错端点的实现，
    症状读起来像「模型 ID 写错了」。**端点写死在模块里**，这条钉住它。
    """

    transport = _RecordingTransport(body=_ok_body())
    EmbeddingClient(transport=transport, binding=_binding()).embed_one(
        "请问你们这个产品怎么收费？", timeout_seconds=5.0
    )

    url = transport.calls[0]["url"]
    assert url.endswith("/embeddings/multimodal"), url
    assert not url.endswith("/v3/embeddings")


def test_the_input_element_is_an_object_not_a_bare_string() -> None:
    """`[{"type": "text", "text": …}]`，实测形状。

    OpenAI 那边是 `["…"]`，而这里传字符串数组会被拒——
    **两种写法都「看起来对」，只有一种能用。**
    """

    transport = _RecordingTransport(body=_ok_body())
    EmbeddingClient(transport=transport, binding=_binding()).embed_one(
        "发货一般要几天？", timeout_seconds=5.0
    )

    payload = transport.calls[0]["payload"]
    assert payload["model"] == "doubao-embedding-vision-251215"
    assert payload["input"] == [{"type": "text", "text": "发货一般要几天？"}]


def test_two_texts_in_one_call_is_refused_rather_than_merged() -> None:
    """**这一条是整个文件里最要紧的。**

    实测：两段文本送进去，`prompt_tokens` 从 26 涨到 31（两段之和），
    **而只回了一个 2048 维向量**。`input` 看起来是批量轴，**它不是**。

    失败方向是安静的：调用方以为拿到了两条内容各自的向量，实际拿到一个
    谁都不代表的平均，**而检索照常返回结果，只是结果没有意义**。
    这正是 `AGENTS.md` 那条「多传一个不报错的接口要有会红的守卫」，
    而嵌入接口就是当时记下的三个实例之一。

    守卫是 `_request` 里那条 `assert`。这条用例从**私有入口**打它，
    因为公开入口 `embed_one` 只收一段文本——**公开面本身就是第一道守卫**，
    而这一条证明的是「即使绕过公开面，那条线仍然在」。
    """

    client = EmbeddingClient(transport=_RecordingTransport(body=_ok_body()), binding=_binding())

    with pytest.raises(AssertionError, match="one chunk per call"):
        client._request(
            [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}],
            timeout_seconds=5.0,
        )


def test_an_empty_body_is_refused_before_a_socket_is_opened() -> None:
    """空正文的向量在余弦相似度下与任何东西都有一个说得过去的分数。

    **拒在这里而不是让它进库**：`knowledge_chunks.body` 的 CHECK 也拒空正文，
    两道闸方向一致 —— 但这一道省掉一次往返，而且错误消息说得清。
    """

    transport = _RecordingTransport(body=_ok_body())
    client = EmbeddingClient(transport=transport, binding=_binding())

    with pytest.raises(ValueError, match="empty text"):
        client.embed_one("   ", timeout_seconds=5.0)
    assert transport.calls == [], "it must not reach the network"


def test_a_missing_credential_is_named_not_swallowed() -> None:
    """凭据缺失走的是既有的 `credential_for`，**不另写一条**。

    它的消息里写着「v2 must use its own account, never a legacy one」——
    那是 v1 隔离那条硬边界在代码里的落点，本模块没有理由重述一遍。
    """

    import os

    transport = _RecordingTransport(body=_ok_body())
    client = EmbeddingClient(transport=transport, binding=_binding())
    saved = os.environ.pop("FASTAGENT_MODEL_API_KEY_VOLCENGINE_ARK", None)
    try:
        with pytest.raises(ProviderCredentialMissing):
            client.embed_one("一段话", timeout_seconds=5.0)
    finally:
        if saved is not None:
            os.environ["FASTAGENT_MODEL_API_KEY_VOLCENGINE_ARK"] = saved
    assert transport.calls == []


# ---------------------------------------------------------------------------
# 响应：`data` 是对象，而 `object` 说 "list"
# ---------------------------------------------------------------------------


def test_the_vector_is_read_out_of_an_object_even_though_object_says_list() -> None:
    """实测那次 200 的原样形状，两件事并存且都照抄。

    **不看 `object` 字段**：它说 `"list"` 而 `data` 是一个对象。
    一个信 `object` 的实现写 `data[0]`，在 dict 上就是 `KeyError: 0`。
    """

    body = _ok_body()
    assert body["object"] == "list"
    assert isinstance(body["data"], dict)

    embedding = EmbeddingClient(
        transport=_RecordingTransport(body=body), binding=_binding()
    ).embed_one("请问你们这个产品怎么收费？", timeout_seconds=5.0)

    assert len(embedding.vector) == 2048
    assert embedding.dimension == 2048
    assert embedding.model_id == "doubao-embedding-vision-251215"
    assert embedding.prompt_tokens == 26


def test_a_data_array_is_refused_rather_than_quietly_indexed() -> None:
    """**反面**：如果供应商哪天改成数组，要红，而不是静默取错。

    没有这一条，上面那条用例只证明了「今天这个形状能读」，
    而不是「别的形状会被拒」——那是一个**只会在正确输入上绿**的读法。
    """

    body = _ok_body()
    body["data"] = [{"embedding": [0.01] * 2048}]

    with pytest.raises(ProviderMalformedOutput, match="expected an object"):
        EmbeddingClient(transport=_RecordingTransport(body=body), binding=_binding()).embed_one(
            "一段话", timeout_seconds=5.0
        )


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda b: b.__setitem__("data", {}), "no vector at all"),
        (lambda b: b["data"].__setitem__("embedding", []), "an empty vector"),
        (lambda b: b["data"].__setitem__("embedding", "0.1,0.2"), "a string, not a list"),
        (lambda b: b["data"].__setitem__("embedding", [None] * 2048), "non-numeric elements"),
    ],
)
def test_a_malformed_vector_is_refused(mutate: Any, why: str) -> None:
    body = _ok_body()
    mutate(body)
    with pytest.raises(ProviderMalformedOutput):
        EmbeddingClient(transport=_RecordingTransport(body=body), binding=_binding()).embed_one(
            "一段话", timeout_seconds=5.0
        )
    assert why


def test_a_wrong_dimension_is_its_own_error_because_retrying_cannot_help() -> None:
    """维度不对 = **模型换了**，而那要新建 release 并重建索引（RFC `:728`）。

    **单独的类型，不是同一个 `ProviderMalformedOutput`**：坏响应重试可能有用，
    这一种重试一万次都是同一个维度。调用方要能把两者分开处置，
    否则「换模型」会被当成「网络抖了一下」，重试到预算耗尽为止。
    """

    with pytest.raises(EmbeddingDimensionMismatch, match="new KnowledgeRelease"):
        EmbeddingClient(
            transport=_RecordingTransport(body=_ok_body(dimension=1024)), binding=_binding()
        ).embed_one("一段话", timeout_seconds=5.0)


def test_a_non_200_is_raised_with_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """打错端点时供应商回的就是 400，见模块 docstring 第 1 条。"""

    transport = _RecordingTransport(body={"error": {"code": "InvalidParameter"}}, status_code=400)
    with pytest.raises(ProviderHttpError, match="400"):
        EmbeddingClient(transport=transport, binding=_binding()).embed_one(
            "一段话", timeout_seconds=5.0
        )


def test_n_chunks_are_n_round_trips_and_that_is_the_documented_cost() -> None:
    """**代价写成用例，因为第 5 批要拿它做决定。**

    「改一条资料要不要重嵌整批」这个问题的输入就是这一条：
    N 条 chunk 是 N 次往返，55 条就是 55 次。
    它会在有人把这个端点换成真批量接口的那天变红 —— 那时该重读的是成本假设。
    """

    transport = _RecordingTransport(body=_ok_body())
    client = EmbeddingClient(transport=transport, binding=_binding())
    for index in range(5):
        client.embed_one(f"第 {index} 段内容", timeout_seconds=5.0)

    assert len(transport.calls) == 5
    for call in transport.calls:
        assert len(call["payload"]["input"]) == 1
