# 维护与发布

本文档面向社区维护者，定义版本准备、发行物和发布验证流程。项目结构和运行链路见[项目架构](architecture.md)，本地构建命令见[开发指南](development.md)。

## 当前过渡状态

首个社区维护版本尚未发布。当前文档允许描述目标发行契约，但在产物通过验证前，不能把 GitHub Releases 或版本化 GHCR 镜像写成可用渠道。

PyPI 上的 `blrec` 属于原作者的历史发行，不是社区维护版的发布渠道。Docker Hub 也不再作为本项目的镜像入口。

当前工作流与目标发行契约仍有差距：现有 Windows portable 工作流不是 PyInstaller 构建，GHCR 工作流只推送 `latest`。发布首个维护版本前必须先完成并验证相应的自动化改造。

## 发行契约

正式版本使用带前导 `v` 的 Git tag，例如 `v2.0.0`。

每个正式版本应提供：

| 目标 | 发行物或标签 | 依赖约定 |
| --- | --- | --- |
| Windows x64 | `blrec-vX.Y.Z-windows-x64.zip` | 包含 blrec、ffmpeg 和 ffprobe |
| Linux amd64 | `blrec-vX.Y.Z-linux-amd64.tar.gz` | 系统提供 ffmpeg 和 ffprobe |
| Linux arm64 | `blrec-vX.Y.Z-linux-arm64.tar.gz` | 系统提供 ffmpeg 和 ffprobe |
| Linux amd64/arm64 | `ghcr.io/tursom/blrec:vX.Y.Z` | 镜像内提供 ffmpeg 和 ffprobe |
| 最新正式版 | `ghcr.io/tursom/blrec:latest` | 与最新版本标签指向相同构建 |

同一版本的二进制、镜像和源码必须来自同一个 Git commit。不要从不同 workflow run 拼接一次发布。

## 发布前准备

1. 确认目标 commit 已合入 `master`，工作树干净。
2. 更新 `src/blrec/__init__.py` 中的版本号。
3. 把 `CHANGELOG.md` 的 `Unreleased` 内容整理为目标版本，并写明日期。
4. 检查 README、安装指南和发行状态提示是否与本次发布一致。
5. 构建前端，确认生成资源来自当前 `webapp/src`。
6. 运行与改动范围匹配的后端、前端和静态检查。
7. 本地构建 PyInstaller 程序和容器镜像，完成最小启动验证。

版本号、Git tag、压缩包文件名和镜像标签必须一致。若任一目标平台未通过验证，应停止发布，而不是只发布部分平台却保留完整平台承诺。

## 创建发行物

从已验证的 commit 创建并推送带签名或受保护的版本 tag。发布工作流应使用该 tag 构建三个 PyInstaller 压缩包，并推送两个架构的 GHCR 镜像。

Windows 压缩包应携带可用的 `ffmpeg` 和 `ffprobe`。Linux 压缩包不携带它们，Release 说明和安装文档必须列出系统依赖。

发布页面应包含：

- 版本变更摘要和完整 CHANGELOG 链接
- 三个平台压缩包及其校验值
- GHCR 的版本标签和 `latest` 标签
- 已知问题、兼容性或迁移提示

不要发布 wheel 或 sdist 到 PyPI。源码归档由 GitHub Release 自动提供，源码安装仅用于开发与验证。

## 发布后验证

对每个二进制压缩包执行以下检查：

1. 在干净环境解压。
2. 运行 `blrec --version` 和 `blrec --help`。
3. 使用临时设置、日志和输出目录启动服务。
4. 访问 Web 首页和 `/docs`。
5. 确认 Windows 能找到包内 ffmpeg，Linux 能找到系统 ffmpeg。

对 GHCR 执行以下检查：

1. 分别检查 `linux/amd64` 和 `linux/arm64` manifest。
2. 拉取版本标签并以临时卷启动。
3. 确认 `/cfg`、`/log`、`/rec` 的默认路径生效。
4. 确认版本标签与 `latest` 指向同一镜像内容。

确认所有产物可用后，移除 README 和安装指南中的“首个社区维护版本尚未发布”提示，并把示例中的占位版本替换为实际版本。

## 回滚与撤回

发行物存在严重问题时，应先在 Release 说明中标记问题，并停止推荐对应版本。不要覆盖已经发布的版本标签或复用同一个版本号构建不同内容。

镜像回滚通过重新指向 `latest` 完成，原有 `vX.Y.Z` 标签保持不可变。修复后发布新的补丁版本，并在 CHANGELOG 中记录影响和迁移方式。

## 文档与支持检查

发布后检查以下入口：

- README、安装指南和 Release 页面使用相同版本与平台名称
- 新问题指向 `tursom/blrec` 的 Issues 和 Discussions
- 原仓库链接只用于上游关系、致谢和历史引用
- PyPI 和 Docker Hub 只出现在明确的历史或弃用说明中
- 示例不包含真实 API Key、Cookie、Webhook URL 或其他凭据
