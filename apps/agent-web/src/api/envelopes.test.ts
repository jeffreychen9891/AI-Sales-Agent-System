import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  COMMAND_RECEIPT_KEYS,
  COMMAND_STATUSES,
  ERROR_ENVELOPE_KEYS,
  PAGE_KEYS,
  RESOURCE_REF_KEYS,
} from './envelopes';

/**
 * 手写的信封类型与 `packages/contracts/control-plane/*.schema.json` 说的是不是同一件事。
 *
 * **这条用例是 `envelopes.ts` 能手写的前提。** 没有它，那份文件就是一组
 * 与后端毫无联系的断言 —— 后端往 `ErrorEnvelope` 加一个字段，前端不会知道，
 * 而 `additionalProperties: false` 意味着那种改动是**破坏性**的。
 *
 * 判据与 `scripts/check_openapi_drift.py` 同源：**比字段集合，不比语义。**
 */

function schema(name: string): {
  required?: string[];
  properties?: Record<string, { enum?: string[] }>;
  additionalProperties?: boolean;
} {
  // **用 `process.cwd()` 而不是 `new URL(..., import.meta.url)`**：后者里
  // 那个 `.json` 会被 vite 当成一次**资源导入**去做静态分析，于是路径变成
  // 一个 `?url` 请求并撞上 `server.fs` 的边界（实测：`Denied ID ...?url`）。
  // 这里要的是一次普通的 Node 读文件，所以路径也用 Node 的方式算。
  // vitest 的 cwd 就是 `apps/agent-web`。
  const path = resolve(process.cwd(), '../../packages/contracts/control-plane', `${name}.schema.json`);
  return JSON.parse(readFileSync(path, 'utf-8')) as ReturnType<typeof schema>;
}

const CASES: ReadonlyArray<readonly [string, string[]]> = [
  ['error-envelope', ERROR_ENVELOPE_KEYS],
  ['page', PAGE_KEYS],
  ['command-receipt', COMMAND_RECEIPT_KEYS],
  ['resource-ref', RESOURCE_REF_KEYS],
];

describe('手写的信封类型与契约 schema', () => {
  it.each(CASES)('%s 的字段集合逐字相同', (name, declared) => {
    const contract = schema(name);
    expect(Object.keys(contract.properties ?? {}).sort()).toEqual(declared);
  });

  it.each(CASES)('%s 的 required 没有一项漏在类型外', (name, declared) => {
    const contract = schema(name);
    for (const field of contract.required ?? []) {
      expect(declared).toContain(field);
    }
  });

  it('CommandReceipt.status 的取值与 schema 的 enum 一致', () => {
    const contract = schema('command-receipt');
    expect(contract.properties?.['status']?.enum?.slice().sort()).toEqual(
      [...COMMAND_STATUSES].sort(),
    );
  });

  it('信封是封闭的 —— 所以上面那条「字段集合相同」才够用', () => {
    // **这条不是凑数。** 若 schema 哪天允许额外字段，「集合相同」就不再
    // 是一个足够的判据（后端可以合法地多回一些东西而前端看不见），
    // 那时要回来改的是判据本身，而不是把这条删掉。
    for (const [name] of CASES) {
      expect(schema(name).additionalProperties).toBe(false);
    }
  });
});
