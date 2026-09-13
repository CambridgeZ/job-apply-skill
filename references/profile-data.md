# 本地资料格式与操作

运行 `scripts/profile_store.py`，需要 Python 3.9+，只使用标准库；文件锁依赖 POSIX，面向 macOS/Linux。本 Skill 的 Mac“信息”步骤另外需要可用的原生应用控制工具。

默认资料路径：`~/Documents/Codex/job-applications/profile.json`。不要将它移动到公开仓库。用户提供自定义路径时，在所有命令中一致使用 `--file PATH`，并在下次调用中继续传入同一路径；不要声称脚本会自动记住自定义位置。

## 文件结构

首次 `init` 创建以下空结构，不写入示例个人资料：

```json
{"schema_version": 1, "facts": [], "applications": []}
```

`facts` 每项记录一个事实或完整的结构化答案：

```json
{
  "key": "availability.notice_period",
  "value": {"amount": 1, "unit": "month"},
  "scope": {},
  "source": {"kind": "user", "reference": "当前对话：用户明确回答通知期一个月"}
}
```

示例仅说明格式，不能写进真实资料。`source.kind` 为 `user` 或 `resume`；`reference` 写实际用户答复的日期/上下文，或所用简历的路径与版本/哈希。工具自动写入 UTC `updated_at`。如果有明确截止时间，附加带时区的 ISO 8601 `valid_until`，如 `2030-01-01T00:00:00Z`；不要凭空设置有效期。

未回答的字段不保存为“已知”，不把 `null`、猜测或占位符当作答案。用户不愿回答某敏感问题时，可以保存明确的“不披露”偏好；它不等于该属性的事实。

为同义表单字段复用相同 `key`。常用约定如下，已有键能表达相同含义时优先沿用：

| 内容 | key | scope / value 注意事项 |
| --- | --- | --- |
| 姓名、电话、邮箱 | `identity.name`、`contact.phone`、`contact.email` | 通常 `{}`；电话含国家区号 |
| 教育、实习/工作、项目 | `education.history`、`experience.history`、`projects` | 有来源的结构化数组；日期精度保持与资料一致 |
| 工作许可 | `work_authorization.allowed` | 按 `country`；保留许可限制与有效期 |
| 签证支持 | `work_authorization.sponsorship` | 按 `country`，区分现在与未来 |
| 薪资 | `compensation.expected` | 按 `company`、`job_id`、`country` 等；value 包含金额/区间、币种、周期、税前/税后 |
| 通知期、可到岗时间 | `availability.notice_period`、`availability.start_date` | 规则与具体日期分开；状态变化后更新 |
| 动机与岗位回答 | `answers.motivation` 等 | 按 `company` 和 `job_id` |
| 浏览器、目标地区/岗位 | `preferences.browser`、`preferences.job_search` | 用户明确偏好才能保存；筛选偏好不等于批量提交授权 |

`scope` 和查询 `context` 都是字符串键值对象，按实际申请填写 `company`、`job_id`、`country`、`location`、`cycle` 等。统一公司标识和国家代码，岗位范围应同时含公司。范围必须能被当前上下文完整匹配，例如中国资格不能在新加坡申请中匹配。薪资单位主要记录在 value，避免把月薪误作年薪。

## 命令

将下面的 `SKILL_DIR` 替换为实际安装目录；不用假定当前工作目录就是 Skill 目录。个人资料从 stdin 传入，不把个人信息拼进 shell 参数。JSON 通过工具直接写入临时文件时也应使用私有目录和 0600 权限，用后移除。

```bash
python3 "$SKILL_DIR/scripts/profile_store.py" init
python3 "$SKILL_DIR/scripts/profile_store.py" lookup contact.phone
python3 "$SKILL_DIR/scripts/profile_store.py" lookup work_authorization.allowed --context '{"country":"SG"}'
python3 "$SKILL_DIR/scripts/profile_store.py" put-fact < "$PRIVATE_INPUT"
python3 "$SKILL_DIR/scripts/profile_store.py" put-fact --replace < "$PRIVATE_INPUT"
python3 "$SKILL_DIR/scripts/profile_store.py" list-applications --company Example --job-id JOB-123
python3 "$SKILL_DIR/scripts/profile_store.py" record-application < "$PRIVATE_INPUT"
python3 "$SKILL_DIR/scripts/profile_store.py" forget contact.phone --scope '{}'
```

`--file PATH` 放在子命令前。`forget KEY` 未指定 `--scope` 时会删除该字段的全部范围，只用于用户要求忘记该字段的情况。

查询返回 `known` 才有可用答案。`missing`、`expired`、`ambiguous` 表示缺失、过期或同样具体的范围有冲突，需要检查并询问必要的信息。工具先选择最具体的适用范围，再检查有效期；该范围已过期时不会退回全局答案。工具不会替代理判断工作状况是否已改变；即使返回 `known`，仍要阅读 source、scope、时间和本次用户指令。

同一个 key + scope 的不同 value 会返回 `conflict`，不覆盖旧记录。用户明确更正后可使用 `--replace`，它是表达“这是已确认更正”的技术开关，不要求再次向用户申请许可。

同值普通写入保留未提供的旧元数据，简历来源不能覆盖已有用户确认来源。使用 `--replace` 会完整替换这条事实及元数据；更正时保留仍适用的范围、有效期和必要说明，不把该开关当作忽略旧记录的捷径。

退出码：成功 `0`；输入/读写错误 `2`；写入冲突 `3`；缺失 `4`；过期 `5`；歧义 `6`。需要检查 JSON 结果和退出码，不能忽略失败后声称已经保存。错误时保留原文件，不自行清空重建。

## 申请记录

```json
{
  "company": "Example",
  "job_id": "JOB-123",
  "account": "primary-application-account",
  "status": "submitted",
  "url": "https://careers.example.com/jobs/JOB-123",
  "resume_ref": "resume.pdf + 对应版本或哈希",
  "evidence": "申请详情页显示已提交，申请编号 EXAMPLE-123"
}
```

同公司 + 岗位编号 + 账号更新同一记录，自动维护 `created_at`、`updated_at`。账号可用稳定别名，避免日志重复记录完整手机号。无编号时用岗位详情 URL 作 `job_id`。允许状态为 `draft`、`ready`、`submitting`、`submitted`、`uncertain`、`blocked`；`submitted` 必须有证据。脚本只保存状态，不能替代浏览器检查成功或阻止所有重复提交。

更新时传入完整记录，保留仍适用的信息；可以附加必要的 `next_step`、`application_id` 或最后核实的时间。记录用稳定、可公开访问的岗位 URL，移除含认证令牌的查询参数；不保存浏览器认证状态。

## 存储边界

脚本用锁与原子替换避免并发丢失数据，资料文件及锁文件权限为 0600，专用父目录为 0700。自定义路径的既有父目录不符合 0700 时会拒绝；选择专用私有子目录，不随意修改 Documents 等共享目录的权限。

凭证字段名检测只是防误存，无法发现自由文本中的所有秘密。是否把普通表单答案存入资料仍按用户的保存要求处理，不能因为有检测就复制整段短信或聊天。备份、分享或发布 Skill 时只选 Skill 程序和文档，不包含资料目录。
