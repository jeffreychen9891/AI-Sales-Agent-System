"""文档让人怎么调 pytest，那个调法就必须能跑。

**这条存在是因为「门禁怎么调」与「文档让人怎么调」是两个事实，
而在这条用例之前只有前一个被验过。**

两个门禁脚本都写 `python -m pytest`，而 `-m` 会把当前目录塞进 `sys.path`。
源仓里有 78 个测试模块写 `from tests.<x> import ...`，那个写法要求仓根在
`sys.path` 上 —— 于是它一直被满足，**而满足它的是调用法，不是任何配置**。
那个依赖因此从来没有在任何一次绿里露过头。

⚠️ 代价是读者付的（2026-10-27 实测，在导出树 `AI-Sales-Agent-System-b4` 里）：
作品集 README 的第一条命令是 `pytest tests/contracts -q`（控制台脚本，不走
`-m`），照抄跑是 **12 errors during collection**；
`pytest tests/integration tests/e2e tests/security -q` 是 **65 errors**。
而那条命令旁边写着「实测产出 2102 passed, 53 skipped」——
**那个数是真的，只是它是另一条命令跑出来的。**

**为什么不去调 `pytest.exe`**：那要求控制台脚本装在一个找得到的位置，
而找不到时唯一的出路是 skip —— 一条会 skip 的守卫和一条不存在的守卫
在日志里长得一样（`AGENTS.md`「不静默跳过」）。
`-P` 复刻的是**那条命令之所以会红的那个机制本身**：
它关掉「把脚本目录 / 当前目录放进 sys.path」这件事，也就是控制台脚本的处境，
而用的是手上这个解释器。实测两个方向都验过：
`pyproject.toml` 的 `pythonpath` 去掉 `"."` 之后这条就红。
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 挑一个**真的**写了 `from tests.<x> import ...` 的模块，而且只采集不跑 ——
#: 这条要验的是 import 解不解得开，不是那些用例过不过（它们要一套栈）。
#: 固定一个坐标而不是让 pytest 扫全树：这条要在**毫秒级**里给出答案，
#: 否则它迟早会被人从门禁里拿掉。
A_MODULE_THAT_IMPORTS_THE_TESTS_PACKAGE = "tests/e2e/test_retry_budget.py"


def test_the_tests_package_import_does_not_depend_on_how_pytest_was_invoked() -> None:
    """`-P`（不把 cwd 放进 sys.path）下也要采集得开。"""

    source = (ROOT / A_MODULE_THAT_IMPORTS_THE_TESTS_PACKAGE).read_text(
        encoding="utf-8"
    )
    assert "from tests." in source, (
        f"{A_MODULE_THAT_IMPORTS_THE_TESTS_PACKAGE} no longer imports through the "
        "`tests.` package, so this case stopped testing what it says it tests -- "
        "point it at a module that still does, do not delete it"
    )

    completed = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
        [
            sys.executable,
            # **这一句就是本条的全部**：关掉 `-m` 顺手做的那件事，
            # 于是进程的处境与 `pytest ...` 控制台脚本一致。
            "-P",
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            A_MODULE_THAT_IMPORTS_THE_TESTS_PACKAGE,
        ],
        cwd=ROOT,
        capture_output=True,
        # 门禁输出里有中文；不指定就按本机 cp1252 解码，
        # 而那个失败的样子是 `stdout is None`（见 test_ci_working_directory 的注记）。
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, (
        "`pytest` invoked the way the documentation tells a reader to invoke it "
        "cannot even collect a module that imports through the `tests.` package. "
        "The fix is `pythonpath` in pyproject.toml, not a different command in "
        f"the README:\n{completed.stdout}\n{completed.stderr}"
    )


def test_the_repository_root_is_on_the_pytest_path() -> None:
    """上一条的配置侧：仓根在 `pythonpath` 里，**且这张表是显式的**。

    两条并排的理由与 `WITHHELD_READ_SCOPE` 那张表同源：
    上一条验的是「今天能不能跑」，这一条让**那张表本身**被人看见，
    于是有人删掉 `"."` 时报错里直接写着删了什么，
    而不是只看到 65 个互不相关的 ImportError。
    """

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pythonpath = config["tool"]["pytest"]["ini_options"]["pythonpath"]
    assert "." in pythonpath, (
        "the repository root must be on pytest's pythonpath: 78 test modules "
        "import through the `tests.` package, and without this they resolve only "
        "when pytest is invoked as `python -m pytest`, which is not what the "
        f"documentation tells a reader to run. pythonpath is {pythonpath}"
    )
