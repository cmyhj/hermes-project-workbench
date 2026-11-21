# hermes-project-workbench

在 **Hermes Agent** 的 Web 控制台里运行的「项目工作台」页面：项目进度甘特图 + 关联 Zotero 论文 +
项目文件夹浏览/预览 + 新建或纳管 `.pm` 项目。

这是一个**独立可安装的 Hermes dashboard 插件**（`wb-project`），不依赖任何本机绝对路径，
所有路径都从配置文件读取。

- 目标 Hermes 版本：**0.23.x**（插件 SDK 契约 `1.1.0`，`.pm` `schema_version = 3`）
- 平台：Windows 11 优先（路径/链接行为按 Windows 设计），Linux 亦可运行
- 许可：MIT（见 `LICENSE`）；第三方声明见 `NOTICE`

---

## 1. 它做什么

| 能力 | 说明 |
|---|---|
| **甘特图** | 按 `.pm` 的 `due_date` 定位条形；起点缺失时只画截止点并标注「推算/未知」，**不伪造日期** |
| **关联 Zotero 论文** | 只读 Zotero 本地 API，列出集合下文献元数据，点击跳 `zotero://` 客户端阅读；不做 PDF 阅读器、不回写批注 |
| **项目文件夹** | 显示项目真实目录层级，**按目录懒加载**（展开一层请求一层，绝不整树递归）；文本/JSON/Markdown 纯文本渲染，图片内联预览，PDF 交给浏览器阅读器，Office/压缩包等只下载 |
| **新建 / 纳管项目** | 新建规范项目目录并写 `.pm`；或把已有但**尚无 `.pm`** 的目录补成项目。先预览计划、确认后才落盘，落盘后读回校验 |
| **模块级 agent 对话** | 复用 vendored 的共享聊天组件，`cwd` 自动指向当前项目真实目录，仍走 Hermes 自己的 agent |

---

## 2. 安装

### 2.1 前置

1. Hermes Agent 已安装并能运行 `hermes dashboard`。
   参考：<https://hermes-agent.nousresearch.com/docs>
2. 用 `hermes dashboard`（**不是** `hermes serve`）——`serve` 是无头后端，不提供浏览器 SPA。

### 2.2 放入插件目录

```bash
# Windows（Git Bash / POSIX 语法）
HERMES_HOME="${HERMES_HOME:-$LOCALAPPDATA/hermes}"
mkdir -p "$HERMES_HOME/plugins/wb-project"
cp -r plugin/. "$HERMES_HOME/plugins/wb-project/"
```

目录结构必须是：

```
$HERMES_HOME/plugins/wb-project/
  dashboard/
    manifest.json
    plugin_api.py
    dist/
      index.js
      style.css
      vendor/wb-agent-chat.js
      vendor/wb-agent-chat.css
```

### 2.3 启用（必须做，否则插件不出现）

用户插件只有出现在 `config.yaml → plugins.enabled` 里才会被扫描并挂载后端。
**先读回当前列表，保留每一条**，再加 `wb-project`：

```bash
hermes config get plugins.enabled        # 记下现有条目
hermes config set plugins.enabled '["<原有条目...>","wb-project"]'
hermes config get plugins.enabled        # 读回确认
```

> `hermes plugins enable wb-project` **对 dashboard 插件无效**（那只认 agent 插件，会报
> `No plugin named 'wb-project'`）。唯一杠杆是 `plugins.enabled`。

或用仓库自带脚本（会合并而不是覆盖）：

```bash
python tools/wb-enable.py            # dry-run：只打印将要写入的数组
python tools/wb-enable.py --apply    # 真的执行 hermes config set
```

### 2.4 写配置

复制示例并按本机改路径：

```bash
mkdir -p "$HERMES_HOME/plugin-data/wb-project"
cp config/project-roots.example.json "$HERMES_HOME/plugin-data/wb-project/project-roots.json"
```

### 2.5 **重启 dashboard（必须）**

```bash
# 停掉你正在跑的那一个（不要用 hermes dashboard --stop，见 §7）
hermes dashboard --port 9119 --host 127.0.0.1 --no-open --skip-build
```

插件发现结果与 `plugin_api.py` 的模块级常量**按进程缓存**。改了 Python、`config.yaml`
或新增插件目录，都必须重启一次后端；只刷新浏览器会表现为「路由 404 / 面板没反应」。
`dist/*.js`、`dist/*.css` 是静态资产，改完**只需刷新页面**（`Cache-Control: no-store`）。

---

## 3. 配置

### 3.1 扫描根：`plugin-data/wb-project/project-roots.json`

**只有这里登记过的绝对路径会被扫描**，接口永不接受客户端传路径。

```jsonc
{
  "active_root": "<活动项目根>",
  "archive_roots": [
    { "path": "<归档根>", "label": "ProjectArchives", "kind": "backup" },
    { "path": "<课程库根>/示例课程", "label": "课程库", "kind": "course" }
  ]
}
```

| 字段 | 含义 |
|---|---|
| `active_root` | 唯一的**活动根**。新建/纳管只在这里发生。 |
| `archive_roots[]` | 归档根，可多条。落在这些根下的项目 `archived=true`，**只读**。 |
| `label` | 出现在 UI 根清单与同名角标里 |

归档判定 = **物理位置**（项目在哪个根下），不是 `.pm` 里的 `project.archived` 标记。
`.pm` 标了 `archived=true` 但目录还在活动根下的项目，界面会显示「未迁移」角标并说明原因 ——
既不隐藏，也不自动搬动。

环境变量覆盖（测试/多机用）：

| 变量 | 默认 | 用途 |
|---|---|---|
| `WB_PROJECT_ROOT` | `<活动项目根>` | 活动根（无配置文件时） |
| `WB_PROJECT_COURSE_ROOT` | `<课程库根>/机器视觉` | 课程归档根默认值 |
| `WB_PROJECT_ROOTS_CONFIG` | `$HERMES_HOME/plugin-data/wb-project/project-roots.json` | 根配置文件位置 |
| `WB_PROJECT_MAX_TEXT_BYTES` | 524288 | 文本预览上限 |
| `WB_PROJECT_MAX_IMAGE_BYTES` | 20971520 | 图片预览上限 |
| `WB_PROJECT_MAX_RAW_BYTES` | 67108864 | 单次下载上限 |

### 3.2 Zotero

无配置项。后端读本地 API `http://127.0.0.1:23119/api/users/0`（Zotero 7）。
条件：Zotero 在运行 + 设置→高级→勾选「允许其他应用程序与本机 Zotero 通信」。
不可达时论文面板显示明确错误，其余功能照常。

集合匹配是**精确匹配**：先看本地已保存映射，再看集合名是否与项目名逐字符相等；
都不中就给候选让你显式选择，**不模糊猜测、不自动绑定**。

---

## 4. 文件格式：`.pm`（schema_version = 3）

`.pm` 是 ProjectManager 的外部副本，本项目只读它、按需新建它。
文件名 = 项目名 = 目录名（同一字符串，例 `2026-11_示例项目/2026-11_示例项目.pm`）。

最小合法结构（本项目创建的就是这个形状）：

```jsonc
{
  "version": "1.0",
  "schema_version": 3,
  "project": {
    "id": "<uuid>",
    "name": "2026-11_示例项目",
    "description": "",
    "template": "todo",
    "color": "#4f46e5",
    "created_at": "2026-11-21T10:00:00.000Z",
    "updated_at": "2026-11-21T10:00:00.000Z",
    "archived": false,
    "start_date": "2026-11-01",
    "end_date": "2026-12-31",
    "kanban_columns": ["待办", "进行中", "已完成"],
    "sync_enabled": true,
    "sort_order": 1000
  },
  "readme_file": "README.md",
  "template": { "dirs": [], "files": ["README.md"], "file_contents": { "README.md": "# …" } },
  "tasks": [
    {
      "id": "<uuid>",
      "title": "任务标题",
      "status": "进行中",              // 存的是**中文看板列名**：待办|进行中|已完成
      "priority": "medium",           // high|medium|low
      "due_date": "2026-12-01",
      "subtasks": [{ "id": "<uuid>", "title": "子任务", "done": false }],
      "tags": ["标签名"],              // 存标签名，不是 tag id
      "dependencies": [],
      "tracked_start": null,          // 真实开始时间；为空表示未真正开始
      "created_at": "2026-11-21T10:00:00.000Z",
      "updated_at": "2026-11-21T10:00:00.000Z",
      "recurrence": null              // 非空时 freq ∈ daily|weekly|monthly|yearly
    }
  ],
  "tags": [{ "id": "<uuid>", "name": "标签名", "color": "#4f46e5" }],
  "milestones": [{ "id": "<uuid>", "title": "中期", "date": "2026-11-20" }],
  "changelog": [{ "version": "0.1.0", "date": "2026-11-21", "info": "初始版本", "auto": false }],
  "flowchart": { "nodes": [], "edges": [] }
}
```

要点：

- 后端认两种落盘形状：`{"project": {...}, "tasks": [...]}` 与顶层扁平。
- **`storage`（本地文件夹路径）不写入 `.pm`** —— 它只活在 ProjectManager 的本机 localStorage。
  别处拿到的 `.pm` 导入后要重新指定文件夹。
- 甘特图的「起点」优先级：`tracked_start` > `created_at` > 项目 `start_date`，
  响应里用 `start_source` 明示来源；三者都没有就返回 `unknown`，不猜。
- 想让它出现在 ProjectManager 侧边栏，用 PM 的「导入」选中该 `.pm`；
  本插件不会写 ProjectManager 的 localStorage。

### 4.1 项目名规范

新建/纳管都要求 `YYYY-MM_名称`（例 `2026-11_航空航天机器人化制造课程论文`），并额外校验：

| 检查 | 拒绝示例 |
|---|---|
| 必须 `YYYY-MM_` 前缀，月份 01–12 | `没有日期前缀`、`2026-13_x` |
| Windows 非法字符 `<>:"/\|?*` | `2026-11_a/b` |
| Windows 保留名（`con`/`nul`/`com1`…） | `con` |
| 不能以点或空格开头/结尾 | `.2026-11_x` |
| 长度 ≤ 120，无控制字符 | — |

目录名与 `.pm` 文件名由同一字符串生成，避免「文件夹叫 A、文件叫 B」。

---

## 5. 预览支持与限制（实测结论）

判定标准是「**浏览器里稳定、安全**」，不是「理论上能渲染」。下列结论都在真机
Chrome / Edge 上跑过（脚本见 `tests/probe_preview*.py`）。

| 格式 | 处理 | 依据 |
|---|---|---|
| `.txt .md .json .csv .yaml .toml .py .js .css .tex .log …` | **纯文本**（`textContent`），等宽、保留换行 | React 文本节点天然不解析 HTML |
| `.png .jpg .jpeg .gif .webp .bmp .ico .avif` | 内联 `<img>`（经 blob） | 实测可显示 |
| `.svg` | 内联 `<img>`，**内联 `<script>` 不执行** | 见 §6 安全边界 |
| `.pdf` | 交给浏览器内置阅读器（`<iframe>` + blob） | 真机 Chrome/Edge 渲染成功；**headless Chromium 下为空白**，属环境限制 |
| `.docx .doc .xlsx .xls .pptx .ppt .odt .zip .7z .rar .exe …` | **只下载**，`application/octet-stream` + `attachment` | 浏览器内解析器不可控 |
| 未知扩展 / 二进制（含大量 NUL）/ 解码失败 | 只下载，并说明原因 | — |

另外：

- 文本预览上限 512 KB，超出**截断并显式标注**（不静默丢内容）。
- UTF-8 优先；解码失败按 **GB18030** 兜底一次并标注 `decode_error`。
- 单文件 > 64 MB：不下发，直接 413。
- 基名命中敏感名单（`.env`、`id_rsa`、`*.pem`、`auth.json`、`credentials.json`、
  `.git`、`node_modules` …）的文件**不进列表、不可预览、不可下载**。

---

## 6. 安全边界（实现约束，不是建议）

后端只接受 **(已登记的项目 id, 项目内相对路径)**，没有任何「传任意路径」的接口。

| 威胁 | 处理 |
|---|---|
| 目录穿越 `../`、绝对路径、盘符、反斜杠、NUL、控制字符 | 400 拒绝（分量逐一校验） |
| symlink / Windows junction / 其它重解析点逃逸 | 403；每级 `lstat` + `realpath` 必须仍在项目真实目录内 |
| 未知项目 id | 404（永不把参数当路径） |
| 敏感文件泄漏 | 服务端剔除并 403，不依赖前端隐藏 |
| 恶意 SVG/HTML 执行脚本 | 见下 |
| 超大目录 | 单层 `offset/limit` 分页，`total/has_more` 如实，硬上限 500/层 |
| 非图片/PDF 被当 inline 渲染 | 强制 `attachment` + `octet-stream` |
| MIME 嗅探 | 所有预览/下载响应带 `X-Content-Type-Options: nosniff` |
| 跨站读取 | `Cross-Origin-Resource-Policy: same-origin` |

### SVG / HTML 脚本实测

- `<img src="…svg">`：SVG 内联 `<script>` **不执行**（图片文档不跑脚本）。
- 直接导航该 SVG URL（用户手动打开）：仅靠 `<img>` 不够，所以 `raw` 响应固定带
  `Content-Security-Policy: sandbox; default-src 'none'; …` —— 实测带该头后
  内联脚本被拦（无头与有头 Chrome/Edge 均验证）。
- 文本类文件**永不 inline**：即使请求 `inline=1`，也返回 `attachment` +
  `application/octet-stream`，避免浏览器把它当 HTML/JS 解释。

### 归档只读

归档根下的项目可浏览、可预览、可下载，但**没有写入口**：新建/纳管只在 `active_root`，
且目标目录必须不存在（新建）或尚无 `.pm`（纳管）。落盘前还有一道 `os.replace` 前的存在性检查。

---

## 7. 运维注意

- **不要用 `hermes dashboard --stop`**：它停掉本机所有 dashboard/serve 进程，
  可能连带杀掉你远程桌面正在连的后端。要重启就按 PID 停那一个，或换端口起一个新的。
- 插件后端按进程缓存：改 `plugin_api.py` / `plugins.enabled` / 新增插件目录 → **重启后端**。
  改 `dist/*.js|css` → 只需刷新页面。
- `hermes serve` 是无头后端（`web UI disabled`）：插件路由可用，但页面 404。要看见 UI 必须 `hermes dashboard`。
- 端口冲突时 `hermes dashboard` 会以 `EX_CONFIG (78)` 退出并指出占用者；换端口做临时验证。

---

## 8. 自测

```bash
# 纯后端（tmp 目录数据，不碰你的真实项目）
python -m pytest tests/test_wb_project_files.py -q          # 70+
python -m pytest tests/test_wb_project_archive_roots.py -q

# 浏览器（需要一个回环端口上的 dashboard；示例用 9147，别用生产端口）
hermes dashboard --port 9147 --host 127.0.0.1 --no-open --skip-build
WB_PROJECT_UI=http://127.0.0.1:9147 python -m pytest tests/test_wb_project_files_ui.py -q

# 预览能力复验（安全结论的来源）
python tests/probe_preview6.py     # 直接导航 SVG：CSP 是否拦住内联脚本
python tests/probe_preview7.py     # PDF：attachment vs inline 的渲染差异
```

前置缺失时用例 **skip** 而非假通过（没有 Playwright / 没有 dashboard / Zotero 未启动）。

---

## 9. 目录结构

```
plugin/dashboard/            # 要复制进 $HERMES_HOME/plugins/wb-project/ 的内容
  manifest.json
  plugin_api.py              # FastAPI 路由（只读扫描 + 文件浏览 + 创建/纳管）
  dist/index.js              # 纯 IIFE，无构建步骤
  dist/style.css             # 全部走 --color-* 主题变量
  dist/vendor/wb-agent-chat.*  # vendored 共享聊天组件（见 shared/README.md）
config/                      # 配置示例（泛化，不含真实路径）
shared/wb-agent-chat/        # 共享聊天组件源码
tools/sync-shared.py         # 把 shared/ 同步进 dist/vendor/（--check 供 CI）
tools/wb-enable.py           # 合并式生成 plugins.enabled
tests/                       # 后端 / 浏览器 / 预览安全探针
docs/                        # 安全评估与测试依据
```

## 10. 许可

插件代码：MIT，见 `LICENSE`。
`dist/vendor/wb-agent-chat.*` 为本项目原创、同许可。
第三方依赖（React / FastAPI 等）由 Hermes 宿主提供，不在本仓库内分发；声明见 `NOTICE`。
