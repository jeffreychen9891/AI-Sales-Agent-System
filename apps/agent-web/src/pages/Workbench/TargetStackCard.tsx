import { Trans, useLingui } from '@lingui/react/macro';
import { Alert, Card, Empty, Space, Spin, Table, Tag, Tooltip, Typography } from 'antd';
import { useEffect, useState } from 'react';

import { api } from '../../api/client';
import { messageOf } from '../../api/envelopes';
import type { components } from '../../api/schema';

type Detail = components['schemas']['ConversationDetail'];
type Frame = components['schemas']['FrameItem'];
type Slot = components['schemas']['SlotItem'];

/**
 * AI 现在在谈什么，以及它到手的信息是谁给的。
 *
 * ## 这一格回答的是「为什么它这么问」
 *
 * 目标栈是引擎的计划：哪一帧在跑、哪一帧被挂起、每一帧还缺什么信息。
 * 坐席接管之后最先要知道的就是这个 —— 否则他接手的是一段没有上下文的对话。
 *
 * ## 槽位的出处必须显示，而且要有第三种
 *
 * ADR-0054 §三.2 让**人写下的槽位成为模型不许改写的事实**。
 * 所以「这一条是谁记的」不是装饰，它决定 AI 接回去之后会不会改它。
 *
 * **第三种是「说不清」**：出处这件事是 §三.2 之后才开始记的，
 * 在那之前写下的槽位有值、没有出处。把它们并进「AI 记的」或「人记的」
 * 都是**替一条读不出来的记录作证**。
 *
 * ## 这一版只看，不改 —— 而这次的理由不是「没排上」
 *
 * `PUT .../frames/{id}/slots` 存在，**但写槽位要先有词表**：
 * 帧的 `information_slots` 定义了哪些名字有意义，
 * 而像 `concern_topic` 这样的槽位还有 `allowed_values` ——
 * **填一个不在表里的值，整条会被丢弃，那一轮等于没填**
 * （话术包里逐字写着）。
 *
 * 一个自由填写的输入框正是本仓 `CLAUDE.md` §三 点名的形状：
 * 能提交、能返回 200、**只有结果是错的**。
 * 词表今天读得到了（批 8 的 `GET /profiles/releases/{id}` 带 `target_frames`），
 * 所以这是一次**接线**而不是从零建 —— 登记 T501。
 */
export function TargetStackCard({
  conversationId,
  reloadToken,
}: {
  conversationId: string;
  /** 变一次就重读一次 —— 接管 / 交还都会改目标栈。 */
  reloadToken: number;
}): JSX.Element {
  const { t } = useLingui();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    void (async () => {
      try {
        const { data, error } = await api.GET('/conversations/{conversation_id}', {
          params: { path: { conversation_id: conversationId } },
        });
        if (data === undefined) {
          setFailure(messageOf(error, t`读取会话详情失败`));
          return;
        }
        setFailure(null);
        setDetail(data);
      } catch {
        setFailure(t`读取会话详情失败`);
      } finally {
        setLoading(false);
      }
    })();
  }, [conversationId, reloadToken, t]);

  return (
    <Card title={t`AI 在谈什么`} style={{ width: 420 }}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {failure !== null && <Alert type="error" message={failure} showIcon />}
        {loading && detail === null ? (
          <Spin />
        ) : detail === null ? (
          <Empty description={t`没有读到数据`} />
        ) : detail.target_stack.length === 0 ? (
          <Empty description={t`引擎还没有为这通对话立目标`} />
        ) : (
          detail.target_stack.map((frame) => <FrameBlock key={String(frame.frame_id)} frame={frame} />)
        )}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          <Trans>这一版只能看。改信息要先有词表，见下一批。</Trans>
        </Typography.Text>
      </Space>
    </Card>
  );
}

function FrameBlock({ frame }: { frame: Frame }): JSX.Element {
  const { t } = useLingui();
  const slots = frame.slots;
  const status = String(frame.status ?? '');

  return (
    <Card
      size="small"
      title={
        <Space size="small">
          <span>{String(frame.frame_id ?? '—')}</span>
          {/*
            **被挂起是接管的后果，不是故障。** 一个光秃秃的 `suspended`
            会被读成出了问题；这里说清是谁让它停的。
          */}
          <Tag color={status === 'active' ? 'green' : status === 'suspended' ? 'orange' : undefined}>
            {status === 'active'
              ? t`在跑`
              : status === 'suspended'
                ? t`因接管挂起`
                : status || t`未知`}
          </Tag>
        </Space>
      }
    >
      <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
        {String(frame.goal ?? t`这一帧没有写目标`)}
      </Typography.Paragraph>
      {slots.length === 0 ? (
        <Typography.Text type="secondary">
          <Trans>还没有问到任何信息。</Trans>
        </Typography.Text>
      ) : (
        <Table<Slot>
          rowKey="name"
          size="small"
          dataSource={slots}
          pagination={false}
          columns={[
            { title: t`信息`, dataIndex: 'name', width: 130 },
            {
              title: t`谁记的`,
              width: 110,
              render: (_, slot) => <Provenance source={slot.source} />,
            },
            {
              title: t`把握`,
              width: 80,
              render: (_, slot) =>
                typeof slot.confidence === 'number' ? (
                  `${Math.round(slot.confidence * 100)}%`
                ) : (
                  <Typography.Text type="secondary">—</Typography.Text>
                ),
            },
          ]}
        />
      )}
    </Card>
  );
}

/**
 * 这一条是谁记的 —— **三种，不是两种**。
 *
 * `null` 不是「AI 记的」的同义词：那是 ADR-0054 §三.2 开始记出处之前
 * 写下的行，而那件事本身值得显示出来。
 */
function Provenance({ source }: { source: string | null }): JSX.Element {
  const { t } = useLingui();
  if (source === 'human') return <Tag color="orange">{t`人记的`}</Tag>;
  if (source === 'model') return <Tag color="blue">{t`AI 记的`}</Tag>;
  return (
    <Tooltip title={t`这一条写在「记录是谁填的」这件事之前，所以查不到出处。`}>
      <Tag>{t`说不清`}</Tag>
    </Tooltip>
  );
}
