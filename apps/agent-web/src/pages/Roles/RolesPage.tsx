import { useLingui } from '@lingui/react/macro';
import { Alert, Card, Space, Table, Tag } from 'antd';
import { useEffect, useState } from 'react';

import { api } from '../../api/client';
import { messageOf } from '../../api/envelopes';
import type { components } from '../../api/schema';

type Role = components['schemas']['RoleItem'];
type Permission = components['schemas']['PermissionItem'];

/**
 * 角色与权限目录（只读）。
 *
 * ## 为什么第一版只读
 *
 * 改权限的那条 route（`PUT /roles/{id}/permissions`）**整体替换**权限数组，
 * 而它有两条会拒的路径：系统角色被拒、最后一个能发权限的人被拒
 * （`replace_role_permissions` 的 docstring）。一个「勾选即保存」的界面
 * 会让这两条拒绝在用户看来像是随机失败。
 *
 * **写那一半要先想清楚交互**（整表提交 + 两条拒绝各自的说法），
 * 而这一批的判据是「有没有真实数据源」，不是「能不能画出来」。
 * 所以先交读，并把写那一半**明说在界面上**，而不是给一个点不动的按钮。
 *
 * ## 权限目录是平台级的，而商家读得到
 *
 * `GET /permissions` 的 `tenant_owned=false` 说的是**这份目录每个租户
 * 看到同一份、谁也改不了**，不是「商家读不到」——
 * `permission.list` 可以授给租户角色。我在盘点 §6 里把这条归错过一次，
 * 当天更正（把「资源的 scope」读成了「谁能读」）。
 */
export function RolesPage(): JSX.Element {
  const { t } = useLingui();
  const [roles, setRoles] = useState<Role[]>([]);
  const [permissions, setPermissions] = useState<Permission[]>([]);
  const [failure, setFailure] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    void (async () => {
      try {
        const [roleList, permissionList] = await Promise.all([
          api.GET('/roles'),
          api.GET('/permissions'),
        ]);
        if (roleList.data === undefined) {
          setFailure(messageOf(roleList.error, t`读取角色失败`));
          return;
        }
        setRoles(roleList.data.items);
        setPermissions(permissionList.data?.items ?? []);
      } catch {
        setFailure(t`读取角色失败`);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      {failure !== null && <Alert type="error" message={failure} showIcon />}
      <Alert
        type="info"
        showIcon
        message={t`这一版只能看，不能改`}
        description={t`改权限是整表替换，且系统角色与「最后一个能发权限的人」会被拒绝 —— 那两条拒绝各自需要一句说法，排在下一批。`}
      />
      <Card title={t`角色`}>
        <Table<Role>
          rowKey="role_id"
          loading={loading}
          dataSource={roles}
          pagination={false}
          locale={{ emptyText: failure === null ? t`还没有角色` : t`没有读到数据` }}
          columns={[
            { title: t`名称`, dataIndex: 'name' },
            {
              title: t`说明`,
              dataIndex: 'description',
              render: (value: string | null) => value ?? '—',
            },
            {
              title: t`类型`,
              dataIndex: 'is_system',
              width: 100,
              render: (value: boolean) => (value ? <Tag>{t`系统`}</Tag> : <Tag color="blue">{t`自建`}</Tag>),
            },
            {
              title: t`权限数`,
              dataIndex: 'permissions',
              width: 90,
              render: (value: string[]) => value.length,
            },
          ]}
        />
      </Card>
      <Card title={t`权限目录（${permissions.length}）`}>
        <Table<Permission>
          rowKey="action"
          size="small"
          dataSource={permissions}
          pagination={{ pageSize: 10 }}
          locale={{ emptyText: t`没有读到权限目录` }}
          columns={[
            { title: t`动作`, dataIndex: 'action' },
            { title: t`资源`, dataIndex: 'resource' },
            { title: t`动词`, dataIndex: 'verb', width: 100 },
            {
              title: t`归属`,
              dataIndex: 'tenant_owned',
              width: 100,
              render: (value: boolean) => (value ? t`租户` : t`平台`),
            },
          ]}
        />
      </Card>
    </Space>
  );
}
