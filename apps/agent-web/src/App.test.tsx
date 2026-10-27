import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from './App';
import { __resetRefreshStateForTests } from './api/client';
import { session } from './api/session';

/**
 * A 面的接线：登录 → （也许）改密 → 工作台。
 *
 * ## 这几条各自钉住什么
 *
 * 最要紧的是 **T130 那一条**：登录响应说 `must_change_password: true` 时，
 * 人必须落在改密页而**不是**工作台。在那个字段存在之前这件事做不到 ——
 * 前端只能先把人放进工作台，等他撞上任意一条 route 的 403 再退回来。
 * 所以这条用例同时是 T130 的验收：**把后端那个字段拿掉，它就红。**
 *
 * ## 为什么替身打在 `fetch` 上而不是 `api`
 *
 * 打在 `api` 上会把 `client.ts` 整层（授权头、401 之后的单飞刷新、重试）
 * 一起绕过去 —— 那一层正是 parity §4.3 要求「只有一处」的东西，
 * 绕过它的用例证明不了接线是通的。打在 `fetch` 上，请求真的穿过那一层。
 */

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

async function signIn(): Promise<void> {
  await userEvent.type(screen.getByLabelText('商家编号'), 'acme');
  await userEvent.type(screen.getByLabelText('邮箱'), 'operator@example.com');
  await userEvent.type(screen.getByLabelText('密码'), 'pw');
  await userEvent.click(screen.getByRole('button', { name: '登录' }));
}

describe('A 面接线', () => {
  beforeEach(() => {
    session.clear();
    __resetRefreshStateForTests();
  });

  it('登录成功且不欠改密时，落在工作台', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse(200, {
        access_token: 'at',
        token_type: 'bearer',
        expires_in: 900,
        must_change_password: false,
      }),
    );

    render(<App />);
    await signIn();

    await waitFor(() => expect(screen.getByText('角色与权限')).toBeInTheDocument());
  });

  it('T130：登录说欠一次改密时，落在改密页而不是工作台', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse(200, {
        access_token: 'at',
        token_type: 'bearer',
        expires_in: 900,
        must_change_password: true,
      }),
    );

    render(<App />);
    await signIn();

    await waitFor(() => expect(screen.getByText('设置新密码')).toBeInTheDocument());
    // **两件都要断言。** 只断言「改密页在」的那一半，会让一个把两页
    // 同时渲染出来的实现照样绿。
    // 「角色与权限」是壳里独有的字样 —— 上一版断言的「已登录」来自那个
    // 占位组件，而它已经被真的壳换掉了。
    expect(screen.queryByText('角色与权限')).not.toBeInTheDocument();
  });

  it('登录失败时显示信封里的 message，并留在登录页', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse(401, {
        code: 'AUTHENTICATION_REQUIRED',
        message: '邮箱或密码不正确',
        request_id: 'r-1',
        field_errors: [],
        retryable: false,
      }),
    );

    render(<App />);
    await signIn();

    await waitFor(() => expect(screen.getByText('邮箱或密码不正确')).toBeInTheDocument());
    expect(screen.queryByText('角色与权限')).not.toBeInTheDocument();
  });

  it('改密成功（204）之后放行到工作台', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch');
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        access_token: 'at',
        token_type: 'bearer',
        expires_in: 900,
        must_change_password: true,
      }),
    );
    render(<App />);
    await signIn();
    await waitFor(() => expect(screen.getByText('设置新密码')).toBeInTheDocument());

    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await userEvent.type(screen.getByLabelText('当前密码'), 'old');
    await userEvent.type(screen.getByLabelText('新密码'), 'new');
    await userEvent.click(screen.getByRole('button', { name: '保存' }));

    await waitFor(() => expect(screen.getByText('角色与权限')).toBeInTheDocument());
  });

  it('那道门是挡着的：欠改密时工作台不出现，改密页退不出去', async () => {
    // **这一条与第二条不同。** 第二条问「落点对不对」，这一条问
    // 「这道门是不是一道门」—— 一个只在登录那一刻判一次的实现，
    // 会在任何一次重渲染之后把人放进工作台。
    session.grant({ access_token: 'at', token_type: 'bearer', expires_in: 900 });
    session.markPasswordChangeRequired();

    const { rerender } = render(<App />);
    expect(screen.getByText('设置新密码')).toBeInTheDocument();
    rerender(<App />);
    expect(screen.getByText('设置新密码')).toBeInTheDocument();
    expect(screen.queryByText('角色与权限')).not.toBeInTheDocument();
  });
});
