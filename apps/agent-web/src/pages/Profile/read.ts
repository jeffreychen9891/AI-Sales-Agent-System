/**
 * 从 profile 正文里取值的几个小读取器。
 *
 * ## 为什么需要它们 —— 也就是为什么响应模型是松的
 *
 * `ProfileReleaseDocument` 把 `target_frames` 声明成
 * `{ [key: string]: unknown }[]`，于是读一个帧的 `goal` 拿到的是 `unknown`。
 * **那不是写模型的人偷懒。**
 *
 * profile 的形状由 `packages/contracts/schemas/merchant-profile.schema.json`
 * 定义，那是**它唯一的真相源**：引擎按它跑，publisher 按它校验，
 * 每一版 package 的 `content_hash` 覆盖的就是这份文档。
 * 把帧的内部结构再抄一遍进 pydantic，等于让同一个形状有两个真相源 ——
 * 而那两份**没有任何东西让它们保持一致**：schema 加一个字段，
 * 模型不跟着改也不会有人发现（ADR-0142 那条守卫比的是**顶层**键集合）。
 *
 * 代价就落在这里：界面要自己把 `unknown` 收窄。这几个函数把那件事
 * 集中到一处，并且**每一个都有一个安全的兜底** ——
 * 一个字段缺了、类型变了，屏幕上少一行，而不是整页白掉。
 *
 * ⚠️ **兜底是空值，不是占位文案。** 「这个帧没写目标」与「这一版读坏了」
 * 在界面上要分得开，所以判断空态由调用方做，这里只负责**不抛**。
 */

export function readText(node: unknown, key: string): string {
  if (typeof node !== 'object' || node === null) return '';
  const value = (node as Record<string, unknown>)[key];
  return typeof value === 'string' ? value : '';
}

export function readNumber(node: unknown, key: string): number | null {
  if (typeof node !== 'object' || node === null) return null;
  const value = (node as Record<string, unknown>)[key];
  return typeof value === 'number' ? value : null;
}

export function readList(node: unknown, key: string): unknown[] {
  if (typeof node !== 'object' || node === null) return [];
  const value = (node as Record<string, unknown>)[key];
  return Array.isArray(value) ? value : [];
}

export function readObject(node: unknown, key: string): Record<string, unknown> {
  if (typeof node !== 'object' || node === null) return {};
  const value = (node as Record<string, unknown>)[key];
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
}

/**
 * 一串东西里能当标签显示的那些。
 *
 * 帧的 `entry_conditions` / `exit_conditions` / `allowed_strategy_set` 在
 * schema 里都是数组，元素**可能是字符串、也可能是对象**（`content_materials`
 * 的 item 逐字写着「either a bare ref or ...」）。所以这里只取得出名字的那些，
 * **取不出名字的不编一个** —— 一个显示成 `[object Object]` 的标签
 * 比少一个标签更糟：它看起来像数据，其实是读失败。
 */
export function readLabels(items: unknown[]): string[] {
  return items
    .map((item) => {
      if (typeof item === 'string') return item;
      if (typeof item === 'object' && item !== null) {
        for (const key of ['ref', 'id', 'name', 'code']) {
          const value = (item as Record<string, unknown>)[key];
          if (typeof value === 'string') return value;
        }
      }
      return '';
    })
    .filter((label) => label !== '');
}
