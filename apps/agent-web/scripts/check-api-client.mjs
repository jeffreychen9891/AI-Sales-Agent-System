#!/usr/bin/env node
/**
 * `src/api/schema.d.ts` 必须就是从 `packages/contracts/openapi.json` 生成的那一份。
 *
 * ## 这是链条上的第二道闸，不是第一道的重复
 *
 * 第一道（`scripts/check_openapi_drift.py`，跑在后端门禁里）问的是
 * **「产物和 app 一致吗」**。本条问的是**「生成物和产物一致吗」**。
 * 两道都要有，因为链条是三节：
 *
 *     app ──①── openapi.json ──②── schema.d.ts ──③── 页面
 *
 * 少了 ②，一个人可以改了 route、重新导出产物、**忘了重新生成类型**，
 * 而前端照样编译通过 —— 因为它查的是那份过期的 `schema.d.ts`。
 * ③ 由 TypeScript 自己担保。
 *
 * ## 判据：重新生成一次，逐字节比
 *
 * 与 ① 同源。不比语义 —— 一个语义比对器要自己回答「什么算等价」，
 * 而那正是本仓记过的那类自造校验器。
 *
 * ## ⚠️ 这条闸自己坏过两次，两次都是「跑不起来」而不是「判错了」
 *
 * 1. 第一版走 `npx openapi-typescript`：`spawnSync` 不带 shell，在 Windows 上
 *    找不到 `npx`，于是**两个方向都 FAIL**；
 * 2. 第二版走 `require.resolve('openapi-typescript/bin/cli.js')`：该包的
 *    `exports` 不暴露这个子路径，解析成了一个不存在的 `cli.mjs`。
 *
 * 两次的共同形状：**它报的是自己的故障，而读日志的人会读成「漂移了」。**
 * 所以现在生成器的路径是直接拼出来的，而且**失败信息里第一句就说清
 * 「这不是漂移」**。一条恒红的闸和一条恒绿的闸一样没用，只是坏得吵一点。
 */

import { spawnSync } from 'node:child_process';
import { readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, URL } from 'node:url';

const GENERATOR = fileURLToPath(
  new URL('../node_modules/openapi-typescript/bin/cli.js', import.meta.url),
);
const GENERATED = 'src/api/schema.d.ts';
const SOURCE = '../../packages/contracts/openapi.json';

const scratch = join(tmpdir(), `agent-web-schema-${process.pid}.d.ts`);

try {
  const run = spawnSync(process.execPath, [GENERATOR, SOURCE, '-o', scratch], {
    stdio: ['ignore', 'pipe', 'pipe'],
    encoding: 'utf-8',
  });

  if (run.status !== 0) {
    process.stderr.write(
      'api client: FAIL -- 生成器没能跑起来。**这不是漂移，是这条闸自己坏了。**\n' +
        `  generator: ${GENERATOR}\n` +
        `  exit=${run.status} error=${run.error ?? 'none'}\n` +
        `${run.stderr ?? ''}${run.stdout ?? ''}\n`,
    );
    process.exit(1);
  }

  const committed = readFileSync(GENERATED, 'utf-8');
  const current = readFileSync(scratch, 'utf-8');

  if (committed === current) {
    const paths = (committed.match(/^ {4}"\//gm) ?? []).length;
    process.stdout.write(`api client: PASS (${paths} paths)\n`);
    process.exit(0);
  }

  // **报首个不同的那一行，不打印整份 diff。** 这份文件 5000 行，
  // 整份 diff 在门禁日志里等于没有报告 —— 与 `check_openapi_drift.py` 同一条理由。
  const a = committed.split('\n');
  const b = current.split('\n');
  const first = a.findIndex((line, i) => line !== b[i]);
  process.stderr.write(
    'api client: FAIL -- src/api/schema.d.ts 与 openapi.json 不一致。\n' +
      `  首个不同在第 ${first + 1} 行：\n` +
      `    仓里: ${a[first] ?? '<文件更短>'}\n` +
      `    现在: ${b[first] ?? '<文件更短>'}\n` +
      `  行数 仓里 ${a.length} / 现在 ${b.length}\n` +
      '  重新生成并把它一起提交：\n' +
      '    npm run api:generate\n',
  );
  process.exit(1);
} finally {
  rmSync(scratch, { force: true });
}
