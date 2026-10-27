import { i18n } from '@lingui/core';

import { messages } from './locales/zh-CN/messages';

/**
 * 启动时装上唯一那份目录。
 *
 * **没有语言协商，而这是 ADR-0143 §四 的直接后果**：只有一个 locale，
 * 所以「读浏览器的 `navigator.language` 再挑一个」这一步**不存在** ——
 * 写一个只会挑出同一个答案的协商逻辑，会让下一个人以为这里支持多语言。
 *
 * **`activate` 在模块加载时跑，不在组件里跑。** 放进 `useEffect` 会有一帧
 * 渲染在目录装上之前发生，而那一帧上每一句文案都是它的 id ——
 * 一屏乱码，且只在慢机器上看得见。
 */
i18n.load('zh-CN', messages);
i18n.activate('zh-CN');

export { i18n };
