import { Trans, useLingui } from '@lingui/react/macro';
import { Alert, Button, Card, Descriptions, Space, Switch, Typography } from 'antd';
import { useCallback, useEffect, useState } from 'react';

import { api } from '../../api/client';
import { messageOf, newIdempotencyKey } from '../../api/envelopes';
import type { components } from '../../api/schema';

type Tenant = components['schemas']['CurrentTenantResponse'];
type Policy = components['schemas']['ControlPolicyResponse'];

/**
 * 设置：本租户 + 接待策略。
 *
 * ## 那个开关为什么在这里，而不是在会话页上
 *
 * v1 把它叫 `GET /conversations/default-status` —— 而 v2
 * `read_control_policy` 的 docstring 逐字写着**那个名字就是缺陷**：
 * 挂在 `/conversations` 下会让界面把它当成**某一条会话**的事实，
 * 于是「被人接管过的那些」会被显示成错的。它是**租户级**的，所以它在设置里。
 *
 * ## 两个字段一起提交，不是拨一下发一次
 *
 * `write_control_policy` 要求两个字段都给：「部分更新在一个两字段的策略上
 * 读起来是『另一个别动』，而那和『我没想过另一个』是同一句话」，
 * 且两者会互相作用 —— **默认人工 + 关掉理解 = 一个什么都不做的引擎**，
 * 那是一个合法的配置，也是一次糟糕的意外。所以界面是「改完一起保存」。
 *
 * ## 保存之后用回执里的值，不用本地草稿
 *
 * `PUT /control-policy` 的 `result` 里带着**写完之后的策略本身**。
 * 拿本地 draft 假装已经生效，就是把「我请求的」显示成「现在是的」。
 * （那两个字段正是本批的响应守卫抓出来的 —— 我最初的模型漏了它们。）
 */
export function SettingsPage(): JSX.Element {
  const { t } = useLingui();
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [draft, setDraft] = useState<Policy | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const reload = useCallback(async (): Promise<void> => {
    try {
      const [current, control] = await Promise.all([
        api.GET('/tenants/current'),
        api.GET('/control-policy'),
      ]);
      setTenant(current.data ?? null);
      if (control.data !== undefined) {
        setPolicy(control.data);
        setDraft(control.data);
      }
      if (current.data === undefined && control.data === undefined) {
        setFailure(t`读取设置失败`);
      }
    } catch {
      setFailure(t`读取设置失败`);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function save(): Promise<void> {
    if (draft === null) return;
    setSaving(true);
    setFailure(null);
    try {
      const { data, error } = await api.PUT('/control-policy', {
        body: {
          default_control_mode: draft.default_control_mode as 'ai' | 'human',
          understanding_during_takeover: draft.understanding_during_takeover,
          // **`transition_existing` 是必填的，而我原本不知道它存在** ——
          // 生成的类型报了 `Property 'transition_existing' is missing`，
          // 否则这就是一个交付之后才发现的 422。
          //
          // 这里给 `'none'`：**改策略与「把在聊的会话批量转给人」是两件事**。
          // 后端那条路只有一个方向（转人工，且一次最多 100 条），
          // 反方向被按名字拒绝 —— 因为把人正握着的会话交回 AI 是一次
          // resume，而 resume 是两阶段命令（先读 preview 再按 digest 确认）。
          // 一个顺手勾上的复选框会把那道安全去掉。批量那一半单独做。
          transition_existing: 'none',
        },
        params: { header: { 'Idempotency-Key': newIdempotencyKey() } },
      });
      if (data === undefined) {
        setFailure(messageOf(error, t`保存失败`));
        return;
      }
      // **两个都设，而这一格是一条用例逼出来的。**
      // 初稿只设了 `policy` —— 而开关绑的是 `draft`，于是保存之后界面显示的
      // 仍然是**用户请求的那个值**，哪怕服务端回的是另一个。
      // 那是「把我请求的显示成现在是的」，正是本页 docstring 要防的事。
      const settled: Policy = {
        default_control_mode: data.result.default_control_mode,
        understanding_during_takeover: data.result.understanding_during_takeover,
      };
      setPolicy(settled);
      setDraft(settled);
    } catch {
      setFailure(t`保存失败`);
    } finally {
      setSaving(false);
    }
  }

  const dirty =
    draft !== null &&
    policy !== null &&
    (draft.default_control_mode !== policy.default_control_mode ||
      draft.understanding_during_takeover !== policy.understanding_during_takeover);

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      {failure !== null && <Alert type="error" message={failure} showIcon />}
      <Card title={t`本商家`}>
        {tenant === null ? (
          <Typography.Text type="secondary">
            <Trans>没有读到租户信息</Trans>
          </Typography.Text>
        ) : (
          <Descriptions column={1} size="small">
            <Descriptions.Item label={t`编号`}>{tenant.code}</Descriptions.Item>
            <Descriptions.Item label={t`状态`}>{tenant.status}</Descriptions.Item>
            <Descriptions.Item label={t`创建于`}>{tenant.created_at}</Descriptions.Item>
          </Descriptions>
        )}
      </Card>
      <Card title={t`接待策略`}>
        {draft === null ? (
          <Typography.Text type="secondary">
            <Trans>没有读到接待策略</Trans>
          </Typography.Text>
        ) : (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Space>
              <Switch
                checked={draft.default_control_mode === 'ai'}
                onChange={(checked) =>
                  setDraft({ ...draft, default_control_mode: checked ? 'ai' : 'human' })
                }
                aria-label={t`新会话默认由 AI 接待`}
              />
              <Typography.Text>{t`新会话默认由 AI 接待`}</Typography.Text>
            </Space>
            <Space>
              <Switch
                checked={draft.understanding_during_takeover}
                onChange={(checked) =>
                  setDraft({ ...draft, understanding_during_takeover: checked })
                }
                aria-label={t`人工接管期间仍然理解对话`}
              />
              <Typography.Text>{t`人工接管期间仍然理解对话`}</Typography.Text>
            </Space>
            <Typography.Text type="secondary">
              <Trans>两个开关一起保存 —— 它们会互相作用：默认人工 + 关掉理解 = 一个什么都不做的引擎。</Trans>
            </Typography.Text>
            <Button type="primary" disabled={!dirty} loading={saving} onClick={() => void save()}>
              <Trans>保存</Trans>
            </Button>
          </Space>
        )}
      </Card>
    </Space>
  );
}
