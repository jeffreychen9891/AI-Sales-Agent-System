import { I18nProvider } from '@lingui/react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConfigProvider } from 'antd';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { session } from '../../api/session';
import { i18n } from '../../i18n';
import { ProfilePage } from './ProfilePage';

/**
 * 话术页的接线。
 *
 * **每一条问的都是「后端说的那件事有没有如实到屏幕上」**，
 * 而不是「它有没有渲染出来」—— 后者任何一个空壳都能通过。
 *
 * ⚠️ 其中一条问的是**没到屏幕上的东西**：ADR-0143 §二 不给的那四项
 * 就算后端哪天多发了，这一页也不该把它们画出来。那条用例故意让
 * 后端多回四个键 —— 它是这一页上**唯一一条「多了也要红」的判据**。
 */

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const SUMMARY = {
  release_id: 'r-1',
  profile_id: 'demo-sales',
  version: '1.2.4',
  content_hash: 'a'.repeat(64),
};

const DOCUMENT = {
  ...SUMMARY,
  target_frames: [
    {
      id: 'greeting',
      goal: '先把客户想买什么问清楚',
      information_slots: [{ id: 'budget' }, { id: 'ring_size' }],
      content_materials: ['mat.welcome'],
    },
    { id: 'closing', goal: '', information_slots: [], content_materials: [] },
  ],
  proactivity_policy: {
    active_window_seconds: 7200,
    reengagement_limits: { window_seconds: 86400, max_delivered: 2 },
    quiet_hours: { timezone: 'Asia/Shanghai', ranges: [{ start: '22:00', end: '08:00' }] },
  },
  memory_fields: [{ id: 'anniversary' }],
  knowledge_bindings: ['kb.jewellery'],
  content_material_texts: { 'mat.welcome': '您好，今天想看点什么？' },
};

function mount(): ReturnType<typeof render> {
  return render(
    <I18nProvider i18n={i18n}>
      <ConfigProvider button={{ autoInsertSpace: false }}>
        <ProfilePage />
      </ConfigProvider>
    </I18nProvider>,
  );
}

/**
 * 请求落在哪条 route 上 —— **去掉 `/api` 前缀之后的那一段**。
 *
 * `api/client.ts` 的 `baseUrl` 是 `/api`，所以 `fetch` 看到的是
 * `/api/profiles/releases`。拿 OpenAPI 里那条路径直接比会**一条都对不上**，
 * 而表现出来是「页面什么都没读到」—— 与真的读不到一模一样
 * （实测 2026-09-20，七条用例一起红在这上面）。
 */
function routeFor(input: RequestInfo | URL): string {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  return new URL(url, 'http://localhost').pathname.replace(/^\/api/, '');
}

function serve(document: unknown = DOCUMENT, summaries: unknown[] = [SUMMARY]): void {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const route = routeFor(input);
    if (route === '/profiles/releases') {
      return json({ items: summaries, next_cursor: null, has_more: false, as_of: 'now' });
    }
    if (route === '/profiles/releases/r-1') return json(document);
    throw new Error(`没有为 ${route} 准备回答`);
  });
}

beforeEach(() => {
  session.clear();
  session.grant({ access_token: 'at', token_type: 'bearer', expires_in: 900 });
});

describe('话术页', () => {
  it('打开就显示第一版的正文，而不是只显示一个版本号', async () => {
    // 上一批的状态正是「看得到有哪几版、看不到内容」——
    // 一个仍然要用户先点一下才出正文的页面，与那个状态区别不大。
    serve();
    mount();

    expect(await screen.findByText('先把客户想买什么问清楚')).toBeInTheDocument();
    expect(screen.getByText('greeting')).toBeInTheDocument();
  });

  it('把引擎的字段名换成商家看得懂的话，单位也跟着换', async () => {
    // `active_window_seconds: 7200` 是引擎的写法。屏幕上要出现「120 分钟」——
    // **秒是给代码的**，而商家是按分钟排班的。
    serve();
    mount();

    expect(await screen.findByText('120 分钟')).toBeInTheDocument();
    expect(screen.queryByText(/active_window_seconds/)).not.toBeInTheDocument();
  });

  it('免打扰时段写明按谁的时区算', async () => {
    // ADR-0097：渠道回调里没有客户时区，所以这个窗口是按**租户**时区判的。
    // 不写明，商家会读成「客户那边的晚上」——而那是它今天做不到的事。
    serve();
    mount();

    expect(await screen.findByText(/22:00–08:00/)).toBeInTheDocument();
    expect(screen.getByText(/Asia\/Shanghai/)).toBeInTheDocument();
  });

  it('后端多回了不该给的四项，这一页也不显示它们', async () => {
    // **这是唯一一条「多了也要红」的判据。** ADR-0143 §二 的裁定由后端
    // 的投影守卫钉着，而那条守卫护不到这一页：一个照着 key 遍历的渲染
    // 会把后端多发的任何东西画上屏幕。
    serve({
      ...DOCUMENT,
      policy_bindings: { some: 'binding' },
      confidence_gate_policy_ref: 'gate-v3',
      manipulation_context_refs: ['ctx-1'],
      golden_manifest: ['case-1'],
    });
    mount();

    await screen.findByText('先把客户想买什么问清楚');
    for (const leaked of ['gate-v3', 'ctx-1', 'case-1', 'binding']) {
      expect(screen.queryByText(new RegExp(leaked))).not.toBeInTheDocument();
    }
  });

  it('界面上说清楚「有东西没给你看」，而不是默默不显示', async () => {
    // 默默不显示，商家会以为自己看到了全部 —— 而他下次问
    // 「为什么 AI 会这么答」时，答案有一半不在他见过的东西里。
    serve();
    mount();

    expect(await screen.findAllByText(/置信闸/)).not.toHaveLength(0);
  });

  it('说的是「不能改」，而不是给一个点不动的编辑按钮', async () => {
    serve();
    mount();

    expect(await screen.findByText('这一版只能看，不能改')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /编辑/ })).not.toBeInTheDocument();
  });

  it('一个字段缺了就少一行，而不是整页白掉', async () => {
    // `goal` 是空串的那个帧仍然要出现在表里 —— 读取器的兜底是空值，
    // 而「这个帧没写目标」与「这一版读坏了」在界面上要分得开。
    serve();
    mount();

    expect(await screen.findByText('closing')).toBeInTheDocument();
  });

  it('没有发布过话术时说的是「还没发布」，不是一张空表', async () => {
    serve(DOCUMENT, []);
    mount();

    expect(await screen.findByText('这个商家还没有发布过话术')).toBeInTheDocument();
  });

  it('换一版会重新去读那一版的正文', async () => {
    // 版本切换只改 URL 里的 id；如果它没有真的再请求一次，
    // 屏幕上会一直留着上一版的内容而看起来完全正常。
    const second = { ...SUMMARY, release_id: 'r-2', version: '1.3.0' };
    const asked: string[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const route = routeFor(input);
      asked.push(route);
      if (route === '/profiles/releases') {
        return json({
          items: [SUMMARY, second],
          next_cursor: null,
          has_more: false,
          as_of: 'now',
        });
      }
      if (route === '/profiles/releases/r-1') return json(DOCUMENT);
      if (route === '/profiles/releases/r-2') {
        return json({ ...DOCUMENT, version: '1.3.0', target_frames: [] });
      }
      throw new Error(`没有为 ${route} 准备回答`);
    });
    mount();
    await screen.findByText('先把客户想买什么问清楚');

    await userEvent.click(screen.getByRole('combobox', { name: '选择一个版本' }));
    await userEvent.click(await screen.findByTitle('demo-sales 1.3.0'));

    await waitFor(() => expect(asked).toContain('/profiles/releases/r-2'));
    expect(await screen.findByText('这一版没有目标帧')).toBeInTheDocument();
  });

  it('读不到就说读不到，而不是显示成一个空的话术', async () => {
    // 空态与失败态长得一样，是本仓反复记的那种形状：
    // 「这个商家没有素材」和「这一版没读出来」在屏幕上必须分得开。
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const route = routeFor(input);
      if (route === '/profiles/releases') {
        return json({ items: [SUMMARY], next_cursor: null, has_more: false, as_of: 'now' });
      }
      // **五个字段一个不少** —— `isErrorEnvelope` 五个都查，少一个就不算信封，
      // `messageOf` 于是退回兜底文案，屏幕上出现的是「读取失败」而不是后端
      // 说的那句话。**一个只长得像的夹具会让这条用例测到别的东西**
      // （实测 2026-09-20：它先用两个字段写成，验到的其实是兜底文案）。
      return json(
        {
          code: 'PROFILE_RELEASE_NOT_FOUND',
          message: '没有这一版',
          request_id: 'req-1',
          field_errors: [],
          retryable: false,
        },
        404,
      );
    });
    mount();

    // 按文案找，不按 `role="alert"` 找：页面顶上那条「只能看不能改」的
    // 提示也是一个 alert，先到的是它 —— 而「找到了一个 alert」并不说明
    // 找到的是**报错**那一个。
    expect(await screen.findByText(/没有这一版/)).toBeInTheDocument();
  });
});
