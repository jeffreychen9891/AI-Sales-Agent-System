/**
 * 控制面的**通用信封**。这些形状是有权威的，而且权威不在 OpenAPI 里。
 *
 * ## 三层合同，三个来源（这一段是本文件存在的理由）
 *
 * | 这一层 | 权威在哪 | 前端怎么拿到 |
 * |---|---|---|
 * | method / path / 请求体 | `packages/contracts/openapi.json` | **生成**（`src/api/schema.d.ts`） |
 * | 通用信封（`Page<T>` / `ErrorEnvelope` / `CommandReceipt` …） | `packages/contracts/control-plane/*.schema.json`（7 份） | **本文件**，由 `envelopes.test.ts` 逐字段比对那 7 份 schema |
 * | 信封里那个 `T`（每条 route 的领域载荷） | **今天没有权威** | 手写，见 `shapes.ts`（登记 **T378**） |
 *
 * **第二层为什么不生成而是手写 + 比对**：那 7 份 schema 一共二十几个字段、
 * 几乎不变，再引一个 `json-schema-to-typescript` 换不到什么；
 * 而一条「手写的类型和 schema 说的是不是同一件事」的用例，
 * 既守住了漂移，又**在 schema 变了的时候指着这里说「改我」**。
 * 判据与 `check_openapi_drift.py` 同源：不比语义，比字段集合。
 */

/**
 * `ErrorEnvelope`。**`additionalProperties: false`** —— 后端那条
 * `validate_control_plane_contract` 保证了这一点，所以这里可以写成封闭形状。
 */
export interface ErrorEnvelope {
  code: string;
  message: string;
  request_id: string;
  field_errors: FieldError[];
  retryable: boolean;
}

export interface FieldError {
  field: string;
  code: string;
  message: string;
}

/**
 * `Page<T>`。
 *
 * **`has_more` 与 `next_cursor` 都照收，不自己推**：后端
 * `_page_or_refuse` 是从 `next_cursor` 推出 `has_more` 的，
 * 而前端再推一遍就成了第二个真相源 —— 本仓反复拒绝的那种。
 */
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
  as_of: string;
}

/** `CommandReceipt.status` 的封闭取值。 */
export type CommandStatus = 'accepted' | 'succeeded' | 'failed' | 'unknown';

export interface ResourceRef {
  resource_type: string;
  resource_id: string;
  tenant_id: string;
}

export interface CommandReceipt {
  command_id: string;
  status: CommandStatus;
  resource_ref: ResourceRef;
  accepted_at: string;
  idempotency_key: string;
  correlation_id: string;
}

/**
 * 每个 create 命令都要带的幂等键。
 *
 * **在客户端生成，而且每次重试用同一个** —— 这是它的全部意义。
 * 每次重试换一个新键，等于告诉后端「这是一条新命令」，
 * 而那正是幂等键要防的事。
 */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

export function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate['code'] === 'string' &&
    typeof candidate['message'] === 'string' &&
    typeof candidate['request_id'] === 'string' &&
    Array.isArray(candidate['field_errors']) &&
    typeof candidate['retryable'] === 'boolean'
  );
}

/**
 * 运行期能看到的字段名表。**与上面那些类型绑死。**
 *
 * TypeScript 的类型在运行期是擦掉的，所以一条「手写类型与 schema 一致」的
 * 用例需要一份运行期的键表 —— 而一份**自由写的**键表会和类型各走各的，
 * 那就成了本仓记过的「数错了的守卫看起来在守」。
 *
 * `Record<keyof T, true>` 让这件事由编译器管：**少一个键、多一个键都是编译错误**。
 * 于是三者串成一条链：`*.schema.json` ←（`envelopes.test.ts`）→ 键表 ←（编译器）→ 类型。
 */
function keysOf<T extends object>(shape: Record<keyof T, true>): string[] {
  return Object.keys(shape).sort();
}

export const ERROR_ENVELOPE_KEYS = keysOf<ErrorEnvelope>({
  code: true,
  message: true,
  request_id: true,
  field_errors: true,
  retryable: true,
});

export const PAGE_KEYS = keysOf<Page<unknown>>({
  items: true,
  next_cursor: true,
  has_more: true,
  as_of: true,
});

export const COMMAND_RECEIPT_KEYS = keysOf<CommandReceipt>({
  command_id: true,
  status: true,
  resource_ref: true,
  accepted_at: true,
  idempotency_key: true,
  correlation_id: true,
});

export const RESOURCE_REF_KEYS = keysOf<ResourceRef>({
  resource_type: true,
  resource_id: true,
  tenant_id: true,
});

export const COMMAND_STATUSES: readonly CommandStatus[] = [
  'accepted',
  'succeeded',
  'failed',
  'unknown',
];

/**
 * 从一个错误值里取出给人看的那句话，取不到就用兜底。
 *
 * **收 `unknown` 而不是 `ErrorEnvelope`，这一格是被类型逼出来的。**
 * 生成的 client 对「没有声明过错误响应」的 route 把 `error` 标成 `never`，
 * 于是 `isErrorEnvelope(error) ? error.message : fallback` 里那个
 * `error.message` 是对 `never` 取属性 —— 编译不过。
 *
 * **而 `never` 不代表运行期不会有错误**：它只代表**那条 route 没在
 * OpenAPI 里声明过错误形状**。所以这里按运行期的事实写：任何值都可能来，
 * 认得出信封就用它的 `message`，认不出就用兜底。
 */
export function messageOf(error: unknown, fallback: string): string {
  return isErrorEnvelope(error) ? error.message : fallback;
}
