# blrec Webapp

这里存放 blrec 的 Angular 15 前端源码。前端负责任务管理、设置、状态展示，以及通过 REST API 和 WebSocket 与 Python 后端通信。

完整的前后端关系见[项目架构](../docs/architecture.md)，仓库级环境配置见[开发指南](../docs/development.md)。

## 安装依赖

使用仓库锁文件安装依赖：

```bash
npm ci
```

## 开发服务器

启动 Angular 开发服务器：

```bash
npm start
```

浏览器访问 `http://localhost:4200`。前端功能需要后端 API 时，应同时在仓库根目录启动 Python 后端。

## 构建

构建生产前端：

```bash
npm run build
```

`angular.json` 将输出目录配置为 `../src/blrec/data/webapp`。构建产物会直接进入 Python 包，不会写入 `webapp/dist`。

不要手工编辑生成的静态资源。修改 `src` 下的前端源码后，应重新运行构建。

## 测试与 lint

在非交互模式运行 Karma 单元测试：

```bash
npm test -- --watch=false
```

运行 Angular ESLint：

```bash
npm run ng -- lint
```

## 生成代码

需要使用 Angular CLI 生成组件等代码时，通过项目本地版本执行：

```bash
npm run ng -- generate component component-name
```

提交生成结果前，应删除未使用的样板代码，并检查路由、模块依赖和测试是否需要同步更新。
