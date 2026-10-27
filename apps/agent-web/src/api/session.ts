/**
 * 当前会话：access token 与「欠一次改密」。
 *
 * ## access token 放内存，不放 `localStorage`
 *
 * 它的 TTL 是 15 分钟（`FASTAGENT_ACCESS_TOKEN_TTL_SECONDS` 默认 900），
 * 而刷新靠的是一个 httpOnly cookie —— 也就是说**刷新页面不需要前端记住它**，
 * 一次 `/auth/refresh` 就能拿回来。放进 `localStorage` 只会多一个
 * 任何 XSS 都读得到的副本，换不到任何东西。
 *
 * ## 「欠一次改密」这一格，以及它为什么长这样
 *
 * **T130**：被管理员 reset 过的人调 `POST /auth/login` 拿到 **200 和一个只能用在
 * 一条 route 上的 token**，而**登录响应体里没有任何字段说这件事**
 * （只有 `access_token` / `token_type` / `expires_in`）。唯一的发现路径是
 * 随后任意一条 route 返回的 **403 `PASSWORD_CHANGE_REQUIRED`**。
 *
 * T130 的到期条件写死是「**第一个真实前端接入登录流程之前**」——
 * 就是这一批。排期批 3 要在后端补上那个字段。
 *
 * **在补上之前，这里就是那条「唯一的发现路径」的实现**：任何一条请求
 * 撞到那个 code，就把这一格点亮，由路由把人送去改密页。
 * 写在 session 而不是写在登录页，是因为它**不是登录的结果**，
 * 是任意一次请求的结果 —— 把它放进登录页会让「刷新页面之后」这条路径漏掉。
 */

import type { AccessGrant } from './client';

/** 后端对「你欠一次改密」用的那个 code，逐字。 */
export const PASSWORD_CHANGE_REQUIRED = 'PASSWORD_CHANGE_REQUIRED';

type Listener = () => void;

interface SessionState {
  accessToken: string | null;
  /** `null` = 还不知道。**不是 `false`** —— 见下。 */
  mustChangePassword: boolean | null;
}

/**
 * **初值是 `null` 而不是 `false`，这一格是有意的。**
 *
 * `false` 会被读成「我们问过了，不欠」，而事实是**我们今天问不出来**
 * （T130：登录不说）。`null` 说的是「还没有任何证据」——
 * 等后端补上那个字段，它会在登录那一刻变成真正的 `true` / `false`，
 * 而在那之前它只会被那个 403 从 `null` 推到 `true`。
 *
 * 这与本仓对 `commitments` 那个 `0` 的裁定同一个形状：
 * **一个会误导人的真值，比一个说「不知道」的空值更贵。**
 */
let state: SessionState = { accessToken: null, mustChangePassword: null };

const listeners = new Set<Listener>();

function emit(): void {
  for (const listener of listeners) listener();
}

export const session = {
  accessToken(): string | null {
    return state.accessToken;
  },

  mustChangePassword(): boolean | null {
    return state.mustChangePassword;
  },

  isAuthenticated(): boolean {
    return state.accessToken !== null;
  },

  grant(access: AccessGrant): void {
    state = { ...state, accessToken: access.access_token };
    emit();
  },

  /** 任意一条请求撞到 403 `PASSWORD_CHANGE_REQUIRED` 时调它。 */
  markPasswordChangeRequired(): void {
    if (state.mustChangePassword === true) return;
    state = { ...state, mustChangePassword: true };
    emit();
  },

  /** 改密成功之后。 */
  markPasswordChanged(): void {
    state = { ...state, mustChangePassword: false };
    emit();
  },

  clear(): void {
    state = { accessToken: null, mustChangePassword: null };
    emit();
  },

  subscribe(listener: Listener): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};

/**
 * 从一个错误信封里认出「欠一次改密」。
 *
 * **只认 `code`，不认 message**：信封的 `code` 是机器可读的那一半
 * （T130 的审查方实测 20 个 route+method 组合全部返回该 code），
 * 而 message 是给人看的、会改的。
 */
export function isPasswordChangeRequired(body: unknown): boolean {
  if (typeof body !== 'object' || body === null) return false;
  const envelope = body as { error?: { code?: unknown }; code?: unknown };
  return (
    envelope.error?.code === PASSWORD_CHANGE_REQUIRED || envelope.code === PASSWORD_CHANGE_REQUIRED
  );
}
