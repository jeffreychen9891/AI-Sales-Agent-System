import { Trans, useLingui } from '@lingui/react/macro';
import { Alert, Button, Card, Space, Statistic, Table, Tag } from 'antd';
import { useCallback, useEffect, useState } from 'react';

import { api } from '../../api/client';
import { messageOf, newIdempotencyKey } from '../../api/envelopes';
import type { components } from '../../api/schema';

type Employee = components['schemas']['EmployeeListItem'];
type Stats = components['schemas']['EmployeeStatsResponse'];

/**
 * 员工列表。**T155 的消费方就是这一页。**
 *
 * ## 「已 purge」与「被停用」是两回事，而这一列就是那个区别
 *
 * `026` 的列注释明写这个状态「不可从墓碑地址的形状反推，也不得那样读」——
 * 而在 `purged_at` 进投影之前（本批），那是这一页唯一可行的读法。
 * 现在它是一个字段，所以这里可以**如实**显示三种状态而不是两种。
 *
 * ## 停用与清除是两个按钮，不是一个带开关的按钮
 *
 * `parity` §6.4 写死：「deactivate 与 purge 是不同 resource action、
 * 不同 route 和不同测试集合；**不得由一个布尔参数切换**」。
 * 界面上同源：两个动作、两句话、两次确认。
 */
export function EmployeesPage(): JSX.Element {
  const { t } = useLingui();
  const [rows, setRows] = useState<Employee[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  const reload = useCallback(async (): Promise<void> => {
    setLoading(true);
    setFailure(null);
    try {
      const [list, counts] = await Promise.all([
        api.GET('/employees'),
        api.GET('/employees/stats'),
      ]);
      if (list.data === undefined) {
        setFailure(messageOf(list.error, t`读取员工列表失败`));
        return;
      }
      setRows(list.data.items);
      // **统计读失败不挡住列表。** 它是一个旁证，不是这一页的主体；
      // 让一个次要读取的失败清空主表，是把一个小故障放大成一次空白页。
      setStats(counts.data ?? null);
    } catch {
      setFailure(t`读取员工列表失败`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function deactivate(employeeId: string): Promise<void> {
    setBusy(employeeId);
    try {
      const { error } = await api.POST('/employees/{employee_id}/deactivate', {
        // **幂等键走 `params.header`，不是顶层 `headers`** —— 生成的类型
        // 当场纠正了这一处：顶层 `headers` 会被送出去，但它不是这条 route
        // 声明的那个 header，于是后端收不到幂等键而请求照样发得出去。
        // 这正是「不手写 method/path」换来的第二样东西。
        params: {
          path: { employee_id: employeeId },
          header: { 'Idempotency-Key': newIdempotencyKey() },
        },
      });
      if (error !== undefined) {
        setFailure(messageOf(error, t`停用失败`));
        return;
      }
      await reload();
    } finally {
      setBusy(null);
    }
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      {stats !== null && (
        <Card>
          <Space size="large">
            <Statistic title={t`全部`} value={stats.total} />
            <Statistic title={t`在用`} value={stats.active} />
            <Statistic title={t`已停用`} value={stats.disabled} />
          </Space>
        </Card>
      )}
      {failure !== null && <Alert type="error" message={failure} showIcon />}
      <Card title={t`员工`}>
        <Table<Employee>
          rowKey="employee_id"
          loading={loading}
          dataSource={rows}
          pagination={false}
          locale={{
            // **空态说清是哪一种空。** 「还没有员工」与「读失败了」在一张
            // 空表上长得一模一样，而后者上面那条 Alert 才是真话。
            emptyText: failure === null ? t`这个租户还没有员工` : t`没有读到数据`,
          }}
          columns={[
            { title: t`邮箱`, dataIndex: 'email' },
            {
              title: t`状态`,
              key: 'status',
              render: (_, row) => <EmployeeStatus row={row} />,
            },
            { title: t`权限数`, dataIndex: 'permission_count', width: 100 },
            {
              title: '',
              key: 'actions',
              width: 120,
              render: (_, row) =>
                // 已 purge 的行没有动作 —— 它不是一个还能被操作的人。
                row.purged_at !== null ? null : (
                  <Button
                    size="small"
                    danger
                    loading={busy === row.employee_id}
                    disabled={row.status !== 'active'}
                    onClick={() => void deactivate(row.employee_id)}
                  >
                    <Trans>停用</Trans>
                  </Button>
                ),
            },
          ]}
        />
      </Card>
    </Space>
  );
}

/**
 * 三种状态，不是两种。
 *
 * **`purged_at` 压过 `status`**：一个被清除的人在 `status` 上仍然是
 * `disabled`，而那两件事不是同一件 —— 前者不可逆。
 */
function EmployeeStatus({ row }: { row: Employee }): JSX.Element {
  const { t } = useLingui();
  if (row.purged_at !== null) {
    return <Tag color="default">{t`已清除`}</Tag>;
  }
  return row.status === 'active' ? (
    <Tag color="green">{t`在用`}</Tag>
  ) : (
    <Tag color="orange">{t`已停用`}</Tag>
  );
}
