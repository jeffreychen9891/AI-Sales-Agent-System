import { useLingui } from '@lingui/react/macro';
import { Layout, Menu, Typography } from 'antd';
import { useState } from 'react';

import { EmployeesPage } from './pages/Employees/EmployeesPage';
import { ProfilePage } from './pages/Profile/ProfilePage';
import { RolesPage } from './pages/Roles/RolesPage';
import { SettingsPage } from './pages/Settings/SettingsPage';
import { WorkbenchPage } from './pages/Workbench/WorkbenchPage';

/**
 * 登录之后的壳。
 *
 * **仍然没有 `react-router`，而这一格是想过的。** 上一批写着「等有了第四屏，
 * 路由再进来」—— 现在有了三屏，而它们之间**没有需要被分享、被收藏、被
 * 后退键还原的状态**：每一屏都是「打开就重新拉一次」。
 *
 * 真正需要 URL 的是**会话工作台**（一条会话的链接要能发给同事），
 * 而它在批 6。路由跟它一起进来，那时它管的是工作台内部的导航 ——
 * 现在先加一个路由器，等于为三个不需要地址的页面背上一套地址方案。
 *
 * **这条写下来是因为它会被重议**：下一个人看到 `useState` 当导航会以为
 * 是偷懒。它不是，它有一个到期条件：**批 6 工作台进来时换掉。**
 */
type Tab = 'workbench' | 'profile' | 'employees' | 'roles' | 'settings';

/**
 * **标签文案在组件里取，不在模块级常量里。**
 *
 * 模块级只留 id。一个在 import 那一刻就算好的中文串，`useLingui` 换语言时
 * 不会跟着变 —— 单语言的今天看不出来，加第二个语言的那天才会，
 * 而那时它会表现成「某几个字没翻译」而不是一处明显的故障。
 */
//: 话术紧挨工作台：坐席在工作台上看到 AI 说了什么，
//: 下一个问题就是「它为什么这么说」—— 答案在话术里。
const TAB_IDS: readonly Tab[] = ['workbench', 'profile', 'employees', 'roles', 'settings'];

export function AppShell(): JSX.Element {
  const { t } = useLingui();
  // **工作台是默认那一屏**：它是这个应用存在的理由，
  // 其余三屏是配置。打开就落在配置页上，等于把主次弄反。
  const [tab, setTab] = useState<Tab>('workbench');

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Header style={{ display: 'flex', alignItems: 'center', gap: 24 }}>
        <Typography.Text strong style={{ color: '#fff' }}>
          {t`商家工作台`}
        </Typography.Text>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[tab]}
          onSelect={(info) => setTab(info.key as Tab)}
          items={TAB_IDS.map((key) => ({
            key,
            label:
              key === 'workbench'
                ? t`工作台`
                : key === 'profile'
                  ? t`话术`
                  : key === 'employees'
                    ? t`员工`
                    : key === 'roles'
                      ? t`角色与权限`
                      : t`设置`,
          }))}
          style={{ flex: 1, minWidth: 0 }}
        />
      </Layout.Header>
      <Layout.Content style={{ padding: 24 }}>
        {tab === 'workbench' && <WorkbenchPage />}
        {tab === 'profile' && <ProfilePage />}
        {tab === 'employees' && <EmployeesPage />}
        {tab === 'roles' && <RolesPage />}
        {tab === 'settings' && <SettingsPage />}
      </Layout.Content>
    </Layout>
  );
}
