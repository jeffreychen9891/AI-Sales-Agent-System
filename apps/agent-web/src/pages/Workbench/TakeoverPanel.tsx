import { Trans, useLingui } from '@lingui/react/macro';
import { Alert, Button, Card, Checkbox, Descriptions, Space, Spin, Tag, Typography } from 'antd';
import { useState } from 'react';

import { api } from '../../api/client';
import { messageOf, newIdempotencyKey } from '../../api/envelopes';
import type { components } from '../../api/schema';

type ResumePreview = components['schemas']['ResumePreview'];
type TakeoverProjection = components['schemas']['TakeoverProjectionShape'];

/**
 * 接管，以及**两阶段的交还**。
 *
 * ## v1 是一个开关，v2 不是 —— 而那个差别必须在屏幕上看得见
 *
 * v1 把「谁在说话」做成 `PUT status`：点一下，立刻生效。
 * v2 的交还是 ADR-0054 §三.4 定的**两阶段命令**：
 * 先读一份预览（`GET .../resume-preview`），它带回一个 `change_digest`；
 * 再用那个 digest 做 CAS（`POST .../resume`）。
 *
 * **所以这一页不提供「直接交还」的入口，一个都没有。**
 * `ResumeConversationRequest` 的 docstring 逐字写着「没有 `force`，
 * 也没有不看就交还的办法」——**一个跳过预览的按钮，就是用默认值
 * 替人回答了「AI 要怎么继承人产生的事实」这个问题**，
 * 而那正是那条裁定写来防的事。
 *
 * ## `CHANGE_DIGEST_STALE` 不是错误，是「你看的那一幅画面变了」
 *
 * 两阶段之间，客户可能又说了话、引擎可能又写了槽位。这时 digest 对不上，
 * 后端回 **409**。**把它显示成「操作失败」是错的**：什么都没坏，
 * 只是要重看一遍再确认。所以这里当场重拉预览，并说清为什么。
 *
 * ## 接管期五项投影里，有一项会让其余四项不可信
 *
 * `understanding_halted_by_cost_gate` 为真，意思是理解步被成本闸拦过 ——
 * **那一段时间里发生的事没有被记下来**。这时候把其余四项照常显示，
 * 等于请人对着一幅缺了几笔的画做决定，所以它单独置顶成一条警告。
 */
export function TakeoverPanel({
  conversationId,
  controlMode,
  onChanged,
}: {
  conversationId: string;
  controlMode: string;
  onChanged: () => void;
}): JSX.Element {
  const { t } = useLingui();
  const [preview, setPreview] = useState<ResumePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [invalidateSummary, setInvalidateSummary] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const heldByPerson = controlMode === 'human';

  async function takeOver(): Promise<void> {
    setBusy(true);
    setFailure(null);
    setNote(null);
    try {
      const { data, error } = await api.POST('/conversations/{conversation_id}/takeover', {
        params: {
          path: { conversation_id: conversationId },
          header: { 'Idempotency-Key': newIdempotencyKey() },
        },
      });
      if (data === undefined) {
        setFailure(messageOf(error, t`接管失败`));
        return;
      }
      // **报出「挂起了几帧、停掉了几个定时器」** —— 别的 route 都不给这个数，
      // 而它是「这通对话真的安静下来了」的唯一依据。
      setNote(
        t`已接管：挂起 ${data.result.frames_suspended} 帧，停掉 ${data.result.events_invalidated} 个待触发的动作。`,
      );
      onChanged();
    } catch {
      setFailure(t`接管失败`);
    } finally {
      setBusy(false);
    }
  }

  async function readPreview(): Promise<void> {
    setBusy(true);
    setFailure(null);
    setNote(null);
    try {
      const { data, error } = await api.GET(
        '/conversations/{conversation_id}/resume-preview',
        { params: { path: { conversation_id: conversationId } } },
      );
      if (data === undefined) {
        setFailure(messageOf(error, t`读取交还预览失败`));
        return;
      }
      setPreview(data);
      setInvalidateSummary(false);
    } catch {
      setFailure(t`读取交还预览失败`);
    } finally {
      setBusy(false);
    }
  }

  async function resume(): Promise<void> {
    if (preview === null) return;
    setBusy(true);
    setFailure(null);
    setStale(false);
    try {
      const { data, error, response } = await api.POST(
        '/conversations/{conversation_id}/resume',
        {
          params: {
            path: { conversation_id: conversationId },
            header: { 'Idempotency-Key': newIdempotencyKey() },
          },
          body: {
            acknowledged_change_digest: preview.change_digest,
            invalidate_summary: invalidateSummary,
          },
        },
      );
      if (data === undefined) {
        // **409 在这条路径上有两种，而只有一种要重看。**
        // `CHANGE_DIGEST_STALE` 说的是画面变了；别的 409（比如这通对话
        // 已经不归人了）是状态冲突，重看解决不了。
        if (response.status === 409 && isStaleDigest(error)) {
          setStale(true);
          await readPreview();
          return;
        }
        setFailure(messageOf(error, t`交还失败`));
        return;
      }
      setPreview(null);
      setNote(
        t`已交还：${data.result.frames_active} 帧恢复。接管时作废的定时器不会回来 —— 它们是照着一份已经不存在的计划铸的。`,
      );
      onChanged();
    } catch {
      setFailure(t`交还失败`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title={t`接管`} style={{ width: 360 }}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {failure !== null && <Alert type="error" message={failure} showIcon />}
        {note !== null && <Alert type="success" message={note} showIcon />}
        {stale && (
          <Alert
            type="warning"
            showIcon
            message={t`这通对话在你看的时候又变了`}
            description={t`没有出错，也没有人做错什么 —— 下面是刚刚重新读到的画面，确认之后再交还。`}
          />
        )}

        {!heldByPerson ? (
          <>
            <Typography.Text type="secondary">
              <Trans>这通对话现在归 AI 接待。</Trans>
            </Typography.Text>
            <Button type="primary" loading={busy} onClick={() => void takeOver()}>
              <Trans>接管</Trans>
            </Button>
          </>
        ) : preview === null ? (
          <>
            <Tag color="orange">{t`归你`}</Tag>
            {/*
              **这里只有「看一眼再交还」，没有「直接交还」。**
              后端没有 `force` 字段，前端也不该造一个看起来像的入口
              —— 见组件 docstring。
            */}
            <Button loading={busy} onClick={() => void readPreview()}>
              <Trans>看一眼再交还给 AI</Trans>
            </Button>
          </>
        ) : (
          <ResumeReview
            preview={preview}
            busy={busy}
            invalidateSummary={invalidateSummary}
            onInvalidateSummaryChange={setInvalidateSummary}
            onConfirm={() => void resume()}
            onCancel={() => setPreview(null)}
          />
        )}
        {busy && preview === null && <Spin />}
      </Space>
    </Card>
  );
}

/** 这个 409 是不是「画面变了」那一种。 */
function isStaleDigest(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    (error as Record<string, unknown>)['code'] === 'CHANGE_DIGEST_STALE'
  );
}

/**
 * 交还前要读的那一幅画面。
 *
 * **它显示的是「你接管期间发生了什么」，不是「这通对话是什么」** ——
 * 后者在逐字记录里。这一格回答的是交还时唯一要紧的问题：
 * **AI 接回去之后，它会以为发生过什么？**
 */
function ResumeReview({
  preview,
  busy,
  invalidateSummary,
  onInvalidateSummaryChange,
  onConfirm,
  onCancel,
}: {
  preview: ResumePreview;
  busy: boolean;
  invalidateSummary: boolean;
  onInvalidateSummaryChange: (next: boolean) => void;
  onConfirm: () => void;
  onCancel: () => void;
}): JSX.Element {
  const { t } = useLingui();
  const projection = preview.takeover_projection;

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <TakeoverProjectionView projection={projection} />

      <Card size="small" title={t`AI 现在以为这通对话在聊什么`}>
        {preview.conversation_summary.length === 0 ? (
          <Typography.Text type="secondary">
            <Trans>还没有摘要 —— 这通对话短到不需要。</Trans>
          </Typography.Text>
        ) : (
          // 摘要本来就是一个列表，就用列表画它 —— 把项目符号拼进文本节点，
          // 读屏软件会把它当成正文的一部分念出来。
          <ul style={{ margin: 0, paddingInlineStart: 20 }}>
            {preview.conversation_summary.map((line) => (
              <li key={line}>
                <Typography.Text>{line}</Typography.Text>
              </li>
            ))}
          </ul>
        )}
        <div style={{ marginTop: 12 }}>
          <Checkbox
            checked={invalidateSummary}
            disabled={preview.conversation_summary.length === 0}
            onChange={(event) => onInvalidateSummaryChange(event.target.checked)}
          >
            <Trans>这份摘要是错的，交还时作废它</Trans>
          </Checkbox>
          {/*
            **作废是有损的**：行不会回到 `active`（`054` 同族那条触发器）。
            所以默认不勾，而且这里写明它做了什么、不做什么 ——
            「作废 + 下一回合重生成」是裁定八「人能改」的最小实现，
            **不是编辑**。
          */}
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 4 }}>
            <Trans>
              作废之后它不会回来，AI 会在下一回合重新总结。这一版只能作废，不能改写。
            </Trans>
          </Typography.Paragraph>
        </div>
      </Card>

      <Space>
        <Button type="primary" loading={busy} onClick={onConfirm}>
          <Trans>确认交还</Trans>
        </Button>
        <Button disabled={busy} onClick={onCancel}>
          <Trans>先不交</Trans>
        </Button>
      </Space>
    </Space>
  );
}

/**
 * 接管期五项投影。
 *
 * **五项里有一项会让其余四项不可信**，所以它不在列表里排队 ——
 * 它置顶，并且说清后果。
 */
export function TakeoverProjectionView({
  projection,
}: {
  projection: TakeoverProjection;
}): JSX.Element {
  const { t } = useLingui();
  const written = projection.slots_written;
  const refused = projection.refused_slot_updates;
  const halted = projection.understanding_halted_by_cost_gate;
  const period = projection.takeover_period as Record<string, unknown>;
  const exit = projection.pending_exit_signal as Record<string, unknown>;

  return (
    <Space direction="vertical" size="small" style={{ width: '100%' }}>
      {halted.halted && (
        <Alert
          type="warning"
          showIcon
          message={t`这段时间里有事情没被记下来`}
          description={t`理解步被成本上限拦过 ${halted.occurrences} 次，所以下面几项是不完整的 —— 交还之前请自己看一遍逐字记录。`}
        />
      )}

      <Descriptions size="small" column={1} title={t`你接管期间`}>
        <Descriptions.Item label={t`从什么时候算起`}>
          {periodLabel(period, t)}
        </Descriptions.Item>
        <Descriptions.Item label={t`你记下的信息`}>
          {written.human.length === 0 ? (
            <Typography.Text type="secondary">{t`没有`}</Typography.Text>
          ) : (
            <Tag color="orange">{t`${written.human.length} 条`}</Tag>
          )}
        </Descriptions.Item>
        <Descriptions.Item label={t`AI 记下的信息`}>
          {written.model.length}
        </Descriptions.Item>
        {/*
          **第三桶照显，哪怕它通常是 0。** 那是 ADR-0054 §三.2 加出处之前
          写下的槽位：有值、说不清是谁写的。把它并进任何一边都是替一条
          读不出来的记录作证。
        */}
        {written.unattributed.length > 0 && (
          <Descriptions.Item label={t`说不清是谁记的`}>
            <Tag>{written.unattributed.length}</Tag>
          </Descriptions.Item>
        )}
        <Descriptions.Item label={t`被拒绝的改写`}>
          {refused.total === 0 ? (
            <Typography.Text type="secondary">{t`没有`}</Typography.Text>
          ) : (
            <Tag color="red">{t`${refused.total} 次`}</Tag>
          )}
        </Descriptions.Item>
        <Descriptions.Item label={t`客户想结束吗`}>
          {exit['present'] === false ? (
            <Typography.Text type="secondary">{t`没有这个迹象`}</Typography.Text>
          ) : (
            <Tag color="volcano">{t`有迹象，交还前请看一眼`}</Tag>
          )}
        </Descriptions.Item>
        {/*
          **承诺那一格照显，而旁边那句话说清这个 0 是哪一种 0**
          （用户 2026-09-13）：台账在 M1a 不存在，所以它永远是 0 ——
          一个光秃秃的 0 与「这一轮确实没有承诺」在屏幕上一模一样。
        */}
        <Descriptions.Item label={t`登记的承诺`}>
          <Typography.Text type="secondary">
            {projection.commitments_registered.count} · {projection.commitments_registered.note}
          </Typography.Text>
        </Descriptions.Item>
      </Descriptions>
    </Space>
  );
}

/**
 * 接管期从什么时候算起 —— **三种答案，不是两种**。
 *
 * `source` 为 `conversation_start` 的那一种最容易被读错：它不是
 * 「不知道」，是**这通对话一出生就归人**（租户的策略如此）。
 */
function periodLabel(period: Record<string, unknown>, t: (s: TemplateStringsArray) => string): string {
  const source = period['source'];
  const startedAt = period['started_at'];
  if (source === 'takeover' && typeof startedAt === 'string') return startedAt;
  if (source === 'conversation_start') return t`这通对话一开始就归人接待`;
  return t`还没有人接管过`;
}
