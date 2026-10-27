import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

/**
 * 界面文案里不许有商家专属词 —— 而这份目录就是 parity §4.2 要的那份 manifest。
 *
 * ## 这条用例是「单语言也 wrap」那个裁定的兑现物
 *
 * ADR-0143 §四 只做中文，所以 wrap 换不到翻译。**它换到的是这个**：
 *
 * > `control-plane-parity.md` §4.2：建立 DOM marker manifest；
 * > 第三合成 profile 的构建与**关键页面测试必须为零珠宝/机票/零售专属词**。
 *
 * **一份集中的消息目录就是那份 manifest。** 扫一份目录文件，比扫整个 `src`
 * 可靠得多 —— 后者要区分代码、注释、类名、样式，而其中任何一种误判都会
 * 让这条检查要么假红要么假绿。
 *
 * 目录**不落后于源码**由 `scripts/check-i18n.mjs` 保证；本文件只问
 * 「这份目录里有没有不该有的词」。两件事，两道闸。
 *
 * ## 这些词从哪来
 *
 * 不是凭空列的。`control-plane-parity.md` §4.2 点名了三个租户的泄漏：
 * **F-17**（珠宝：样品阶段、三级价格、`min_quote` 必填）、
 * **D-10**（机票：IATA 资料类型）、**C-19**（商家占位文案）。
 * v1 把它们写死在通用 UI 里，而 v2 的同一批字段要由
 * effective merchant profile 驱动 —— 也就是说它们**永远不该出现在文案里**。
 */

const CATALOGUE = resolve(process.cwd(), 'src/locales/zh-CN/messages.po');

/**
 * **词表按来源分组，每组带坐标。**
 *
 * 一张没有出处的禁词表，下一个人只能整张照搬或者整张删掉；
 * 带了出处，他可以逐条判断哪一条今天还成立。
 */
const FORBIDDEN: ReadonlyArray<{ word: string; source: string }> = [
  { word: '珠宝', source: 'parity §4.2 · F-17' },
  { word: '样品阶段', source: 'parity §4.2 · F-17' },
  { word: '三级价格', source: 'parity §4.2 · F-17' },
  { word: 'min_quote', source: 'parity §4.2 · F-17' },
  { word: '机票', source: 'parity §4.2 · D-10' },
  { word: 'IATA', source: 'parity §4.2 · D-10' },
  { word: '航班', source: 'parity §4.2 · D-10' },
  { word: '零售', source: 'parity §4.2 · C-19' },
];

/** 只看 `msgid`，不看注释与源码位置 —— 那些里面出现文件名是正常的。 */
function messageIds(catalogue: string): string[] {
  return [...catalogue.matchAll(/^msgid "(.*)"$/gm)]
    .map((match) => match[1] ?? '')
    .filter((id) => id !== '');
}

describe('消息目录（= parity §4.2 的 DOM marker manifest）', () => {
  const catalogue = readFileSync(CATALOGUE, 'utf-8');
  const ids = messageIds(catalogue);

  it('目录不是空的 —— 否则下面那条恒绿', () => {
    // **下界写成一个真实的数而不是 `> 0`。** 一份只剩一条的目录，
    // 在 `> 0` 的判据下照样绿，而那恰恰是「提取跑了一半」的样子。
    expect(ids.length).toBeGreaterThanOrEqual(50);
  });

  it.each(FORBIDDEN)('没有商家专属词：$word（$source）', ({ word }) => {
    const hits = ids.filter((id) => id.toLowerCase().includes(word.toLowerCase()));
    expect(hits, `这些文案里有「${word}」：\n  ${hits.join('\n  ')}`).toEqual([]);
  });

  it('判据自己认得出违规 —— 否则上面那几条只是在数空集', () => {
    const fake = ['一句正常的文案', '珠宝分级说明'];
    const caught = fake.filter((id) =>
      FORBIDDEN.some((entry) => id.toLowerCase().includes(entry.word.toLowerCase())),
    );
    expect(caught).toEqual(['珠宝分级说明']);
  });
});
