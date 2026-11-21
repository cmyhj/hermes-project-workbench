# shared/wb-agent-chat — 共享 agent 聊天组件

一个自包含的浏览器端聊天面板，被项目区（`plugin/dashboard/dist/`）通过 vendored 副本使用。
它**不是**第二个聊天客户端：所有对话仍走 Hermes 自己的 `/api/ws`（`session.create` +
`prompt.submit`），只是把「模块 / 对象 / 目录上下文」注入到会话里。

## 为什么要有 vendored 副本

Hermes 的插件静态资产路由是 `/dashboard-plugins/<name>/<path>`，服务端用
`is_relative_to` 把请求**锁死在该插件自己的 `dashboard/` 目录内**
（`hermes_cli/web_routers/dashboard_ui.py`）。于是：

- 不能在 `index.js` 里 `import` 或 `<script src="../shared/...">` —— 会被 404 / 拒绝；
- 唯一官方允许的复用方式是：源码留在 `shared/`，由脚本复制到每个插件的 `dist/vendor/`。

```
shared/wb-agent-chat/          ← 唯一源码（改这里）
plugin/dashboard/dist/vendor/  ← 同步产物（不要手改）
```

## 同步

```bash
python tools/sync-shared.py           # 复制
python tools/sync-shared.py --check   # 只校验，不一致则非零退出（供 CI）
```

改了 `shared/` 却忘了同步，在浏览器里表现为**旧组件继续运行**，看起来只是「改动没生效」。
所以发布前跑一次 `--check`。

## 加载协议

`dist/index.js` 里的 `loadSharedChat()` 负责注入：

1. `<link>` 引 CSS（带 `data-wb-chat-css` 标记，防重复注入）；
2. `<script>` 引 JS（带 `data-wb-chat-js` 标记）；
3. 脚本加载完调用 `window.__WB_CHAT_READY__(api)` 把 `api` 交给调用方；
4. 调用方用 `api.makePanel(SDK)` 造出 React 组件。

两个实例同时加载时，后者等前者的回调（`window.__WB_CHAT_READY__` 链式转发），
不会重复注入第二份脚本。

## 组件契约（调用方必须遵守）

| 入参 | 含义 |
|---|---|
| `moduleId` | 模块 id（这里是 `wb-project`），用于会话与槽位隔离 |
| `moduleName` | 展示名（`项目`） |
| `objectId` / `objectName` | 当前对象的稳定 id 与名字 |
| `getContext()` | **每次新建会话时现取**的上下文：`{ module, object, cwd, facts[], files[], notes[] }` |
| `onTurnComplete()` | 一轮对话结束的回调（用于刷新页面数据） |
| `onBusy(bool)` | 忙碌状态（只用于折叠把手上的小点） |
| `onCollapse()` | 窄屏抽屉里请求收起 |

`cwd` 必须是**服务端已验证的真实目录**（`/projects` 返回的 `dir_path`），不要在前端拼路径。
`files[]` 同理，只放服务端确认存在的文件。

## 安全

- 组件只发 JSON-RPC/`fetch` 到同源 `/api/ws` 与 `/api/plugins/wb-project/`，不读取任何本地文件；
- 不解析、不渲染任何来自文件内容的 HTML（文件预览在 `dist/index.js` 里单独实现，用 `textContent`）；
- 鉴权沿用宿主 SDK 的 `fetchJSON` / `buildWsUrl`，不自己读 session token。
