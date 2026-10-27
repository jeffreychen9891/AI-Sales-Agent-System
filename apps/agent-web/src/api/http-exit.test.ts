import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, resolve, sep } from 'node:path';

import { describe, expect, it } from 'vitest';

/**
 * 这个应用只有**一个** HTTP 出口，而在本文件之前那只是一句约定。
 *
 * `control-plane-parity.md` §4.3 定死两条：
 *
 * > 前端只调用 generated client 或明确的 typed adapter；
 * > 禁止页面/组件直接调用 raw `request/axios`。
 * > WebSocket 使用独立 typed adapter；页面只订阅允许的 topic/event，
 * > 不拼 URL、不携带长期 token。
 *
 * **今天全仓 0 违规 —— 而那正是写这条用例的时候。** 一条零违规的规矩
 * 离被悄悄破坏只差一个 PR：下一个页面里的一句 `fetch('/api/...')`
 * 能编译、能跑、能返回 200，**只是它绕过了授权头、绕过了 401 之后的
 * 单飞刷新、也绕过了那份从 OpenAPI 生成的类型**。
 * 而绕过去的那一层正是 §4.3 要求「只有一处」的东西。
 *
 * 本仓的说法是：**一个「多传一个不报错」的地方必须有一条会变红的用例**
 * （`CLAUDE.md` §三）。这是那条用例。
 *
 * ## 判据：源码扫描，而它的边界写在这里
 *
 * 扫 `src/**` 的 `.ts` / `.tsx`，**排除 `src/api/`**（那里就是出口本身）
 * 与 `*.test.*`（用例要替换 `globalThis.fetch` 才能证明请求真的穿过那一层）。
 *
 * **它看不见运行期。** 一句 `const f = globalThis['fet' + 'ch']` 躲得过去 ——
 * 本仓记过「源码扫描看不见代码跑没跑」。所以这条用例挡的是**顺手写的**
 * 那一种，不是刻意绕的那一种；而顺手写的那一种正是会真的发生的那一种。
 */

const SRC = resolve(process.cwd(), 'src');

/** 出口本身。**唯一的豁免，而且它是一个目录不是一份名单。** */
const EXIT_DIR = `api${sep}`;

const FORBIDDEN: ReadonlyArray<{ pattern: RegExp; why: string }> = [
  {
    pattern: /(?<![.\w])fetch\s*\(/,
    why: '直接调 fetch —— 绕过 src/api/client.ts 的授权头与 401 单飞刷新',
  },
  { pattern: /\baxios\b/, why: 'axios —— v2 不用它，HTTP 只走 generated client' },
  { pattern: /\bXMLHttpRequest\b/, why: 'XMLHttpRequest —— 同上' },
  {
    pattern: /new\s+WebSocket\s*\(/,
    why: 'new WebSocket —— parity §9.1 要的是 typed adapter + 一次性 ticket，页面不拼 URL',
  },
];

function sourceFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      found.push(...sourceFiles(full));
      continue;
    }
    if (!/\.tsx?$/.test(entry) || /\.test\./.test(entry)) continue;
    found.push(full);
  }
  return found;
}

export function offenders(root: string = SRC): string[] {
  const hits: string[] = [];
  for (const file of sourceFiles(root)) {
    const rel = relative(root, file);
    if (rel.startsWith(EXIT_DIR)) continue;
    const lines = readFileSync(file, 'utf-8').split('\n');
    lines.forEach((line, index) => {
      // 注释里提到它们不算 —— 本文件自己的 docstring 就提到了 `axios`。
      const code = line.replace(/\/\/.*$/, '').replace(/^\s*\*.*$/, '');
      for (const rule of FORBIDDEN) {
        if (rule.pattern.test(code)) {
          hits.push(`${rel}:${index + 1}: ${rule.why}`);
        }
      }
    });
  }
  return hits;
}

describe('这个应用只有一个 HTTP 出口', () => {
  it('src/api 之外没有任何直接的网络调用', () => {
    const hits = offenders();
    expect(hits, `这些地方绕过了 src/api/client.ts：\n  ${hits.join('\n  ')}`).toEqual([]);
  });

  it('判据自己不是空的 —— 喂它一段已知违规，它要认得出来', () => {
    // **不落盘，直接问判据。** 往真树里写一个违规文件再删掉，
    // 是本仓记过的那种「还原不干净」的做法。
    const samples = [
      "const r = await fetch('/api/employees');",
      "import axios from 'axios';",
      "const ws = new WebSocket('wss://x');",
    ];
    for (const sample of samples) {
      expect(FORBIDDEN.some((rule) => rule.pattern.test(sample))).toBe(true);
    }
    // 而这些**不该**命中：`api.GET` 是出口自己的方法名，注释不算。
    for (const clean of ["await api.GET('/employees');", '// 这里原本写的是 fetch(', 'prefetch(x)']) {
      const code = clean.replace(/\/\/.*$/, '');
      expect(FORBIDDEN.some((rule) => rule.pattern.test(code))).toBe(false);
    }
  });
});
