import { Trans, useLingui } from '@lingui/react/macro';
import { Alert, Badge, Card, Descriptions, Empty, List, Space, Spin, Tag, Typography } from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';

import { api } from '../../api/client';
import { messageOf, newIdempotencyKey } from '../../api/envelopes';
import type { components } from '../../api/schema';
import { SendBox } from './SendBox';
import { TakeoverPanel } from './TakeoverPanel';
import { TargetStackCard } from './TargetStackCard';

type Conversation = components['schemas']['ConversationListItem'];
type Message = components['schemas']['MessageItem'];
type Contact = components['schemas']['ContactDetail'];

/**
 * 会话工作台（读那一侧）。
 *
 * ## 轮询，不是 WebSocket（ADR-0143 §三）
 *
 * `WS /ws` 今天握手通、ticket 一次性消费都真，而**全仓零处往那条连接推
 * 任何东西**。建一个订阅者等于建一个永远不响的组件，而它在演示里与
 * 「今天没有新消息」长得一模一样。
 *
 * **间隔 15 秒，而这个数是有意义的**：ADR-0143 §三 把 WS 发布者的解除
 * 条件写成「轮询间隔被迫压到 5 秒以内」。15 秒留了三倍余量 ——
 * 哪天真的要压到 5 秒以下，那本身就是该去建发布者的信号，
 * 而不是把这个数再调小一次。
 *
 * ## 这一页不做什么
 *
 * **不做接管、不做发送、不做撤回。** 接管与恢复在批 7（两阶段命令，
 * 交互要重画）；人工发送在批 9（后端那一半还没有）；撤回被 K15 移出，
 * 而 `revoked` 恒为 `False` 是**答案不是占位** —— 所以这里**不放撤回入口**，
 * 不是放一个置灰的。
 */

/** 轮询间隔。见上面那段 —— 这个数与 ADR-0143 §三 的解除条件绑在一起。 */
const POLL_MS = 15_000;

export function WorkbenchPage(): JSX.Element {
  const { t } = useLingui();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  /**
   * **只记 id，不记那一行。**
   *
   * 记住整行是个快照：接管成功之后列表刷新了，而这个快照里的
   * `control_mode` 还是 `ai` —— 于是屏幕上「已接管」与发送框
   * 「先接管」同时出现，要用户再点一次才对得上。
   * 从最新的列表里按 id 取，那个时差就不存在。
   */
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected =
    conversations.find((row) => row.conversation_id === selectedId) ?? null;
  const [failure, setFailure] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const loadConversations = useCallback(async (): Promise<void> => {
    try {
      const { data, error } = await api.GET('/conversations');
      if (data === undefined) {
        setFailure(messageOf(error, t`读取会话列表失败`));
        return;
      }
      setFailure(null);
      setConversations(data.items);
    } catch {
      setFailure(t`读取会话列表失败`);
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadConversations();
    const timer = setInterval(() => void loadConversations(), POLL_MS);
    return () => clearInterval(timer);
  }, [loadConversations]);

  return (
    <Space align="start" size="large" style={{ width: '100%' }}>
      <Card
        title={t`会话`}
        style={{ width: 340, flex: '0 0 340px' }}
        styles={{ body: { padding: 0, maxHeight: '70vh', overflowY: 'auto' } }}
      >
        {failure !== null && <Alert type="error" message={failure} showIcon />}
        <List<Conversation>
          loading={loading}
          dataSource={conversations}
          locale={{
            emptyText: (
              <Empty
                description={
                  // **空态说清是哪一种空。** 「还没有会话」与「读失败了」
                  // 在一张空列表上长得一模一样，而后者上面那条 Alert 才是真话。
                  failure === null ? t`还没有会话` : t`没有读到数据`
                }
              />
            ),
          }}
          renderItem={(row) => (
            <ConversationRow
              row={row}
              selected={row.conversation_id === selectedId}
              onSelect={() => setSelectedId(row.conversation_id)}
            />
          )}
        />
      </Card>
      {selected === null ? (
        <Card style={{ flex: 1, minWidth: 420 }}>
          <Typography.Text type="secondary">
            <Trans>从左边选一条会话。</Trans>
          </Typography.Text>
        </Card>
      ) : (
        <ConversationDetail
          key={selected.conversation_id}
          conversationId={selected.conversation_id}
          // **控制方从列表那一行带下来，不在详情里再查一次。**
          // 再查一次会有两个答案，而它们之间有一个轮询间隔的时差 ——
          // 于是发送框可能按一个已经过期的状态决定自己能不能用。
          controlMode={selected.control_mode}
          onRead={() => void loadConversations()}
          onChanged={() => void loadConversations()}
        />
      )}
    </Space>
  );
}

/**
 * 列表里的一行 —— `parity` §6.2 的九项，**每一项都显示**。
 *
 * **不省略空态**：没有联系人、没有消息、没有排期事件各自显示成一句话，
 * 而不是让那一格消失。一格消失了，读的人分不出「没有」与「没查」。
 */
function ConversationRow({
  row,
  selected,
  onSelect,
}: {
  row: Conversation;
  selected: boolean;
  onSelect: () => void;
}): JSX.Element {
  const { t } = useLingui();
  return (
    <List.Item
      onClick={onSelect}
      style={{
        cursor: 'pointer',
        padding: '12px 16px',
        background: selected ? 'rgba(22,119,255,0.06)' : undefined,
      }}
    >
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        <Space size="small" style={{ width: '100%', justifyContent: 'space-between' }}>
          <Typography.Text strong ellipsis style={{ maxWidth: 190 }}>
            {row.contact?.external_user_id ?? t`未识别的客户`}
          </Typography.Text>
          <Space size={4}>
            {row.unread_inbound > 0 && <Badge count={row.unread_inbound} />}
            <Tag color={row.control_mode === 'ai' ? 'blue' : 'orange'}>
              {row.control_mode === 'ai' ? t`AI 接待` : t`人工`}
            </Tag>
          </Space>
        </Space>
        <Typography.Text type="secondary" ellipsis style={{ maxWidth: 300 }}>
          {typeof row.last_message?.content === 'string'
            ? row.last_message.content
            : t`（还没有消息）`}
        </Typography.Text>
        <Space size={4} wrap>
          {row.target !== null && <Tag>{row.target.status}</Tag>}
          {row.next_event !== null && (
            <Tag color="purple">{t`下一步：${row.next_event.kind}`}</Tag>
          )}
          {row.open_manual_tasks > 0 && (
            <Tag color="red">{t`待办 ${row.open_manual_tasks}`}</Tag>
          )}
        </Space>
      </Space>
    </List.Item>
  );
}

function ConversationDetail({
  conversationId,
  controlMode,
  onRead,
  onChanged,
}: {
  conversationId: string;
  controlMode: string;
  onRead: () => void;
  onChanged: () => void;
}): JSX.Element {
  const { t } = useLingui();
  const [messages, setMessages] = useState<Message[]>([]);
  const [contact, setContact] = useState<Contact | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  /** 已经为哪一条消息报过已读 —— 免得每次轮询都发一次同样的命令。 */
  const reported = useRef<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    void (async () => {
      setLoading(true);
      try {
        const [page, who] = await Promise.all([
          api.GET('/conversations/{conversation_id}/messages', {
            params: { path: { conversation_id: conversationId } },
          }),
          api.GET('/contacts/by-conversation/{conversation_id}', {
            params: { path: { conversation_id: conversationId } },
          }),
        ]);
        if (page.data === undefined) {
          setFailure(messageOf(page.error, t`读取消息失败`));
          return;
        }
        setFailure(null);
        setMessages(page.data.items);
        // **联系人读失败不挡住消息**：它是旁证，不是这一屏的主体。
        setContact(who.data ?? null);

        const last = page.data.items.at(-1);
        if (last !== undefined && reported.current !== last.message_id) {
          reported.current = last.message_id;
          // **报的是消息 id，不是我们自己的时钟**：客户端送一个时间戳，
          // 等于对一个它不拥有的排序下断言（`MarkConversationReadRequest`
          // 的 description 逐字写着这件事）。
          await api.POST('/conversations/{conversation_id}/read', {
            params: {
              path: { conversation_id: conversationId },
              header: { 'Idempotency-Key': newIdempotencyKey() },
            },
            body: { last_read_ref: last.message_id },
          });
          onRead();
        }
      } catch {
        setFailure(t`读取消息失败`);
      } finally {
        setLoading(false);
      }
    })();
  }, [conversationId, onRead, t, reloadToken]);

  return (
    <Space align="start" size="large" style={{ flex: 1 }}>
      <Card title={t`逐字记录`} style={{ width: 520 }} styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}>
        {failure !== null && <Alert type="error" message={failure} showIcon />}
        {loading ? (
          <Spin />
        ) : messages.length === 0 ? (
          <Empty description={failure === null ? t`这条会话还没有消息` : t`没有读到数据`} />
        ) : (
          <Space direction="vertical" size="small" style={{ width: '100%' }}>
            {messages.map((message) => (
              <MessageBubble key={message.message_id} message={message} />
            ))}
          </Space>
        )}
        <div style={{ marginTop: 16 }}>
          <SendBox
            conversationId={conversationId}
            takenOver={controlMode === 'human'}
            // 排队之后重拉一次：出站行会作为一条 `PENDING` 出现在记录里，
            // 而**那一行的 `delivery_status` 才是「送到了没有」的答案**。
            onQueued={() => setReloadToken((n) => n + 1)}
          />
        </div>
      </Card>
      <TakeoverPanel
        conversationId={conversationId}
        controlMode={controlMode}
        // 接管 / 交还都会改目标栈（挂起、恢复），所以两件事用同一个信号：
        // 列表重拉一次，目标栈也重读一次。
        onChanged={() => {
          onChanged();
          setReloadToken((n) => n + 1);
        }}
      />
      <TargetStackCard conversationId={conversationId} reloadToken={reloadToken} />
      <Card title={t`客户`} style={{ width: 300 }}>
        {contact === null ? (
          <Typography.Text type="secondary">
            <Trans>没有读到客户信息</Trans>
          </Typography.Text>
        ) : (
          <Space direction="vertical" size="small" style={{ width: '100%' }}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label={t`渠道标识`}>{contact.external_user_id}</Descriptions.Item>
              <Descriptions.Item label={t`首次出现`}>{contact.first_seen_at}</Descriptions.Item>
              <Descriptions.Item label={t`最近活跃`}>{contact.last_seen_at}</Descriptions.Item>
              <Descriptions.Item label={t`消息数`}>{contact.message_count}</Descriptions.Item>
            </Descriptions>
            {/*
              **T179：`profile_data` 今天恒空**（M1a 不调渠道 profile API）。
              一个空的资料区与「这个客户没填资料」在屏幕上一模一样 ——
              所以这里**写明是哪一种空**，与承诺那个 `0` 的裁定同一个形状。
            */}
            {Object.keys(contact.profile_data).length === 0 && (
              <Alert
                type="info"
                showIcon
                message={t`渠道资料尚未接入`}
                description={t`这里只有内部标识与活跃度 —— 不是这个客户没有资料，是我们还没有去取。`}
              />
            )}
          </Space>
        )}
      </Card>
    </Space>
  );
}

/**
 * 一条消息。
 *
 * **没有撤回入口，而这不是漏了。** 撤回被 ADR-0048 K15 移出，`revoked`
 * 恒为 `False` 且它的 docstring 明写那是「答案不是占位」——
 * 放一个置灰的按钮等于说「这件事将来能做」，而那不是今天的事实。
 */
function MessageBubble({ message }: { message: Message }): JSX.Element {
  const { t } = useLingui();
  const inbound = message.direction === 'inbound';
  return (
    <div style={{ textAlign: inbound ? 'left' : 'right' }}>
      <Card
        size="small"
        style={{
          display: 'inline-block',
          maxWidth: '80%',
          background: inbound ? '#fff' : '#e6f4ff',
        }}
      >
        <Typography.Paragraph style={{ marginBottom: 4, whiteSpace: 'pre-wrap' }}>
          {typeof message.content === 'string' ? message.content : t`（无法显示的内容）`}
        </Typography.Paragraph>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {message.created_at}
          {!inbound && ` · ${message.delivery_status}`}
        </Typography.Text>
      </Card>
    </div>
  );
}
