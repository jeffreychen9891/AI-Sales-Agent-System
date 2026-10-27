import createClient, { type Middleware } from 'openapi-fetch';

import type { paths } from './schema';
import { session } from './session';

/**
 * 整个应用**唯一**的 HTTP 出口。
 *
 * `control-plane-parity.md` §4.3 定死两件：
 *
 * > 生成 TypeScript client 后，两前端不得手写 method/path。
 * > refresh 由 auth client 封装；页面、拦截器和业务 service 不各自实现 rotation。
 *
 * 第一件由 `paths`（从 `packages/contracts/openapi.json` 生成）在类型层担保：
 * 拼错一个路径是编译错误，不是运行期 404。
 * 第二件由本文件的 `refreshOnce` 担保 —— 全仓只有这一处会调 `/auth/refresh`。
 *
 * ## ⚠️ 响应体**没有**类型，而那不是本文件的疏漏
 *
 * 实测（2026-09-20，产物 53 path）：**59 条 2xx 响应里带具体 schema 的是 0 条**，
 * 每一条都是 `{"type":"object","additionalProperties":true}` ——
 * 因为 route handler 全部标注成 `-> dict[str, object]`（**72 处**，实测
 * `grep -cE '\) -> dict\[str, object\]:'`；初稿写 89，那是一个更宽的图案
 * 把别的函数也数进去了 —— 本仓的规矩是数字要核过再写）。
 *
 * 所以 §4.3 那句「OpenAPI 是 HTTP 合同权威」**对请求成立、对响应不成立**：
 * `paths` 管住了 method / path / 请求体，**响应形状要在别处钉**。
 * 本仓的钉法见 `src/api/shapes.ts` 与 `packages/contracts/response-shapes.json`，
 * 登记 **T378**。在那之前，**任何 `as SomeType` 都是一次没有守卫的断言**。
 */

/** 后端地址。**一处读，不散落**；开发与生产是同一个答案。 */
const BASE_URL = (import.meta.env['VITE_API_BASE_URL'] as string | undefined) ?? '/api';

/**
 * `POST /auth/refresh` 的返回。**手写的**，理由见上面那段。
 *
 * 逐字对应 `services/api/src/fastagent_v2/api/app.py` 里
 * `refresh_session` 与 `login` 的 `return {...}`：三个键，一个不多。
 */
export interface AccessGrant {
  access_token: string;
  token_type: string;
  expires_in: number;
}

function isAccessGrant(value: unknown): value is AccessGrant {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate['access_token'] === 'string' &&
    typeof candidate['token_type'] === 'string' &&
    typeof candidate['expires_in'] === 'number'
  );
}

/**
 * 正在进行的那一次刷新。**并发的 401 共享它。**
 *
 * **这不是省一次网络往返，这是正确性。** 后端的 refresh 是一次性 rotation
 * **且带 reuse detection**（`app.py` 的 `_revoke_refresh_family`：一个被重放的
 * token 会把**整条链**撤掉）。两个请求同时 401、各自去刷新，第二个拿的就是
 * 一个已经轮换过的 token —— 后端会正确地把它判成重放，然后**把用户整个踢下线**。
 *
 * 也就是说：少了这个单飞，页面上同时发两个请求就会偶发登出，
 * 而它看起来像「会话随机失效」。
 */
let inFlightRefresh: Promise<string | null> | null = null;

async function refreshOnce(): Promise<string | null> {
  inFlightRefresh ??= (async () => {
    try {
      const response = await fetch(`${BASE_URL}/auth/refresh`, {
        method: 'POST',
        // refresh token 是 httpOnly + SameSite=strict 的 cookie（`app.py:825`），
        // 前端**看不见也不该看见**它，只要让浏览器带上。
        credentials: 'include',
        headers: { 'content-type': 'application/json' },
      });
      if (!response.ok) return null;
      const body: unknown = await response.json();
      if (!isAccessGrant(body)) return null;
      session.grant(body);
      return body.access_token;
    } catch {
      // 网络错误与「刷新被拒」在这里**同一处置**：都回 `null`，
      // 由调用方决定是登出还是重试。区分它们要一个本文件答不了的问题
      //（「后端挂了」还是「你被登出了」），而猜错的代价是把一次网络抖动
      // 变成一次登出。
      return null;
    } finally {
      inFlightRefresh = null;
    }
  })();
  return inFlightRefresh;
}

/** 已经重试过一次的请求，不再重试第二次。 */
const RETRIED = Symbol('retried');

const authMiddleware: Middleware = {
  async onRequest({ request }) {
    const token = session.accessToken();
    if (token) request.headers.set('authorization', `Bearer ${token}`);
    return request;
  },

  async onResponse({ request, response }) {
    if (response.status !== 401) return response;
    // `PASSWORD_CHANGE_REQUIRED` 走的是 403，不是这里；见 `session.ts`。
    if (Reflect.get(request, RETRIED) === true) {
      session.clear();
      return response;
    }
    const token = await refreshOnce();
    if (!token) {
      session.clear();
      return response;
    }
    const retry = request.clone();
    retry.headers.set('authorization', `Bearer ${token}`);
    Reflect.set(retry, RETRIED, true);
    return fetch(retry);
  },
};

export const api = createClient<paths>({
  baseUrl: BASE_URL,
  credentials: 'include',
  // **每次调用去取 `globalThis.fetch`，不是建客户端那一刻取一次。**
  //
  // `openapi-fetch` 的默认值是 `globalThis.fetch`，而它在 `createClient`
  // 那一刻求值 —— 这个模块是在应用启动时 import 的，于是那个引用被**冻**住了。
  // 后果有两层：
  //
  // * 任何在此之后替换 `globalThis.fetch` 的东西（用例里的替身、一个迟到的
  //   polyfill、一层遥测包装）**对它无效**，而且是**静默无效** ——
  //   第一版的用例就是这样：替身装好了，`fetch` 一次都没被调用，
  //   页面停在登录页，而报错只说「找不到『已登录』」；
  // * 它让「这个应用怎么发 HTTP」这件事有两个答案：模块加载时的那个，
  //   和此刻 `globalThis` 上的那个。
  //
  // 一层薄转发让它只有一个答案。
  fetch: (request) => globalThis.fetch(request),
});

api.use(authMiddleware);

/** 只给用例用：把单飞状态清掉，免得一条用例的刷新泄进下一条。 */
export function __resetRefreshStateForTests(): void {
  inFlightRefresh = null;
}
