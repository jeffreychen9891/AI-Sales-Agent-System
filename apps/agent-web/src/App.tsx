import { I18nProvider } from '@lingui/react';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';

import { AppShell } from './AppShell';
import { i18n } from './i18n';
import { useSession } from './auth/useSession';
import { ChangePasswordPage } from './pages/ChangePassword/ChangePasswordPage';
import { LoginPage } from './pages/Login/LoginPage';

/**
 * 三个状态，一条优先级链。
 *
 * **没有 react-router，而这是有意的。** 今天只有三屏，而它们之间的转移
 * **不由 URL 决定，由会话状态决定** —— 一个能通过敲地址栏跳过改密页的
 * 路由，会让 `must_change_password` 那道门变成装饰。等有了第四屏
 * （工作台里的多个页面），路由再进来，那时它管的是工作台**内部**的导航。
 *
 * **优先级是写死的，不是几个独立的 `if`**：
 *
 *     没登录        → 登录页
 *     登录了但欠改密 → 改密页（**退不出去**）
 *     其余          → 工作台
 *
 * 第二条压过第三条，这一点是 K8 的界面形态：
 * 「while `must_change_password` is true, everything answers 403」。
 * 前端若不挡，用户看到的是一个每个请求都报错的工作台。
 */
export function App(): JSX.Element {
  return (
    <I18nProvider i18n={i18n}>
      <ConfigProvider
        locale={zhCN}
      // **antd 会在两个中文字之间自动插一个空格**（`登录` → `登 录`）。
      // 那不只是排版：它进了**可访问名**，于是读屏软件念出来的与按钮上
      // 写的是两样东西，而任何按名字找元素的东西（用例、自动化）都对不上。
      // 关掉它。
        button={{ autoInsertSpace: false }}
      >
        <Routes />
      </ConfigProvider>
    </I18nProvider>
  );
}

/**
 * **`ConfigProvider` 在 `App` 里而不是在 `main.tsx` 里，这一格是有意的。**
 *
 * 它原本在 `main.tsx`，而用例渲染的是 `<App />` —— 于是**测试树与生产树
 * 不是同一棵**：用例跑在没有 locale、没有按钮配置的树上。那种差异不会让
 * 用例变红，它只会让用例**证明不了它声称证明的那件事**。
 */
function Routes(): JSX.Element {
  const { authenticated, mustChangePassword } = useSession();

  if (!authenticated) return <LoginPage />;
  if (mustChangePassword === true) return <ChangePasswordPage />;
  return <AppShell />;
}
