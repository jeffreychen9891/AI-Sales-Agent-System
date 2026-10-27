import { Trans, useLingui } from '@lingui/react/macro';
import { Alert, Button, Input, Space, Typography } from 'antd';
import { useState } from 'react';

import { api } from '../../api/client';
import { messageOf, newIdempotencyKey } from '../../api/envelopes';

/**
 * 坐席说一句话。
 *
 * ## 它只在会话归人的时候能用，而**不能用的时候说清为什么**
 *
 * 后端在 `control_mode != 'human'` 时回 **409 `CONVERSATION_NOT_TAKEN_OVER`**
 * —— 那不是「你没有权限」，是「这条会话现在不归你说话」。
 * 所以这里不是把按钮置灰完事：置灰的按钮说不出「先接管」这三个字，
 * 而那正是用户此刻唯一需要知道的事。
 *
 * ## 「已发送」不来自这个按钮
 *
 * `parity` §6.2 逐字：「UI 的『已发送』只能来自 receipt，不来自按钮点击成功」。
 * 这条 route 回的是 **202 + `status: "accepted"`** —— 意思是**排进队列了**，
 * 不是**送到了**。所以成功之后这里说的是「已排队」，
 * 而真正的投递状态由逐字记录里那一行的 `delivery_status` 显示。
 *
 * **这不是措辞讲究**：把「排队了」写成「已发送」，会让坐席在一次投递失败
 * 之后仍然以为客户收到了 —— 而那种误会要到客户抱怨时才会暴露。
 *
 * ## 不发文件
 *
 * ADR-0141 决策 2 把边界划死：只发文字，`B4 #7`（发素材）不跟着开。
 * 所以这里**没有附件入口**，不是有一个点不动的 ——
 * 一个置灰的回形针说「将来能发」，而那条路的另一头今天根本不存在。
 */
export function SendBox({
  conversationId,
  takenOver,
  onQueued,
}: {
  conversationId: string;
  takenOver: boolean;
  onQueued: () => void;
}): JSX.Element {
  const { t } = useLingui();
  const [text, setText] = useState('');
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [queued, setQueued] = useState(false);

  if (!takenOver) {
    return (
      <Alert
        type="info"
        showIcon
        message={t`这条会话还归 AI 接待`}
        description={t`先接管，才能替商家说话 —— 否则会有两个说话者而没有交接记录。`}
      />
    );
  }

  async function send(): Promise<void> {
    const body = text.trim();
    if (body === '') return;
    setPending(true);
    setFailure(null);
    setQueued(false);
    try {
      const { data, error } = await api.POST('/conversations/{conversation_id}/messages', {
        params: {
          path: { conversation_id: conversationId },
          header: { 'Idempotency-Key': newIdempotencyKey() },
        },
        body: { text: body },
      });
      if (data === undefined) {
        setFailure(messageOf(error, t`发送失败`));
        return;
      }
      setText('');
      setQueued(true);
      onQueued();
    } catch {
      setFailure(t`发送失败`);
    } finally {
      setPending(false);
    }
  }

  return (
    <Space direction="vertical" size="small" style={{ width: '100%' }}>
      {failure !== null && <Alert type="error" message={failure} showIcon />}
      {queued && (
        <Alert
          type="success"
          showIcon
          // **「已排队」不是「已发送」** —— 见 docstring。投递结果在逐字记录
          // 那一行的 `delivery_status` 上，不在这里。
          message={t`已排队`}
          description={t`投递结果会显示在上面那条记录上。`}
        />
      )}
      <Input.TextArea
        value={text}
        onChange={(event) => setText(event.target.value)}
        rows={3}
        maxLength={2000}
        showCount
        disabled={pending}
        placeholder={t`输入要发给客户的话`}
        aria-label={t`要发给客户的话`}
      />
      <Space style={{ width: '100%', justifyContent: 'space-between' }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          <Trans>这一版只能发文字。</Trans>
        </Typography.Text>
        <Button
          type="primary"
          loading={pending}
          disabled={text.trim() === ''}
          onClick={() => void send()}
        >
          <Trans>发送</Trans>
        </Button>
      </Space>
    </Space>
  );
}
