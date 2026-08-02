# Bilibili Live Streaming Recorder (blrec)

`blrec` 是一个带 Web 界面的 B 站直播录制工具。它可以自动监控直播状态，录制直播流和弹幕，并完成文件分割、时间戳修复、后处理、通知和磁盘空间回收。

## 维护状态

本仓库是由 [tursom](https://github.com/tursom) 接续维护的社区分支。原仓库 [acgnhiki/blrec](https://github.com/acgnhiki/blrec) 已归档，不再发布 blrec 更新。

本仓库保留原项目名称、历史和 GPLv3 许可证。社区维护版的代码、问题反馈和后续发行均以 [tursom/blrec](https://github.com/tursom/blrec) 为准。

> [!IMPORTANT]
> 首个社区维护版本尚未发布。GitHub Releases 中的 PyInstaller 二进制和带版本标签的 GHCR 镜像将在首个维护版本发布后提供。PyPI 上的 `blrec` 是原作者发布的历史版本，不代表本仓库当前代码。

## 功能

- 自动监控并录制直播，支持选择画质和流格式
- 同步保存结构化弹幕和原始弹幕数据
- 修复时间戳跳变、反跳等问题
- 在流参数变化时自动分割文件，降低花屏风险
- 在网络中断后尝试拼接流，并支持无缝拼接
- 为 FLV 文件写入关键帧等元数据
- 按文件大小或时长分割录制文件
- 使用 `ffmpeg` 将 FLV remux 为 MP4
- 自定义保存路径和文件名模板
- 监控磁盘空间并按策略回收旧录播文件
- 通过邮件、ServerChan、PushDeer、pushplus、Telegram 和 Bark 发送通知
- 通过 Webhook 和 REST API 接入后续压制、上传等自动化流程
- 持久化保存脱敏后的 B 站 HTTP 请求历史，并可从 Web 界面导出排障包

## 开始使用

当前用户发行渠道正在迁移。不要使用 PyPI 上的历史版本验证本仓库的新功能，也不要继续使用旧文档中的 Docker Hub 地址。

首个社区维护版本发布后，项目将提供以下用户安装方式：

| 使用场景 | 发行方式 | 状态 |
| --- | --- | --- |
| 服务器部署 | `ghcr.io/tursom/blrec` 容器镜像 | 待首个维护版本发布 |
| Windows x64 | GitHub Release 中的 PyInstaller 压缩包 | 待首个维护版本发布 |
| Linux amd64/arm64 | GitHub Release 中的 PyInstaller 压缩包 | 待首个维护版本发布 |

完整的安装、运行、升级和卸载说明见[安装与使用](docs/installation.md)。参与开发时，请从源码安装并阅读[开发指南](docs/development.md)。

## 使用入口

启动 `blrec` 后，在浏览器访问 `http://localhost:2233`。默认设置文件位于 `~/.blrec/settings.toml`，默认日志目录位于 `~/.blrec/logs`。

Web 界面用于管理录制任务和设置。交互式 REST API 文档位于 `http://localhost:2233/docs`，事件和异常通过 WebSocket 推送到前端。

## 文档

- [安装与使用](docs/installation.md)：发行状态、二进制、Docker、运行、安全、升级和卸载
- [常见问题](FAQ.md)：录制、流中断、文件分割、后处理和磁盘回收
- [开发指南](docs/development.md)：后端与前端开发环境、构建和测试
- [贡献指南](CONTRIBUTING.md)：Issue、Discussion 和 Pull Request 协作方式
- [项目架构](docs/architecture.md)：模块边界、数据流和构建交付关系
- [维护与发布](docs/maintenance.md)：版本、发行物和发布验证清单
- [更新日志](CHANGELOG.md)：版本变更历史

## 获取支持

- 可复现的缺陷和功能需求请提交到 [Issues](https://github.com/tursom/blrec/issues)。
- 使用问题、部署交流和方案讨论请发布到 [Discussions](https://github.com/tursom/blrec/discussions)。
- 准备提交代码前，请先阅读[贡献指南](CONTRIBUTING.md)。

公开日志、配置或截图前，请移除 Cookie、API Key、Webhook URL、邮箱凭据和其他敏感信息。

## 截图

![Web 管理界面](https://user-images.githubusercontent.com/33854576/128959800-451d03e7-c9f9-4732-ac90-97fdb6b88972.png)

![终端录制状态](https://user-images.githubusercontent.com/33854576/128959819-70d72937-65da-4c15-b61c-d2da65bf42be.png)

## 许可证与致谢

本项目基于 [GNU General Public License v3.0](LICENSE) 发布。

感谢原作者 [acgnhiki](https://github.com/acgnhiki) 以及所有历史贡献者建立和维护 blrec。当前社区分支延续原项目历史，但使用独立的代码仓库、支持入口和发行渠道。
