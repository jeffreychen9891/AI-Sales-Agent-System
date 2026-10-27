import { I18nProvider } from '@lingui/react';
import { render, screen } from '@testing-library/react';
import { ConfigProvider } from 'antd';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { session } from '../../api/session';
import { i18n } from '../../i18n';
import { TargetStackCard } from './TargetStackCard';

/**
 * 目标栈与信息槽的接线。
 *
 * **这一族里最要紧的一条是关于第三种出处的。** ADR-0054 §三.2 之前
 * 写下的槽位有值、没有出处，而把它们显示成「AI 记的」是**替一条
 * 读不出来的记录作证** —— 那会让坐席以为可以放心让 AI 改它。
 */

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function detail(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    conversation_id: 'c-1',
    lifecycle: 'open',
    control_mode: 'human',
    projection_version: 7,
    generation: 3,
    profile_pin: { release_id: 'r-1' },
    channel_config_id: 'cfg-1',
    last_inbound_at: null,
    last_outbound_at: null,
    target_stack: [
      {
        frame_id: 'greeting',
        definition_id: 'greeting@1',
        goal: '先把客户想买什么问清楚',
        status: 'suspended',
        entered_at: null,
        last_progress_at: null,
        parent_frame_id: null,
        interruption_reason: null,
        resume_confirmation_policy: null,
        frame_generation: 1,
        slots: [
          {
            name: 'budget',
            confidence: 0.9,
            source: 'human',
            recorded_at: null,
            evidence_event_id: 'm-1',
          },
          {
            name: 'timeline',
            confidence: 0.4,
            source: 'model',
            recorded_at: null,
            evidence_event_id: null,
          },
          {
            name: 'legacy',
            confidence: null,
            source: null,
            recorded_at: null,
            evidence_event_id: null,
          },
        ],
      },
    ],
    commitments: { registered: 0, status: 'ledger_not_enabled', note: '台账未启用' },
    outcome: { outcome_ref: null, status: 'not_implemented', note: '结果尚未接入' },
    last_transition: null,
    takeover_projection: {},
    ...overrides,
  };
}

function mount(): ReturnType<typeof render> {
  return render(
    <I18nProvider i18n={i18n}>
      <ConfigProvider button={{ autoInsertSpace: false }}>
        <TargetStackCard conversationId="c-1" reloadToken={0} />
      </ConfigProvider>
    </I18nProvider>,
  );
}

beforeEach(() => {
  session.clear();
  session.grant({ access_token: 'at', token_type: 'bearer', expires_in: 900 });
});

describe('目标栈', () => {
  it('把帧的目标和它问到的信息显示出来', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(detail()));
    mount();

    expect(await screen.findByText('先把客户想买什么问清楚')).toBeInTheDocument();
    expect(screen.getByText('budget')).toBeInTheDocument();
  });

  it('槽位的出处分三种，没有出处的那一条说「说不清」', async () => {
    // **这是本族的核心判据。** 把 `source: null` 显示成「AI 记的」
    // 会让坐席以为那条可以被模型改写 —— 而 ADR-0054 §三.2 的规矩
    // 正好相反：人写的不许改，而这一条根本不知道是谁写的。
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(detail()));
    mount();

    expect(await screen.findByText('人记的')).toBeInTheDocument();
    expect(screen.getByText('AI 记的')).toBeInTheDocument();
    expect(screen.getByText('说不清')).toBeInTheDocument();
  });

  it('被挂起的帧说清是接管挂的，不是出了故障', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(detail()));
    mount();

    expect(await screen.findByText('因接管挂起')).toBeInTheDocument();
    expect(screen.queryByText('suspended')).not.toBeInTheDocument();
  });

  it('引擎还没立目标时说的是「还没立」，不是一张空表', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      json(detail({ target_stack: [] })),
    );
    mount();

    expect(await screen.findByText('引擎还没有为这通对话立目标')).toBeInTheDocument();
  });

  it('界面上说清这一版不能改，而不是给一个点不动的编辑框', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(detail()));
    mount();

    expect(await screen.findByText(/这一版只能看/)).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('读不到就说读不到，而不是显示成一个没有目标的会话', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      json(
        {
          code: 'CONVERSATION_NOT_FOUND',
          message: '没有这通对话',
          request_id: 'req-1',
          field_errors: [],
          retryable: false,
        },
        404,
      ),
    );
    mount();

    expect(await screen.findByText('没有这通对话')).toBeInTheDocument();
  });
});
