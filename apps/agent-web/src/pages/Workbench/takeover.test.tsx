import { I18nProvider } from '@lingui/react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConfigProvider } from 'antd';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { session } from '../../api/session';
import { i18n } from '../../i18n';
import { TakeoverPanel } from './TakeoverPanel';

/**
 * 接管与两阶段交还的接线。
 *
 * **这一族里最要紧的两条问的都是「没发生什么」**：
 *
 * 1. 已接管的会话上**不存在**「直接交还」的入口 —— 后端没有 `force`，
 *    前端也不该造一个看起来像的按钮；
 * 2. `CHANGE_DIGEST_STALE` **不显示成失败** —— 什么都没坏，
 *    只是画面变了。
 *
 * 两条都属于「不做某件事」那一型，而那正是最容易被下一个人顺手改掉的形状。
 */

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** 后端真发的错误信封是五个字段 —— 少一个 `messageOf` 就退回兜底文案。 */
function errorEnvelope(code: string, message: string): Record<string, unknown> {
  return { code, message, request_id: 'req-1', field_errors: [], retryable: false };
}

const PROJECTION = {
  slots_written: { human: [{ name: 'budget' }], model: [], unattributed: [] },
  pending_exit_signal: { present: false, note: 'the customer has not signalled an exit' },
  commitments_registered: {
    count: 0,
    status: 'ledger_not_enabled',
    note: '承诺台账尚未启用，所以这个 0 不代表这一轮没有承诺',
  },
  takeover_period: { source: 'takeover', started_at: '2026-09-20T01:00:00Z' },
  refused_slot_updates: { total: 0, by_reason_code: {}, shown: 0, items: [], note: '' },
  understanding_halted_by_cost_gate: {
    halted: false,
    occurrences: 0,
    last_at: null,
    note: '',
  },
};

const PREVIEW = {
  conversation_id: 'c-1',
  conversation_summary: ['客户在问三万预算能买什么'],
  control_mode: 'human',
  projection_version: 7,
  change_digest: 'a'.repeat(64),
  takeover_projection: PROJECTION,
};

function routeFor(input: RequestInfo | URL): string {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  return new URL(url, 'http://localhost').pathname.replace(/^\/api/, '');
}

function mount(controlMode: string, onChanged = (): void => {}): ReturnType<typeof render> {
  return render(
    <I18nProvider i18n={i18n}>
      <ConfigProvider button={{ autoInsertSpace: false }}>
        <TakeoverPanel conversationId="c-1" controlMode={controlMode} onChanged={onChanged} />
      </ConfigProvider>
    </I18nProvider>,
  );
}

beforeEach(() => {
  session.clear();
  session.grant({ access_token: 'at', token_type: 'bearer', expires_in: 900 });
});

describe('接管', () => {
  it('接管之后报出挂起了几帧、停掉了几个待触发动作', async () => {
    // 别的 route 都不给这个数，而它是「这通对话真的安静下来了」的唯一依据。
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      json({
        receipt: {},
        result: {
          control_mode: 'human',
          generation: 4,
          frames_suspended: 2,
          events_invalidated: 3,
        },
      }),
    );
    mount('ai');

    await userEvent.click(screen.getByRole('button', { name: '接管' }));

    expect(await screen.findByText(/挂起 2 帧/)).toBeInTheDocument();
    expect(screen.getByText(/停掉 3 个待触发的动作/)).toBeInTheDocument();
  });

  it('接管失败时说后端那句话，不说一句自己编的', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      json(errorEnvelope('CONVERSATION_ALREADY_HUMAN', '这通对话已经归人了'), 409),
    );
    mount('ai');

    await userEvent.click(screen.getByRole('button', { name: '接管' }));

    expect(await screen.findByText('这通对话已经归人了')).toBeInTheDocument();
  });
});

describe('交还', () => {
  it('已接管的会话上没有「直接交还」的入口', async () => {
    // **这是一条「多了要红」的判据。** 后端 `ResumeConversationRequest`
    // 逐字写着「没有 force，也没有不看就交还的办法」——
    // 一个跳过预览的按钮就是用默认值替人回答了那个问题。
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json({}));
    mount('human');

    expect(screen.getByRole('button', { name: '看一眼再交还给 AI' })).toBeInTheDocument();
    for (const forbidden of [/^交还给 AI$/, /^直接交还$/, /^强制交还$/]) {
      expect(screen.queryByRole('button', { name: forbidden })).not.toBeInTheDocument();
    }
  });

  it('看预览时把接管期发生的事显示出来，包括那个会误导人的 0', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(PREVIEW));
    mount('human');

    await userEvent.click(screen.getByRole('button', { name: '看一眼再交还给 AI' }));

    expect(await screen.findByText('客户在问三万预算能买什么')).toBeInTheDocument();
    // 承诺那一格：**光秃秃的 0 与「这一轮确实没有承诺」在屏幕上一模一样**，
    // 所以后端那句说明必须跟着上屏。
    expect(screen.getByText(/承诺台账尚未启用/)).toBeInTheDocument();
  });

  it('确认交还时送的是预览里那个 digest，不是别的', async () => {
    const sent: Request[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const request = input as Request;
      if (routeFor(input) === '/conversations/c-1/resume-preview') return json(PREVIEW);
      sent.push(request);
      return json({
        receipt: {},
        result: {
          control_mode: 'ai',
          generation: 5,
          frames_active: 2,
          timers_restored: 0,
        },
      });
    });
    mount('human');

    await userEvent.click(screen.getByRole('button', { name: '看一眼再交还给 AI' }));
    await userEvent.click(await screen.findByRole('button', { name: '确认交还' }));

    await waitFor(() => expect(sent).toHaveLength(1));
    const body = JSON.parse(await sent[0]!.clone().text()) as Record<string, unknown>;
    expect(body['acknowledged_change_digest']).toBe(PREVIEW.change_digest);
    expect(body['invalidate_summary']).toBe(false);
    expect(sent[0]!.headers.get('idempotency-key')).toBeTruthy();
  });

  it('交还成功时说清定时器不会回来', async () => {
    // `timers_restored` 恒为 0，而那是一条裁定不是一个占位符 ——
    // 一个光秃秃的 0 会被读成「系统忘了恢复」。
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (routeFor(input) === '/conversations/c-1/resume-preview') return json(PREVIEW);
      return json({
        receipt: {},
        result: { control_mode: 'ai', generation: 5, frames_active: 2, timers_restored: 0 },
      });
    });
    mount('human');

    await userEvent.click(screen.getByRole('button', { name: '看一眼再交还给 AI' }));
    await userEvent.click(await screen.findByRole('button', { name: '确认交还' }));

    expect(await screen.findByText(/定时器不会回来/)).toBeInTheDocument();
  });

  it('digest 过期不显示成失败，而是重新读一遍画面', async () => {
    // **409 在这里不是错误**：客户又说了话，或者引擎又写了槽位。
    // 什么都没坏，只是要重看一遍再确认。
    let previews = 0;
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (routeFor(input) === '/conversations/c-1/resume-preview') {
        previews += 1;
        return json(PREVIEW);
      }
      return json(errorEnvelope('CHANGE_DIGEST_STALE', 'digest 不匹配'), 409);
    });
    mount('human');

    await userEvent.click(screen.getByRole('button', { name: '看一眼再交还给 AI' }));
    await userEvent.click(await screen.findByRole('button', { name: '确认交还' }));

    expect(await screen.findByText('这通对话在你看的时候又变了')).toBeInTheDocument();
    // 重拉了一次，而且没有把它说成「交还失败」。
    await waitFor(() => expect(previews).toBe(2));
    expect(screen.queryByText('交还失败')).not.toBeInTheDocument();
  });

  it('别的 409 照常报错，不当成「画面变了」', async () => {
    // 上一条单独存在会被一个「所有 409 都重拉」的实现骗过 ——
    // 而那种实现会把「这通对话已经不归你了」变成一个永远重试的循环。
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (routeFor(input) === '/conversations/c-1/resume-preview') return json(PREVIEW);
      return json(errorEnvelope('CONVERSATION_NOT_HELD_BY_A_PERSON', '这通对话不归你'), 409);
    });
    mount('human');

    await userEvent.click(screen.getByRole('button', { name: '看一眼再交还给 AI' }));
    await userEvent.click(await screen.findByRole('button', { name: '确认交还' }));

    expect(await screen.findByText('这通对话不归你')).toBeInTheDocument();
    expect(screen.queryByText('这通对话在你看的时候又变了')).not.toBeInTheDocument();
  });

  it('理解步被成本闸拦过时，先说「这段时间有事没记下来」', async () => {
    // 这一项为真，其余四项就是不完整的 —— 照常显示等于请人对着
    // 一幅缺了几笔的画做决定。
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      json({
        ...PREVIEW,
        takeover_projection: {
          ...PROJECTION,
          understanding_halted_by_cost_gate: {
            halted: true,
            occurrences: 4,
            last_at: '2026-09-20T02:00:00Z',
            note: '',
          },
        },
      }),
    );
    mount('human');

    await userEvent.click(screen.getByRole('button', { name: '看一眼再交还给 AI' }));

    expect(await screen.findByText('这段时间里有事情没被记下来')).toBeInTheDocument();
    expect(screen.getByText(/拦过 4 次/)).toBeInTheDocument();
  });

  it('勾了「这份摘要是错的」就把作废一起送过去', async () => {
    const sent: Request[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (routeFor(input) === '/conversations/c-1/resume-preview') return json(PREVIEW);
      sent.push(input as Request);
      return json({
        receipt: {},
        result: { control_mode: 'ai', generation: 5, frames_active: 1, timers_restored: 0 },
      });
    });
    mount('human');

    await userEvent.click(screen.getByRole('button', { name: '看一眼再交还给 AI' }));
    await userEvent.click(await screen.findByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', { name: '确认交还' }));

    await waitFor(() => expect(sent).toHaveLength(1));
    const body = JSON.parse(await sent[0]!.clone().text()) as Record<string, unknown>;
    expect(body['invalidate_summary']).toBe(true);
  });

  it('没有摘要时那个作废勾选点不动', async () => {
    // 作废一份不存在的摘要是一个无意义的命令 —— 而一个能点的勾选框
    // 会让人以为自己做了什么。
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      json({ ...PREVIEW, conversation_summary: [] }),
    );
    mount('human');

    await userEvent.click(screen.getByRole('button', { name: '看一眼再交还给 AI' }));

    expect(await screen.findByText(/还没有摘要/)).toBeInTheDocument();
    expect(screen.getByRole('checkbox')).toBeDisabled();
  });
});
