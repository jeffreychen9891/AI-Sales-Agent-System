"""向量化：调豆包的多模态嵌入端点，**一次一条**。

**这个模块的形状不是照着 OpenAI 的 embeddings API 写的，是照着实测写的。**
`evidence/knowledge/embedding-dimension-2026-09-11.md` 记了那次真实调用，
三条与「按 OpenAI 兼容接口写」完全不同的事实：

1. **`/embeddings` 直接 400**：

       {"error":{"code":"InvalidParameter","message":"the requested model
        doubao-embedding-vision-251215 does not support this api..", …}}

   **只有 `/embeddings/multimodal` 答。** 所以本模块不复用
   `OpenAICompatibleChatClient` 的路径拼法，也不该有人把它「统一」回去；

2. **请求的 `input` 是 `[{"type": "text", "text": …}]`**，不是字符串数组；

3. **响应的 `data` 是一个对象，不是数组** —— 而同一份响应里的 `object`
   字段写着 `"list"`。**照着 `object` 的字面意思写 `data[0]` 会当场 `KeyError`。**

## 最要紧的那一条：`input` **不是批量轴**

**两段文本送进去，回来一个揉在一起的向量**（实测：`prompt_tokens` 从 26 涨到 31
——两段文本之和——而只回了**一个** 2048 维向量）。

**这正是「多传一个不报错」那一类接口**，而本仓为它付过三次钱
（`AGENTS.md`；嵌入这一条就是当时的三个实例之一）。一个能收下两段文本、
返回一个向量的接口，**失败方向是安静的**：调用方以为拿到了两条内容各自的向量，
实际拿到的是一个谁都不代表的平均。检索会照常返回结果，**只是结果没有意义**。

所以 `embed_one` **只收一段文本**，而 `_request` 里那条 `assert` 是它的守卫：
**接口能做的事，和我们允许它做的事，不是同一件事。**

## 代价，写在明面上

**N 条 chunk = N 次 HTTP 调用。** 重建一次索引的成本按 N 线性增长，
55 条就是 55 次往返。这不是实现偷懒，是这个端点的形状 ——
写在这里是因为第 5 批要决定「改一条资料要不要重嵌整批」，
而那个决定的输入就是这句话。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastagent_v2.worker.providers import (
    ChatTransport,
    ProviderHttpError,
    ProviderMalformedOutput,
    ProviderTimeout,
    credential_for,
)

#: 端点路径。**写死 `multimodal`**，理由见模块 docstring 第 1 条。
_ENDPOINT = "/embeddings/multimodal"


class EmbeddingDimensionMismatch(ProviderMalformedOutput):
    """回来的向量维度与 release 声明的不一致。

    **单独一个类型，因为它的处置和「响应坏了」不同**：坏响应重试可能有用，
    维度不对说明**模型换了**，而那要新建一个 `KnowledgeRelease` 并重建索引
    （RFC `:728`）—— 重试一万次都是同一个维度。
    """


@dataclass(frozen=True, slots=True)
class EmbeddingBinding:
    """model policy release 里 `embedding` 那一块，取出用得上的几项。

    **它先前零读者** —— `release-2026.09.02-5.json` 里躺着，
    `model_policy.py` 只取 `roles`。本模块是它的第一个读者。
    """

    provider_id: str
    model_id: str
    api_base: str
    dimension: int
    distance_metric: str
    normalization: str
    truncation_policy: str

    @classmethod
    def from_release(cls, document: dict[str, Any]) -> EmbeddingBinding:
        block = document["embedding"]
        return cls(
            provider_id=str(block["provider_id"]),
            model_id=str(block["model_id"]),
            api_base=str(block["api_base"]).rstrip("/"),
            dimension=int(block["dimension"]),
            distance_metric=str(block["distance_metric"]),
            normalization=str(block["normalization"]),
            truncation_policy=str(block["truncation_policy"]),
        )


@dataclass(frozen=True, slots=True)
class Embedding:
    """一条 chunk 的向量，以及它是在哪个绑定下算出来的。

    `model_id` 与 `dimension` 跟着向量一起走，**不是为了好看**：
    `knowledge_releases` 要把它们冻结下来，而「这个向量是哪个模型算的」
    在换模型之后是唯一能把旧向量认出来的东西。
    """

    vector: tuple[float, ...]
    model_id: str
    dimension: int
    prompt_tokens: int


class EmbeddingClient:
    """唯一开这个端点的地方。

    **复用 `ChatTransport` 而不是另写一个 transport**：那个协议是
    「一次 JSON POST」，与聊天没有关系，而第二份实现意味着超时、
    错误分类、`post_json` 的签名各有两套。
    """

    def __init__(self, *, transport: ChatTransport, binding: EmbeddingBinding) -> None:
        self._transport = transport
        self._binding = binding

    def embed_one(self, text: str, *, timeout_seconds: float) -> Embedding:
        """一段文本 → 一个向量。**一次一条，这不是限制，是那个接口的事实。**"""

        if not text.strip():
            # 空正文没有向量可言，而送进去会拿回一个「空的意思」的向量——
            # 它在余弦相似度下与任何东西都有一个说得过去的分数。
            raise ValueError("refusing to embed empty text")

        body = self._request([{"type": "text", "text": text}], timeout_seconds=timeout_seconds)
        return self._read(body)

    def _request(
        self, input_items: list[dict[str, str]], *, timeout_seconds: float
    ) -> dict[str, Any]:
        # **守卫，不是断言风格问题。** 见模块 docstring：`input` 能收下多条，
        # 收下之后把它们揉成一个向量，而调用方看不出来。
        # 这条 `assert` 是「接口能做的」与「我们允许的」之间那道线。
        assert len(input_items) == 1, (
            f"the multimodal embeddings endpoint merges multiple inputs into one "
            f"vector; embed one chunk per call (got {len(input_items)})"
        )

        try:
            response = self._transport.post_json(
                url=f"{self._binding.api_base}{_ENDPOINT}",
                headers={
                    "Authorization": f"Bearer {credential_for(self._binding.provider_id)}",
                    "Content-Type": "application/json",
                },
                payload={"model": self._binding.model_id, "input": input_items},
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError as error:
            raise ProviderTimeout(str(error), role="EMBEDDING") from error

        if response.status_code != 200:
            # **400 在这里有一个具体的含义**，见 docstring 第 1 条：
            # 打错端点时供应商回的就是 400，而消息里点名的是 model 不是 url。
            raise ProviderHttpError(
                f"embeddings endpoint returned {response.status_code}", role="EMBEDDING"
            )
        return response.body

    def _read(self, body: dict[str, Any]) -> Embedding:
        # **`data` 是对象不是数组** —— 而同一份响应里 `object` 字段写着 "list"。
        # 这里不看 `object`，看 `data` 本身的类型：**照字面意思写 `data[0]`
        # 会 KeyError**，而那是实测里最容易被抄错的一处。
        data = body.get("data")
        if not isinstance(data, dict):
            raise ProviderMalformedOutput(
                f"embeddings response `data` is {type(data).__name__}, expected an object "
                "(the endpoint returns one object even though `object` says 'list')",
                role="EMBEDDING",
            )

        raw = data.get("embedding")
        if not isinstance(raw, list) or not raw:
            raise ProviderMalformedOutput(
                "embeddings response carries no vector", role="EMBEDDING"
            )
        try:
            vector = tuple(float(value) for value in raw)
        except (TypeError, ValueError) as error:
            raise ProviderMalformedOutput(
                "embeddings response vector is not numeric", role="EMBEDDING"
            ) from error

        if len(vector) != self._binding.dimension:
            # RFC `:728`：维度变化要新建 release 并重建索引。**在这里拒**，
            # 因为 `knowledge_chunks.embedding` 是 `vector(2048)`，
            # 写下去会在数据库那一层报一个读不懂的错。
            raise EmbeddingDimensionMismatch(
                f"embedding has {len(vector)} dimensions, release declares "
                f"{self._binding.dimension}; a model or dimension change needs a new "
                "KnowledgeRelease and a full index rebuild, not a retry",
                role="EMBEDDING",
            )

        usage = body.get("usage") or {}
        return Embedding(
            vector=vector,
            model_id=self._binding.model_id,
            dimension=len(vector),
            prompt_tokens=int(usage.get("total_tokens") or 0),
        )
