import { Trans, useLingui } from '@lingui/react/macro';
import { Alert, Button, Card, Form, Input, Typography } from 'antd';
import { useState } from 'react';

import { api } from '../../api/client';
import { messageOf } from '../../api/envelopes';
import { session } from '../../api/session';

/**
 * 登录。**v1 布局复用，调用层重写**（ADR-0057 决策 1：只复用视觉与交互层）。
 *
 * ## 这一页与 v1 的三处不同，每一处都有坐标
 *
 * 1. **没有注册入口。** `control-plane-interface-inventory.md` §5 的退役台账：
 *    页面、route、Login 上的注册链接、service 与测试全删。
 * 2. **没有「记住我」。** refresh 走的是 httpOnly cookie，它的存活由
 *    `FASTAGENT_REFRESH_TOKEN_TTL_SECONDS` 决定 —— 前端给一个勾选框，
 *    等于让用户以为他在控制一件他控制不了的事。
 * 3. **登录成功之后可能还欠一次改密**，而这一页**自己就知道**（T130）：
 *    `must_change_password` 在响应体里。在那个字段存在之前，唯一的发现
 *    路径是随后任意一条 route 的 403。
 */
export function LoginPage(): JSX.Element {
  // **`useLingui()` 的 `t` 是宏，不是运行期函数。** babel 在构建时把它
  // 展开成一个带 id 的查表调用，`lingui extract` 据此把这句话收进目录 ——
  // 也就是说「哪些字面量是给人看的」这个问题由构建回答，不由人回答。
  const { t } = useLingui();
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  async function submit(values: {
    tenant_code: string;
    email: string;
    password: string;
  }): Promise<void> {
    setPending(true);
    setFailure(null);
    try {
      const { data, error } = await api.POST('/auth/login', {
        body: {
          // **`tenant_code` 是生成的类型逼出来的。** 初稿只写了邮箱 + 密码，
          // `tsc` 当场报 `Property 'tenant_code' is missing` ——
          // 而在手写 client 的世界里，那会是一个交付之后才发现的 422。
          // 这就是 parity §4.3「不得手写 method/path」换来的东西。
          tenant_code: values.tenant_code,
          email: values.email,
          password: values.password,
        },
      });
      if (error !== undefined || data === undefined) {
        // **信封的 `code` 是机器可读的那一半，`message` 是给人看的那一半。**
        // 这里显示 `message`，而任何分支都只许看 `code`
        // （parity §5.2：「页面不得分支于人类可读的消息」）。
        setFailure(
          messageOf(error, t`登录失败，请稍后再试`),
        );
        return;
      }
      session.grant(data);
      // **这一行就是 T130 的全部价值。** 后端在响应里说了，前端据此把人
      // 送去改密页 —— 而不是让他撞上下一条 route 的 403 再回头。
      if (data.must_change_password) {
        session.markPasswordChangeRequired();
      } else {
        session.markPasswordChanged();
      }
    } catch {
      // **`catch` 而不是只有 `finally`。** 少了这一支，一次抛出去的
      // 错误（网络断了、URL 拼不出来、后端返回的不是 JSON）会让
      // 转圈停下而**界面上一句话都没有** —— 用户看到的是一个点了
      // 没反应的按钮。实测撞到过一次：`new Request` 在一个不接受
      // 相对地址的运行时里抛了，而这一页什么都没显示。
      setFailure(t`登录失败，请稍后再试`);
    } finally {
      setPending(false);
    }
  }

  return (
    <Card style={{ maxWidth: 360, margin: '10vh auto' }}>
      <Typography.Title level={3} style={{ textAlign: 'center' }}>
        <Trans>商家工作台</Trans>
      </Typography.Title>
      {failure !== null && (
        <Alert type="error" message={failure} showIcon style={{ marginBottom: 16 }} />
      )}
      <Form layout="vertical" onFinish={submit} disabled={pending}>
        <Form.Item
          label={t`商家编号`}
          name="tenant_code"
          rules={[{ required: true, message: t`请填写商家编号` }]}
        >
          <Input autoComplete="organization" />
        </Form.Item>
        <Form.Item
          label={t`邮箱`}
          name="email"
          rules={[{ required: true, message: t`请填写邮箱` }]}
        >
          <Input autoComplete="username" type="email" />
        </Form.Item>
        <Form.Item
          label={t`密码`}
          name="password"
          rules={[{ required: true, message: t`请填写密码` }]}
        >
          <Input.Password autoComplete="current-password" />
        </Form.Item>
        <Button type="primary" htmlType="submit" block loading={pending}>
          <Trans>登录</Trans>
        </Button>
      </Form>
    </Card>
  );
}
