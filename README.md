# XHS Android Publisher

通过一台 USB 调试安卓设备安全地保存、定时或公开发布小红书图文内容。
项目只负责设备控制和发布，不负责生成正文或配图。

## 隔离快速发布路径（alpha）

新增 `python3 -m agent.publish`：直接编辑后发布，不经过中间草稿；
页面快照按操作失效、同页检查共享，TypeSafe 仅辅助普通导航。
账号核对、防重提交、公开验收及实际熄屏仍由确定性代码控制。
目前仅完成模拟故障测试及真机只读探测，不自动替换旧流程或连接服务端队列。
配置、使用、证据边界及参考案例见 [快速发布设计与验收](references/fast-publish.md)。

## 能力

- 严格选择一台授权ADB设备。
- 自动唤醒、解锁、打开小红书，并在所有结果后熄屏。
- 校验账号、标题、正文、图片数量和公开可见。
- 使用文章专属临时相册和逐张SHA-256核对，保持封面第一与图片顺序。
- 保存草稿、修改已有草稿、设置小红书原生定时发布。
- 常规 ADB 流程找不到控件时，由受控 UI-TARS 恢复层接管页面导航。
- 恢复轨迹连续成功两次后晋级为可复用经验，首次复用失败立即隔离。
- 默认把未知恢复问题写入 Codex 接管队列，也可接入 UI-TARS 视觉模型。
- 精确选择 USB OnePlus，忽略其他网络 ADB 设备。
- CAPTCHA、登录异常、违规提示或结果不明确时停止，不盲目重试。
- 使用macOS钥匙串保存设备PIN，不写入项目配置。

## 安装

按照 [setup.md](references/setup.md) 配置ADB、手机、小红书和本机配置文件。

```bash
cp assets/xhs_config.example.json ~/.config/codex/xhs-android-publisher.json
cp assets/xhs_ui_tars.example.json ~/.config/codex/xhs-ui-tars.json
scripts/xhs prepare
scripts/xhs doctor
```

## 使用

```bash
scripts/xhs article-check 001
scripts/xhs article-draft 001
scripts/xhs article-schedule 001
scripts/safe_publish.py 001
scripts/xhs observe --no-model
scripts/xhs recovery-status
scripts/xhs end
```

公开发布必须有明确授权；上传或保存草稿不等于发布成功。
UI-TARS 只恢复普通页面导航和缺失控件，永远不能代替发布、定时、删除、
登录、验证码、隐私范围或发布后验收门禁。云端模型必须显式允许截图上传。

## 仓库边界

不提交真实账号、设备PIN、验证码、文章、配图、排期、设备标识或运行截图。

## 版本与发布

每次迭代先更新 `VERSION` 与 `CHANGELOG.md`，提交后运行
`scripts/release.sh`。脚本会推送代码、创建 `vVERSION` 标签，并把本次
更新内容发布为 GitHub Release。
