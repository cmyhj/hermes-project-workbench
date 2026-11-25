"""wb-project — 项目区后端（甘特图 + 只读 Zotero + 项目文件夹懒加载 + 项目创建/纳管）。

挂载点：``/api/plugins/wb-project/``

两条数据线：
1. 项目与任务：只读扫描**已注册的多个根**下 ``<根>/<项目>/*.pm``（JSON，schema_version 3）。
   两种真实落盘形状都要认：``{"project": {...}, "tasks": [...]}`` 与
   ``{"name": ..., "tasks": [...]}``（顶层扁平）。
2. Zotero 论文：只读 ``GET http://127.0.0.1:23119/api/users/0/...``（Zotero 7 本地 API）。
   只做元数据列表，不做 PDF 下载 / 批注回写。

归档识别（2026-11 改）：
* **归档 = 物理位置**。项目所在的根是归档根 → ``archived=true``；``.pm`` 里的
  ``project.archived`` 只作为 ``pm_archived`` 如实带出。
* Active 根下留着 ``pm_archived=true`` 的项目 → 照常在活动列表里显示，
  并在 ``archive_anomaly`` 里说明「标了归档但没真的搬走」，接口与 UI 都不许静默藏起来。
* 归档位置不是单一硬编码：默认 ``E:/Backups/ProjectArchives``，课程项目登记在
  课程库根（如 ``E:/Libraries/Courses/机器视觉``）。根清单可被
  ``plugin-data/wb-project/project-roots.json`` 覆盖 —— 只有**被登记过的**绝对路径才会扫描。

安全约束（本文件必须保持）：
* 客户端只能传 **项目 id**（扫描结果里的 id / 全局唯一的项目名）与 **8 位 collection key**，
  两者都过白名单/正则；不接受任何文件路径或 URL，避免变成任意文件读取 / SSRF。
  ``/roots`` 只读，新增根必须改服务端配置文件。
* 唯一的写操作是把「项目 → Zotero collection」映射落到插件自己的 ``plugin-data/wb-project/``，
  不写用户库、不写 .pm，不移动/删除任何项目目录。
* 日期一律如实返回：缺失就是 ``None``；甘特图的「起点」若为推算值，用
  ``start_source`` 明示来源（``tracked`` / ``created`` / ``project``），不伪造精确日期。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter()

PROJECTS_ROOT = Path(os.environ.get("WB_PROJECT_ROOT") or r"D:/ActiveProjects")
DEFAULT_PROJECTS_ROOT = Path(r"D:/ActiveProjects")
DEFAULT_ARCHIVE_ROOT = Path(r"E:/Backups/ProjectArchives")
# 课程归档根：课程项目搬进课程库对应的课程目录下（不是 Backup）。
# 只扫这一层的一级子目录，不递归 —— 课程目录里还有实验1..实验N 等资料树。
COURSE_ARCHIVE_ROOT = Path(os.environ.get("WB_PROJECT_COURSE_ROOT") or r"E:/Libraries/Courses/机器视觉")
ROOTS_CONFIG_NAME = "project-roots.json"
MAX_PM_BYTES = 32 * 1024 * 1024     # .pm 规格上限；超了不读，避免拖死请求
ZOTERO_ROOT = "http://127.0.0.1:23119/api/users/0"
ZOTERO_TIMEOUT = 6.0
MAX_ZOTERO_PAGES = 25          # 分页上限，防止病态库把接口拖死
ZOTERO_PAGE_SIZE = 100

_COLLECTION_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DONE_STATUSES = ("已完成", "done", "Done", "完成")
DOING_STATUSES = ("进行中", "in_progress", "In Progress", "doing")

_MAP_LOCK = threading.Lock()
_COL_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}
_COL_TTL = 10.0  # 秒；同一页面会连打几次（link/items/collections），避免重复抓全量


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _today() -> _dt.date:
    return _dt.date.today()


def _as_date(value: Any) -> Optional[str]:
    """把 ``2026-10-20`` / ``2026-10-20T01:02:03Z`` 收敛成 ``YYYY-MM-DD``；认不出就 None。"""
    if not isinstance(value, str):
        return None
    head = value.strip()[:10]
    return head if _DATE_RE.match(head) else None


def _task_start(task: Dict[str, Any], project_start: Optional[str]) -> Tuple[Optional[str], str]:
    """任务的甘特起点 + 来源标记。

    真实字段优先级：``tracked_start``（真的开始做了）> ``created_at``（任务创建时间）
    > 项目 ``start_date``（只能算粗略下界）。三者都缺则返回 ``(None, "unknown")``。
    """
    tracked = _as_date(task.get("tracked_start"))
    if tracked:
        return tracked, "tracked"
    created = _as_date(task.get("created_at"))
    if created:
        return created, "created"
    if project_start:
        return project_start, "project"
    return None, "unknown"


def _status_group(status: Any) -> str:
    s = str(status or "")
    if s in DONE_STATUSES:
        return "done"
    if s in DOING_STATUSES:
        return "doing"
    return "todo"


def _read_pm(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.stat().st_size > MAX_PM_BYTES:
            return None                     # 超规格文件不读，避免单个坏文件拖死整个请求
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _project_meta(data: Dict[str, Any], pm: Path) -> Dict[str, Any]:
    """兼容 ``{"project": {...}}`` 与顶层扁平两种真实形状。"""
    inner = data.get("project")
    meta = inner if isinstance(inner, dict) else data
    return meta if isinstance(meta, dict) else {}


def _normalize_project(data: Dict[str, Any], pm: Path,
                       root: Dict[str, Any]) -> Dict[str, Any]:
    """把一份 .pm 归一成项目对象。

    ``root`` 是它被扫到的那个根（``{"path","label","kind","location"}``）。
    ``archived`` 由**物理位置**决定（根是不是归档根），而不是 .pm 里的标记 ——
    .pm 的 ``project.archived`` 只作为 ``pm_archived`` 如实带出，供 UI 暴露矛盾。
    """
    meta = _project_meta(data, pm)
    name = str(meta.get("name") or pm.stem).strip()
    start = _as_date(meta.get("start_date"))
    end = _as_date(meta.get("end_date"))

    raw_tasks = data.get("tasks")
    tasks_in = raw_tasks if isinstance(raw_tasks, list) else []
    tasks: List[Dict[str, Any]] = []
    counts = {"total": 0, "done": 0, "doing": 0, "todo": 0, "unscheduled": 0, "overdue": 0}
    today = _today()

    for t in tasks_in:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or "").strip()
        if not title:
            continue
        due = _as_date(t.get("due_date"))
        start_at, start_source = _task_start(t, start)
        status = str(t.get("status") or "")
        group = _status_group(status)

        subs = t.get("subtasks")
        subs = subs if isinstance(subs, list) else []
        subs_done = sum(1 for s in subs if isinstance(s, dict) and s.get("done"))

        tags = t.get("tags")
        tags = [str(x) for x in tags] if isinstance(tags, list) else []

        deps = t.get("dependencies")
        deps = [str(x) for x in deps] if isinstance(deps, list) else []

        entry = {
            "id": str(t.get("id") or f"{name}:{title}"),
            "title": title,
            "status": status,
            "status_group": group,
            "priority": (str(t.get("priority")) if t.get("priority") else None),
            "due": due,
            "start": start_at,
            "start_source": start_source,
            "subtasks": {"total": len(subs), "done": subs_done},
            "tags": tags,
            "dependencies": deps,
            "has_description": bool((t.get("description") or "").strip()),
        }
        counts["total"] += 1
        counts[group] += 1
        if not due:
            counts["unscheduled"] += 1
        elif group != "done" and due < today.isoformat():
            counts["overdue"] += 1
        tasks.append(entry)

    # 甘特图行序：有截止日的按 (起点, 截止日) 排，无截止日沉的排在后面
    tasks.sort(key=lambda x: (x["due"] is None, x["start"] or "9999-12-31", x["due"] or "9999-12-31"))

    milestones: List[Dict[str, Any]] = []
    raw_ms = data.get("milestones")
    for m in (raw_ms if isinstance(raw_ms, list) else []):
        if not isinstance(m, dict):
            continue
        date = _as_date(m.get("date"))
        if not date:
            continue
        milestones.append({
            "id": str(m.get("id") or m.get("title") or date),
            "title": str(m.get("title") or "里程碑"),
            "date": date,
            "color": (str(m.get("color")) if m.get("color") else None),
        })
    milestones.sort(key=lambda m: m["date"])

    location = root["location"]
    pm_archived = bool(meta.get("archived"))
    is_archived = location == "archive"

    # Active 里留着一个 .pm 标了 archived=true 的项目：物理上没搬走。
    # 既不能按标记把它藏起来（用户就看不见了），也不能说它已归档 —— 如实标记。
    anomaly = None
    if not is_archived and pm_archived:
        anomaly = ("pm 里标了 archived=true，但目录仍在 Active 根下（%s）—— 未真正迁移，"
                   "这里按「活动项目」显示，不做任何搬迁。" % root["path"])

    return {
        "id": name,                  # 占位；_assign_ids() 按跨根重名再定稿
        "legacy_id": name,           # 历史 id 就是项目名（映射/chat 槽位兼容用）
        "name": name,
        "dir_name": pm.parent.name,
        "dir_path": str(pm.parent),  # 真实项目目录：模块级对话的 cwd 用它，不猜
        "pm_file": pm.name,
        "location": location,        # "active" | "archive"
        "root": str(root["path"]),
        "root_label": root["label"],
        "root_kind": root["kind"],
        "archived": is_archived,     # 只由物理位置决定
        "pm_archived": pm_archived,  # .pm 自报的标记，原样带出
        "archive_anomaly": anomaly,
        "color": (str(meta.get("color")) if meta.get("color") else None),
        "template": (str(meta.get("template")) if meta.get("template") else None),
        "start": start,
        "end": end,
        "updated_at": (str(meta.get("updated_at")) if meta.get("updated_at") else None),
        "counts": counts,
        "tasks": tasks,
        "milestones": milestones,
    }


# --------------------------------------------------------------------------
# 根配置（Active + 可扩展的归档根）
# --------------------------------------------------------------------------
def _roots_file() -> Path:
    """根配置文件路径。``WB_PROJECT_ROOTS_CONFIG`` 可指向别处（测试用隔离数据，不碰真实配置）。"""
    override = os.environ.get("WB_PROJECT_ROOTS_CONFIG")
    if override:
        return Path(override)
    return _store_file().parent / ROOTS_CONFIG_NAME

def _default_roots() -> List[Dict[str, Any]]:
    """没有配置文件时的内置默认。

    归档不止一个物理位置：默认 Backup 归档根 + 课程库里的课程归档根。
    **只有在这里或配置文件里被注册过的路径才会被扫描**；接口不接受客户端传任意路径。
    """
    out = [{
        "path": str(PROJECTS_ROOT),
        "label": "ActiveProjects",
        "kind": "active",
        "location": "active",
    }]
    for path, label, kind in (
        (DEFAULT_ARCHIVE_ROOT, "ProjectArchives", "backup"),
        (COURSE_ARCHIVE_ROOT, "课程库·机器视觉", "course"),
    ):
        out.append({"path": str(path), "label": label, "kind": kind, "location": "archive"})
    return out


def _valid_root(raw: Any, default_location: str) -> Optional[Dict[str, Any]]:
    """校验一条根配置：必须是绝对路径、必须存在且是目录。非法条目直接丢弃而不是猜。"""
    if isinstance(raw, str):
        raw = {"path": raw}
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "").strip()
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts or _is_link(p):
        return None
    if not p.is_dir():
        return None
    location = str(raw.get("location") or default_location)
    if location not in ("active", "archive"):
        location = default_location
    return {
        "path": str(p),
        "label": str(raw.get("label") or p.name),
        "kind": str(raw.get("kind") or ("active" if location == "active" else "archive")),
        "location": location,
    }


def _project_roots() -> List[Dict[str, Any]]:
    """返回本次扫描要用的根列表（active 在前）。读配置文件，失败回落到默认。"""
    default = _default_roots()
    cfg: Dict[str, Any] = {}
    path = _roots_file()
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            cfg = {}

    roots: List[Dict[str, Any]] = []
    seen = set()

    def _add(entry: Optional[Dict[str, Any]]) -> None:
        if not entry:
            return
        key = os.path.normcase(os.path.abspath(entry["path"]))
        if key in seen:
            return
        seen.add(key)
        roots.append(entry)

    active = cfg.get("active_root")
    _add(_valid_root({"path": active, "label": "ActiveProjects", "kind": "active",
                      "location": "active"} if active else None, "active")
         or _valid_root(default[0], "active"))

    archives = cfg.get("archive_roots")
    if isinstance(archives, list) and archives:
        for item in archives:
            _add(_valid_root(item, "archive"))
    else:
        for item in default[1:]:
            _add(_valid_root(item, "archive"))

    # 一个归档根都不存在（比如离线机器）时，仍然要有它，好在 /roots 里说明「已注册但缺失」
    return roots


def _registered_roots() -> List[Dict[str, Any]]:
    """给 /roots 与 UI 看的根清单，含「已注册但当前不存在」的条目（不隐藏缺盘）。"""
    out = _project_roots()
    have = {os.path.normcase(os.path.abspath(r["path"])) for r in out}
    cfg_path = _roots_file()
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        for item in (cfg.get("archive_roots") or []) if isinstance(cfg, dict) else []:
            raw = item if isinstance(item, dict) else {"path": item}
            p = Path(str(raw.get("path") or ""))
            if not str(p) or not p.is_absolute() or ".." in p.parts:
                continue
            key = os.path.normcase(os.path.abspath(str(p)))
            if key in have:
                continue
            if p.exists() and (p.is_file() or _is_link(p)):
                continue        # 指向文件/链接的条目不是根，别拿来展示
            have.add(key)
            entry = {
                "path": str(p),
                "label": str(raw.get("label") or p.name),
                "kind": str(raw.get("kind") or "archive"),
                "location": "archive",
                "exists": p.is_dir(),
            }
            if not entry["exists"]:
                entry["missing"] = True
            out.append(entry)

    for r in out:
        r.setdefault("exists", Path(r["path"]).is_dir())
    return out


def _is_link(p: Path) -> bool:
    """符号链接 / Windows junction / 其它重解析点一律当链接，不跟随（防目录逃逸）。"""
    try:
        if p.is_symlink():
            return True
        st = p.lstat()
    except OSError:
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    reparse = getattr(os.stat_result, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if attrs & reparse:
        return True
    try:
        return os.path.realpath(p) != os.path.abspath(p)
    except OSError:
        return True


def _root_pm_files(root: Dict[str, Any]) -> List[Path]:
    """扫一个根下**一级**子目录里的 .pm。

    不做递归（课程库目录本身就是资料树，递归会扫到无关文件）；
    隐藏目录、链接/junction、解析后跑出根外的路径全部跳过。
    """
    base = Path(root["path"])
    if not base.is_dir() or _is_link(base):
        return []
    real_base = os.path.normcase(os.path.realpath(base))
    found: List[Path] = []
    try:
        children = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for child in children:
        name = child.name
        if name.startswith(".") or name.startswith("$"):
            continue
        try:
            if not child.is_dir() or _is_link(child):
                continue
        except OSError:
            continue
        real_child = os.path.normcase(os.path.realpath(child))
        if os.path.dirname(real_child) != real_base:   # 目录逃逸
            continue
        try:
            files = sorted(child.glob("*.pm"))
        except OSError:
            continue
        # 同名 .pm 优先（目录名 = 项目名的约定），其余仍认，交给跨根去重
        files.sort(key=lambda f: (f.stem != child.name, f.name))
        for pm in files:
            if pm.is_symlink() or _is_link(pm):
                continue
            real_pm = os.path.normcase(os.path.realpath(pm))
            if os.path.dirname(real_pm) != real_child:
                continue
            found.append(pm)
    return found


def _assign_ids(projects: List[Dict[str, Any]]) -> None:
    """跨根同名时定稿 id。

    规则（兼容历史：id 就是项目名）：
    * 名字全局唯一 → id 仍等于项目名，老映射 / 老 chat 槽位不受影响；
    * 名字撞车 → **活动的那份保号**（id = 名字），归档那份改成 ``名字@<根slug>``；
      两者都归档时按根顺序第一份保号。
    每份都带 ``legacy_id``（= 项目名），供映射回落只在该名字唯一时使用。
    """
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for p in projects:
        by_name.setdefault(p["name"], []).append(p)

    used = set()
    for name, group in by_name.items():
        if len(group) == 1:
            group[0]["id"] = name
            used.add(name)
            continue
        group.sort(key=lambda p: (p["location"] != "active", p["root"]))
        keeper = group[0]
        keeper["id"] = name
        used.add(name)
        for p in group[1:]:
            slug = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", p["root_label"] or p["root"]).strip("-") or "archive"
            cand = f"{name}@{slug}"
            n = 2
            while cand in used:
                cand = f"{name}@{slug}#{n}"
                n += 1
            p["id"] = cand
            used.add(cand)
            p["id_conflict_with_active"] = True


def _scan_projects(include_archived: bool) -> List[Dict[str, Any]]:
    roots = _project_roots()
    if not roots:
        return []
    raw: List[Dict[str, Any]] = []
    seen_pm = set()
    for root in roots:
        for pm in _root_pm_files(root):
            key = os.path.normcase(os.path.abspath(str(pm)))
            if key in seen_pm:
                continue                      # 根互相嵌套时同一份 .pm 只读一次
            seen_pm.add(key)
            data = _read_pm(pm)
            if data is None:
                continue
            raw.append(_normalize_project(data, pm, root))

    _assign_ids(raw)
    out = [p for p in raw if include_archived or not p["archived"]]
    # 三次稳定排序叠出「活动在前 → 组内 updated_at 倒序 → 名字」。
    # 用复合 key 反转字符串会把中文/emoji 搞出代理对问题，稳定排序更安全。
    out.sort(key=lambda p: p["name"])
    out.sort(key=lambda p: p["updated_at"] or "", reverse=True)
    out.sort(key=lambda p: p["archived"])
    return out


def _project_index(include_archived: bool = True) -> Dict[str, Dict[str, Any]]:
    return {p["id"]: p for p in _scan_projects(include_archived)}


def resolve_project(project_id: str) -> Optional[Dict[str, Any]]:
    """把客户端传来的 **项目 id** 解析成项目对象；解析不出来就 None（调用方回 404）。

    只做白名单内的查表：先按 id 精确匹配，再按项目名匹配**且该名字全局唯一**。
    永不把参数当路径用 —— 这是本文件的安全边界。
    """
    if not isinstance(project_id, str) or not project_id.strip():
        return None
    pid = project_id.strip()
    index = _project_index()
    hit = index.get(pid)
    if hit:
        return hit
    owners = [p for p in index.values() if p["name"] == pid]
    if len(owners) == 1:
        return owners[0]
    return None


# --------------------------------------------------------------------------
# Zotero 本地 API（只读）
# --------------------------------------------------------------------------
def _zotero_get(path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[Any, Dict[str, str]]:
    url = ZOTERO_ROOT + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Zotero-API-Version": "3",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=ZOTERO_TIMEOUT) as resp:
            body = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:            # 4xx/5xx 都是 Zotero 侧的拒绝
        raise HTTPException(status_code=502, detail=f"Zotero 返回 HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:  # 没启动 / 本地 API 未开
        raise HTTPException(
            status_code=503,
            detail="Zotero 本地 API 不可达（需要 Zotero 在运行且已开启 "
                   "设置→高级→允许其他应用通信）：%s" % e,
        )
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=502, detail="Zotero 返回了非 JSON 响应")
    return data, headers


def _zotero_paged(path: str) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """按 ``Total-Results`` 分页取全量，返回 (items, total)。"""
    items: List[Dict[str, Any]] = []
    total: Optional[int] = None
    start = 0
    for _ in range(MAX_ZOTERO_PAGES):
        page, headers = _zotero_get(path, {"limit": ZOTERO_PAGE_SIZE, "start": start})
        if not isinstance(page, list):
            break
        if total is None:
            try:
                total = int(headers.get("total-results", ""))
            except (TypeError, ValueError):
                total = None
        items.extend(x for x in page if isinstance(x, dict))
        start += len(page)
        if not page or (total is not None and start >= total):
            break
        if len(page) < ZOTERO_PAGE_SIZE:
            break
    return items, total


def _zotero_collections() -> List[Dict[str, Any]]:
    now = time.time()
    cached = _COL_CACHE.get("data")
    if cached is not None and now - float(_COL_CACHE.get("at") or 0) < _COL_TTL:
        return cached
    raw, _ = _zotero_paged("/collections")
    out: List[Dict[str, Any]] = []
    for c in raw:
        d = c.get("data") or {}
        key = d.get("key") or c.get("key")
        if not isinstance(key, str) or not _COLLECTION_KEY_RE.match(key):
            continue
        meta = c.get("meta") or {}
        parent = d.get("parentCollection")
        out.append({
            "key": key,
            "name": str(d.get("name") or ""),
            "parent": parent if isinstance(parent, str) else None,
            "num_items": meta.get("numItems"),
            "num_collections": meta.get("numCollections"),
        })
    out.sort(key=lambda c: c["name"].lower())
    _COL_CACHE["at"] = now
    _COL_CACHE["data"] = out
    return out


def _collection_path(cols: Dict[str, Dict[str, Any]], key: str) -> str:
    names: List[str] = []
    seen = set()
    cur: Optional[str] = key
    while cur and cur in cols and cur not in seen:
        seen.add(cur)
        names.append(cols[cur]["name"])
        parent = cols[cur].get("parent")
        cur = parent if isinstance(parent, str) else None
    return " / ".join(reversed(names))


def _creator_name(c: Dict[str, Any]) -> str:
    last = str(c.get("lastName") or "").strip()
    first = str(c.get("firstName") or "").strip()
    if last and first:
        return f"{last} {first}"
    return (last or first or str(c.get("name") or "")).strip()


def _year_of(item: Dict[str, Any]) -> Optional[str]:
    for field in ("date", "issueDate"):
        val = item.get(field)
        if isinstance(val, str) and len(val) >= 4 and val[:4].isdigit():
            return val[:4]
    return None


def _normalize_item(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    d = entry.get("data") or {}
    key = d.get("key") or entry.get("key")
    if not isinstance(key, str) or not key:
        return None
    creators = d.get("creators")
    creators = creators if isinstance(creators, list) else []
    authors = [n for n in (_creator_name(c) for c in creators if isinstance(c, dict)) if n]
    tags = d.get("tags")
    tags = [str(t.get("tag")) for t in tags if isinstance(t, dict) and t.get("tag")] \
        if isinstance(tags, list) else []
    return {
        "key": key,
        "title": str(d.get("title") or "（无标题）"),
        "authors": authors,
        "author_line": "、".join(authors[:3]) + (" 等" if len(authors) > 3 else ""),
        "year": _year_of(d),
        "item_type": str(d.get("itemType") or ""),
        "publication": str(d.get("publicationTitle") or d.get("bookTitle")
                            or d.get("proceedingsTitle") or d.get("websiteTitle") or ""),
        "doi": str(d.get("DOI") or "") or None,
        "url": str(d.get("url") or "") or None,
        "tags": tags[:6],
        "zotero_uri": f"zotero://select/library/items/{key}",
    }


def _collection_items(collection_key: str) -> Dict[str, Any]:
    """取集合下（含子集合）的顶层文献元数据。只读。"""
    if not _COLLECTION_KEY_RE.match(collection_key):
        raise HTTPException(status_code=400, detail="collection key 形状非法（应为 8 位大写字母/数字）")
    raw, total = _zotero_paged(f"/collections/{collection_key}/items/top")
    items = [x for x in (_normalize_item(e) for e in raw) if x]
    items.sort(key=lambda x: (x["year"] or "0000", x["title"]), reverse=True)
    return {"collection": collection_key, "total_results": total, "returned": len(items), "items": items}


# --------------------------------------------------------------------------
# 项目 → collection 映射（本地持久化，不猜）
# --------------------------------------------------------------------------
def _mapping_key(project: Dict[str, Any]) -> str:
    """这个项目在映射表里用的键。

    历史键就是项目名（= 当时唯一的 id）。现在 id 仍等于项目名，**除非**跨根重名
    （归档那份拿到 ``名字@根slug``）。重名时归档那份一律用自己的新 id 作键，
    绝不回落到项目名 —— 否则会和活动项目抢同一个 collection（映射串号）。
    """
    return project["id"]


def _mapped_collection_key(project: Dict[str, Any], mapping: Dict[str, str]) -> Optional[str]:
    key = _mapping_key(project)
    if key in mapping:
        return mapping[key]
    # 老映射兼容：只有当本项目的 id 就是历史 id（项目名）时，才认名字键。
    legacy = project.get("legacy_id")
    if legacy and legacy == key and legacy in mapping:
        return mapping[legacy]
    return None

def _store_file() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir  # 与 CLI/其它插件同一约定
        return plugin_data_dir("wb-project") / "zotero-mappings.json"
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
        root = home / "plugin-data" / "wb-project"
        root.mkdir(parents=True, exist_ok=True)
        return root / "zotero-mappings.json"


def _load_mappings() -> Dict[str, str]:
    path = _store_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and _COLLECTION_KEY_RE.match(v)}


def _save_mappings(mapping: Dict[str, str]) -> None:
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True)
    with _MAP_LOCK:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@router.get("/health")
def health() -> Dict[str, Any]:
    zotero: Dict[str, Any] = {"reachable": False}
    try:
        data, _ = _zotero_get("/collections", {"limit": 1})
        zotero["reachable"] = isinstance(data, list)
        zotero["sample"] = len(data) if isinstance(data, list) else 0
    except HTTPException as e:
        zotero["error"] = e.detail
    roots = _registered_roots()
    return {
        "ok": True,
        "plugin": "wb-project",
        # 保留老字段（部署校验/旧前端在用），但它的含义是「活动根」
        "projects_root": str(PROJECTS_ROOT),
        "projects_root_exists": PROJECTS_ROOT.is_dir(),
        "roots": roots,
        "roots_config": str(_roots_file()),
        "roots_config_exists": _roots_file().is_file(),
        "zotero": zotero,
    }


@router.get("/roots")
def roots() -> Dict[str, Any]:
    """已注册的扫描根（活动根 + 各归档根）。

    只读：这里**不能**新增/修改根。加一个新归档位置要改服务端配置文件
    ``plugin-data/wb-project/project-roots.json``（人/agent 显式登记），
    接口不开「客户端传路径」的口子 —— 那等于任意目录读取。
    """
    items = _registered_roots()
    return {
        "config_file": str(_roots_file()),
        "roots": items,
        "active_count": sum(1 for r in items if r["location"] == "active"),
        "archive_count": sum(1 for r in items if r["location"] == "archive"),
        "note": "归档位置不是硬编码的：默认 ProjectArchives，课程项目另注册课程库根。"
                "新增位置请改服务端配置文件，不接受接口传任意路径。",
    }


class TaskStatusUpdate(BaseModel):
    status: str


@router.put("/projects/{project_id}/tasks/{task_id}/status")
def update_task_status(project_id: str, task_id: str, body: TaskStatusUpdate) -> Dict[str, Any]:
    """Update only status of a task in a registered, active-root .pm."""
    allowed = {"待办", "进行中", "已完成"}
    if body.status not in allowed:
        raise HTTPException(status_code=400, detail="非法任务状态")
    project = resolve_project(project_id)
    if project is None or project.get("archived") or project.get("location") != "active":
        raise HTTPException(status_code=404, detail="未知活动项目")
    pm = Path(project["dir_path"]) / project["pm_file"]
    root = next((r for r in _project_roots() if r["location"] == "active" and
                 os.path.normcase(os.path.realpath(r["path"])) == os.path.normcase(os.path.realpath(project["root"]))), None)
    if root is None or _is_link(pm.parent) or _is_link(pm) or not pm.is_file():
        raise HTTPException(status_code=404, detail="项目文件不可用")
    base = os.path.normcase(os.path.realpath(root["path"]))
    if os.path.dirname(os.path.normcase(os.path.realpath(pm.parent))) != base or os.path.dirname(os.path.normcase(os.path.realpath(pm))) != os.path.normcase(os.path.realpath(pm.parent)):
        raise HTTPException(status_code=404, detail="项目路径越界")
    if pm.stat().st_size > MAX_PM_BYTES:
        raise HTTPException(status_code=400, detail="项目文件超过大小限制")
    try:
        raw = pm.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        raise HTTPException(status_code=400, detail="项目 JSON 损坏")
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise HTTPException(status_code=400, detail="项目 JSON 结构无效")
    task = next((t for t in data["tasks"] if isinstance(t, dict) and str(t.get("id") or "") == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    task["status"] = body.status
    backup = pm.with_name(pm.name + ".bak-before-status-" + _dt.date.today().strftime("%Y%m%d"))
    try:
        if backup.exists():
            backup = pm.with_name(pm.name + ".bak-before-status-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        backup.write_bytes(raw)
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=str(pm.parent), prefix=pm.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, pm)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="写入项目失败：%s" % exc)
    updated_data = _read_pm(pm)
    normalized = _normalize_project(updated_data, pm, root)
    _assign_ids([normalized])
    normalized_task = next(t for t in normalized["tasks"] if t["id"] == task_id)
    return {"ok": True, "task": normalized_task, "project": normalized, "backup": backup.name}


@router.get("/projects")
def projects(include_archived: bool = Query(False)) -> Dict[str, Any]:
    items = _scan_projects(include_archived)
    roots_used = _registered_roots()
    anomalies = [
        {
            "id": p["id"],
            "name": p["name"],
            "dir_path": p["dir_path"],
            "detail": p["archive_anomaly"],
        }
        for p in items if p.get("archive_anomaly")
    ]
    return {
        "root": str(PROJECTS_ROOT),                 # 老字段：活动根
        "roots": roots_used,                        # 本次实际参与扫描的根
        "include_archived": include_archived,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "count": len(items),
        "active_count": sum(1 for p in items if not p["archived"]),
        "archived_count": sum(1 for p in items if p["archived"]),
        "anomalies": anomalies,                     # Active 里 pm 标 archived=true 的
        "projects": items,
    }


@router.get("/zotero/status")
def zotero_status() -> Dict[str, Any]:
    try:
        cols = _zotero_collections()
    except HTTPException as e:
        return {"reachable": False, "error": e.detail, "collections": 0}
    return {"reachable": True, "collections": len(cols), "endpoint": ZOTERO_ROOT}


@router.get("/zotero/collections")
def zotero_collections() -> Dict[str, Any]:
    cols = _zotero_collections()
    index = {c["key"]: c for c in cols}
    for c in cols:
        c["path"] = _collection_path(index, c["key"])
        c["depth"] = c["path"].count(" / ")
    return {"count": len(cols), "collections": cols}


@router.get("/zotero/link")
def zotero_link(project: str = Query(..., min_length=1, max_length=200)) -> Dict[str, Any]:
    """解析某项目对应的 Zotero 集合。

    精确匹配规则，**不做模糊猜测**：
    1. 本地已保存映射（按项目 id）且该 key 仍存在 → matched, source="mapping"
    2. 集合名与项目名完全相同（去首尾空白后逐字符相等）→ matched, source="exact"
    3. 否则 matched=false，返回候选供 UI 显式选择；候选只是候选，不自动绑定。

    ``project`` 只接受 /projects 返回的 id（或全局唯一的项目名）；不是路径。
    """
    target = resolve_project(project)
    if target is None:
        raise HTTPException(status_code=404, detail="未知项目 id（只接受 /projects 返回的项目 id）")

    cols = _zotero_collections()
    index = {c["key"]: c for c in cols}
    mapping = _load_mappings()

    mapped_key = _mapped_collection_key(target, mapping)
    if mapped_key and mapped_key in index:
        col = dict(index[mapped_key])
        col["path"] = _collection_path(index, mapped_key)
        return {"matched": True, "source": "mapping", "project": target["id"],
                "location": target["location"], "collection": col}

    exact = [c for c in cols if c["name"].strip() == target["name"].strip()]
    if len(exact) == 1:
        col = dict(exact[0])
        col["path"] = _collection_path(index, col["key"])
        return {"matched": True, "source": "exact", "project": target["id"],
                "location": target["location"], "collection": col}

    return {
        "matched": False,
        "source": "none",
        "project": target["id"],
        "location": target["location"],
        "candidates": [dict(c, path=_collection_path(index, c["key"]))
                       for c in sorted(cols, key=lambda x: x["name"].lower())],
        "note": "未找到与项目名完全相同的 Zotero 集合，请在 UI 中显式选择并保存映射。",
    }


@router.get("/zotero/items")
def zotero_items(collection: str = Query(..., min_length=8, max_length=8)) -> Dict[str, Any]:
    if not _COLLECTION_KEY_RE.match(collection):
        raise HTTPException(status_code=400, detail="collection key 形状非法（应为 8 位大写字母/数字）")
    # Zotero 对形状合法但不存在的 key 返回 200 + 空数组，必须自己核对存在性
    known = {c["key"] for c in _zotero_collections()}
    if collection not in known:
        raise HTTPException(status_code=404, detail="该 collection key 在当前 Zotero 库中不存在")
    return _collection_items(collection)


@router.get("/zotero/mappings")
def zotero_mappings() -> Dict[str, Any]:
    return {"mappings": _load_mappings(), "file": _store_file().name}


@router.put("/zotero/mappings")
def zotero_set_mapping(project: str = Query(..., min_length=1, max_length=200),
                       collection: Optional[str] = Query(None, min_length=8, max_length=8)) -> Dict[str, Any]:
    """保存 / 清除一个项目到 collection 的映射。只能写插件自己的 plugin-data。

    键用项目的**当前 id**。历史键（= 项目名）只在它确实等于当前 id 时才被更新，
    这样跨根重名时不会出现「给归档项目保存集合，结果活动项目也跟着变」。
    """
    target = resolve_project(project)
    if target is None:
        raise HTTPException(status_code=404, detail="未知项目 id（只接受 /projects 返回的项目 id）")

    key = _mapping_key(target)
    mapping = _load_mappings()
    if collection is None:
        mapping.pop(key, None)
        if key != target["id"]:
            mapping.pop(target["id"], None)
        _save_mappings(mapping)
        return {"ok": True, "project": target["id"], "collection": None, "mappings": mapping}

    if not _COLLECTION_KEY_RE.match(collection):
        raise HTTPException(status_code=400, detail="collection key 形状非法")
    cols = {c["key"] for c in _zotero_collections()}
    if collection not in cols:
        raise HTTPException(status_code=404, detail="该 collection key 在当前 Zotero 库中不存在")

    mapping[key] = collection
    _save_mappings(mapping)
    return {"ok": True, "project": target["id"], "collection": collection, "mappings": mapping}


# ==========================================================================
# 项目文件夹：懒加载层级浏览 / 安全预览 / 下载
# ==========================================================================
# 安全边界（本段必须保持，全部由 tests/test_wb_project_files.py 覆盖）：
# * 只认**已登记项目 id**；``path`` 只能是该项目目录内的**安全相对路径**，
#   绝不接受绝对路径 / 盘符 / ``..`` / 反斜杠 / NUL / 控制字符。
# * 每一级分量都 lstat 检查（symlink / Windows junction / 其他重解析点一律拒绝），
#   且 realpath 必须仍在项目真实目录内 —— 防目录穿越与链接逃逸。
# * 敏感文件（.env / .git / 密钥 / 凭据 / token 库）**不出现在列表里**，
#   也永远不能被预览或下载（在服务端拦，不是靠前端隐藏）。
# * 不做整树递归：``/tree`` 一次只列**一层**，超大目录按 offset/limit 分页。
# * 只有图片（含 SVG）与 PDF 允许 inline；Office / 压缩包 / 可执行 / 未知二进制
#   一律 ``application/octet-stream`` + ``Content-Disposition: attachment``。
# * 预览响应固定带 ``nosniff`` + ``CSP: sandbox`` + ``CORP: same-origin``：
#   SVG/HTML 里的内联脚本**即便直接导航也不执行**（真机 Chrome/Edge 实测，见 tests）。
# * 归档项目只读：本段所有端点都不写盘；写盘只发生在下面的「创建/纳管」段，
#   且只在活动根、且目录必须不存在（new）或尚无 .pm（adopt）。

import mimetypes
import uuid as _uuid
from typing import Iterable as _Iterable

from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

MAX_TEXT_BYTES = int(os.environ.get("WB_PROJECT_MAX_TEXT_BYTES") or 512 * 1024)
MAX_IMAGE_BYTES = int(os.environ.get("WB_PROJECT_MAX_IMAGE_BYTES") or 20 * 1024 * 1024)
MAX_RAW_BYTES = int(os.environ.get("WB_PROJECT_MAX_RAW_BYTES") or 64 * 1024 * 1024)
MAX_LIST_ENTRIES = 500          # 单层一次最多返回多少条（硬上限）
DEFAULT_LIST_ENTRIES = 200
MAX_NAME_LEN = 120

# 「文本渲染」白名单：前端只用 textContent 纯文本展示，绝不解析 HTML、不执行。
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".ini", ".cfg", ".conf",
    ".yaml", ".yml", ".toml", ".json", ".jsonl", ".xml", ".tex", ".bib", ".py", ".pyi",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".scss", ".sh", ".bash",
    ".ps1", ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp", ".m", ".java", ".kt", ".go",
    ".rs", ".sql", ".gitignore", ".gitattributes", ".editorconfig",
}
# 图片：全部走 <img>（SVG 也是），内联脚本不执行 —— 这是实测结论，不是偏好。
IMAGE_MIMES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".ico": "image/x-icon", ".svg": "image/svg+xml",
    ".avif": "image/avif",
}
# 复杂 Office / 压缩容器 / 可执行：只下载，不做任何浏览器内解析。
DOWNLOAD_ONLY_EXTS = {
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".odt", ".ods", ".odp", ".rtf",
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".exe", ".msi", ".dll", ".so",
    ".dylib", ".bin", ".iso", ".img",
}
# 敏感名：出现在列表里就已经是泄漏，所以列表阶段就剔除（不是等预览时再拦）。
SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", ".npmrc", ".netrc", "_netrc", ".pgpass",
    ".git-credentials", ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "credentials.json", "auth.json", "secrets.json", "token.json",
    ".htpasswd", "security.json",
}
SENSITIVE_DIRS = {
    ".git", ".hg", ".svn", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure",
    "__pycache__", "node_modules", ".venv", "venv", ".cache", ".next",
}
SENSITIVE_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".kdbx")

_WIN_RESERVED = {"con", "prn", "aux", "nul",
                 *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_PM_NAME_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])_.+$")
_BAD_NAME_CHARS = set('<>:"/\\|?*')
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

PM_TEMPLATE_DIRS = {
    "todo": [],
    "default": ["Bitmap", "Hardware", "Software", "Reference", "DataSheet"],
    "hardware": ["Bitmap", "Hardware", "Software", "DataSheet", "BOM"],
    "electron": ["src", "assets", "build", "docs"],
    "web": ["src", "public", "docs"],
}
PM_TEMPLATES = tuple(PM_TEMPLATE_DIRS)
STATUS_COLUMNS = ["待办", "进行中", "已完成"]


def _sensitive(name: str) -> bool:
    low = name.casefold()
    if low in SENSITIVE_NAMES or low in SENSITIVE_DIRS:
        return True
    if low.startswith(".env"):
        return True
    return any(low.endswith(sfx) for sfx in SENSITIVE_SUFFIXES)


def _hidden(name: str) -> bool:
    return name.startswith(".") or name.startswith("$")


def _project_dir_or_404(project_id: str) -> Tuple[Dict[str, Any], Path]:
    """已登记项目 → (项目对象, 真实目录)。未知 id 一律 404：永不把参数当路径。"""
    target = resolve_project(project_id)
    if target is None:
        raise HTTPException(status_code=404, detail="未知项目 id（只接受 /projects 返回的项目 id）")
    raw = target.get("dir_path")
    if not isinstance(raw, str) or not raw:
        raise HTTPException(status_code=404, detail="该项目的真实目录未知（.pm 未绑定 storage 路径）")
    base = Path(raw)
    try:
        if not base.is_dir():
            raise HTTPException(status_code=404, detail="项目目录当前不可达（盘/路径不存在）")
    except OSError as e:
        raise HTTPException(status_code=404, detail=f"项目目录不可读：{e}")
    if _is_link(base):
        raise HTTPException(status_code=403, detail="项目目录是符号链接/junction，已拒绝访问")
    return target, base


def _safe_rel(rel: str) -> List[str]:
    """把客户端传来的相对路径拆成**已验证**的分量；任何可疑形状直接拒绝。"""
    raw = str(rel or "").strip()
    if not raw or raw in (".", "./"):
        return []
    if len(raw) > 1024:
        raise HTTPException(status_code=400, detail="路径过长")
    if "\0" in raw or _CTRL_RE.search(raw):
        raise HTTPException(status_code=400, detail="路径含控制字符")
    if raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw):
        raise HTTPException(status_code=400, detail="只接受项目目录内的相对路径，不接受绝对路径/盘符")
    if "\\" in raw:
        raise HTTPException(status_code=400, detail="路径分隔符请用 /（反斜杠会绕过 Windows 上的分量校验）")
    parts: List[str] = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise HTTPException(status_code=400, detail="路径不允许包含 ..")
        if _sensitive(seg):
            raise HTTPException(status_code=403, detail="该路径涉及敏感文件，已拒绝")
        parts.append(seg)
    return parts


def _walk_join(base: Path, parts: List[str]) -> Path:
    """逐级拼接并校验：每级都不许是链接，realpath 不许跑出项目目录。"""
    real_base = os.path.normcase(os.path.realpath(base))
    cur = base
    reparse = getattr(os.stat_result, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for seg in parts:
        cur = cur / seg
        try:
            st = cur.lstat()
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="路径不存在")
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"路径不可访问：{e}")
        if cur.is_symlink() or (getattr(st, "st_file_attributes", 0) & reparse):
            raise HTTPException(status_code=403, detail="路径包含符号链接/junction，已拒绝")
    real = os.path.normcase(os.path.realpath(cur))
    if real != real_base and not real.startswith(real_base.rstrip(os.sep) + os.sep):
        raise HTTPException(status_code=403, detail="路径解析后跑出项目目录，已拒绝")
    return cur


def _category(path: Path) -> str:
    """一个文件在 UI 里算什么：text / image / pdf / download / other。"""
    ext = path.suffix.casefold()
    if ext in IMAGE_MIMES:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in TEXT_EXTS:
        return "text"
    return "download"


def _entry(base: Path, path: Path) -> Dict[str, Any]:
    name = path.name
    is_dir = path.is_dir()
    out: Dict[str, Any] = {
        "name": name,
        "path": path.relative_to(base).as_posix(),
        "kind": "dir" if is_dir else "file",
        "hidden": _hidden(name),
    }
    try:
        st = path.lstat()
        out["mtime"] = _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
    except OSError:
        out["mtime"] = None
        return out
    if is_dir:
        out["category"] = "dir"
        return out
    size = int(st.st_size)
    ext = path.suffix.casefold()
    cat = _category(path)
    out.update({
        "size": size,
        "ext": ext,
        "category": cat,
        "mime": IMAGE_MIMES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream",
        "previewable": cat in ("text", "image", "pdf") and size <= MAX_RAW_BYTES,
        "downloadable": size <= MAX_RAW_BYTES,
    })
    return out


@router.get("/projects/{project_id}/tree")
def project_tree(project_id: str,
                 path: str = Query("", max_length=1024),
                 include_hidden: bool = Query(False),
                 offset: int = Query(0, ge=0),
                 limit: int = Query(DEFAULT_LIST_ENTRIES, ge=1, le=MAX_LIST_ENTRIES)) -> Dict[str, Any]:
    """**单层**目录列表（懒加载：展开一层才请求一层，绝不整树递归）。

    归档项目可读（只读浏览）；敏感名不出现；链接目录不跟随；超大目录分页。
    """
    target, base = _project_dir_or_404(project_id)
    parts = _safe_rel(path)
    node = _walk_join(base, parts)
    if not node.is_dir():
        raise HTTPException(status_code=400, detail="该路径不是目录")

    entries: List[Dict[str, Any]] = []
    try:
        with os.scandir(node) as it:
            for de in it:
                nm = de.name
                if _sensitive(nm) or (not include_hidden and _hidden(nm)):
                    continue
                try:
                    entries.append(_entry(base, Path(de.path)))
                except OSError:
                    continue
    except PermissionError:
        raise HTTPException(status_code=403, detail="该目录没有读取权限")
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"目录不可读：{e}")

    entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].casefold()))
    total = len(entries)
    page = entries[offset:offset + limit]
    return {
        "project": target["id"],
        "path": node.relative_to(base).as_posix() if parts else "",
        "dir_name": base.name,
        "abs_dir": str(node),
        "archived": bool(target.get("archived")),
        "read_only": bool(target.get("archived")),
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "has_more": offset + len(page) < total,
        "entries": page,
        "limits": {"max_entries": MAX_LIST_ENTRIES, "max_raw_bytes": MAX_RAW_BYTES,
                   "max_text_bytes": MAX_TEXT_BYTES, "max_image_bytes": MAX_IMAGE_BYTES},
    }


def _text_preview(path: Path, size: int) -> Dict[str, Any]:
    """文本预览：UTF-8 优先，失败按 GB18030 兜一次；截断如实标记，不静默丢内容。"""
    budget = min(size, MAX_TEXT_BYTES)
    try:
        with open(path, "rb") as fh:
            raw = fh.read(budget + 1)
    except PermissionError:
        raise HTTPException(status_code=403, detail="该文件没有读取权限")
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"文件不可读：{e}")
    truncated = len(raw) > budget
    raw = raw[:budget]

    if raw and raw.count(b"\0") / len(raw) > 0.02:
        return {"kind": "download", "download_only": True,
                "reason": "内容含大量 NUL 字节，判定为二进制，只能下载"}

    encoding = "utf-8"
    decode_error = None
    text: Optional[str] = None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        decode_error = f"UTF-8 解码失败（字节 {e.start}）：{e.reason}"
        encoding = "gb18030"
        try:
            text = raw.decode("gb18030", errors="replace")
            decode_error += "；已按 GB18030 兜底解码，个别字符显示为 U+FFFD"
        except Exception:
            text = None
    if text is None:
        return {"kind": "download", "download_only": True,
                "reason": "文本解码失败，按二进制处理，只能下载"}

    ext = path.suffix.casefold().lstrip(".")
    lang = {"md": "markdown", "markdown": "markdown", "json": "json", "jsonl": "jsonl",
            "py": "python", "js": "javascript", "mjs": "javascript", "ts": "typescript",
            "tsx": "tsx", "jsx": "jsx", "css": "css", "scss": "scss", "tex": "latex",
            "yaml": "yaml", "yml": "yaml", "toml": "toml", "sh": "bash", "bash": "bash",
            "c": "c", "h": "c", "cpp": "cpp", "hpp": "cpp", "java": "java", "sql": "sql",
            "xml": "xml", "csv": "csv", "tsv": "tsv"}.get(ext, "text")
    out: Dict[str, Any] = {
        "kind": "text",
        "text": text,
        "encoding": encoding,
        "decode_error": decode_error,
        "lang": lang,
        "bytes": size,
        "shown_bytes": len(raw),
        "truncated": truncated,
        # 明确契约：前端只做纯文本渲染（textContent），绝不当 HTML 注入。
        "render": "textContent",
    }
    if truncated:
        out["notice"] = (f"文件 {size} 字节，超过预览上限 {MAX_TEXT_BYTES} 字节，"
                         f"只显示前 {len(raw)} 字节")
    return out


@router.get("/projects/{project_id}/file")
def project_file(project_id: str,
                 path: str = Query(..., min_length=1, max_length=1024)) -> Dict[str, Any]:
    """单个文件的预览描述。**不提供 HTML 执行路径**：文本给原文，图片/PDF 给受控 raw 链接。"""
    target, base = _project_dir_or_404(project_id)
    parts = _safe_rel(path)
    if not parts:
        raise HTTPException(status_code=400, detail="必须给出文件相对路径")
    node = _walk_join(base, parts)
    if node.is_dir():
        raise HTTPException(status_code=400, detail="该路径是目录，请用 /tree")

    try:
        st = node.stat()
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"文件不可读：{e}")
    size = int(st.st_size)
    rel = node.relative_to(base).as_posix()
    raw_url = (f"/api/plugins/wb-project/projects/{urllib.parse.quote(str(target['id']))}"
               f"/raw?path={urllib.parse.quote(rel)}")
    meta: Dict[str, Any] = {
        "project": target["id"],
        "path": rel,
        "name": node.name,
        "bytes": size,
        "mtime": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "archived": bool(target.get("archived")),
        "read_only": bool(target.get("archived")),
        "download_url": raw_url + "&download=1",
    }
    if size > MAX_RAW_BYTES:
        return {**meta, "kind": "download", "download_only": True,
                "reason": f"文件 {size} 字节超过上限 {MAX_RAW_BYTES} 字节，只能下载"}

    cat = _category(node)
    if cat == "text":
        return {**meta, **_text_preview(node, size)}
    if cat == "image":
        if size > MAX_IMAGE_BYTES:
            return {**meta, "kind": "download", "download_only": True,
                    "reason": f"图片 {size} 字节超过预览上限 {MAX_IMAGE_BYTES} 字节，只能下载"}
        return {**meta, "kind": "image", "raw_url": raw_url,
                "mime": IMAGE_MIMES.get(node.suffix.casefold(), "application/octet-stream"),
                "notice": "SVG 也走 <img> 内联：内联脚本不执行（真机 Chrome/Edge 实测）"}
    if cat == "pdf":
        return {**meta, "kind": "pdf", "raw_url": raw_url, "mime": "application/pdf",
                "preview_supported": True,
                "notice": "PDF 由浏览器内置阅读器渲染（同源 blob 实测可显示）。"
                          "无 PDF 插件或 headless 环境下会空白，此时请下载后查看。"}
    return {**meta, "kind": "download", "download_only": True,
            "reason": "该格式不做网页内预览（Office/压缩包/可执行/未知二进制），只能下载"}


@router.get("/projects/{project_id}/raw")
def project_raw(project_id: str,
                path: str = Query(..., min_length=1, max_length=1024),
                download: bool = Query(False),
                inline: bool = Query(False)):
    """原始字节流。只有图片（含 SVG）与 PDF 允许 inline；其余**强制附件**。"""
    target, base = _project_dir_or_404(project_id)
    parts = _safe_rel(path)
    if not parts:
        raise HTTPException(status_code=400, detail="必须给出文件相对路径")
    node = _walk_join(base, parts)
    if node.is_dir():
        raise HTTPException(status_code=400, detail="该路径是目录")
    try:
        size = node.stat().st_size
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"文件不可读：{e}")
    if size > MAX_RAW_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"文件 {size} 字节超过单次下载上限 {MAX_RAW_BYTES} 字节")

    ext = node.suffix.casefold()
    safe_inline = ext in IMAGE_MIMES or ext == ".pdf"
    allow_inline = (inline or not download) and safe_inline
    media = (IMAGE_MIMES.get(ext) or "application/pdf") if allow_inline else "application/octet-stream"
    headers = {
        "X-Content-Type-Options": "nosniff",
        # sandbox：直接导航该 URL 时脚本也不执行（实测 SVG 内联脚本被拦）
        "Content-Security-Policy": ("sandbox; default-src 'none'; style-src 'unsafe-inline'; "
                                   "img-src 'self' data:; object-src 'none'; frame-ancestors 'self'"),
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cache-Control": "private, max-age=60",
        "Content-Disposition": ("inline" if allow_inline else "attachment") +
                               f"; filename*=UTF-8''{urllib.parse.quote(node.name)}",
        # HTTP 头只能是 latin-1：项目 id 常含中文，必须百分号编码后再放进头里。
        "X-WB-Project": urllib.parse.quote(str(target["id"])),
        "X-WB-Archived": "1" if target.get("archived") else "0",
    }
    return FileResponse(str(node), media_type=media, headers=headers)


# --------------------------------------------------------------------------
# 项目创建 / 纳管（.pm）：计划 → 确认 → 落盘 → 读回校验
# --------------------------------------------------------------------------
class ProjectPlanBody(BaseModel):
    """创建/纳管的**计划**请求。项目名 = 目录名 = .pm 文件名（同一字符串）。"""
    mode: str = Field("new", description="new=新建规范项目目录；adopt=把已有无 .pm 目录补成项目")
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    template: str = Field("todo", max_length=32)
    description: str = Field("", max_length=4000)
    start_date: Optional[str] = Field(None, max_length=10)
    end_date: Optional[str] = Field(None, max_length=10)
    color: str = Field("#4f46e5", max_length=32)
    dirs: Optional[List[str]] = Field(None, description="仅 new：额外要建的子目录")


def _validate_project_name(name: str) -> str:
    """项目名须符合既有 ``YYYY-MM_名称`` 规范，且是合法的 Windows 目录名。"""
    nm = str(name or "").strip()
    if not nm:
        raise HTTPException(status_code=400, detail="项目名不能为空")
    if len(nm) > MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail=f"项目名超过 {MAX_NAME_LEN} 字符")
    if _CTRL_RE.search(nm):
        raise HTTPException(status_code=400, detail="项目名含控制字符")
    if any(ch in _BAD_NAME_CHARS for ch in nm):
        raise HTTPException(status_code=400, detail='项目名不能含 <>:"/\\|?* 或路径分隔符')
    if nm != nm.strip(" ."):
        raise HTTPException(status_code=400, detail="项目名不能以空格或点开头/结尾")
    if nm.casefold().split(".")[0] in _WIN_RESERVED:
        raise HTTPException(status_code=400, detail=f"「{nm}」是 Windows 保留名，不能用作目录名")
    if not _PM_NAME_RE.match(nm):
        raise HTTPException(
            status_code=400,
            detail="项目名须符合既有规范：YYYY-MM_名称（例 2026-11_航空航天机器人化制造课程论文）")
    return nm


def _active_root() -> Path:
    """**只有活动根**可写。归档根永远只读：归档是单独的人工动作，不在这里做。"""
    for r in _project_roots():
        if r["location"] == "active":
            return Path(r["path"])
    raise HTTPException(status_code=503, detail="活动根未登记或不可达，无法创建项目")


def _build_pm(name: str, template: str, description: str, start_date: Optional[str],
              end_date: Optional[str], color: str) -> Dict[str, Any]:
    """生成最小合规 .pm（schema_version=3）。空任务列表是合法状态。"""
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    today = _dt.date.today().isoformat()
    readme = f"# {name}\n\n{description.strip()}\n" if description.strip() else f"# {name}\n"
    dirs = list(PM_TEMPLATE_DIRS.get(template, []))
    return {
        "version": "1.0",
        "schema_version": 3,
        "project": {
            "id": str(_uuid.uuid4()),
            "name": name,
            "description": description.strip(),
            "template": template,
            "color": color or "#4f46e5",
            "created_at": ts,
            "updated_at": ts,
            "archived": False,
            "start_date": _as_date(start_date),
            "end_date": _as_date(end_date),
            "kanban_columns": list(STATUS_COLUMNS),
            "sync_enabled": True,
            "sort_order": 1000,
        },
        "readme_file": "README.md",
        "template": {"dirs": dirs, "files": ["README.md"],
                     "file_contents": {"README.md": readme}},
        "tasks": [],
        "tags": [],
        "milestones": [],
        "changelog": [{"version": "0.1.0", "date": today, "info": "初始版本", "auto": False}],
        "flowchart": {"nodes": [], "edges": []},
    }


def _plan_id(payload: Dict[str, Any]) -> str:
    import hashlib
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def _existing_pm(dir_path: Path) -> Optional[Path]:
    """目录里已有的 .pm（同名优先）；存在就表示「已纳管」，绝不覆盖。"""
    try:
        same = dir_path / f"{dir_path.name}.pm"
        if same.is_file() and not _is_link(same):
            return same
        for p in sorted(dir_path.glob("*.pm")):
            if p.is_file() and not _is_link(p):
                return p
    except OSError:
        return None
    return None


def _scan_untracked(root: Path) -> List[Dict[str, Any]]:
    """活动根下**尚无 .pm** 的一级目录（可纳管候选）。隐藏/链接/敏感目录跳过。"""
    out: List[Dict[str, Any]] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.casefold())
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"活动根不可读：{e}")
    for child in children:
        nm = child.name
        if _sensitive(nm) or _hidden(nm):
            continue
        try:
            if not child.is_dir() or _is_link(child):
                continue
        except OSError:
            continue
        if _existing_pm(child) is not None:
            continue
        try:
            entries = sum(1 for _ in os.scandir(child))
            newest = max((de.stat().st_mtime for de in os.scandir(child)),
                         default=child.stat().st_mtime)
        except OSError:
            entries, newest = None, None
        out.append({
            "name": nm,
            "dir": str(child),
            "entries": entries,
            "updated_at": (_dt.datetime.fromtimestamp(newest).isoformat(timespec="seconds")
                           if newest else None),
            "name_valid": (bool(_PM_NAME_RE.match(nm))
                           and not any(c in _BAD_NAME_CHARS for c in nm)
                           and nm.casefold().split(".")[0] not in _WIN_RESERVED),
        })
    return out


@router.get("/projects/creatable")
def projects_creatable() -> Dict[str, Any]:
    """一次看两件事：活动根在哪（能新建），以及哪些已有目录还没纳管。

    只读：不创建、不改用户数据。候选只是候选，落盘必须走 plan → create。
    """
    root = _active_root()
    untracked = _scan_untracked(root)
    return {
        "active_root": str(root),
        "active_root_exists": root.is_dir(),
        "templates": PM_TEMPLATE_DIRS,
        "name_pattern": "YYYY-MM_名称",
        "reserved_names": sorted(_WIN_RESERVED),
        "untracked": untracked,
        "untracked_count": len(untracked),
        "note": "归档根只读：新建/纳管只在活动根进行。已有 .pm 的目录不会出现在候选里，也不会被覆盖。",
    }


def _plan_for(body: ProjectPlanBody) -> Dict[str, Any]:
    """算出「将要发生什么」——不落盘。返回值可直接给 UI 做确认页。"""
    mode = str(body.mode or "new").strip().lower()
    if mode not in ("new", "adopt"):
        raise HTTPException(status_code=400, detail="mode 只能是 new 或 adopt")
    name = _validate_project_name(body.name)
    template = str(body.template or "todo").strip()
    if template not in PM_TEMPLATES:
        raise HTTPException(status_code=400, detail=f"template 必须是 {list(PM_TEMPLATES)} 之一")
    if body.start_date and not _as_date(body.start_date):
        raise HTTPException(status_code=400, detail="start_date 必须是 YYYY-MM-DD")
    if body.end_date and not _as_date(body.end_date):
        raise HTTPException(status_code=400, detail="end_date 必须是 YYYY-MM-DD")
    start, end = _as_date(body.start_date), _as_date(body.end_date)
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="start_date 晚于 end_date")

    extra_dirs = [d.strip() for d in (body.dirs or []) if isinstance(d, str) and d.strip()]
    for d in extra_dirs:
        if (d in (".", "..") or "/" in d or "\\" in d
                or any(c in _BAD_NAME_CHARS for c in d) or _sensitive(d)
                or d.casefold() in _WIN_RESERVED):
            raise HTTPException(status_code=400, detail=f"子目录名非法：{d}")

    root = _active_root()
    target = root / name
    exists = target.exists()
    existing = _existing_pm(target) if exists and target.is_dir() else None

    actions: List[str] = []
    blockers: List[str] = []
    if mode == "new":
        if exists:
            blockers.append(f"目录已存在：{target}（新建要求同名不存在；要复用已有目录请改用 adopt 纳管）")
        actions.append(f"创建目录 {target}")
        for d in dict.fromkeys(list(PM_TEMPLATE_DIRS.get(template, [])) + extra_dirs):
            actions.append(f"创建子目录 {target / d}")
        actions.append(f"写入 {target / (name + '.pm')}（schema_version=3，{template} 模板，0 任务）")
    else:
        if not exists:
            blockers.append(f"目标目录不存在：{target}（纳管只能选已有目录）")
        elif _is_link(target):
            blockers.append("目标目录是符号链接/junction，拒绝纳管")
        elif existing is not None:
            blockers.append(f"该目录已纳管：{existing.name}（存在 .pm 时不覆盖、不重复纳管）")
        else:
            actions.append(f"在既有目录中生成 {target / (name + '.pm')}（不改动已有文件）")
            actions.append("写入 README.md（仅当不存在时）")

    pm_plan = _build_pm(name, template, body.description or "", start, end, body.color or "#4f46e5")
    dirs = (list(dict.fromkeys(list(PM_TEMPLATE_DIRS.get(template, [])) + extra_dirs))
            if mode == "new" else [])
    payload = {
        "mode": mode, "name": name, "template": template, "dirs": dirs,
        "description": (body.description or "").strip(),
        "start_date": start, "end_date": end, "color": body.color or "#4f46e5",
    }
    return {
        "ok": not blockers,
        "blockers": blockers,
        "mode": mode,
        "name": name,
        "exists": exists,
        "already_tracked": existing is not None,
        "existing_pm": str(existing) if existing else None,
        "plan_id": _plan_id(payload),
        "active_root": str(root),
        "target_dir": str(target),
        "pm_path": str(target / f"{name}.pm"),
        "actions": actions,
        "template": template,
        "template_dirs": PM_TEMPLATE_DIRS.get(template, []),
        "dirs": dirs,
        "pm_preview": {
            "schema_version": 3,
            "name": name,
            "start_date": start,
            "end_date": end,
            "kanban_columns": STATUS_COLUMNS,
            "readme_file": "README.md",
            "tasks": 0,
            "milestones": 0,
            "changelog": pm_plan["changelog"],
        },
        "payload": payload,
        "note": "这是计划，尚未写任何文件。确认后 POST /projects/create 并带上 plan_id。",
    }


@router.post("/projects/plan")
def projects_plan(body: ProjectPlanBody) -> Dict[str, Any]:
    """预览：返回预期目录、模板、将写入的文件与阻塞原因。**不落盘**。"""
    return _plan_for(body)


@router.post("/projects/create")
def projects_create(body: ProjectPlanBody,
                     plan_id: str = Query(..., min_length=8, max_length=64)) -> Dict[str, Any]:
    """按**已预览的**计划落盘并读回校验。

    两道保险：① 必须带与当前参数完全一致的 ``plan_id``（证明用户看过计划）；
    ② 落盘后再扫一遍，确认 .pm 能解析且下一次 /projects 扫描能看到它。
    """
    plan = _plan_for(body)
    if plan["blockers"]:
        raise HTTPException(status_code=409, detail="；".join(plan["blockers"]))
    if plan_id != plan["plan_id"]:
        raise HTTPException(status_code=409,
                            detail="plan_id 与当前参数不一致（计划已过期或被改动），请重新预览并确认")

    target = Path(plan["target_dir"])
    pm_path = Path(plan["pm_path"])

    if body.mode == "new" and target.exists():
        raise HTTPException(status_code=409, detail=f"目录已存在（并发创建）：{target}，未做任何覆盖")
    if body.mode == "adopt":
        if not target.is_dir():
            raise HTTPException(status_code=409, detail="目标目录已消失，未做任何写入")
        if _existing_pm(target) is not None:
            raise HTTPException(status_code=409, detail="该目录已纳管（存在 .pm），未覆盖")

    created_dirs: List[str] = []
    created_files: List[str] = []
    try:
        if body.mode == "new":
            target.mkdir(parents=False, exist_ok=False)
            created_dirs.append(str(target))
            for d in plan["dirs"]:
                (target / d).mkdir(parents=True, exist_ok=True)
                created_dirs.append(str(target / d))

        pm = _build_pm(plan["name"], plan["template"], plan["payload"]["description"],
                       plan["payload"]["start_date"], plan["payload"]["end_date"],
                       plan["payload"]["color"])
        # 原子写入：同目录临时文件 + os.replace，避免半截 .pm 被扫描到
        fd, tmp = tempfile.mkstemp(dir=str(target), prefix=".pm-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(pm, ensure_ascii=False, indent=1))
            if pm_path.exists():                    # 最后一道：绝不覆盖
                raise HTTPException(status_code=409, detail=f"{pm_path.name} 已存在，未覆盖")
            os.replace(tmp, pm_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        created_files.append(str(pm_path))

        readme = target / "README.md"
        if not readme.exists():
            fd, tmp2 = tempfile.mkstemp(dir=str(target), prefix=".rd-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(pm["template"]["file_contents"]["README.md"])
                if not readme.exists():
                    os.replace(tmp2, readme)
                    created_files.append(str(readme))
            finally:
                if os.path.exists(tmp2):
                    os.unlink(tmp2)
    except HTTPException:
        raise
    except OSError as e:
        raise HTTPException(status_code=500,
                            detail=f"落盘失败：{e}（可能已创建：{created_dirs}）")

    # —— 读回校验：不拿「写入成功」当验收
    verify: Dict[str, Any] = {"pm_parses": False, "visible_in_projects": False, "pm_tasks": None}
    back = _read_pm(pm_path)
    if back is not None:
        verify["pm_parses"] = True
        verify["pm_name"] = _project_meta(back, pm_path).get("name")
        tasks = back.get("tasks")
        verify["pm_tasks"] = len(tasks) if isinstance(tasks, list) else None
        verify["schema_version"] = back.get("schema_version")
    try:
        fresh = [p for p in _scan_projects(include_archived=False) if p["name"] == plan["name"]]
        verify["visible_in_projects"] = bool(fresh)
        if fresh:
            verify["project_id"] = fresh[0]["id"]
            verify["dir_path"] = fresh[0]["dir_path"]
    except Exception as e:                          # 扫描失败不影响落盘结果，但要如实报
        verify["list_error"] = str(e)[:200]

    return {
        "ok": bool(verify["pm_parses"]),
        "mode": body.mode,
        "name": plan["name"],
        "target_dir": str(target),
        "pm_file": str(pm_path),
        "created_dirs": created_dirs,
        "created_files": created_files,
        "verify": verify,
        "verified": bool(verify["pm_parses"] and verify["visible_in_projects"]),
        "note": "已读回校验：pm_parses=磁盘 .pm 能解析；visible_in_projects=下次 /projects "
                "扫描能看到它。ProjectManager 里仍需自行导入该 .pm。",
    }


@router.get("/projects/{project_id}/pm")
def project_pm_summary(project_id: str) -> Dict[str, Any]:
    """该项目目录由哪份 .pm 管理（给文件面板显示归属，避免「这是谁的项目」混淆）。"""
    target = resolve_project(project_id)
    if target is None:
        raise HTTPException(status_code=404, detail="未知项目 id")
    dir_path = target.get("dir_path")
    if not isinstance(dir_path, str) or not dir_path:
        raise HTTPException(status_code=404, detail="该项目没有可读目录")
    base = Path(dir_path)
    pm = _existing_pm(base) if base.is_dir() else None
    return {
        "project": target["id"],
        "dir_path": dir_path,
        "pm_file": pm.name if pm else None,
        "pm_path": str(pm) if pm else None,
        "tracked": pm is not None,
        "archived": bool(target.get("archived")),
        "read_only": bool(target.get("archived")),
    }


# ---------------------------------------------------------------------------
# TextFold：把预览里的 PDF 交给本机折读服务（127.0.0.1:3017）
# 原先独立的「阅读」插件已移除，代理与进程控制留在这里，只服务本插件的预览按钮。
# ---------------------------------------------------------------------------
TEXTFOLD_BASE = os.environ.get("WB_PROJECT_TEXTFOLD_BASE") or "http://127.0.0.1:3017"
TEXTFOLD_PY = os.environ.get("WB_PROJECT_TEXTFOLD_PY") or "D:/DevTool/textfold-venv/Scripts/python.exe"
TEXTFOLD_LOG = os.environ.get("WB_PROJECT_TEXTFOLD_LOG") or "D:/DevTool/textfold-serve.log"
_PDF_MAGIC = b"%PDF-"
_TF_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


_TF_LAST = [time.time()]          # 最近一次代理调用（近似「有人在用」）
_TF_IDLE_MIN = int(os.environ.get("WB_PROJECT_TEXTFOLD_IDLE_MINUTES", "3") or 0)


_TF_ACTIVITY_FILE = os.environ.get("WB_PROJECT_TF_ACTIVITY") or "D:/DevTool/textfold-activity"


def _tf_touch() -> None:
    """记录一次真实使用；同时写文件，让别的进程（gateway 里的代理）也看得到。"""
    _TF_LAST[0] = time.time()
    try:
        with open(_TF_ACTIVITY_FILE, "a"):
            pass
        os.utime(_TF_ACTIVITY_FILE, None)
    except OSError:
        pass


def _tf_established() -> bool:
    """3017 上是否还有连接或最近的请求痕迹（读者页有 30s 心跳）。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, errors="replace", timeout=10).stdout or ""
    except Exception:
        return True                      # 查不到就当作有人在用，宁可不关
    # ESTABLISHED = 此刻挂着连接；TIME_WAIT/CLOSE_WAIT = 最近一两分钟刚发过请求
    marks = ("ESTABLISHED", "TIME_WAIT", "CLOSE_WAIT")
    return any(":3017" in line and any(m in line for m in marks) for line in out.splitlines())


def _tf_jobs_busy() -> bool:
    try:
        jobs = _tf("/api/jobs", timeout=8, touch=False)
    except Exception:
        return True
    return any(j.get("status") in ("running", "queued") for j in (jobs if isinstance(jobs, list) else []))


def _tf_activity_ts() -> float:
    """最近一次「有人真的在用」的时间：接口调用 或 代理被访问（阅读页心跳）。"""
    last = _TF_LAST[0]
    mod = globals().get("_TF_PROXY_MOD")
    if mod is not None:
        try:
            last = max(last, float(mod.LAST_ACTIVITY[0]))
        except Exception:
            pass
    try:                                  # 代理可能在另一个进程里（gateway）写这个文件
        last = max(last, os.path.getmtime(_TF_ACTIVITY_FILE))
    except OSError:
        pass
    return last


def _tf_reaper() -> None:
    """空闲自动停：没人访问、也没有任务在跑，才停。

    注意：不看 netstat。回收器自己每分钟探测 /api/jobs 会产生 TIME_WAIT，
    用它当「有人在用」的判据会让服务永远停不下来（这正是之前停不掉的原因）。
    """
    while True:
        time.sleep(30)
        try:
            with open("D:/DevTool/textfold-reaper.log", "a", encoding="utf-8") as _trace:
                _trace.write("%s alive=%s idle=%ds\n" % (
                    time.strftime("%H:%M:%S"), _tf_alive(), int(time.time() - _tf_activity_ts())))
            if _TF_IDLE_MIN <= 0 or not _tf_alive():
                continue
            if time.time() - _tf_activity_ts() < _TF_IDLE_MIN * 60:
                continue
            if _tf_jobs_busy():
                _tf_touch()              # 有任务在跑，重新计时
                continue
            textfold_stop()
        except Exception:
            continue


def _start_reaper() -> None:
    thread = threading.Thread(target=_tf_reaper, name="textfold-idle-reaper", daemon=True)
    thread.start()


def _tf(path: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 60,
        raw: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
        touch: bool = True):
    if touch:
        _tf_touch()
    body = raw if raw is not None else (json.dumps(payload).encode("utf-8") if payload is not None else None)
    req = urllib.request.Request(TEXTFOLD_BASE + path, data=body,
                                 headers=headers or {"Content-Type": "application/json"})
    with _TF_OPENER.open(req, timeout=timeout) as resp:
        data = resp.read()
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return {"raw": data[:200].decode("utf-8", "replace")}


def _tf_alive() -> bool:
    try:
        _tf("/api/models", timeout=5, touch=False)
        return True
    except Exception:
        return False


@router.get("/textfold/health")
def textfold_health() -> Dict[str, Any]:
    try:
        models = _tf("/api/models", timeout=6)
        return {"running": True, "base": TEXTFOLD_BASE,
                "selected": models.get("selected"), "selectedName": models.get("selectedName")}
    except Exception:
        return {"running": False, "base": TEXTFOLD_BASE}


@router.post("/textfold/upload")
async def textfold_upload(request: Request, name: str = "paper.pdf", lang: str = "zh") -> Dict[str, Any]:
    """浏览器把预览中的 PDF 字节直接转给 TextFold（不落盘、不碰路径白名单）。"""
    data = await request.body()
    if not data.startswith(_PDF_MAGIC):
        raise HTTPException(status_code=400, detail="只支持 PDF")
    if len(data) > 80 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过 80 MB")
    headers = {
        "Content-Type": "application/pdf",
        "X-Name": name.encode("utf-8").decode("latin-1", "ignore") or "paper.pdf",
        "X-Lang": lang,
    }
    try:
        return _tf("/api/files", raw=data, timeout=300, headers=headers)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"TextFold 返回 {exc.code}")
    except Exception:
        # 没在跑就自动拉起，再试一次
        try:
            textfold_start()
            return _tf("/api/files", raw=data, timeout=300, headers=headers)
        except Exception:
            raise HTTPException(status_code=502, detail="TextFold 启动失败：检查 D:\DevTool\textfold-venv")


@router.get("/textfold/jobs/{job_id}")
def textfold_job(job_id: str) -> Dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]{4,40}", job_id or ""):
        raise HTTPException(status_code=400, detail="job id 形状非法")
    try:
        jobs = _tf("/api/jobs", timeout=15)
    except Exception:
        raise HTTPException(status_code=502, detail="TextFold 不可达")
    for job in jobs if isinstance(jobs, list) else []:
        if job.get("id") == job_id:
            return job
    raise HTTPException(status_code=404, detail="没有这个 job")


@router.post("/textfold/start")
def textfold_start() -> Dict[str, Any]:
    _tf_touch()                      # 手动启动也算一次真实使用，别刚起来就被回收
    if _tf_alive():
        return {"started": False, "already": True}
    if not os.path.isfile(TEXTFOLD_PY):
        raise HTTPException(status_code=500, detail=f"找不到 TextFold 的 Python：{TEXTFOLD_PY}")
    os.makedirs(os.path.dirname(TEXTFOLD_LOG) or ".", exist_ok=True)
    log = open(TEXTFOLD_LOG, "ab")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([TEXTFOLD_PY, "-m", "textfold", "serve"], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, creationflags=flags)
    for _ in range(25):
        time.sleep(0.8)
        if _tf_alive():
            return {"started": True}
    return {"started": True, "slow": True}


@router.get("/textfold/proxy")
def textfold_proxy_status() -> Dict[str, Any]:
    """跨设备阅读用的反向代理（3018 → 127.0.0.1:3017）。"""
    binds = [f"{srv.server_address[0]}:{srv.server_address[1]}" for srv in (_TF_PROXY or [])]
    return {"enabled": "1" if binds else "0", "bind": ", ".join(binds)}


@router.post("/textfold/stop")
def textfold_stop() -> Dict[str, Any]:
    if not _tf_alive():
        return {"stopped": False, "already": True}
    port = urllib.parse.urlparse(TEXTFOLD_BASE).port or 3017
    pid = None
    if os.name == "nt":
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, errors="replace").stdout or ""
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.split()[-1]
                break
    if not pid:
        raise HTTPException(status_code=500, detail=f"找不到监听 {port} 的进程")
    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=20)
    for _ in range(10):
        time.sleep(0.5)
        if not _tf_alive():
            return {"stopped": True, "pid": pid}
    return {"stopped": True, "pid": pid, "slow": True}


if _TF_IDLE_MIN > 0:
    _start_reaper()

_TF_PROXY: list = []
_TF_PROXY_MOD = None
if os.environ.get("WB_PROJECT_TF_PROXY", "1") != "0":
    try:
        import importlib.util
        _spec = importlib.util.spec_from_file_location(
            "wb_tf_proxy", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tf_proxy.py"))
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _TF_PROXY = _mod.start()
        _TF_PROXY_MOD = _mod
    except Exception:
        _TF_PROXY = []
