# 开发指南

本文档面向从源码调试、修改和验证 blrec 的开发者。普通用户应等待社区维护版发行物，并阅读[安装与使用](installation.md)。

## 仓库结构

blrec 在源码层面分为 Python 后端和 Angular 前端，发行时将前端构建产物嵌入后端。完整模块关系见[项目架构](architecture.md)。

| 路径 | 用途 |
| --- | --- |
| `src/blrec` | Python 后端、录制核心和已构建的前端资源 |
| `webapp` | Angular 前端源码 |
| `tests` | Python 测试 |
| `docs` | 用户、开发与维护文档 |

## 后端开发环境

需要 Python 3.11、`ffmpeg` 和 `ffprobe`。克隆仓库后创建虚拟环境，并以可编辑方式安装开发依赖：

```bash
git clone https://github.com/tursom/blrec.git
cd blrec
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
python -m pip install -e ".[dev]"
```

Windows PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活虚拟环境。

如果受限网络中的构建隔离无法解析依赖，可以在确认本地构建工具已经安装后使用：

```bash
python -m pip install -e ".[dev]" --no-build-isolation
```

## 运行后端

直接运行命令行入口：

```bash
blrec
```

也可以作为 ASGI 应用运行：

```bash
uvicorn blrec.web:app --host localhost --port 2233
```

常用环境变量如下：

| 环境变量 | 用途 |
| --- | --- |
| `BLREC_CONFIG` | 设置文件路径 |
| `BLREC_OUT_DIR` | 覆盖录播输出目录 |
| `BLREC_LOG_DIR` | 覆盖日志目录 |
| `BLREC_API_KEY` | Web API Key |

命令行参数会在导入 Web 应用前映射到这些环境变量。需要完整参数列表时运行 `blrec --help`。

## 后端检查

当前测试使用 Python 标准库 `unittest`：

```bash
python -m unittest discover -s tests
```

提交 Python 改动前，根据改动范围运行格式、导入排序、静态检查和类型检查：

```bash
black --check src tests
isort --check-only src tests
flake8 src tests
mypy src/blrec
```

仓库中的工具版本较旧。若检查器与 Python 3.11 或依赖发生兼容问题，请在 Pull Request 中记录命令、版本和完整错误，不要静默跳过。

## 前端开发环境

前端使用 Angular 15。进入 `webapp` 后安装锁文件中的依赖：

```bash
cd webapp
npm ci
```

启动开发服务器：

```bash
npm start
```

浏览器访问 `http://localhost:4200`。前端需要连接后端 API 时，应同时启动后端，并按当前开发环境配置处理 API 地址。

## 前端构建与检查

运行单元测试和 lint：

```bash
npm test -- --watch=false
npm run ng -- lint
```

构建生产前端：

```bash
npm run build
```

`webapp/angular.json` 把构建目录配置为 `../src/blrec/data/webapp`。构建会直接改写 Python 包中的静态资源，而不是写入 `webapp/dist`。

不要手工修改 `src/blrec/data/webapp` 中的编译产物。修改 `webapp/src` 后重新构建，并将源码和对应构建产物一并检查。

## 本地打包

Python 源码包和 wheel 可以使用以下命令构建：

```bash
python -m build --sdist --wheel --outdir dist
```

仓库根目录的 `blrec.spec` 定义 PyInstaller 入口、数据文件和隐藏导入。当前 Dockerfile 会在构建阶段使用它生成单文件程序，再复制到最终镜像。

本地构建容器镜像：

```bash
docker build -t blrec:dev .
```

正式发行流程和目标产物见[维护与发布](maintenance.md)。本地构建成功不代表发行渠道已经可用。

## 提交改动

提交前阅读[贡献指南](../CONTRIBUTING.md)。后端接口、设置模型或事件结构发生变化时，应同步检查 Angular 调用方和相关文档。
