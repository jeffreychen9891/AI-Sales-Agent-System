import { useSyncExternalStore } from 'react';

import { session } from '../api/session';

/**
 * 把 `session` 那个模块级 store 接进 React。
 *
 * **用 `useSyncExternalStore` 而不是 `useState` + `useEffect`**：
 * 会话状态是**在 React 之外**被改的（`client.ts` 的中间件在一次 401 之后
 * 会调 `session.clear()`），而 `useEffect` 订阅有一个窗口 ——
 * 首次渲染读到的值与订阅建立之间，store 可能已经变了。
 * 那种漏掉的更新在这里的后果是：**用户已经被登出，而界面还显示着工作台**。
 */
export function useSession(): {
  authenticated: boolean;
  mustChangePassword: boolean | null;
} {
  const authenticated = useSyncExternalStore(
    session.subscribe,
    session.isAuthenticated,
    // 服务端快照。本应用不做 SSR，但 `useSyncExternalStore` 要求给一个，
    // 而给 `false` 比给 `undefined` 诚实：没有请求上下文时没有人登录过。
    () => false,
  );
  const mustChangePassword = useSyncExternalStore(
    session.subscribe,
    session.mustChangePassword,
    () => null,
  );
  return { authenticated, mustChangePassword };
}
