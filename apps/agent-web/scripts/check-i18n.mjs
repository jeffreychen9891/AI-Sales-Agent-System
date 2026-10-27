#!/usr/bin/env node
/**
 * 仓里那份消息目录必须就是从今天的源码提取出来的那一份。
 *
 * ## 形状与另外两道闸同源
 *
 *     app        ──①── openapi.json ──②── schema.d.ts
 *     源码里的文案 ──③── src/locales/zh-CN/messages.{po,ts}
 *
 * ③ 就是本文件：**快照 → 重新提取编译 → 逐字节比 → 还原**。
 * 不一致说明有人改了文案而没有重新提取，于是目录落后于界面。
 *
 * 搬自 v1 `agent-frontend/scripts/check-i18n.mjs`（只读基线 `e514f6e`）的
 * 判据，**实现重写**（ADR-0057 决策 1：只复用视觉与交互层，不复用实现）。
 *
 * ## 为什么单语言也要这道闸
 *
 * ADR-0143 §四 只做中文，**而目录的第二个用途与语言无关**：
 * `control-plane-parity.md` §4.2 要求「建立 DOM marker manifest；
 * 关键页面测试必须为零珠宝/机票/零售专属词」——
 * **一份集中的消息目录就是那份 manifest**，而它只有在「不落后于源码」
 * 的前提下才算数。那一条由 `src/locales/catalogue.test.ts` 查，本文件
 * 保证它查的是一份**新鲜的**目录。
 *
 * ## 还原是有意的，而它有一个已知代价
 *
 * 比完把目录还原回快照 —— 本闸是**只读检查**，不该顺手改工作树
 * （一个会偷偷改文件的检查，在 CI 上表现为「莫名其妙的脏树」）。
 * ⚠️ 代价：`lingui extract` 写文件，所以本闸跑的过程中工作树**短暂是脏的**。
 * 与另一个正在读这些文件的进程并发跑，会读到中间态。今天没有那样的用法。
 */

import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath, URL } from 'node:url';

const CATALOGS = [
  'src/locales/zh-CN/messages.po',
  'src/locales/zh-CN/messages.ts',
];

/**
 * **`.po` 里带平台相关的元数据**（源码位置的路径分隔符），所以逐字节比
 * 之前先把它归一。`.ts` 是编译产物、没有路径，逐字节比。
 *
 * v1 那份的注释逐字记着同一件事：「PO source-reference metadata differs
 * across platforms; compiled catalogs are the runtime gate.」
 */
function normalise(text) {
  return text.replace(/\r\n/g, '\n').replace(/^#:.*$/gm, '#:');
}

const cli = fileURLToPath(new URL('../node_modules/@lingui/cli/dist/lingui.js', import.meta.url));

function lingui(command) {
  const run = spawnSync(process.execPath, [cli, command], {
    stdio: ['ignore', 'pipe', 'pipe'],
    encoding: 'utf-8',
  });
  if (run.status !== 0) {
    process.stderr.write(
      `i18n: FAIL -- \`lingui ${command}\` 没能跑起来。**这不是漂移，是这道闸自己坏了。**\n` +
        `  cli: ${cli}\n  exit=${run.status} error=${run.error ?? 'none'}\n` +
        `${run.stderr ?? ''}${run.stdout ?? ''}\n`,
    );
    process.exit(1);
  }
}

const before = new Map(CATALOGS.map((file) => [file, readFileSync(file, 'utf-8')]));

lingui('extract');
lingui('compile');

const changed = CATALOGS.filter(
  (file) => normalise(readFileSync(file, 'utf-8')) !== normalise(before.get(file)),
);

// 还原，无论结果如何。
for (const [file, content] of before) writeFileSync(file, content);

if (changed.length > 0) {
  process.stderr.write(
    'i18n: FAIL -- 消息目录落后于源码。\n' +
      `  不一致的文件：${changed.join(', ')}\n` +
      '  改了文案就重新提取并把目录一起提交：\n' +
      '    npm run i18n:extract && npm run i18n:compile\n',
  );
  process.exit(1);
}

const count = (before.get(CATALOGS[0]).match(/^msgid "/gm) ?? []).length;
process.stdout.write(`i18n: PASS (${count} messages)\n`);
