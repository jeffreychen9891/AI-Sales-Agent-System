"""T362：大文件那一档**算得出来，而没有任何代码去走它**。

## 今天这条路是安静的

`provider_file_type()` 在 20MB 两侧真的给出不同的值（`4` / `22`，
一条用例钉着），**而 `22` 之后走的还是唯一那个端点** ——
`upload_file_by_url` → `/cloud/cdnUploadByUrlAsync`。
`upload_big_file_multipart`（`/cloud/cdnBigFileUpload`）**全仓 0 个实现**。

于是：算得出 `22` · 发得出去 · 等一个永远不会成功的回调。
**没有任何东西会变红。**

运维 2026-09-17 实测那一次，**供应商把整份 48MB 拉走了才拒**
（access.log 有 `200 48219856`），我们这边只看到
`500100 文件上传限制:20MB`。**那一次是我们让它白拉的。**

## 本批做的不是实现那个端点

用户 2026-09-18 裁定**不实现** `cdnBigFileUpload`（演示改用 ≤20MB 的片子）。
**换片是把那一次的路让开，不是把这一格关上** —— 下一份 >20MB 的文件
还会撞同一堵墙。本批让走错的那条路**当场说话**。

## 它自己写着什么时候该被删

`test_the_endpoint_still_has_no_implementation` 红的那一天，
就是有人实现了那个端点的那一天 —— **那时这道拒绝连同它两个调用点一起删。**
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
IMPORT_SCRIPT = ROOT / "scripts" / "import_materials.py"
UPLOAD_SCRIPT = ROOT / "scripts" / "upload_materials.py"


def _module() -> Any:
    if "import_materials" in sys.modules:
        return sys.modules["import_materials"]
    spec = importlib.util.spec_from_file_location("import_materials", IMPORT_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["import_materials"] = module
    spec.loader.exec_module(module)
    return module


def _entry(**overrides: Any) -> dict[str, Any]:
    entry = {
        "slug": "product-demo-video",
        "version": 1,
        "filename": "product-demo-video.mp4",
        "media_type": "video/mp4",
        "source_url": "https://assets.example.test/assets/product-demo-video-v1.mp4",
    }
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# 一 · 那一档被拒，而别的档不受影响
# ---------------------------------------------------------------------------


def test_only_the_tier_without_an_endpoint_is_refused() -> None:
    """**只拒 `22`。** 图片、文件、20MB 以内的视频一条都不该被拦。

    这是本文件最该被反向探针打的一条：一道多拒一档的闸，
    会让整批资料传不上去 —— 而它与「该拒的拒了」在绿色上长得一模一样。
    """

    module = _module()

    assert module.unsupported_tier_refusal(module.BIG_VIDEO_FILE_TYPE) is not None
    for supported in (1, 4, 5):
        assert module.unsupported_tier_refusal(supported) is None, (
            f"fileType {supported} 今天走得通，不该被拦"
        )


def test_the_refusal_says_why_and_where_to_look() -> None:
    """拒绝要写清**为什么**与**下一步去哪看** —— 读它的是整理资料的人。"""

    module = _module()
    text = module.unsupported_tier_refusal(module.BIG_VIDEO_FILE_TYPE) or ""

    assert "cdnBigFileUpload" in text, "没说清缺的是哪个端点"
    assert "500100" in text, "没说清照现在这条路传出去会撞上什么"
    assert "T362" in text, "没说下一步去哪看"


# ---------------------------------------------------------------------------
# 二 · 导入那一道：门槛两侧
# ---------------------------------------------------------------------------


def test_a_video_over_the_threshold_is_refused_at_import() -> None:
    """**目录自己报了大小的，在导入就拒。**

    与 `media_type` 那条判据同一个理由（那条注释写着）：
    「导入时就要判『这个 media_type 供应商收不收』，否则一份收不了的资料
    会一路躺到发送那一刻才失败 —— 而那时客户已经在等了。」
    **大小是同一个问题的另一半。**
    """

    module = _module()
    over = module.entry_refusals(
        _entry(byte_size=module.BIG_VIDEO_THRESHOLD_BYTES + 1)
    )

    assert any("cdnBigFileUpload" in line for line in over), f"没拒：{over}"


def test_exactly_at_the_threshold_still_goes_through() -> None:
    """**门槛上那一份仍然走得通** —— `provider_file_type` 的判据是「大于」。

    差一个字节的两侧各钉一条：一道写成 `>=` 的闸会把刚好 20MB 的文件也拦下，
    而那与「拦对了」在绿色上分不出来。
    """

    module = _module()
    at = module.entry_refusals(_entry(byte_size=module.BIG_VIDEO_THRESHOLD_BYTES))

    assert not any("cdnBigFileUpload" in line for line in at), f"门槛上这份被误拦：{at}"


def test_a_big_image_is_not_the_big_video_tier() -> None:
    """**只有视频那一类按大小换档。** 一张 30MB 的图片仍然是 `1`。

    这一条防的是把大小判据写成「与 media_type 无关」——
    那样每一份大图都会被这道闸拦下，而它们今天传得上去。
    """

    module = _module()
    found = module.entry_refusals(
        _entry(
            slug="handoff-flow-diagram",
            filename="handoff-flow-diagram.jpg",
            media_type="image/jpeg",
            byte_size=module.BIG_VIDEO_THRESHOLD_BYTES * 2,
            source_url="https://assets.example.test/assets/handoff-flow-diagram-v1.jpg",
        )
    )

    assert not any("cdnBigFileUpload" in line for line in found), f"大图被误拦：{found}"


class _Connection:
    """够 `import_entries` 用的那点连接：查得到「已经有没有」，记得下 INSERT。"""

    def __init__(self) -> None:
        self.inserts: list[str] = []

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> Any:
        if statement.strip().startswith("INSERT"):
            self.inserts.append(str(parameters[2]))
        return self

    def fetchone(self) -> None:
        return None  # 这条 slug/version 还没有


def test_the_size_is_judged_again_after_it_is_measured(monkeypatch: Any) -> None:
    """**目录没报大小的那些，量完再判一次 —— 而这一条真的跑一遍导入。**

    `entry_refusals` 只看得见目录里写着的数；真身只有 `measure()` 之后才知道。
    少了这一道，一份不报 `byte_size` 的大视频照样进得了库。

    ⚠️ **第一版是一条源码扫描**（「`import_entries` 的正文里出现过
    `unsupported_tier_refusal`，且在 `measure` 之后」）——
    反向探针 `D` 把那个 `if` 改成 `if False:` 之后，**那两句话仍然全对，用例全绿**。
    一条只看得见「这行代码在不在」的守卫，看不见「它干不干活」。
    """

    module = _module()
    monkeypatch.setattr(
        module,
        "measure",
        lambda url: (module.BIG_VIDEO_THRESHOLD_BYTES + 1, "0" * 64),  # noqa: ARG005
    )
    connection = _Connection()

    counts = module.import_entries(
        connection,
        tenant_id=uuid4(),
        entries=[_entry()],  # 刻意不带 byte_size / content_hash
    )

    assert connection.inserts == [], "一份传不上去的大视频进了库"
    assert counts["refused"] == 1, f"没数出来：{counts}"
    assert counts["inserted"] == 0


def test_import_exits_non_zero_when_it_refused() -> None:
    """**印了却仍然回 0，等于没拒** —— 读退出码的人看不到正文。"""

    source = IMPORT_SCRIPT.read_text(encoding="utf-8")

    assert 'return 1 if counts["refused"] else 0' in source


# ---------------------------------------------------------------------------
# 三 · 上传那一道：在打电话给供应商**之前**
# ---------------------------------------------------------------------------


def test_the_upload_script_refuses_before_it_calls_the_provider() -> None:
    """**顺序就是这一条的全部。**

    拦在调用之后也能记一条失败，**而供应商已经把整份文件拉完了** ——
    运维 09-17 实测：`200 48219856`，48MB 走完才回
    `500100 文件上传限制:20MB`。
    """

    source = UPLOAD_SCRIPT.read_text(encoding="utf-8")
    loop = source[source.index("for row, file_type in plan:\n            name") :]

    assert "unsupported_tier_refusal(" in loop, "真跑那一圈里没有这道闸"
    assert loop.index("unsupported_tier_refusal(") < loop.index(
        "client.upload_file_by_url("
    ), "拦在调用之后 —— 那时文件已经被拉完了"


def test_the_dry_run_marks_them_and_exits_non_zero() -> None:
    """dry-run 的全部用处是「真跑之前先看一眼」。

    一行看起来正常的 `WOULD … → fileType 22` 读起来就是「这份会传上去」，
    而它不会。
    """

    source = UPLOAD_SCRIPT.read_text(encoding="utf-8")

    assert "BLOCKED" in source, "清单里分不出哪几份走不了"
    assert "return 1 if unsupported else 0" in source, "dry-run 看见了却仍然回 0"


# ---------------------------------------------------------------------------
# 四 · 这道拒绝什么时候该被删
# ---------------------------------------------------------------------------


def test_the_endpoint_still_has_no_implementation() -> None:
    """**这道拒绝立在「那个端点不存在」这个事实上。**

    它红的那一天，就是有人实现了 `cdnBigFileUpload` 的那一天 ——
    **那时这道拒绝连同它两个调用点一起删**，而不是让一份传得上去的文件
    被一条陈旧的拒绝挡住。

    按 `CLAUDE.md` 第二条：一条「某物今天不存在」的断言，配一条会在它失效时变红的守卫。
    """

    hits = [
        path
        for folder in ("packages", "services", "scripts")
        for path in (ROOT / folder).rglob("*.py")
        if "upload_big_file_multipart" in path.read_text(encoding="utf-8")
        and "def upload_big_file_multipart" in path.read_text(encoding="utf-8")
    ]

    assert hits == [], (
        f"`cdnBigFileUpload` 有实现了（{[str(p) for p in hits]}）—— "
        "那么 `unsupported_tier_refusal` 与它的两个调用点该删了，见 T362"
    )


def test_the_tier_is_still_computable_so_the_refusal_is_not_dead_code() -> None:
    """**另一头**：`provider_file_type` 仍然返回得出 `22`。

    它哪天不再返回那个值了，这道拒绝就成了零触发的代码 ——
    那时要删的是拒绝，不是让它继续挂着。
    """

    module = _module()

    assert (
        module.provider_file_type("video/mp4", module.BIG_VIDEO_THRESHOLD_BYTES + 1)
        == module.BIG_VIDEO_FILE_TYPE
    )
    assert module.provider_file_type("video/mp4", 1024) == 4
