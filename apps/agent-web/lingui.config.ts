import type { LinguiConfig } from '@lingui/conf';

/**
 * i18n 配置。**ADR-0141 决策 1：统一用 `@lingui`。**
 *
 * 形状照搬 v1 的 `agent-frontend/lingui.config.ts`（只读基线 `e514f6e`），
 * 三处差异写在这里：
 *
 * * `include` 指向 `src`，与 v1 同；
 * * **没有 `@lingui/macro` 的 runtime 依赖**：v5 的宏从 `@lingui/react/macro`
 *   取，babel 插件在 `vite.config.ts` 里挂着；
 * * **`locales` 只有 `zh-CN`**（v1 有 `en-US`）—— 见下。
 *
 * `sourceLocale: 'zh-CN'` 意味着**源码里直接写中文**，而今天没有翻译目标。
 * 这条决定了 `check-i18n.mjs` 的判据：它问的不是「有没有硬编码中文」，
 * 而是「提取出来的目录和仓里那份一不一样」。
 */
const config: LinguiConfig = {
  // **只有一个 locale，而这是一次裁定不是省事**（ADR-0143 §四，用户 2026-09-20）：
  // 今天没有任何商家要英文，而**一份永远跟不上的翻译比没有翻译更糟**。
  //
  // **那为什么还要 wrap**：`control-plane-parity.md` §4.2 要求建立
  // 「DOM marker manifest」，关键页面测试必须为零珠宝/机票/零售专属词 ——
  // **一份消息目录就是那份 manifest**。扫一份集中的目录文件，
  // 比扫整个 `src` 可靠得多（后者要区分代码、注释、类名、样式）。
  //
  // 要加第二个语言时，把它加回这个数组即可；目录结构不用动。
  locales: ['zh-CN'],
  sourceLocale: 'zh-CN',
  catalogs: [
    {
      path: 'src/locales/{locale}/messages',
      include: ['src'],
      exclude: ['**/node_modules/**'],
    },
  ],
  format: 'po',
  compileNamespace: 'ts',
};

export default config;
