# 安装与使用

本文档面向运行 blrec 的用户。参与开发或从源码验证当前代码时，请阅读[开发指南](development.md)。

## 发行状态

首个社区维护版本尚未发布。GitHub Releases 中的 PyInstaller 二进制和带版本标签的 GHCR 镜像将在首个维护版本发布后提供。

PyPI 上的 `blrec` 是原作者发布的历史版本，不包含本仓库后续维护内容。请勿使用 `pip install blrec` 或 `pipx install blrec` 安装社区维护版。

| 平台 | 计划发行物 | ffmpeg/ffprobe |
| --- | --- | --- |
| Windows x64 | GitHub Release PyInstaller 压缩包 | 包内提供 |
| Linux amd64 | GitHub Release PyInstaller 压缩包 | 通过系统安装 |
| Linux arm64 | GitHub Release PyInstaller 压缩包 | 通过系统安装 |
| Linux amd64/arm64 | `ghcr.io/tursom/blrec` 容器镜像 | 镜像内提供 |

在首个维护版本发布前，如需验证仓库当前代码，请按[开发指南](development.md)从源码运行。

## 使用二进制发行物

以下步骤描述首个社区维护版本发布后的使用方式。发布前，请不要根据文件名猜测下载地址。

### Windows x64

1. 从 [GitHub Releases](https://github.com/tursom/blrec/releases) 下载 Windows x64 压缩包。
2. 解压到普通用户具有写权限的目录。
3. 在终端中进入解压目录并运行 `blrec.exe`。
4. 在浏览器访问 `http://localhost:2233`。

Windows 压缩包会同时提供 `ffmpeg` 和 `ffprobe`。不要只移动可执行文件；升级前也应保留完整目录结构。

### Linux amd64/arm64

先通过系统包管理器安装 `ffmpeg`，并确认以下命令均可执行：

```bash
ffmpeg -version
ffprobe -version
```

然后从 [GitHub Releases](https://github.com/tursom/blrec/releases) 下载与系统架构匹配的压缩包，解压并赋予主程序执行权限：

```bash
chmod +x blrec
./blrec
```

在浏览器访问 `http://localhost:2233`。

## 使用 Docker

正式镜像地址为 `ghcr.io/tursom/blrec`。首个社区维护版本发布后，每次正式发布同时提供不可变的 `vX.Y.Z` 标签和滚动更新的 `latest` 标签。

生产环境建议固定版本标签。下面的 `<version>` 表示包含前导 `v` 的版本，例如 `v2.0.0`：

```bash
docker pull ghcr.io/tursom/blrec:<version>
docker run --name blrec \
  -v /etc/blrec:/cfg \
  -v /var/log/blrec:/log \
  -v /srv/blrec:/rec \
  -p 2233:2233 \
  -d ghcr.io/tursom/blrec:<version>
```

镜像支持 `linux/amd64` 和 `linux/arm64`。容器内置 `ffmpeg` 和 `ffprobe`，默认监听 `0.0.0.0:2233`。

### 数据目录

| 容器路径 | 用途 | 默认环境变量 |
| --- | --- | --- |
| `/cfg` | 设置文件 | `BLREC_DEFAULT_SETTINGS_FILE=/cfg/settings.toml` |
| `/log` | 日志文件 | `BLREC_DEFAULT_LOG_DIR=/log` |
| `/rec` | 录播、弹幕和元数据文件 | `BLREC_DEFAULT_OUT_DIR=/rec` |

务必把 `/cfg` 和 `/rec` 挂载到持久化存储。删除容器不会删除已挂载的数据，但未挂载目录中的数据会随容器一起丢失。

### 传递启动参数

镜像入口已经设置为 `blrec --host 0.0.0.0 --no-progress`。在镜像名后追加的参数会传给 blrec：

```bash
docker run --name blrec \
  -v /etc/blrec:/cfg \
  -v /var/log/blrec:/log \
  -v /srv/blrec:/rec \
  -p 2233:2233 \
  -d ghcr.io/tursom/blrec:<version> \
  --api-key change-this-key
```

查看全部参数：

```bash
docker run --rm ghcr.io/tursom/blrec:<version> --help
```

## 命令行运行

直接运行二进制时，使用以下命令查看全部参数：

```bash
blrec --help
```

默认监听 `localhost:2233`。默认设置文件为 `~/.blrec/settings.toml`，默认日志目录为 `~/.blrec/logs`，默认录播目录为当前工作目录。

指定设置文件、录播目录和日志目录：

```bash
blrec \
  --config /path/to/settings.toml \
  --out-dir /path/to/records \
  --log-dir /path/to/logs
```

设置文件不存在时会自动创建。命令行中的目录参数会覆盖设置文件中的对应值。

## 网络安全

不要把未设置 API Key 的 blrec 直接暴露到公网。远程访问时，优先通过可信反向代理提供 HTTPS，并限制来源网络。

blrec 也可以直接加载 TLS 证书并设置 API Key：

```bash
blrec \
  --host 0.0.0.0 \
  --key-file /path/to/key.pem \
  --cert-file /path/to/cert.pem \
  --api-key change-this-key
```

API Key 长度必须为 8 至 80 个字符，只能包含字母、数字和连字符。浏览器首次访问时会要求输入，并把它保存在浏览器的 Local Storage 中。

在共享设备上使用后应清理站点数据。提交问题时，不要公开 API Key、Cookie、Webhook URL 或完整设置文件。

## API、Webhook 与 PWA

默认情况下，交互式 REST API 文档位于 `http://localhost:2233/docs`。程序支持通过 Webhook 发送事件，可与 REST API 配合实现录制后的压制、上传和其他自动化操作。

Web 界面支持 PWA。浏览器只会在本机地址或 HTTPS 环境中开放安装能力。

## 升级与回滚

升级前先停止 blrec，并备份 `settings.toml`。录播目录通常不需要复制，但应确认它位于持久化存储中。

二进制用户应解压新版本到新目录，再复制或显式指定原设置文件和数据目录。确认新版本正常后，再删除旧程序目录。

Docker 用户应拉取新的版本标签，用相同的卷挂载和启动参数重建容器。不要依赖 `latest` 完成可审计的生产升级。

回滚时停止新版本，并使用原版本二进制或原镜像标签启动。恢复设置备份前，先确认新版本是否已经修改了设置文件格式。

## 卸载

二进制用户可以删除程序目录。设置、日志和录播是否被删除，取决于它们是否保存在该目录中；操作前先核对路径。

Docker 用户可以删除容器和镜像。只有在确认不再需要设置与录播后，才删除宿主机上的挂载目录。
