import { Trans, useLingui } from '@lingui/react/macro';
import { Alert, Button, Card, Form, Input, Typography } from 'antd';
import { useState } from 'react';

import { api } from '../../api/client';
import { messageOf } from '../../api/envelopes';
import { session } from '../../api/session';

/**
 * 改密。**被管理员重置过的人必须先走完这一页。**
 *
 * `POST /auth/change-password` 返回 **204**，没有响应体 —— 所以这里
 * 判据是状态码，不是解析出来的什么东西。成功之后把那一格翻过来，
 * 路由随之放行（见 `App.tsx`）。
 *
 * **这一页不显示「你为什么在这里」的两种措辞。** 一个主动改密的人和一个
 * 被重置的人看到同一页；区别只在后者被路由挡着、退不出去。
 * 写两套文案要前端知道「你是被重置的」，而那正是 T130 之前拿不到的信息 ——
 * 今天拿得到了，但**多一种文案就多一种要维护的状态**，而它换不到什么。
 */
export function ChangePasswordPage(): JSX.Element {
  const { t } = useLingui();
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function submit(values: {
    current_password: string;
    new_password: string;
  }): Promise<void> {
    setPending(true);
    setFailure(null);
    try {
      const { error, response } = await api.POST('/auth/change-password', {
        body: {
          current_password: values.current_password,
          new_password: values.new_password,
        },
      });
      if (response.status !== 204) {
        setFailure(messageOf(error, t`改密失败，请稍后再试`));
        return;
      }
      session.markPasswordChanged();
      setDone(true);
    } catch {
      // **`catch` 而不是只有 `finally`。** 少了这一支，一次抛出去的
      // 错误（网络断了、URL 拼不出来、后端返回的不是 JSON）会让
      // 转圈停下而**界面上一句话都没有** —— 用户看到的是一个点了
      // 没反应的按钮。实测撞到过一次：`new Request` 在一个不接受
      // 相对地址的运行时里抛了，而这一页什么都没显示。
      setFailure(t`改密失败，请稍后再试`);
    } finally {
      setPending(false);
    }
  }

  return (
    <Card style={{ maxWidth: 420, margin: '10vh auto' }}>
      <Typography.Title level={4}>
        <Trans>设置新密码</Trans>
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        <Trans>这个账号的密码是别人替你设的。设置一个只有你知道的密码之后才能继续。</Trans>
      </Typography.Paragraph>
      {failure !== null && (
        <Alert type="error" message={failure} showIcon style={{ marginBottom: 16 }} />
      )}
      {done && <Alert type="success" message={t`密码已更新`} showIcon style={{ marginBottom: 16 }} />}
      <Form layout="vertical" onFinish={submit} disabled={pending}>
        <Form.Item
          label={t`当前密码`}
          name="current_password"
          rules={[{ required: true, message: t`请填写当前密码` }]}
        >
          <Input.Password autoComplete="current-password" />
        </Form.Item>
        <Form.Item
          label={t`新密码`}
          name="new_password"
          rules={[{ required: true, message: t`请填写新密码` }]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Button type="primary" htmlType="submit" block loading={pending}>
          <Trans>保存</Trans>
        </Button>
      </Form>
    </Card>
  );
}
