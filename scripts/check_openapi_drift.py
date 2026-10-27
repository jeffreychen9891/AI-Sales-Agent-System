"""仓里那份 `openapi.json` 必须就是今天的 app 导出来的那一份。

## 它守什么

`docs/rfcs/control-plane-parity.md` §4.3 把 OpenAPI 定成 HTTP 合同的权威，
而前端的 TypeScript client 是**从它生成的**。于是这份产物一旦落后于 app，
**前端会按一份过期的合同去调一个已经改了的后端**——而那个错是安静的：
类型检查全绿（它查的是产物），请求发出去才 4xx / 422。

## 判据：重新导出一次，逐字节比

**不比语义。** 一个语义比对器要自己回答「什么算等价」，
而本仓记过那类自造校验器——它只证明它自己那一层
（`memory`：熵解码器只解第一个分量，12 条全绿而供应商拒）。
「重新生成一次会不会得到同一份文件」这个判据不需要任何人定义等价，
而 `dump_openapi.render()` 的 `sort_keys=True` + 固定缩进保证它可重放
（实测：连跑两次逐字节相同）。

## 它不守什么

* **不守 `WS /ws`。** FastAPI 不把 websocket 写进 OpenAPI，所以这份产物里
  **没有** `/ws`（实测 `grep -c '"/ws"'` → 0）。实时层的合同要靠别的东西钉，
  今天靠的是 `POST /ws/tickets` 的 `topics` 校验与那条集成用例。
  **写在这里是因为「client 由 OpenAPI 生成」这句话对 WS 不成立**，
  而下一个人很容易以为它成立。
* **不守产物与 `route-manifest.json` 一致。** 那是另一条闸的职责
  （两份产物各自与 app 比，而不是互相比）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dump_openapi import ARTIFACT, render  # noqa: E402


def main() -> int:
    if not ARTIFACT.exists():
        print(
            f"openapi drift: FAIL -- {ARTIFACT} 不存在。\n"
            "  生成它：python scripts/dump_openapi.py",
            file=sys.stderr,
        )
        return 1

    committed = ARTIFACT.read_text(encoding="utf-8")
    current = render()
    if committed == current:
        paths = committed.count('\n    "/')
        print(f"openapi drift: PASS ({paths} paths)")
        return 0

    # **报差在哪一族，不打印整份 diff。** 这份文件 4000 行，
    # 一个整份 diff 在门禁日志里等于没有报告。
    committed_lines = committed.splitlines()
    current_lines = current.splitlines()
    first = next(
        (
            i
            for i, (a, b) in enumerate(zip(committed_lines, current_lines, strict=False))
            if a != b
        ),
        min(len(committed_lines), len(current_lines)),
    )
    print(
        "openapi drift: FAIL -- 仓里那份与今天的 app 不一致。\n"
        f"  首个不同在第 {first + 1} 行：\n"
        f"    仓里: {committed_lines[first] if first < len(committed_lines) else '<文件更短>'}\n"
        f"    现在: {current_lines[first] if first < len(current_lines) else '<文件更短>'}\n"
        f"  行数 仓里 {len(committed_lines)} / 现在 {len(current_lines)}\n"
        "  改了 route 就重新导出并把产物一起提交：\n"
        "    python scripts/dump_openapi.py",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
