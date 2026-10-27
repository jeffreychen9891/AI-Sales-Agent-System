import { Trans, useLingui } from '@lingui/react/macro';
import {
  Alert,
  Card,
  Descriptions,
  Empty,
  List,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd';
import { useEffect, useState } from 'react';

import { api } from '../../api/client';
import { messageOf } from '../../api/envelopes';
import type { components } from '../../api/schema';
import { readLabels, readList, readNumber, readObject, readText } from './read';

type ReleaseSummary = components['schemas']['ProfileReleaseItem'];
type ReleaseDocument = components['schemas']['ProfileReleaseDocument'];

/**
 * 话术 —— **AI 手上有哪些话术，商家第一次看得见**。
 *
 * ## 在这一页之前，商家能看到的只有版本号
 *
 * `GET /profiles/releases` 每行四个元数据字段，它的 docstring 逐字写着
 * 「文档故意不在列表里」。所以商家知道自己有 `1.2.4` 这一版，
 * **却看不到这一版说了什么** —— 而那正是他付钱买的东西。
 *
 * ## 这一页只给商家那半，而**它明说少了什么**
 *
 * ADR-0143 §二 裁定不给四项（`policy_bindings`、
 * `confidence_gate_policy_ref`、`manipulation_context_refs`、
 * `golden_manifest`），理由是平台的实现细节一旦被看见就不再是内部字段。
 *
 * **这一页把「有东西没给你看」写在界面上**，而不是默默不显示。
 * 默默不显示，商家会以为自己看到了全部 —— 而他下次问「为什么 AI 会这么答」
 * 时，答案有一半根本不在他见过的东西里。
 *
 * ## 只能看，不能改
 *
 * 写那一半今天**不存在**：package 来自仓库镜像，控制面没有任何改 profile
 * 的 route（ADR-0143 §二 末段，T373 的另一半仍开着）。所以这里不放一个
 * 点不动的「编辑」—— 按 `RolesPage` 同一条道理，说清为什么比给一个灰按钮有用。
 *
 * ## 字段名不是原样搬的
 *
 * `active_window_seconds` / `reengagement_limits` 这些名字是**引擎的**，
 * 商家看到它们只会去猜。所以标签写成一句问话
 * （「客户不回之后，多久算这轮结束」），而**单位也跟着换** ——
 * 秒是给代码的，分钟才是人排班用的。
 */
export function ProfilePage(): JSX.Element {
  const { t } = useLingui();
  const [releases, setReleases] = useState<ReleaseSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [document, setDocument] = useState<ReleaseDocument | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [listing, setListing] = useState(true);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        const { data, error } = await api.GET('/profiles/releases');
        if (data === undefined) {
          setFailure(messageOf(error, t`读取话术版本失败`));
          return;
        }
        setReleases(data.items);
        // 默认打开第一版：一个只列出版本号的页面，与上一批那个
        // 「看得到有哪几版、看不到内容」的状态没有区别。
        // `noUncheckedIndexedAccess` 下 `items[0]` 是 `T | undefined` —— 取出来再判，
        // 而不是用 `!` 把它断言掉：空列表在这里是真会发生的状态（新商家）。
        const first = data.items[0];
        if (first !== undefined) setSelected(first.release_id);
      } catch {
        setFailure(t`读取话术版本失败`);
      } finally {
        setListing(false);
      }
    })();
  }, []);

  useEffect(() => {
    if (selected === null) return;
    setLoading(true);
    setDocument(null);
    void (async () => {
      try {
        const { data, error } = await api.GET('/profiles/releases/{release_id}', {
          params: { path: { release_id: selected } },
        });
        if (data === undefined) {
          setFailure(messageOf(error, t`读取这一版话术失败`));
          return;
        }
        setFailure(null);
        setDocument(data);
      } catch {
        setFailure(t`读取这一版话术失败`);
      } finally {
        setLoading(false);
      }
    })();
  }, [selected]);

  const proactivity: unknown = document?.proactivity_policy ?? {};
  const quiet = readObject(proactivity, 'quiet_hours');
  const limits = readObject(proactivity, 'reengagement_limits');
  const activeWindow = readNumber(proactivity, 'active_window_seconds');
  const quietRanges = readList(quiet, 'ranges')
    .map((range) => `${readText(range, 'start')}–${readText(range, 'end')}`)
    .filter((label) => label !== '–');
  const quietZone = readText(quiet, 'timezone');

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      {failure !== null && <Alert type="error" message={failure} showIcon />}
      <Alert
        type="info"
        showIcon
        message={t`这一版只能看，不能改`}
        description={t`改话术的接口今天还不存在 —— 这里显示的内容来自发布时的那份包。另外，这一页给的是话术里跟你有关的那半：平台侧的策略绑定、置信闸与基线清单不在其中。`}
      />

      <Card
        title={t`版本`}
        extra={
          <Select
            style={{ minWidth: 260 }}
            loading={listing}
            value={selected}
            onChange={(value: string) => setSelected(value)}
            placeholder={t`选择一个版本`}
            aria-label={t`选择一个版本`}
            options={releases.map((release) => ({
              value: release.release_id,
              label: `${release.profile_id} ${release.version}`,
            }))}
          />
        }
      >
        {listing ? (
          <Spin />
        ) : releases.length === 0 ? (
          <Empty description={t`这个商家还没有发布过话术`} />
        ) : (
          <Descriptions size="small" column={2}>
            <Descriptions.Item label={t`话术包`}>{document?.profile_id ?? '—'}</Descriptions.Item>
            <Descriptions.Item label={t`版本`}>{document?.version ?? '—'}</Descriptions.Item>
            <Descriptions.Item label={t`内容指纹`} span={2}>
              {/* 全长显示：截断过的哈希对不上任何东西，也就没法拿去比对。 */}
              <Typography.Text code copyable style={{ fontSize: 12 }}>
                {document?.content_hash ?? '—'}
              </Typography.Text>
            </Descriptions.Item>
          </Descriptions>
        )}
      </Card>

      {loading && <Spin />}

      {document !== null && (
        <>
          <Card title={t`AI 会谈哪些事（${document.target_frames.length}）`}>
            <Table<Record<string, unknown>>
              rowKey={(frame) => readText(frame, 'id')}
              size="small"
              dataSource={document.target_frames}
              pagination={false}
              locale={{ emptyText: t`这一版没有目标帧` }}
              columns={[
                {
                  title: t`名称`,
                  width: 180,
                  render: (_, frame) => readText(frame, 'id'),
                },
                {
                  title: t`它是干什么的`,
                  render: (_, frame) =>
                    readText(frame, 'goal') || (
                      <Typography.Text type="secondary">—</Typography.Text>
                    ),
                },
                {
                  title: t`要问到的信息`,
                  width: 220,
                  render: (_, frame) => {
                    const labels = readLabels(readList(frame, 'information_slots'));
                    return labels.length === 0 ? (
                      <Typography.Text type="secondary">{t`不需要问`}</Typography.Text>
                    ) : (
                      <Space size={[0, 4]} wrap>
                        {labels.map((label) => (
                          <Tag key={label}>{label}</Tag>
                        ))}
                      </Space>
                    );
                  },
                },
                {
                  title: t`会用到的素材`,
                  width: 200,
                  render: (_, frame) => {
                    const labels = readLabels(readList(frame, 'content_materials'));
                    return labels.length === 0 ? (
                      <Typography.Text type="secondary">—</Typography.Text>
                    ) : (
                      <Space size={[0, 4]} wrap>
                        {labels.map((label) => (
                          <Tag key={label} color="blue">
                            {label}
                          </Tag>
                        ))}
                      </Space>
                    );
                  },
                },
              ]}
            />
          </Card>

          <Card title={t`主动联系的规矩`}>
            <Descriptions size="small" column={1}>
              <Descriptions.Item label={t`客户不回之后，多久算这轮结束`}>
                {activeWindow === null ? '—' : t`${Math.round(activeWindow / 60)} 分钟`}
              </Descriptions.Item>
              <Descriptions.Item label={t`同一段时间里最多主动发几条`}>
                {readNumber(limits, 'max_delivered') ?? '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t`免打扰时段`}>
                {quietRanges.length === 0
                  ? t`没有设置`
                  : // ⚠️ **时区是租户的，不是客户的**（ADR-0097）：渠道回调里没有
                    // 客户时区，所以这里写明按谁的时间算。不写明，商家会读成
                    // 「客户那边的晚上」—— 而那是这个字段今天**做不到**的事。
                    t`${quietRanges.join('、')}（按 ${quietZone} 计）`}
              </Descriptions.Item>
            </Descriptions>
          </Card>

          <Card title={t`要记住客户的哪些事（${document.memory_fields.length}）`}>
            {document.memory_fields.length === 0 ? (
              <Empty description={t`这一版没有声明要记的事`} />
            ) : (
              <Space size={[0, 8]} wrap>
                {readLabels(document.memory_fields).map((label) => (
                  <Tag key={label}>{label}</Tag>
                ))}
              </Space>
            )}
          </Card>

          <Card title={t`素材正文（${Object.keys(document.content_material_texts).length}）`}>
            {Object.keys(document.content_material_texts).length === 0 ? (
              <Empty description={t`这一版没有素材`} />
            ) : (
              <List
                size="small"
                dataSource={Object.entries(document.content_material_texts)}
                renderItem={([ref, text]) => (
                  <List.Item key={ref}>
                    <List.Item.Meta
                      title={<Typography.Text code>{ref}</Typography.Text>}
                      description={<Typography.Text>{text}</Typography.Text>}
                    />
                  </List.Item>
                )}
              />
            )}
          </Card>

          <Card title={t`用到的知识库（${document.knowledge_bindings.length}）`}>
            {document.knowledge_bindings.length === 0 ? (
              <Empty description={t`这一版没有绑定知识库`} />
            ) : (
              <Space size={[0, 8]} wrap>
                {document.knowledge_bindings.map((binding) => (
                  <Tag key={binding}>{binding}</Tag>
                ))}
              </Space>
            )}
          </Card>

          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            <Trans>
              这一页显示的是话术里跟你有关的那半。平台侧的策略绑定、置信闸与基线清单不在其中。
            </Trans>
          </Typography.Paragraph>
        </>
      )}
    </Space>
  );
}
