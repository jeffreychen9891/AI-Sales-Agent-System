"""那个补齐环境的 fixture：它补什么、不覆盖什么、以及它代偿掉的那条要求。

## 它为什么必须和 fixture 同一批

在这批之前，本机跑 `tests/contracts/` 固定 **9 条红**（`test_superuser_allowlist`
全部），成因是它们调 `Settings.from_env("api")` 而本机没有那几个变量。
**而「缺这几个变量时 `Settings.from_env` 必须报错」这条行为，全仓没有任何用例** ——
那 9 条红是它唯一的证据。

`conftest.py` 的 `api_role_settings_are_buildable` 把那 9 条变绿，
**于是那个唯一的证据也被拿掉了**：那条要求会从「本机每次红给你看」
变成没人验。本文件就是接手的那个人。

（`AGENTS.md`「一个长期存在的失败，可能正在代偿一条没有用例的要求」——
本批是那条纪律的第一个实例，而它是在做这件事的过程中被发现的。）

## 本文件是**独守的**，而这一点是量出来的

反向探针（2026-09-13）：

* `conftest.py` 那个 fixture 不再 `autouse` → **红 13 条**
  （9 条 `test_superuser_allowlist`，加本文件里要真 settings 的那几条）；
* fixture 改成「无条件覆盖」→ **红 2 条**
  （`test_an_existing_value_is_never_overridden` 与 `test_only_the_absent_ones_are_filled`）；
* **把本文件那条「缺密钥要报错」删掉 → 全绿**；
* **连「逐个缺席」那一组也删掉 → 仍然全绿**。

后两行是这份文件存在的全部理由：**删掉它，没有任何别的东西会说话。**
探针标签第一版写的是「该红」，那是**问错了问题** ——
删掉一条用例当然不会让别的用例变红；要问的是「还有别人看着吗」，
而**绿**就是那个问题的答案：没有。

**所以改动本文件之前先问「那条要求此后由谁守」，而不是「门禁还绿吗」。**
它守的不是一段代码的行为，是一条**曾经由环境缺陷代偿的要求** ——
这类要求没有第二个自然落点，因为在它被写下来之前，它根本不在任何人的清单上。
"""

from __future__ import annotations

import pytest
from fastagent_v2.runtime.settings import Settings, SettingsError

from tests.conftest import API_ROLE_DEFAULTS, api_role_env_defaults


def test_settings_still_refuses_to_start_without_the_signing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**这一条接手了那 9 条红原本在证明的事。**

    签名密钥缺席时，`api` 角色**必须建不起来**。它不是一条洁癖：
    那个密钥签的是访问令牌，一个用空串或默认值签发的令牌，
    任何人都能自己造一个。所以这里要的是**拒绝启动**，不是「回退到某个默认」。

    用 `delenv` 而不是新起一个进程：本文件跑在 fixture 已经生效的会话里，
    而 `monkeypatch` 的还原是按用例的 —— 出了这个函数，环境回到 fixture 给的样子。
    """

    monkeypatch.delenv("FASTAGENT_ACCESS_SIGNING_KEY", raising=False)

    with pytest.raises(SettingsError, match="FASTAGENT_ACCESS_SIGNING_KEY"):
        Settings.from_env("api")


@pytest.mark.parametrize("name", sorted(API_ROLE_DEFAULTS))
def test_settings_refuses_to_start_without_any_one_of_them(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**四个变量逐个删一遍，每一个缺席都要拒绝启动。**

    只验签名密钥那一个是不够的：这个 fixture 补的是**一整个集合**，
    而「这个集合恰好等于 `from_env("api")` 的必填集」是它唯一的立足点。
    少验一个，那一项就可以在某天被从必填里去掉而没人发现 ——
    **而它一旦不再必填，fixture 给它的那个值就成了一个没人要求的默认值。**

    这一条也顺带钉住了反向：**fixture 不许比必填集大**。
    多补一个非必填的变量，它就在替某个角色悄悄做决定，
    而那个决定不会出现在任何 diff 里。
    """

    monkeypatch.delenv(name, raising=False)

    with pytest.raises(SettingsError, match=name):
        Settings.from_env("api")


def test_the_defaults_are_exactly_enough_to_build_the_api_role() -> None:
    """**补完这四个，`api` 角色就建得出来** —— 不多不少的「不少」那一半。

    上面那组验的是「少一个就不行」，这一条验的是「四个齐了就行」。
    两边合起来才说明 `API_ROLE_DEFAULTS` 与 `from_env("api")` 的必填集是同一个集合；
    只有前者的话，一个**漏了一项**的 fixture 也能全绿
    （它会把本机的 9 条红换成 9 条别的红）。
    """

    settings = Settings.from_env("api")

    assert settings.access_signing_key, "签名密钥没进 settings"


def test_an_existing_value_is_never_overridden() -> None:
    """**门禁里那一半：已经有值时，一个都不动。**

    这是这条机制能不能算数的全部。「被绕过的东西在门禁里必须仍然跑」那条约束
    不是靠人记得配对，而是靠这个判断：`ci.sh` / `ci.ps1` 都 export 了真值，
    于是这里回空字典，fixture 什么都不做，**门禁跑的仍是门禁给的那些值**。

    **用纯函数驱动，而不是驱动 fixture 本身**：那个 fixture 是 session 作用域、
    一轮只跑一次，用它没法在一次跑里同时验「缺席时补」与「已有时不动」。
    """

    present = {name: "already-set-by-the-gate" for name in API_ROLE_DEFAULTS}

    assert api_role_env_defaults(present) == {}


def test_only_the_absent_ones_are_filled() -> None:
    """部分缺席时，只补缺的那些。

    一个「要么全补要么全不补」的实现也能让上面两条通过，
    而它在**门禁少给一个变量**时会把另外三个真值一起冲掉 ——
    那种偏移在本机看不见，只在门禁里看得见，而那时故障已经在门禁上。
    """

    one_missing = {name: "real" for name in API_ROLE_DEFAULTS}
    del one_missing["FASTAGENT_CACHE_REDIS_URL"]

    filled = api_role_env_defaults(one_missing)

    assert sorted(filled) == ["FASTAGENT_CACHE_REDIS_URL"]


def test_an_empty_string_counts_as_absent() -> None:
    """空串算缺席 —— 与 `Settings` 自己的判据一致。

    `_required` 把空串也当缺失（它检的是真值性），所以 fixture 不能只看键在不在：
    **两处对「有值」的定义必须是同一个**，否则会出现「fixture 认为有、
    settings 认为没有」的一轮，而那一轮的报错指向 settings，
    而真正该改的是那个空串是怎么来的。
    """

    assert api_role_env_defaults({name: "" for name in API_ROLE_DEFAULTS}) != {}
    assert sorted(api_role_env_defaults({name: "" for name in API_ROLE_DEFAULTS})) == sorted(
        API_ROLE_DEFAULTS
    )


def test_the_signing_key_is_generated_not_hard_coded() -> None:
    """密钥每次生成一个新的，**不是仓里写死的一个常量**。

    一个写死在仓里的测试密钥，早晚会有人拿去别处用 —— 而那一刻它就不是测试密钥了。
    两次调用给出不同的值，是「它是生成的」最直接的证据。
    """

    first = api_role_env_defaults({})["FASTAGENT_ACCESS_SIGNING_KEY"]
    second = api_role_env_defaults({})["FASTAGENT_ACCESS_SIGNING_KEY"]

    assert first != second
    # `Settings` 要求至少 32 字节；`token_urlsafe(36)` 给的更长，这里钉住下界。
    assert len(first.encode()) >= 32


def test_the_two_connection_urls_name_the_fixture_in_their_host() -> None:
    """两个连接串用**自报身份的哨兵地址**，不是像真的那种。

    给连接串假值的风险是把「你没有配置」变成「你没有数据库」，
    而后者报得更晚、更难读。哨兵地址让那种情形自证：
    谁真去连它，报错里就有 `fixture-not-a-real-host`，一眼看出是谁给的。

    **写成断言而不是注释**，因为下一个人很可能觉得
    `postgresql://localhost:5432/fastagent` 更「正常」—— 而那正是会让人
    调半小时本机 Postgres 的那一个。
    """

    for name in ("FASTAGENT_DATABASE_URL", "FASTAGENT_CACHE_REDIS_URL"):
        assert "fixture-not-a-real-host" in API_ROLE_DEFAULTS[name], (
            f"{name} 的默认值不再自报身份 —— 谁连上它都会以为是自己的环境坏了"
        )


# **这里原本还有两条，删掉了，理由留在这里**（`AGENTS.md`「删自己写的东西也要留一句理由」）：
#
# * `test_the_generated_key_is_the_only_runtime_value` —— 它断言「表里只有密钥那项
#   是 `None`」，量的是表的形状而不是任何行为。表长什么样，读表的人看一眼就知道；
#   而它会在「第二项也要生成」那天红，而那天该做的是给表换个形状，不是改这条断言。
# * `test_a_generated_key_is_not_reused_across_names` —— 它今天**恒真**（只有一项生成），
#   是替一个还不存在的情形写的；而它最后那句
#   `secrets.token_urlsafe(36) != filled[...]` 被我写成了「证明用的是真模块」，
#   **那句话什么也没证明**（两次随机取值不同是必然的，与用哪个模块无关）。
#
# 上面留下的八条都驱动真实行为：四个变量逐个缺席都要拒绝启动、齐了就建得出来、
# 已有值不覆盖、只补缺的、空串算缺席、密钥是生成的、两个连接串自报身份。
