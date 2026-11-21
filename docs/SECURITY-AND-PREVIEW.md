# 安全与预览可行性评估

本文件记录的是**实测依据**，不是设计意图。每条结论都能用 `tests/` 下的脚本复现。
不能稳定成立的能力**没有实现**；下面明确写出哪些格式被降级为只下载。

环境：Windows 11 · Chrome 与 Edge（playwright `channel=chrome|msedge`，headless 与 headed 各跑一遍）
· Python 3.11 · playwright 1.62。

---

## 1. 「内联预览」到底安全不安全（SVG / HTML）

恶意 SVG 带内联脚本，是这类文件浏览器最常见的 XSS 面。四轮探针的结论：

| 场景 | 结果 | 依据 |
|---|---|---|
| `<img src="x.svg">`，SVG 内有 `<script>` | **不执行** | `probe_preview3.py`：`img_svg_painted=true`，`top_has_svg_var=false`，`doc_title` 未被脚本改写 |
| 直接导航 `x.svg`（用户手动打开该 URL），响应**无** CSP | **执行**（`document.title` 被改成 `pwned`） | `probe_preview6.py`：`svg_direct|plain|headless=False` → `ran: true` |
| 直接导航，响应带 `CSP: default-src 'none'` | **不执行** | `probe_preview6.py`：`ran: false` |
| 直接导航，响应带 `CSP: sandbox; default-src 'none'` | **不执行** | `probe_preview6.py`：`ran: false` |
| 带该 CSP 的 SVG 仍能正常显示 | 能 | `probe_preview5.py`：`in_img {w:160,h:60,ok:true}` |

**采取的措施**（`plugin_api.py` 的 `project_raw`）：

```
Content-Type: image/svg+xml            （仅 .svg / 位图 / .pdf 才 inline）
X-Content-Type-Options: nosniff
Content-Security-Policy: sandbox; default-src 'none'; style-src 'unsafe-inline';
                         img-src 'self' data:; object-src 'none'; frame-ancestors 'self'
Cross-Origin-Resource-Policy: same-origin
```

即：正常路径用 `<img>`（本来就不跑脚本），异常路径（直接打开 URL）由 `sandbox` 兜住。
两条路都堵上，才允许 SVG 进预览白名单。

> 为什么不用「服务端清洗 SVG」：清洗会改变用户文件内容，也会让「预览」和「下载」不一致。
> 用响应头 + `<img>` 保持文件原样，同时脚本不执行。

---

## 2. PDF

| 场景 | 结果 | 依据 |
|---|---|---|
| headed Chrome，同源 `<iframe src="x.pdf">` | **渲染成功**（13 种颜色，说明真的画出了页面与文字） | `probe_preview7.py` `plain: colors=13` |
| headed Chrome + `CSP: default-src 'none'` | 仍渲染 | `csp_none: colors=13` |
| headed Chrome + `CSP: sandbox; …` | 仍渲染 | `csp_sandbox: colors=13` |
| 响应带 `Content-Disposition: attachment` | **空白**（`colors=1`，iframe 变 `about:blank`） | `attachment: colors=1` |
| headless Chromium | **空白** | `probe_preview3/6`：`colors=1~2` |

**采取的措施**：

- PDF 允许 `inline`，且**不能**带 `attachment`（这是它能否渲染的开关）；
- 前端不是把 URL 塞进 `<iframe src>`，而是 `authedFetch → blob → iframe src=blob:`——
  这样 token/cookie 模式都成立，也不受 `Content-Disposition` 波动影响（`probe_blob.py`：
  Chrome 与 Edge 均 `colors=13`）；
- iframe 带 `sandbox=""`（不给 `allow-scripts`）；
- 文案明确写「无头浏览器/无 PDF 插件环境会空白，请下载后查看」——**不假装支持**。

**结论**：PDF 预览在真实浏览器成立，在无头环境不成立 → 保留预览，但把限制写进 UI 与 README。

---

## 3. 目录穿越与链接逃逸

实现是**逐级校验 + realpath 兜底**（`_safe_rel` → `_walk_join`）：

1. 只接受相对路径；`/`、`\`、盘符前缀、`..`、NUL、控制字符、反斜杠一律 400；
2. 每个分量 `lstat()`，命中 `FILE_ATTRIBUTE_REPARSE_POINT` 或是 `is_symlink()` → 403
   （Windows junction 走这条，不只查 `is_symlink`）；
3. 最终 `realpath` 必须仍在项目真实目录内，否则 403。

测试（`tests/test_wb_project_files.py`，全部走真实 HTTP）：

| 输入 | 期望 | 用例 |
|---|---|---|
| `../outside/secret.txt`、`docs/../../outside/…`、`..`、`..\..\x` | 400/403 | `test_path_traversal_rejected`（8 组参数） |
| `/etc/passwd`、`<system-path>/win.ini`、`<system-path>\win.ini` | 400/403 | 同上 |
| `docs\guide.md`（反斜杠绕过） | 400 | 同上 |
| `docs/\x00`、`\x01bad` | 400/403 | 同上 |
| 未知项目 id、`<活动项目根>` 当 id | 404 | `test_unknown_project_id_is_404` |
| symlink 目录 / symlink 文件 | 403 | `test_symlink_escape_is_refused` |
| Windows junction 目录 | 403 | `test_windows_junction_escape_is_refused`（`mklink /J` 真实创建） |

> 本机管理员权限下 `symlink` 不可建（两条用例 skip 并打印原因），但 junction 用例**真的跑了**，
> 证明重解析点被拦。`_is_link()` 同时覆盖两者，所以 symlink 走同一条代码路径。

---

## 4. 敏感文件

剔除在**服务端列表阶段**完成（`SENSITIVE_NAMES` / `SENSITIVE_DIRS` / `SENSITIVE_SUFFIXES`），
不是前端隐藏，所以「列表里没有」等于「接口也拿不到」：

| 类别 | 例子 | 行为 |
|---|---|---|
| 环境变量/密钥 | `.env`、`.env.*`、`id_rsa`、`*.pem`、`*.key`、`*.kdbx` | 不进列表；`/file` 与 `/raw` 直接 403 |
| 凭据 | `auth.json`、`credentials.json`、`.git-credentials`、`.npmrc`、`.netrc` | 同上 |
| VCS / 依赖目录 | `.git`、`.hg`、`.svn`、`node_modules`、`.venv`、`__pycache__` | 同上 |
| 云/容器凭据目录 | `.ssh`、`.aws`、`.gnupg`、`.kube`、`.docker`、`.azure` | 同上 |

用例：`test_sensitive_files_never_listed`、`test_sensitive_files_cannot_be_read_even_by_name`
（正向断言 403，不是「看起来没显示」）。

**没有**把敏感文件打包下载的接口：本插件没有任何「整包导出」端点，也不把 `dir_path` 暴露给
远程调用方以外的地方（`dir_path` 本来就由 `/projects` 返回给已登录的 dashboard 使用者）。

---

## 5. 大小 / 编码 / 超大目录

| 项 | 上限 | 超限行为 | 用例 |
|---|---|---|---|
| 文本预览 | 512 KB（`WB_PROJECT_MAX_TEXT_BYTES`） | 截断 + `truncated:true` + `notice` 写明原始字节 | `test_text_truncation_is_labelled` |
| 图片预览 | 20 MB | 只下载 + 说明 | — |
| 单次下载 | 64 MB | `413` | `test_oversized_file_download_only` |
| 单层条目 | 500（硬上限），默认 200 | `offset/limit` 分页，`total`/`has_more` 如实 | `test_large_directory_pagination` |
| 目录深度 | 无硬限制，靠懒加载（一次一层） | 不会整树扫描 | `test_tree_lists_one_level_only` |

编码：

- UTF-8（含 BOM）优先；
- 失败按 **GB18030** 兜底一次，并在 `decode_error` 里标注（中文 Windows 的典型编码）；
- 两种都失败，或 NUL 字节占比 > 2% → 判定二进制，`kind=download`，不给文本渲染。

用例：`test_text_preview_gb18030_fallback`、`test_binary_disguised_as_text_degrades_to_download`。

---

## 6. 为什么这些格式不预览

| 格式 | 决定 | 理由 |
|---|---|---|
| `.docx .doc .xlsx .xls .pptx .ppt .odt` | 只下载 | 都是 zip/复合容器；浏览器内没有稳定解析器，纯前端口径要么引第三方库（体积/许可/攻击面），要么解析出错给用户看假内容 |
| `.zip .7z .rar .tar.gz` | 只下载 | 同上 |
| `.exe .msi .dll .iso .bin` | 只下载 | 无预览语义 |
| 未知扩展 | 只下载 | 不猜 |
| `.pdf` | 预览（有环境限制） | §2 已实测 |
| 文本 / 图片 / SVG | 预览 | §1 已实测脚本不执行 |

所有非图片/PDF 的 `/raw` 响应固定：

```
Content-Type: application/octet-stream
Content-Disposition: attachment; filename*=UTF-8''…
X-Content-Type-Options: nosniff
```

**文本文件也走 attachment**：即使请求 `inline=1` 也不给 `text/html`——否则浏览器可能把它当
HTML/JS 解释（`test_raw_text_never_inline`）。文本预览走的是 JSON 接口 + `textContent`。

---

## 7. 创建 / 纳管项目的写入安全

| 风险 | 处理 | 用例 |
|---|---|---|
| 覆盖已有项目 | 新建要求目录**不存在**；纳管要求目录存在且**无 `.pm`**；两道检查（plan 时 + 落盘前） | `test_new_rejects_existing_dir`、`test_adopt_rejects_already_tracked`、`test_create_is_idempotent_refused` |
| 计划与执行不一致（用户看的是 A，写的是 B） | `plan_id = sha256(payload)[:32]`，`/create` 必须带上，不匹配 409 | `test_create_requires_matching_plan_id`、`test_plan_id_is_stable_for_same_params` |
| 并发下被别人抢先纳管 | 落盘前再次检查 `.pm` 是否存在，存在则 409 且**不覆盖** | `test_adopt_refuses_when_pm_appears_between_plan_and_create` |
| 半截文件被扫描到 | 同目录临时文件 + `os.replace` 原子替换 | 实现 `projects_create` |
| 写到归档根 | 写端点只解析 `active_root`；归档根没有任何写入口 | `test_write_endpoints_only_touch_active_root` |
| 路径注入（名字里带分隔符） | `<>:"/\|?*` + 保留名 + `YYYY-MM_` 规范 + 控制字符校验 | `test_project_name_validation_rejects`（12 组参数）、`test_windows_reserved_name_rejected` |
| 纳管改坏已有文件 | 只新增 `.pm` 与（缺省时）`README.md`，不动其它文件 | `test_adopt_does_not_touch_existing_files`（断言原文件字节不变） |
| 「写成功」当验收 | 落盘后重新解析 `.pm` 并重扫 `/projects`，返回 `verify` | `test_create_round_trip_and_verify` |

---

## 8. 结论汇总

- **做了**：文本 / Markdown / JSON / 代码 纯文本预览；位图与 SVG 内联预览（脚本不执行）；
  PDF 预览（真机成立，无头不成立并已声明）；单层懒加载目录树；分页；只读归档浏览；
  新建/纳管（预览 → 确认 → 读回校验）。
- **没做（并已降级为下载）**：Office 全系、压缩包、可执行、未知二进制。
- **没有做的原因**：无法在浏览器里稳定且安全地渲染，任何「尽力而为」的解析都会给用户看错内容。
- **不做的**：任何接受任意绝对路径的接口；任何把项目目录整体打包下发的接口。
