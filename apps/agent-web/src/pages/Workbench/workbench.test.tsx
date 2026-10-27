import { I18nProvider } from '@lingui/react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConfigProvider } from 'antd';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { session } from '../../api/session';
import { i18n } from '../../i18n';
import { WorkbenchPage } from './WorkbenchPage';

/**
 * 工作台的接线。
 *
 * **每一条问的都是「这一屏有没有把后端说的那件事如实显示出来」**，
 * 而不是「它有没有渲染出来」—— 后者任何一个空壳都能通过。
 */

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const ROW = {
  conversation_id: 'c-1',
  contact: {
    contact_id: 'p-1',
    config_id: 'cfg-1',
    external_user_id: 'wx-abc',
    profile_data: {},
  },
  last_message: {
    message_id: 'm-9',
    direction: 'inbound',
    created_at: '2026-09-20T01:00:00Z',
    content: '在吗',
  },
  unread_inbound: 3,
  control_mode: 'ai',
  target: { frame_id: 'f-1', definition_id: 'd-1', status: 'active' },
  next_event: { kind: 'follow_up', due_at: '2026-09-21T01:00:00Z' },
  open_manual_tasks: 2,
  projection_version: 7,
};

/**
 * 会话详情那一条 route 的回答。
 *
 * ⚠️ **批 7 之前这个替身没有它**，而那套替身的兜底分支对**任何**没匹配上的
 * 路径都返回一页会话列表 —— 于是 `GET /conversations/{id}` 也拿到一页列表，
 * `target_stack` 是 `undefined`，整块详情当场崩掉。
 * **兜底分支不是「其余都无所谓」，它是一个会说谎的默认值。**
 */
function conversationDetail(): unknown {
  return {
    conversation_id: 'c-1',
    lifecycle: 'open',
    control_mode: 'ai',
    projection_version: 1,
    generation: 1,
    profile_pin: { release_id: 'r-1' },
    channel_config_id: 'cfg-1',
    last_inbound_at: null,
    last_outbound_at: null,
    target_stack: [],
    commitments: { registered: 0, status: 'ledger_not_enabled', note: '' },
    outcome: { outcome_ref: null, status: 'not_implemented', note: '' },
    last_transition: null,
    takeover_projection: {},
  };
}

/**
 * 是不是「一条会话的详情」那条路径（而不是会话列表、也不是它下面的子路径）。
 *
 * ⚠️ **不锚在开头。** 本文件的 `routeFor` **不剥 `/api` 前缀**（它一直只用
 * `endsWith` / `includes`，所以这件事从来没暴露过），于是这里拿到的是
 * `/api/conversations/c-1`。第一版写成 `/^\/conversations\/[^/]+$/`，
 * 一条都匹配不上 —— 而那**不会报错**：它安静地掉进兜底分支，
 * 详情拿到一页会话列表，`target_stack` 是 `undefined`。
 *
 * 末尾那个 `$` 仍然要：没有它，`/conversations/c-1/messages` 也会被它接走。
 */
function isConversationDetail(path: string): boolean {
  return /\/conversations\/[^/]+$/.test(path);
}

function page(items: unknown[]): unknown {
  return { items, next_cursor: null, has_more: false, as_of: '2026-09-20T00:00:00Z' };
}

function mount(): ReturnType<typeof render> {
  return render(
    <I18nProvider i18n={i18n}>
      <ConfigProvider button={{ autoInsertSpace: false }}>
        <WorkbenchPage />
      </ConfigProvider>
    </I18nProvider>,
  );
}

function routeFor(input: RequestInfo | URL): string {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  return new URL(url, 'http://localhost').pathname;
}

beforeEach(() => {
  session.clear();
  session.grant({ access_token: 'at', token_type: 'bearer', expires_in: 900 });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('会话列表', () => {
  it('九项读模型里那些**会被看见**的，每一项都显示出来', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(page([ROW])));

    mount();

    await waitFor(() => expect(screen.getByText('wx-abc')).toBeInTheDocument());
    const row = screen.getByText('wx-abc').closest('li') as HTMLElement;
    // 最后一条消息、未读数、控制方、目标状态、下一主动事件、人工任务数。
    expect(within(row).getByText('在吗')).toBeInTheDocument();
    expect(within(row).getByText('3')).toBeInTheDocument();
    expect(within(row).getByText('AI 接待')).toBeInTheDocument();
    expect(within(row).getByText('active')).toBeInTheDocument();
    expect(within(row).getByText(/follow_up/)).toBeInTheDocument();
    expect(within(row).getByText(/待办 2/)).toBeInTheDocument();
  });

  it('空态说清是哪一种空 —— 「还没有会话」与「读失败了」不是同一句话', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(page([])));
    mount();
    await waitFor(() => expect(screen.getByText('还没有会话')).toBeInTheDocument());
  });

  it('读失败时不显示「还没有会话」 —— 那会把一次故障说成一个事实', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json(
        {
          code: 'FORBIDDEN',
          message: '没有权限',
          request_id: 'r',
          field_errors: [],
          retryable: false,
        },
        403,
      ),
    );
    mount();
    await waitFor(() => expect(screen.getByText('没有权限')).toBeInTheDocument());
    expect(screen.queryByText('还没有会话')).not.toBeInTheDocument();
  });

  it('轮询：到了间隔会再拉一次，而不是只拉一次就不动了', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(page([ROW])));
    mount();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(15_000);
    await vi.waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2));
  });
});

/**
 * 一套把四条 route 都接上的替身。**返回类型交给推断**：`vi.spyOn` 的泛型
 * 带着 fetch 的两个重载，手写一个标注会把它们抹平成
 * `(...args: unknown[])`，于是 tsc 报不兼容。
 */
function wireDetail(
  options: { controlMode?: string; sendResponse?: Response } = {},
) {
  const controlMode = options.controlMode ?? 'ai';
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const request = input as Request;
    const path = routeFor(input);
    if (path.endsWith('/messages') && request.method === 'POST') {
      return (
        options.sendResponse?.clone() ??
        json({
          command_id: 'c',
          status: 'accepted',
          resource_ref: { resource_type: 'conversation_message', resource_id: 'o', tenant_id: 't' },
          accepted_at: 'x',
          idempotency_key: 'k',
          correlation_id: 'r',
        }, 202)
      );
    }
    if (path.endsWith('/messages')) {
      return json(page([]));
    }
    if (path.includes('/contacts/by-conversation/')) {
      return json({
        contact_id: 'p-1',
        config_id: 'cfg-1',
        external_user_id: 'wx-abc',
        profile_data: {},
        first_seen_at: '2026-09-01T00:00:00Z',
        last_seen_at: '2026-09-20T01:00:00Z',
        message_count: 12,
      });
    }
    if (path.endsWith('/read')) {
      return json({ receipt: {}, result: {} });
    }
    if (isConversationDetail(path)) {
      return json(conversationDetail());
    }
    return json(page([{ ...ROW, control_mode: controlMode }]));
  });
}

describe('选中一条会话', () => {
  function wire() {
    return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const path = routeFor(input);
      if (path.endsWith('/messages')) {
        return json(
          page([
            {
              message_id: 'm-1',
              direction: 'inbound',
              created_at: '2026-09-20T01:00:00Z',
              delivery_status: 'DELIVERED',
              revoked: false,
              content: '在吗',
              actor: { kind: 'contact' },
              occurred_at: '2026-09-20T01:00:00Z',
              correlation_id: 'cor-1',
            },
            {
              message_id: 'm-2',
              direction: 'outbound',
              created_at: '2026-09-20T01:01:00Z',
              delivery_status: 'DELIVERED',
              revoked: false,
              content: '在的',
            },
          ]),
        );
      }
      if (path.includes('/contacts/by-conversation/')) {
        return json({
          contact_id: 'p-1',
          config_id: 'cfg-1',
          external_user_id: 'wx-abc',
          profile_data: {},
          first_seen_at: '2026-09-01T00:00:00Z',
          last_seen_at: '2026-09-20T01:00:00Z',
          message_count: 12,
        });
      }
      if (path.endsWith('/read')) {
        return json({ receipt: {}, result: {} });
      }
      if (isConversationDetail(path)) {
        return json(conversationDetail());
      }
      return json(page([ROW]));
    });
  }

  it('拉出逐字记录，并**按最后一条消息的 id** 报已读', async () => {
    const fetchMock = wire();
    mount();
    await waitFor(() => expect(screen.getByText('wx-abc')).toBeInTheDocument());
    await userEvent.click(screen.getByText('wx-abc'));

    await waitFor(() => expect(screen.getByText('在的')).toBeInTheDocument());

    const read = fetchMock.mock.calls
      .map(([input]) => input as Request)
      .find((request) => routeFor(request).endsWith('/read'));
    expect(read, '没有发出已读命令').toBeDefined();
    // **报的是消息 id，不是客户端的时钟。** 送时间戳等于对一个自己不拥有
    // 的排序下断言 —— `MarkConversationReadRequest` 的 description 逐字写着。
    expect(JSON.parse(await read!.clone().text())).toEqual({ last_read_ref: 'm-2' });
    // 幂等键走 header，而不是随便挂在哪里。
    expect(read!.headers.get('idempotency-key')).toBeTruthy();
  });

  it('出站消息显示投递状态；**整屏没有撤回入口**', async () => {
    wire();
    mount();
    await waitFor(() => expect(screen.getByText('wx-abc')).toBeInTheDocument());
    await userEvent.click(screen.getByText('wx-abc'));

    await waitFor(() => expect(screen.getByText('在的')).toBeInTheDocument());
    expect(screen.getByText(/DELIVERED/)).toBeInTheDocument();
    // **撤回被 K15 移出，`revoked` 恒 False 是答案不是占位。**
    // 放一个置灰的按钮等于说「这件事将来能做」，而那不是今天的事实。
    expect(screen.queryByText(/撤回/)).not.toBeInTheDocument();
  });

  it('T179：客户资料为空时写明是哪一种空', async () => {
    wire();
    mount();
    await waitFor(() => expect(screen.getByText('wx-abc')).toBeInTheDocument());
    await userEvent.click(screen.getByText('wx-abc'));

    await waitFor(() => expect(screen.getByText('渠道资料尚未接入')).toBeInTheDocument());
    // **两件都要断言**：只断言那句提示在，会让一个把提示常显的实现照样绿；
    // 这里同时要求真实数据（消息数）也在 —— 提示不是用来替代内容的。
    expect(screen.getByText('12')).toBeInTheDocument();
  });
});

describe('发送框', () => {
  it('会话还归 AI 时，说清「先接管」而不是给一个点不动的按钮', async () => {
    wireDetail();
    mount();
    await waitFor(() => expect(screen.getByText('wx-abc')).toBeInTheDocument());
    await userEvent.click(screen.getByText('wx-abc'));

    await waitFor(() => expect(screen.getByText('这条会话还归 AI 接待')).toBeInTheDocument());
    // **置灰的按钮说不出「先接管」三个字**，而那是用户此刻唯一需要知道的事。
    expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument();
  });

  it('接管之后能发，且请求体与幂等键都对', async () => {
    const fetchMock = wireDetail({ controlMode: 'human' });
    mount();
    await waitFor(() => expect(screen.getByText('wx-abc')).toBeInTheDocument());
    await userEvent.click(screen.getByText('wx-abc'));
    await waitFor(() => expect(screen.getByLabelText('要发给客户的话')).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText('要发给客户的话'), '在的');
    await userEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(screen.getByText('已排队')).toBeInTheDocument());
    const sent = fetchMock.mock.calls
      .map(([input]) => input as Request)
      .find((r) => r.method === 'POST' && routeFor(r).endsWith('/messages'));
    expect(sent, '没有发出那条命令').toBeDefined();
    expect(JSON.parse(await sent!.clone().text())).toEqual({ text: '在的' });
    expect(sent!.headers.get('idempotency-key')).toBeTruthy();
  });

  it('成功之后说的是「已排队」，不是「已发送」', async () => {
    wireDetail({ controlMode: 'human' });
    mount();
    await waitFor(() => expect(screen.getByText('wx-abc')).toBeInTheDocument());
    await userEvent.click(screen.getByText('wx-abc'));
    await waitFor(() => expect(screen.getByLabelText('要发给客户的话')).toBeInTheDocument());
    await userEvent.type(screen.getByLabelText('要发给客户的话'), '在的');
    await userEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(screen.getByText('已排队')).toBeInTheDocument());
    // **这一条不是措辞讲究。** 202 的意思是排进队列，不是送达；
    // 把它写成「已发送」，坐席会在一次投递失败之后仍以为客户收到了 ——
    // 而那种误会要到客户抱怨时才暴露。parity §6.2：
    // 「UI 的『已发送』只能来自 receipt，不来自按钮点击成功」。
    expect(screen.queryByText('已发送')).not.toBeInTheDocument();
  });

  it('后端拒绝时显示信封里的 message，并且不说「已排队」', async () => {
    wireDetail({
      controlMode: 'human',
      sendResponse: json(
        {
          code: 'CONVERSATION_NOT_TAKEN_OVER',
          message: '这条会话现在不归你说话',
          request_id: 'r',
          field_errors: [],
          retryable: false,
        },
        409,
      ),
    });
    mount();
    await waitFor(() => expect(screen.getByText('wx-abc')).toBeInTheDocument());
    await userEvent.click(screen.getByText('wx-abc'));
    await waitFor(() => expect(screen.getByLabelText('要发给客户的话')).toBeInTheDocument());
    await userEvent.type(screen.getByLabelText('要发给客户的话'), '在的');
    await userEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(screen.getByText('这条会话现在不归你说话')).toBeInTheDocument());
    expect(screen.queryByText('已排队')).not.toBeInTheDocument();
  });
});
