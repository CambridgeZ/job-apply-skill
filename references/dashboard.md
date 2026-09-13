# 投递记录网页

页面由本地资料文件生成，展示公司、岗位、申请状态、更新时间、公司招聘入口与岗位详情链接，以及每份实际填写内容和确认状态。没有记录时显示空状态，不从简历推断曾经投过的公司。

## 生成与打开

从实际 Skill 安装目录执行，或用脚本的绝对路径：

```bash
python3 scripts/dashboard.py build
python3 scripts/dashboard.py serve --port 8765
```

默认读取 `~/Documents/Codex/job-applications/profile.json`，生成 `~/Documents/Codex/job-applications/dashboard.html`。`build` 生成可离线打开的 HTML；`serve` 仅监听 `127.0.0.1`，启动结果会输出准确的网页 URL。使用那个 URL 向用户展示页面，不猜测端口是否可用。

用户指定其他资料文件时：

```bash
python3 scripts/dashboard.py build --file "$PROFILE_PATH" --output "$DASHBOARD_PATH"
python3 scripts/dashboard.py serve --file "$PROFILE_PATH" --output "$DASHBOARD_PATH" --port 8765
```

`--file` 与 `--output` 可以放在子命令前或后。自定义输出使用专用私有目录，不写到公开 Skill 仓库。默认位置是本机个人记录，不是 GitHub Pages。

需要在 Codex 中展示时，将服务器实际输出的 URL 传给可用的 `open_in_codex` 浏览器入口。保留服务进程供用户查看；已知本任务的服务还在运行时复用它，不重复启动。端口冲突时换一个空闲端口并使用新输出地址，不终止来源未知的进程。

服务运行时每隔几秒读取资料，网页定期刷新显示；每次记录变更后仍运行 `build`，使离线页面保持更新。服务器停止后，本机 URL 不再可用，但生成的 HTML 仍可离线查看；下次查看时重新启动即可。不要承诺本机地址可从其他设备访问。

## 用户可见内容

- 列表能搜索公司/岗位，按状态筛选。
- `submitted` 才计入“已提交”；待确认、填写中、需要处理单独展示。
- 公司招聘入口使用实际记录的 `recruiting_url`，岗位详情使用 `url`。缺失时显示未记录，不编造链接。只有 HTTP/HTTPS 链接可以点击。
- 展开的核对详情显示 `review.fields` 中的最终答案、改写说明，以及是否存在与当前版本匹配的确认。页面只读，不能点击网页按钮绕过对话中的用户确认。
- 展示申请编号和成功证据摘要，方便用户回查。资料中的账号、完整简历路径、全量 facts、短信等不作为页面的数据接口输出；核对字段本身可能含有用户实际提交的个人信息，页面因此仍应保留在本机。

资料未初始化或读取失败时要说明具体状态；读取失败不能冒充“还没有投递记录”。不要因为生成或打开页面失败就声称投递失败，申请事实和页面展示是两项独立结果。

## 发布边界

网页生成器、空模板及测试可以随 Skill 发布；实际 `profile.json`、生成的 `dashboard.html`、核对报告和浏览器截图不加入 Git。这个本地记录页不会将个人申请记录同步到公开仓库。
