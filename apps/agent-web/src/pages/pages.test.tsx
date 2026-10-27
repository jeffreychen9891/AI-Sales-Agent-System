import { I18nProvider } from '@lingui/react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConfigProvider } from 'antd';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { i18n } from '../i18n';
import { session } from '../api/session';
import { EmployeesPage } from './Employees/EmployeesPage';
import { SettingsPage } from './Settings/SettingsPage';

/**
 * B 面三页的接线。
 *
 * **每一条都问「这一页有没有把后端说的那件事如实显示出来」**，
 * 而不是「它有没有渲染出来」。后者任何一个空壳都能通过。
 */

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function page(items: unknown[]): unknown {
  return { items, next_cursor: null, has_more: false, as_of: '2026-09-20T00:00:00Z' };
}

/**
 * 与生产同一棵树 —— 理由见 `App.tsx`。
 *
 * **`I18nProvider` 也在里面，而这一格是被一次红逼出来的**：文案 wrap 之后
 * `useLingui()` 要求这个 provider，没有它整页抛。更要紧的是**就算它不抛**，
 * 一棵少了 provider 的测试树也会让用例跑在一个与生产不同的渲染环境里 ——
 * 那种差异不会让用例变红，它只会让用例证明不了它声称证明的那件事。
 */
function mount(node: JSX.Element): ReturnType<typeof render> {
  return render(
    <I18nProvider i18n={i18n}>
      <ConfigProvider button={{ autoInsertSpace: false }}>{node}</ConfigProvider>
    </I18nProvider>,
  );
}

function routeFor(input: RequestInfo | URL): string {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  return new URL(url, 'http://localhost').pathname;
}

describe('员工页', () => {
  beforeEach(() => {
    session.clear();
    session.grant({ access_token: 'at', token_type: 'bearer', expires_in: 900 });
  });

  it('T155：已 purge 的人显示成「已清除」，而不是和「已停用」混在一起', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const path = routeFor(input);
      if (path.endsWith('/employees/stats')) {
        return json({ total: 3, active: 1, disabled: 2, as_of: '2026-09-20T00:00:00Z' });
      }
      return json(
        page([
          {
            employee_id: 'e1',
            email: 'live@example.com',
            status: 'active',
            permission_count: 2,
            purged_at: null,
          },
          {
            employee_id: 'e2',
            email: 'off@example.com',
            status: 'disabled',
            permission_count: 0,
            purged_at: null,
          },
          {
            employee_id: 'e3',
            email: 'gone@example.com',
            status: 'disabled',
            permission_count: 0,
            purged_at: '2026-09-01T00:00:00Z',
          },
        ]),
      );
    });

    mount(<EmployeesPage />);

    await waitFor(() => expect(screen.getByText('live@example.com')).toBeInTheDocument());

    // **三种状态各出现一次。** 在 `purged_at` 进投影之前，后两行会是同一种 ——
    // 而 `026` 的列注释明写不得从墓碑地址的形状去猜那个区别。
    const rowOf = (email: string): HTMLElement =>
      screen.getByText(email).closest('tr') as HTMLElement;
    expect(within(rowOf('live@example.com')).getByText('在用')).toBeInTheDocument();
    expect(within(rowOf('off@example.com')).getByText('已停用')).toBeInTheDocument();
    expect(within(rowOf('gone@example.com')).getByText('已清除')).toBeInTheDocument();
  });

  it('已清除的行没有动作按钮 —— 它不是一个还能被操作的人', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) =>
      routeFor(input).endsWith('/stats')
        ? json({ total: 1, active: 0, disabled: 1, as_of: 'x' })
        : json(
            page([
              {
                employee_id: 'e3',
                email: 'gone@example.com',
                status: 'disabled',
                permission_count: 0,
                purged_at: '2026-09-01T00:00:00Z',
              },
            ]),
          ),
    );

    mount(<EmployeesPage />);

    await waitFor(() => expect(screen.getByText('gone@example.com')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: '停用' })).not.toBeInTheDocument();
  });

  it('统计读失败不清空主表 —— 一个旁证的故障不该变成一次空白页', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) =>
      routeFor(input).endsWith('/stats')
        ? json({ code: 'FORBIDDEN', message: '没有权限', request_id: 'r', field_errors: [], retryable: false }, 403)
        : json(
            page([
              {
                employee_id: 'e1',
                email: 'live@example.com',
                status: 'active',
                permission_count: 1,
                purged_at: null,
              },
            ]),
          ),
    );

    mount(<EmployeesPage />);

    await waitFor(() => expect(screen.getByText('live@example.com')).toBeInTheDocument());
  });
});

describe('设置页', () => {
  beforeEach(() => {
    session.clear();
    session.grant({ access_token: 'at', token_type: 'bearer', expires_in: 900 });
  });

  it('保存两个开关时带上 transition_existing: none，并用回执里的值刷新显示', async () => {
    const seen: Array<Record<string, unknown>> = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const request = input as Request;
      const path = routeFor(input);
      if (path.endsWith('/tenants/current')) {
        return json({ tenant_id: 't', code: 'acme', status: 'active', created_at: 'x' });
      }
      if (request.method === 'PUT') {
        seen.push(JSON.parse(await request.clone().text()) as Record<string, unknown>);
        return json({
          receipt: {},
          result: {
            // **回执说的是 `human`，而用户拨的是 `ai`。** 页面必须显示回执，
            // 不是显示它自己请求的东西 —— 这一条就是那个区别。
            default_control_mode: 'human',
            understanding_during_takeover: true,
            conversations_transitioned: 0,
            conversations_remaining: 0,
            conversations_skipped: 0,
          },
        });
      }
      return json({ default_control_mode: 'human', understanding_during_takeover: true });
    });

    mount(<SettingsPage />);
    await waitFor(() => expect(screen.getByText('acme')).toBeInTheDocument());

    await userEvent.click(screen.getByLabelText('新会话默认由 AI 接待'));
    await userEvent.click(screen.getByRole('button', { name: '保存' }));

    await waitFor(() => expect(seen).toHaveLength(1));
    // **批量那一半必须是显式的 `none`。** 漏了它是 422；顺手给别的值
    // 等于把「改策略」和「把在聊的会话批量转走」合成一个动作。
    expect(seen[0]).toMatchObject({
      default_control_mode: 'ai',
      understanding_during_takeover: true,
      transition_existing: 'none',
    });

    // 回执说 `human`，所以开关要回到 `human` —— 而不是停在用户拨的 `ai`。
    await waitFor(() =>
      expect(screen.getByLabelText('新会话默认由 AI 接待')).not.toBeChecked(),
    );
  });
});
