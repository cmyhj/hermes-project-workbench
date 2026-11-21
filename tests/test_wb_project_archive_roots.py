"""wb-project 归档识别与双根扫描测试（**全部用 scratch/temp 数据**）。

设计约束（与实现对齐）：
* 归档 = **物理位置**：项目落在归档根下 → ``archived=true``；``.pm`` 里的
  ``project.archived`` 只作为 ``pm_archived`` 带出，不参与判定。
* Active 根下留着 ``archived=true`` 的 .pm → 照常出现在活动列表里，并带
  ``archive_anomaly`` 说明「未真正迁移」；不静默隐藏、不自动搬。
* 归档根不是单一硬编码：默认 Backup 归档根，课程项目另有课程库根，
  可被 ``project-roots.json`` 扩展。**只有登记过的绝对路径会被扫描**。
* 跨根同名 id 不冲突：活动那份保号（id = 项目名，兼容历史映射与 chat 槽位），
  归档那份改成 ``名字@根slug``；映射键随 id 走，不串。

隔离手段：``WB_PROJECT_ROOTS_CONFIG`` 指向 tmp 里的根配置，``plugin-data`` 用
``HERMES_HOME`` 指向 tmp —— 不读也不写真实项目、真实配置、真实 Zotero 映射。

    venv/Scripts/python.exe -m pytest tests/test_wb_project_archive_roots.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_API = Path(os.environ.get(
    "WB_PROJECT_API",
    REPO_ROOT / "plugin" / "dashboard" / "plugin_api.py",
))


# ---------------------------------------------------------------- 造数据
def _pm(name: str, *, archived: bool = False, tasks: int = 2,
        start: str = "2026-10-01", end: str = "2026-12-31",
        updated: str = "2026-11-01T10:00:00") -> dict:
    return {
        "version": "1.0",
        "schema_version": 3,
        "project": {
            "id": f"pm-{name}",
            "name": name,
            "archived": archived,
            "start_date": start,
            "end_date": end,
            "created_at": "2026-10-01T00:00:00",
            "updated_at": updated,
            "color": "#3366cc",
            "template": "todo",
        },
        "tasks": [
            {
                "id": f"t{i}",
                "title": f"{name} 任务 {i}",
                "status": "进行中" if i == 0 else "已完成",
                "priority": "medium",
                "due_date": f"2026-11-{10 + i:02d}",
                "created_at": "2026-10-02T00:00:00",
                "subtasks": [],
                "tags": [],
                "dependencies": [],
            }
            for i in range(tasks)
        ],
        "milestones": [{"id": "m1", "title": "中期", "date": "2026-11-20"}],
    }


def _project_dir(root: Path, name: str, **kw) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.pm").write_text(
        json.dumps(_pm(name, **kw), ensure_ascii=False), encoding="utf-8")
    return d


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """一套双根世界：Active + 两个归档根（Backup 型 / 课程型）。

    目录布局（刻意覆盖所有关键场景）：
      active/ 2026-10_毕设                 （普通活动项目）
      active/ 2026-10_机器视觉实验         （pm 标 archived=true —— 未真正迁移）
      archive_backup/ 2024-05_国机赛       （已归档，pm 标 false —— 位置说了算）
      archive_backup/ 2026-10_毕设         （与活动项目**同名** → id 冲突）
      archive_course/ 2026-10_机器视觉实验 （课程项目，物理归档）
      archive_course/ 实验1                （不是项目：目录名与 .pm 不同名也无 .pm）
    """
    active = tmp_path / "active"
    backup = tmp_path / "archive_backup"
    course = tmp_path / "archive_course"
    for d in (active, backup, course):
        d.mkdir()

    _project_dir(active, "2026-10_毕设", updated="2026-11-10T09:00:00")
    _project_dir(active, "2026-10_机器视觉实验", archived=True,
                 updated="2026-11-11T09:00:00")
    _project_dir(backup, "2024-05_国机赛-3D视觉", archived=False,
                 updated="2024-06-01T09:00:00")
    _project_dir(backup, "2026-10_毕设", archived=False,
                 updated="2026-09-01T09:00:00", tasks=1)
    _project_dir(course, "2026-10_机器视觉实验", archived=True,
                 updated="2026-11-12T09:00:00", tasks=4)
    (course / "实验1").mkdir()          # 没有 .pm：不该被当成项目

    # 隐藏目录 / 链接目录：都必须被跳过
    hidden = backup / ".git"
    hidden.mkdir()
    (hidden / "x.pm").write_text("{}", encoding="utf-8")

    plugin_home = tmp_path / "hermes_home"
    (plugin_home / "plugin-data" / "wb-project").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(plugin_home))

    roots_cfg = tmp_path / "project-roots.json"
    roots_cfg.write_text(json.dumps({
        "active_root": str(active),
        "archive_roots": [
            {"path": str(backup), "label": "ProjectArchives", "kind": "backup"},
            {"path": str(course), "label": "课程库·机器视觉", "kind": "course"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("WB_PROJECT_ROOTS_CONFIG", str(roots_cfg))

    api = _load_module()
    return {
        "api": api, "active": active, "backup": backup, "course": course,
        "tmp": tmp_path, "roots_cfg": roots_cfg, "home": plugin_home,
    }


def _load_module():
    """每次 fixture 重新 import，避免模块级 PROJECTS_ROOT / store 路径串到别的用例。"""
    name = f"wb_project_api_{os.getpid()}_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_API)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _by_id(payload):
    return {p["id"]: p for p in payload["projects"]}


# ---------------------------------------------------------------- 双根与默认视图
def test_default_view_is_active_only(world):
    data = world["api"].projects(include_archived=False)
    names = sorted(p["name"] for p in data["projects"])
    assert set(names) == {"2026-10_毕设", "2026-10_机器视觉实验"}, names
    assert all(p["location"] == "active" for p in data["projects"])
    assert all(p["archived"] is False for p in data["projects"])
    assert data["active_count"] == 2 and data["archived_count"] == 0


def test_include_archived_reads_every_registered_root(world):
    data = world["api"].projects(include_archived=True)
    assert data["count"] == 5, [(p["name"], p["root_label"]) for p in data["projects"]]
    labels = {p["root_label"] for p in data["projects"] if p["archived"]}
    assert labels == {"ProjectArchives", "课程库·机器视觉"}, labels
    assert data["archived_count"] == 3


def test_course_project_is_visible_from_its_own_root(world):
    """课程归档根不是硬编码的 Backup —— 它自己那条根也要能读出项目。"""
    data = world["api"].projects(include_archived=True)
    course = [p for p in data["projects"] if p["root_kind"] == "course"]
    assert len(course) == 1
    p = course[0]
    assert p["name"] == "2026-10_机器视觉实验"
    assert p["archived"] is True
    assert Path(p["dir_path"]) == world["course"] / "2026-10_机器视觉实验"
    assert p["counts"]["total"] == 4


def test_roots_endpoint_lists_all_roots_without_accepting_paths(world):
    r = world["api"].roots()
    paths = {x["path"] for x in r["roots"]}
    assert str(world["active"]) in paths
    assert str(world["backup"]) in paths
    assert str(world["course"]) in paths
    assert r["active_count"] == 1 and r["archive_count"] == 2
    # 只读端点：没有任何接受 path 参数的入参
    import inspect
    sig = inspect.signature(world["api"].roots)
    assert list(sig.parameters) == []


def test_missing_archive_root_is_reported_not_hidden(world, tmp_path):
    """登记了但盘不在的根：不该让整个列表崩，也不该假装它不存在。"""
    cfg = json.loads(world["roots_cfg"].read_text(encoding="utf-8"))
    cfg["archive_roots"].append({"path": str(tmp_path / "nope"), "label": "离线盘"})
    world["roots_cfg"].write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    api = _load_module()
    r = api.roots()
    offline = [x for x in r["roots"] if x["label"] == "离线盘"]
    assert offline, "已登记但不存在的根必须出现在 /roots 里"
    assert offline[0]["exists"] is False
    # 扫描仍然正常
    assert api.projects(include_archived=True)["count"] == 5


# ---------------------------------------------------------------- 归档判定
def test_archived_comes_from_physical_location_not_pm_flag(world):
    """归档根里 pm 标 archived=false 也要算归档；Active 里标 true 也不算。"""
    data = world["api"].projects(include_archived=True)
    ids = _by_id(data)

    # 归档根 + pm 标 false → 归档（位置说了算）
    gunji = [p for p in data["projects"] if "国机赛" in p["name"]][0]
    assert gunji["archived"] is True
    assert gunji["pm_archived"] is False

    # Active 根 + pm 标 true → 活动，并暴露异常
    anomaly = ids["2026-10_机器视觉实验"]
    assert anomaly["archived"] is False
    assert anomaly["pm_archived"] is True
    assert anomaly["archive_anomaly"], "Active 下 pm 标 archived 必须暴露，不能静默"


def test_active_archived_flag_is_never_silently_hidden(world):
    data = world["api"].projects(include_archived=False)
    names = [p["name"] for p in data["projects"]]
    assert "2026-10_机器视觉实验" in names, "标了 archived 但没搬走的项目不能被藏起来"
    assert [a["name"] for a in data["anomalies"]] == ["2026-10_机器视觉实验"]
    detail = data["anomalies"][0]["detail"]
    assert "未真正迁移" in detail
    # 异常说明里不能声称已经搬了
    assert "已迁移" not in detail


def test_archive_pm_flag_is_not_rewritten(world):
    """读取不改用户文件：扫一遍之后归档 .pm 里的 archived 标记保持原样。"""
    before = {
        p: p.read_bytes()
        for p in world["tmp"].rglob("*.pm")
    }
    world["api"].projects(include_archived=True)
    world["api"].projects(include_archived=False)
    after = {p: p.read_bytes() for p in world["tmp"].rglob("*.pm")}
    assert before == after, "只读扫描不得改动任何 .pm"


# ---------------------------------------------------------------- 同名 / id
def test_same_name_active_and_archive_get_distinct_ids(world):
    data = world["api"].projects(include_archived=True)
    same = [p for p in data["projects"] if p["name"] == "2026-10_毕设"]
    assert len(same) == 2, "同名项目必须都读出来"
    ids = {p["id"] for p in same}
    assert len(ids) == 2, f"同名项目 id 撞了: {ids}"

    active = [p for p in same if p["location"] == "active"][0]
    archived = [p for p in same if p["location"] == "archive"][0]
    # 活动那份保号 = 项目名（历史映射 / chat 槽位兼容）
    assert active["id"] == "2026-10_毕设"
    assert archived["id"] != "2026-10_毕设"
    assert archived["id"].startswith("2026-10_毕设@")
    assert archived["id_conflict_with_active"] is True
    # dir_path 各指各的，不串
    assert Path(active["dir_path"]) == world["active"] / "2026-10_毕设"
    assert Path(archived["dir_path"]) == world["backup"] / "2026-10_毕设"


def test_unique_names_keep_legacy_id_equals_name(world):
    """没撞车时 id 仍等于项目名 —— 老 Zotero 映射、老 chat 槽位继续有效。"""
    dup_names = {"2026-10_毕设", "2026-10_机器视觉实验"}   # 这两组刻意同名
    data = world["api"].projects(include_archived=True)
    checked = 0
    for p in data["projects"]:
        if p["name"] in dup_names:
            continue
        checked += 1
        assert p["id"] == p["name"], p
        assert p["legacy_id"] == p["name"]
    assert checked >= 1


def test_resolve_project_accepts_id_and_unique_name_only(world):
    api = world["api"]
    # 按 id（活动那份持有）
    active = [p for p in api.projects(include_archived=True)["projects"]
              if p["name"] == "2026-10_机器视觉实验" and p["location"] == "active"][0]
    assert api.resolve_project(active["id"])["location"] == "active"
    # 同名项目：名字键归活动那份（保号），不会随机挑到归档那份
    dup = [p for p in api.projects(include_archived=True)["projects"]
           if p["name"] == "2026-10_毕设"]
    resolved = api.resolve_project("2026-10_毕设")
    assert resolved is not None and resolved["location"] == "active"
    assert {p["location"] for p in dup} == {"active", "archive"}
    # 路径形状一律不接受
    for bad in ["../active", str(world["active"] / "2026-10_毕设"),
                "<abs>/x/y.pm", "", "   "]:
        assert api.resolve_project(bad) is None, bad


def test_archived_duplicate_is_reachable_by_its_own_id(world):
    api = world["api"]
    data = api.projects(include_archived=True)
    archived = [p for p in data["projects"]
                if p["name"] == "2026-10_毕设" and p["location"] == "archive"][0]
    got = api.resolve_project(archived["id"])
    assert got is not None
    assert Path(got["dir_path"]) == world["backup"] / "2026-10_毕设"


# ---------------------------------------------------------------- 聊天 cwd
def test_chat_dir_path_is_the_real_directory_for_each_root(world):
    """模块级对话的 cwd 取项目实际所在目录（包括归档根），不是活动根下拼出来的。"""
    data = world["api"].projects(include_archived=True)
    for p in data["projects"]:
        d = Path(p["dir_path"])
        assert d.is_dir(), p["dir_path"]
        assert (d / p["pm_file"]).is_file()
        # 必须在它自己那条根下面
        assert Path(p["root"]) == d.parent, (p["root"], p["dir_path"])
    # 特别是课程归档那份
    course = [p for p in data["projects"] if p["root_kind"] == "course"][0]
    assert course["dir_path"] == str(world["course"] / "2026-10_机器视觉实验")


# ---------------------------------------------------------------- 扫描卫生
def test_hidden_and_non_project_dirs_are_ignored(world):
    data = world["api"].projects(include_archived=True)
    for p in data["projects"]:
        assert not Path(p["dir_path"]).name.startswith("."), p["dir_path"]
        assert Path(p["dir_path"]).name != "实验1"
    assert not any(p["name"] == "x" for p in data["projects"])


def test_deep_nested_pm_is_not_scanned(world):
    """只认一级子目录：课程目录里的 实验1/实验2 深层即使有 .pm 也不算项目。"""
    deep = world["course"] / "实验1" / "sub"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "hidden.pm").write_text(json.dumps(_pm("深层项目"), ensure_ascii=False),
                                    encoding="utf-8")
    api = _load_module()
    names = [p["name"] for p in api.projects(include_archived=True)["projects"]]
    assert "深层项目" not in names


def test_symlink_dir_is_skipped(world):
    """链接/junction 目录不跟随 —— 否则归档根可以指到任意位置。"""
    target = world["tmp"] / "outside"
    target.mkdir()
    _project_dir(target, "逃逸项目")
    link = world["backup"] / "linked"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("当前环境不允许创建符号链接（未提权）")

    api = _load_module()
    names = [p["name"] for p in api.projects(include_archived=True)["projects"]]
    assert "逃逸项目" not in names


def test_scan_dedupes_when_roots_overlap(world, tmp_path):
    """一个根套在另一个根里时，同一份 .pm 只出现一次。"""
    cfg = json.loads(world["roots_cfg"].read_text(encoding="utf-8"))
    cfg["archive_roots"].append({"path": str(world["backup"] / "2024-05_国机赛-3D视觉"),
                                "label": "重复根"})
    world["roots_cfg"].write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    api = _load_module()
    data = api.projects(include_archived=True)
    gunji = [p for p in data["projects"] if "国机赛" in p["name"]]
    assert len(gunji) == 1, [p["dir_path"] for p in gunji]


def test_invalid_root_entries_are_dropped(world):
    """相对路径 / 带 .. / 非目录的根条目直接丢弃，不猜也不炸。"""
    cfg = json.loads(world["roots_cfg"].read_text(encoding="utf-8"))
    some_pm = world["active"] / "2026-10_毕设" / "2026-10_毕设.pm"
    cfg["archive_roots"].extend([
        {"path": "relative/path"},
        {"path": str(world["tmp"] / ".." / "escape")},
        {"path": str(some_pm)},                 # 是文件不是目录
        {"path": ""},
        "not-a-dict-at-all-but-a-string",
    ])
    world["roots_cfg"].write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    api = _load_module()
    r = api.roots()
    paths = [x["path"] for x in r["roots"]]
    assert not any("relative" in p for p in paths)
    assert not any(p.endswith(".pm") for p in paths)
    assert api.projects(include_archived=True)["count"] == 5


# ---------------------------------------------------------------- 映射不串
def _stub_zotero(monkeypatch, api):
    cols = [{"key": "AAAAAAAA", "name": "集合A", "parent": None, "num_items": 1,
             "num_collections": 0},
            {"key": "BBBBBBBB", "name": "集合B", "parent": None, "num_items": 1,
             "num_collections": 0}]
    monkeypatch.setattr(api, "_zotero_collections", lambda: [dict(c) for c in cols])
    monkeypatch.setattr(api, "_collection_items",
                        lambda k: {"collection": k, "total_results": 0,
                                   "returned": 0, "items": []})


def test_mapping_key_does_not_cross_between_same_name_projects(world, monkeypatch):
    """给归档那份存集合，不能改到活动那份的映射（也不能反过来）。"""
    api = world["api"]
    _stub_zotero(monkeypatch, api)

    data = api.projects(include_archived=True)
    same = [p for p in data["projects"] if p["name"] == "2026-10_毕设"]
    active_id = [p["id"] for p in same if p["location"] == "active"][0]
    archived_id = [p["id"] for p in same if p["location"] == "archive"][0]

    api.zotero_set_mapping(project=archived_id, collection="BBBBBBBB")
    link_active = api.zotero_link(project=active_id)
    link_archived = api.zotero_link(project=archived_id)
    assert link_archived["matched"] is True
    assert link_archived["source"] == "mapping"
    assert link_archived["collection"]["key"] == "BBBBBBBB"
    # 活动那份不能被归档的映射污染
    assert link_active.get("source") != "mapping"

    api.zotero_set_mapping(project=archived_id, collection=None)
    assert "mapping" not in {api.zotero_link(project=archived_id)["source"]}


def test_legacy_name_mapping_still_works_for_unique_project(world, monkeypatch):
    """老映射文件用项目名做键：名字唯一时仍要命中，不能因为改了 id 就丢。"""
    api = world["api"]
    _stub_zotero(monkeypatch, api)
    api._save_mappings({"2024-05_国机赛-3D视觉": "AAAAAAAA"})

    link = api.zotero_link(project="2024-05_国机赛-3D视觉")
    assert link["matched"] is True
    assert link["source"] == "mapping"
    assert link["collection"]["key"] == "AAAAAAAA"


def test_legacy_name_mapping_is_ignored_when_name_is_ambiguous(world, monkeypatch):
    """名字撞车时，名字键只能归活动那份；归档那份不许靠名字键串过去。"""
    api = world["api"]
    _stub_zotero(monkeypatch, api)
    api._save_mappings({"2026-10_毕设": "AAAAAAAA"})

    data = api.projects(include_archived=True)
    archived = [p for p in data["projects"]
                if p["name"] == "2026-10_毕设" and p["location"] == "archive"][0]
    link = api.zotero_link(project=archived["id"])
    assert link.get("source") != "mapping", link


def test_unknown_project_id_still_404(world, monkeypatch):
    api = world["api"]
    _stub_zotero(monkeypatch, api)
    for bad in ["../../etc/passwd", str(world["active"]), "不存在的项目"]:
        with pytest.raises(HTTPException) as exc:
            api.zotero_link(project=bad)
        assert exc.value.status_code == 404


def test_ids_are_stable_across_include_archived_toggles(world, monkeypatch):
    """勾/不勾「隐藏已归档」不能让 id 漂移 —— 否则存好的映射下次就对不上了。"""
    api = world["api"]
    _stub_zotero(monkeypatch, api)
    api.zotero_set_mapping(project="2024-05_国机赛-3D视觉", collection="AAAAAAAA")

    for _ in range(3):
        api.projects(include_archived=False)
        full = api.projects(include_archived=True)
        ids_now = {p["name"] + "|" + p["location"]: p["id"] for p in full["projects"]}
        if _ == 0:
            first = ids_now
        else:
            assert ids_now == first, "id 在多次扫描间漂移了"

    link = api.zotero_link(project="2024-05_国机赛-3D视觉")
    assert link["matched"] is True and link["source"] == "mapping"


def test_plugin_data_stays_plugin_scoped(world):
    api = world["api"]
    path = str(api._store_file()).replace("\\", "/")
    assert "plugin-data" in path and "wb-project" in path
    assert str(world["home"]).replace("\\", "/") in path
    assert str(api._roots_file()).endswith("project-roots.json")


# ---------------------------------------------------------------- 相对时间不变
def test_counts_and_milestones_survive_multi_root(world):
    for p in world["api"].projects(include_archived=True)["projects"]:
        c = p["counts"]
        assert c["done"] + c["doing"] + c["todo"] == c["total"] == len(p["tasks"])
        assert p["milestones"], p["name"]
        for t in p["tasks"]:
            assert t["start_source"] in {"tracked", "created", "project", "unknown"}
