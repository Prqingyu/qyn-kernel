#!/usr/bin/env python3
"""smoke_qyn.py — 确定性内核 CLI（qyn.py）端到端冒烟测试

为什么要有它：
  迁移到 DSH 后，`qyn.py` 是「确定性 + 结构校验」的唯一实现，
  skill 里的语义层完全依赖它的行为。项目此前**没有任何测试**。

2026-10 架构（三个文件）：
  rules.json     规则
  schedule.json  周期层：作息 + **带日期的**每一节课 + 作业/考试周期
  data.json      LIST：物化后的待办，规划与提醒的唯一权威

★ 本套测试贯彻两条硬约束：
  1. **不用周计算时间** —— 夹具里每个日期都是**具体的**（相对今天生成），
     断言也只比较具体日期；任何"第N周"推演都会在这里露馅。
  2. 用例**自己定义输入**（固定夹具），不复制仓库里正在使用的 data.json ——
     否则用例会随真实使用而漂移（实测踩过：跑一次 sync 就有三个用例失效）。

**全程在临时目录里跑，绝不触碰真实 data/*.json。**

用法：
    python scripts/smoke_qyn.py        # Windows
    python3 scripts/smoke_qyn.py       # Linux
退出码：0 = 全部通过，1 = 有失败项。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)          # AGENT/
REPO = os.path.dirname(AGENT)          # 仓库根

# Windows 控制台是 GBK，非 ASCII 输出会直接崩（项目踩过）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _d(offset: int) -> str:
    """相对今天偏移 N 天的日期。夹具用它生成 —— 这样换一天跑也不会红。"""
    return (datetime.now().date() + timedelta(days=offset)).isoformat()


def _due_spec(minutes: int) -> tuple[str, str]:
    """相对**现在**偏移 N 分钟的 (日期, HH:MM)。

    用来精确测"过点"边界 —— 只用 `_d(0)` + 写死时刻会在凌晨跨天而误判
    （例如 00:30 用"2 小时前"会落到昨天）。
    """
    t = datetime.now() + timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%d"), t.strftime("%H:%M")


# ────────────────────────── 固定夹具 ──────────────────────────

FIXTURE_RULES = {
    "planning_rules": [
        {"id": "rule-001", "kind": "preference", "condition": "中午没课",
         "body": "优先安排示例课程作业", "source": "", "created_at": "2026-08-06",
         "scope": {"task": "示例课程作业"}},          # 故意写错名 → 触发 rule_scope_mismatch
    ],
}

FIXTURE_SCHEDULE = {
    "semester": "2026-1",
    "periods": {
        "1": "08:00-08:45", "2": "08:55-09:40", "3": "10:10-10:55", "4": "11:05-11:50",
        "5": "14:20-15:05", "6": "15:15-16:00", "7": "16:30-17:15", "8": "17:25-18:10",
    },
    "dated_sessions": [
        {"date": _d(1), "periods": "1-2", "course": "示例课程E", "location": "示例教室X1"},
        {"date": _d(2), "periods": "3-4", "course": "示例课程A", "location": "示例教室X1"},
        {"date": _d(9), "periods": "3-4", "course": "示例课程A", "location": "示例教室X1"},
        {"date": _d(-3), "periods": "5-6", "course": "示例课程D", "location": "示例教室X1"},
        {"date": _d(4), "periods": "5-8", "course": "示例课程F",
         "location": "示例教室X1", "kind": "moved"},
        {"date": _d(5), "periods": "1-2", "course": "停掉的课", "kind": "cancelled"},
    ],
    "periodic_tasks": [
        {"task": "示例课程A作业", "follow_course": "示例课程A",
         "due_time": "23:59", "priority": "medium"},
        {"task": "锚不到作业", "follow_course": "课表里没有这门课"},
    ],
}

FIXTURE_DATA = {
    "registry": [
        {"task": "结课论文", "date": _d(-5), "priority": "high", "status": "pending",
         "detail": {"hints": "热条目，滚出但还没搬走"}},
        # _d(-10) 超出 LIST_KEEP_DAYS，sync 会把它搬进冷存储 → 用来测"归档后怎么读"
        {"task": "实验报告", "date": _d(-10), "priority": "medium", "status": "completed",
         "detail": {"hints": "已归档条目的完整信息"}},
    ],
    "meta": {"last_updated": ""},
}


# ────────────────────────── 脚手架 ──────────────────────────

def _setup(work: str) -> str:
    """把 CLI 运行所需的最小集合复制进临时目录，并写入**固定夹具**。"""
    for item in ("core", "scripts"):
        src = os.path.join(AGENT, item)
        if not os.path.isdir(src):
            raise SystemExit(f"缺少目录: {src}")
        shutil.copytree(src, os.path.join(work, item),
                        ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(os.path.join(AGENT, "qyn.py"), os.path.join(work, "qyn.py"))
    # ★ 规划快照搬到 plans.json 之后，不再需要"丢掉仓库里的 agent.db"那一步：
    #   夹具不复制 data/，所以 last_plan 一开局就是 None。
    # ★ 用夹具覆盖数据，**不复制**仓库里那份正在被使用的 data.json。
    # 用例自己定义输入，才能长期稳定；否则会随用户真实使用而漂移。
    data_dir = os.path.join(work, "data")
    os.makedirs(data_dir, exist_ok=True)
    for name, payload in (("rules.json", FIXTURE_RULES),
                          ("schedule.json", FIXTURE_SCHEDULE),
                          ("data.json", FIXTURE_DATA)):
        with open(os.path.join(data_dir, name), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    return os.path.join(work, "qyn.py")



def _archive_all(work: str) -> dict:
    """把 `data/archive/*.json`（按月分文件）合并成一份，方便断言。"""
    out = {"registry": [], "sessions": []}
    d = os.path.join(work, "data", "archive")
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith(".json"):
                one = json.load(open(os.path.join(d, f), encoding="utf-8"))
                out["registry"] += one["registry"]
                out["sessions"] += one["sessions"]
    return out


def _fresh():
    """给需要「干净数据」的用例一个独立工作区。"""
    work = tempfile.mkdtemp(prefix="qyn-smoke-")
    return _setup(work), work


WD_CN = ["一", "二", "三", "四", "五", "六", "日"]


def _label(d: str) -> str:
    """与内核 `_d_label` 同一形状（`10/16 周五`）—— 用来断言"那天有没有被展开"。"""
    return "%d/%d 周%s" % (int(d[5:7]), int(d[8:10]),
                           WD_CN[datetime.strptime(d, "%Y-%m-%d").weekday()])


def _kernel_call(work: str, expr: str, now: tuple | None = None) -> dict:
    """在**工作区副本**里 import qyn 并求值 `expr`（结果按 JSON 返回）。

    ★ 为什么要进程内：`passed` 依赖 `datetime.now()`，命令行没法把时钟拨到 23:30。
      这里把 `qyn.datetime` 换成固定在 `now` 的假类，调的是**真实函数** ——
      而不是把判据再抄一遍（抄一遍的断言守不住任何东西）。
    """
    script = ["import argparse, json, sys",
              "sys.path.insert(0, %r)" % work,
              "from datetime import datetime as _dt",
              "import qyn"]
    if now:
        script += ["class _Now(qyn.datetime):",
                   "    @classmethod",
                   "    def now(cls, tz=None):",
                   "        return qyn.datetime(*%r)" % (tuple(now),),
                   "qyn.datetime = _Now"]
    script.append("print(json.dumps(%s, ensure_ascii=False))" % expr)
    proc = subprocess.run([sys.executable, "-c", "\n".join(script)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise AssertionError("内核探针失败: %s" % ((proc.stderr or proc.stdout) or "")[:400])
    return json.loads(proc.stdout)


def _run(cli: str, *args: str, cwd: str = ""):
    proc = subprocess.run([sys.executable, cli, *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=cwd or None)
    return proc.returncode, proc.stdout, proc.stderr


def _json(cli: str, *args: str, expect_ok: bool = True):
    code, out, err = _run(cli, *args)
    if expect_ok and code != 0:
        raise AssertionError(f"{args} 退出码 {code}；stderr={err.strip()[:200]}")
    try:
        return code, json.loads(out)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{args} 输出不是 JSON: {exc}; raw={out[:200]!r}")


def _write(path: str, payload) -> None:
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(payload, str):
            f.write(payload)
        else:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def _hot(work: str, name: str):
    """直接读**热文件** —— 专门用来核对"写操作到底落盘了什么"。

    读取视图（context / sessions）是有意收窄过的（课次与注册表分家、只给未过期的），
    所以"校验落盘"必须直读文件，不能用视图当证据。
    """
    with open(os.path.join(work, "data", name), encoding="utf-8") as f:
        return json.load(f)


def _apply(cli: str, work: str, payload):
    """走**唯一写入口** `apply`（set-deadline / set-schedule / set-rule 等子命令已删）。

    为什么收敛成一个入口：那些子命令与 apply 能力完全重合，留着就是"两个写入口"，
    而技能教模型用的是 apply —— 两套入口只会让模型和文档各自漂移。
    """
    p = os.path.join(work, "_apply.json")
    _write(p, payload)
    return _json(cli, "apply", "--file", p)


def _apply_err(cli: str, work: str, payload):
    """同上，但预期失败（用来验证"被拒绝的写零副作用"）。"""
    p = os.path.join(work, "_apply.json")
    _write(p, payload)
    return _run(cli, "apply", "--file", p)


# ────────────────────────── 用例 ──────────────────────────

def case_readonly_no_side_effect(cli: str, work: str) -> str:
    """只读命令不得改动**三个**文件里任何一个的字节，也不得凭空建出 plans.json。

    ★ `brief` / `wall` 也是只读命令（它们读规划快照）：一起守 ——
      快照读写分离是"读命令零副作用"这条硬性质的边界，漏一个就等于没守。
    """
    paths = [os.path.join(work, "data", n)
             for n in ("rules.json", "schedule.json", "data.json")]
    before = [open(p, "rb").read() for p in paths]
    for args in (("context",), ("sessions", "--days", "14"), ("validate",),
                 ("history", "--days", "7"), ("anomalies",),
                 ("brief", "--kind", "plan"), ("wall",)):
        _json(cli, *args)
    after = [open(p, "rb").read() for p in paths]
    assert before == after, "只读命令改动了数据文件"
    plans = os.path.join(work, "data", "plans.json")
    assert not os.path.exists(plans), "只读命令把 plans.json 建出来了（读命令有副作用）"
    return ("context/sessions/validate/history/anomalies/brief/wall 均未改动三个文件，"
            "也未建出 plans.json")


def case_deadline_create_then_update(cli: str, work: str) -> str:
    """注册表条目的新增与**整条替换**（走唯一写入口 apply）。"""
    fresh_cli, fresh_work = _fresh()
    try:
        _, r = _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "史纲论文", "date": "2026-12-01", "due_time": "23:59",
             "detail": {"effort": "两三天"}}]}})
        assert r["report"]["list"]["added"] == 1, r
        # ★ 只改注册表时，report **必须非空** —— 曾经 list 的报告单独放在顶层
        #   list 键里，而工具描述写着"必须看 report"，模型照做会看到空 dict。
        assert set(r["report"]) & {"list"}, r["report"]
        _, r2 = _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "史纲论文", "date": "2026-12-03",
             "detail": {"hints": "先写提纲"}}]}})
        assert r2["report"]["list"]["updated"] == 1 and r2["report"]["list"]["added"] == 0, r2
        hit = next(x for x in _hot(fresh_work, "data.json")["registry"]
                   if x["task"] == "史纲论文")
        assert hit["date"] == "2026-12-03", hit
        # ★ upsert 是**整条替换**：旧的 detail 不残留（否则会出现"看起来对但其实过期"的条目）
        assert hit["detail"] == {"hints": "先写提纲"}, hit
        return "新建→整条替换生效，旧 detail 不残留"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_reject_invalid_date(cli: str, work: str) -> str:
    """非法日期格式必须被拒（且不落盘）。"""
    fresh_cli, fresh_work = _fresh()
    try:
        code, _, err = _apply_err(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "坏日期", "date": "2026/12/01"}]}})
        assert code != 0, "2026/12/01 竟然被接受"
        assert "date" in err, err[:200]
        names = [x["task"] for x in _hot(fresh_work, "data.json")["registry"]]
        assert "坏日期" not in names, "被拒绝的写入竟然落了盘"
        return "2026/12/01 被拒绝，且未落盘"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_conservative_match(cli: str, work: str) -> str:
    """名称不完全一致时拒绝，并返回已知任务供回询。"""
    _, r = _json(cli, "sync")                      # 先让 LIST 有内容
    code, _, err = _run(cli, "mark", "--task", "论文", "--status", "done")
    assert code != 0, "名称不一致竟然标记成功（破坏保守原则）"
    payload = json.loads(err)
    assert payload["known_tasks"], "拒绝时未给出已知任务列表"
    assert "结课论文" in payload["known_tasks"], payload["known_tasks"]
    return "名称不一致被拒，且返回 known_tasks 供回询"


def case_sync_materializes(cli: str, work: str) -> str:
    """★ 核心：sync 把「带日期的课表」与「锚定周期任务」物化进 LIST。

    全部日期来自**夹具里写死的具体日期**，没有任何周推算。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _, r = _json(fresh_cli, "sync")
        sess = [c for c in r["created"] if c["kind"] == "session"]
        per = [c for c in r["created"] if c["kind"] == "periodic"]

        # ① 上课条目：5 节（停课那节不登记）
        assert len(sess) == 5, f"应登记 5 节课，实际 {len(sess)}: {sess}"
        assert not any(c["date"] == _d(5) for c in sess), "停课（cancelled）不该被登记"
        # ② **物化不改状态**（架构铁律：数据工具层不做语义判断）
        #    原先"已过去的课直接记 completed"是代码替时间宣布完成 ✗
        #    现在一律 pending —— 过期与否由**视图**算（`_active` 按时刻过滤）
        past = next(c for c in sess if c["date"] == _d(-3))
        assert past["status"] == "pending", f"★ 又在代码里替时间宣布完成了：{past}"
        # ③ 时间由作息表推出（跨节取首节开始、末节结束）
        lab = next(c for c in sess if c["task"] == "示例课程F")
        assert lab["time"] == "14:20-18:10", lab
        assert lab["location"] == "示例教室X1", lab

        # ④ 周期任务锚到那门课的**实际课次日期**
        assert [c["task"] for c in per] == ["示例课程A作业"], per
        assert per[0]["anchored_to"] == _d(2), per[0]
        assert per[0]["date"] == _d(2), per[0]

        # ⑤ 锚不到的课的任务**不猜**，如实报出来
        assert any(u["task"] == "锚不到作业" for u in r["unanchored"]), r["unanchored"]

        # ⑥ 幂等
        _, r2 = _json(fresh_cli, "sync")
        assert r2["created"] == [], f"第二次 sync 不该再登记：{r2['created']}"

        # ⑦ 改单次（调休）：只动 date，**身份 anchored_to 不变**
        _, ctx = _json(fresh_cli, "context")
        before = next(d for d in ctx["active"] if d.get("occurrence_of"))
        _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "示例课程A作业", "date": _d(6)}]}})
        _, ctx2 = _json(fresh_cli, "context")
        after = next(d for d in ctx2["active"] if d.get("occurrence_of"))
        assert after["date"] == _d(6), after
        assert after["anchored_to"] == before["anchored_to"] == _d(2), after

        # ⑧ 改过之后 sync 不能再补一条（身份认得出这天的已登记过）
        _, r3 = _json(fresh_cli, "sync")
        assert r3["created"] == [], f"改期后重复登记了：{r3['created']}"

        # ⑨ 取消这一件 → sync 登记**下一节**（第二次课 _d(9)）
        _json(fresh_cli, "mark", "--task", "示例课程A作业", "--status", "cancelled")
        _, r4 = _json(fresh_cli, "sync")
        nxt = [c for c in r4["created"] if c["task"] == "示例课程A作业"]
        assert nxt and nxt[0]["anchored_to"] == _d(9), r4["created"]
        return (f"登记 4 节课（过去=completed、停课跳过、跨节 {lab['time']}）；"
                f"作业锚到 {_d(2)}；改期身份不变；取消后锚到 {_d(9)}")
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_sessions_view(cli: str, work: str) -> str:
    """sessions 视图按**实际日期**取窗口。"""
    fresh_cli, fresh_work = _fresh()
    try:
        _, s = _json(fresh_cli, "sessions", "--days", "3")
        got = {x["date"] for x in s["sessions"]}
        assert got == {_d(1), _d(2)}, f"3 天窗口应只含 {_d(1)}/{_d(2)}，实际 {sorted(got)}"
        _, s2 = _json(fresh_cli, "sessions", "--days", "1")
        assert {x["date"] for x in s2["sessions"]} == {_d(1)}, s2["sessions"]
        return "days=3/1 的窗口边界与日期一致（与周无关）"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_schedule_incremental(cli: str, work: str) -> str:
    """课表**增量**改：只写要改的那节；★ 覆盖/漏删都**如实报**（守卫不丢，只是不再拦死）。

    这是把 set-schedule 的 add/modify/delete 合并进 apply 之后**不能丢**的东西：
    旧 add 在"同一格换成另一门课"时会直接报错、旧 delete 在"没删到"时也报错。
    合并后若变成静默 no-op，模型就再也发现不了自己写错了课程名或身份。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        ident = f"{_d(7)}|9-10"
        _, r = _apply(fresh_cli, fresh_work, {"schedule": {"sessions": {"upsert": [
            {"date": _d(7), "periods": "9-10", "course": "示例课程I",
             "location": "示例教室X1"}]}}})
        assert r["report"]["schedule"]["dated_sessions"]["added"] == 1, r["report"]
        assert not r.get("check_this"), r

        # 同一格覆盖成**另一门课** → 不拦，但 report 必须点名
        _, r2 = _apply(fresh_cli, fresh_work, {"schedule": {"sessions": {"upsert": [
            {"date": _d(7), "periods": "9-10", "course": "撞车课", "location": "X"}]}}})
        rep = r2["report"]["schedule"]["dated_sessions"]
        assert rep["updated"] == 1 and rep["added"] == 0, rep
        assert rep["replaced"] and rep["replaced"][0]["was"] == "示例课程I", rep
        assert r2.get("check_this"), "覆盖了别的课却没提示"

        # 改地点：只给身份 + 要改的字段（course 必填由 schema 保证，不会丢课名）
        _, r3 = _apply(fresh_cli, fresh_work, {"schedule": {"sessions": {"upsert": [
            {"date": _d(7), "periods": "9-10", "course": "撞车课", "location": "示例教室X1"}]}}})
        assert r3["report"]["schedule"]["dated_sessions"]["updated"] == 1, r3["report"]
        sess = next(s for s in _hot(fresh_work, "schedule.json")["dated_sessions"]
                    if s["date"] == _d(7) and s["periods"] == "9-10")
        assert sess["location"] == "示例教室X1" and sess["course"] == "撞车课", sess

        # 删
        _, r4 = _apply(fresh_cli, fresh_work,
                       {"schedule": {"sessions": {"remove": [ident]}}})
        assert r4["report"]["schedule"]["dated_sessions"]["removed"] == 1, r4["report"]

        # 删不存在的 → 不拦，但 report.missing 必须点名
        _, r5 = _apply(fresh_cli, fresh_work,
                       {"schedule": {"sessions": {"remove": [ident]}}})
        assert r5["report"]["schedule"]["dated_sessions"]["missing"] == [ident], r5["report"]
        assert r5.get("check_this"), "漏删却没提示"
        return "增量增改删生效；覆盖别课 / 漏删 都在 report 里点名（守卫没丢，只是不拦死）"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_schedule_replace(cli: str, work: str) -> str:
    """整批导入课表（多模态录入走这条路）：原子，非法整份拒绝。"""
    fresh_cli, fresh_work = _fresh()
    try:
        p = os.path.join(fresh_work, "sched.json")
        _write(p, {"schedule": {"replace_future": True, "dated_sessions": [
            {"date": _d(10), "periods": "1-2", "course": "示例课程H",
             "location": "示例教室X1"},
            {"date": _d(10), "periods": "3-4", "course": "示例课程A",
             "location": "示例教室X1"}]}})
        _, r = _json(fresh_cli, "apply", "--file", p)
        assert r["written"] == ["schedule.json"], r
        assert len(_hot(fresh_work, "schedule.json")["dated_sessions"]) == 2

        # 非法整批：必须整份拒绝，不能把原来那两条洗掉
        for bad, why in (
            ({"schedule": {"replace_future": True, "dated_sessions": [{"date": _d(10), "periods": "1-2"}]}}, "缺 course"),
            ({"schedule": {"replace_future": True, "dated_sessions": [{"date": "第6周", "periods": "1-2",
                                               "course": "X"}]}}, "★ 非具体日期（不许用周）"),
            ({"nope": 1}, "不认识的节"),
            ({"schedule": {"sessons": []}}, "写错的键名"),
            ("", "空输入"),
        ):
            _write(p, bad)
            code, _, _ = _run(fresh_cli, "apply", "--file", p)
            assert code != 0, f"{why} 竟然被接受"
        left = _hot(fresh_work, "schedule.json")["dated_sessions"]
        assert len(left) == 2, f"非法整批把原课表洗掉了：{left}"
        return "整批导入生效；缺字段/非具体日期/错节/错键/空输入 均整份拒绝且原数据未损"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_anomalies_detects(cli: str, work: str) -> str:
    """确定性异常：锚不到周期任务 / 规则 scope 失效；**且过期注册不出现**。

    ★ 注册系统按日期滚转向前：夹具里 `结课论文`（_d(-5)）已滚出，所以它
      **既不在 active、也不在异常里** —— 对模型等效隐藏；但 history 仍查得到。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _, a = _json(fresh_cli, "anomalies")
        kinds = {x["type"] for x in a["anomalies"]}
        for want in ("periodic_unanchored", "rule_scope_mismatch"):
            assert want in kinds, f"未检出 {want}；实际检出 {sorted(kinds)}"
        assert "schedule_empty" not in kinds, kinds
        assert "overdue_deadline" not in kinds, \
            "★ 「逾期」应随滚转退场：日期一过就滚出活跃集，不可能再报逾期"
        assert not any(x.get("task") == "结课论文" for x in a["anomalies"]), \
            "★ 已滚出的注册不该出现在异常里"

        _, ctx = _json(fresh_cli, "context")
        assert not any(d["task"] == "结课论文" for d in ctx["active"]), \
            "★ 已滚出的不该出现在 active"

        _, h = _json(fresh_cli, "history", "--days", "30")
        assert any(d["task"] == "结课论文" for d in h["entries"]), \
            "★ 滚出 ≠ 丢失：history 必须查得到"
        assert "结课论文" in h["unfinished"], h
        assert a["clean"] is False, a
        return f"检出 {sorted(kinds)}；逾期项已退场、过期注册滚出且可回溯"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_anomalies_empty_schedule(cli: str, work: str) -> str:
    """课表为空时必须明说"排期没有依据"，而不是静静排出一堆东西。"""
    fresh_cli, fresh_work = _fresh()
    try:
        _write(os.path.join(fresh_work, "data", "schedule.json"),
               dict(FIXTURE_SCHEDULE, dated_sessions=[], periodic_tasks=[]))
        _, a = _json(fresh_cli, "anomalies")
        kinds = {x["type"] for x in a["anomalies"]}
        assert "schedule_empty" in kinds, f"课表为空却没报：{sorted(kinds)}"
        return "课表为空 → schedule_empty"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_roll_forward(cli: str, work: str) -> str:
    """★ 注册系统**按日期滚转向前**：日期一过即滚出（纯日期驱动，无需写盘）。

    用户设计的三条不变量里，这是最容易漏的一条 —— 没有它，过期条目会永远
    挤占模型上下文（实测：一条逾期 50 天的论文每次都被塞进 context）。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _, ctx = _json(fresh_cli, "context")
        active = {d["task"] for d in ctx["active"]}
        assert "结课论文" not in active, "过期的应滚出活跃集"
        assert "实验报告" not in active, "已完成的不该在活跃集里"

        _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "明天的事", "date": _d(1)}]}})
        _, ctx2 = _json(fresh_cli, "context")
        assert "明天的事" in {d["task"] for d in ctx2["active"]}, ctx2["active"]

        _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "昨天的事", "date": _d(-1)}]}})
        _, ctx3 = _json(fresh_cli, "context")
        assert "昨天的事" not in {d["task"] for d in ctx3["active"]}, \
            "★ 昨天注册的应立刻滚出活跃集"
        _, h = _json(fresh_cli, "history", "--days", "7")
        assert any(d["task"] == "昨天的事" for d in h["entries"]), "滚出的必须可回溯"

        _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "今天的事", "date": _d(0)}]}})
        _, ctx4 = _json(fresh_cli, "context")
        # ★ 午夜边界：`_d(0)` 的截止是**当天 23:59**。若正好在 23:59 那一分钟跑，
        #   它已经"过点"（过点即翻篇）—— 那不是缺陷，是时间到了。这一分钟内跳过断言。
        _n = datetime.now()
        if not (_n.hour == 23 and _n.minute >= 59):
            assert "今天的事" in {d["task"] for d in ctx4["active"]}, \
                "当天到期的必须仍在活跃集（否则当天就看不见了）"
        return "_d(-1) 立刻滚出、_d(0)/_d(1) 在活跃集；已完成不在；滚出可回溯"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_anomalies_follow_state(cli: str, work: str) -> str:
    """★ `anomalies` **只报 `active` 表达不了的机械异常**。

    实测（真实数据）它一共 8 条、**8 条全部**是 expiring_deadline，逐条与 `active` 重合
    —— 那是一个常驻工具定义（442 字符/轮）加一次调用往返，换来零新信息。已删除。
    所以这条用例守的是**新契约**：
      · 快到期的条目**不**出现在 anomalies 里（它只在 `active`，带 urgency/why）
      · 配置类机械异常（规则 scope 指向不存在的任务）照旧报
      · 条目处理完之后，anomalies 里当然也没有它
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "待处理的事", "date": _d(2)}]}})
        _, ctx = _json(fresh_cli, "context")
        assert any(x["task"] == "待处理的事" for x in ctx["active"]), "活跃集里该有它"

        _, a = _json(fresh_cli, "anomalies")
        assert not any(x["type"] == "expiring_deadline" for x in a["anomalies"]), \
            f"★ '即将到期'又回到 anomalies 里了（那是 active 的活）：{a['anomalies']}"
        assert not any(x.get("task") == "待处理的事" for x in a["anomalies"]), a["anomalies"]

        _json(fresh_cli, "mark", "--task", "待处理的事", "--status", "done")
        _, a2 = _json(fresh_cli, "anomalies")
        kinds = {x["type"] for x in a2["anomalies"]}
        assert not any(x.get("task") == "待处理的事" for x in a2["anomalies"]), \
            f"已完成仍出现在异常里：{a2['anomalies']}"
        assert "rule_scope_mismatch" in kinds, "配置类异常不该被任务状态影响"
        return "anomalies 不含到期项（只在 active）；配置类机械异常照旧报"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_dormant_data(cli: str, work: str) -> str:
    """判据必须是**业务记录**：只有一条很旧的待办时报"久未维护"。"""
    fresh_cli, fresh_work = _fresh()
    try:
        _write(os.path.join(fresh_work, "data", "data.json"), {
            "registry": [{"task": "陈年旧账", "date": _d(-90),
                           "priority": "medium", "status": "pending"}],
            "meta": {"last_updated": ""},
        })
        _, a = _json(fresh_cli, "anomalies")
        d = next(x for x in a["anomalies"] if x["type"] == "dormant_data")
        assert d["days_since"] >= 90, d
        return f"久未维护 {d['days_since']} 天被检出"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_remove_periodic(cli: str, work: str) -> str:
    """删周期任务定义（在 schedule.json）；★ 名称不匹配时**如实报 missing**，不静默。"""
    fresh_cli, fresh_work = _fresh()
    try:
        _, r = _apply(fresh_cli, fresh_work,
                      {"schedule": {"periodic_tasks": {"remove": ["示例课程A作业"]}}})
        rep = r["report"]["schedule"]["periodic_tasks"]
        assert rep["removed"] == 1 and rep["missing"] == [], rep
        left = [p["task"] for p in _hot(fresh_work, "schedule.json")["periodic_tasks"]]
        assert left == ["锚不到作业"], left

        # 名称不完全一致 → 没删掉，且 report.missing 点名（旧 remove-periodic 是直接报错的）
        _, r2 = _apply(fresh_cli, fresh_work,
                       {"schedule": {"periodic_tasks": {"remove": ["示例课程A作"]}}})
        rep2 = r2["report"]["schedule"]["periodic_tasks"]
        assert rep2["removed"] == 0 and rep2["missing"] == ["示例课程A作"], rep2
        assert r2.get("check_this"), "没删掉却没提示"
        left2 = [p["task"] for p in _hot(fresh_work, "schedule.json")["periodic_tasks"]]
        assert left2 == ["锚不到作业"], f"名称不一致竟然删掉了：{left2}"
        return "删除生效；名称不一致不静默 —— report.missing 点名 + check_this 提示，且没删掉"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_bounded_views(cli: str, work: str) -> str:
    """★ 历史可以无限，但**模型视图必须有限**（用户要求：归档后模型只看 list，
    非必要不看完整信息）。

    三处收口：
      · `history` 单次上限 —— 超出只报 `more`，不倒全部历史
      · `show` 已归档条目默认**不回 detail**，要 `cold: true` 显式声明"这次必要"
      · `show` 失败时的 `known` **只列活跃注册的名字**，不并上整份冷存储
        （那条失败提示以前会随年头线性膨胀，恰恰在最该短的时候最长）
    外加：内核**不做**策略/语义类的"重复"检查（红线）—— 只做机制。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _json(fresh_cli, "sync")          # 物化；_d(-10) 那条被搬进冷存储

        # ① history 有上限，超出只报计数
        _, h = _json(fresh_cli, "history", "--days", "30", "--limit", "1")
        assert h["limit"] == 1 and len(h["entries"]) == 1, h
        assert h["count"] >= 2 and h["more"] == h["count"] - 1, h

        # ② 已归档：默认只给列表视图字段
        _, s1 = _json(fresh_cli, "show", "--task", "实验报告")
        assert s1["archived"] is True and s1["has_detail"] is True, s1
        assert "detail" not in s1["entry"], f"归档条目默认不该回 detail：{s1}"
        assert "cold" in s1.get("note", "") or "归档" in s1.get("note", ""), s1
        # 显式声明必要 → 给
        _, s2 = _json(fresh_cli, "show", "--task", "实验报告", "--cold")
        assert s2["entry"].get("detail"), s2

        # ③ 热条目不受影响
        _, s3 = _json(fresh_cli, "show", "--task", "结课论文")
        assert s3["archived"] is False and s3["entry"].get("detail"), s3

        # ④ 找不到名字时，known 只列**热**名字
        code, _, err = _run(fresh_cli, "show", "--task", "根本不存在")
        assert code != 0, "不存在的名字竟然读成功"
        payload = json.loads(err)
        assert "实验报告" not in payload["known"], \
            f"★ known 里混进了已归档的名字（会随年头膨胀）：{payload['known']}"
        assert "结课论文" in payload["known"], payload["known"]

        # ⑤ ★ 红线守门：规则条数超了也好、名字撞了也好，内核**都不报** ——
        #    「规则该有几条」「这是不是重复」是**策略与语义**，写在 AGENTS.md（准则）
        #    和技能里，由 Agent 判断。内核只做机制（存/算/校验/滚转）。
        rules = [{"id": f"rule-{i:03d}", "kind": "preference",
                  "condition": f"条件{i}", "body": f"做法{i}"} for i in range(1, 18)]
        _apply(fresh_cli, fresh_work, {"rules": {"planning_rules": rules}})
        _, a = _json(fresh_cli, "anomalies")
        kinds = {x["type"] for x in a["anomalies"]}
        for banned in ("rules_bloat", "duplicate_identity", "duplicate_task"):
            assert banned not in kinds, f"★ 内核又去做策略/语义检查了：{banned} ∈ {kinds}"
        return ("history 有上限(more)；归档条目默认不回 detail(cold 才给)；"
                "known 只列热名字；★ 内核不报策略/重复类检查")
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)






def case_sync_reports_not_decides(cli: str, work: str) -> str:
    """★ 循环任务的"这一天算不算发生"是 **Agent 的判断**，内核只报、不替它决定。

    用户明确要求：「不要随意让内核检查重复，『面对循环任务怎么写』是 Agent 的任务，
    而不是内核的任务。」

    内核该守的只有一条**机制**底线：**不为同一次发生建第二条** ——
    实测踩到：Agent 先手工登记一次、又注册成周期任务，旧 sync 的查重只认
    `occurrence_of` ⇒ 认不出那条手工的 ⇒ 又物化一条同名的 ⇒ LIST 里同名两条，
    按名字操作的 `mark` / `list.upsert` 立刻有歧义。

    至于那条手工条目**算不算**这个周期任务的这一次、要不要补 `occurrence_of`，
    内核**不擅自**做 —— 它只报出来，认领由 Agent 经唯一写入口完成。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        # 夹具里「示例课程A作业」跟「示例课程A」走，最近一节是 _d(2)
        _apply(fresh_cli, fresh_work, {"list": {"upsert": [{
            "task": "示例课程A作业", "date": _d(2), "due_time": "上课前（10:10 前）",
            "detail": {"note": "手工登记的，信息更全"}}]}})

        _, r = _json(fresh_cli, "sync")
        # ① 机制底线：没有建第二条
        assert not any(x["task"] == "示例课程A作业" for x in r["created"]), r["created"]
        hot = [d for d in _hot(fresh_work, "data.json")["registry"]
               if d["task"] == "示例课程A作业"]
        assert len(hot) == 1, f"★ 同一次发生被建了两条：{hot}"
        # ② 但报出来了，把判断交给 Agent
        assert [x["task"] for x in r["already_present"]] == ["示例课程A作业"], r
        assert "由你判断" in r["already_present"][0]["note"], r["already_present"][0]
        # ③ ★ 内核**没有**替 Agent 认领：那条条目一个字段都没被改
        assert "occurrence_of" not in hot[0], f"★ 内核擅自认领了：{hot[0]}"
        assert hot[0]["due_time"] == "上课前（10:10 前）", hot[0]
        assert hot[0]["detail"]["note"] == "手工登记的，信息更全", hot[0]
        # ④ 待决问题**持续**报（它是常驻信号，不是内核替人做的决定）
        _, r2 = _json(fresh_cli, "sync")
        assert [x["task"] for x in r2["already_present"]] == ["示例课程A作业"], r2
        assert len([d for d in _hot(fresh_work, "data.json")["registry"]
                    if d["task"] == "示例课程A作业"]) == 1
        # ⑤ 认领由 Agent 走唯一写入口：补 occurrence_of / anchored_to
        _apply(fresh_cli, fresh_work, {"list": {"upsert": [{
            "task": "示例课程A作业", "date": _d(2), "due_time": "上课前（10:10 前）",
            "occurrence_of": "示例课程A作业", "anchored_to": _d(2),
            "detail": {"note": "手工登记的，信息更全"}}]}})
        _, r3 = _json(fresh_cli, "sync")
        assert r3["already_present"] == [], r3
        assert not any(x["task"] == "示例课程A作业" for x in r3["created"]), r3
        return ("内核不为同一次发生建第二条、并报 already_present；"
                "**不擅自补 occurrence_of**；认领由 Agent 经 apply 完成")
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_atomic_write(cli: str, work: str) -> str:
    """★ 整份重写的文件必须**原子落盘** —— 写到一半崩，旧内容不能变成半截 JSON。

    为什么必须有这条：四个文件都是"读全量 → 改 → 全量重写"。若直接 `open(path,"w")`
    然后崩在 `json.dump` 中途，文件就被截成半截 ⇒ **整份内容没了**（冷存储最惨，
    它装着全部历史，每次 sync 都要整体重写）。所以：临时文件 + `os.replace`，
    失败擦干净、不留 `.tmp`。

    跑在子进程 + 临时副本里（import core.datastore 会按 core.paths 定位数据目录，
    绝不能在仓库里做这件事）。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        script = os.path.join(fresh_work, "_atomic_probe.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write('''
import io, json, os, sys
sys.path.insert(0, ".")
from core import datastore as ds

target = os.path.join("data", "data.json")
before = io.open(target, encoding="utf-8").read()

# ① 校验不过 → 既不写、也不留临时文件
class Boom(Exception):
    pass


def bad_validator(_):
    raise Boom("校验失败")


try:
    ds._write_json(target, {"nope": 1}, bad_validator)
except Boom:
    pass
assert io.open(target, encoding="utf-8").read() == before, "校验失败却改了文件"
assert not os.path.exists(target + ".tmp"), "校验失败留下了 .tmp"

# ② 序列化中途失败（不可序列化对象）→ 旧内容完好、无残留
try:
    ds._write_json(target, {"x": object()}, lambda d: None)
except TypeError:
    pass
assert io.open(target, encoding="utf-8").read() == before, "写失败把文件弄坏了"
assert not os.path.exists(target + ".tmp"), "写失败留下了 .tmp"

# ③ 正常写必须生效
ds._write_json(target, json.loads(before), lambda d: None)
assert io.open(target, encoding="utf-8").read() == before, "正常写没生效"
print("ATOMIC-OK")
''')
        code, stdout, _ = _run(script, cwd=fresh_work)
        assert code == 0 and "ATOMIC-OK" in stdout, f"原子写探针失败：{stdout}"
        return "校验失败/序列化中途失败 都不改文件、不留 .tmp；正常写生效"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_expired_rolls_off(cli: str, work: str) -> str:
    """★ **过点即默认完成**（用户要求："过期的就不要管了，默认完成了"）。

    判定精确到**时刻**，不只是日期：
      · 今天 10:10 截止的事，到了晚上不该再算活跃 —— 更不该以
        "已过 13 小时（仍未处理）"的形式追着模型/用户
      · 所谓"逾期提醒"在本系统里**不存在**：过点 = 翻篇

    但内核**不改写存储**：`status` 仍是 `pending` —— 状态是用户/生命周期的信息，
    内核不替用户宣布"你做完了"。改动只在**视图与判断**层；
    真要回顾（"上周那件交没交"）用 `history` / `show` 显式取。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        past_d, past_hm = _due_spec(-120)      # 两小时前（过点）
        soon_d, soon_hm = _due_spec(120)       # 两小时后（没到点）
        _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "刚过点的事", "date": past_d, "due_time": past_hm},
            {"task": "还有两小时", "date": soon_d, "due_time": soon_hm},
        ]}})

        _, ctx = _json(fresh_cli, "context")
        names = [x["task"] for x in ctx["active"]]
        assert "刚过点的事" not in names, f"★ 过点的还留在活跃集：{names}"
        assert "还有两小时" in names, f"没到点的不该消失：{names}"

        # 过点的**不得**出现在异常里（"逾期"这个概念不存在）
        _, a = _json(fresh_cli, "anomalies")
        assert not any(x.get("task") == "刚过点的事" for x in a["anomalies"]), a["anomalies"]

        # 但存储**没有被改写**：状态仍是 pending
        hot = [d for d in _hot(fresh_work, "data.json")["registry"]
               if d["task"] == "刚过点的事"]
        assert len(hot) == 1 and hot[0]["status"] == "pending", hot

        # 显式取回仍拿得到（用户主动问"那件怎么样了"）——
        # 注意 `history` 只覆盖**日期已过**的；今天刚过点的仍在热文件里，用 `show` 取。
        _, one = _json(fresh_cli, "show", "--task", "刚过点的事")
        assert one["ok"] and one["entry"]["status"] == "pending", one
        assert one["entry"]["due_time"] == past_hm, one

        # 日期已过的那一类照旧可回溯
        _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "昨天的事", "date": _d(-1)}]}})
        _, h = _json(fresh_cli, "history", "--days", "3")
        assert "昨天的事" in [x["task"] for x in h["entries"]], h

        # 紧迫度只讲"还剩"，不再有"已过…（仍未处理）"这种追问语气
        _, sv = _json(fresh_cli, "sessions", "--days", "3")
        for x in sv["due"]:
            assert "已过" not in x.get("why", ""), x
            # ★ 还要守「还剩 0 分钟」：合并自 case_past_due_not_in_sessions。
            #   实测那个 bug 的症状正是 why="还剩 0 分钟"（**不是**"已过"）——
            #   只断言"已过"抓不到它（两条都在查 sessions，但抓的不是同一件事）。
            assert "还剩 0 分钟" not in x.get("why", ""), x
        assert not any(x.get("task") == "刚过点的事" for x in sv["due"]), \
            "过点条目还在 sessions.due 里：%s" % sv["due"]
        for x in ctx["active"]:
            assert "已过" not in x.get("why", ""), x
        return "过点即翻篇（不进视图、不报异常、无'已过'字样）；存储状态未改写；history 仍可回溯"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_calendar_periodic(cli: str, work: str) -> str:
    """★ **日历周期**：与上课无关的固定规律（每周几 + 时刻）。

    为什么必须有（实测卡了两轮）：`follow_course` 只能锚到某门课的**课次**，
    而「每周三晚上 23:59 交」是日历规律；更要命的是同一门课一周上两次时
    （示例课程D在周二、周四都有），课程锚点会取到**错的星期** ——
    用户两次要求"周三交"，内核两次物化成"周二"。

    现在 `every: weekly` + `weekday`（ISO：1=周一…7=周日）由内核算出**具体日期**，
    仍然不涉及周次；且只挑**那一刻还没到**的（过点的按"默认完成"翻篇，不再物化）。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _apply(fresh_cli, fresh_work, {"schedule": {"periodic_tasks": {"upsert": [
            {"task": "周三晚交的作业", "every": "weekly", "weekday": 3,
             "due_time": "23:59"}]}}})
        _, r = _json(fresh_cli, "sync")
        made = [x for x in r["created"] if x["task"] == "周三晚交的作业"]
        assert made, f"日历周期没被物化：{r}"
        d = made[0]["date"]
        assert datetime.strptime(d, "%Y-%m-%d").isoweekday() == 3, f"★ 应落在周三，实际 {d}"
        assert datetime.strptime(d + " 23:59", "%Y-%m-%d %H:%M") >= datetime.now(), \
            f"★ 物化出了一个已经过点的：{d}"

        # 幂等：再 sync 不多建
        _, r2 = _json(fresh_cli, "sync")
        assert not any(x["task"] == "周三晚交的作业" for x in r2["created"]), r2

        # 缺 weekday → 内核**不猜**，明确报 unanchored
        _apply(fresh_cli, fresh_work, {"schedule": {"periodic_tasks": {"upsert": [
            {"task": "缺星期的事", "every": "weekly"}]}}})
        _, r3 = _json(fresh_cli, "sync")
        un = [x for x in r3["unanchored"] if x["task"] == "缺星期的事"]
        assert un and "weekday" in un[0]["reason"], r3

        # 与课次无关的日历任务**毫不在意**课表里有没有这门课
        hot = [x for x in _hot(fresh_work, "data.json")["registry"]
               if x["task"] == "周三晚交的作业"]
        assert len(hot) == 1 and hot[0]["occurrence_of"] == "周三晚交的作业", hot
        return "weekly 落在正确的星期且那一刻未到；幂等；缺 weekday 明确报 unanchored"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_plan_snapshot_bounded(cli: str, work: str) -> str:
    """★ 规划快照：**文件存储 + 有界**（从 SQLite 搬出来的那个）。

    原先存在 `早期 SQLite 实现` 的 `timeline_plans` 表，三个问题：
      · 红线是「文件作为存储」—— 别的状态都在 `data/*.json`
      · `*.db` 被 gitignore ⇒ 快照**不进版本控制**
      · 它**只增不减**（实测已 27 行），而"不能无限膨胀"是硬要求
    现在在 `data/plans.json`，只留最近 `KEEP`（20）次。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        def snap(i):
            p = os.path.join(fresh_work, f"_plan{i}.json")
            _write(p, {"order": [f"第{i}份"], "focus": f"第{i}次规划",
                       "by_day": [{"date": _d(0), "blocks": [{"task": f"第{i}份"}]}]})
            return _json(fresh_cli, "plan-snapshot", "--file", p,
                         "--triggered-by", f"第{i}次")

        code, r1 = snap(1)
        assert r1["ok"] and r1["kept"] == 1, r1
        assert os.path.isfile(os.path.join(fresh_work, "data", "plans.json")), \
            "快照没落到 plans.json（文件存储）"

        # 存到超过上限：最旧的应被淘汰，总数封顶
        for i in range(2, 25):
            _, r = snap(i)
        assert r["kept"] == 20, f"★ 快照没有收口：{r['kept']}"
        _, ctx = _json(fresh_cli, "context")
        lp = ctx.get("last_plan")
        assert lp is not None, "last_plan 读不回来"
        assert lp["order"] == ["第24份"], lp
        assert lp["triggered_by"] == "第24次", lp
        assert {"focus", "stale", "slots", "order"} <= set(lp), lp
        assert lp["order"] == ["第24份"], lp   # 兼容 order / by_day 两种形状
        hist = ctx.get("plan_history") or []
        assert hist and hist[0]["triggered_by"] == "第24次", hist
        assert all("plan" not in h for h in hist), "历史不该带正文（上下文会被撑爆）"

        # 旧的 SQLite 层必须已经不在了
        assert not os.path.exists(os.path.join(fresh_work, "knowledge", "db", "agent.db")), \
            "还在用 SQLite 存快照"
        assert not os.path.exists(os.path.join(fresh_work, "core", "database.py")), \
            "core/database.py 应该已经删掉"
        return "快照写进 plans.json、读得回来、封顶 20 份、历史只给元信息；SQLite 层已移除"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_cancelled_occurrence_stays_dead(cli: str, work: str) -> str:
    """★ 取消过的那一次**不能被 sync 复活**（实测踩到的第三个坑）。

    场景就是用户这轮做的：
      ① 周期任务先按**课次模式**物化过一次（`anchored_to` = 那节课的日期）
      ② 用户说"这周取消了" ⇒ Agent 把它 `mark` 成 `cancelled`
      ③ 改成**日历周期**（每周几）
    此时若查重只认 `anchored_to`，日历周期找"下一个那天"会算出**被取消的那一天**，
    当场复活 —— 而 Agent 已经明确告诉用户"下周才生成"。

    修法：`taken` 同时收 **锚点**与**排期日期** —— 这一次发生**已经被处理过**
    （完成 / 取消 / 改期都算），不该再物化第二遍。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        wd = (datetime.now() + timedelta(days=1)).isoweekday()  # 锚到"明天"那个星期几
        _apply(fresh_cli, fresh_work, {"schedule": {"periodic_tasks": {"upsert": [
            {"task": "每周交的活", "every": "weekly", "weekday": wd,
             "due_time": "23:59"}]}}})
        _, r1 = _json(fresh_cli, "sync")
        made = [x for x in r1["created"] if x["task"] == "每周交的活"]
        assert made, f"第一次没物化出来：{r1}"
        d1 = made[0]["date"]

        _json(fresh_cli, "mark", "--task", "每周交的活", "--status", "cancelled")

        _, r2 = _json(fresh_cli, "sync")
        again = [x for x in r2["created"] if x["task"] == "每周交的活"]
        assert not any(x["date"] == d1 for x in again), f"★ 取消的那次被复活了：{again}"
        wk = (datetime.strptime(d1, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
        assert again and again[0]["date"] == wk, f"应滚到下周同一天 {wk}：{again}"
        return f"{d1} 取消后不复活，如期滚到 {again[0]['date']}"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_no_week_counting(cli: str, work: str) -> str:
    """★ 守住「**不以周作为计数方法**」—— 用机器守，不靠记性。

    用户的要求：日期只能来自截图 / 口述 / 已给日期加减，**不做周次推算**。
    理由：学校会调休、停课、调课、换教室，"第N周周三"推不出"10月14日"，硬推必错。

    但口语里的"这周/下周/周三"必须能用 —— 区别在于它们属于哪一类"周"：
      · **周次**（第N周 / 教学周）＝ 计数系统       ⇒ **禁止**（不存字段、不做算术）
      · **周几**（周三 ＝ 一周里第三天）＝ 日历属性 ⇒ **允许**（every + weekday，ISO 1-7）
      · **相对指针**（这周 / 下周）＝ 只活在对话里   ⇒ 由 Agent 解析成**具体日期**才写进来

    这条用例守"禁止"那一半：
      ① 内核源码里不许有周号运算（isocalendar / %W / %U）与周次字段名
      ② 数据文件里不许出现周次值（YYYY-Www）或周次键（weekday 除外 —— 那是日历属性）
      ③ 行为上：`every: weekly` 是**逐日推进**找一个星期几，不是"周次×7"
    """
    import re as _re
    import importlib

    # ① 源码
    #    ★ 判据要**精确**：说明文字（docstring / 注释）里总会提到这些词（"已删除
    #      current_week"、"没有 anchor_week 概念"），按子串扫会把说明判成违规 ——
    #      第一版就是这么误报的。只认**代码形态**：函数调用、格式串、带引号的键名。
    srcs = [os.path.join(work, "qyn.py")]
    core = os.path.join(work, "core")
    srcs += [os.path.join(core, f) for f in sorted(os.listdir(core)) if f.endswith(".py")]
    banned = (
        (_re.compile(r"isocalendar\s*\("), "周号运算 isocalendar()"),
        (_re.compile(r"""['"]%[WU]['"]"""), "周号格式化 %W / %U"),
        (_re.compile(r"""['"](current_week|anchor_week|week_no)['"]\s*:"""), "周次字段"),
    )
    hits = []
    for p in srcs:
        for ln in io.open(p, encoding="utf-8").read().splitlines():
            code = ln.split("#", 1)[0]
            for pat, label in banned:
                if pat.search(code):
                    hits.append(f"{os.path.basename(p)} [{label}]: {ln.strip()[:60]}")
    assert not hits, f"★ 内核里还有周次运算/字段：{hits}"

    # ② 数据
    weekval = _re.compile(r"\d{4}-W\d{2}")
    weekkey = _re.compile(r'"[a-z_]*week[a-z_]*"\s*:')
    for name in ("data.json", "schedule.json"):
        raw = io.open(os.path.join(work, "data", name), encoding="utf-8").read()
        m = weekval.search(raw)
        assert not m, f"★ {name} 里出现了周次值：{m.group()}"
        keys = {k for k in weekkey.findall(raw) if '"weekday"' not in k}
        assert not keys, f"★ {name} 里出现了周次键：{keys}"

    # ③ 行为：weekly 逐日推进
    sys.path.insert(0, work)
    sys.modules.pop("qyn", None)
    q = importlib.import_module("qyn")
    tgt = (datetime.now() + timedelta(days=3)).isoweekday()
    a, why = q._next_anchor({"task": "x", "every": "weekly", "weekday": tgt,
                             "due_time": "23:59"}, {"dated_sessions": []}, set(),
                            now=datetime.now())
    assert a, f"weekly 锚不出来：{why}"
    d = datetime.strptime(a, "%Y-%m-%d")
    assert d.isoweekday() == tgt, f"锚点不是目标星期几：{a}"
    assert timedelta(0) <= (d - datetime.now()) < timedelta(days=7), f"weekly 应落在 7 天内：{a}"
    return "源码无周次运算、数据无周次字段/值；weekly 是逐日推进（ISO 1-7 只是日历属性）"


def case_ddl_or_nothing(cli: str, work: str) -> str:
    """★ **DDL 的结构不变式**：没有 `date` 就不驱动紧迫度。

    用户发现的逻辑环（排期写进 due_time ⇒ 被当死线 ⇒ 不急的事浮到最前）
    现在由**结构**挡住，不需要内核去"检测"：
      · `date` 有 ⇒ 算余量、参与排序
      · `date` 没有 ⇒ 算不出余量 ⇒ 排最后，理由**如实**说
        （只有 due_time 就说『缺 DDL 日期』，不假装它不急）
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "有截止的", "date": _d(3), "due_time": "23:59"},
            {"task": "没截止的"},
            {"task": "只有时刻没日期", "due_time": "周五 14:20 前"}]}})
        _, ctx = _json(fresh_cli, "context")
        rows = {r["task"]: r for r in ctx["active"]}

        assert rows["有截止的"]["urgency"] != "later", rows["有截止的"]
        assert rows["没截止的"]["urgency"] == "later", rows["没截止的"]
        assert "没有 DDL 日期" in rows["没截止的"]["why"], rows["没截止的"]
        assert rows["只有时刻没日期"]["urgency"] == "later", rows["只有时刻没日期"]
        assert "缺 DDL 日期" in rows["只有时刻没日期"]["why"], rows["只有时刻没日期"]
        return "没有 date 的条目不驱动紧迫度；只有 due_time 的如实报『缺 DDL 日期』"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_roll_forward_not_delete(cli: str, work: str) -> str:
    """★ 任务只在 deadline 或宣布完成时才消失（用户定的口径）。

    · 无 DDL 的条目：日期过了也**留在活跃集**，只有完成/取消能让它消失
      （`_compact` 也不许把它搬进冷存储 —— 搬走就是从视图里删掉）
    · 计划里排过的时段：内核只给**事实** `passed`（已过/未过）；
      "已过但没做 ⇒ 往后延"是 Agent 的判断（准则 15）
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "有死线的活", "date": _d(3)},
            {"task": "没死线的活"}]}})

        _, ctx = _json(fresh_cli, "context")
        names = [r["task"] for r in ctx["active"]]
        assert "没死线的活" in names, f"★ 无 DDL 的条目被挤掉了：{names}"

        _json(fresh_cli, "sync")               # 含 compact
        hot = _hot(fresh_work, "data.json")["registry"]
        assert any(d["task"] == "没死线的活" for d in hot), "★ 无 DDL 的条目被搬进冷存储了"

        plan = {"focus": "昨天的安排", "order": ["有死线的活"],
                "slots": [{"date": _d(-1), "time": "14:00-16:00", "task": "有死线的活"}]}
        p = os.path.join(fresh_work, "_plan.json")
        _write(p, plan)
        _json(fresh_cli, "plan-snapshot", "--file", p, "--triggered-by", "用例")
        _, ctx2 = _json(fresh_cli, "context")
        sl = ctx2["last_plan"]["slots"]
        assert sl and sl[0]["task"] == "有死线的活" and sl[0]["passed"] is True, sl
        return "无 DDL 的条目过了日期也不消失、不进冷存储；排过时段的 passed=true（事实）"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_soft_task_ranks_last(cli: str, work: str) -> str:
    """★ **没有硬截止的软任务**必须排最后，且不被自己的描述性时间顶上去。

    实测（用户一眼看出来）：一条「睡前顺手取包裹」写成
    due_time:"23:00 后睡前办（24:00 前）" —— 内核从描述里解出 23:00、
    算出「还剩 4 小时」、给 88 分 urgent，把**最不要紧的事排到第一位**。

    修法：条目上一个布尔 
o_deadline（语义由 Agent 判断，这里只做确定性后果）：
      · 排最后（later，理由没有硬截止）
      · due_time 只当描述，**不参与紧迫度**
      · 过了描述时刻也**不**被判成过期（它本来就没有死线）
    """
    fresh_cli, fresh_work = _fresh()
    try:
        soon_d, soon_hm = _due_spec(60)          # 一小时后的"描述性时间"
        _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            # ★ 没有 DDL：不写 date / due_time —— 建议时段只进计划的 slots
            {"task": "顺手办的小事"},
            {"task": "真有死线的事", "date": _d(3)}]}})

        _, ctx = _json(fresh_cli, "context")
        rows = ctx["active"]
        by = {r["task"]: r for r in rows}
        assert by["顺手办的小事"]["urgency"] == "later", by["顺手办的小事"]
        assert "没有 DDL 日期" in by["顺手办的小事"]["why"], by["顺手办的小事"]
        assert rows[-1]["task"] == "顺手办的小事", f"软任务该排最后：{[r['task'] for r in rows]}"
        return "没有 DDL 的条目排最后、理由说清、不被建议时段顶上第一位"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_write_invalidates_plan(cli: str, work: str) -> str:
    """★ 写操作附带一条**事实**：注册表变了 ⇒ 上一份计划快照不再是它的派生物。

    `plan_invalidated: true` 是事实（不是指令 —— 那段"你该重排"的话已删掉，
    属于准则 14）。只在**真的改了东西**时附带：no-op 的写不该报（否则是假警报）。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _, r = _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "新登记的事", "date": _d(3)}]}})
        assert r.get("plan_invalidated") is True, r
        assert "next" not in r, f"★ 内核又在给指令了：{r.get('next')}"

        _, m = _json(fresh_cli, "mark", "--task", "新登记的事", "--status", "done")
        assert m.get("plan_invalidated") is True, m
        assert m.get("changed") == 1, m

        _, m2 = _json(fresh_cli, "mark", "--task", "新登记的事", "--status", "done")
        assert m2.get("plan_invalidated") is None, f"no-op 不该报：{m2}"
        assert m2.get("changed") == 0, m2

        # ★ 只改**课表**也必须作废旧规划（判据按集合级摊平：原先只看顶层，
        #   而 `report["schedule"]` 是嵌套的 ⇒ 只改课表时永远不附带 —— 一份基于旧课表的
        #   规划会被当成"新鲜的"继续用）。
        _, sc = _apply(fresh_cli, fresh_work, {"schedule": {"sessions": {"upsert": [
            {"date": _d(2), "periods": "7-8", "course": "新加的课"}]}}})
        assert sc.get("plan_invalidated") is True, f"只改课表却没作废旧规划：{sc}"
        # 什么都改不到 ⇒ 不报（假警报会让"重排"变成每轮都做的事）
        _, no = _apply(fresh_cli, fresh_work, {"list": {"upsert": [], "remove": []}})
        assert no.get("plan_invalidated") is None, f"什么都没改却报作废：{no}"
        return "真改时带 plan_invalidated（事实、无指令，注册表/课表都算）；no-op 不带"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_materialize_window(cli: str, work: str) -> str:
    """★ **物化窗口**：课表可以一次给一学期，注册表只留近处的。

    没有窗口时（实测）：一学期几百节课会全部变成注册条目 ⇒ `data.json` 与每轮
    上下文一起膨胀。有了窗口：窗口外的留在 `schedule.json` 里等日期推进 ——
    **队列式滚动**，和 `_compact`（过期搬冷存储）配成一对。

    这也是"一周周传 vs 一学期一次性传"都能工作的原因：每节课**自带绝对日期**，
    身份是 `date|periods`，所以根本不需要区分"这周/下周"。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        days = [1, 5, 13, 20, 100]
        _apply(fresh_cli, fresh_work, {"schedule": {
            "periods": {"1": "08:00-09:40"},
            "replace_future": True, "dated_sessions": [{"date": _d(k), "periods": "1", "course": f"课{k}",
                                "location": "示例教室X1", "kind": "normal"} for k in days]}})

        _, r = _json(fresh_cli, "sync")
        made = {c["date"] for c in r["created"] if c["kind"] == "session"}
        # 窗口内（≤14 天）的三节被登记；20 天与 100 天的留着等推进
        assert made == {_d(1), _d(5), _d(13)}, f"★ 窗口没起作用：{sorted(made)}"
        assert r["deferred"] == 2, f"★ 窗口外的应报 deferred：{r['deferred']}"

        # 注册表里确实只有窗口内那三节
        hot = _hot(fresh_work, "data.json")["registry"]
        sess = [d for d in hot if d.get("session_key")]
        assert len(sess) == 3, f"★ 注册表被课表撑大了：{len(sess)} 条课次"

        # 幂等：再跑一次不会重复登记，deferred 仍是 2
        _, r2 = _json(fresh_cli, "sync")
        assert not [c for c in r2["created"] if c["kind"] == "session"], r2["created"]
        assert r2["deferred"] == 2, r2["deferred"]
        return "窗口内 3 节进注册表、窗口外 2 节报 deferred 并留待推进；幂等"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_big_timetable(cli: str, work: str) -> str:
    """★ **一学期课表一次性给**：整条路径都要撑得住。

    ① 写入：一次 apply 120 节（模型输出有限，SKILL 要求分批；内核要接得住）
    ② 物化：只登记近 14 天（`MATERIALIZE_DAYS`），其余 `deferred`
    ③ 注册表不随课表长度增长
    ④ 课次视图**有上限**（`SESSIONS_LIMIT`）+ `more` / `capped_at`  ← 原先漏了这环
    ⑤ `qyn_state` 不随课表长度增长（课表全量不进上下文）
    """
    fresh_cli, fresh_work = _fresh()
    try:
        # 120 节：从明天起，每周一三五各 2 节，约 5 个月（≈ 一学期）
        sess = []
        d = 1
        while len(sess) < 120:
            if datetime.strptime(_d(d), "%Y-%m-%d").weekday() in (0, 2, 4):
                for p in ("1-2", "3-4"):
                    sess.append({"date": _d(d), "periods": p, "course": f"课{p}",
                                 "location": "示例教室X1", "kind": "normal"})
            d += 1
        p = os.path.join(fresh_work, "_sem.json")
        _write(p, {"schedule": {"periods": {"1": "08:00-09:40", "3": "10:10-11:50"},
                                "replace_future": True, "dated_sessions": sess}})
        _, r = _json(fresh_cli, "apply", "--file", p)
        assert r["ok"], r

        # ② 物化窗口：近 14 天的进注册表，其余 deferred
        _, sy = _json(fresh_cli, "sync")
        made = [c for c in sy["created"] if c["kind"] == "session"]
        assert made and all(c["date"] <= _d(14) for c in made), made
        assert sy["deferred"] > 100, f"★ 窗口没挡住整学期：deferred={sy['deferred']}"

        # ③ 注册表有界（只有窗口内那几节）
        hot = _hot(fresh_work, "data.json")["registry"]
        sess_entries = [x for x in hot if x.get("session_key")]
        assert len(sess_entries) == len(made), (len(sess_entries), len(made))

        # ④ 课次视图有上限
        _, sv = _json(fresh_cli, "sessions", "--days", "200")
        assert sv["count"] == 120, sv["count"]
        assert len(sv["sessions"]) == 60 and sv["more"] == 60, (len(sv["sessions"]), sv["more"])
        assert sv["capped_at"], sv
        # 显式提高上限也照办
        _, sv2 = _json(fresh_cli, "sessions", "--days", "200", "--limit", "120")
        assert len(sv2["sessions"]) == 120 and sv2["more"] == 0, sv2["more"]

        # ⑤ 上下文不随课表长度增长
        _, ctx = _json(fresh_cli, "context")
        assert "dated_sessions" not in ctx, "★ 课表全量进上下文了"
        assert len(json.dumps(ctx, ensure_ascii=False)) < 12000, len(json.dumps(ctx))
        return ("120 节一次写入 OK；物化只取窗口内、deferred>100；注册表不涨；"
                "课次视图 60+more 60+capped_at；上下文不含课表")
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_plan_view_contract(cli: str, work: str) -> str:
    """★ 规划读回**只给事实** —— 用户指出内核被我塞满了机械检查与说教。

    `hint` / `not_active` / `not_planned` / `overdue_slots` / `self_imposed` 全删：
    那些是**策略提示**，属于 AGENTS.md 的准则与技能；内核里塞它们属于越界
    （实测把 `_plan_view` 撑到 172 行）。保留的只有事实 + 算术：
    `slots`（含算好的 `passed`）、`order`、`focus`、`horizon`、`stale`、`age_days`。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "甲任务", "date": _d(1)},
            {"task": "乙任务", "date": _d(2)}]}})

        plan = {"focus": "先甲后乙", "horizon": f"{_d(-1)} 至 {_d(2)}",
                "order": ["甲任务", "乙任务"],
                "slots": [{"date": _d(1), "time": "09:00-11:00", "task": "甲任务"},
                          {"date": _d(-1), "time": "09:00-11:00", "task": "乙任务"}]}
        p = os.path.join(fresh_work, "_plan.json")
        _write(p, plan)
        _json(fresh_cli, "plan-snapshot", "--file", p, "--triggered-by", "用例")

        _, ctx = _json(fresh_cli, "context")
        lp = ctx["last_plan"]
        assert [s["task"] for s in lp["slots"]] == ["甲任务", "乙任务"], lp["slots"]
        assert lp["order"] == ["甲任务", "乙任务"], lp["order"]
        # `passed` 是**算好的事实**：明天的 false、昨天的 true
        assert lp["slots"][0]["passed"] is False, lp["slots"]
        assert lp["slots"][1]["passed"] is True, lp["slots"]
        # ★ 内核**不许**再给指令/差异列表
        for gone in ("hint", "not_active", "not_planned", "overdue_slots",
                     "self_imposed", "slots_note"):
            assert gone not in lp, f"★ 内核又在给指令了：{gone}"
        return "slots 带算好的 passed；内核不再输出 hint / 差异列表（只给事实）"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_plan_is_readable(cli: str, work: str) -> str:
    """★ 规划必须**读得回来** —— Agent 接管时间管理的前提。

    只读**事实**：`order` / `slots` / `stale` / `focus`。数据在上次规划**之后**又变过
    ⇒ `stale=True`（"那该怎么办"是准则 14 的事，内核不说）。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        p = os.path.join(fresh_work, "_plan.json")
        _write(p, {"order": ["先写示例课程F"], "focus": "先写示例课程F"})
        _json(fresh_cli, "plan-snapshot", "--file", p, "--triggered-by", "冒烟")
        _, ctx = _json(fresh_cli, "context")
        lp = ctx.get("last_plan")
        assert lp and lp["focus"] == "先写示例课程F", lp
        assert lp["order"] == ["先写示例课程F"], lp
        assert lp["stale"] is False, lp

        # 数据在上次规划**之后**又变过 ⇒ stale（事实，不是建议）
        dpath = os.path.join(fresh_work, "data", "data.json")
        raw = json.load(open(dpath, encoding="utf-8"))
        raw["meta"]["last_updated"] = (datetime.now()
                                       + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        _write(dpath, raw)
        _, ctx2 = _json(fresh_cli, "context")
        assert ctx2["last_plan"]["stale"] is True, ctx2["last_plan"]
        return "读得回 order/focus；数据变过之后 stale=True（只给事实）"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_apply_port(cli: str, work: str) -> str:
    """★ 通用写入端口：一份改写意图 → 可跨文件落盘；注册表条目带**自由 detail**。

    设计意图（用户原话）：待办列表是**注册表**；AGENT 把任务/作业/提醒/课程
    总结成 JSON 结构体送进来；图上读到的信息（做哪几题、有什么额外要求）必须完整留存。
    所以内核只管身份/日期/状态，其余交给 detail，且 **state 只给列表视图、show 给详情**。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        p = os.path.join(fresh_work, "patch.json")

        # ① 注册一条带结构体的作业
        _write(p, {"list": {"upsert": [{
            "task": "示例课程C 作业3", "date": _d(3), "due_time": "上课前",
            "detail": {"problems": ["5", "6", "7"], "submission": {"paper": "一页A4"}},
        }]}})
        _, r = _json(fresh_cli, "apply", "--file", p)
        assert r["ok"] and "data.json" in r["written"], r
        assert r["report"]["list"]["added"] == 1, r

        # ② 同名再注册 = **整条替换**（旧字段不残留），不新增
        _write(p, {"list": {"upsert": [{
            "task": "示例课程C 作业3", "date": _d(5),
            "detail": {"problems": ["5", "6", "7", "10"]},
        }]}})
        _, r2 = _json(fresh_cli, "apply", "--file", p)
        assert r2["report"]["list"]["updated"] == 1 and r2["report"]["list"]["added"] == 0, r2
        _, s = _json(fresh_cli, "show", "--task", "示例课程C 作业3")
        assert s["entry"]["date"] == _d(5), s
        assert s["entry"]["detail"]["problems"] == ["5", "6", "7", "10"], s
        assert "due_time" not in s["entry"], f"整条替换应清掉旧字段：{s['entry']}"

        # ③ state 是列表视图：不含 detail，但有 has_detail 标记
        _, ctx = _json(fresh_cli, "context")
        view = next(d for d in ctx["active"]
                    if d["task"] == "示例课程C 作业3")
        assert "detail" not in view and view.get("has_detail") is True, view

        # ④ show 未命中 → 拒绝并给 known
        code, _, err = _run(fresh_cli, "show", "--task", "错名")
        assert code != 0 and "known" in err, err[:200]

        # ⑤ ★ 一处不合规 → 整份不写（同一次 apply 里另一节合法内容也不许落）
        before = open(os.path.join(fresh_work, "data", "data.json"), "rb").read()
        _write(p, {"schedule": {"replace_future": True, "dated_sessions": [
                     {"date": _d(1), "periods": "1-2", "course": "合法课"}]},
                   "list": {"upsert": [{"task": "坏条目", "date": "第6周"}]}})
        code, _, _ = _run(fresh_cli, "apply", "--file", p)
        assert code != 0, "非法条目竟然通过"
        got = _hot(fresh_work, "schedule.json")["dated_sessions"]
        assert not any(x["course"] == "合法课" for x in got), \
            "★ 同一份 apply 里另一节合法内容被写进去了 —— 没做到先全量校验再落盘"
        after = open(os.path.join(fresh_work, "data", "data.json"), "rb").read()
        assert before == after, "失败的 apply 改动了 data.json"
        return "upsert(新增/整条替换) + show 取详情 + state 列表视图；跨文件一处不合规则整份不写"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)
def case_apply_incremental(cli: str, work: str) -> str:
    """★ apply 的**增量**写法：模型只写"要改的那一条"。

    这是按 AI 特性设计的：整段替换要求模型把几十条**原样搬回**，那是它的弱项
    （漏一条、串一行就毁数据）。增量只让它写出真正要改的，其余由内核保持不动。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        p = os.path.join(fresh_work, "p.json")
        before = _hot(fresh_work, "schedule.json")["dated_sessions"]
        before_n = len(before)

        # ① 改一节课的地点：只写这一条
        _write(p, {"schedule": {"sessions": {"upsert": [
                     {"date": _d(1), "periods": "1-2", "course": "示例课程E",
                      "location": "新楼X101"}]}}})
        _, r = _json(fresh_cli, "apply", "--file", p)
        assert r["written"] == ["schedule.json"], r
        after = _hot(fresh_work, "schedule.json")["dated_sessions"]
        assert len(after) == before_n, f"增量改法不该新增/减少条目：{before_n} -> {len(after)}"
        hit = next(s for s in after if s["date"] == _d(1) and s["periods"] == "1-2")
        assert hit["location"] == "新楼X101", hit
        # 其余课次原样未动
        assert [s["course"] for s in after if s["date"] == _d(2)] == ["示例课程A"], after

        # ② 按身份删一节（字符串身份即可）
        _write(p, {"schedule": {"sessions": {"remove": [f"{_d(1)}|1-2"]}}})
        _json(fresh_cli, "apply", "--file", p)
        gone = _hot(fresh_work, "schedule.json")["dated_sessions"]
        assert len(gone) == before_n - 1, f"按身份删失败：{len(gone)}"
        assert not any(s["date"] == _d(1) for s in gone), gone

        # ③ 周期任务 增 / 删
        _write(p, {"schedule": {"periodic_tasks": {"upsert": [
                     {"task": "示例课程A作业", "follow_course": "示例课程A",
                      "due_time": "23:59"}]}}})
        _json(fresh_cli, "apply", "--file", p)
        _, ctx = _json(fresh_cli, "context")
        assert "示例课程A作业" in ctx["periodic_tasks"], ctx["periodic_tasks"]
        _write(p, {"schedule": {"periodic_tasks": {"remove": ["示例课程A作业"]}}})
        _json(fresh_cli, "apply", "--file", p)
        _, ctx2 = _json(fresh_cli, "context")
        assert "示例课程A作业" not in ctx2["periodic_tasks"], ctx2["periodic_tasks"]

        # ④ 规则 增 / 删（按 id）
        _write(p, {"rules": {"planning_rules": {"upsert": [
                     {"id": "rule-999", "kind": "preference",
                      "condition": "中午没课", "body": "优先安排示例课程作业"}]}}})
        _json(fresh_cli, "apply", "--file", p)
        _, ctx3 = _json(fresh_cli, "context")
        assert any(r["id"] == "rule-999" for r in ctx3["planning_rules"]), ctx3["planning_rules"]
        _write(p, {"rules": {"planning_rules": {"remove": ["rule-999"]}}})
        _json(fresh_cli, "apply", "--file", p)
        _, ctx4 = _json(fresh_cli, "context")
        assert not any(r["id"] == "rule-999" for r in ctx4["planning_rules"])

        # ⑤ ★ 写错键名 → 结构化报错，模型能直接照做（而不是一句"命令失败"）
        _write(p, {"schedule": {"sessons": []}})
        code, _, err = _run(fresh_cli, "apply", "--file", p)
        assert code != 0 and json.loads(err)["error"].startswith("schedule 不认识的键"), err[:200]
        return "sessions/periodic_tasks/planning_rules 的 upsert·remove 都生效；错键结构化报错"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_relation_fields(cli: str, work: str) -> str:
    """★ `list.upsert` 只替换**内容**，必须继承**关系字段**。

    关系字段不是内容，而是"这条属于什么"：
      · `session_key`                    —— 对应课表里的哪一节
      · `occurrence_of` / `anchored_to`   —— 属于哪个周期任务、锚在哪次课

    实测踩到（用户真实使用）：改一条周期任务生成的待办时，upsert 把 `occurrence_of`
    丢了 ⇒ 而 `sync` 以为"还没有待办"又生成一条，于是真的变成两条同名条目
    （内核为此在 `apply` 里继承关系字段）。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _, sync1 = _json(fresh_cli, "sync")
        assert [c for c in sync1["created"] if c.get("kind") == "periodic"], sync1["created"]

        def occ():
            return [d for d in _hot(fresh_work, "data.json")["registry"]
                    if d["task"] == "示例课程A作业"]

        before = occ()
        assert len(before) == 1 and before[0].get("occurrence_of"), before

        # 只改 due_time —— 不给 occurrence_of
        p = os.path.join(fresh_work, "p.json")
        _write(p, {"list": {"upsert": [{"task": "示例课程A作业",
                                       "date": before[0]["date"],
                                       "due_time": "周二 23:59 前"}]}})
        _json(fresh_cli, "apply", "--file", p)

        after = occ()
        assert len(after) == 1, f"★ 应该还是一条：{after}"
        assert after[0]["due_time"] == "周二 23:59 前", after
        assert after[0].get("occurrence_of") == before[0]["occurrence_of"], \
            f"★ occurrence_of 丢了：{after[0]}"
        assert after[0].get("anchored_to") == before[0].get("anchored_to"), after[0]

        # 再 sync 不该多生成一条（内核靠 occurrence_of 判断"已经有待办了"）
        _json(fresh_cli, "sync")
        assert len(occ()) == 1, f"★ sync 又生成了一条：{occ()}"

        # ★ 内核不在 anomalies 里做"重复"判断（红线）；它只在**操作时**报命中几条。
        #   这里只有一条 ⇒ matched 必须是 1，模型据此确知"改干净了"。
        _, m = _json(fresh_cli, "mark", "--task", before[0]["task"], "--status", "done")
        assert m["matched"] == 1 and m["changed"] == 1, m
        assert "note" not in m, f"只有一条时不该有歧义提示：{m}"
        return ("upsert 换内容但继承 occurrence_of/anchored_to；sync 不重复生成；"
                "mark 报 matched=1（无歧义）")
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_urgency(cli: str, work: str) -> str:
    """★ 紧迫度按**小时**算（不是天），且视图不再给不含信息的字段。

    起因（用户，两轮）：
      ① 清单上的条目**必然**未完成 —— `status: pending` 零信息；`priority` 默认填
         medium 更是编出来的噪声。轻重应该**算**出来，并给出理由。
      ② **规划要基于当前时刻** —— 早上 8 点和晚上 11 点问待办，答案必须不同。

    断言刻意写成**不受"现在几点"影响**的形式（比顺序、比理由的措辞），
    否则这套用例自己就会在半夜挂掉。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        p = os.path.join(fresh_work, "p.json")
        _write(p, {"list": {"upsert": [
            {"task": "今天", "date": _d(0), "due_time": "23:59"},
            {"task": "明天", "date": _d(1)},
            {"task": "五天后", "date": _d(5)},
            {"task": "三个月后", "date": _d(90)},
            {"task": "对照组（同日期不提前）", "date": _d(4)},
            {"task": "要提前准备", "date": _d(4), "lead_days": 3},
        ]}})
        _json(fresh_cli, "apply", "--file", p)
        _, ctx = _json(fresh_cli, "context")

        # ① 读取输出带"现在" —— 规划的时间参照，模型不该自己估
        n = ctx["now"]
        assert n.get("date") and n.get("time") and n.get("phase"), n
        assert n.get("iso") and "+" in n["iso"], n

        view = {x["task"]: x for x in ctx["active"]}
        # ★ 午夜边界：「今天」这条的截止是当天 23:59，23:59 那一分钟它已"过点"、
        #   会从活跃集消失（过点即翻篇）。那不是缺陷 —— 那一分钟内跳过与它相关的断言。
        _n2 = datetime.now()
        last_minute = _n2.hour == 23 and _n2.minute >= 59
        if last_minute:
            view.pop("今天", None)
        for name, x in view.items():
            assert "status" not in x, f"★ {name} 还有冗余字段：{x}"
            # `priority` 默认 medium 是噪声 ⇒ 视图里只该出现**非默认**的；`lead_days` 同理
            assert x.get("priority") != "medium", f"★ {name} 的默认 priority 是噪声：{x}"
            assert not x.get("lead_days") or x["lead_days"], x
            assert x.get("urgency") and x.get("why"), x

        # ② 越近越紧急 —— 比**顺序**，不受当前时刻影响
        order = [x["task"] for x in ctx["active"]]
        pairs = [("今天", "明天"), ("明天", "五天后"), ("五天后", "三个月后")]
        for a, b in pairs:
            if a not in order:
                continue                       # 午夜那一分钟「今天」不在
            assert order.index(a) < order.index(b), f"{a} 应排在 {b} 之前：{order}"

        # ③ 近期用小时/分钟说，远期才说天
        first_why = view.get("今天", {}).get("why", "")
        if first_why:                          # 午夜那一分钟「今天」不在活跃集
            assert "还剩" in first_why, first_why
        assert "天" in view["三个月后"]["why"], view["三个月后"]["why"]

        # ④ lead_days 抬高档位并写进理由（语义给模型、算术给内核）
        assert order.index("要提前准备") < order.index("对照组（同日期不提前）"), order
        # ★ `why` 是 **DDL 那一行**的标签，只放时间（用户 2026-10-17：
        #   "需要提前几天准备这个信息为什么要进DDL DDL单纯就是一个时间才对啊"）
        #   ⇒ 提前准备/优先级**不进标签**，走独立字段。
        _why = view["要提前准备"]["why"]
        assert "提前" not in _why, _why
        assert view["要提前准备"].get("lead_days") == 3, view["要提前准备"]
        assert "提前" not in view["对照组（同日期不提前）"]["why"]
        assert "优先" not in view["要提前准备"]["why"]

        # ⑤ 课次视图也按时刻：带 starts_in；已下课的不会出现
        _, s = _json(fresh_cli, "sessions", "--days", "9")
        assert s["now"]["date"] == n["date"], s["now"]
        assert s["now"]["time"], s["now"]
        for c in s["sessions"]:
            assert c.get("starts_in"), f"课次缺 starts_in：{c}"

        # ⑥ anomalies 不再重复报"即将到期"（那是 active 的活）
        _, a = _json(fresh_cli, "anomalies")
        assert not any(x["type"] == "expiring_deadline" for x in a["anomalies"]), a["anomalies"]
        return "读取带 now；紧迫度按小时、越近越急；lead_days 抬档并进理由；课次带 starts_in"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_compact(cli: str, work: str) -> str:
    """★ 冷存储：过期内容搬出热文件（幂等，且**搬走 ≠ 丢**）。

    起因：滚转只是**视图过滤** —— "过期的看不见"不等于"没占地方"。
    `dated_sessions` 全量交给模型 ⇒ 上下文随使用时间线性增长。所以 sync 要把
    过期内容搬到 archive/YYYY-MM.json（按月分文件），热文件保持常数大小；history / show 仍取得到。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        def hot(name):
            path = os.path.join(fresh_work, "data", name)
            return json.loads(open(path, encoding="utf-8").read())

        # ① 5 节"一个月前"的课 + 1 节将来的课
        p = os.path.join(fresh_work, "s.json")
        _write(p, {"schedule": {
            "periods": {"1": "08:00-08:45", "2": "08:55-09:40"},
            "replace_future": True, "dated_sessions": [{"date": _d(-30 - i), "periods": "1-2",
                                "course": f"旧课{i}"} for i in range(5)]
                               + [{"date": _d(2), "periods": "1-2", "course": "将来的课"}]}})
        _json(fresh_cli, "apply", "--file", p)

        # ② 一条很久以前的注册（带 detail）
        p2 = os.path.join(fresh_work, "d.json")
        _write(p2, {"list": {"upsert": [{"task": "很久以前的事", "date": _d(-40),
                                         "detail": {"note": "要留得住"}}]}})
        _json(fresh_cli, "apply", "--file", p2)

        _, s1 = _json(fresh_cli, "sync")
        assert s1["compacted"]["sessions"] == 5, s1["compacted"]
        assert s1["compacted"]["registry"] >= 1, s1["compacted"]

        # ③ 热文件只剩将来的；保留窗口内的旧条目仍留着
        sch = hot("schedule.json")
        assert [x["course"] for x in sch["dated_sessions"]] == ["将来的课"], \
            sch["dated_sessions"]
        dat = hot("data.json")
        assert not any(d["task"] == "很久以前的事" for d in dat["registry"]), \
            dat["registry"]
        assert any(d["task"] == "结课论文" for d in dat["registry"]), \
            "保留窗口内（_d(-5)）的条目不该被搬走"

        # ④ 冷存储里在
        arch = _archive_all(fresh_work)
        assert len(arch["sessions"]) == 5, arch["sessions"]
        assert "很久以前的事" in [x["task"] for x in arch["registry"]], arch["registry"]

        # ⑤ ★ 搬走 ≠ 丢：show / history 仍取得到
        #    （但**归档条目默认不回 detail** —— 要看必须显式 --cold，见 case_bounded_views）
        _, sh = _json(fresh_cli, "show", "--task", "很久以前的事")
        assert sh["archived"] is True and "detail" not in sh["entry"], sh
        _, shc = _json(fresh_cli, "show", "--task", "很久以前的事", "--cold")
        assert shc["entry"]["detail"]["note"] == "要留得住", shc
        _, h = _json(fresh_cli, "history", "--days", "90")
        assert "很久以前的事" in [x["task"] for x in h["entries"]], h

        # ⑥ 交给模型的课次视图有界（过期的一律不出现）
        _, sv = _json(fresh_cli, "sessions", "--days", "400")
        assert [x["course"] for x in sv["sessions"]] == ["将来的课"], sv["sessions"]

        # ⑦ 幂等：再 sync 不重复搬
        _, s2 = _json(fresh_cli, "sync")
        assert s2["compacted"] == {"sessions": 0, "registry": 0}, s2["compacted"]
        assert len(_archive_all(fresh_work)["sessions"]) == 5, "重复搬运导致冷存储膨胀"
        return "过期课次/注册搬入冷存储；热文件只剩将来；show/history 仍可取；重复 sync 不重复搬"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_rejected_write_no_side_effect(cli: str, work: str) -> str:
    """**被拒绝的写操作不得改动磁盘。**

    真实踩到：写命令是"先 import datastore，再校验参数"，而 DataStore.__init__
    曾经会写盘刷新 meta —— 于是一次**被正确拒绝**的 mark 也让 data.json 变了。
    内容没坏，但"拒绝 = 零副作用"这条性质被破坏。
    """
    import hashlib
    fresh_cli, fresh_work = _fresh()
    try:
        paths = [os.path.join(fresh_work, "data", n)
                 for n in ("data.json", "schedule.json", "rules.json")]
        before = [hashlib.sha256(open(p, "rb").read()).hexdigest() for p in paths]

        code, _, _ = _run(fresh_cli, "mark", "--task", "不存在的任务", "--status", "done")
        assert code != 0, "名称不一致的 mark 本应被拒绝"

        # 三条**校验不通过**的 apply：先全量校验再落盘 ⇒ 一个字节都不该写
        for payload in (
            {"list": {"upsert": [{"task": "坏日期", "date": "第6周"}]}},
            {"schedule": {"replace_future": True, "dated_sessions": [{"date": _d(1), "periods": "1-2"}]}},  # 缺 course
            {"list": {"upsert": [{"task": "合法名", "date": _d(3)}]}, "nope": {}},   # 不认识的节
            # ★ 旧字段 workload 已并入 detail：再来一个必须被拒（守门：单一载荷字段）
            {"list": {"upsert": [{"task": "带旧字段", "date": _d(2),
                                  "workload": {"hints": "旧写法"}}]}},
        ):
            code, _, _ = _apply_err(fresh_cli, fresh_work, payload)
            assert code != 0, f"{payload} 本应被拒绝"

        after = [hashlib.sha256(open(p, "rb").read()).hexdigest() for p in paths]
        assert before == after, "被拒绝的写操作改动了文件"
        return "四条拒绝路径对三个文件均零改动"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_validate_reports_all(cli: str, work: str) -> str:
    """validate 必须覆盖三个文件，并在坏数据上给出 FAIL。"""
    fresh_cli, fresh_work = _fresh()
    try:
        _, r = _json(fresh_cli, "validate")
        assert set(r) >= {"rules.json", "schedule.json", "data.json", "all_ok"}, r
        assert r["all_ok"] is True, r

        _write(os.path.join(fresh_work, "data", "schedule.json"), {"semester": "X"})
        _, r2 = _json(fresh_cli, "validate")
        assert r2["all_ok"] is False and r2["schedule.json"].startswith("FAIL"), r2
        return "三个文件都被校验；坏数据报 FAIL"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


# ────────────────────────── 运行器 ──────────────────────────

def case_no_side_effects(cli: str, work: str) -> str:
    """只读与拒绝的写都零副作用

    合并自：case_readonly_no_side_effect、case_rejected_write_no_side_effect（**委派**调用，断言一条不丢）。
    """
    r = []
    r.append(case_readonly_no_side_effect(cli, work))
    r.append(case_rejected_write_no_side_effect(cli, work))
    return " / ".join(r)


def case_plan_lifecycle(cli: str, work: str) -> str:
    """★ 计划的整条命：读得回 / 只给事实 / 文件有界 / 写作废

    合并自：case_plan_is_readable、case_plan_view_contract、case_plan_snapshot_bounded、case_write_invalidates_plan（**委派**调用，断言一条不丢）。
    """
    r = []
    r.append(case_plan_is_readable(cli, work))
    r.append(case_plan_view_contract(cli, work))
    r.append(case_plan_snapshot_bounded(cli, work))
    r.append(case_write_invalidates_plan(cli, work))
    return " / ".join(r)


def case_timetable_bulk(cli: str, work: str) -> str:
    """★ 一学期课表整条路径（写入 / 物化窗口 / 视图 / 上下文）

    合并自：case_materialize_window、case_big_timetable（**委派**调用，断言一条不丢）。
    """
    r = []
    r.append(case_materialize_window(cli, work))
    r.append(case_big_timetable(cli, work))
    return " / ".join(r)


def case_no_ddl_lifecycle(cli: str, work: str) -> str:
    """★ 无 DDL 的条目：不驱动紧迫度 / 排最后 / 不消失 / 往后延

    合并自：case_ddl_or_nothing、case_soft_task_ranks_last、case_roll_forward_not_delete（**委派**调用，断言一条不丢）。
    """
    r = []
    r.append(case_ddl_or_nothing(cli, work))
    r.append(case_soft_task_ranks_last(cli, work))
    r.append(case_roll_forward_not_delete(cli, work))
    return " / ".join(r)


def case_schedule_edits(cli: str, work: str) -> str:
    """课表增改删：增量 + 整批替换（原子）

    合并自：case_schedule_incremental、case_schedule_replace（**委派**调用，断言一条不丢）。
    """
    r = []
    r.append(case_schedule_incremental(cli, work))
    r.append(case_schedule_replace(cli, work))
    return " / ".join(r)


def case_anomalies_suite(cli: str, work: str) -> str:
    """异常清单：检出 / 空课表明说 / 随状态更新 / 久未维护

    合并自：case_anomalies_detects、case_anomalies_empty_schedule、case_anomalies_follow_state、case_dormant_data（**委派**调用，断言一条不丢）。
    """
    r = []
    r.append(case_anomalies_detects(cli, work))
    r.append(case_anomalies_empty_schedule(cli, work))
    r.append(case_anomalies_follow_state(cli, work))
    r.append(case_dormant_data(cli, work))
    return " / ".join(r)


def case_archive_and_history(cli: str, work: str) -> str:
    """★ 冷存储与历史视图：搬得出去、查得回来、视图有界

    合并自：case_compact、case_bounded_views（**委派**调用，断言一条不丢）。
    """
    r = []
    r.append(case_compact(cli, work))
    r.append(case_bounded_views(cli, work))
    return " / ".join(r)


def case_write_port(cli: str, work: str) -> str:
    """★ 写入口：增量只写要改的 + 整份重写原子

    合并自：case_apply_incremental、case_atomic_write（**委派**调用，断言一条不丢）。
    """
    r = []
    r.append(case_apply_incremental(cli, work))
    r.append(case_atomic_write(cli, work))
    return " / ".join(r)


def case_rejections(cli: str, work: str) -> str:
    """拒绝：非法日期 / 名字不完全一致

    合并自：case_reject_invalid_date、case_conservative_match（**委派**调用，断言一条不丢）。
    """
    r = []
    r.append(case_reject_invalid_date(cli, work))
    r.append(case_conservative_match(cli, work))
    return " / ".join(r)



def case_loop_gate(cli: str, work: str) -> str:
    """★ 循环闸：反复重排要被**工具**拦住，不能只靠提示词。

    实测代价（会话 7cb5b775）：一轮里 qyn_brief × 11、qyn_plan_snapshot × 8 ——
    8 分钟内存了 8 份快照，一次回合烧掉几十万 token。

    ★ 语义（审查后定的）：循环闸**不再无条件抢在 problems 前面**。只要 30 分钟内存了
      3 份快照就拒绝给正文，会让"用户 10 分钟内连提三次修改、规划其实完全成立"也去报
      "排不下" —— 那是**谎报**。现在：有阻塞才拦，没阻塞照发正文 + 一句提示。

    合并自 case_loop_gate 与 case_loop_gate_thresholds：两条测的是同一机制，这里合成一条
    （阈值 + 去重 + 只补 allow），断言一条不丢；并统一改用 `_fresh()`，不再依赖共享工作区里
    前面攒下的快照（原来那条是顺序相关的）。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        def store(i):
            return _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(
                fresh_work, "s%d.json" % i, {"slots": [
                    {"date": "2099-03-0%d" % (i + 1), "time": "10:00-11:00",
                     "task": "照天排的活"}]}))

        # ① 2 份 ⇒ 正常，不报循环
        store(0)
        store(1)
        _, b2 = _json(fresh_cli, "brief", "--kind", "plan")
        assert b2["ok"] is True, b2
        assert not [w for w in b2.get("warnings", []) if w.get("kind") == "loop"], \
            "只有 2 份快照不该报循环：%s" % b2.get("warnings")

        # ② 3 份、且**没有阻塞** ⇒ 照给正文，只附一句"别再重排了"
        store(2)
        _, b3 = _json(fresh_cli, "brief", "--kind", "plan")
        assert b3["ok"] is True and "text" in b3, "规划本身没问题时不该拦：%s" % b3
        loop = [w for w in b3.get("warnings", []) if w.get("kind") == "loop"]
        assert loop and loop[0]["recent_snapshots"] >= 3, b3.get("warnings")

        # ③ 3 份以上 + 这份**有阻塞** ⇒ 拦住，并把 problems 一起给出来（别只说"排不下"）
        _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "cls.json", {"schedule": {
            "replace_future": True, "dated_sessions": [{"date": "2099-03-01", "time": "08:00-09:30", "course": "甲课"}]}}))
        _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(
            fresh_work, "clash.json", {"slots": [
                {"date": "2099-03-01", "time": "09:00-10:00", "task": "撞课"}]}))
        _, b4 = _json(fresh_cli, "brief", "--kind", "plan")
        assert b4["ok"] is False and b4.get("kind") == "loop", "有阻塞时的循环闸没生效：%s" % b4
        assert "text" not in b4, "被拦住时不该给正文"
        assert b4.get("problems"), "拦住时必须把 problems 一起给出来（否则模型只会谎报排不下）"
        assert any(p.get("kind") == "clashes_class" for p in b4["problems"]), b4["problems"]

        # ④ 同一份内容不重复存
        same = {"slots": [{"date": "2099-03-03", "time": "10:00-11:00", "task": "活2"}]}
        _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "dup1.json", same))
        _, d2 = _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "dup2.json", same))
        assert d2.get("action") == "snapshot_unchanged" and d2.get("stored") is False, \
            "去重没生效：%s" % d2

        # ⑤ ★ 去重不能把"只补 allow 豁免"的修正吃掉（技能教的补救路径正是它）
        _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "allow1.json", {
            "slots": same["slots"], "allow": ["rule-004"]}))
        _, a2 = _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "allow2.json", {
            "slots": same["slots"], "allow": ["rule-004", "rule-005"]}))
        assert a2.get("action") == "snapshot_saved" and a2.get("stored") is not False, \
            "只改 allow 的修正被去重吃掉了（补救路径失效）：%s" % a2
        return ("2 份⇒正常；3 份无阻塞⇒照发+loop；3 份有阻塞⇒ok=false/kind=loop+problems；"
                "同一份不重复存；只补 allow 能存进去")
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)



def case_wall_view(cli: str, work: str) -> str:
    """★ 大屏只读视图 `wall`：结构（含每块的 why）+ 过滤 / 保留 / 排序。

    合并自：case_wall_readonly_view、case_wall_filter_and_order（**委派**调用，断言一条不丢）。
    """
    r = []
    r.append(case_wall_readonly_view(cli, work))
    r.append(case_wall_filter_and_order(cli, work))
    return " / ".join(r)


def case_brief_guards(cli: str, work: str) -> str:
    """★ `brief` / `review` 的把关族：撞课/重叠/漏排、课间硬约束、排到截止之后、畸形快照。

    合并自：case_review_conflicts、case_gap_is_hard_constraint、case_brief_catches_unscheduled、
    case_past_deadline、case_malformed_snapshot_no_crash（**委派**调用，断言一条不丢）。
    """
    r = []
    r.append(case_review_conflicts(cli, work))
    r.append(case_gap_is_hard_constraint(cli, work))
    r.append(case_brief_catches_unscheduled(cli, work))
    r.append(case_past_deadline(cli, work))
    r.append(case_malformed_snapshot_no_crash(cli, work))
    return " / ".join(r)


def case_review_conflicts(cli: str, work: str) -> str:
    """★ 排完自检：撞课 / 时段重叠 / 漏排都报得出来；干净草稿 ok=true。

    ★ 它只做**时间算术** —— 不报"违反规则"（规则是自然语言，内核不解释）。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _, s = _json(fresh_cli, "sessions", "--days", "14")
        cls = [x for x in s["sessions"] if x.get("time") and x.get("kind") != "cancelled"]
        assert cls, "夹具里应有带时间的课次"
        c = cls[0]
        bad = [{"date": c["date"], "time": c["time"], "task": "撞课的草稿"},
               {"date": c["date"], "time": c["time"], "task": "自重叠的草稿"}]
        p1 = os.path.join(fresh_work, "draft.json")
        _write(p1, {"slots": bad})
        _, r = _json(fresh_cli, "review", "--file", p1)
        assert r["ok"] is False, "撞课必须让 ok=false"
        kinds = {x["kind"] for x in r["conflicts"]}
        assert "clashes_class" in kinds, f"未报撞课；实际 {kinds}"
        assert "overlap" in kinds, f"未报时段自重叠；实际 {kinds}"
        # ★ 自己造一条"有 DDL 但没排"的（不依赖夹具里恰好有什么）
        _, ap = _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "reg.json",
                      {"list": {"upsert": [{"task": "漏排的作业", "date": "2099-01-02",
                                            "due_time": "23:59"}]}}))
        assert ap["ok"] is True, ap
        _, r1 = _json(fresh_cli, "review", "--file", p1)
        assert any(u["task"] == "漏排的作业" and u["why"] == "有 DDL 却没有任何时段"
                   for u in r1["unscheduled"]), f"有 DDL 却没排的要报出来：{r1['unscheduled']}"

        # ★ 规则时段：no_slots 是结构化的 ⇒ 审查要能机械校验（自然语言的 body 内核不解释）
        _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "rule.json", {
            "rules": {"planning_rules": {"upsert": [
                {"id": "rule-t1", "kind": "constraint", "condition": "午休",
                 "body": "12:10-12:55 不排任务", "no_slots": ["12:10-12:55"]},
                {"id": "rule-t2", "kind": "preference", "condition": "深夜",
                 "body": "深夜尽量不排", "no_slots": ["00:00-06:00"]}]}}}))
        p3 = os.path.join(fresh_work, "draft3.json")
        _write(p3, {"slots": [{"date": "2099-01-02", "time": "12:30-12:50", "task": "撞硬约束"},
                              {"date": "2099-01-02", "time": "03:00-03:30", "task": "撞软偏好"}]})
        _, r3 = _json(fresh_cli, "review", "--file", p3)
        assert r3["ok"] is False, "硬约束被违反必须 ok=false"
        assert any(c.get("kind") == "violates_rule" and c.get("rule") == "rule-t1"
                   for c in r3["conflicts"]), f"没报硬约束违反：{r3['conflicts']}"
        assert any(w.get("rule") == "rule-t2" for w in r3.get("warnings", [])), \
            f"没报软偏好警告：{r3.get('warnings')}"
        assert not any(c.get("rule") == "rule-t2" for c in r3["conflicts"]), \
            "软偏好不该进 conflicts"

        p2 = os.path.join(fresh_work, "draft2.json")
        _write(p2, {"slots": [{"date": "2099-01-01", "time": "08:00-09:00",
                               "task": "干净草稿"}]})
        _, r2 = _json(fresh_cli, "review", "--file", p2)
        assert r2["ok"] is True, f"干净草稿不该报冲突：{r2['conflicts']}"
        return f"撞课/重叠/漏排都报出（{sorted(kinds)}）；干净草稿 ok=true"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def _mkdraft(work: str, name: str, payload) -> str:
    """把草稿写成 JSON 文件并返回路径（review / apply 都走 --file，避开 shell 编码）。"""
    p = os.path.join(work, name)
    _write(p, payload)
    return p


def case_gap_is_hard_constraint(cli: str, work: str) -> str:
    """★ 课间禁排 = **硬约束**（用户 2026-10-15）。

    用户原话：昨天出现一次直接排在课程上的 —— 开始时间排在课间，然后一整个小时把后面的
    课程覆盖掉；"把课间时间禁止排任何任务也作为一个硬约束。我们课件一般需要改课时，
    需要转场，没时间写东西的。"

    判据：整块落在**同一天两节课之间**、且空隙 < 60 分钟 ⇒ 冲突（不是警告）。
    长空隙（如午休 2.5 小时）不算课间，正常可用。
    """
    d = "2099-05-05"
    _json(cli, "apply", "--file", _mkdraft(work, "gapsched.json", {"schedule": {
        "replace_future": True, "dated_sessions": [
            {"date": d, "time": "08:00-09:30", "course": "甲课"},
            {"date": d, "time": "10:00-11:30", "course": "乙课"}]}}))
    # 课间（09:30-10:00 空隙，30 分钟）里的一块 ⇒ 必须报冲突
    _, r1 = _json(cli, "review", "--file", _mkdraft(work, "gapin.json", {"slots": [
        {"date": d, "time": "09:35-09:55", "task": "课间硬塞"}]}))
    kinds = [c.get("kind") for c in r1["conflicts"]]
    assert "in_class_gap" in kinds, "课间块没被硬拦：%s" % r1["conflicts"]
    assert r1["ok"] is False, "有课间块时 ok 必须为 false"
    # 长空隙（11:30 之后，与两节课都不相邻）⇒ 不该报课间
    _, r2 = _json(cli, "review", "--file", _mkdraft(work, "gapout.json", {"slots": [
        {"date": d, "time": "12:00-13:00", "task": "长空隙可用"}]}))
    assert "in_class_gap" not in [c.get("kind") for c in r2["conflicts"]], \
        "长空隙被误判成课间：%s" % r2["conflicts"]
    # 整块压住后面的课 ⇒ 撞课（这条本来就有，防止课间改造把它弄丢）
    _, r3 = _json(cli, "review", "--file", _mkdraft(work, "gapcross.json", {"slots": [
        {"date": d, "time": "09:40-10:40", "task": "跨课块"}]}))
    assert "clashes_class" in [c.get("kind") for c in r3["conflicts"]], \
        "跨课块没报撞课：%s" % r3["conflicts"]

    # ★★ brief 侧也必须拦 —— 这一条曾经完全缺失：brief 的课间判据拿**同一节课**当
    #    "前一节"和"后一节"（恒不成立），于是同一份 slots 过 review 报冲突、过 brief
    #    却 ok=true 把课间块照发正文。审查时四个子代理里有两个独立复现了它。
    cli2, work2 = _fresh()
    _json(cli2, "apply", "--file", _mkdraft(work2, "gapsched.json", {"schedule": {
        "replace_future": True, "dated_sessions": [
            {"date": d, "time": "08:00-09:30", "course": "甲课"},
            {"date": d, "time": "10:00-11:30", "course": "乙课"}]}}))
    _json(cli2, "plan-snapshot", "--file", _mkdraft(work2, "gap.json", {"slots": [
        {"date": d, "time": "09:35-09:55", "task": "课间硬塞"}]}))
    _, rb = _json(cli2, "brief", "--kind", "plan")
    assert rb["ok"] is False, "brief 没拦住课间块（判据是死代码？）：%s" % rb
    assert "in_class_gap" in [p.get("kind") for p in rb.get("problems", [])], \
        "brief 的 problems 里没有 in_class_gap：%s" % rb.get("problems")
    assert "text" not in rb, "有硬约束违反时不该给正文"
    # 同一份 slots 过两个命令，判据必须一致（这是"同一规则两份实现"的守门断言）
    rk = [c.get("kind") for c in r1["conflicts"]]
    rp = [p.get("kind") for p in rb["problems"]]
    assert "in_class_gap" in rk and "in_class_gap" in rp, \
        "review 与 brief 的课间口径不一致：%s vs %s" % (rk, rp)
    return "课间=硬约束（<60 分钟的空隙禁排）；长空隙可用；跨课仍报撞课；**brief 与 review 同口径**"


def case_wall_readonly_view(cli: str, work: str) -> str:
    """★ 大屏只读视图 `wall`：规划快照的完整结构，**含每块的 why**。

    踩到过：`_plan_view`（context.last_plan）只取 date/time/task ⇒ slots[].why 被丢弃，
    而技能契约明写这个字段、历史快照 261 个 slot 里 260 个都带它。
    `wall` 是给外部（大屏）的只读出口：不截断、不省字段、零副作用。
    """
    import datetime as _dt
    d0 = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
    payload = {
        "focus": "测试重心", "horizon": d0, "order": ["带理由的活", "没理由的活"],
        "slots": [
            {"date": d0, "time": "09:00-10:00", "task": "带理由的活",
             "why": "因为上午只有这个空档"},
            {"date": d0, "time": "14:00-15:00", "task": "没理由的活"},
        ],
    }
    _json(cli, "plan-snapshot", "--file", _mkdraft(work, "wall.json", payload))
    _, w = _json(cli, "wall", "--days", "7")
    assert w["focus"] == "测试重心", w
    assert w["order"] == ["带理由的活", "没理由的活"], w
    slots = {x["task"]: x for x in w["slots"]}
    assert slots["带理由的活"]["why"] == "因为上午只有这个空档", slots
    assert "why" not in slots["没理由的活"], "缺 why 时应省略，不该写空串：%s" % slots["没理由的活"]
    assert slots["带理由的活"]["date"] == d0 and slots["带理由的活"]["time"] == "09:00-10:00"
    assert "now" in w and "created_at" in w and "horizon" in w

    # ★ DDL 与规划的关系（用户 2026-10-15：严重的 DDL/规划冲突要标红；
    #   任务 DDL 进红区时那块要**跟着闪**）—— 大屏标红的数据来源，必须由内核给。
    #   这里造一件"明早 10:10 上课前交"的作业，然后把它排到**当天晚上**（截止之后）。
    import datetime as _dt2
    d1 = (_dt2.date.today() + _dt2.timedelta(days=2)).isoformat()
    _apply(cli, work, {"list": {"upsert": [
        {"task": "排到截止之后的作业", "date": d1, "due_time": "10:10 上课前"}]}})
    _json(cli, "plan-snapshot", "--file", _mkdraft(work, "late.json", {"slots": [
        {"date": d1, "time": "19:00-20:00", "task": "排到截止之后的作业"}]}))
    _, w2 = _json(cli, "wall", "--days", "7")
    s2 = {x["task"]: x for x in w2["slots"]}["排到截止之后的作业"]
    assert s2.get("flags") == ["past_deadline"], "越过后没打标记：%s" % s2
    assert s2.get("ddl") == d1 + " 10:10", s2
    assert isinstance(s2.get("ddl_in_minutes"), int) and s2["ddl_in_minutes"] > 0, s2
    assert w2.get("ok") is False, "整份规划该标红（ok=false）：%s" % w2.get("ok")
    assert [p.get("kind") for p in (w2.get("problems") or [])] == ["past_deadline"], w2.get("problems")

    # 挪到截止之前 ⇒ 标记消失、规划重新 ok
    _json(cli, "plan-snapshot", "--file", _mkdraft(work, "ontime.json", {"slots": [
        {"date": d1, "time": "08:00-09:00", "task": "排到截止之后的作业"}]}))
    _, w3 = _json(cli, "wall", "--days", "7")
    s3 = {x["task"]: x for x in w3["slots"]}["排到截止之后的作业"]
    assert "flags" not in s3, "排得进却还打标记：%s" % s3
    assert w3.get("ok") is True and not w3.get("problems"), w3
    return ("wall 只读返回完整快照：why 有就带、没有就省略；不截断；"
            "每块带 DDL 事实（ddl/ddl_in_minutes/flags），整份带 problems/ok")






def case_start_time(cli: str, work: str) -> str:
    """★ 「周三开始、周五结束」—— `start` 与 `date` 必须分开记（用户 2026-10-27）。

    原先只有 `date`（DDL）一个日期 ⇒ Agent 把 DDL 当开始、或者干脆丢掉"什么时候才能开始"。
    现在：`start` = 最早可开始（可选 `start_time`），`date` = 截止；
    只读视图算出 `starts_in` / `can_start`；排在 `start` 之前的块被 `review` 拦（`before_start`）；
    `start > date`（写反）进 `anomalies`（`start_after_due`）。
    """
    fresh_cli, fresh_work = _fresh()
    d_start, d_due = _d(3), _d(5)
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "p.json", {"list": {"upsert": [{
        "task": "周三才能开始的事", "start": d_start, "date": d_due,
        "due_time": "23:59", "status": "pending"}]}}))
    _, ctx = _json(fresh_cli, "context")
    row = [a for a in ctx["active"] if a["task"] == "周三才能开始的事"][0]
    assert row.get("start") == d_start, row
    assert row.get("can_start") is False, row
    assert "才能开始" in str(row.get("starts_in")), row

    # 排在 start 之前 ⇒ before_start
    rv = {"slots": [{"date": _d(1), "time": "20:00-21:00", "task": "周三才能开始的事"}]}
    _, out = _json(fresh_cli, "review", "--file", _mkdraft(fresh_work, "r.json", rv))
    assert not out["ok"], out
    assert any(c.get("kind") == "before_start" for c in out["conflicts"]), out["conflicts"]

    # 排在 start 当天 ⇒ 通过
    rv2 = {"slots": [{"date": d_start, "time": "20:00-21:00", "task": "周三才能开始的事"}]}
    _, out2 = _json(fresh_cli, "review", "--file", _mkdraft(fresh_work, "r2.json", rv2))
    assert out2["ok"], out2

    # start 晚于 date ⇒ anomalies 报 start_after_due
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "p2.json", {"list": {"upsert": [{
        "task": "日期写反的", "start": _d(9), "date": _d(2), "status": "pending"}]}}))
    _, an = _json(fresh_cli, "anomalies")
    assert any(a.get("type") == "start_after_due" for a in an["anomalies"]), an["anomalies"]
    return "start 独立于 date；starts_in/can_start 算好；before_start 被拦；start>date 进 anomalies"

def case_horizon_check(cli: str, work: str) -> str:
    """★ 课表**覆盖不足**要机械报出来（2026-10-21 事故的根因）。

    实测事故：课表只到 10/23，示例课程A下一节在 10/26 ⇒ 周期任务锚不上 ⇒ Agent 就近拿了
    10/23 那节**示例课程课**当锚，把示例课程A作业登记成了示例课程。
    判据是纯日期算术（最远课次 − 今天），所以归内核报，不靠模型"注意"。
    """
    fresh_cli, fresh_work = _fresh()
    d1 = _d(1)
    # ① 只覆盖到明天 ⇒ 必须报
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "s.json", {"schedule": {
        "replace_future": True,
        "dated_sessions": [{"date": d1, "periods": "1-2", "course": "甲课"}]}}))
    _, an = _json(fresh_cli, "anomalies")
    hits = [a for a in an["anomalies"] if a["type"] == "schedule_horizon"]
    assert hits, "课表只覆盖到明天，该报 schedule_horizon：%s" % an["anomalies"]
    assert 0 < hits[0]["days_left"] < 14, hits[0]
    assert "补到至少" in hits[0]["detail"], hits[0]

    # ② 覆盖到 20 天后 ⇒ 不再报
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "s2.json", {"schedule": {
        "replace_future": True,
        "dated_sessions": [
            {"date": d1, "periods": "1-2", "course": "甲课"},
            {"date": _d(20), "periods": "1-2", "course": "乙课"}]}}))
    _, an2 = _json(fresh_cli, "anomalies")
    assert not [a for a in an2["anomalies"] if a["type"] == "schedule_horizon"], an2["anomalies"]
    return "课表覆盖 < 14 天 ⇒ 报 schedule_horizon；覆盖够 ⇒ 不报"

def case_write_lock(cli: str, work: str) -> str:
    """★ 写命令有**互斥写锁**：锁被别的进程占着时，写必须明确报错，不能照写不误。

    另一个 agent 转来的缺口（用户 2026-10-20）：「如果将来有第四个入口直接调 qyn.py
    （不经桥也不经 tick.sh），它不会拿这把锁」。桥与 tick.sh 共用的是**整轮锁**
    （`logs/agent-run.lock`，管"同时只跑一轮"）；内核补的是**独立的写锁**
    （`logs/qyn-write.lock`，管"同时只有一个进程改数据"）——两者不能合并：
    tick.sh 持有一整轮，Agent 在**这一轮里**调内核写，复用整轮锁会**等自己**（死锁）。
    """
    fresh_cli, fresh_work = _fresh()
    lock_path = os.path.join(fresh_work, "logs", "qyn-write.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fh = open(lock_path, "a+")
    # 用与内核同一套方式占住写锁
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        code, _out, err = _run(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "p.json", {
            "list": {"upsert": [{"task": "锁被占时不该写进去"}]}}))
        assert code != 0, "写锁被占用时 apply 竟然成功了"
        assert "正在写数据" in err, "锁被占要明确说清：%s" % err[:220]
        # 只读命令不受影响（它不拿写锁）
        code_r, _o2, _e2 = _run(fresh_cli, "context")
        assert code_r == 0, "只读命令不该被写锁挡住"
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    # 锁释放之后，同一个写命令必须能过
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "p2.json", {
        "list": {"upsert": [{"task": "锁释放后能写"}]}}))
    _, ctx = _json(fresh_cli, "context")
    names = [x["task"] for x in ctx["active"]]
    assert "锁释放后能写" in names, names
    return "写锁被占 ⇒ 写命令明确报错、只读不受影响；释放后能写"

def case_plan_stale_inputs(cli: str, work: str) -> str:
    """★ 只改**课表/规则**也要让规划快照作废（`stale`）—— 2026-10-16 课表事故的连带问题。

    实测经过：课表被整段替换冲掉、恢复之后，`apply` 回了 `plan_invalidated: true`，
    但 `context.last_plan.stale` 仍是 **false**（判据只看 `data.json` 的 `meta.last_updated`，
    而课表改动不碰它）⇒ Agent 判定"计划没动"⇒ **不重排**，一份基于旧课表的规划继续用。

    现在 `stale` 按**三个输入文件的内容指纹**判（存快照时记下）：data / schedule / rules。
    """
    fresh_cli, fresh_work = _fresh()
    _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "p.json", {"slots": [
        {"date": _d(1), "time": "10:00-11:00", "task": "活"}]}))
    _, c1 = _json(fresh_cli, "context")
    assert c1["last_plan"]["stale"] is False, c1["last_plan"]

    # ① 只改**课表**（不碰 data.json）⇒ 必须作废
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "s.json", {"schedule": {
        "sessions": {"upsert": [{"date": _d(1), "periods": "1-2", "course": "新课甲"}]}}}))
    _, c2 = _json(fresh_cli, "context")
    assert c2["last_plan"]["stale"] is True, "只改课表没让规划作废：%s" % c2["last_plan"]

    # ② 重存快照 ⇒ 又新鲜；再只改**规则** ⇒ 又作废
    #    ★ 内容要和上一份**不同** —— 相同内容会被快照去重（`snapshot_unchanged`），
    #      那样 `latest()` 还是旧那份，用例会误判成"没变新鲜"
    _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "p2.json", {"slots": [
        {"date": _d(1), "time": "10:00-11:00", "task": "活"},
        {"date": _d(2), "time": "10:00-11:00", "task": "活二"}]}))
    _, c3 = _json(fresh_cli, "context")
    assert c3["last_plan"]["stale"] is False, "重存之后该新鲜：%s" % c3["last_plan"]
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "r.json", {"rules": {
        "planning_rules": {"upsert": [{"id": "rule-901", "kind": "preference",
                                       "condition": "为任务选择时段时",
                                       "body": "测试规则"}]}}}))
    _, c4 = _json(fresh_cli, "context")
    assert c4["last_plan"]["stale"] is True, "只改规则没让规划作废：%s" % c4["last_plan"]

    # ③ 快照里确实记下了三个输入文件的指纹（老快照没有 ⇒ 退回时间戳口径）
    import json as _jsonlib
    plans = _jsonlib.load(open(os.path.join(fresh_work, "data", "plans.json"), encoding="utf-8"))
    ins = plans["plans"][-1].get("inputs") or {}
    assert set(ins) == {"data", "schedule", "rules"} and all(ins.values()), ins
    return "只改课表/规则都让规划作废；重存即新鲜；快照记下三个输入文件的指纹"

def case_schedule_replace_guard(cli: str, work: str) -> str:
    """★ 整段替换课表不许**悄悄删掉今天及以后**的课次（用户 2026-10-16 报的严重问题）。

    原话：「课表系统存在严重问题 传入新一周课表后旧课表直接报废了，今天明明有课
    但是传入新课表后被认为没课 新课从19日开始」。

    根因：导入新一周（10/19 起）时用了 `dated_sessions` —— 那是**整段替换**
    （`_apply_collection` 对 list 直接 return）⇒ 本周（10/16–10/18）的课次被整批冲掉。

    守四条：
      · 整段替换会删掉**今天/以后**的课次 ⇒ 拒绝，并指路 `sessions.upsert`
      · 显式 `replace_future: true` ⇒ 放行（真要做整段替换）
      · 只删**已过去**的课次 ⇒ 放行（历史整理是正常操作）
      · `sessions.upsert` 从来不会触发它（只加不删）
    """
    fresh_cli, fresh_work = _fresh()
    d_past, d_today, d_future = _d(-3), _d(0), _d(6)
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "s.json", {"schedule": {
        "replace_future": True, "dated_sessions": [
            {"date": d_past, "periods": "1-2", "course": "旧课甲"},
            {"date": d_today, "periods": "3-4", "course": "今天的课乙"},
            {"date": d_future, "periods": "5-6", "course": "下周的课丙"},
        ]}}))

    # ① 用整段替换"导入新课表"（只给下周的）⇒ 必须拒绝，且要说清怎么改
    code, _out, err = _run(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "new.json", {
        "schedule": {"dated_sessions": [
            {"date": _d(8), "periods": "1-2", "course": "新课丁"}]}}))
    assert code != 0, "整段替换把今天的课冲掉了，竟然放行"
    assert "sessions.upsert" in err and "今天及以后" in err, "拒绝理由要指路 upsert：%s" % err[:300]
    assert "replace_future" in err, "要告诉它怎么强制：%s" % err[:300]

    # ② 显式强制 ⇒ 放行
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "force.json", {
        "schedule": {"replace_future": True, "dated_sessions": [
            {"date": _d(8), "periods": "1-2", "course": "新课丁"}]}}))
    _, sv = _json(fresh_cli, "sessions", "--days", "30")
    names = [s.get("course") for s in sv["sessions"]]
    assert "新课丁" in names and "今天的课乙" not in names, "强制后该真替换：%s" % names

    # ③ 只删已过去的 ⇒ 不拦
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "hist.json", {
        "schedule": {"replace_future": True, "dated_sessions": [
            {"date": _d(8), "periods": "1-2", "course": "新课丁"}]}}))
    # 上面这次会拦（它删掉了 d_future？不 —— 此时只剩新课丁）。改用只含"过去"的场景：
    fresh2, work2 = _fresh()
    _json(fresh2, "apply", "--file", _mkdraft(work2, "s2.json", {"schedule": {
        "replace_future": True, "dated_sessions": [
            {"date": d_past, "periods": "1-2", "course": "旧课甲"},
            {"date": d_today, "periods": "3-4", "course": "今天的课乙"}]}}))
    _json(fresh2, "apply", "--file", _mkdraft(work2, "hist2.json", {"schedule": {
        "dated_sessions": [{"date": d_today, "periods": "3-4", "course": "今天的课乙"}]}}))
    return ("整段替换删掉今天/以后的课次 ⇒ 拒绝并指路 sessions.upsert；"
            "replace_future=true ⇒ 放行；只删过去的 ⇒ 放行")

def case_water_follows_course(cli: str, work: str) -> str:
    """★ 水课**跟着课程**，不是跟着课次（用户 2026-10-16：「这种明显要跟着课程不是课次」）。

    实测踩到：用户说「示例课程G算水课」，Agent 只能把**那一次**课次标成 `water` ——
    以后课表里再来示例课程G的课次还得再标一遍。水课是**课程**的性质。

    守三条：
      · 课程标一次 ⇒ 该课**已有**课次都算水课（课次视图 / review 警告 / brief 标记三处同口径）
      · 该课**以后新加**的课次也算（不用再标）
      · 课次自己标 `water` 仍然认（历史数据与个别例外）
    """
    fresh_cli, fresh_work = _fresh()
    # ★ 日期必须落在 `sessions --days` 的窗口里 —— 2099 年那种"远期"会被窗口滤掉（我第一版就踩了）
    d1, d2 = _d(1), _d(2)
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "s.json", {"schedule": {
        "replace_future": True, "dated_sessions": [{"date": d1, "time": "08:00-09:40", "course": "水课甲"}]}}))
    # ① 只在**课程**上标一次
    # ★ 增量必须写 `{"upsert": [...]}` —— 裸列表是**整段替换**（第一版我写成裸列表，
    #   后面加课次时把前面那条整批替换掉了，用例自己抓到的）
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "c.json", {"schedule": {
        "courses": {"upsert": [{"course": "水课甲", "water": True}]}}}))
    _, sv = _json(fresh_cli, "sessions", "--days", "30")
    got = [s for s in sv["sessions"] if s.get("course") == "水课甲"]
    assert got and got[0].get("water") is True, "课程标了水课，课次视图却没带 water：%s" % got
    # ② 该课**以后新加**的课次也算（这才是"跟着课程"的意义）
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "s2.json", {"schedule": {
        "sessions": {"upsert": [{"date": d2, "time": "08:00-09:40", "course": "水课甲"}]}}}))
    _, sv2 = _json(fresh_cli, "sessions", "--days", "30")
    got2 = [s for s in sv2["sessions"] if s.get("date") == d2]
    assert got2 and got2[0].get("water") is True, "新加的课次没继承课程的水课标记：%s" % got2
    # ③ 非水课不受影响
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "s3.json", {"schedule": {
        "sessions": {"upsert": [{"date": d2, "time": "14:00-15:40", "course": "正经课乙"}]}}}))
    _, sv3 = _json(fresh_cli, "sessions", "--days", "30")
    other = [s for s in sv3["sessions"] if s.get("course") == "正经课乙"]
    assert other and not other[0].get("water"), "别的课被误标成水课：%s" % other
    # ④ review 与 brief 同口径：水课上的时段只给 water_class 警告、不算撞课
    _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "p.json", {"slots": [
        {"date": d1, "time": "08:30-09:30", "task": "课上写作业"}]}))
    _, rv = _json(fresh_cli, "review", "--file", _mkdraft(fresh_work, "rv.json", {"slots": [
        {"date": d1, "time": "08:30-09:30", "task": "课上写作业"}]}))
    kinds = [w.get("kind") for w in rv.get("warnings", [])]
    assert "water_class" in kinds, "水课时段该给 water_class 警告：%s" % rv.get("warnings")
    assert not rv.get("conflicts"), "水课不该算撞课：%s" % rv.get("conflicts")
    _, br = _json(fresh_cli, "brief", "--kind", "schedule", "--days", "9999")
    assert "（课·水）" in br["text"], "brief 的日程里该标出（课·水）：%s" % br["text"][:200]
    # ⑤ 课次自己标 water 仍然认（历史数据/个别例外）
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "s4.json", {"schedule": {
        "sessions": {"upsert": [{"date": d2, "time": "16:00-17:40", "course": "临时水课丙",
                                 "water": True}]}}}))
    _, sv4 = _json(fresh_cli, "sessions", "--days", "30")
    one = [s for s in sv4["sessions"] if s.get("course") == "临时水课丙"]
    assert one and one[0].get("water") is True, "课次自带 water 仍要认：%s" % one
    return "水课跟着课程：课程标一次 ⇒ 已有/以后新加的课次都算；课次自带仍认；三处同口径"

def case_water_not_clash(cli: str, work: str) -> str:
    """★ 水课标记：`water: true` 的课次，时段是**可用**的（能在课上写作业），不算撞课。

    踩到过：审查把示例课程G（水课）当成撞课，把水课上的作业排期判成冲突 ✗。

    ★★ 这条用例原先有两处**守不住东西**（审查实测）：
      · 断言 `assert b.get("ok") in (True, False)` —— **恒真**，等于没断言；
      · 用的是**共享工作区**，前一个用例留下的快照已经把它顶到"循环闸"
        （`kind=loop`、`ok=false`），于是"水课快照"这条分支**根本没走到**。
      现在：独立工作区（`_fresh()`）+ 硬断言（`kind != "loop"`、warnings 含 water_class、
      有正文）。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _, ses = _json(fresh_cli, "sessions", "--days", "30")
        target = next((x for x in (ses.get("sessions") or [])
                       if x.get("periods") and x.get("kind") != "cancelled"), None)
        assert target, f"课次表里没有可用的课：{ses.get('sessions')}"
        date, periods = target["date"], target["periods"]
        course = target.get("course", "")

        # 标成水课（走唯一写入口）
        _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "water-mark.json", {
            "schedule": {"sessions": {"upsert": [
                {"date": date, "periods": periods, "course": course, "water": True}]}}}))
        slot = {"date": date, "time": target.get("time") or "10:10-11:50",
                "task": "水课上写作业"}
        _, r = _json(fresh_cli, "review", "--file",
                     _mkdraft(fresh_work, "water-draft.json", {"slots": [slot]}))
        clash = [c for c in r["conflicts"] if c.get("kind") == "clashes_class"]
        assert not clash, "水课被当成撞课了：%s" % clash
        assert any(w.get("kind") == "water_class" for w in r.get("warnings", [])), \
            "水课应给警告（可用但不建议）：%s" % r.get("warnings")

        # ★ 回归：把"排在水课里"的规划存成快照，再走 brief —— 曾经在这里抛 UnboundLocalError
        #   （撞课段先 warns.append，而 warns 的初始化在后面）
        _json(fresh_cli, "plan-snapshot", "--file",
              _mkdraft(fresh_work, "water-snap.json", {"slots": [slot]}))
        _, b = _json(fresh_cli, "brief", "--kind", "plan", "--days", "9999")
        assert "error" not in b, "brief 崩了：%s" % b.get("error")
        assert b.get("kind") != "loop", "被循环闸顶掉了，没走到水课分支：%s" % b
        assert b["ok"] is True and "text" in b, "水课快照该照发正文：%s" % b
        assert any(w.get("kind") == "water_class" for w in b.get("warnings", [])), \
            "brief 侧也要给 water_class 警告：%s" % b.get("warnings")
        return ("水课=可用但不建议：review/brief 都不报撞课、都给 water_class 警告"
                "（独立工作区 + 硬断言，不再自证）")
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)



def case_next_tick(cli: str, work: str) -> str:
    """★ 到点唤醒（用户 2026-10-15）：内核只说「下一个边界是哪一刻」，不说「到了该干什么」。

    用户原话：不建议单纯使用定时器，而是跟随系统规划的下一时间点 ——
    「现在是三点，PLAN 的下一项是 4-5 点做一个作业，那么到了 5 点就自动触发更新」。

    所以边界 = 第一块**结束时刻晚于 now** 的块的结束时刻；规划里没有可跟的边界 ⇒ 回落锚点。
    """
    d = "2099-06-06"
    # ① 还没有快照 ⇒ 回落锚点（次日 07:00）
    _, r0 = _json(cli, "next-tick", "--after", f"{d} 09:00")
    assert r0["ok"] and r0["kind"] == "anchor" and r0["fallback"] is True, r0
    assert r0["next_at"].endswith("07:00"), r0
    # ② 存一份快照：14:00-16:00 一块、17:00-18:00 一块
    _json(cli, "plan-snapshot", "--file", _mkdraft(work, "tick.json", {"slots": [
        {"date": d, "time": "14:00-16:00", "task": "甲"},
        {"date": d, "time": "17:00-18:00", "task": "乙"}]}))
    # ③ 模拟「现在 15:00」——正在跑的那块 ⇒ 回它的结束时刻 16:00
    _, r1 = _json(cli, "next-tick", "--after", f"{d} 15:00")
    assert r1["next_at"] == f"{d} 16:00" and r1["kind"] == "block_end", r1
    assert r1["block"]["task"] == "甲", r1
    # ④ 模拟「现在 16:30」（两块之间）⇒ 回下一块的结束 18:00
    _, r2 = _json(cli, "next-tick", "--after", f"{d} 16:30")
    assert r2["next_at"] == f"{d} 18:00", r2
    # ⑤ 模拟「现在 19:00」（都过去了）⇒ 回落锚点，并报出「刚结束的那一块」（提示词要用）
    _, r3 = _json(cli, "next-tick", "--after", f"{d} 19:00")
    assert r3["kind"] == "anchor" and r3["fallback"] is True, r3
    assert r3["just_ended"]["task"] == "乙", r3
    # ⑥ 过去的块不当边界（改不了了）
    _, r4 = _json(cli, "next-tick", "--after", f"{d} 13:00")
    assert r4["next_at"] == f"{d} 16:00", r4
    return "next-tick：块结束即边界 / 两块之间取下一块 / 都过去了回锚点并报刚结束那块"


def case_past_deadline(cli: str, work: str) -> str:
    """★ 排在自己的硬截止之后 = 等于没排（用户 2026-10-15 报的真问题）。

    原话：「明天早上有示例课程作业要讲，但是今管没做，Agent 其实已经排不开了，
    但是并没有提供一个报错，而是强行把它排到了明天晚上 —— 但那个时候课堂已经过了。
    这种存在硬截止、但是排不开的情况，应该醒目地给一个报告。」

    内核只做时间算术：登记的 date/due_time 是硬截止，块的时刻在计划里。
    截止还没到 ⇒ 禁止（逼出"往前挪或如实说排不下"）；截止已过 ⇒ 警告（追不回）。
    """
    cli2, work2 = _fresh()
    d_due, d_late = "2099-07-01", "2099-07-02"
    _apply(cli2, work2, {"list": {"upsert": [
        {"task": "示例课程作业 HW#", "date": d_due, "due_time": "10:10 上课前"}]}})
    late_slots = {"slots": [{"date": d_late, "time": "19:00-20:00", "task": "示例课程作业 HW#"}]}
    # ① 排到截止之后 ⇒ brief 必须拦（这正是原来漏掉的那条）
    _json(cli2, "plan-snapshot", "--file", _mkdraft(work2, "late.json", late_slots))
    _, b = _json(cli2, "brief", "--kind", "plan")
    assert b["ok"] is False, "排到截止之后没被拦：%s" % b
    assert "past_deadline" in [p.get("kind") for p in b.get("problems", [])], b.get("problems")
    assert "text" not in b, "拦下时不该给正文"
    # ② 同一份 slots 过 review 也要报冲突（判据只有一份，和课间那条同一要求）
    _, r = _json(cli2, "review", "--file", _mkdraft(work2, "late2.json", late_slots))
    assert "past_deadline" in [c.get("kind") for c in r["conflicts"]], r["conflicts"]
    # ③ 挪到截止之前 ⇒ 放行
    _json(cli2, "plan-snapshot", "--file", _mkdraft(work2, "ok.json", {"slots": [
        {"date": d_due, "time": "08:00-09:00", "task": "示例课程作业 HW#"}]}))
    _, b2 = _json(cli2, "brief", "--kind", "plan")
    assert b2["ok"] is True and "text" in b2, "挪到截止之前应该放行：%s" % b2
    # ④ 跨过截止（前半段赶得上）⇒ 只警告，不阻塞
    _json(cli2, "plan-snapshot", "--file", _mkdraft(work2, "cross.json", {"slots": [
        {"date": d_due, "time": "09:30-11:00", "task": "示例课程作业 HW#"}]}))
    _, b3 = _json(cli2, "brief", "--kind", "plan")
    kinds3 = [w.get("kind") for w in b3.get("warnings", [])]
    assert b3["ok"] is True and "crosses_deadline" in kinds3, b3
    # ⑤ 截止已过（追不回）⇒ 警告，不阻塞
    _json(cli2, "apply", "--file", _mkdraft(work2, "past.json", {"list": {"upsert": [
        {"task": "早就该交的作业", "date": "2000-01-01", "due_time": "23:59"}]}}))
    _json(cli2, "plan-snapshot", "--file", _mkdraft(work2, "p2.json", {"slots": [
        {"date": "2099-07-01", "time": "08:00-09:00", "task": "示例课程作业 HW#"},
        {"date": "2099-07-03", "time": "19:00-20:00", "task": "早就该交的作业"}]}))
    _, b4 = _json(cli2, "brief", "--kind", "plan")
    k4 = [w.get("kind") for w in b4.get("warnings", [])]
    assert b4["ok"] is True and "past_deadline" in k4, b4
    return "排到硬截止之后 ⇒ brief 拦 / review 报冲突；跨截止 ⇒ 警告；截止已过 ⇒ 警告不阻塞"
def case_brief_shapes(cli: str, work: str) -> str:
    """★ 三种输出的**形状由内核保证**（不再靠模型自觉）：

    · `plan` 规划 —— **一条课都不能有**（课只在 schedule 里）
    · `schedule` 日程 —— 含课，且**同一天内**课与任务按开始时间混排；`→` 要点紧跟它的任务行
    · `todo` 待办 —— 不含课次条目
    """
    fresh_cli, fresh_work = _fresh()
    try:
        day = "2099-01-01"
        _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "s.json", {
            "schedule": {"sessions": {"upsert": [
                {"date": day, "time": "14:00-15:00", "course": "测试课B"},
                {"date": day, "time": "10:00-11:00", "course": "测试课A"}]}},
            "list": {"upsert": [{"task": "测试作业", "date": day,
                                 "gist": "P1 第 1 题"}]}}))
        _json(fresh_cli, "sync")
        _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "p.json", {
            "slots": [{"date": day, "time": "09:00-10:00", "task": "测试作业"},
                      {"date": day, "time": "16:00-17:00", "task": "测试作业"}]}))

        _, p = _json(fresh_cli, "brief", "--kind", "plan", "--days", "9999")
        assert p["text"].startswith("规划 · "), p["text"][:40]
        assert "现在做什么" in p["text"], "规划必须有「现在做什么」块"
        assert "（课）" not in p["text"], "★ 规划里不许出现课"
        assert "测试作业" in p["text"], "规划要含排期"
        assert "建议" in p["note"], "返回值要提示补建议行"

        _, sc = _json(fresh_cli, "brief", "--kind", "schedule", "--days", "9999")
        assert sc["text"].startswith("日程 · "), sc["text"][:40]
        assert "（课）" in sc["text"], "日程必须含课"

        # ★ 按天检查（`————————————` 分块）：跨天时间会回绕，不能整体比
        checked, all_rows = 0, []
        for blk in sc["text"].split("————————————"):
            rows = [l[2:7] for l in blk.splitlines()
                    if l.startswith("- ") and len(l) > 6 and l[2:4].isdigit() and l[4] == ":"]
            all_rows += rows
            if len(rows) >= 2:
                assert rows == sorted(rows), f"某天没按时间混排：{rows}"
                checked += 1
        assert checked >= 1, "至少要检查到一天的时序"
        assert len(all_rows) >= 4, f"日程该有课有任务：{all_rows}"
        for i, l in enumerate(sc["text"].splitlines()):
            if l.startswith("- → "):
                assert sc["text"].splitlines()[i - 1].startswith("- "), "要点行没跟着它的任务行"

        _, td = _json(fresh_cli, "brief", "--kind", "todo", "--days", "9999")
        assert td["text"].startswith("待办 A · "), td["text"][:40]
        assert "待办 B · " in td["text"], "待办要有两张表"
        assert "（课）" not in td["text"], "★ 待办里不许出现课次条目"
        assert "测试作业" in td["text"], "待办要含任务"
        return "规划不含课 / 日程含课且按时序 / 待办不含课；要点跟着任务行"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_cancelled_is_not_a_class(cli: str, work: str) -> str:
    """★ 停课（`kind=cancelled`）**不是课**：不算"下一节"、不排期、不撞课、不进异常。

    实测（改前）：`context.next_class = {course:"停掉的课", kind:"cancelled"}`、
    `class_count` 把停课算进去、`sessions` 也列出它，而 `brief --kind schedule` 里没有 ——
    **同一件事两种口径**（只读视图与 brief/review 不一致），模型可能把停掉的课报成"下一节"。
    修法：`_visible_sessions()` 是唯一口径，只读视图与 brief/review/anomalies 都走它。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        d1, d2 = _d(1), _d(2)
        _apply(fresh_cli, fresh_work, {"schedule": {
            "periods": {"3": "10:10-10:55", "4": "11:05-11:50"},
            "replace_future": True, "dated_sessions": [
                # 停课但**时刻算得出来**：撞课检查若漏了 kind，就会把它当课比
                {"date": d1, "periods": "3-4", "course": "停课·有时刻", "kind": "cancelled"},
                # 停课且**算不出时刻**：它也不该进 session_without_time（停课不用比时间）
                {"date": d1, "periods": "99", "course": "停课·没时刻", "kind": "cancelled"},
                {"date": d2, "periods": "3-4", "course": "正常课"}]}})
        _, ctx = _json(fresh_cli, "context")
        assert ctx["next_class"] and ctx["next_class"]["course"] == "正常课", ctx["next_class"]
        assert ctx["class_count"] == 1, ctx["class_count"]
        _, s = _json(fresh_cli, "sessions", "--days", "5")
        assert [x["course"] for x in s["sessions"]] == ["正常课"], s["sessions"]
        # 撞课口径与只读视图一致：排在**停课那一格**里不算冲突
        draft = _mkdraft(fresh_work, "cancelled.json", {"slots": [
            {"date": d1, "time": "10:10-11:50", "task": "停课那格排活"}]})
        _, rv = _json(fresh_cli, "review", "--file", draft)
        assert not [c for c in rv["conflicts"] if c.get("kind") == "clashes_class"], \
            "停课被当成撞课：%s" % rv["conflicts"]
        # brief 的日程：停课不上日程，别的课照常
        _json(fresh_cli, "plan-snapshot", "--file", draft)
        _, sc = _json(fresh_cli, "brief", "--kind", "schedule", "--days", "9999")
        assert "正常课" in sc["text"], sc["text"][-300:]
        assert "停课·有时刻" not in sc["text"] and "停课·没时刻" not in sc["text"], \
            sc["text"][-300:]
        # 异常清单：停课不参与"算不出时刻"的统计
        _, an = _json(fresh_cli, "anomalies")
        assert not [a for a in an["anomalies"] if a["type"] == "session_without_time"], \
            "停课不该进 session_without_time：%s" % an["anomalies"]
        return "停课不算课：next_class/class_count/sessions/日程/撞课/anomalies 全部同口径"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_session_without_time(cli: str, work: str) -> str:
    """★ **算不出时刻的课次**必须被报出来 —— 否则撞课检查静默不比、异常清单还是空的。

    实测（改前）：`periods: {}` + 课次 `periods: "3-4"` ⇒ `anomalies` 为空、
    `review ok=true, conflicts=[]`（**撞课根本没比**）、`context.next_class` 连 `time` 都没有。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        d = _d(1)
        _apply(fresh_cli, fresh_work, {"schedule": {
            "periods": {},
            "replace_future": True, "dated_sessions": [{"date": d, "periods": "3-4", "course": "没有作息表的课"}]}})
        _, an = _json(fresh_cli, "anomalies")
        hit = [a for a in an["anomalies"] if a["type"] == "session_without_time"]
        assert hit, "算不出时刻的课次没进异常清单：%s" % an["anomalies"]
        assert any(x["course"] == "没有作息表的课" for x in hit[0]["sessions"]), hit[0]
        # review：报 unknown_class_time（**警告**，不阻塞 —— 先警告，稳定后再考虑升阻塞）
        draft = _mkdraft(fresh_work, "notime.json", {"slots": [
            {"date": d, "time": "10:10-11:50", "task": "可能撞课"}]})
        _, rv = _json(fresh_cli, "review", "--file", draft)
        assert any(w.get("kind") == "unknown_class_time" for w in rv.get("warnings", [])), rv
        assert rv["ok"] is True, "算不出时刻只警告，不该阻塞：%s" % rv
        # brief：同一口径（同一个函数）
        _json(fresh_cli, "plan-snapshot", "--file", draft)
        _, b = _json(fresh_cli, "brief", "--kind", "plan")
        assert any(w.get("kind") == "unknown_class_time" for w in b.get("warnings", [])), b
        assert b["ok"] is True and "text" in b, b
        return "算不出时刻：anomalies 列出课次；review/brief 都报 unknown_class_time（警告不阻塞）"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_brief_default_days(cli: str, work: str) -> str:
    """★ `brief` **默认只展开 3 天**，并把剩下的日子报成一行（既有用例全传 `--days 9999`）。

    还守住另一条：`--days` **不再静默改写** —— 实测改前 `0 → 3`、负数 `→ 1`，
    模型以为自己缩小/放大了窗口，其实拿到的是别的天数；现在非法值直接报错。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        slots = [{"date": _d(k), "time": "21:00-22:00", "task": "照天排的活"} for k in range(1, 7)]
        _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "d3.json", {"slots": slots}))
        _, b = _json(fresh_cli, "brief", "--kind", "plan")
        assert b["ok"] is True, b
        assert b.get("days") == 3, "要在返回里回显生效天数：%s" % b.get("days")
        import re as _re
        heads = [l for l in b["text"].splitlines()
                 if _re.match(r"^\d{1,2}/\d{1,2} 周[一二三四五六日]$", l)]
        assert heads == [_label(_d(1)), _label(_d(2)), _label(_d(3))], \
            "默认只该展开 3 天：%s" % heads
        assert ("（后面还有 3 天有安排，最近 %s" % _label(_d(4))) in b["text"], \
            "剩余天数要报出来：%s" % b["text"]
        # 显式给天数照办
        _, b9 = _json(fresh_cli, "brief", "--kind", "plan", "--days", "9999")
        assert b9.get("days") == 9999 and _label(_d(6)) in b9["text"], b9
        # 非法天数：报错，不静默改成默认值
        for bad in ("0", "-3"):
            code, _out, err = _run(fresh_cli, "brief", "--kind", "plan", "--days", bad)
            assert code != 0, f"--days {bad} 竟然被接受（静默改写？）"
            assert "--days" in err, err[:200]
        return "brief 默认 3 天 +「后面还有 N 天有安排」；--days 9999 照办；0/-3 报错不静默改写"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_wall_filter_and_order(cli: str, work: str) -> str:
    """★ `wall`：`--days` 过滤 / 无 date 的 slot 永远保留 / 字段省略 / **slots 排序**。

    ★ 排序为什么要守：唯一消费者 `外部时间面板消费者` 把 `slots[0]`
      当"计划表**最上面**那条（最早的时段）"来判断"该不该重排" ——
      快照的**书写顺序**不是顺序，内核必须给出确定的次序。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        payload = {"slots": [
            {"date": _d(30), "time": "10:00-11:00", "task": "远处的活"},
            {"date": _d(1), "time": "14:00-15:00", "task": "晚些的活", "why": "下午才空"},
            {"date": _d(1), "time": "09:00-10:00", "task": "早些的活"},
            {"time": "08:00-09:00", "task": "无日期的块"},
        ]}
        _json(fresh_cli, "plan-snapshot", "--file",
              _mkdraft(fresh_work, "wall2.json", payload))
        _, w = _json(fresh_cli, "wall", "--days", "7")
        names = [s["task"] for s in w["slots"]]
        assert "远处的活" not in names, "更远的 slot 要被 --days 丢掉：%s" % names
        assert "无日期的块" in names, "无 date 的 slot 永远保留：%s" % names
        assert names == ["早些的活", "晚些的活", "无日期的块"], \
            "slots 必须按 (date,time) 排序、无日期的排最后：%s" % names
        assert w["slots"][0]["task"] == "早些的活", "slots[0] 得是计划表最上面那条"
        keys = [(s["date"], s["time"]) for s in w["slots"] if s["date"]]
        assert keys == sorted(keys), keys
        by = {s["task"]: s for s in w["slots"]}
        assert by["晚些的活"]["why"] == "下午才空", by["晚些的活"]
        assert "why" not in by["早些的活"], "缺 why 时省略、不写空串：%s" % by["早些的活"]
        assert "why" not in by["无日期的块"], by["无日期的块"]
        _, w2 = _json(fresh_cli, "wall", "--days", "60")
        assert "远处的活" in [s["task"] for s in w2["slots"]], w2["slots"]
        return "wall：--days 过滤、无日期保留、字段省略、slots[0]=最早那块（已排序）"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


ALLOW_RULES = [
    {"id": "rule-pref", "kind": "preference", "condition": "深夜",
     "body": "深夜尽量不排", "no_slots": ["19:00-23:59"]},
    {"id": "rule-con", "kind": "constraint", "condition": "午休",
     "body": "午休不排任何任务", "no_slots": ["12:10-12:55"]},
]


def case_allow_exemptions(cli: str, work: str) -> str:
    """★ `allow` 四连：preference+allow ⇒ 干净；**slots 一个字不改也要落库**；
    constraint **无豁免**；`allow` 指向不存在的规则要被**报出来**（原先静默忽略）。

    前三条是"用户明确同意破例"的完整链路；第四条是"拼错 id 的同意"——
    原先它看着像生效了，其实那条规则根本没被豁免。
    """
    out = []
    # ① preference + allow ⇒ ok=true 且 warnings 空；**只加 allow 也必须存进去**
    c1, w1 = _fresh()
    try:
        _json(c1, "apply", "--file", _mkdraft(w1, "rules.json",
                                              {"rules": {"planning_rules": {"upsert": ALLOW_RULES}}}))
        slot = {"date": _d(1), "time": "20:00-21:00", "task": "晚上写的活"}
        _json(c1, "plan-snapshot", "--file", _mkdraft(w1, "p1.json", {"slots": [slot]}))
        _, b1 = _json(c1, "brief", "--kind", "plan")
        assert b1["ok"] is True, b1
        assert any(w.get("kind") == "violates_rule" and w.get("rule") == "rule-pref"
                   for w in b1["warnings"]), b1["warnings"]
        _, s2 = _json(c1, "plan-snapshot", "--file",
                      _mkdraft(w1, "p2.json", {"slots": [slot], "allow": ["rule-pref"]}))
        assert s2.get("action") == "snapshot_saved" and s2.get("stored") is not False, \
            "slots 不变、只补 allow 的修正被去重吃掉了（补救路径失效）：%s" % s2
        _, b2 = _json(c1, "brief", "--kind", "plan")
        assert b2["ok"] is True and b2.get("warnings") == [], \
            "豁免过的软偏好不该再警告：%s" % b2.get("warnings")
        out.append("preference+allow⇒干净")
    finally:
        shutil.rmtree(w1, ignore_errors=True)
    # ② constraint + allow ⇒ **不豁免**（禁止就是禁止）
    c2, w2 = _fresh()
    try:
        _json(c2, "apply", "--file", _mkdraft(w2, "rules.json",
                                              {"rules": {"planning_rules": {"upsert": ALLOW_RULES}}}))
        _json(c2, "plan-snapshot", "--file", _mkdraft(w2, "p.json", {
            "slots": [{"date": _d(1), "time": "12:30-12:50", "task": "撞午休"}],
            "allow": ["rule-con"]}))
        _, b = _json(c2, "brief", "--kind", "plan")
        assert b["ok"] is False and b.get("kind") == "plan", b
        assert any(p.get("kind") == "violates_rule" and p.get("rule") == "rule-con"
                   for p in b["problems"]), b["problems"]
        assert "text" not in b, "硬约束被违反还给正文"
        out.append("constraint+allow⇒拦")
    finally:
        shutil.rmtree(w2, ignore_errors=True)
    # ③ allow 指向不存在的规则 ⇒ 报出来（但已豁免的照旧安静）
    c3, w3 = _fresh()
    try:
        _json(c3, "apply", "--file", _mkdraft(w3, "rules.json",
                                              {"rules": {"planning_rules": {"upsert": ALLOW_RULES}}}))
        _json(c3, "plan-snapshot", "--file", _mkdraft(w3, "p.json", {
            "slots": [{"date": _d(1), "time": "20:00-21:00", "task": "晚上写的活"}],
            "allow": ["rule-pref", "rule-nope"]}))
        _, b = _json(c3, "brief", "--kind", "plan")
        assert b["ok"] is True, b
        bad = [w for w in b.get("warnings", []) if w.get("kind") == "allow_unknown_rule"]
        assert bad and bad[0]["allow"] == ["rule-nope"], \
            "指向不存在规则的 allow 没被报出来：%s" % b.get("warnings")
        assert not any(w.get("kind") == "violates_rule" for w in b.get("warnings", [])), \
            "已豁免的 rule-pref 不该再报：%s" % b.get("warnings")
        out.append("allow 指向不存在的规则⇒报出")
    finally:
        shutil.rmtree(w3, ignore_errors=True)
    return "allow 四连：" + " / ".join(out)
def case_malformed_snapshot_no_crash(cli: str, work: str) -> str:
    """★ 畸形快照（缺 `date` / 缺 `time` / 非对象 slot）⇒ **不崩**，进 `problems`。

    读命令（`context` / `wall`）读同一份快照，也必须不崩（日期坏 ⇒ 不猜、`passed=false`）。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "bad.json", {"slots": [
            {"time": "10:00-11:00", "task": "缺日期"},
            {"date": _d(1), "task": "缺时间"},
            "我不是对象",
            {"date": _d(1), "time": "13:00-14:00", "task": "正常的一块"}]}))
        _code, b = _json(fresh_cli, "brief", "--kind", "plan")
        assert "error" not in b, "畸形 slot 把 brief 打崩了：%s" % b.get("error")
        assert b["ok"] is False and b.get("kind") == "plan", b
        bad = [p for p in b.get("problems", []) if p.get("kind") == "bad_slot"]
        assert len(bad) == 3, "三个畸形 slot 都要进 problems：%s" % b.get("problems")
        assert "text" not in b, "有畸形 slot 不该给正文"
        # 同一份快照的只读出口也不许崩
        _json(fresh_cli, "context")
        _json(fresh_cli, "wall")
        return "缺 date / 缺 time / 非对象 slot：brief 进 problems（不崩不给正文），context/wall 不崩"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_late_night_window(cli: str, work: str) -> str:
    """★ 结束时刻 `24:00`（= 次日 00:00）在**四处口径一致**。

    用户要把睡前窗口写成 `23:30-24:00`，实测（改前）`validate_rules` 直接拒绝
    （`TIME_RANGE` 只认到 `23:59`）；而 `23:00-24:00` 的块在 23:01 就被标 `passed`
    （`_plan_view` 与 `wall` 两处都 `min(hm[0], 23)`）。
    这里同时守 schema / `_hhmm(last=True)` / `_passed_at` / 两个消费者。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        d = _d(1)
        # ① schema：规则窗口与课次 time 都走 TIME_RANGE ⇒ `24:00` 收得下
        _, r = _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "late.json", {
            "rules": {"planning_rules": {"upsert": [
                {"id": "rule-late", "kind": "preference", "condition": "睡前",
                 "body": "睡前不排活", "no_slots": ["23:30-24:00"]}]}},
            "schedule": {"sessions": {"upsert": [
                {"date": d, "time": "22:00-24:00", "course": "晚课"}]}}}))
        assert r["ok"] is True, r
        assert r["report"]["rules"]["planning_rules"]["added"] == 1, r["report"]
        # ② `_hhmm(last=True)` / `_passed_at` / context.last_plan / wall 同口径
        _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "late-snap.json", {"slots": [
            {"date": d, "time": "22:00-24:00", "task": "晚课后写作业"}]}))
        y, m, dd = (int(x) for x in d.split("-"))
        nx = datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)
        expr = (
            '{"hhmm": qyn._hhmm("22:00-24:00", last=True),'
            ' "passed_2330": qyn._passed_at(%r, "22:00-24:00", _dt(%d, %d, %d, 23, 30)),'
            ' "passed_2359": qyn._passed_at(%r, "22:00-24:00", _dt(%d, %d, %d, 23, 59)),'
            ' "passed_next": qyn._passed_at(%r, "22:00-24:00", _dt(%d, %d, %d, 0, 1)),'
            ' "ctx": [s["passed"] for s in ((qyn.cmd_context(None).get("last_plan") or {})'
            '.get("slots") or [])],'
            ' "wall": [s["passed"] for s in qyn.cmd_wall(argparse.Namespace(days=7))["slots"]]}'
        ) % (d, y, m, dd, d, y, m, dd, d, nx.year, nx.month, nx.day)
        probe_2330 = _kernel_call(fresh_work, expr, now=(y, m, dd, 23, 30))
        assert probe_2330["hhmm"] == [24, 0], probe_2330["hhmm"]
        assert probe_2330["passed_2330"] is False, \
            "23:30 时 22:00-24:00 还没结束，不该 passed：%s" % probe_2330
        assert probe_2330["passed_2359"] is False, probe_2330
        assert probe_2330["passed_next"] is True, probe_2330
        assert probe_2330["ctx"] == [False] and probe_2330["wall"] == [False], \
            "两个消费者都必须用同一个判据：%s" % probe_2330
        # 过了 24:00（次日 00:01）⇒ 才算过去（`end < now`；正好 24:00 那一刻仍不算过）
        # 注意 `wall` 按"今天起"过滤，次日再看时昨天那块已被滤掉 —— 所以这一档用
        # `context.last_plan` 证明消费者还在用同一个判据（`wall` 已在上一个探针里验过）。
        probe_next = _kernel_call(fresh_work, expr, now=(nx.year, nx.month, nx.day, 0, 1))
        assert probe_next["ctx"] == [True], \
            "过了 24:00 就该算过去：%s" % probe_next
        return ("24:00 口径统一：schema 收得下 23:30-24:00；23:30/23:59 不算 passed；"
                "次日 00:01 才算；context.last_plan 与 wall 同判据")
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_brief_catches_unscheduled(cli: str, work: str) -> str:
    """★ 漏排：`brief`（**发出前最后一道闸**）不再放过"今天有 DDL 却没排"的任务。

    实测（改前）：快照漏掉当天有 DDL 的任务时，`review` 报 `unscheduled=['DUE_TODAY']`
    而 `brief ok=true` 直接把正文给出去了。现在两边走**同一个函数**；
    阻塞口径：今天到期的进 `problems`（拦），远期 DDL 只报不拦（天然不在本周计划里）。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "今天要交的", "date": today, "due_time": "23:59"},
            {"task": "下个月才交的", "date": _d(30)}]}})
        _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "empty.json", {"slots": []}))
        _, b = _json(fresh_cli, "brief", "--kind", "plan")
        assert b["ok"] is False and b.get("kind") == "plan", b
        assert any(p.get("kind") == "unscheduled" and p["task"] == "今天要交的"
                   for p in b["problems"]), b["problems"]
        assert "text" not in b, "漏排今天的事还给正文"
        assert [u["task"] for u in b["unscheduled"]] == ["今天要交的", "下个月才交的"] \
            or {u["task"] for u in b["unscheduled"]} == {"今天要交的", "下个月才交的"}, \
            b["unscheduled"]
        # 排上之后照发
        _json(fresh_cli, "plan-snapshot", "--file", _mkdraft(fresh_work, "ok.json", {"slots": [
            {"date": today, "time": "20:00-21:00", "task": "今天要交的"}]}))
        _, b2 = _json(fresh_cli, "brief", "--kind", "plan")
        assert b2["ok"] is True and "今天要交的" in b2["text"], b2
        assert any(u["task"] == "下个月才交的" for u in b2["unscheduled"]), \
            "远期 DDL 只报不拦：%s" % b2["unscheduled"]
        # review 用同一个函数（同一形状）——两边口径一致
        draft = _mkdraft(fresh_work, "rv.json", {"slots": [
            {"date": today, "time": "20:00-21:00", "task": "今天要交的"}]})
        _, rv = _json(fresh_cli, "review", "--file", draft)
        assert rv["ok"] is True, rv
        assert [u["task"] for u in rv["unscheduled"]] == ["下个月才交的"], rv["unscheduled"]
        return "brief 拦「今天有 DDL 却没排」（problems/unscheduled）；远期只报不拦；与 review 同函数"
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_same_name_addressing(cli: str, work: str) -> str:
    """★ 同名字条目的**寻址**：新增 / 更新 / 拒绝 / 消歧（机制，不猜语义）。

    背景（用户 2026-10-15）：两条同名「示例课程D作业」（`anchored_to` 10-15 / 10-21）
    只想删掉其中一条，而 `list.remove` 按名字**全删**、`upsert`/`mark` 只动**第一条** ——
    更糟的是底层 `set_registry_status` 把同名的**全改掉**，返回值却写"只改了第一条"。

    判据（确定性、可复述）：
      · 表里**没有同名** ⇒ 新增（哪怕带了 `date`/`occurrence_of` —— 那是内容，不是指认）
      · 表里有同名**唯一一条**：`upsert` 就动它（改期/补认领都是这条路径）；
        `remove` 则是"带消歧字段却对不上"⇒ 拒绝
      · 表里有同名**多条**：不给消歧字段 ⇒ **拒绝**并报候选
        （不再"全删"或"只动第一条"）；给了且唯一命中 ⇒ 只动那一条
    """
    out = []
    fresh_cli, fresh_work = _fresh()
    try:
        # ①/② **空注册表** + 带 date / 带 date+occurrence_of ⇒ 都是**新增**
        #     （这条是 task-5 的 P0 回归：`date` 被当成消歧字段 ⇒ 新任务被拒）
        _write(os.path.join(fresh_work, "data", "data.json"),
               {"registry": [], "meta": {"last_updated": ""}})
        _, r1 = _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "插件冒烟", "date": "2026-12-01", "priority": "high"}]}})
        assert r1["report"]["list"]["added"] == 1, r1
        _, r2 = _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "周期活", "date": "2026-12-02", "occurrence_of": "周期活",
             "anchored_to": "2026-12-02"}]}})
        assert r2["report"]["list"]["added"] == 1, r2
        # ③ 同名唯一一条 + 不给消歧 ⇒ 更新它（改期）
        _, r3 = _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "插件冒烟", "date": "2026-12-05"}]}})
        assert r3["report"]["list"]["updated"] == 1 and r3["report"]["list"]["added"] == 0, r3
        # ④ 起 **预设两条同名**（真实形态：周期任务的两次发生，`anchored_to` 不同）——
        #    同名多条**没法**用 upsert 造出来（同名唯一时它按定义就是"更新那一条"），
        #    所以这里是**测试夹具**，走真实形态而不是拼一个内核做不到的输入。
        _write(os.path.join(fresh_work, "data", "data.json"), {"registry": [
            {"task": "插件冒烟", "date": "2026-12-05", "priority": "high", "status": "pending"},
            {"task": "同名的活", "date": _d(3), "anchored_to": _d(3), "status": "pending"},
            {"task": "同名的活", "date": _d(9), "anchored_to": _d(9), "status": "pending"}],
            "meta": {"last_updated": ""}})
        # ⑤ 同名 ×2 ⇒ 不给消歧字段就**拒绝**并报候选（不猜、不动第一条）
        code5, _, err5 = _apply_err(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "同名的活", "gist": "改要点"}]}})
        assert code5 != 0, "同名多条不给消歧竟然写进去了"
        bad5 = json.loads(err5)
        assert len(bad5.get("candidates") or []) == 2, bad5
        assert not any(d.get("gist") == "改要点" for d in _hot(fresh_work, "data.json")["registry"]), \
            "被拒绝的 upsert 不该改到任何一条"
        # ⑥ 同名 ×2 + 消歧字段指对一条 ⇒ 只动那一条，并报"按什么匹配"
        _, r5 = _apply(fresh_cli, fresh_work, {"list": {"upsert": [
            {"task": "同名的活", "date": _d(9), "anchored_to": _d(9),
             "gist": "只改这一次"}]}})
        assert r5["report"]["list"]["updated"] == 1, r5
        assert r5["report"]["list"]["matched_by"], \
            "要报出按什么字段匹配、命中哪条：%s" % r5["report"]
        rows = {d["date"]: d.get("gist") for d in _hot(fresh_work, "data.json")["registry"]
                if d["task"] == "同名的活"}
        assert rows == {_d(3): None, _d(9): "只改这一次"}, rows
        # ⑧ 同名 ×2 + remove 不给消歧 ⇒ 拒绝（**不是**全删）
        code8, _, err8 = _apply_err(fresh_cli, fresh_work, {"list": {"remove": ["同名的活"]}})
        assert code8 != 0, "同名多条不给消歧，remove 竟然全删了"
        assert len(json.loads(err8).get("candidates") or []) == 2, err8
        assert len([d for d in _hot(fresh_work, "data.json")["registry"]
                    if d["task"] == "同名的活"]) == 2, "被拒绝的 remove 不该删掉任何一条"
        # ⑨ remove 带 date ⇒ 只删指定那条，另一条完好
        _, r9 = _apply(fresh_cli, fresh_work, {"list": {"remove": [
            {"task": "同名的活", "date": _d(9)}]}})
        assert r9["report"]["list"]["removed"] == ["同名的活"], r9
        assert r9["report"]["list"]["matched_by"], r9["report"]
        left = [d["date"] for d in _hot(fresh_work, "data.json")["registry"]
                if d["task"] == "同名的活"]
        assert left == [_d(3)], left
        # ⑩ remove 同名唯一一条 + 消歧字段对不上 ⇒ 拒绝并报候选（不是"照样删"）
        code10, _, err10 = _apply_err(fresh_cli, fresh_work, {"list": {"remove": [
            {"task": "插件冒烟", "date": "2099-01-01"}]}})
        assert code10 != 0, "唯一候选但日期对不上，竟然还是删了"
        assert json.loads(err10).get("candidates"), err10
        assert any(d["task"] == "插件冒烟" for d in
                   _hot(fresh_work, "data.json")["registry"]), "被拒绝的 remove 不该删掉东西"
        out.append("upsert/remove：新增/更新/拒绝/消歧 全对")
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)

    # ⑪ `mark`：同名多条不给 `--date` ⇒ 拒绝（原先会改掉第一条、底层却把两条都改了）
    c2, w2 = _fresh()
    try:
        _write(os.path.join(w2, "data", "data.json"), {"registry": [
            {"task": "同名标记", "date": _d(1), "anchored_to": _d(1), "status": "pending"},
            {"task": "同名标记", "date": _d(5), "anchored_to": _d(5), "status": "pending"}],
            "meta": {"last_updated": ""}})
        code11, _, err11 = _run(c2, "mark", "--task", "同名标记", "--status", "done")
        assert code11 != 0, "同名多条 mark 竟然通过了"
        assert len(json.loads(err11).get("candidates") or []) == 2, err11
        assert all(d["status"] == "pending"
                   for d in _hot(w2, "data.json")["registry"]), "被拒绝的 mark 不该改到任何一条"
        # ⑫ 带 --date ⇒ 只标那一条
        _, m = _json(c2, "mark", "--task", "同名标记", "--status", "done", "--date", _d(5))
        assert m["changed"] == 1 and m["matched"] == 1, m
        st = {d["date"]: d["status"] for d in _hot(w2, "data.json")["registry"]}
        assert st == {_d(1): "pending", _d(5): "completed"}, st
        # ⑬ --date 对不上 ⇒ 拒绝并报候选
        code13, _, err13 = _run(c2, "mark", "--task", "同名标记", "--status", "cancelled",
                                "--date", "2099-01-01")
        assert code13 != 0 and json.loads(err13).get("candidates"), err13
        out.append("mark --date：拒绝/只标一条/对不上也拒绝")
    finally:
        shutil.rmtree(w2, ignore_errors=True)
    return "同名条目寻址：" + "；".join(out)


def case_first_class_window(cli: str, work: str) -> str:
    """★ 规则窗口 `before_first_class`（**相对当天课表**）：上午有课才生效，review/brief 同口径。

    rule-007 原话：「当天上午有课时，**上课前**的时段不安排任务，用户要睡觉」
    —— `no_slots` 只能写固定时刻，表达不了"从 00:00 到当天第一节课开始"。

    口径（全是机制）：窗口 = `00:00 → 当天第一节课开始`；**仅当**该开始时刻
    ≤ `max_start`（默认 12:00，可写在规则里）时**当天才生效**；
    没课 / 只有停课 / 节次算不出时刻 ⇒ **不生效、也不报错**。
    `kind` 决定分级：preference ⇒ warnings（可被快照 `allow` 豁免），constraint ⇒ 阻塞。
    """
    fresh_cli, fresh_work = _fresh()
    try:
        dm, da, dn, dc = _d(1), _d(2), _d(3), _d(4)
        _apply(fresh_cli, fresh_work, {
            "schedule": {"periods": {}, "replace_future": True, "dated_sessions": [
                {"date": dm, "time": "08:00-09:40", "course": "早课"},
                {"date": da, "time": "14:00-15:40", "course": "下午课"},
                {"date": dc, "time": "08:00-09:40", "course": "停了的早课",
                 "kind": "cancelled"}]},
            "rules": {"planning_rules": {"upsert": [
                {"id": "rule-007", "kind": "preference", "condition": "当天上午有课",
                 "body": "上课前不排任务（要睡觉）", "before_first_class": {}}]}}})

        def draft(name, date, span="07:00-07:30"):
            return _mkdraft(fresh_work, name, {"slots": [
                {"date": date, "time": span, "task": "早上的活"}]})

        # ① 上午有课（08:00 ≤ 12:00）⇒ 窗口 `00:00-08:00` 里的一块 = 违反（preference ⇒ 警告）
        _, r1 = _json(fresh_cli, "review", "--file", draft("w1.json", dm))
        assert r1["ok"] is True, r1                     # preference 不阻塞
        h1 = [w for w in r1["warnings"] if w.get("rule") == "rule-007"]
        assert h1 and h1[0]["window"] == "00:00-08:00", r1["warnings"]
        assert h1[0]["first_class"] == "08:00" and h1[0]["course"] == "早课", h1[0]
        assert not [c for c in r1["conflicts"] if c.get("rule") == "rule-007"], r1["conflicts"]
        # ② 第一节课在阈值之后（14:00）⇒ **不生效**
        _, r2 = _json(fresh_cli, "review", "--file", draft("w2.json", da))
        assert not [w for w in r2["warnings"] if w.get("rule") == "rule-007"], r2["warnings"]
        # ③ 当天没有课 ⇒ 不生效
        _, r3 = _json(fresh_cli, "review", "--file", draft("w3.json", dn))
        assert not [w for w in r3["warnings"] if w.get("rule") == "rule-007"], r3["warnings"]
        # ④ 当天**只有停课** ⇒ 不生效（顺带证明它复用了 `_visible_sessions`：停课不算课）
        _, r4 = _json(fresh_cli, "review", "--file", draft("w4.json", dc))
        assert not [w for w in r4["warnings"] if w.get("rule") == "rule-007"], r4["warnings"]
        # ⑤ 阈值**显式写在规则里**：`max_start: "07:00"` ⇒ 08:00 的课不算"上午有课"⇒ 不生效
        _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "thr.json", {
            "rules": {"planning_rules": {"upsert": [
                {"id": "rule-007b", "kind": "preference", "condition": "只有清早的课",
                 "body": "上课前不排", "before_first_class": {"max_start": "07:00"}}]}}}))
        _, r5 = _json(fresh_cli, "review", "--file", draft("w5.json", dm))
        assert not [w for w in r5["warnings"] if w.get("rule") == "rule-007b"], r5["warnings"]
        assert [w for w in r5["warnings"] if w.get("rule") == "rule-007"], \
            "同一节课上，默认 12:00 的规则该生效而 07:00 阈值的不该 —— 阈值没起作用？%s" % r5
        # ⑥ review 与 brief **同一份实现**：同一份 slots、preference ⇒ 两边都只警告
        slot = {"date": dm, "time": "07:00-07:30", "task": "早上的活"}
        _json(fresh_cli, "plan-snapshot", "--file",
              _mkdraft(fresh_work, "p.json", {"slots": [slot]}))
        _, b = _json(fresh_cli, "brief", "--kind", "plan")
        assert b["ok"] is True and "text" in b, b
        bh = [w for w in b.get("warnings", []) if w.get("rule") == "rule-007"]
        assert bh and bh[0]["window"] == "00:00-08:00", b.get("warnings")
        # 同一份 slots 换成 constraint ⇒ 两边都必须**阻塞**（守门：不许一边死代码）
        _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "hard.json", {
            "rules": {"planning_rules": {"upsert": [
                {"id": "rule-007c", "kind": "constraint", "condition": "当天上午有课",
                 "body": "上课前绝对不排", "before_first_class": {}}]}}}))
        _, rc = _json(fresh_cli, "review", "--file", draft("wc.json", dm))
        assert rc["ok"] is False, rc
        assert [c["kind"] for c in rc["conflicts"] if c.get("rule") == "rule-007c"] \
            == ["violates_rule"], rc["conflicts"]
        _, b2 = _json(fresh_cli, "brief", "--kind", "plan")
        assert b2["ok"] is False and b2.get("kind") == "plan", b2
        assert [p["kind"] for p in b2["problems"] if p.get("rule") == "rule-007c"] \
            == ["violates_rule"], b2["problems"]
        # ⑦ schema 校验新字段：坏阈值 / 不认识的键 ⇒ 拒绝且不落盘
        for bad, why in (
            ({"id": "x1", "kind": "preference", "condition": "c", "body": "b",
              "before_first_class": {"max_start": "25:00"}}, "坏阈值 25:00"),
            ({"id": "x2", "kind": "preference", "condition": "c", "body": "b",
              "before_first_class": {"until": "12:00"}}, "不认识的键 until"),
        ):
            code, _o, err = _apply_err(fresh_cli, fresh_work,
                                       {"rules": {"planning_rules": {"upsert": [bad]}}})
            assert code != 0, f"{why} 竟然被接受"
            assert "before_first_class" in err or "max_start" in err, err[:200]
        assert not [r for r in _hot(fresh_work, "rules.json")["planning_rules"]
                    if str(r.get("id", "")).startswith("x")], "被拒绝的规则落盘了"
        # ⑧ 守门：新窗口必须复用 `_visible_sessions`（不许出现第三份"当天有哪些课"）
        src = io.open(os.path.join(fresh_work, "qyn.py"), encoding="utf-8").read()
        body = src.split("def _first_class_min", 1)[1].split("\ndef ", 1)[0]
        assert "_visible_sessions(" in body, \
            "`_first_class_min` 没复用 `_visible_sessions` —— 又一份'当天有哪些课'的实现？"
        return ("上午有课⇒00:00-08:00 窗口违反（preference 警告/constraint 阻塞，review 与 brief 同口径）；"
                "下午课/没课/只有停课/阈值 07:00 ⇒ 都不生效不报错；schema 拒绝坏形状")
    finally:
        shutil.rmtree(fresh_work, ignore_errors=True)


def case_estimate_minutes(cli: str, work: str) -> str:
    """★ 「预留时长」是**规划辅助事实**，不是建议时段（用户 2026-11-08）。

    用户原话：「比如我说示例课程F以后预留一小时 预习报告预留半小时这些帮助规划的信息？
    是不是要写一个新的文件来存储」

    结论与实现：
      · **不新建文件** —— 周期任务（"以后每次"）写 `periodic_tasks[].estimate_minutes`，
        物化时**自动带到**那一条上；单次任务直接写条目的 `estimate_minutes`。
      · 只读视图原样透出（没有就不带这个键，与 priority/lead_days 同一规矩）。
      · `review` 把「块的时长 < 预留时长」报成**警告**（不阻塞：拆两段接力也是合法的）。
      · 临时安排（几点做多久）仍然只进 `plans` 的 slots，**绝不**写进 data.json。
    """
    fresh_cli, fresh_work = _fresh()
    d1, d2 = _d(1), _d(4)
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "s.json", {"schedule": {
        "replace_future": True,
        "dated_sessions": [{"date": d1, "time": "08:00-09:40", "course": "实验课甲"},
                           {"date": d2, "time": "08:00-09:40", "course": "实验课甲"}],
        "periodic_tasks": [{"task": "实验报告", "follow_course": "实验课甲",
                            "due_time": "23:59", "estimate_minutes": 60},
                           {"task": "预习报告", "follow_course": "实验课甲",
                            "due_time": "上课前", "estimate_minutes": 30}]}}))
    _json(fresh_cli, "sync")

    # ① 周期任务物化 ⇒ 那一条上要带着估时（不然排期时又要重新问用户）
    _, ctx = _json(fresh_cli, "context")
    rows = {a["task"]: a for a in ctx["active"]}
    assert rows.get("实验报告", {}).get("estimate_minutes") == 60, rows.get("实验报告")
    assert rows.get("预习报告", {}).get("estimate_minutes") == 30, rows.get("预习报告")

    # ② 视图：没有估时的条目不出现这个键（与 priority/lead_days 同一规矩）
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "e.json", {"list": {"upsert": [{
        "task": "没估时的事", "date": d2, "due_time": "23:59", "status": "pending"}]}}))
    _, ctx2 = _json(fresh_cli, "context")
    bare = [a for a in ctx2["active"] if a["task"] == "没估时的事"][0]
    assert "estimate_minutes" not in bare, "没有估时却带出了键：%s" % bare

    # ③ 单次任务也能直接写（用户对某一件说"这个留一小时"）
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "e2.json", {"list": {"upsert": [{
        "task": "单次的活", "date": d2, "due_time": "23:59", "status": "pending",
        "estimate_minutes": 90}]}}))
    _, ctx3 = _json(fresh_cli, "context")
    one = [a for a in ctx3["active"] if a["task"] == "单次的活"][0]
    assert one.get("estimate_minutes") == 90, one

    # ④ review：块比预留短 ⇒ 警告（不阻塞）
    rv = {"slots": [{"date": d1, "time": "20:00-20:40", "task": "实验报告"}]}
    _, out = _json(fresh_cli, "review", "--file", _mkdraft(fresh_work, "r.json", rv))
    kinds = [w.get("kind") for w in out.get("warnings") or []]
    assert "slot_shorter_than_estimate" in kinds, out.get("warnings")
    hit = [w for w in out["warnings"] if w.get("kind") == "slot_shorter_than_estimate"][0]
    assert hit["slot_minutes"] == 40 and hit["estimate_minutes"] == 60, hit
    assert out.get("ok") is True, "块短只是警告，不该阻塞：%s" % out

    # ⑤ 块够长 ⇒ 不再报
    rv2 = {"slots": [{"date": d1, "time": "19:00-20:30", "task": "实验报告"}]}
    _, out2 = _json(fresh_cli, "review", "--file", _mkdraft(fresh_work, "r2.json", rv2))
    kinds2 = [w.get("kind") for w in out2.get("warnings") or []]
    assert "slot_shorter_than_estimate" not in kinds2, out2.get("warnings")

    return "物化继承 ✓ 视图透出/缺省 ✓ 单次可写 ✓ 块短警告 ✓"

def case_reminders(cli: str, work: str) -> str:
    """★ 提醒**独立于任务与规划**（用户 2026-11-08）。

    用户原话：「比如我想提醒明天中午取快递这种东西 带有"提醒我"这样的 不算任务但是算代办的
    应该独立于基础的系统。比如你看最新的 我想明天中午取快递 但是由于审查系统被扔到下午了」

    事故：这类东西被登记成**没有 date 的任务** ⇒ 注册表里它是"永远活跃的活" ⇒
    规划器按课间/整块/晚上不排去搬它 ⇒ "中午"变成"随便什么时候"。

    守六条：
      · 独立存储：读得到，但**不进注册表 / 不进规划**
      · 到点判定：过了点但在迟到窗口内 ⇒ due；更早的只算 missed，不再打扰
      · 投递标记：管道发完记 fired_at ⇒ 不会重复发
      · 叫醒：next-tick 会为提醒单独产生一个唤醒点（不依赖规划里有没有块）
      · 形状写错要被拒（text 空）
    """
    fresh_cli, fresh_work = _fresh()
    tomorrow = _d(1)
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "rm.json", {"reminders": {"upsert": [
        {"date": tomorrow, "time": "12:00", "text": "取快递"}]}}))
    _, out = _json(fresh_cli, "reminders", "--days", "3")
    assert out["count"] == 1, out
    r = out["reminders"][0]
    assert r["text"] == "取快递" and r["time"] == "12:00", r
    assert r["key"].startswith(tomorrow + "|12:00|"), r

    # ① 不进注册表、不进规划（这次事故的核心）
    _, ctx = _json(fresh_cli, "context")
    assert not [a for a in ctx["active"] if "取快递" in str(a.get("task"))], ctx["active"]
    assert any(x["text"] == "取快递" for x in ctx["reminders"]), ctx["reminders"]

    # ② 到点判定（迟到窗口内）
    past, past_t = _due_spec(-180)
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "rm2.json", {"reminders": {"upsert": [
        {"date": past, "time": past_t, "text": "已经过点的"}]}}))
    _, due = _json(fresh_cli, "reminders", "--due")
    hit = [x for x in due["reminders"] if x["text"] == "已经过点的"]
    assert hit and hit[0].get("late_minutes", 0) >= 170, due

    # ③ 投递标记 ⇒ 不再重复发
    key = hit[0]["key"]
    _, fired = _json(fresh_cli, "reminder-fired", "--key", key)
    assert fired["fired"] == [key], fired
    _, due2 = _json(fresh_cli, "reminders", "--due")
    assert not [x for x in due2["reminders"] if x["text"] == "已经过点的"], due2

    # ④ next-tick 为提醒单独叫醒（用 --after 把"现在"挪到明天 10:00 排除锚点干扰）
    _, nt = _json(fresh_cli, "next-tick", "--after", tomorrow + " 10:00")
    assert nt["kind"] == "reminder", nt
    assert nt["next_at"] == tomorrow + " 12:00", nt

    # ⑤ 形状写错 ⇒ 拒
    # 写错的 ERROR 走 stderr（结构化 JSON），所以用 _run 看退出码与 stderr
    code, _out, err = _run(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "rm3.json", {"reminders": {"upsert": [
        {"date": tomorrow, "text": ""}]}}))
    assert code != 0 and "error" in (err or ""), (code, (err or "")[:200])

    return "独立存储 ✓ 不进注册表 ✓ 到点/迟到窗口 ✓ 不重复发 ✓ 叫醒 ✓ 拒坏形状 ✓"

def case_schedule_guards(cli: str, work: str) -> str:
    """★ 三条课表机械防线（2026-11-10 事故：示例课程D"丢了"）。

    事故：Agent 把第10周周四的**绿色调课**当成"周二那节被挪走了"，于是给周二 5-6
    的示例课程D打了 `kind: cancelled`（图上那格是蓝色=正常上课）⇒ 课在视图里消失。
    同一轮它还把第9周周四的两格（示例课程B 5-6 示例教室X1 / 示例课程G 2-4）整段抄到了第10周。

    守三条（都是纯结构判断，不需要理解语义）：
      · duplicate_sessions     同一 (date, periods|time) 有多条
      · cancelled_without_note 标了停课却没写依据
      · cancelled_vs_moved     同一周同一门课既有 cancelled 又有 moved ⇒ 极可能调课当停课
    """
    fresh_cli, fresh_work = _fresh()
    d1, d2 = _d(1), _d(3)
    bad = [
        # ① 重复：同一格两条
        {"date": d1, "time": "08:00-09:40", "course": "重复课甲"},
        {"date": d1, "time": "08:00-09:40", "course": "重复课甲"},
        # ② 停课没写依据
        {"date": d1, "time": "10:10-11:50", "course": "悄悄停掉的课", "kind": "cancelled"},
        # ③ 同一周：课乙 停课 + 调课（把调课当停课的典型形状）
        {"date": d1, "time": "14:20-16:00", "course": "课乙", "kind": "cancelled"},
        {"date": d2, "time": "19:00-20:40", "course": "课乙", "kind": "moved"},
    ]
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "g.json", {"schedule": {
        "replace_future": True, "dated_sessions": bad}}))
    code, out = _json(fresh_cli, "anomalies", expect_ok=False)
    kinds = [a.get("type") for a in out.get("anomalies") or []]
    for want in ("duplicate_sessions", "cancelled_without_note", "cancelled_vs_moved"):
        assert want in kinds, "没报出 %s：%s" % (want, kinds)

    # ④ 修干净之后这三条都不该再报
    good = [
        {"date": d1, "time": "08:00-09:40", "course": "重复课甲"},
        {"date": d1, "time": "10:10-11:50", "course": "悄悄停掉的课", "kind": "cancelled",
         "note": "第10周图上周三这一格是灰色停课"},
        {"date": d1, "time": "14:20-16:00", "course": "课乙"},
        {"date": d2, "time": "19:00-20:40", "course": "课乙", "kind": "moved",
         "note": "第10周图上绿色：调到周四晚上"},
    ]
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "g2.json", {"schedule": {
        "replace_future": True, "dated_sessions": good}}))
    _, out2 = _json(fresh_cli, "anomalies", expect_ok=False)
    kinds2 = [a.get("type") for a in out2.get("anomalies") or []]
    for bad_kind in ("duplicate_sessions", "cancelled_without_note", "cancelled_vs_moved"):
        assert bad_kind not in kinds2, "修干净后还在报 %s：%s" % (bad_kind, kinds2)

    return "重复 ✓ 停课无依据 ✓ 调课当停课 ✓ 修干净后不再报 ✓"

def case_week_and_diff(cli: str, work: str) -> str:
    """★ 严格照课表：`week` 完整回读 + 写课表必须返回**逐格差异**（用户 2026-11-10）。

    用户原话：「就是户外改教室 这些都是随着课表变化的 要求AGENT严格按照课表来」
    ⇒ 教室/户外/节次每周都变，所以"照当周图逐格抄 + 抄完逐条念"要做成机制：
      · `week --date`：那一周每天几节、课名、**教室**、停课/调课的依据，全都列出来
      · `apply` 改课表后：`schedule_diff` 列出新增/删除/**改了教室**的格子 ——
        Agent 不念就等于没核对
    """
    fresh_cli, fresh_work = _fresh()
    d = _d(1)
    _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "w1.json", {"schedule": {
        "replace_future": True, "dated_sessions": [
            {"date": d, "time": "08:00-09:40", "course": "示例课程C", "location": "校外场地"},
            {"date": d, "time": "10:10-11:50", "course": "示例课程J", "location": "教学楼 示例教室X1"},
            {"date": d, "time": "14:20-16:00", "course": "示例课程D", "location": "教学楼 示例教室X1"},
        ]}}))

    # ① week：这一周的完整回读（教室必须在）
    _, wk = _json(fresh_cli, "week", "--date", d)
    assert wk["week_start"] <= d <= wk["week_end"], wk
    day = [x for x in wk["days"] if x["date"] == d][0]
    assert len(day["entries"]) == 3, day
    assert day["entries"][0]["location"] == "校外场地", day["entries"][0]
    assert wk["count"] == 3, wk["count"]

    # ② 改课表：户外 → 教室 + 删一节 + 加一节 ⇒ 差异必须如实列出来
    code, out = _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "w2.json", {"schedule": {
        "replace_future": True, "dated_sessions": [
            {"date": d, "time": "08:00-09:40", "course": "示例课程C", "location": "教学楼 示例教室X1"},
            {"date": d, "time": "14:20-16:00", "course": "示例课程D", "location": "教学楼 示例教室X1"},
            {"date": d, "time": "16:30-18:10", "course": "示例课程B", "location": "教学楼 示例教室X1"},
        ]}}))
    sd = out.get("schedule_diff") or []
    kinds = {x.get("kind") for x in sd}
    assert kinds == {"added", "removed", "changed"}, sd
    changed = [x for x in sd if x["kind"] == "changed"][0]["entries"][0]
    assert changed["course"] == "示例课程C", changed
    assert changed["fields"]["location"] == {"was": "校外场地", "now": "教学楼 示例教室X1"}, changed
    removed = [x for x in sd if x["kind"] == "removed"][0]["entries"]
    assert [e["course"] for e in removed] == ["示例课程J"], removed
    added = [x for x in sd if x["kind"] == "added"][0]["entries"]
    assert [e["course"] for e in added] == ["示例课程B"], added

    # ③ 没改东西 ⇒ 不该硬塞一份空差异
    _, out2 = _json(fresh_cli, "apply", "--file", _mkdraft(fresh_work, "w3.json", {"schedule": {
        "replace_future": True, "dated_sessions": [
            {"date": d, "time": "08:00-09:40", "course": "示例课程C", "location": "教学楼 示例教室X1"},
            {"date": d, "time": "14:20-16:00", "course": "示例课程D", "location": "教学楼 示例教室X1"},
            {"date": d, "time": "16:30-18:10", "course": "示例课程B", "location": "教学楼 示例教室X1"},
        ]}}))
    assert "schedule_diff" not in out2, out2.get("schedule_diff")

    return "week 回读带教室 ✓ 改教室被点名 ✓ 增删被点名 ✓ 无改动不硬塞 ✓"

def main() -> int:
    work = tempfile.mkdtemp(prefix="qyn-smoke-main-")
    try:
        cli = _setup(work)
        print("=" * 62)
        print("  qyn.py 端到端冒烟测试（临时目录，未触碰真实数据）")
        print("=" * 62)
        cases = [
            ("只读与拒绝的写都零副作用", lambda: case_no_side_effects(cli, work)),
            ("一次性截止日期增改", lambda: case_deadline_create_then_update(cli, work)),
            ("拒绝：非法日期 / 名字不完全一致", lambda: case_rejections(cli, work)),
            ("★ 物化：课表与锚定周期任务", lambda: case_sync_materializes(cli, work)),
            ("sessions 视图按日期", lambda: case_sessions_view(cli, work)),
            ("课表增改删：增量 + 整批替换（原子）", lambda: case_schedule_edits(cli, work)),
            ("异常清单：检出 / 空课表明说 / 随状态更新 / 久未维护", lambda: case_anomalies_suite(cli, work)),
            ("★ 注册按日期滚转向前", lambda: case_roll_forward(cli, work)),
            ("★ 冷存储与历史视图：搬得出去、查得回来、视图有界", lambda: case_archive_and_history(cli, work)),
            ("★ 写入口：增量只写要改的 + 整份重写原子", lambda: case_write_port(cli, work)),
            ("★ upsert 继承关系字段（周期来源/课次身份）", lambda: case_relation_fields(cli, work)),
            ("★ 紧迫度是算出来的（不是填出来的）", lambda: case_urgency(cli, work)),
            ("删除周期任务定义", lambda: case_remove_periodic(cli, work)),
            ("★ 通用写入端口 + 注册表详情", lambda: case_apply_port(cli, work)),
            ("★ 计划的整条命：读得回 / 只给事实 / 文件有界 / 写作废", lambda: case_plan_lifecycle(cli, work)),
            ("★ 无 DDL 的条目：不驱动紧迫度 / 排最后 / 不消失 / 往后延", lambda: case_no_ddl_lifecycle(cli, work)),
            ("★ 一学期课表整条路径（写入 / 物化窗口 / 视图 / 上下文）", lambda: case_timetable_bulk(cli, work)),
            ("★ 不以周作为计数方法（守住）", lambda: case_no_week_counting(cli, work)),
            ("★ 取消过的那次不会被复活", lambda: case_cancelled_occurrence_stays_dead(cli, work)),
            ("★ 日历周期（每周几，与上课无关）", lambda: case_calendar_periodic(cli, work)),
            ("★ 过点即默认完成（不再管过期）", lambda: case_expired_rolls_off(cli, work)),
            ("★ 循环任务只报不替 Agent 决定", lambda: case_sync_reports_not_decides(cli, work)),
        ("★ 把关族：撞课/重叠/漏排 + 课间硬约束 + 排到截止之后 + 畸形快照", lambda: case_brief_guards(cli, work)),
            ("★ 三种输出的形状（内核保证）", lambda: case_brief_shapes(cli, work)),
        ("★ 到点唤醒：下一个边界由规划决定", lambda: case_next_tick(cli, work)),
        ("★ 循环闸与快照去重", lambda: case_loop_gate(cli, work)),
        ("★ 水课跟着课程（不是课次）", lambda: case_water_follows_course(cli, work)),
        ("★ 整段替换课表不许删掉今天及以后的课次", lambda: case_schedule_replace_guard(cli, work)),
        ("★ 只改课表/规则也让规划作废（stale）", lambda: case_plan_stale_inputs(cli, work)),
        ("★ 写命令有互斥写锁（锁被占时明确报错）", lambda: case_write_lock(cli, work)),
        ("★ 课表覆盖不足 14 天要机械报出来", lambda: case_horizon_check(cli, work)),
        ("★ 任务有开始时间（start）时它与 DDL 分开算", lambda: case_start_time(cli, work)),
        ("★ 水课不算撞课", lambda: case_water_not_clash(cli, work)),
        ("★ wall 只读视图：结构（含 why）+ 过滤 / 保留 / 排序", lambda: case_wall_view(cli, work)),
            ("validate 覆盖三文件", lambda: case_validate_reports_all(cli, work)),
            # ── 内核口径统一（task-2）补的守门用例 ──
            ("★ 停课不算课（只读视图/日程/撞课/anomalies 同口径）",
             lambda: case_cancelled_is_not_a_class(cli, work)),
            ("★ 算不出时刻的课次：进异常、review/brief 都警告",
             lambda: case_session_without_time(cli, work)),
            ("★ brief 默认只展开 3 天 + --days 非法值报错",
             lambda: case_brief_default_days(cli, work)),
            ("★ allow 四连：preference 豁免 / 只加 allow 也落库 / constraint 无豁免 / 拼错要被报",
             lambda: case_allow_exemptions(cli, work)),
            ("★ 结束时刻 24:00 的口径（schema/passed/两个消费者）",
             lambda: case_late_night_window(cli, work)),
            # ── task-5：同名字条目的寻址 ──
            ("★ 同名字条目寻址：新增/更新/拒绝/消歧（upsert/remove/mark --date）",
             lambda: case_same_name_addressing(cli, work)),
            # ── task-6：相对课表的规则窗口 ──
            ("★ 规则窗口 before_first_class：上午有课才生效，review/brief 同口径",
             lambda: case_first_class_window(cli, work)),
            # ── task-7：预留时长（规划辅助事实，用户 2026-11-08）──
            ("★ 预留时长 estimate_minutes：周期任务继承 / 视图透出 / review 拦块短",
             lambda: case_estimate_minutes(cli, work)),
            # ── task-8：提醒（独立于任务与规划，用户 2026-11-08）──
            ("★ 提醒 reminders：不算任务、不进规划，到点由管道投递",
             lambda: case_reminders(cli, work)),
            # ── task-9：课表机械防线（2026-11-10 示例课程D"丢了"）──
            ("★ 课表防线：重复 / 停课无依据 / 调课被当停课",
             lambda: case_schedule_guards(cli, work)),
            # ── task-10：严格照课表（week 回读 + 逐格差异，用户 2026-11-10）──
            ("★ 严格照课表：week 完整回读 + 写课表必须报逐格差异",
             lambda: case_week_and_diff(cli, work)),
        ]
        passed = failed = 0
        for name, fn in cases:
            try:
                detail = fn()
                passed += 1
                print(f"[PASS] {name}")
                if detail:
                    print(f"       {detail}")
            except AssertionError as exc:
                failed += 1
                print(f"[FAIL] {name}")
                print(f"       {exc}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"[FAIL] {name}")
                print(f"       {type(exc).__name__}: {exc}")
        print("-" * 62)
        print(f"合计 {passed + failed} 项，通过 {passed}，失败 {failed}")
        return 0 if failed == 0 else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
