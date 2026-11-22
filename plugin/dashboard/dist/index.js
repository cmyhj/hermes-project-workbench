/*
 * wb-project — 项目区：项目进度甘特图 + 关联 Zotero 论文
 *
 * ── 约束简报（先定层级，再写 markup）────────────────────────────────
 * 主（primary）：甘特图本身（条形位置 = 截止日；今天线 = 眼下的注意力落点）
 *               一个主动作：在未匹配项目上「选择 Zotero 集合」
 * 次（secondary）：项目列表（左栏，选中态唯一）、状态计数（3 个数字，非 6 个）
 * 三（tertiary）：任务行细节（优先级、子任务进度、标签）、论文作者/年份
 *
 * 设计令牌：全部取 Dashboard 主题变量（--color-*），不写死 hex，
 * 唯一硬编码的是「今天线」与状态色相，明暗主题下都保持 ≥3:1 的条块可辨识度。
 *
 * 布局：左栏 260px 项目列表 / 右侧 甘特图 + 论文；<1000px 折成上下堆叠。
 * 视口：时间轴按项目 [最早起点..最晚截止] 自适应，超出右边界只在有日期时拉伸。
 *
 * 无 CDN：甘特图是手写 SVG，不引第三方库（任务量级 10–40 条，够用且零体积）。
 * 纯 IIFE + React.createElement，不 import、不 JSX。
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useMemo = SDK.hooks.useMemo;
  var useCallback = SDK.hooks.useCallback;
  var useRef = SDK.hooks.useRef;
  var fetchJSON = SDK.fetchJSON;

  var BASE = "/api/plugins/wb-project";
  var NS = "wbp";

  // ---------------------------------------------------------------- 共享聊天组件
  // 唯一源码在仓库 shared/wb-agent-chat/，由 tools/sync-shared.py 复制到本插件的
  // dist/vendor/。插件静态资产用 is_relative_to 锁死在本插件目录内，所以**不能**
  // 写 ../shared/... 跨目录引用 —— 这里的 vendored 副本就是官方允许的复用方式。
  var CHAT_VENDOR = "/dashboard-plugins/wb-project/dist/vendor/";
  var chatPromise = null;

  function loadSharedChat() {
    if (window.__WB_CHAT__) return Promise.resolve(window.__WB_CHAT__);
    if (chatPromise) return chatPromise;
    chatPromise = new Promise(function (resolve, reject) {
      if (!document.querySelector('link[data-wb-chat-css]')) {
        var link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = CHAT_VENDOR + "wb-agent-chat.css";
        link.setAttribute("data-wb-chat-css", "1");
        document.head.appendChild(link);
      }
      if (document.querySelector("script[data-wb-chat-js]")) {
        // 另一个实例已在加载：等它的回调，避免重复注入
        var prev = window.__WB_CHAT_READY__;
        window.__WB_CHAT_READY__ = function (api) {
          if (typeof prev === "function") prev(api);
          resolve(api);
        };
        return;
      }
      window.__WB_CHAT_READY__ = function (api) { resolve(api); };
      var s = document.createElement("script");
      s.src = CHAT_VENDOR + "wb-agent-chat.js";
      s.async = true;
      s.setAttribute("data-wb-chat-js", "1");
      s.onerror = function () {
        reject(new Error("共享聊天组件加载失败：" + s.src + "（运行 tools/sync-shared.py 后刷新）"));
      };
      document.head.appendChild(s);
    });
    return chatPromise;
  }

  // ---------------------------------------------------------------- 主题色
  // 状态色只用三个，和「待办/进行中/已完成」一一对应；色相取自主题无关的
  // 中性-琥珀-青绿三元组，明暗都在 3:1 以上。
  var STATUS_COLORS = {
    done: "#2f8f5b",
    doing: "#c97a12",
    todo: "#6b7280",
  };
  var STATUS_LABEL = { done: "已完成", doing: "进行中", todo: "待办" };

  // ---------------------------------------------------------------- 日期
  function parseDay(s) {
    if (!s || typeof s !== "string") return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
    if (!m) return null;
    return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  }

  function dayDiff(a, b) {
    return Math.round((b.getTime() - a.getTime()) / 86400000);
  }

  function addDays(d, n) {
    var x = new Date(d.getTime());
    x.setDate(x.getDate() + n);
    return x;
  }

  function fmtDay(d) {
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }

  function todayLocal() {
    var n = new Date();
    return new Date(n.getFullYear(), n.getMonth(), n.getDate());
  }

  // ---------------------------------------------------------------- 小组件
  function Stat(props) {
    return h("div", { className: NS + "-stat" },
      h("span", { className: NS + "-stat-num", style: props.color ? { color: props.color } : null },
        String(props.value)),
      h("span", { className: NS + "-stat-label" }, props.label));
  }

  function StatusPill(props) {
    var g = props.group || "todo";
    return h("span", { className: NS + "-pill " + NS + "-pill-" + g },
      h("i", { className: NS + "-dot", style: { background: STATUS_COLORS[g] } }),
      STATUS_LABEL[g] || props.status || "待办");
  }

  function Empty(props) {
    return h("div", { className: NS + "-empty" },
      h("p", { className: NS + "-empty-title" }, props.title),
      props.hint ? h("p", { className: NS + "-empty-hint" }, props.hint) : null);
  }

  // ---------------------------------------------------------------- 甘特图
  // 行 = 任务；条 = [起点 .. 截止日]。起点缺失时只画「截止点」标记并明示来源，
  // 不拿假起点撑满一条 —— 这正是需求里「缺日期明确未知不可伪造」的 UI 侧落实。
  function Gantt(props) {
    var allTasks = props.tasks || [];
    var milestones = props.milestones || [];
    var LABEL_W = 208;
    var ROW_H = 26;

    // 只有带截止日的任务能定位条形。其余集中成一行「N 项无截止日」的说明，
    // 而不是每项都占一行画「无截止日」—— 那会把图表变成噪声，也不诚实。
    var tasks = allTasks.filter(function (t) { return t.due; });
    var unscheduled = allTasks.filter(function (t) { return !t.due; });

    if (!tasks.length) {
      return h(Empty, {
        title: "本项目暂无带截止日的任务",
        hint: "甘特图需要 due_date 才能定位条形；" + unscheduled.length +
          " 项任务没有日期，如实留空，不推算。",
      });
    }

    var anchors = [];
    tasks.forEach(function (t) {
      var s = parseDay(t.start), d = parseDay(t.due);
      if (s) anchors.push(s);
      if (d) anchors.push(d);
    });
    milestones.forEach(function (m) {
      var d = parseDay(m.date);
      if (d) anchors.push(d);
    });

    var minD = new Date(Math.min.apply(null, anchors));
    var maxD = new Date(Math.max.apply(null, anchors));
    // 两端各留 3 天呼吸，且保证至少 14 天跨度（避免单日项目被拉成一条线）
    minD = addDays(minD, -3);
    maxD = addDays(maxD, 3);
    if (dayDiff(minD, maxD) < 14) maxD = addDays(minD, 14);

    var span = Math.max(1, dayDiff(minD, maxD));
    var chartW = Math.max(420, Math.min(980 - LABEL_W, 40 + span * 7));
    var W = LABEL_W + chartW + 16;

    function xOf(d) {
      return LABEL_W + (dayDiff(minD, d) / span) * chartW;
    }

    var today = todayLocal();
    var todayX = (today >= minD && today <= maxD) ? xOf(today) : null;

    // 月份网格线
    var ticks = [];
    var cursor = new Date(minD.getFullYear(), minD.getMonth(), 1);
    while (cursor <= maxD) {
      var nx = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1);
      if (nx > minD) ticks.push({ at: new Date(cursor), x: xOf(cursor) });
      cursor = nx;
      if (ticks.length > 60) break;
    }

    var height = tasks.length * ROW_H + 34;

    var rows = tasks.map(function (t, i) {
      var y = i * ROW_H + 26;
      var due = parseDay(t.due);
      var start = parseDay(t.start);
      var color = STATUS_COLORS[t.status_group] || STATUS_COLORS.todo;
      var parts = [];

      // 仅偶行铺底，给行一个视觉锚点；奇行保持透空避免条纹噪声
      if (i % 2 === 0) {
        parts.push(h("rect", {
          key: "bg", x: LABEL_W, y: y - 8, width: chartW, height: ROW_H - 8,
          fill: "currentColor", opacity: 0.035,
        }));
      }

      var x1 = xOf(due);
      // 真实条 vs 推算条：只有 tracked_start 才是「真的这个区间」。
      // created_at / 项目 start_date 推出来的区间一律降权 + 虚线描边，
      // 免得读者以为任务真从那天做起了（四个现存项目 tracked_start 全为空）。
      var measured = t.start_source === "tracked";
      if (start) {
        var x0 = Math.min(xOf(start), x1);
        parts.push(h("rect", {
          key: "bar", x: x0, y: y - 4, width: Math.max(4, x1 - x0), height: 12,
          rx: 3, fill: color, opacity: measured ? 0.95 : 0.5,
          stroke: measured ? "none" : color,
          strokeWidth: measured ? 0 : 1.4,
          strokeDasharray: measured ? null : "4 2.5",
          style: { cursor: "pointer" },
          onClick: function () { props.onPick && props.onPick(t); },
        }, h("title", null, t.title + " · " + t.start + " → " + t.due +
          (measured ? "" : "（起始日为推算：" + (t.start_source === "project" ? "项目起始" : "任务创建") + "）"))));
      } else {
        // 起点未知：只画截止点，形状上和「有区间」区分开
        parts.push(h("circle", {
          key: "dot", cx: x1, cy: y + 2, r: 5, fill: color,
          style: { cursor: "pointer" },
          onClick: function () { props.onPick && props.onPick(t); },
        }, h("title", null, t.title + " · 截止 " + t.due + "（无起始日）")));
      }
      // 截止日：右边放得下就贴右，否则贴左边，避免最右一列标签被裁掉
      if (x1 + 46 < W - 6) {
        parts.push(h("text", {
          key: "due", x: x1 + 8, y: y + 7, className: NS + "-due",
        }, t.due.slice(5)));
      } else {
        parts.push(h("text", {
          key: "due", x: x1 - 8, y: y + 7, className: NS + "-due",
          textAnchor: "end",
        }, t.due.slice(5)));
      }

      parts.push(h("text", {
        key: "label", x: 0, y: y + 7, className: NS + "-rowlabel",
        onClick: function () { props.onPick && props.onPick(t); },
      }, t.title.length > 30 ? t.title.slice(0, 29) + "…" : t.title));

      return h("g", { key: t.id || i }, parts);
    });

    // 未排期任务：一行说明 + 截断的清单（完整清单在 tooltip 里，避免窄屏溢出）
    if (unscheduled.length) {
      var y0 = tasks.length * ROW_H + 26;
      var fullList = unscheduled.map(function (t) { return t.title; }).join(" · ");
      var maxChars = Math.max(16, Math.floor(chartW / 6));
      rows.push(h("g", { key: "unsched" },
        h("text", { x: 0, y: y0 + 7, className: NS + "-nodate" },
          unscheduled.length + " 项无截止日"),
        h("text", { x: LABEL_W + 8, y: y0 + 7, className: NS + "-nodate" },
          fullList.length > maxChars ? fullList.slice(0, maxChars - 1) + "…" : fullList,
          h("title", null, fullList))));
      height = y0 + ROW_H + 12;
    }

    var msMarks = milestones.map(function (m, i) {
      var d = parseDay(m.date);
      if (!d) return null;
      var x = xOf(d);
      return h("g", { key: "ms" + i },
        h("line", {
          x1: x, x2: x, y1: 14, y2: height - 4,
          stroke: m.color || "currentColor", strokeWidth: 1, strokeDasharray: "2 3", opacity: 0.55,
        }),
        h("circle", { cx: x, cy: 14, r: 3.2, fill: m.color || "currentColor" },
          h("title", null, "里程碑 · " + m.title + " · " + m.date)));
    }).filter(Boolean);

    return h("div", { className: NS + "-gantt-wrap" },
      h("svg", {
        className: NS + "-gantt",
        viewBox: "0 0 " + W + " " + height,
        width: "100%",
        height: height,
        role: "img",
        "aria-label": "项目任务甘特图",
      },
        // 月份网格
        ticks.map(function (tk, i) {
          return h("g", { key: "tk" + i },
            h("line", {
              x1: tk.x, x2: tk.x, y1: 18, y2: height,
              stroke: "currentColor", strokeWidth: 0.5, opacity: 0.18,
            }),
            h("text", { x: tk.x + 3, y: 12, className: NS + "-tick" },
              tk.at.getFullYear() + "-" + String(tk.at.getMonth() + 1).padStart(2, "0")));
        }),
        // 今天线
        todayX !== null ? h("g", { key: "today" },
          h("line", {
            x1: todayX, x2: todayX, y1: 16, y2: height,
            stroke: "#e05252", strokeWidth: 1.5, opacity: 0.95,
          }),
          h("title", null, "今天 " + fmtDay(today))) : null,
        msMarks,
        rows),
      h("div", { className: NS + "-legend" },
        Object.keys(STATUS_COLORS).map(function (k) {
          return h("span", { key: k, className: NS + "-legend-item" },
            h("i", { className: NS + "-dot", style: { background: STATUS_COLORS[k] } }),
            STATUS_LABEL[k]);
        }),
        h("span", { className: NS + "-legend-item" },
          h("i", { className: NS + "-legend-today" }), "今天"),
        milestones.length ? h("span", { className: NS + "-legend-item" },
          h("i", { className: NS + "-legend-ms" }), "里程碑") : null,
        // 全项目 tracked_start 目前都为空，条形区间是推算的 —— 必须讲明白
        tasks.some(function (t) { return t.start_source !== "tracked"; })
          ? h("span", { className: NS + "-legend-item " + NS + "-legend-note" },
            h("i", { className: NS + "-legend-dashed" }), "虚线条 = 起止为推算")
          : null));
  }

  // ---------------------------------------------------------------- 项目文件夹（懒加载）
  // 后端契约（plugin_api.py 的实现与 tests/test_wb_project_files.py 一起定义）：
  //   GET /projects/<id>/tree?path=&offset=&limit=   一次一层，绝不整树
  //   GET /projects/<id>/file?path=               预览描述（text 给原文，image/pdf 给 raw_url）
  //   GET /projects/<id>/raw?path=&download=      受控字节流（非图片/PDF 一律附件）
  // 安全：客户端只传「项目 id + 相对路径」；路径安全、敏感文件、大小与格式判定**全在后端**。
  // 前端职责：文本只用 textContent 渲染（绝不 innerHTML/不执行脚本）；图片与 PDF 走
  // authedFetch → blob → <img>/<iframe>，不把带 token 的 URL 塞进 src。
  var FILE_LIMIT = 200;

  function apiUrl(path, params) {
    var q = [];
    if (params) {
      Object.keys(params).forEach(function (k) {
        var v = params[k];
        if (v === undefined || v === null || v === "") return;
        q.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
      });
    }
    return BASE + path + (q.length ? "?" + q.join("&") : "");
  }

  function fmtSize(n) {
    if (typeof n !== "number") return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  var FILE_ICON = {
    dir: "▸", text: "≡", image: "▣", pdf: "▤", download: "↓", other: "·",
  };

  /** 一个目录节点：展开时才请求（懒加载），收起即释放。 */
  function DirNode(props) {
    var [open, setOpen] = useState(props.depth === 0);
    var [state, setState] = useState({ loading: false, error: null, data: null });
    var [offset, setOffset] = useState(0);

    var load = useCallback(function (nextOffset) {
      var off = typeof nextOffset === "number" ? nextOffset : 0;
      setState(function (s) { return { loading: true, error: s.error, data: s.data }; });
      return fetchJSON(apiUrl("/projects/" + encodeURIComponent(props.projectId) + "/tree",
        { path: props.path, offset: off, limit: FILE_LIMIT }))
        .then(function (d) {
          setOffset(off);
          setState({
            loading: false, error: null,
            data: off === 0 ? d : Object.assign({}, d, {
              entries: ((state.data && state.data.entries) || []).concat(d.entries),
            }),
          });
          return d;
        })
        .catch(function (e) {
          setState({ loading: false, error: String(e && e.message || e), data: null });
          return null;
        });
    }, [props.projectId, props.path, state.data]);

    useEffect(function () {
      if (open && !state.data && !state.loading) load(0);
    }, [open]);

    var d = state.data;
    var kids = (d && d.entries) || [];
    var label = props.name || (d && d.dir_name) || "项目目录";

    return h("div", { className: NS + "-fnode" },
      h("button", {
        className: NS + "-frow " + NS + "-frow-dir",
        onClick: function () { setOpen(!open); },
        title: props.path ? "展开/收起 " + props.path : "展开/收起项目根目录",
        "aria-expanded": open,
      },
        h("span", { className: NS + "-fcaret" }, FILE_ICON.dir),
        h("span", { className: NS + "-fname" }, label),
        d ? h("span", { className: NS + "-fcount" }, d.total) : null),
      open
        ? h("div", { className: NS + "-fkids" },
          state.loading ? h("p", { className: NS + "-fhint" }, "读取目录…") : null,
          state.error
            ? h("p", { className: NS + "-err" }, "目录不可读：" + state.error)
            : null,
          kids.map(function (e) {
            return e.kind === "dir"
              ? h(DirNode, {
                key: e.path, projectId: props.projectId, path: e.path,
                name: e.name, depth: (props.depth || 0) + 1,
                // 必须透传：否则第二层以下的文件点了不会有反应（实测踩到）
                activeFile: props.activeFile, onOpen: props.onOpen,
              })
              : h(FileRow, {
                key: e.path, projectId: props.projectId, entry: e,
                active: props.activeFile === e.path,
                onOpen: props.onOpen,
              });
          }),
          d && d.has_more
            ? h("button", {
              className: NS + "-btn " + NS + "-btn-quiet " + NS + "-fmore",
              disabled: state.loading,
              onClick: function () { load(offset + FILE_LIMIT); },
            }, "继续加载（已显示 " + kids.length + " / " + d.total + "）")
            : null,
          d && !kids.length && !state.loading
            ? h("p", { className: NS + "-fhint" }, "（空目录，或全部是敏感/隐藏文件）")
            : null)
        : null);
  }

  function FileRow(props) {
    var e = props.entry;
    var icon = FILE_ICON[e.category] || FILE_ICON.other;
    return h("button", {
      className: NS + "-frow " + NS + "-frow-file" +
        (props.active ? " " + NS + "-frow-active" : ""),
      onClick: function () { props.onOpen && props.onOpen(e); },
      title: e.path + (e.size != null ? " · " + fmtSize(e.size) : "") +
        (e.previewable ? " · 可预览" : e.category === "download" ? " · 仅下载" : ""),
    },
      h("span", { className: NS + "-ficon " + NS + "-ficon-" + (e.category || "other") }, icon),
      h("span", { className: NS + "-fname" }, e.name),
      e.size != null ? h("span", { className: NS + "-fsize" }, fmtSize(e.size)) : null);
  }

  /** 预览：文本原样显示；图片/PDF 走 blob；其余只给下载。 */
  function FilePreview(props) {
    var projectId = props.projectId;
    var entry = props.entry;
    var [meta, setMeta] = useState(null);
    var [error, setError] = useState(null);
    var [blobUrl, setBlobUrl] = useState(null);
    var [blobKind, setBlobKind] = useState(null);   // "image" | "pdf"
    var holder = useRef(null);

    useEffect(function () {
      setMeta(null); setError(null); setBlobUrl(null); setBlobKind(null);
      fetchJSON(apiUrl("/projects/" + encodeURIComponent(projectId) + "/file",
        { path: entry.path }))
        .then(function (d) { setMeta(d); })
        .catch(function (e) { setError(String(e && e.message || e)); });
    }, [projectId, entry.path]);

    // 图片 / PDF：用带鉴权的 fetch 取 blob，再交给 <img>/<iframe>；
    // 不把 token 放进 src，也避免 raw 的 inline 头在部分浏览器里被当下载。
    useEffect(function () {
      if (!meta || (meta.kind !== "image" && meta.kind !== "pdf")) return;
      var alive = true;
      var made = null;
      var f = (SDK.authedFetch || fetch)(apiUrl("/projects/" + encodeURIComponent(projectId) +
        "/raw", { path: entry.path }));
      f.then(function (r) {
        if (!r || !r.ok) throw new Error("读取失败 HTTP " + (r && r.status));
        return r.blob();
      }).then(function (b) {
        if (!alive) return;
        made = URL.createObjectURL(b);
        setBlobUrl(made);
        setBlobKind(meta.kind);
      }).catch(function (e) {
        if (alive) setError("预览加载失败：" + String(e && e.message || e));
      });
      return function () {
        alive = false;
        if (made) URL.revokeObjectURL(made);
      };
    }, [meta]);

    if (error) {
      return h("div", { className: NS + "-fpreview" }, h("p", { className: NS + "-err" }, error));
    }
    if (!meta) {
      return h("div", { className: NS + "-fpreview" }, h("p", { className: NS + "-fhint" }, "读取文件…"));
    }

    var head = h("div", { className: NS + "-fpreview-head" },
      h("strong", { className: NS + "-fpreview-title" }, meta.name),
      h("span", { className: NS + "-fpmeta" }, fmtSize(meta.bytes) +
        (meta.mtime ? " · " + meta.mtime.slice(0, 16).replace("T", " ") : "")),
      meta.read_only ? h("span", { className: NS + "-tag " + NS + "-tag-archive" }, "归档只读") : null,
      h("span", { className: NS + "-fpspacer" }),
      meta.download_url
        ? h("a", {
          className: NS + "-btn " + NS + "-btn-quiet",
          href: meta.download_url,
          // 用 authedFetch 走 cookie/token 模式不可靠：下载走同一鉴权通道取 blob 再落盘
          onClick: function (ev) {
            ev.preventDefault();
            props.onDownload && props.onDownload(meta);
          },
        }, "下载")
        : null,
      h("button", {
        className: NS + "-btn " + NS + "-btn-quiet",
        onClick: props.onClose,
      }, "关闭"));

    var body = null;
    if (meta.kind === "text") {
      // 纯文本：React 会把字符串当文本节点插入，天然不解析 HTML。
      body = h("div", { className: NS + "-ftextwrap" },
        meta.notice ? h("p", { className: NS + "-fhint" }, meta.notice) : null,
        meta.decode_error ? h("p", { className: NS + "-fhint" }, meta.decode_error) : null,
        h("pre", {
          className: NS + "-ftext",
          "data-lang": meta.lang || "text",
          "data-encoding": meta.encoding || "utf-8",
        }, meta.text));
    } else if (meta.kind === "image") {
      body = h("div", { className: NS + "-fimgwrap" },
        blobUrl
          ? h("img", {
            className: NS + "-fimg",
            src: blobUrl,
            alt: meta.name,
            // 图片内部任何脚本都不会执行；SVG 同样如此（后端 raw 另带 sandbox CSP）
            onError: function () { setError("图片无法解码显示（可能不是真正的图片）"); },
          })
          : h("p", { className: NS + "-fhint" }, "加载图片…"),
        h("p", { className: NS + "-fhint" }, meta.notice));
    } else if (meta.kind === "pdf") {
      body = h("div", { className: NS + "-fpdfwrap" },
        h("div", { className: NS + "-fhint" }, meta.notice),
        blobUrl
          ? h("iframe", {
            className: NS + "-fpdf",
            src: blobUrl + "#toolbar=1&view=FitH",
            title: meta.name + "（PDF 预览）",
            // 沙箱：即使 PDF 内容被塞了脚本也拿不到本站上下文
            sandbox: "",
          })
          : h("p", { className: NS + "-fhint" }, "浏览器正在渲染 PDF…"));
    } else {
      body = h("div", { className: NS + "-fdl" },
        h("p", { className: NS + "-fhint" },
          meta.reason || "该格式不做网页内预览。"),
        h("button", {
          className: NS + "-btn " + NS + "-btn-primary",
          onClick: function () { props.onDownload && props.onDownload(meta); },
        }, "下载文件"));
    }

    return h("div", { className: NS + "-fpreview", ref: holder },
      head,
      body,
      h("p", { className: NS + "-fpath" }, meta.path));
  }

  /** 下载：走同一鉴权通道取 blob，再用 <a download> 落盘（避免把 token 放 URL）。 */
  function downloadFile(projectId, meta) {
    var url = apiUrl("/projects/" + encodeURIComponent(projectId) + "/raw",
      { path: meta.path, download: 1 });
    return (SDK.authedFetch || fetch)(url).then(function (r) {
      if (!r || !r.ok) throw new Error("下载失败 HTTP " + (r && r.status));
      return r.blob();
    }).then(function (b) {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(b);
      a.download = meta.name || "download";
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      setTimeout(function () {
        URL.revokeObjectURL(a.href);
        a.remove();
      }, 4000);
    });
  }

  // ---------------------------------------------------------------- 新建 / 纳管项目
  // 两段式：先 /projects/plan 拿「预期目录 + 模板 + 将写入的文件」（不落盘），
  // 用户点确认才 POST /projects/create 带 plan_id，并在响应里做读回校验。
  function ProjectCreatePanel(props) {
    var [mode, setMode] = useState(props.mode || "new");
    var [name, setName] = useState("");
    var [template, setTemplate] = useState("todo");
    var [description, setDescription] = useState("");
    var [startDate, setStartDate] = useState("");
    var [endDate, setEndDate] = useState("");
    var [plan, setPlan] = useState(null);
    var [planned, setPlanned] = useState(null);     // 与 plan 同时记住「发出计划时的入参」
    var [busy, setBusy] = useState(false);
    var [error, setError] = useState(null);
    var [result, setResult] = useState(null);

    function body(nextMode) {
      return {
        mode: nextMode || mode,
        name: name.trim(),
        template: template,
        description: description,
        start_date: startDate || null,
        end_date: endDate || null,
      };
    }

    var preview = useCallback(function (nextMode) {
      var b = body(nextMode);
      if (!b.name) { setError("先填项目名（YYYY-MM_名称）"); return; }
      setBusy(true); setError(null); setResult(null);
      fetchJSON(BASE + "/projects/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(b),
      }).then(function (d) {
        setBusy(false);
        setPlan(d);
        setPlanned(b);
        if (d && d.mode) setMode(d.mode);
      }).catch(function (e) { setBusy(false); setPlan(null); setError(String(e && e.message || e)); });
    }, [mode, name, template, description, startDate, endDate]);

    var create = useCallback(function () {
      if (!plan || !planned) return;
      setBusy(true); setError(null);
      fetchJSON(apiUrl("/projects/create", { plan_id: plan.plan_id }), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(planned),
      }).then(function (d) {
        setBusy(false);
        setResult(d);
        setPlan(null);
        if (d && d.verified && props.onCreated) props.onCreated(d);
      }).catch(function (e) { setBusy(false); setError(String(e && e.message || e)); });
    }, [plan, planned]);

    var untracked = props.untracked || [];
    var hint = mode === "new"
      ? "在活动根新建一个规范项目目录（目录名 = .pm 文件名）。同名目录存在会被拒绝。"
      : "从下面「尚无 .pm」的已有目录中选一个补成规范项目；已有 .pm 的目录不会被覆盖。";

    return h("div", { className: NS + "-panel " + NS + "-create" },
      h("div", { className: NS + "-panel-head" },
        h("h3", { className: NS + "-panel-title" }, "新建 / 纳管项目"),
        h("span", { className: NS + "-badge-warn" }, "需确认"),
        h("span", { className: NS + "-fpspacer" }),
        h("button", {
          className: NS + "-btn " + NS + "-btn-quiet",
          onClick: props.onClose,
        }, "收起")),

      h("div", { className: NS + "-seg" },
        h("button", {
          className: NS + "-segbtn" + (mode === "new" ? " " + NS + "-segbtn-on" : ""),
          onClick: function () { setMode("new"); setPlan(null); },
        }, "新建规范项目"),
        h("button", {
          className: NS + "-segbtn" + (mode === "adopt" ? " " + NS + "-segbtn-on" : ""),
          onClick: function () { setMode("adopt"); setPlan(null); },
        }, "纳管已有目录")),

      h("p", { className: NS + "-fhint" }, hint +
        (props.activeRoot ? " 活动根：" + props.activeRoot : "")),

      h("div", { className: NS + "-grid" },
        h("label", { className: NS + "-field-label" },
          "项目名（YYYY-MM_名称）",
          h("input", {
            className: NS + "-input",
            value: name,
            placeholder: "2026-11_示例项目",
            onChange: function (e) { setName(e.target.value); setPlan(null); },
          })),
        mode === "adopt"
          ? h("label", { className: NS + "-field-label" },
            "已有目录（尚无 .pm）",
            h("select", {
              className: NS + "-select",
              value: untracked.some(function (u) { return u.name === name; }) ? name : "",
              onChange: function (e) { setName(e.target.value); setPlan(null); },
            },
              h("option", { value: "" }, untracked.length ? "— 请选择 —" : "— 没有可纳管目录 —"),
              untracked.map(function (u) {
                return h("option", { key: u.name, value: u.name },
                  u.name + (u.entries != null ? "（" + u.entries + " 项）" : "") +
                    (u.name_valid ? "" : " · 名字不合规范"));
              })))
          : h("label", { className: NS + "-field-label" },
            "模板",
            h("select", {
              className: NS + "-select",
              value: template,
              onChange: function (e) { setTemplate(e.target.value); setPlan(null); },
            }, (function () {
              // creatable 还没返回时 props.templates 是 null：用内置默认顶住，
              // 绝不在这里解引用 null（会让整个面板崩掉、按钮点了没反应）。
              var tpls = props.templates || { todo: [], default: [], hardware: [],
                electron: [], web: [] };
              return Object.keys(tpls).map(function (k) {
                var dirs = tpls[k] || [];
                return h("option", { key: k, value: k },
                  k + (dirs.length ? "（" + dirs.join("/") + "）" : "（纯清单，无子目录）"));
              });
            })())),
        h("label", { className: NS + "-field-label" }, "开始日期",
          h("input", {
            className: NS + "-input", type: "date", value: startDate,
            onChange: function (e) { setStartDate(e.target.value); setPlan(null); },
          })),
        h("label", { className: NS + "-field-label" }, "结束日期",
          h("input", {
            className: NS + "-input", type: "date", value: endDate,
            onChange: function (e) { setEndDate(e.target.value); setPlan(null); },
          }))),

      h("label", { className: NS + "-field-label" }, "说明",
        h("textarea", {
          className: NS + "-textarea", rows: 2, value: description,
          placeholder: "目标、硬性要求、提交方式…",
          onChange: function (e) { setDescription(e.target.value); setPlan(null); },
        })),

      h("div", { className: NS + "-toolbar" },
        h("button", {
          className: NS + "-btn", disabled: busy,
          onClick: function () { preview(); },
        }, busy && !plan ? "校验中…" : "预览将创建的内容"),
        plan && plan.ok && !result
          ? h("button", {
            className: NS + "-btn " + NS + "-btn-primary", disabled: busy,
            onClick: create,
          }, busy ? "写入中…" : "确认创建")
          : null),

      error ? h("p", { className: NS + "-err" }, error) : null,

      // —— 计划（不落盘）：目录、模板、将写入的文件、阻塞原因
      plan
        ? h("div", { className: NS + "-plan" },
          h("p", { className: NS + "-plantitle" },
            plan.ok ? "计划（尚未写盘）" : "无法执行：" + plan.blockers.join("；")),
          h("dl", { className: NS + "-kv" },
            h("dt", null, "目标目录"), h("dd", null, plan.target_dir),
            h("dt", null, ".pm 路径"), h("dd", null, plan.pm_path),
            h("dt", null, "模板"), h("dd", null, plan.template +
              (plan.template_dirs && plan.template_dirs.length
                ? "（子目录 " + plan.template_dirs.join("、") + "）" : "（无子目录）")),
            h("dt", null, "任务/里程碑"), h("dd", null,
              (plan.pm_preview.tasks || 0) + " 任务 / " + (plan.pm_preview.milestones || 0) + " 里程碑"),
            plan.already_tracked ? h("dt", null, "已纳管") : null,
            plan.already_tracked ? h("dd", null, plan.existing_pm) : null),
          plan.actions && plan.actions.length
            ? h("ul", { className: NS + "-planlist" },
              plan.actions.map(function (a, i) { return h("li", { key: i }, a); }))
            : null,
          h("p", { className: NS + "-fhint" }, plan.note))
        : null,

      // —— 落盘结果 + 读回校验
      result
        ? h("div", { className: NS + "-plan " + (result.verified ? NS + "-plan-ok" : NS + "-plan-warn") },
          h("p", { className: NS + "-plantitle" },
            result.verified ? "已创建并读回校验通过" : "已写入，但读回校验未全部通过"),
          h("dl", { className: NS + "-kv" },
            h("dt", null, "目录"), h("dd", null, result.target_dir),
            h("dt", null, ".pm"), h("dd", null, result.pm_file),
            h("dt", null, "可解析"), h("dd", null, result.verify.pm_parses ? "是" : "否"),
            h("dt", null, "出现在项目列表"), h("dd", null,
              result.verify.visible_in_projects ? "是" : "否（可在 PM 里导入该 .pm）")),
          h("p", { className: NS + "-fhint" }, result.note))
        : null);
  }

  // ---------------------------------------------------------------- 论文列表
  function PaperRow(props) {
    var p = props.paper;
    return h("a", {
      className: NS + "-paper",
      href: p.zotero_uri,
      title: "在 Zotero 中打开：" + p.title,
      // zotero:// 由系统 handler 接住；浏览器无 handler 时也不会跳走当前页
      onClick: function () { props.onOpen && props.onOpen(p); },
    },
      h("span", { className: NS + "-paper-title" }, p.title),
      h("span", { className: NS + "-paper-meta" },
        h("span", { className: NS + "-paper-authors" }, p.author_line || "作者未知"),
        p.year ? h("span", { className: NS + "-paper-year" }, p.year) : null,
        p.publication ? h("span", { className: NS + "-paper-venue" }, p.publication) : null));
  }

  function ZoteroPanel(props) {
    var project = props.project;
    var link = props.link;
    var items = props.items;
    var status = props.linkStatus;
    var [filter, setFilter] = useState("");
    var [openAll, setOpenAll] = useState(false);

    if (status === "loading") {
      return h("div", { className: NS + "-panel" },
        h("div", { className: NS + "-hint" }, "正在读取 Zotero 本地 API…"));
    }
    if (status === "error") {
      return h("div", { className: NS + "-panel" },
        h(Empty, {
          title: "Zotero 本地 API 不可达",
          hint: "请确认 Zotero 已启动，并在 设置 → 高级 勾选「允许其他应用程序与本机 Zotero 通信」。" +
            "论文区只读元数据，不做 PDF 阅读或批注回写。",
        }));
    }
    if (!link) return h("div", { className: NS + "-panel" });

    if (!link.matched) {
      return h("div", { className: NS + "-panel" },
        h("div", { className: NS + "-panel-head" },
          h("h3", { className: NS + "-panel-title" }, "关联 Zotero 论文"),
          h("span", { className: NS + "-badge-warn" }, "未匹配")),
        h(Empty, {
          title: "Zotero 里没有与「" + project.name + "」完全同名的集合",
          hint: "不会自动猜测。请从下面选择本项目对应集合；保存后写进插件本地映射（plugin-data/wb-project）。",
        }),
        h("div", { className: NS + "-pickers" },
          h("label", { className: NS + "-field-label", htmlFor: NS + "-colsel" }, "选择 Zotero 集合"),
          h("select", {
            id: NS + "-colsel",
            className: NS + "-select",
            value: props.chooseKey || "",
            onChange: function (e) { props.onChoose && props.onChoose(e.target.value); },
          },
            h("option", { value: "" }, "— 请选择 —"),
            (link.candidates || []).map(function (c) {
              return h("option", { key: c.key, value: c.key },
                c.path + (typeof c.num_items === "number" ? "（" + c.num_items + " 条）" : ""));
            })),
          h("button", {
            className: NS + "-btn " + NS + "-btn-primary",
            disabled: !props.chooseKey || props.saving,
            onClick: props.onSaveMapping,
          }, props.saving ? "保存中…" : "保存关联"),
          props.saveError ? h("p", { className: NS + "-err" }, props.saveError) : null));
    }

    var col = link.collection;
    var list = (items && items.items) || [];
    var q = filter.trim().toLowerCase();
    var shown = q ? list.filter(function (p) {
      return (p.title + " " + p.author_line + " " + (p.year || "")).toLowerCase().indexOf(q) >= 0;
    }) : list;
    var shownFinal = openAll ? shown : shown.slice(0, 8);

    return h("div", { className: NS + "-panel" },
      h("div", { className: NS + "-panel-head" },
        h("h3", { className: NS + "-panel-title" }, "关联 Zotero 论文"),
        h("span", { className: NS + "-badge-ok" },
          link.source === "mapping" ? "已映射" : "同名匹配"),
        h("span", { className: NS + "-panel-col" }, col.path || col.name),
        h("button", {
          className: NS + "-btn " + NS + "-btn-quiet",
          title: "解除本项目与 Zotero 集合的关联",
          onClick: props.onClearMapping,
        }, "解除关联")),

      items && items.error ? h("p", { className: NS + "-err" }, items.error) : null,

      h("div", { className: NS + "-toolbar" },
        h("input", {
          className: NS + "-input", type: "search", placeholder: "按标题 / 作者 / 年份筛选",
          value: filter, onChange: function (e) { setFilter(e.target.value); },
        }),
        h("span", { className: NS + "-count" },
          q ? shown.length + " / " + list.length + " 篇" : list.length + " 篇")),

      !list.length
        ? h(Empty, {
          title: "该集合下没有文献条目",
          hint: items && items.total_results === 0
            ? "Zotero 里这个集合确实是空的。" : "可以试试 Zotero 里重新同步。",
        })
        : h("div", { className: NS + "-papers" },
          shownFinal.map(function (p) { return h(PaperRow, { key: p.key, paper: p }); })),
      shown.length > 8
        ? h("button", {
          className: NS + "-btn " + NS + "-btn-quiet",
          onClick: function () { setOpenAll(!openAll); },
        }, openAll ? "收起" : "展开全部 " + shown.length + " 篇")
        : null);
  }

  function ProjectFilesPanel(props) {
    var current = props.project;
    return h("div", { className: NS + "-files" },
      h("div", { className: NS + "-files-head" },
        h("h3", { className: NS + "-panel-title" }, "项目文件夹"),
        h("span", { className: NS + "-badge-ok" }, current.archived ? "归档只读" : "活动"),
        h("span", { className: NS + "-files-dir" }, current.dir_path || "（后端未提供目录）"),
        props.downloadError ? h("span", { className: NS + "-err" }, props.downloadError) : null),
      current.dir_path ? h("div", { className: NS + "-files-body" },
        h("div", { className: NS + "-filetree" }, h(DirNode, {
          key: current.id, projectId: current.id, path: "", name: current.dir_name || current.name,
          depth: 0, activeFile: props.activeFile && props.activeFile.path, onOpen: props.onOpen,
        })),
        h("div", { className: NS + "-filepane" }, props.activeFile
          ? h(FilePreview, { key: props.activeFile.path, projectId: current.id, entry: props.activeFile,
            onClose: function () { props.onOpen(null); }, onDownload: props.onDownload })
          : h("div", { className: NS + "-fhint " + NS + "-filepane-empty" },
            "左侧展开目录并点一个文件：文本/markdown/json 按纯文本显示，图片内联预览，PDF 交给浏览器阅读器；Office/压缩包等只提供下载。")))
        : h("p", { className: NS + "-err" }, "后端未提供项目目录（需重启一次 dashboard 以加载新版 plugin_api.py）"));
  }

  // ---------------------------------------------------------------- 页面
  /** 窄屏判定：抽屉模式必须自己管，CSS 媒体查询拿不到「是否该渲染成抽屉」这个结构决策。 */
  function useNarrow(breakpoint) {
    var [narrow, setNarrow] = useState(function () {
      return typeof window !== "undefined" && window.innerWidth <= breakpoint;
    });
    useEffect(function () {
      function onResize() { setNarrow(window.innerWidth <= breakpoint); }
      window.addEventListener("resize", onResize);
      return function () { window.removeEventListener("resize", onResize); };
    }, []);
    return narrow;
  }

  function ProjectPage() {
    var [projects, setProjects] = useState(null);
    var [loadError, setLoadError] = useState(null);
    var [selectedId, setSelectedId] = useState(null);
    // Everyday project tracking is active-only; archived roots never enter this view.

    var [link, setLink] = useState(null);
    var [linkStatus, setLinkStatus] = useState("idle");
    var [items, setItems] = useState(null);
    var [chooseKey, setChooseKey] = useState("");
    var [saving, setSaving] = useState(false);
    var [saveError, setSaveError] = useState(null);

    var [focusTask, setFocusTask] = useState(null);

    // —— 项目文件夹面板（懒加载）+ 新建/纳管面板
    // 这里只存「当前展开到哪、预览选中哪个文件」这类 UI 状态；目录内容由 DirNode 自己按需请求。
    var [filesOpen, setFilesOpen] = useState(false);
    var [ganttOpen, setGanttOpen] = useState(true);
    var [activeFile, setActiveFile] = useState(null);   // 选中的 entry 对象
    var [createOpen, setCreateOpen] = useState(false);
    var [createMode, setCreateMode] = useState("new");
    var [creatable, setCreatable] = useState(null);     // /projects/creatable 的只读快照
    var [downloadError, setDownloadError] = useState(null);

    var refreshCreatable = useCallback(function () {
      return fetchJSON(BASE + "/projects/creatable")
        .then(function (d) { setCreatable(d); return d; })
        .catch(function () { setCreatable(null); return null; });
    }, []);

    // —— 对话面板：右侧可折叠栏（宽屏）/ 抽屉 + 堆叠（窄屏）
    var [chatComp, setChatComp] = useState(null);      // 共享组件的 React 面板
    var [chatError, setChatError] = useState(null);
    var [chatOpen, setChatOpen] = useState(true);      // 宽屏：栏是否展开
    var [chatDrawer, setChatDrawer] = useState(false); // 窄屏：抽屉是否打开
    var [reloadAt, setReloadAt] = useState(0);         // 刷新计数器：对话完成/gantt 兜底按钮共用
    var narrow = useNarrow(1180);
    var [chatBusy, setChatBusy] = useState(false);     // 只用于折叠把手上的忙碌小点

    // 选中项目要跨刷新保留：localStorage 里只存 id，加载后校验它还在列表里
    var SELECT_KEY = "wbp.selected.v1";

    useEffect(function () {
      loadSharedChat().then(function (api) {
        // React 会把函数型入参当成 updater 调用，所以这里用 updater 形式「返回」组件本身，
        // 否则 state 里存的是被调用过的 null（TypeError: reading 'moduleId' of null）。
        var panel = api.makePanel(SDK);
        setChatComp(function () { return panel; });
      }).catch(function (e) {
        setChatError(String(e && e.message || e));
      });
    }, []);

    function readSelected() {
      try { return window.localStorage.getItem(SELECT_KEY) || null; } catch (e) { return null; }
    }

    function writeSelected(id) {
      try {
        if (id) window.localStorage.setItem(SELECT_KEY, id);
        else window.localStorage.removeItem(SELECT_KEY);
      } catch (e) { /* 隐私模式：只是不保留选择 */ }
    }

    /** 重新拉项目数据，但保留当前选择（loading 期间不清空当前对象）。 */
    var reload = useCallback(function () {
      return fetchJSON(BASE + "/projects?include_archived=false")
        .then(function (d) {
          if (!d || !Array.isArray(d.projects)) {
            setLoadError("后端返回的是旧版骨架（缺少 /projects 的 projects 字段）。" +
              "插件后端按进程缓存，需要重启一次 dashboard 才能加载新版 plugin_api.py。");
            return null;
          }
          setLoadError(null);
          setProjects(d);
          return d;
        })
        .catch(function (e) { setLoadError(String(e && e.message || e)); return null; });
    }, []);

    useEffect(function () {
      fetchJSON(BASE + "/projects?include_archived=false")
        .then(function (d) {
          // 旧骨架版后端只返回 [{name,path,tasks:9}]（数组、无日期字段）。
          // 那种形状喂给甘特图会静默渲染成空图，所以明确报「后端未重启」而不是装作有数据。
          if (!d || !Array.isArray(d.projects)) {
            setLoadError("后端返回的是旧版骨架（缺少 /projects 的 projects 字段）。" +
              "插件后端按进程缓存，需要重启一次 dashboard 才能加载新版 plugin_api.py。");
            return;
          }
          setProjects(d);
          if (!d.projects.length) return;
          var remembered = readSelected();
          var hit = remembered && d.projects.some(function (p) { return p.id === remembered; });
          setSelectedId(hit ? remembered : d.projects[0].id);
        })
        .catch(function (e) { setLoadError(String(e && e.message || e)); });
    }, []);

    // 刷新兜底：对话说「改完了」或用户按刷新时，重新读 .pm（保留选择，见 reload）
    useEffect(function () {
      if (!reloadAt) return;
      reload();
    }, [reloadAt]);

    function selectProject(id) {
      setSelectedId(id);
      writeSelected(id);
    }

    var list = useMemo(function () {
      if (!projects || !projects.projects) return [];
      // Refresh callbacks can return broader caches; physically archived projects stay excluded.
      return projects.projects.filter(function (p) { return !p.archived; });
    }, [projects]);

    var current = useMemo(function () {
      if (!list.length) return null;
      for (var i = 0; i < list.length; i++) if (list[i].id === selectedId) return list[i];
      return list[0];
    }, [list, selectedId]);

    // 选中项目变化 → 解析 Zotero 关联 → 取条目
    useEffect(function () {
      if (!current) return;
      setLink(null); setItems(null); setLinkStatus("loading");
      setChooseKey(""); setSaveError(null);
      fetchJSON(BASE + "/zotero/link?project=" + encodeURIComponent(current.id))
        .then(function (d) {
          setLink(d);
          setLinkStatus("ready");
          if (d && d.matched && d.collection) {
            return fetchJSON(BASE + "/zotero/items?collection=" + encodeURIComponent(d.collection.key))
              .then(function (res) { setItems({ items: (res && res.items) || [], total_results: res && res.total_results }); })
              .catch(function (e) { setItems({ items: [], error: String(e && e.message || e) }); });
          }
        })
        .catch(function (e) {
          setLinkStatus("error");
          setSaveError(String(e && e.message || e));
        });
    }, [current]);

    var saveMapping = useCallback(function () {
      if (!current || !chooseKey) return;
      setSaving(true); setSaveError(null);
      fetchJSON(BASE + "/zotero/mappings?project=" + encodeURIComponent(current.id) +
        "&collection=" + encodeURIComponent(chooseKey), { method: "PUT" })
        .then(function () {
          setSaving(false);
          return fetchJSON(BASE + "/zotero/link?project=" + encodeURIComponent(current.id))
            .then(function (d) {
              setLink(d);
              if (d && d.matched && d.collection) {
                return fetchJSON(BASE + "/zotero/items?collection=" + encodeURIComponent(d.collection.key))
                  .then(function (res) { setItems({ items: (res && res.items) || [], total_results: res && res.total_results }); });
              }
            });
        })
        .catch(function (e) { setSaving(false); setSaveError(String(e && e.message || e)); });
    }, [current, chooseKey]);

    var clearMapping = useCallback(function () {
      if (!current) return;
      setSaving(true); setSaveError(null);
      fetchJSON(BASE + "/zotero/mappings?project=" + encodeURIComponent(current.id), { method: "PUT" })
        .then(function () {
          setSaving(false);
          return fetchJSON(BASE + "/zotero/link?project=" + encodeURIComponent(current.id))
            .then(function (d) { setLink(d); setItems(null); });
        })
        .catch(function (e) { setSaving(false); setSaveError(String(e && e.message || e)); });
    }, [current]);

    // 切项目时清掉上一个项目的文件选择与预览：避免把 A 项目的文件路径带到 B 项目请求里
    useEffect(function () {
      setActiveFile(null);
      setDownloadError(null);
    }, [current && current.id]);

    if (loadError) {
      return h("div", { className: NS + "-root" },
        h(Empty, { title: "读取项目失败", hint: loadError + "（后端 /api/plugins/wb-project/projects）" }));
    }
    if (!projects) {
      return h("div", { className: NS + "-root" }, h("div", { className: NS + "-hint" }, "加载项目…"));
    }
    if (!list.length) {
      var scanned = (projects.roots || []).filter(function (r) { return r.location === "active"; })
        .map(function (r) { return r.path; });
      var emptyHint = scanned.length ? "扫描路径：" + scanned.join("、") : "";
      return h("div", { className: NS + "-root" },
        h("aside", { className: NS + "-side" },
          h("div", { className: NS + "-side-head" },
            h("h2", { className: NS + "-side-title" }, "项目"),
            h("span", { className: NS + "-side-count" }, "0")),
          h("button", { className: NS + "-btn " + NS + "-btn-primary " + NS + "-sidebar-create",
            onClick: function () { setCreateOpen(true); setCreateMode("new"); refreshCreatable(); }
          }, "新建/纳管")),
        h(Empty, { title: "活动根下没有可显示的 .pm 项目",
          hint: emptyHint || null,
        }),
        h("div", { className: NS + "-empty-actions" },
          h("button", {
            className: NS + "-btn " + NS + "-btn-primary",
            onClick: function () {
              setCreateOpen(true);
              setCreateMode("new");
              refreshCreatable();
            },
          }, "新建 / 纳管项目"),
          createOpen
            ? h(ProjectCreatePanel, {
              key: "create",
              mode: createMode,
              templates: (creatable && creatable.templates) || null,
              untracked: (creatable && creatable.untracked) || [],
              activeRoot: creatable && creatable.active_root,
              onClose: function () { setCreateOpen(false); },
              onCreated: function () {
                setReloadAt(Date.now());
                refreshCreatable();
              },
            })
            : null));
    }

    var c = current.counts;

    // —— 对话面板（宽屏右侧栏 / 窄屏抽屉）。上下文是**数据**：模块、对象、
    // 真实项目目录与服务端已验证的 .pm 路径；getContext 每次新建会话时现取，
    // 所以切项目后新会话拿到的是新对象的上下文。
    function chatPanel(extraClass) {
      if (!chatComp || !current) return null;
      return h(chatComp, {
        key: "chat",
        moduleId: "wb-project",
        moduleName: "项目",
        objectId: current.id,
        objectName: current.name,
        className: extraClass,
        getContext: function () {
          var facts = [
            "任务总数 " + c.total + "：待办 " + c.todo + "、进行中 " + c.doing + "、已完成 " + c.done +
              (c.overdue ? "、已逾期 " + c.overdue : "") +
              (c.unscheduled ? "、无截止日 " + c.unscheduled : ""),
            "项目起止：" + (current.start || "未知") + " → " + (current.end || "未知"),
            "项目位置：" + (current.dir_path || "未知") +
              (current.archived
                ? "（归档，根 " + (current.root_label || current.root) + "）"
                : "（活动根 " + (current.root_label || current.root) + "）"),
            "任务清单（标题 / 状态 / 截止日）：" + (current.tasks || []).slice(0, 20).map(function (t) {
              return t.title + "(" + (t.status || "无状态") + (t.due ? "/" + t.due : "/无截止日") + ")";
            }).join("；"),
          ];
          if (current.archive_anomaly) {
            facts.push("归档状态异常：" + current.archive_anomaly);
          }
          if (current.tasks && current.tasks.length > 20) {
            facts.push("（上面只列前 20 条，完整清单请用 read_file 读 .pm）");
          }
          var files = current.dir_path
            ? [{ path: current.dir_path + "/" + current.pm_file, role: "本项目 .pm（任务/里程碑数据源）" }]
            : [];
          if (!current.dir_path) {
            // 旧后端（未重启的进程）没有 dir_path：如实说明，不拼一个 "undefined/..." 的假路径
            facts.push("（后端未提供项目目录：plugin_api.py 改了字段，需重启一次 dashboard 才能拿到 dir_path）");
          }
          if (link && link.matched && link.collection) {
            facts.push("关联 Zotero 集合：" + (link.collection.path || link.collection.name));
          }
          return {
            module: "项目区（wb-project）：甘特图 + 关联 Zotero 论文",
            object: current.id + (current.dir_name ? "（目录 " + current.dir_name + "）" : ""),
            cwd: current.dir_path || undefined,
            facts: facts,
            files: files,
            notes: [
              "写 .pm 前先备份；不要改 schema 之外的字段。",
              "Zotero 侧只读元数据：不要写用户库、不要回写批注。",
            ],
          };
        },
        onTurnComplete: function () { setReloadAt(Date.now()); },
        onBusy: setChatBusy,
      });
    }

    var chatColumn = (!narrow && chatOpen && chatComp) ? chatPanel(NS + "-chatcol") : null;

    return h("div", { className: NS + "-root" + (chatColumn ? " " + NS + "-root-chat" : "") },
      // —— 左栏：项目列表
      h("aside", { className: NS + "-side" },
        h("div", { className: NS + "-side-head" },
          h("h2", { className: NS + "-side-title" }, "项目"),
          h("span", { className: NS + "-side-count" }, list.length)),
        h("ul", { className: NS + "-projlist" },
          list.map(function (p) {
            var active = current && p.id === current.id;
            var total = p.counts.total || 1;
            return h("li", {
              key: p.id,
              className: NS + "-projitem" + (active ? " " + NS + "-projitem-active" : ""),
              onClick: function () { selectProject(p.id); },
              title: (p.dir_path || p.dir_name) + (p.archived ? "（归档：" + (p.root_label || p.root) + "）" : ""),
            },
              h("span", {
                className: NS + "-projdot",
                style: { background: p.color || "currentColor" },
              }),
              h("span", { className: NS + "-projname" }, p.name,
                p.archive_anomaly
                  ? h("span", {
                    className: NS + "-tag " + NS + "-tag-warn",
                    title: p.archive_anomaly,
                  }, "未迁移")
                  : null),
              h("span", { className: NS + "-projprog" },
                h("i", {
                  className: NS + "-projprogbar",
                  style: { width: Math.round((p.counts.done / total) * 100) + "%" },
                })),
              h("span", { className: NS + "-projnum" },
                p.counts.done + "/" + p.counts.total));
          })),
        !current.archived ? h("button", { className: NS + "-btn " + NS + "-btn-primary " + NS + "-sidebar-create",
          onClick: function () { var next = !createOpen; setCreateOpen(next); setCreateMode("new"); if (next) refreshCreatable(); }
        }, createOpen ? "收起新建" : "新建/纳管") : null),

      // —— 右栏：甘特 + 论文
      h("section", { className: NS + "-main" },
        h("header", { className: NS + "-head" },
          h("select", { className: NS + "-mobile-project-picker", value: current.id,
            "aria-label": "选择项目", onChange: function (e) { selectProject(e.target.value); } },
            list.map(function (p) { return h("option", { key: p.id, value: p.id }, p.name); })),
          h("div", { className: NS + "-head-main" },
            h("h1", { className: NS + "-title" }, current.name),
            h("p", { className: NS + "-subtitle" },
              (current.start || "起始未知") + " → " + (current.end || "结束未知"),
              current.updated_at ? " · 更新于 " + current.updated_at.slice(0, 10) : ""),
            current.archive_anomaly
              ? h("p", { className: NS + "-warnline", role: "status" }, current.archive_anomaly)
              : null),
          h("div", { className: NS + "-stats" },
            h(Stat, { value: c.total, label: "任务" }),
            h(Stat, { value: c.todo, label: "待办", color: STATUS_COLORS.todo }),
            h(Stat, { value: c.doing, label: "进行中", color: STATUS_COLORS.doing }),
            h(Stat, { value: c.done, label: "已完成", color: STATUS_COLORS.done }),
            c.overdue ? h(Stat, { value: c.overdue, label: "已逾期", color: "#d33f3f" }) : null),
          h("div", { className: NS + "-chattools" },
            // 刷新兜底：对话改了数据但没触发 onTurnComplete（比如你中途刷新），按钮仍在
            h("button", {
              className: NS + "-btn " + NS + "-btn-quiet",
              title: "重新读取 .pm（保留当前选择）",
              onClick: function () { setReloadAt(Date.now()); },
            }, "刷新数据"),
            // 项目文件夹：按目录懒加载，点文件才预览
            h("button", {
              className: NS + "-btn " + NS + "-btn-quiet" +
                (filesOpen ? " " + NS + "-btn-on" : ""),
              title: "浏览本项目真实文件夹（按目录懒加载，敏感文件不会列出）",
              onClick: function () { setFilesOpen(!filesOpen); },
            }, filesOpen ? "收起文件" : "文件"),
            // 新建/纳管：只在活动项目上给入口；归档项目只读，不给写入口
            !current.archived ? h("button", {
              className: NS + "-btn " + NS + "-btn-quiet" +
                (createOpen ? " " + NS + "-btn-on" : ""),
              title: "新建规范项目目录，或把已有无 .pm 目录纳管为项目",
              onClick: function () {
                var next = !createOpen;
                setCreateOpen(next);
                setCreateMode("new");
                if (next) refreshCreatable();
              },
            }, createOpen ? "收起新建" : "新建/纳管") : null,
            !narrow ? h("button", {
              className: NS + "-btn " + NS + "-btn-quiet",
              title: chatOpen ? "收起 agent 对话面板" : "展开 agent 对话面板",
              onClick: function () { setChatOpen(!chatOpen); },
            }, chatOpen ? "收起对话" : "对话" + (chatBusy ? " ●" : "")) : h("button", {
              className: NS + "-btn " + NS + "-btn-quiet",
              onClick: function () { setChatDrawer(true); },
            }, "对话" + (chatBusy ? " ●" : "")))),

        chatError ? h("p", { className: NS + "-err" }, "对话组件不可用：" + chatError) : null,

        h("section", { className: NS + "-content-section " + NS + "-gantt-section" },
          h("div", { className: NS + "-section-head" },
            h("h2", { className: NS + "-section-title" }, "甘特图"),
            h("button", { className: NS + "-btn " + NS + "-btn-quiet", "aria-expanded": ganttOpen,
              onClick: function () { setGanttOpen(!ganttOpen); }
            }, ganttOpen ? "收起甘特图" : "展开甘特图")),
          ganttOpen ? h(React.Fragment, null,
            h("div", { className: NS + "-recent-todos" },
              h("h3", { className: NS + "-panel-title" }, "近期未完成待办"),
              (current.tasks || []).filter(function (t) { return t.status_group !== "done"; })
                .slice().sort(function (a, b) {
                  var ad = a.due || "9999-99-99", bd = b.due || "9999-99-99";
                  return ad < bd ? -1 : ad > bd ? 1 : String(a.title || "").localeCompare(String(b.title || ""), "zh-CN");
                }).slice(0, 8).map(function (t) {
                  return h("button", { key: t.id || t.title, className: NS + "-todo-row", onClick: function () { setFocusTask(t); } },
                    h(StatusPill, { group: t.status_group, status: t.status }),
                    h("span", { className: NS + "-todo-title" }, t.title),
              h("time", { className: NS + "-todo-due" }, t.due || "无截止日"));
                })),
            h(Gantt, { tasks: current.tasks, milestones: current.milestones, onPick: function (t) { setFocusTask(t); } })) : null),

        focusTask ? h("div", { className: NS + "-focus" },
          h("div", { className: NS + "-focus-head" },
            h(StatusPill, { group: focusTask.status_group, status: focusTask.status }),
            h("strong", null, focusTask.title),
            focusTask.priority ? h("span", { className: NS + "-prio" }, "优先级 " + focusTask.priority) : null,
            h("button", {
              className: NS + "-btn " + NS + "-btn-quiet",
              onClick: function () { setFocusTask(null); },
            }, "关闭")),
          h("dl", { className: NS + "-kv" },
            h("dt", null, "起始"), h("dd", null,
              focusTask.start
                ? focusTask.start + "（来源：" + ({
                  tracked: "实际开始", created: "任务创建", project: "项目起始日推算", unknown: "未知",
                }[focusTask.start_source] || focusTask.start_source) + "）"
                : "未知"),
            h("dt", null, "截止"), h("dd", null, focusTask.due || "未知"),
            focusTask.subtasks && focusTask.subtasks.total
              ? h("dt", null, "子任务") : null,
            focusTask.subtasks && focusTask.subtasks.total
              ? h("dd", null, focusTask.subtasks.done + " / " + focusTask.subtasks.total + " 完成") : null,
            focusTask.tags && focusTask.tags.length ? h("dt", null, "标签") : null,
            focusTask.tags && focusTask.tags.length
              ? h("dd", null, focusTask.tags.join("、")) : null)) : null,

        filesOpen ? h(ProjectFilesPanel, { project: current, activeFile: activeFile, onOpen: setActiveFile,
          downloadError: downloadError, onDownload: function (meta) {
            setDownloadError(null);
            downloadFile(current.id, meta).catch(function (e) { setDownloadError("下载失败：" + String(e && e.message || e)); });
          } }) : null,
        h("section", { className: NS + "-content-section " + NS + "-zotero-section" },
        h("h2", { className: NS + "-section-title" }, "Zotero"),
        h(ZoteroPanel, {
          project: current,
          link: link,
          linkStatus: linkStatus,
          items: items,
          chooseKey: chooseKey,
          saving: saving,
          saveError: saveError,
          onChoose: setChooseKey,
          onSaveMapping: saveMapping,
          onClearMapping: clearMapping,
          onOpen: function (p) {
            // zotero:// 交给系统 handler；同时给个可见反馈，避免用户以为没反应
            try { window.location.href = p.zotero_uri; } catch (e) { /* handler 缺失时忽略 */ }
          },
        }))),

      chatColumn,

      // —— 新建 / 纳管（预览 → 确认 → 读回校验）。Archived 项目不给写入口。
      // 放在 root 网格里整行铺开：塞进 section.wbp-main 会被压成 289px 而且与聊天栏重叠。
      createOpen
        ? h("div", { key: "panels", className: NS + "-modal-layer", role: "dialog",
          "aria-modal": true, "aria-label": "新建/纳管项目" },
          h("div", { className: NS + "-modal-backdrop", onClick: function () { setCreateOpen(false); } }),
          h(ProjectCreatePanel, {
            key: "create",
            mode: createMode,
            templates: (creatable && creatable.templates) || null,
            untracked: (creatable && creatable.untracked) || [],
            activeRoot: creatable && creatable.active_root,
            onClose: function () { setCreateOpen(false); },
            onCreated: function (res) {
              // 落盘后重新读项目列表与候选：不靠「POST 成功」当验收
              setReloadAt(Date.now());
              refreshCreatable();
              if (res && res.mode === "new" && res.verified) {
                setFilesOpen(true);
              }
            },
          }))
        : null,

      // —— 窄屏抽屉：铺在页面上，关闭后回到原来的甘特图，不挤压既有布局
      (narrow && chatDrawer && chatComp)
        ? h("div", { key: "drawer", className: NS + "-drawer-layer" },
          h("div", {
            className: "wbc-drawer-mask",
            onClick: function () { setChatDrawer(false); },
          }),
          h("div", { className: "wbc-drawer" },
            h(chatComp, {
              key: "chat-drawer",
              moduleId: "wb-project",
              moduleName: "项目",
              objectId: current.id,
              objectName: current.name,
              className: "wbc-panel-drawer",
              getContext: function () {
                return {
                  module: "项目区（wb-project）",
                  object: current.id,
                  cwd: current.dir_path,
                  files: current.dir_path
                    ? [{ path: current.dir_path + "/" + current.pm_file, role: "本项目 .pm" }] : [],
                  facts: [
                    "任务 " + c.total + "：待办 " + c.todo + "、进行中 " + c.doing + "、已完成 " + c.done,
                  ],
                  notes: ["写 .pm 前先备份；删除/外发先确认。"],
                };
              },
              onTurnComplete: function () { setReloadAt(Date.now()); },
              onBusy: setChatBusy,
              onCollapse: function () { setChatDrawer(false); },
            })))
        : null);
  }

  window.__HERMES_PLUGINS__.register("wb-project", ProjectPage);
})();
