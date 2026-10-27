"""「用例不许连外网」这道闸**今天真的在拦**，而不是只被写在某个 docstring 里。

## 它补的那个洞

`worker/providers.py` 的模块 docstring 第 4 行写着

> nothing in the test suite is allowed to open a socket

而在本文件之前，`requirements-dev.in` 里**没有任何东西执行它**（七行全读过）。
按 `AGENTS.md` 开头那张表，这是**五条判据的第 1 类**：
「约束声明了，运行期无人执行」—— 而它犯在那条约束自己的模块里。

## 为什么这两条用例要**真的去连一次**

一条「检查 `socket.socket.connect` 被打过补丁」的断言，在 pytest-socket 换了
实现之后会继续绿（`AGENTS.md`「守卫本身也可能是空的」）。
所以这里按行为验：**一个外网地址连不上、一个回环地址连得上**。

**两侧都要**（`AGENTS.md`「放宽一条判据时，正反两侧都要验」）：
只验前一半时，一个把所有 socket 都禁掉的配置也能让它绿 ——
而那会让整个 `integration` / `e2e` 层连不上本机的 PostgreSQL 与 Redis。
后一半就是在验「我放行的那一种仍然过得去」。
"""

from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketConnectBlockedError

#: RFC 5737 §3 的 TEST-NET-3，**保留给文档用，不会被路由**。
#:
#: 挑一个保留段而不是挑一个真实域名，有两个理由：
#: ① 闸真的失效时这条用例的失败形态是「0.25 秒后超时」而不是
#:    「悄悄连上了某个第三方」；
#: ② 它不需要 DNS —— 一个字面量 IP 的 `getaddrinfo` 不出本机，
#:    于是这条用例在断网的机器上与在联网的机器上行为相同。
UNROUTABLE_ADDRESS = ("203.0.113.1", 80)

#: 够短，使得闸失效时这条用例是「红得很快」而不是「挂住」。
PROBE_TIMEOUT_SECONDS = 0.25


def test_a_test_cannot_reach_a_non_loopback_address() -> None:
    """外网那一侧：拦下来，而且**报错里要有被拦的那个主机**。

    断言异常类型而不只断言「抛了」：`AGENTS.md`「红了之后，要确认红的是你要
    测的那一条」—— 一条只写 `pytest.raises(OSError)` 的用例在
    「闸没装、网络不通」时**也会通过**，而那两件事要修的地方完全不同。
    """

    with pytest.raises(SocketConnectBlockedError) as blocked:
        socket.create_connection(UNROUTABLE_ADDRESS, timeout=PROBE_TIMEOUT_SECONDS)

    # 报错要指向「是谁被拦了」，否则读到它的人不知道该去改哪一行。
    assert UNROUTABLE_ADDRESS[0] in str(blocked.value)


def test_loopback_is_still_reachable() -> None:
    """回环那一侧：**仍然通**，否则整个要栈的那一层会连不上库。

    这条用例自己起一个监听者，而不是去连某个「应该在跑」的东西 ——
    那样它会在没起栈的机器上红，而它要验的事与栈在不在无关。
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(
            listener.getsockname(), timeout=PROBE_TIMEOUT_SECONDS
        ) as client:
            assert client.fileno() != -1


def test_the_guard_is_configured_where_the_comment_says_it_is(
    pytestconfig: pytest.Config,
) -> None:
    """上面两条验行为，这一条**点名该去哪改** —— 两者挡的不是同一件事。

    有人把 `--allow-hosts` 从 `addopts` 里删掉时，上面两条会红，
    而红的样子是「连得上外网了」，**不会说出配置在哪一行**。
    这一条红的时候说的是那一行。
    （`AGENTS.md`「一个条件如果在别处会被报成另一件事，
    就在它自己那一处先拦一次」。）
    """

    allowed = pytestconfig.getoption("--allow-hosts")
    assert allowed, (
        "pytest-socket is installed but no host allow-list is configured, so "
        "`socket_allow_hosts` returns without patching anything and this gate is "
        "off. Restore `--allow-hosts=127.0.0.1,::1,localhost` in "
        "`[tool.pytest.ini_options] addopts` in pyproject.toml."
    )
    # 回环那三个值都要在：少了 `localhost` 时按主机名连本机库的那些用例会红，
    # 而那条红看起来像「库挂了」。
    assert {"127.0.0.1", "::1", "localhost"} <= set(str(allowed).split(","))
