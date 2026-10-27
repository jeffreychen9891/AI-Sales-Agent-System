import { fileURLToPath, URL } from 'node:url';

import { lingui } from '@lingui/vite-plugin';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/**
 * `apps/agent-web` 的构建配置。
 *
 * **`babel-plugin-lingui-macro` 挂在 react 插件上，不是单独一条。** lingui v5 的
 * 宏（`<Trans>` / `useLingui`）是在 babel 层展开的，而 `@vitejs/plugin-react`
 * 已经在跑 babel —— 再挂一条独立的 babel 流水线会让同一份源码被转两遍。
 *
 * **没有 proxy 配置，而这是有意的。** 开发时后端地址走 `VITE_API_BASE_URL`，
 * 由 `src/api/client.ts` 一处读。写成 vite proxy 会让「前端怎么找到后端」
 * 在开发和生产两种模式下有两个答案，而 `parity` §4.3 要的是一个。
 */
export default defineConfig({
  plugins: [
    react({
      babel: {
        plugins: ['@lingui/babel-plugin-lingui-macro'],
      },
    }),
    lingui(),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    fs: {
      // 用例要读 `packages/contracts/**` 里的契约 schema —— 那是**仓内**文件，
      // 只是不在这个 npm 包的根下。放开到仓根，不放开更多。
      allow: ['../..'],
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    // **`restoreMocks` 开着**：一条用例替换掉 `fetch` 之后忘了还原，
    // 下一条会拿到上一条的替身而且**照样绿** —— 那是本仓记过的那种
    // 「看起来在守」的用例。
    restoreMocks: true,
    clearMocks: true,
    env: {
      // **jsdom 的 `new Request()` 不接受相对地址。** 生产里 `'/api'`
      // 是对的（同源反代），但在用例里它会让请求在发出之前就抛。
      // 给一个绝对的 base，**只改测试环境**，生产那条路径一字不动。
      VITE_API_BASE_URL: 'http://localhost/api',
    },
  },
});
