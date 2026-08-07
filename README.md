# XHS Android Publisher

通过一台 USB 调试安卓设备安全地保存、定时或公开发布小红书图文内容。
项目只负责设备控制和发布，不负责生成正文或配图。

## 能力

- 严格选择一台授权ADB设备。
- 自动唤醒、解锁、打开小红书，并在所有结果后熄屏。
- 校验账号、标题、正文、图片数量和公开可见。
- 使用文章专属临时相册和逐张SHA-256核对，保持封面第一与图片顺序。
- 保存草稿、修改已有草稿、设置小红书原生定时发布。
- CAPTCHA、登录异常、违规提示或结果不明确时停止，不盲目重试。
- 使用macOS钥匙串保存设备PIN，不写入项目配置。

## 安装

按照 [setup.md](references/setup.md) 配置ADB、手机、小红书和本机配置文件。

```bash
cp assets/xhs_config.example.json ~/.config/codex/xhs-android-publisher.json
scripts/xhs prepare
scripts/xhs doctor
```

## 使用

```bash
scripts/xhs article-check 001
scripts/xhs article-draft 001
scripts/xhs article-schedule 001
scripts/safe_publish.py 001
scripts/xhs end
```

公开发布必须有明确授权；上传或保存草稿不等于发布成功。

## 仓库边界

不提交真实账号、设备PIN、验证码、文章、配图、排期、设备标识或运行截图。

## 版本与发布

每次功能迭代必须更新 `VERSION` 和 `CHANGELOG.md`。提交并推送后创建对应
每次迭代先更新 `VERSION` 与 `CHANGELOG.md`，提交后运行
`scripts/release.sh`。脚本会推送代码、创建 `vX.Y.Z` 标签，并把本次
更新内容发布为 GitHub Release。
