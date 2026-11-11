#!/usr/bin/env python3
"""qyn.py — 信息中心 · 确定性内核 CLI

迁移定位（见 项目主页的迁移文档）：
  DSH 负责「会话 + 语义 + 调度」，本 CLI 负责「确定性计算 + 结构校验」。
  本文件里**没有** LLM 调用、没有 subprocess、没有网络 —— 语义一律由模型做，
  结构合法性一律由这里保证（项目开发风格：确定性→代码，语义→模型）。

三个文件（2026-10 架构）：
  rules.json     规则层：规划偏好 / 约束
  schedule.json  周期层：作息时间 + **带日期的**每一节课 + 作业/考试周期
  data.json      LIST 层：物化后的待办清单 —— 规划与提醒的**唯一权威**

★★ **不做周推算**（用户明确要求）：图片里有日期，一切日期都来自"外部给定"。
  学校会调休、停课、调课、换教室，"第N周周三"推不出"10月14日"，硬推必错。
  所以本文件里**没有** is_active_week / 奇偶周 / 周锚点 之类的概念。

设计约定：
  · 读命令（context / sessions / show / history / anomalies / validate）**不产生写盘副作用**。
  · 写命令一律经 `core.datastore`，落盘前过 `core.schema` 严格校验，违规拒绝。
  · 面向模型的输出**一律 UTF-8 JSON**（stdout）；错误走 stderr + 非零退出码。

用法（仓库根或 AGENT/ 下都能跑；Linux 上把 python 换成 python3）：
  python qyn.py context
  python qyn.py sessions --days 14
  python qyn.py apply --file 改写意图.json     # ★ 唯一写入口（课表/规则/注册表都走它）
  python qyn.py sync                 # 物化进注册表
  python qyn.py mark --task 示例课程A作业 --status done
  python qyn.py anomalies
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

# Windows 控制台默认 GBK：打任何非 ASCII 就 UnicodeEncodeError 直接崩（项目踩过）
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.paths import (RULES_FILE, SCHEDULE_FILE, DATA_FILE, ARCHIVE_FILE, ARCHIVE_DIR,  # noqa: E402
                        PLANS_FILE)
from core.schema import (validate_rules, validate_schedule, validate_data,  # noqa: E402
                         validate_archive, validate_plans)
# ★ 纯几何从 core.session 拿（**不能**从 core.datastore 拿 —— 那个模块 import 时就建
#   store、会读盘，而读命令必须零副作用。这正是当初抽出 core/paths.py 的同一理由）。
from core.session import (session_key_of, session_time_of,  # noqa: E402
                          session_window_of)

# ★ S0 拆分：这三组函数已搬进 core/，名字不变 ⇒ 下面的调用点一行没改。
from core.io import (_load, _fingerprint, _plan_inputs, _plan_invalidated,  # noqa: E402,F401
                     DISAMBIGUATORS)
from core.schema import validate_reminders, DEFAULT_REMINDER_TIME  # noqa: E402
from core.timeaxis import (_hhmm, _mins, _span, _due_moment, _fmt_span,  # noqa: E402,F401
                           _fmt_until, _starts_in, _now_view)
from core.schedule import (_is_cancelled, _is_water, _visible_sessions,  # noqa: E402,F401
                           _active, _guard_schedule_replace)


# ────────────────────────── 只读层（无副作用） ──────────────────────────


# 「远未来」的日期上界（ISO 日期按字典序 = 按时间序，字符串比较就是日期比较）。
# 用来把 `_upcoming` 交给 `_sessions_between` —— **同一语义只留一份实现**。
FAR_FUTURE = "9999-12-31"


def _upcoming(sched: dict, now: datetime | None = None) -> list[dict]:
    """**还没结束的课**（日期未过，且今天那些还没下课）。

    ★ 交给模型的课次视图只给这一段：课表每周往里加，全量返回会让上下文**线性增长**。
    ★ **时刻相关**：晚上 11 点问"接下来有什么课"，今天早上那节已经上完了 ——
      它不该再算进来。这正是"早上问和晚上问应当不同"。

    ★★ 这里**不再自己实现一遍**：它就是 `_sessions_between(今天, 远未来, now=now)`。
      原先两处各写一份（过滤条件几乎逐行相同）—— 同一语义两份实现，迟早漂移
      （漂移一次就是"停课被当成下一节"这类事故）。`lean=False` 要的是**原始条目**：
      `cmd_context` 还要按 name 取 `_class_view` 与算 `class_count`。
    """
    now = now or datetime.now()
    return _sessions_between(sched, now.strftime("%Y-%m-%d"), FAR_FUTURE,
                             now=now, lean=False)


def _compact(today: str = "") -> dict:
    """把**已过期**的内容从热文件搬进冷存储（幂等）。

    ★ 为什么必须做：日期滚转只是**视图过滤** —— 过期的看不见了，但文件只增不减，
      而 `state` 会把 `dated_sessions` 全量交给模型 ⇒ 上下文随使用时间**线性增长**。
      搬走之后热文件是常数大小；`history` 需要时查冷存储，历史不丢。

    顺序：**先写冷存储、再从热文件删**。反过来的话中间崩了就是真丢。
      万一"写成功、删失败"，下一次跑的按身份去重会让冷存储不重复累积 —— 可收敛。
    """
    from core.datastore import archive as arch
    from core.datastore import data as data_store
    from core.datastore import schedule as sched_store

    today = today or datetime.now().strftime("%Y-%m-%d")
    sess_cut = _shift(today, -SESSION_KEEP_DAYS)
    list_cut = _shift(today, -LIST_KEEP_DAYS)

    move_s = [s for s in sched_store.dated_sessions if s.get("date", "") < sess_cut]
    keep_s = [s for s in sched_store.dated_sessions if s.get("date", "") >= sess_cut]
    # ★ 无硬截止的**未完成**软任务**不搬冷存储** —— 它还在活跃集里（只有完成/取消能让它消失），
    #   搬走就等于"从视图里删掉"，那与用户的口径冲突。
    def _movable(d: dict) -> bool:
        if not d.get("date"):
            return False       # ★ 没有 DDL ⇒ 还在活跃集里，搬走就是从视图里删掉
        if d.get("date", "") >= list_cut:
            return False
        return True

    move_r = [d for d in data_store.registry if _movable(d)]
    keep_r = [d for d in data_store.registry if not _movable(d)]

    if not move_s and not move_r:
        return {"sessions": 0, "registry": 0}

    added = arch.absorb(move_s, move_r)
    if move_s:
        sched_store.import_bundle({"dated_sessions": keep_s})
    if move_r:
        data_store.replace_registry(keep_r)
    return added



# 没有可跟的规划边界时，回落到次日这个时刻（控制层的每日锚点也是它）
ANCHOR_HHMM = "07:00"


def _padded(mins_: int) -> str:
    return f"{mins_ // 60:02d}:{mins_ % 60:02d}"


def _slot_rows(slots) -> tuple[list, list]:
    """把 slots 整理成 (rows, bad)。rows = [(date, "HH:MM", "HH:MM", task)]（时刻**补零归一**），
    bad = 结构不完整的项（进 problems，不给正文）。

    ★ 唯一实现，`review` 与 `brief` 都走它。畸形 slot 曾经有三种结局：
      静默放行（`time:"09:35"`）、KeyError 崩溃（缺 date/task）、ValueError 崩溃（date 非法）。
    """
    rows, bad = [], []
    for s in slots or []:
        if not isinstance(s, dict):
            bad.append({"kind": "bad_slot", "slot": str(s)[:60], "note": "slot 必须是对象"})
            continue
        task = str(s.get("task") or "").strip()
        date = str(s.get("date") or "").strip()
        span = _span(s.get("time"))
        if not task or not date or span is None:
            bad.append({"kind": "bad_slot", "date": date, "slot": str(s.get("time") or ""),
                        "task": task or "(无 task)",
                        "note": "每块都要有 task、date、time（HH:MM-HH:MM）"})
            continue
        rows.append((date, _padded(span[0]), _padded(span[1]), task))
    return rows, bad


def _plan_key(plan) -> str:
    """规划正文的规范化指纹（去重用：整份比，不只是 slots —— 否则"只补 allow 豁免"存不进去）。"""
    return json.dumps(plan or {}, sort_keys=True, ensure_ascii=False)


def _unscheduled(dat: dict, planned: set, now: datetime | None = None) -> list[dict]:
    """**有 DDL 却没有任何时段**的活跃条目（漏排）—— `review` 与 `brief` 共用。

    ★ 唯一实现。`review` 一直有这一步，而 `brief`（**发出去前的最后一道闸**）没有：
      实测快照漏掉当天有 DDL 的任务时，`review` 报 `unscheduled=['DUE_TODAY']`
      而 `brief ok=true` 直接把正文给出去 —— 用户拿到的规划里没有今天要交的东西。
    ★ 「无 DDL，可后补」的不阻塞（内核只如实说"它没有截止"）；课次条目不是任务。
    ★ **拦不拦各有其职，判据只有这一份**：`review` 只拦时间冲突（干净草稿必须
      `ok=true`，已有断言守住）；`brief` 对**今天到期**的漏排判阻塞 ——
      远期 DDL 天然不在本周计划里，报出来即可，不能因此拦下正文。
    """
    return [{"task": d["task"], "date": d.get("date"),
             "why": "有 DDL 却没有任何时段" if d.get("date") else "无 DDL，可后补"}
            for d in _active(dat, now=now)
            if not d.get("session_key") and d["task"] not in planned]


def _due_by_task(dat: dict) -> dict:
    """任务名 → **最早**的硬截止时刻（同名多条取最早，保守）。

    ★ 唯一实现：`_past_deadline`（brief/review 的把关）与 `wall`（大屏标红）都用它 ——
      否则"哪条 DDL 算数"会有两份说法。
    · 只算**未终结的任务条目**：`status=pending` 且不是课次（`session_key`）
    · 没有 `date` 的条目**不受 DDL 约束**（`_due_moment` 返回 None）
    """
    out: dict = {}
    for d in dat.get("registry", []):
        if d.get("status") != "pending" or d.get("session_key"):
            continue
        m = _due_moment(d)
        if not m:
            continue
        name = str(d.get("task") or "")
        if name and (name not in out or m < out[name]):
            out[name] = m
    return out


def _past_deadline(rows, dat: dict, now: datetime | None = None) -> tuple[list, list]:
    """块排在了它自己的**硬截止之后** —— 等于没排（用户 2026-10-15 报的真问题）。

    实测：明早 10:10 上课要讲的「示例课程作业 HW#」，规划把它排到了**第二天晚上** ——
    课堂早过了，而 `brief` 照样 `ok=true` 把正文发出去（把关没有这一条）。

    ★ 只做**时间算术**：登记的 `date` / `due_time` 是硬截止（用户定的语义），
      块的时刻在计划里 —— 两者一比就知道"这块永远赶不上"。内核不判断该不该做，
      也不替 Agent 决定怎么办。
    两种结局，口径不同：
      · 截止**还没到**（改得动）⇒ 进 problems/conflicts —— **禁止**，
        逼出"往前挪；实在排不下就**醒目地**说排不下"，而不是假装排上了
      · 截止**已经过了**（追不回）⇒ 进 warnings —— 不阻塞，但报告里要点名
    压在截止上的块（开始早于截止、结束晚于截止）⇒ 警告 `crosses_deadline`。
    同名多条（周期任务的不同次发生）取**最早**的那个截止（保守）。
    """
    now = now or datetime.now()
    due = _due_by_task(dat)
    hard, soft = [], []
    for day, a, b, task in rows:
        m = due.get(task)
        if not m:
            continue
        due_day, due_min = m.strftime("%Y-%m-%d"), m.hour * 60 + m.minute
        try:
            start, end = _mins(a), _mins(b)
        except (ValueError, TypeError):
            continue
        if (day, start) > (due_day, due_min):
            hit = {"kind": "past_deadline", "task": task, "date": day, "slot": f"{a}-{b}",
                   "deadline": m.strftime("%Y-%m-%d %H:%M"),
                   "note": (f"这块排在「{task}」截止（{m:%m-%d %H:%M}）**之后** —— 等于没排。"
                            f"往前挪到截止之前；实在排不下就**醒目地告诉用户排不下**"
                            f"（说明卡在哪、给可选方案），不要假装排上了。")}
            (hard if m > now else soft).append(hit)
        elif (day, end) > (due_day, due_min):
            soft.append({"kind": "crosses_deadline", "task": task, "date": day,
                         "slot": f"{a}-{b}", "deadline": m.strftime("%Y-%m-%d %H:%M"),
                         "note": f"这块跨过「{task}」的截止（{m:%m-%d %H:%M}）—— 只有前半段赶得上。"})
    return hard, soft


def _unknown_class_time(sched: dict, dates: set) -> list[dict]:
    """这些日期上**算不出时刻**的课次（撞课 / 课间**没有比**，静默漏过）。

    ★ `core/session.py` 查不到节次时返回 `("", "")`（内核**不编时间**），
      原先 `review` / `brief` 两侧都 `continue` —— 实测 `periods: {}` + 课次
      `periods: "3-4"` 得到 `review ok=true, conflicts=[]`：撞课根本没比，
      异常清单也是空的。这里把"没比成"如实报出来（先警告；稳定后再考虑升阻塞）。
    唯一实现，`review` 与 `brief` 共用；`dates` 只算这次真要比的日期，避免整表噪声。
    """
    periods = sched.get("periods", {})
    out = []
    for c in _visible_sessions(sched):
        if c.get("date") not in dates:
            continue
        if session_window_of(c, periods) == ("", ""):
            out.append({"kind": "unknown_class_time", "date": c.get("date"),
                        "course": c.get("course", ""), "periods": c.get("periods", ""),
                        "note": "这节算不出时刻（作息表 `periods` 里查不到相关节次）—— "
                                "撞课与课间**没有比**，补齐节次/作息表再排"})
    return out


def _class_gap(d, a, b, sched, periods) -> dict | None:
    """块 (a,b) 是否落在**同一天两节课之间的空隙**里；是则返回 {gap_minutes, between}。

    ★ 课间禁排的**唯一实现**（用户 2026-10-15：要改教室、要转场，没时间写东西）。
      `cmd_review` 与 `cmd_brief` 都调它 —— 曾经两边各写一份，brief 那份拿**同一节课**
      当"前一节"和"后一节"（`a >= cb` 且 `ca >= b`，而 cb > ca）⇒ 恒不成立，是死代码，
      于是"硬约束"只在 review 一侧成立，课间块照样发到用户手上。
    水课不算"课"（它的时段是可用的，不用转场）。空隙 ≥60 分钟不算课间（如午休 2.5 小时）。
    """
    segs = []
    for c in _visible_sessions(sched):                 # 停课不算"课"（唯一口径）
        if c.get("date") != d or _is_water(sched, c):
            continue
        s, e = session_window_of(c, periods)
        sp = _span(f"{s}-{e}") if (s and e) else None
        if sp:
            segs.append((sp[0], sp[1], str(c.get("course", ""))))
    A, B = _mins(a), _mins(b)
    for s1, e1, n1 in segs:
        if e1 > A:
            continue
        for s2, e2, n2 in segs:
            if s2 < B:
                continue
            gap = s2 - e1
            if gap < 60:
                return {"gap_minutes": gap, "between": f"{n1} → {n2}"}
    return None


# `before_first_class` 的默认生效阈值：当天第一节课开始 **≤ 它**，才算"上午有课"。
# ★ 它是**默认值**，规则里写 `max_start` 即可覆盖 —— 阈值不是藏在代码里的魔法数。
FIRST_CLASS_MAX_START = "12:00"


def _first_class_min(sched: dict, date: str) -> tuple:
    """当天第一节课的**开始分钟数**与课名；当天没有可用的课 ⇒ `(None, "")`。

    ★ 只认**可上课次**（`_visible_sessions`：停课不算课）—— "当天有哪些课"只有这一份实现。
    ★ 算不出时刻的课次（作息表查不到相关节次）**跳过、不报错**：相对窗口宁可当天不生效，
      也不能拿一个编出来的时刻去判用户。
    """
    best = None
    for c in _visible_sessions(sched):
        if c.get("date") != date:
            continue
        t = session_window_of(c, sched.get("periods", {}))[0]
        if not t:
            continue
        hm = _hhmm(t)
        if hm is None:
            continue
        mins = hm[0] * 60 + hm[1]
        if best is None or mins < best[0]:
            best = (mins, str(c.get("course", "")))
    return best or (None, "")


def _window_hits(rules: dict, sched: dict, rows: list) -> list[dict]:
    """结构化规则窗口的**唯一实现**（`review` 与 `brief` 共用）。

    两类窗口：
      · `no_slots`           —— **绝对**时段（固定时刻），如 `["19:00-23:59"]`
      · `before_first_class` —— **相对当天课表**：`00:00 → 当天第一节课开始`，
        且**仅当**该开始时刻 ≤ `max_start`（默认 `FIRST_CLASS_MAX_START` = 12:00）时生效
        （这就是"当天上午有课"的表达方式 —— 阈值显式写出，内核不猜"上午"是什么意思）

    `rows` = `[(date, 开始, 结束, task)]`（`_slot_rows` 的形状；`review` 用同样的元组）。
    返回的每项都带 `rule_kind`，调用方据此分流：`constraint` ⇒ 阻塞（conflicts/problems），
    `preference` ⇒ 警告（warnings；`brief` 里可被快照正文的 `allow` 豁免）。

    ★ 当天没课 / 只有停课 / 节次查不出时刻 ⇒ **不生效，也不报错**（规则那天不管用而已）。
    ★ 为什么必须共用：原先 `review` 与 `brief` 各自内联一份 `no_slots` 判据，
      这个项目刚因此翻过车（`brief` 的课间判据变成死代码，只有 `review` 生效）。
    """
    out = []
    for r in rules.get("planning_rules", []):
        rid, rk, body = r.get("id", ""), r.get("kind", "preference"), r.get("body", "")

        def _hit(win: str, d, a, b, task, extra=None) -> None:
            rec = {"kind": "violates_rule", "rule": rid, "rule_kind": rk, "window": win,
                   "date": d, "slot": f"{a}-{b}", "task": task, "body": body}
            rec.update(extra or {})
            out.append(rec)

        for w in (r.get("no_slots") or []):
            ww = _span(w)
            if not ww:
                continue
            for d, a, b, task in rows:
                xw = _span(f"{a}-{b}")
                if xw and xw[0] < ww[1] and ww[0] < xw[1]:
                    _hit(w, d, a, b, task)

        spec = r.get("before_first_class")
        if not isinstance(spec, dict):
            continue
        thr = _hhmm(str(spec.get("max_start") or FIRST_CLASS_MAX_START))
        if thr is None:
            continue                        # 阈值坏 ⇒ 不生效（schema 已挡在写入前）
        thr_min = thr[0] * 60 + thr[1]
        for d, a, b, task in rows:
            start_min, course = _first_class_min(sched, d)
            if start_min is None or start_min > thr_min:
                continue                    # 当天没课 / 第一节课太晚 ⇒ 规则当天不生效
            xw = _span(f"{a}-{b}")
            if xw and xw[0] < start_min:    # 块的**开始**落在"上课前"的窗口里
                _hit(f"00:00-{_padded(start_min)}", d, a, b, task, {
                    "first_class": _padded(start_min), "course": course,
                    "note": (f"当天第一节 {_padded(start_min)} {course}"
                             f"（≤ {_padded(thr_min)}）⇒ 上课前不排")})
    return out


def _passed_at(date: str, time: str, now: datetime) -> bool:
    """时段 `time`（`HH:MM-HH:MM`）在某天是否**已经过去** —— `passed` 的唯一实现。

    ★ `_plan_view`（`context.last_plan`）与 `wall` 原先各写一份，**两处同错**：
      都用 `min(hm[0], 23)` 夹小时。而 `_hhmm(last=True)` 本来就允许 `24:00`
      （见它的 docstring："一天的结束"）⇒ `23:00-24:00` 的块在 23:01 就被标 `passed`
      （实测：把 now 拨到当天 23:30，`context.last_plan` 与 `wall` 都给 `passed=true`）。
      正确口径：`24:00` = **次日 00:00**，用 timedelta 加，不夹小时。
    日期解析不了 ⇒ 返回 False（**不猜**）。
    """
    try:
        day = datetime.strptime(date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    hm = _hhmm(time, last=True) or (23, 59)
    return day + timedelta(hours=hm[0], minutes=hm[1]) < now


def _urgency(d: dict, now: datetime) -> tuple[int, str, str]:
    """**紧迫度**：算出来的，不是填出来的。返回 (score, level, why)。

    清单上的条目**必然**未完成（完成的不在清单上），所以 `status: pending` 不含信息；
    `priority` 若被随手填成 medium 同样是噪声。所以这里**算**。

    ★ **按小时，不按天**：一件"今天 23:59 交"的事，早上还有 16 小时、深夜只剩 1 小时。
      参照是**当前时刻**，不是"今天/明天"。

    分工：**算术**归内核（这里）；**语义**归模型（要不要提前准备 → 它写 `lead_days`）。
    """
    # 没有 DDL 日期 ⇒ 算不出余量，**如实说**（不下"不急"的判断）：
    # 只有 `due_time` 说明 DDL 记不全 —— 补 `date`，或确认它其实没有截止。
    if not d.get("date"):
        return 8, "later", ("没有 DDL 日期" if not d.get("due_time")
                            else "缺 DDL 日期（只有 due_time）")

    due = _due_moment(d)
    if due is None:
        return 30, "normal", "日期不可解析"
    hours = max(0.0, (due - now).total_seconds() / 3600)

    if hours < 3:
        score, why = 95, _fmt_span(hours)
    elif hours < 8:
        score, why = 88, _fmt_span(hours)
    elif hours < 24:
        score, why = 72, _fmt_span(hours)
    elif hours < 48:
        score, why = 55, _fmt_span(hours)
    elif hours < 96:
        score, why = 40, _fmt_span(hours)
    elif hours < 168:
        score, why = 25, _fmt_span(hours)
    else:
        score, why = 12, _fmt_span(hours)

    # ★ `why` 是 **DDL 那一行**的标签，只放**时间**（用户 2026-10-17：
    #   "需要提前几天准备这个信息为什么要进DDL DDL单纯就是一个时间才对啊"）。
    #   `lead_days` / `priority` 是**规划信号**（语义，归 Agent）—— 它们继续参与打分与排序
    #   （那是机械的），但由 `_list_view` 作为**独立字段**透出，不拼进标签。
    lead = int(d.get("lead_days") or 0)
    if lead:
        score += min(20, lead * 5)
    pr = d.get("priority")
    if pr == "high":
        score += 15
    elif pr == "low":
        score -= 10
    score = max(0, min(100, score))
    level = ("urgent" if score >= 80 else "soon" if score >= 50
             else "normal" if score >= 25 else "later")
    return score, level, why


def _list_view(entries: list[dict], now: datetime | None = None,
               with_detail: bool = False, urgency: bool = True) -> list[dict]:
    """注册条目的**列表视图**：剥掉 detail、去掉不含信息的字段、补上**算出来的紧迫度**。

    · 去掉 `detail`：正文可能很大 —— 列表只给 `has_detail` 标记，要详情用 `show`
    · 去掉 `status`：清单上的条目**必然**未完成，它不含信息
    · `priority` / `lead_days` **原样透出**（不当标签用）：紧迫度是算出来的排序依据，
      这两个是模型给的**规划信号** —— 分开摆，模型要规划时能看见，大屏的 DDL 行只显示时间
    · 补 `urgency`(urgent/soon/normal/later) 与 `why`（一句话说清凭什么）
    · 按紧迫度**排序** —— 确定性的事由代码做完，模型拿到的就直接是"该先做哪件"
    """
    if with_detail:
        return list(entries)
    now = now or datetime.now()
    rows = []
    for d in entries:
        e = {k: v for k, v in d.items()
             if k not in ("detail", "status")}
        # ★ 两个**规划信号**（不是 DDL 标签）：只在**有信息**时透出 ——
        #   `priority` 默认 medium 是编出来的噪声（用户早先指出过），`lead_days` 为 0 同理。
        #   它们仍参与打分排序（见 _urgency），只是不再拼进 `why`（用户 2026-10-17）。
        if e.get("priority") in (None, "medium"):
            e.pop("priority", None)
        if not e.get("lead_days"):
            e.pop("lead_days", None)
        # ★ 预留时长（用户 2026-11-08）：与 priority/lead_days 同一规矩 —— 没有就不带，
        #   有就原样透出给模型/大屏，内核不把它拼进 why（时间是时间，估时是估时）。
        if not e.get("estimate_minutes"):
            e.pop("estimate_minutes", None)
        if d.get("detail"):
            e["has_detail"] = True
        # ★ **开始时间**（用户 2026-10-27）：这件事最早什么时候能做。
        #   有 `start` 就透出，并算出"还要多久才能开始"（机械算术，语义仍是模型给的）。
        if d.get("start"):
            e["start"] = d["start"]
            if d.get("start_time"):
                e["start_time"] = d["start_time"]
            e["starts_in"], e["can_start"] = _starts_in(d, now)
        score, level, why = _urgency(d, now)
        if urgency:
            e["urgency"] = level
            e["why"] = why
        rows.append((score if urgency else 0, d.get("date", ""),
                     d.get("task", ""), e))
    if urgency:
        rows.sort(key=lambda r: (-r[0], r[1], r[2]))
    return [r[3] for r in rows]


def _calendar_anchor(pt: dict, taken: set, now: datetime) -> tuple:
    """**日历周期**的下一个日期 —— 纯日期算术，**不涉及"第N周"**。

    为什么必须有它（实测卡了两轮的真问题）：`follow_course` 只能锚到某门课的**课次**，
    而"每周三晚上 23:59 交"是**与上课无关**的日历规律。更要命的是：
    同一门课一周上两次时（示例课程D在周二、周四都有），课程锚点会取到**错的星期**
    —— 用户两次要求"周三交"，内核两次物化成"周二"。

    选中的日期要满足两个条件：
      · 那一刻**还没到**（过点的按"默认完成"翻篇，不再物化一件出来就过期的）
      · 不在 `taken` 里（这一天的已经登记过）
    """
    every = pt.get("every", "")
    hm = _hhmm(pt.get("due_time", "")) or (23, 59)

    def moment(day):
        return datetime(day.year, day.month, day.day, hm[0], hm[1])

    if every == "weekly":
        wd = pt.get("weekday")
        if not isinstance(wd, int) or not 1 <= wd <= 7:
            return None, ("every=weekly 需要 weekday（ISO：1=周一 … 7=周日，周三写 3）"
                          " —— 缺了它内核不知道是每周几，不猜")
        for i in range(0, 400):
            day = (now + timedelta(days=i)).date()
            if day.isoweekday() != wd or day.isoformat() in taken:
                continue
            if moment(day) < now:
                continue
            return day.isoformat(), ""
        return None, "未来 400 天内没有可用的星期"

    if every == "daily":
        for i in range(0, 400):
            day = (now + timedelta(days=i)).date()
            if day.isoformat() in taken or moment(day) < now:
                continue
            return day.isoformat(), ""
        return None, "未来 400 天内没有可用日期"

    return None, f"不认识的 every={every!r}（可用：weekly / daily）"


def _next_anchor(pt: dict, sched: dict, taken: set, now: datetime | None = None) -> tuple:
    """周期任务的下一个**锚点日期**，返回 (date | None, 失败原因)。

    两种模式：
      · `follow_course` —— 锚到那门课**当周实际那节课**。这是"不用周推算"的落点：
        截止日**不是算出来的**，而是来自录入的课表。
      · `every`（+`weekday`）—— **日历周期**：与上课无关的固定规律（每周三 23:59）。
        日期由纯日期算术得出，仍然是**具体日期**，不涉及周次。

    `taken`：该任务已登记过的锚点集合 —— 身份（anchored_to）与排期（date）分开，
    所以改期之后仍然认得出"这天的已经登记过"。
    """
    now = now or datetime.now()
    course = pt.get("follow_course", "")
    if course:
        # ★ 停课不产生作业截止 ⇒ 候选课次只取**可上课次**（`_visible_sessions`，唯一口径）
        sessions = sorted([s for s in _visible_sessions(sched) if s.get("course") == course],
                          key=lambda s: (s["date"], s.get("periods") or s.get("time") or ""))
        if not sessions:
            return None, f"课表里找不到课程「{course}」的任何课次"
        hm = _hhmm(pt.get("due_time", "")) or (23, 59)
        for s in sessions:
            if s["date"] in taken:
                continue
            # ★ 那一刻已经过了就不再物化（否则刚建出来就是过期的，"默认完成"口径下没意义）
            try:
                day = datetime.strptime(s["date"], "%Y-%m-%d")
            except ValueError:
                return None, f"课次日期不合法: {s.get('date')!r}"
            if day.replace(hour=hm[0], minute=hm[1]) < now:
                continue
            return s["date"], ""
        # ★ 这条消息是下游**最强**的信号，必须把两种"锚不上"分开说：
        #   ① 课表里**根本没有**这门课的未来课次（课表覆盖不到）⇒ 该补课表
        #   ② 有未来课次，但都已物化过（taken 里）⇒ 本次不必再物化
        #   2026-10-21 实测：原来只写"所有课次都已登记过"，Agent 以为是①的反面，
        #   于是**拿别的课（10/23 示例课程）当锚**，把示例课程A的作业登记成了示例课程的。
        #   并且明确写一句"别拿别的课当锚" —— 这是它当时踩的坑。
        last = max((s["date"] for s in sessions), default="")
        today = now.strftime("%Y-%m-%d")
        future = [s for s in sessions if s["date"] >= today]
        if not future:
            return None, (f"课表里没有课程「{course}」的**未来**课次了（课表覆盖到 {last}）"
                          f" —— 需要补下周课表；这次**不要拿别的课当锚**，"
                          f"直接用 `date` 写截止日，并回询用户")
        return None, (f"课程「{course}」的未来课次（{future[0]['date']} 起）都已物化过"
                      f"（`taken` 里有），本次不必再物化")

    if pt.get("every"):
        return _calendar_anchor(pt, taken, now)

    return None, ("既没有 `follow_course` 也没有 `every` —— 内核不知道该按什么重复、"
                  "不猜日期；先问用户这个周期跟着哪门课（或每周几）")


def _shift(date_str: str, days: int) -> str:
    """在**已给定的日期**上做简单加减（不是周推算）。"""
    return (datetime.strptime(date_str, "%Y-%m-%d").date()
            + timedelta(days=days)).isoformat()






def _session_moment(s: dict, periods: dict, which: str = "start"):
    """课次的开始 / 结束时刻（datetime）。解析不出来返回 None。"""
    idx = 0 if which == "start" else 1
    hhmm = session_window_of(s, periods)[idx]
    hm = _hhmm(hhmm)
    if not hm:
        return None
    try:
        return datetime.strptime(s["date"], "%Y-%m-%d").replace(hour=hm[0], minute=hm[1])
    except (KeyError, ValueError, TypeError):
        return None


def _class_view(s: dict, periods: dict, sched: dict | None = None) -> dict:
    """课次的**精简视图**：只留"什么时候上什么课"真正需要的字段。

    ★ 为什么要有统一视图：同一节课在课表、注册表、`sessions` 里各存一份，
      三处都按原始条目返回的话，同一件事会被投给模型三遍（实测约 12k 字符/轮）。
      这里统一成一个形状，并省略空字段与默认值（`kind` 省略即 normal）。
    """
    out = {"date": s["date"], "course": s["course"]}
    if s.get("periods"):
        out["periods"] = s["periods"]
    t = session_time_of(s, periods)
    if t:
        out["time"] = t
    if s.get("location"):
        out["location"] = s["location"]
    if s.get("kind") and s["kind"] != "normal":
        out["kind"] = s["kind"]
    if _is_water(sched or {}, s):
        out["water"] = True       # 水课：课上能做作业性任务（排期时当可用时段）
    return out


def _sessions_between(sched: dict, start: str, end: str, now: datetime | None = None,
                      lean: bool = True) -> list[dict]:
    """给定日期区间内的课次（按**实际日期**，与"第几周"无关）。

    `now` 给了就**滤掉已经下课的**（含今天早上的）—— "接下来有什么"不该包含上完的课。
    要整份课表（不过滤下课的课）就不传 `now`。

    ★ 只取**可上课次**（`_visible_sessions`：停课不算课）—— 只有这一份口径，
      `brief` 的日程与 `review` 的撞课都走它。

    `lean=True`（默认）返回精简视图。
    """
    periods = sched.get("periods", {})
    got = sorted([s for s in _visible_sessions(sched)
                  if start <= s["date"] <= end],
                 key=lambda s: (s["date"], s.get("periods") or s.get("time") or ""))
    if now is not None:
        keep = []
        for s in got:
            end_dt = _session_moment(s, periods, "end")
            if end_dt is None or end_dt >= now:
                keep.append(s)
        got = keep
    if not lean:
        return got
    views = []
    for s in got:
        v = _class_view(s, periods, sched)
        if now is not None:
            st = _session_moment(s, periods, "start")
            v["starts_in"] = _fmt_until(st, now)
        views.append(v)
    return views


# 最新的业务记录超过这个天数，就判定为「久未维护」：提醒用户核对，而不是让模型硬排。
# ⚠️ 判据是**业务记录**（最新的截止日期 / 周日志），不是 meta.last_updated ——
#    后者会被任何一次 `import core.datastore` 刷新掉，永远检测不出陈旧（见 core/paths.py）。
DORMANT_DATA_DAYS = 21

# 单次能取回的历史条目上限。历史是无限的，**模型视图不该无限** ——
# 超过就告诉还有多少条，让模型缩小窗口或显式提高上限。
# （这是**机制**：它保护的是"一次调用不会把无限的东西塞进上下文"。
#   而"规则该有几条"是**策略**，写在 AGENTS.md 里，内核不设线。）
HISTORY_LIMIT = 50

# ★ **课次视图单次上限**：课表可以一次给一学期（几百节），但模型视图必须有限 ——
#   否则 `qyn_sessions --days 200` 一次就把整学期塞进上下文（实测：漏了这一环）。
#   超了就报 `more` + `capped_at`，让模型缩小窗口或从那里续查。
#   这是**机制参数**（保护上下文不随课表长度增长），不是策略。
SESSIONS_LIMIT = 60

# 冷存储的两个保留窗口（`_compact` 用）—— 决定热文件在"用得越久"时能保持多小。
#   课次：日期一过就搬走。它已经物化成注册条目了，热文件里不再需要它。
#   注册：滚出后再留几天，这样 `history` 查最近的事不必碰冷存储。
SESSION_KEEP_DAYS = 0
LIST_KEEP_DAYS = 7

# ★ **物化窗口**：`sync` 只把**近期**的课次登记进注册表（默认 14 天）。
#
#   为什么需要它（用户问"能不能一口气把一学期课表给你"）：课表可以一次给一学期
#   （几百节），但**注册表只该留近处的** —— 否则 `data.json` 与每轮上下文会随
#   课表长度一起膨胀（实测：没有窗口时，一学期课表 = 几百条注册条目全进 `active`）。
#   有了它：课表里可以躺着整学期，`sync` 每轮把进入窗口的那些登记进来 ——
#   **队列式滚动**，与 `_compact`（过期搬冷存储）配成一对。
#
#   这是**机制参数**（保护"上下文不随数据长度增长"），不是策略：
#   "该提前多久登记"不涉及语义判断，所以放在工具层 ✓（架构铁律见 docs/ARCHITECTURE.md）。
MATERIALIZE_DAYS = 14


def _newest_record_date(dat: dict):
    """最新的业务记录日期 = `registry[].date` 里最新的一个。

    ★ 判据是**业务记录**，不是 meta.last_updated —— 后者会被任何一次
      `import core.datastore` 刷新掉，永远检测不出陈旧（见 core/paths.py）。
    """
    dates = []
    for d in dat.get("registry", []):
        try:
            dates.append(datetime.strptime(d["date"], "%Y-%m-%d").date())
        except (ValueError, TypeError, KeyError):
            pass
    return max(dates) if dates else None


# ★ 课表至少要覆盖这么多天：周期任务（`follow_course`）锚的是**下一节课**，
#   课表不够远 ⇒ 锚不上 ⇒ Agent 会缺依据（2026-10-21 就因此把示例课程A作业登记成了示例课程）。
HORIZON_MIN_DAYS = 14


def _anomalies(rules: dict, sched: dict, dat: dict) -> list[dict]:
    """确定性异常检查 —— **只报 `active` 表达不了的机械异常**。

    报什么：课表为空（排期没依据）、周期任务锚不到日期、规则 scope 指向不存在的
    任务/课程（该规则从未生效）、数据久未维护。

    ★ 不报"即将到期"：那是 `context.active` 的活（`urgency` + `why`，按小时算）。
      实测（真实数据）anomalies 共 8 条、**8 条全部**是 expiring_deadline，逐条与
      `active` 重合 —— 一个常驻工具定义（442 字符/轮）加一次往返，换来零新信息。

    **为什么放在代码里**：这些判断的结果是精确的、可验证的（日期比较、状态枚举、
    名称集合运算），按项目开发风格（DEV_STYLE.md）就该由代码做 ——
    不该让模型去"注意"这些。模型负责的是**语义冲突**：
    课程时间部分重叠、同义任务名、含义模糊的调整要求。

    之前每日检查的提示词写的是"重点查两类异常"，等于把确定性计算推给了模型：
    模型可能漏、可能算错日期、也可能每次结果不一致。这里改成代码算好再交给它。
    """
    now = datetime.now()
    today = now.date()
    out: list[dict] = []
    sessions = sched.get("dated_sessions", [])
    periodic = sched.get("periodic_tasks", [])
    # ★ 只看**活跃注册**：日期已过的条目已滚出，对模型等效隐藏 ——
    #   所以"逾期"在这里不可能出现（date < today 的都不在 act 里）。
    act = _active(dat)

    # 1) 课表是否为排期提供依据
    if not sessions:
        out.append({"type": "schedule_empty", "severity": "medium",
                    "detail": "课表里还没有任何**带日期的**课次 —— 排期没有依据。"
                              "请录入下一周的课表截图（含日期），再跑 sync。"})

    # 1a) 课表**覆盖不足** ⇒ 周期任务锚不到更远的课次（2026-10-21 事故根因）
    #     纯日期算术：最远课次 − 今天。够不到 HORIZON_MIN_DAYS 就报。
    if sessions:
        vis = _visible_sessions(sched)
        last = max((str(s.get("date") or "") for s in vis), default="")
        horizon = None
        if last:
            try:
                horizon = (datetime.strptime(last, "%Y-%m-%d").date() - today).days
            except ValueError:
                horizon = None
        if horizon is not None and horizon < HORIZON_MIN_DAYS:
            out.append({
                "type": "schedule_horizon", "severity": "medium",
                "last_session": last, "days_left": horizon,
                "detail": (f"课表只覆盖到 {last}（还剩 {horizon} 天）—— 周期任务"
                           f"（follow_course）锚的是**下一节课**，不够远就锚不上，"
                           f"Agent 会缺依据。请补到至少 {HORIZON_MIN_DAYS} 天"
                           f"（建议一次录到学期末，或每周日补下周）。"),
            })

    # 1b2) 同一 (date, periods|time) 有**多条** ⇒ schema 写着"只应有一条"，但没人查 ✗
    #      （2026-11-10 我自己修数据时就造出过一条重复，视图里那节课显示两遍）
    _seen_slot: dict = {}
    for s in sessions:
        _k = (str(s.get("date") or ""),
              str(s.get("periods") or ""), str(s.get("time") or ""))
        if not _k[0]:
            continue
        _seen_slot.setdefault(_k, []).append(str(s.get("course") or ""))
    for (d, p_, tm), courses in sorted(_seen_slot.items()):
        if len(courses) > 1:
            out.append({"type": "duplicate_sessions", "severity": "medium",
                        "date": d, "periods": p_ or tm, "courses": courses,
                        "detail": (f"{d} {p_ or tm} 有 {len(courses)} 条课次（{('、'.join(courses))}）"
                                   f" —— 同一格只该有一条，视图里会重复显示/重复计入课表。")})

    # 1b3) 标了**停课**却没写依据（`note`）⇒ 没人知道凭什么是停课 ✗
    #      2026-11-10 事故：Agent 把「周二 5-6 示例课程D」标成 cancelled，note 空着；
    #      用户第二天发现课"丢了"。停课这种**删除语义**必须留下依据（图哪格/谁说的）。
    for s in sessions:
        if str(s.get("kind") or "") != "cancelled":
            continue
        if str(s.get("date") or "") < today.isoformat():
            continue                     # 过去的停课不再追问
        if not str(s.get("note") or "").strip():
            out.append({"type": "cancelled_without_note", "severity": "medium",
                        "date": s.get("date"), "periods": s.get("periods") or s.get("time"),
                        "course": s.get("course"),
                        "detail": (f"{s.get('date')} {s.get('periods') or s.get('time')}"
                                   f"「{s.get('course')}」标了停课，但没写依据 —— "
                                   f"停课等于把这节课删掉，必须写清是哪张图哪一格/谁说的。")})

    # 1b4) 同一周、同一门课既有 `cancelled` 又有 `moved` ⇒ **极可能把调课当停课** ✗
    #      （2026-11-10 事故就是这个形状：周二的蓝色格子被标停，周四的绿色调课留着）
    _wk: dict = {}
    for s in sessions:
        kind = str(s.get("kind") or "")
        if kind not in ("cancelled", "moved"):
            continue
        d = str(s.get("date") or "")
        try:
            wk = (datetime.strptime(d, "%Y-%m-%d").date() - today).days // 7
        except ValueError:
            continue
        _wk.setdefault((wk, str(s.get("course") or "")), set()).add(kind)
    _already = {(a["date"], a["course"]) for a in out
                if a.get("type") in ("cancelled_without_note",)}
    for (wk, course), kinds in sorted(_wk.items(), key=lambda kv: str(kv[0])):
        if kinds == {"cancelled", "moved"}:
            out.append({"type": "cancelled_vs_moved", "severity": "medium",
                        "course": course, "week_offset": wk,
                        "detail": (f"「{course}」同一周既有停课又有调课 —— 常见原因是把图上"
                                   f"绿色的**调课**当成\"那节课被挪走了\"，于是把另一格（蓝色=正常上课）"
                                   f"标成停课 ✗。绿色只表示这一次课被调整过，**不代表同名的另一节停**。"
                                   f"请核对原图那一周是空着（灰色/无格）还是有蓝格。")})

    # 1a2) `start` 晚于 `date` ⇒ 一定是写反了（用户 2026-10-27 加的字段）
    for d in act:
        st, du = d.get("start"), d.get("date")
        if st and du and str(st) > str(du):
            out.append({"type": "start_after_due", "severity": "medium",
                        "task": d.get("task", ""), "start": st, "date": du,
                        "detail": f"「{d.get('task')}」的开始时间 {st} 晚于截止 {du} —— "
                                  f"开始不可能晚于结束，多半是把两个日期写反了。"})

    # 1b) 课次**算不出时刻** ⇒ 撞课/课间检查根本没比（静默漏过）
    #     为什么必须报：`core/session.py` 查不到节次时返回 `("", "")`（内核不编时间），
    #     而 review / brief 两侧原先都 `continue` —— 实测 `periods: {}` + 课次
    #     `periods: "3-4"` 得到 `review ok=true, conflicts=[]`：撞课根本没比，
    #     异常清单也是空的（用户看不出这套排期其实没被检查过）。
    no_time = []
    for s in _visible_sessions(sched):
        if session_window_of(s, sched.get("periods", {})) == ("", ""):
            no_time.append({"date": s.get("date"), "course": s.get("course", ""),
                            "periods": s.get("periods", "")})
    if no_time:
        out.append({"type": "session_without_time", "severity": "medium",
                    "count": len(no_time), "sessions": no_time[:20],
                    "detail": f"{len(no_time)} 节课次算不出时刻（作息表 `periods` 里查不到"
                              "相关节次）—— 排期时**撞课与课间没有比**。"
                              "把作息表补进 schedule.json 的 `periods`（或给课次写 `time`）"})

    # 2) 周期任务锚不到日期 ⇒ 内核**不猜**，明确报出来让 Agent 回询用户
    for pt in periodic:
        name = pt["task"]
        anchor, why = _next_anchor(pt, {"dated_sessions": sessions}, set())
        if anchor is not None:
            continue
        out.append({"type": "periodic_unanchored", "severity": "medium", "task": name,
                    "detail": f"周期任务「{name}」锚不到具体日期：{why}"})

    # 3) ★ 这里**刻意不再报"即将到期"** —— 那是 `context.active` 的活。
    #
    #    实测（真实数据）：anomalies 一共 8 条，**8 条全部**是 expiring_deadline，
    #    而它们逐条与 `active` 重合（同样的 task/date，`why` 里就是剩余时间）。
    #    也就是说这一整段是 `active` 的**副本**：多一个常驻工具定义（442 字符/轮）、
    #    多一次调用往返，换来零新信息。删掉。
    #
    #    "还剩多少时间"只在一个地方给：`active[].urgency` + `why`（按**小时**算）。
    #    `anomalies` 从此只报 **`active` 表达不了的机械异常**：见下面 4/5/6。
    #
    #    ⚠️ 但**课次**仍然排除在 active 之外（它们在 sessions 里以统一形状给过一次），
    #       所以"今天还有哪些课"看 `sessions` / `context.next_class`。

    # 4) ★ 这里**刻意没有**"同名/重复"类的检查。
    #
    #    架构红线（用户明确要求）：**内核只做内核** —— 只报告**机制性**事实，
    #    不做"这两条是不是同一件事"的语义判断，也不审查数据是否"重复"。
    #    「面对循环任务怎么写」「同名算不算重复」都是 **Agent 的职责**（见 AGENTS.md 准则 6）。
    #
    #    内核该报的是**自己操作的后果**：所以"按名字操作只命中第一条"这种事，
    #    由 `mark` / `list.upsert` 在**执行时**如实报告（见 cmd_mark / _cmd_apply），
    #    而不是在这里对整份数据做一次审计。

    # 5) 规划规则的 scope 指向不存在的任务/课程 ⇒ 该规则**实际从未生效**
    periodic_names = {p["task"] for p in periodic}
    known_tasks = periodic_names | {d["task"] for d in _active(dat)}
    known_courses = {s.get("course") for s in sessions}
    for r in rules.get("planning_rules", []):
        scope = r.get("scope") or {}
        t = scope.get("task")
        if t and t not in known_tasks:
            out.append({"type": "rule_scope_mismatch", "severity": "medium",
                        "rule_id": r.get("id"), "field": "scope.task", "value": t,
                        "known": sorted(known_tasks),
                        "detail": f"规则 {r.get('id')} 的 scope.task=「{t}」在系统中不存在，"
                                  f"该规则实际不会生效（现有任务：{'、'.join(sorted(known_tasks))}）"})
        c = scope.get("course")
        if c and c not in known_courses:
            out.append({"type": "rule_scope_mismatch", "severity": "medium",
                        "rule_id": r.get("id"), "field": "scope.course", "value": c,
                        "detail": f"规则 {r.get('id')} 的 scope.course=「{c}」不在课表里，"
                                  f"该规则实际不会生效"})

    # 6) 数据是否久未维护（判据见 _newest_record_date：不能用 meta.last_updated）
    newest = _newest_record_date(dat)
    if newest is not None:
        gap = (today - newest).days
        if gap >= DORMANT_DATA_DAYS:
            out.append({"type": "dormant_data", "severity": "medium",
                        "newest_record": newest.isoformat(), "days_since": gap,
                        "detail": f"最新的业务记录是 {newest.isoformat()}（{gap} 天前）。"
                                  f"数据可能久未维护，据此排期不可信 —— "
                                  f"应先与用户核对待办的真实状态"})

    # 7) ★ 这里**刻意没有**"规则条数超限"这类检查 —— **阈值是策略，不是机制**。
    #    "规则要少、要短、过期就删"是**行事准则**，写在 `AGENTS.md`（system prompt）里，
    #    由 Agent 每轮据此判断；内核不替它设线、也不替它催办。
    #    （规则条数本来就随 `context` 一起给出，Agent 想看随时看得到。）

    order = {"high": 0, "medium": 1, "info": 2}
    out.sort(key=lambda a: (order.get(a["severity"], 9),
                            -(a.get("days_overdue") or 0), a.get("task", "")))
    return out


def _plan_view(dat: dict) -> dict | None:
    """最近一次规划快照的**事实**（只读、纯函数）。

    ★ 内核只给事实：上次排了什么（`order`）、排在什么时候（`slots`，每项附算好的
      `passed`）、那份快照之后数据是否又变过（`stale`）。
      **不替 Agent 判断"该不该重排""哪些时段空出来了"** —— 那是策略，写在
      AGENTS.md 的准则与技能里。内核里塞说教是越界：实测这么塞下去，这个函数被撑到
      172 行、还给模型发 `hint` 指令（用户指出后清理回 60 行）。
    """
    from core.datastore import plans as _plans

    p = _plans.latest()
    if p is None:
        return None
    created = p.get("created_at", "")
    plan = p.get("plan") if isinstance(p.get("plan"), dict) else {}

    def _ts(s: str):
        """兼容"有秒/没秒"两种写法（早期快照来自 SQLite 迁移，带秒）。"""
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(s, fmt)
            except (ValueError, TypeError):
                continue
        return None

    # ★ `stale` 优先按**三个输入文件的指纹**判（存快照时记下的）：只看 data.json 的
    #   `meta.last_updated` 抓不到"课表/规则改了"（2026-10-16 实测：恢复课表后仍报 stale=false）。
    #   老快照没有指纹 ⇒ 退回时间戳口径（向后兼容）。
    tc = _ts(created)                      # 后面 age 还要用它，别挪进分支
    rec = p.get("inputs") if isinstance(p.get("inputs"), dict) else {}
    if rec:
        cur = _plan_inputs()
        stale = any(cur.get(k) != v for k, v in rec.items())
    else:
        # 老快照没有指纹 ⇒ 退回时间戳口径。★ 但**不能只看 data.json 的 meta**：
        #   课表/规则改动不碰它。这里取"三个输入文件里最新的那个时刻"当输入时间。
        cands = [(dat.get("meta") or {}).get("last_updated", "")]
        for _p in (SCHEDULE_FILE, RULES_FILE):
            try:
                cands.append(datetime.fromtimestamp(os.path.getmtime(_p))
                             .strftime("%Y-%m-%d %H:%M:%S"))
            except OSError:
                pass
        tu = max([x for x in (_ts(c) for c in cands) if x], default=None)
        stale = bool(tc and tu and tu > tc)
    age = (datetime.now() - tc).days if tc else None

    # `order`：模型自己写的，字符串数组 / 对象数组都收
    order = []
    for o in (plan.get("order") or []):
        if isinstance(o, str) and o:
            order.append(o)
        elif isinstance(o, dict) and o.get("task"):
            order.append(o["task"])

    # `slots`：契约形状 [{date, time, task}]；每项附一个**算好的事实** `passed`
    now = datetime.now()
    slots = []
    for s in (plan.get("slots") or []):
        if not isinstance(s, dict) or not s.get("task"):
            continue
        date, time = str(s.get("date", "")), str(s.get("time", ""))
        slots.append({"date": date, "time": time,
                      "task": str(s["task"])[:40], "passed": _passed_at(date, time, now)})

    return {
        "created_at": created,
        "triggered_by": p.get("triggered_by", ""),
        "focus": plan.get("focus") or plan.get("summary", ""),
        "horizon": plan.get("horizon", ""),
        "order": order[:12],
        "slots": slots[:12],
        "stale": stale,
        "age_days": age,
    }


# ── 提醒（用户 2026-11-08）：**独立于任务与规划**的一层 ──────────────────
#  用户原话：「比如我想提醒明天中午取快递这种东西 带有"提醒我"这样的 不算任务但是算代办的
#  应该独立于基础的系统……但是由于审查系统被扔到下午了」。
#  根因：这种东西被登记成**没有 date 的任务** ⇒ 注册表里它是"永远活跃的活" ⇒
#  规划器按课间/整块/晚上不排等规则去搬它 ⇒ "中午"变成"随便什么时候" ✗。
#  ⇒ 提醒单独存 `reminders.json`：只认**一个时刻**，不进注册表、不进规划、
#    不参与任何撞课/课间/整块校验；到点由管道**确定性**投递（这句话不需要模型理解）。
REMINDER_LATE_HOURS = 6          # 过了点多久还值得补发；更晚只报"错过"，不打扰


def reminder_key_of(r: dict) -> str:
    """提醒的身份：日期 + 时刻 + 原文。三样都一样才算同一条。"""
    return "%s|%s|%s" % (str(r.get("date") or ""), str(r.get("time") or ""),
                         str(r.get("text") or ""))


def _reminder_moment(r: dict):
    """提醒的时刻（date + time）。只给日期 ⇒ 当天 DEFAULT_REMINDER_TIME（内核定死，不猜）。"""
    d = str(r.get("date") or "")
    if not d:
        return None
    try:
        day = datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        return None
    hhmm = str(r.get("time") or "").strip() or DEFAULT_REMINDER_TIME
    try:
        h, m = [int(x) for x in hhmm.split(":")]
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return day.replace(hour=h, minute=m)


def _reminder_view(now: datetime, days: int = 7, include_fired: bool = False) -> list:
    """提醒的只读视图。

    ★ 它**不参与排期**：`due` 只表示"这句话现在该说了"，不是"该把时间挪过来"。
    """
    from core.datastore import reminders as _rm
    out = []
    for r in _rm.items:
        moment = _reminder_moment(r)
        if moment is None:
            continue
        status = str(r.get("status") or "pending")
        if status == "done":
            continue
        fired = bool(r.get("fired_at"))
        if fired and not include_fired:
            continue
        if moment.date() > (now + timedelta(days=days)).date():
            continue
        item = {"date": moment.strftime("%Y-%m-%d"),
                "time": moment.strftime("%H:%M"),
                "at": moment.strftime("%Y-%m-%d %H:%M"),
                "text": str(r.get("text") or ""),
                "key": reminder_key_of(r),
                "status": status,
                "fired": fired,
                "when_in": _fmt_until(moment, now)}
        if r.get("note"):
            item["note"] = str(r["note"])
        if moment <= now:
            late = int((now - moment).total_seconds() // 60)
            item["late_minutes"] = late
            if not fired:
                item["due"] = late <= REMINDER_LATE_HOURS * 60
                if not item["due"]:
                    item["missed"] = True
        out.append(item)
    out.sort(key=lambda x: (x["date"], x["time"], x["text"]))
    return out


def cmd_reminders(args) -> dict:
    """提醒清单（只读）—— **独立于任务与规划**，不进 slots、不受审查搬动。

    用户 2026-11-08：「提醒我明天中午取快递」这类**不算任务**的东西，登记成任务会
    被规划器当"没有截止的活"搬到别的时间。提醒只认一个时刻，到点说一句话就完。

    * `--due`  只给"现在该说了"的（到点 && 未投递 && 迟到不超过 REMINDER_LATE_HOURS）
    * `--days N` 往后看几天（默认 7）
    * 返回值里 `key` 是身份，投递成功后由管道用 `reminder-fired --key` 标记
    """
    now = datetime.now()
    days = max(1, int(getattr(args, "days", 7) or 7))
    items = _reminder_view(now, days=days)
    if getattr(args, "due", False):
        items = [r for r in items if r.get("due")]
    return {"ok": True, "now": _now_view(now),
            "late_window_hours": REMINDER_LATE_HOURS,
            "count": len(items), "reminders": items}


def cmd_reminder_fired(args) -> dict:
    """标记提醒**已投递**（写操作）。

    ★ 只有管道用得到（发完就记一笔，免得下一轮又发一遍）；模型一般不该调它 ——
      用户说"这事办完了"用 `apply` 把那条 `status` 改成 `done` 才对。
    """
    from core.datastore import reminders as _rm
    keys = [str(k) for k in (getattr(args, "key", None) or []) if str(k).strip()]
    if not keys:
        raise SystemExit("需要至少一个 --key（取 `reminders` 返回的 key 字段）")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hit, missing = [], []
    for r in _rm.items:
        k = reminder_key_of(r)
        if k in keys:
            r["fired_at"] = now
            hit.append(k)
    for k in keys:
        if k not in hit:
            missing.append(k)
    if not hit:
        raise SystemExit(json.dumps({"ok": False, "error": "没有匹配的提醒", "missing": missing},
                                    ensure_ascii=False))
    _rm._save()
    return {"ok": True, "fired": hit, "missing": missing, "fired_at": now}


def _week_bounds(day: str) -> tuple[str, str]:
    """给定日期 ⇒ 它所在那一周（**周一到周日**）的起止日期。纯算术，不猜。"""
    d = datetime.strptime(str(day), "%Y-%m-%d").date()
    start = d - timedelta(days=d.weekday())
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def cmd_week(args) -> dict:
    """某一周课表的**完整回读**（只读）—— 专给「录完必须逐条念给用户核对」用。

    ★ 用户 2026-11-10：「户外改教室 这些都是随着课表变化的 要求 AGENT 严格按照课表来」。
      所以这里**不做任何过滤**：停课/调课都列出来（带 `kind` 与依据 `note`），
      也不按紧迫度重排 —— 就是"这一周每天几节、在哪上"，原样给人对。

    `--date`：这一周里的任意一天（默认今天）。
    """
    day = str(getattr(args, "date", "") or "") or datetime.now().strftime("%Y-%m-%d")
    try:
        start, end = _week_bounds(day)
    except ValueError:
        raise SystemExit("--date 需要 YYYY-MM-DD")
    sched = _load(SCHEDULE_FILE, validate_schedule)
    periods = sched.get("periods", {})
    buckets: dict = {}
    for s in sched.get("dated_sessions", []):
        d = str(s.get("date") or "")
        if not (start <= d <= end):
            continue
        buckets.setdefault(d, []).append(s)
    # ★ 已经过去的周：课次会被滚进冷存储（`data/archive/YYYY-MM.json`）⇒ 只读热文件
    #   会看到"这一周是空的"✗，Agent 就会以为漏录、再去补一遍。这里把归档的也读进来，
    #   并标 `archived: true`（"已经过去的周"和"这一周本来没课"必须能区分开）。
    try:
        from core.datastore import archive as _arch
        hot = {session_key_of(s) for s in (buckets and [x for v in buckets.values() for x in v] or [])}
        for s in _arch.sessions_between(start, (datetime.strptime(end, "%Y-%m-%d")
                                                + timedelta(days=1)).strftime("%Y-%m-%d")):
            d = str(s.get("date") or "")
            if not (start <= d <= end):
                continue
            if session_key_of(s) in hot:
                continue                      # 热区里已有（同一天同一节）就不重复
            buckets.setdefault(d, []).append(dict(s, _archived=True))
    except Exception as e:                    # noqa: BLE001 —— 归档读不到不影响热区回读
        print(json.dumps({"ok": False, "warning": f"读归档失败：{type(e).__name__}: {e}"},
                         ensure_ascii=False), file=sys.stderr)
    days = []
    for i in range(7):
        d = (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=i)).strftime("%Y-%m-%d")
        entries = []
        for s in sorted(buckets.get(d, []),
                        key=lambda x: str(x.get("periods") or x.get("time") or "")):
            ent = {"periods": s.get("periods") or s.get("time") or "",
                   "course": str(s.get("course") or ""),
                   "location": str(s.get("location") or "")}
            if s.get("kind"):
                ent["kind"] = str(s["kind"])
            if s.get("note"):
                ent["note"] = str(s["note"])
            if s.get("_archived"):
                ent["archived"] = True       # 已滚进冷存储（过去的周），不代表本周没课
            entries.append(ent)
        days.append({"date": d,
                     "weekday": "一二三四五六日"[datetime.strptime(d, "%Y-%m-%d").weekday()],
                     "entries": entries})
    total = sum(len(x["entries"]) for x in days)
    return {"ok": True, "week_start": start, "week_end": end,
            "date": day, "count": total, "days": days,
            "note": ("录完课表要按这份**逐条念给用户**（日期·周几·节次·课名·教室，"
                     "停课/调课带上依据）—— 教室与户外每周都会变，念的必须是**当周**的图。")}


def cmd_context(_args) -> dict:
    """模型用的完整上下文（只读）。

    三层分清楚：
      · planning_rules                          规则层
      · periods / dated_sessions / periodic_tasks  周期层（课表**带具体日期**）
      · active / occurrences                    注册表（唯一权威）
    ★ 这里出现的每个日期都来自录入或显式给定，**没有任何周推算**。
    """
    rules = _load(RULES_FILE, validate_rules)
    sched = _load(SCHEDULE_FILE, validate_schedule)
    dat = _load(DATA_FILE, validate_data)
    from core.datastore import plans as _plans          # 只读：规划快照（惰性，不建文件）
    now = datetime.now()
    periods = sched.get("periods", {})
    upcoming = _upcoming(sched, now)
    # ★ 课次**只由 `sessions` 提供**（统一形状、不再重复第三遍）：
    #   这里只给"最近一节 + 总量"，让模型一眼知道有没有课，想看全再调 sessions。
    nxt = None
    if upcoming:
        first = sorted(upcoming, key=lambda s: (s["date"],
                                                s.get("periods") or s.get("time") or ""))[0]
        nxt = _class_view(first, periods, sched)
        nxt["starts_in"] = _fmt_until(_session_moment(first, periods, "start"), now)
    # ★ 注册表视图**不含课次**：课次已在上面/`sessions` 里，重复投递会被模型当成两件事
    active = [d for d in _active(dat, now.strftime("%Y-%m-%d"))
              if not d.get("session_key")]
    return {
        "now": _now_view(now),
        "planning_rules": rules.get("planning_rules", []),
        "semester": sched.get("semester", ""),
        "class_count": len(upcoming),
        "next_class": nxt,
        "periodic_tasks": [p["task"] for p in sched.get("periodic_tasks", [])],
        # ★ 提醒（用户 2026-11-08）：独立于任务与规划 —— 只到点说一句，**不要**排进 slots，
        #   也不要因为它"没排上"去调整别的东西。
        "reminders": _reminder_view(now, days=7),
        # ★ 只给**活跃注册**（未终结 且 日期未过）—— 已滚出的对模型等效于隐藏。
        #   这就是「注册系统按日期滚转向前」：日期一到自动滚出，无需写盘。
        "active": _list_view(active, now=now),
        "occurrences": _list_view([d for d in _active(dat)
                                               if d.get("occurrence_of")
                                   if not d.get("session_key")], now=now),
        "last_plan": _plan_view(dat),
        # ★ 只留最近 2 次（原先 5 次）：它是**对账**用的，不是每轮都要看的清单 ——
        #   每轮常驻的输出越短，模型的注意力越集中在**规则与待办**上。
        "plan_history": _plans.history(2),
        # ★ 这里**不再附字段说明**：那是工具描述该讲的事（讲一次就够），
        #   每轮重复 200 多字纯属噪声（`now`/`why`/`active` 的含义见 qyn_state 的描述）。
    }


def cmd_sessions(args) -> dict:
    """未来 N 天的课次与待办（只读）。

    ★ 按**实际日期**取，不按周 —— 用户明确要求不要用周计算时间。
    ★ **时刻相关**：已经下课的（含今天早上的）不再列出；每节课给 `starts_in`。
      晚上 11 点问和早上 8 点问，返回的课次**不同**。

    ★★ **单次上限 `SESSIONS_LIMIT`**：课表可以一次给一学期（几百节），
      但**模型视图必须有限** —— 否则 `--days 200` 一次就把整学期塞进上下文。
      截断时给 `more`（还剩多少）与 `capped_at`（最后一节的日期）——
      要继续看就从那里往后查，不用重来。
    """
    sched = _load(SCHEDULE_FILE, validate_schedule)
    dat = _load(DATA_FILE, validate_data)
    now = datetime.now()
    days = args.days if args.days is not None else 14
    start = now.strftime("%Y-%m-%d")
    end = (now.date() + timedelta(days=days)).isoformat()
    # ★ due 里**不含课次**：课次已经在 sessions 里了，再说一遍是纯浪费（实测重复约 5k 字符）
    # ★★ **活跃判定只允许有一份实现** —— 就是 `_active()`。
    #   这里曾经内联了一份（只比日期、不比时刻），于是"过点即翻篇"在 sessions 里失效：
    #   10/14 14:20 就该翻篇的条目 19:00 还在 due 里闪"还剩 0 分钟"（实测）。
    #   注意保留 `not d.get("date")`：没有 DDL 的条目永远活跃，直到用户说做完。
    due = _list_view([d for d in _active(dat, now=now)
                      if not d.get("session_key")
                      if (not d.get("date")) or (start <= d.get("date", "") <= end)],
                     now=now)
    classes = _sessions_between(sched, start, end, now=now)
    limit = getattr(args, "limit", None)
    limit = SESSIONS_LIMIT if limit is None else max(1, limit)
    more = max(0, len(classes) - limit)
    shown = classes[:limit]
    return {
        "now": _now_view(now),
        "until": end,
        "days": days,
        "count": len(classes),
        "limit": limit,
        "more": more,
        "capped_at": shown[-1]["date"] if more and shown else "",
        "sessions": shown,
        "next": shown[0] if shown else None,
        "due": due,
        "note": ("sessions = **还没下课**的课次（按时间先后，带 `starts_in`）；"
                 "due = **非课次**的待办（已按紧迫度排序）。两者分列，同一件事不会以两种形状出现。"
                 f"**单次最多 {limit} 节** —— `more` 是还有多少节没给；"
                 "要继续看就把 `days` 缩小到 `capped_at` 附近，或显式提高 limit。"),
    }


def cmd_sync(_args) -> dict:
    """**物化**：把「课表里带日期的每一节课」+「周期任务」登记进 LIST。

    ★ 全程**不做周推算**：
      · 上课条目直接使用 `dated_sessions` 里那一节的日期
      · 周期任务有两种模式（见 `_next_anchor`）：
        `follow_course` 锚到那门课的**实际课次日期**；`every`+`weekday` 走**日历周期**
        （每周几、几点）。两者产出的都是**具体日期**，都不涉及"第N周"。
      日期全部"外部给定"或由明确的日历规则算出，内核只做搬运、去重、加减。

    幂等：上课条目按 `session_key`（date|periods）判重；
    周期任务按 (task, anchored_to) 判重 —— anchored_to 是**身份**，改期不会丢。

    这是**写操作**，所以单独成命令（读命令零副作用是硬性质）。
    """
    from core.datastore import data, schedule as sched_store

    created, skipped, unanchored = [], [], []
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # ① 上课条目
    #   ★ 只物化**窗口内**的（`MATERIALIZE_DAYS`）：课表可以一次给一学期，
    #     注册表只留近处的，其余留在课表里等时间推进 —— 队列式滚动。
    horizon = _shift(today, MATERIALIZE_DAYS)
    deferred = 0
    seen = {d.get("session_key") for d in data.registry if d.get("session_key")}
    for s in sched_store.dated_sessions:
        key = sched_store.session_key(s)
        if key in seen:
            skipped.append({"session": key, "reason": "已登记过"})
            continue
        if _is_cancelled(s):                    # 判据只有一份（见 `_is_cancelled`）
            skipped.append({"session": key, "reason": "停课（图例灰）—— 不登记"})
            continue
        if s.get("date", "") > horizon:
            deferred += 1                       # 还没进窗口：留在课表里，等着
            continue
        t = sched_store.session_time(s)
        # ★ **物化只登记，不改状态**（架构铁律：数据工具层不做语义判断）。
        #   原先这里写的是 `"completed" if s["date"] < today else "pending"` ——
        #   用代码替时间宣布"这节上完了 = 完成"，那是**语义判断**；
        #   而且和视图层的"过点即翻篇"重复（`_active` 已按时刻过滤）。
        #   现在：一律 `pending` —— 过期与否由**视图**算，不由存储宣布。
        data.add_session_entry(s, time_range=t, status="pending")
        seen.add(key)
        created.append({"kind": "session", "task": s["course"], "date": s["date"],
                        "periods": s.get("periods", ""), "time": t, "status": "pending",
                        "location": s.get("location", "")})

    # ② 周期任务（作业/考试）：锚到那门课的实际课次日期
    #
    # ★ **一个周期任务同时只保留一件待办** —— 否则同名条目一多，`apply`（按名字整条
    #   替换）/ `mark` 就有歧义（改的是哪一次？）。那一件处理掉后，下次 sync 才锚下一节。
    # ★★ 查重**不能只看 `occurrence_of`**（实测 bug）：用户先手工登记一条、又把它注册成
    #   周期任务 —— 手工那条没有 `occurrence_of`，旧逻辑认不出 ⇒ 同一条在清单里出现两遍。
    #   现在按**同名 + 同日期**认"这一次发生"，只为它兜机制底线：**不再物化第二条**。
    #   至于"那条算不算这个周期任务的这一次"—— 内核**不判断**，只报 `already_present`。
    # ★ `taken` 同时收 **锚点** 与 **排期日期**（实测 bug）：只认 `anchored_to` 时，
    #   "取消本周这一次"后内核会把它**当场复活**（Agent 明明说了下周才生成）。
    #   代价：某次被改期到将来某个已有发生的同一天时会被跳过 —— 比"取消的东西自己长回来"好。
    taken, pending_occ = {}, set()
    for d in data.registry:
        if d.get("occurrence_of"):
            if d["status"] == "pending":
                pending_occ.add(d["occurrence_of"])
            slot = taken.setdefault(d["occurrence_of"], set())
            if d.get("anchored_to"):
                slot.add(d["anchored_to"])
            if d.get("date"):
                slot.add(d["date"])

    already = []
    for pt in sched_store.periodic_tasks:
        name = pt["task"]
        if name in pending_occ:
            # ★ 补空（2026-10-29 实测）：周期任务是**后来**才写上 `due_time`/`gist` 的，
            #   早先物化的那一条会一直挂着空值 ⇒ `_due_moment` 退回 23:59 ⇒ 墙上的
            #   "剩余时间"偏差（实测：示例课程E作业 显示"还剩 34 小时"，实际到明早 10:10
            #   只剩 ~21 小时）。这里**只补空、绝不覆盖** —— Agent 可能给这一条写过自己
            #   的 due_time/gist，那是它的判断，内核不抢。
            filled = []
            for d in data.registry:
                if (d.get("task") == name and d.get("occurrence_of") == name
                        and d.get("status") == "pending"):
                    ch = {}
                    if not d.get("due_time") and pt.get("due_time"):
                        d["due_time"] = pt["due_time"]
                        ch["due_time"] = d["due_time"]
                    if not d.get("gist") and pt.get("gist"):
                        d["gist"] = pt["gist"]
                        ch["gist"] = d["gist"]
                    if ch:
                        filled.append({"task": name, "date": d.get("date"), **ch})
            if filled:
                data._save()          # 只补了字段，不需要整份重校验
            skipped.append({"task": name, "reason": "LIST 里已有待办的那一件",
                            "backfilled": filled})
            continue
        anchor, why = _next_anchor(pt, {"dated_sessions": sched_store.dated_sessions},
                                   taken.get(name, set()), now=now)
        if anchor is None:
            unanchored.append({"task": name, "reason": why})
            continue
        offset = int(pt.get("offset_days", 0) or 0)
        date = _shift(anchor, offset)

        mine = next((d for d in data.registry
                     if d["task"] == name and d.get("date") == date), None)
        if mine is not None:
            # ★★ **内核只报，不替 Agent 决定**（用户明确要求）：
            #   "这一天那个同名条目算不算这个周期任务的这一次"是**语义判断**，归 Agent
            #   （先手工登记第一次？只注册周期定义？那条手工的认不认成 occurrence？）。
            #   内核不补 `occurrence_of`、不改写 Agent 写的条目，也不记进 `taken` ——
            #   于是每次 sync 都会**重复报告**这个待决问题：那是有意的常驻信号，
            #   不是内核替人做的决定。机制底线只有一条：**不为同一次发生建第二条**。
            already.append({"kind": "periodic", "task": name, "date": date,
                            "anchored_to": anchor,
                            "existing_status": mine.get("status"),
                            "note": ("这一天已有同名条目，**没有**再物化一条。"
                                     "它算不算这个周期任务的这一次，**由你判断** —— "
                                     "要认就在 `apply` 的 `list.upsert` 里写 "
                                     "`occurrence_of` + `anchored_to`；"
                                     "若它其实是另一件事，就改名或另立周期任务。")})
            continue

        data.add_registry_entry(name, date, pt.get("priority") or "",
                          due_time=pt.get("due_time", ""), detail=pt.get("detail"),
                          gist=pt.get("gist", ""),
                          estimate_minutes=pt.get("estimate_minutes"),
                          occurrence_of=name, anchored_to=anchor)
        pending_occ.add(name)
        created.append({"kind": "periodic", "task": name, "date": date,
                        "anchored_to": anchor, "offset_days": offset})

    compacted = _compact(today)
    out = {
        "ok": True,
        "action": "sync",
        "today": datetime.now().strftime("%Y-%m-%d"),
        "created": created,
        "already_present": already,
        "skipped": skipped,
        "unanchored": unanchored,
        "deferred": deferred,
        "compacted": compacted,
        "note": ("created = 本次新物化的。**already_present = 这一天已有同名条目，"
                 "内核没有再造一条，也没有替你认领** —— 该不该把它认成这个周期任务的"
                 "这一次，是你要判断的（见每条的 note）。"
                 "两者都是注册表里独立可编辑的条目：改期用 `apply` 的 list.upsert、"
                 "取消/完成用 `mark`。unanchored 表示锚不到日期，**内核不猜**，请回询用户。"
                 f"deferred = 课表里**还没进物化窗口**（{MATERIALIZE_DAYS} 天）的课次数 ——"
                 "课表可以一次给一学期，注册表只留近处的，其余随日期推进自动登记。"
                 "compacted = 本次搬进冷存储的过期条目数（热文件因此不随使用时间变大）。"),
    }
    # ★ 物化了新条目（或搬走了过期的）⇒ 注册表变了 ⇒ 上一份计划作废
    if created or (isinstance(compacted, dict) and any(compacted.values())):
        out.update(_plan_invalidated())
    return out




def cmd_anomalies(args) -> dict:
    """确定性异常清单：把「该发现的」先算出来交给模型，而不是让模型去注意。

    模型拿到这份清单后只需要做两件它擅长的事：判断该怎么处置、
    以及发现代码发现不了的**语义**冲突（部分时间重叠、同义任务名）。
    """
    rules = _load(RULES_FILE, validate_rules)
    sched = _load(SCHEDULE_FILE, validate_schedule)
    dat = _load(DATA_FILE, validate_data)
    items = _anomalies(rules, sched, dat)
    counts: dict[str, int] = {}
    for a in items:
        counts[a["severity"]] = counts.get(a["severity"], 0) + 1
    return {
        "today": datetime.now().strftime("%Y-%m-%d"),
        "clean": not items,
        "counts": counts,
        "anomalies": items,
    }


def validate_report() -> dict:
    """校验四个热文件 + 两个"用到才有"的文件。**校验逻辑只有这一份**。

    ★ 人读版在 `scripts/check_data.py`，它调用这里再排版 ——
      以前两边各写一遍"哪些文件、哪个 validator"，改一处就会漂移（第 5 步复审发现）。
    ★ `plans.json` / `archive.json` 在全新环境里**合法地不存在**（快照没存过、
      还没滚转过）—— 所以它们是"存在才校验"，缺席报 skip 而不是 FAIL。
    """
    report: dict = {}
    _sess_rep: dict = {}
    _pt_rep: dict = {}
    _rule_rep: dict = {}
    ok = True
    for label, path, validator in (("rules.json", RULES_FILE, validate_rules),
                                   ("schedule.json", SCHEDULE_FILE, validate_schedule),
                                   ("data.json", DATA_FILE, validate_data)):
        try:
            _load(path, validator)
            report[label] = "ok"
        except Exception as exc:  # noqa: BLE001 —— 校验失败就是结果本身
            report[label] = f"FAIL: {exc}"
            ok = False
    if os.path.isfile(PLANS_FILE):
        try:
            _load(PLANS_FILE, validate_plans)
            report["plans.json"] = "ok"
        except Exception as exc:  # noqa: BLE001
            report["plans.json"] = f"FAIL: {exc}"
            ok = False
    else:
        report["plans.json"] = "skip（还没存过规划快照）"
    # 冷存储：按月分文件（archive/YYYY-MM.json）—— 逐个校验，报"几个月、有没有坏的"
    from core.datastore import archive_months
    months = archive_months()
    if months:
        bad = []
        for m in months:
            try:
                _load(os.path.join(ARCHIVE_DIR, f"{m}.json"),
                      lambda d, mm=m: validate_archive(d, f"archive/{mm}.json"))
            except Exception as exc:  # noqa: BLE001
                bad.append(f"{m}: {exc}")
        if bad:
            report["archive/"] = f"FAIL: {'; '.join(bad)}"
            ok = False
        else:
            report["archive/"] = f"ok（{len(months)} 个月：{months[0]} … {months[-1]}）"
    else:
        report["archive/"] = "skip（冷存储尚未生成，跑一次 sync 才会有）"
    if os.path.isfile(ARCHIVE_FILE):
        # 旧单文件还在 ⇒ 说明迁移没跑完。**报出来**，不静默（且算不合格）。
        report["archive.json"] = "FAIL: 旧的单文件冷存储还在（跑 .runtime/migrate_archive_split.py）"
        ok = False
    report["all_ok"] = ok
    return report


def cmd_validate(_args) -> dict:
    return validate_report()


# ────────────────────────── 写入层（经 datastore，严格校验） ──────────────────────────







def cmd_mark(args) -> dict:
    """按**完全一致**的任务名标记状态。名称不完全一致时拒绝，交给模型回询用户。

    ★ 周期任务的状态记在 **LIST 里物化出来的那一条**上（不再有 task_log）。
      LIST 里没有 → 说明还没 sync（或那一件已处理完），如实告诉模型去 sync。

    ★★ **同名多条必须消歧，否则拒绝**（用户 2026-10-15 的数据事故）。
      实测原实现有两处会在同名时**说一套做一套**：
        · `DataStore.set_registry_status` 会把**所有同名条目一起改**，
          而返回值却写"只改了第一条、其余没有改动" —— 报告与事实相反；
        · 调用方拿着 `ok: true` 继续走，于是"改了一条"其实是"全改了"。
      现在：给了 `--date` ⇒ 只作用于 `date` 全等的那一条；没给而同名多条 ⇒
      **拒绝**并列出候选（加 `--date` 重试）。落盘只改命中的那一条。
      （内核该做的是"报机制后果"，不是替 Agent 判断"这些算不算重复"。）
    """
    from core.datastore import data, schedule as sched_store

    mapped = {"done": "completed"}.get(args.status, args.status)
    want_date = getattr(args, "date", None)
    named = [d for d in data.registry if d["task"] == args.task]
    matches = [d for d in named if d.get("date") == want_date] if want_date else list(named)
    if want_date and named and not matches:
        raise SystemExit(json.dumps({
            "ok": False,
            "error": f"「{args.task}」有 {len(named)} 条，但没有一条的 `date` 是 {want_date!r}",
            "candidates": [_fingerprint(d) for d in named],
            "hint": "`--date` 要写成 `YYYY-MM-DD`（全等匹配，不从描述里猜）。",
        }, ensure_ascii=False))
    if len(matches) > 1:
        raise SystemExit(json.dumps({
            "ok": False,
            "error": (f"「{args.task}」同名 {len(matches)} 条 —— mark 不替你猜要标哪一条"
                      "（不猜、也不会只动第一条）"),
            "candidates": [_fingerprint(d) for d in matches],
            "hint": ("加 `--date YYYY-MM-DD` 指认是哪一次发生；"
                     "要改内容（日期/要点）用 `apply` 的 `list.upsert`，"
                     f"它能带消歧字段（{' / '.join(DISAMBIGUATORS)}）"),
        }, ensure_ascii=False))
    if matches:
        hit = matches[0]
        # ★ 只有**状态真的变了**才算改了注册表 —— 否则会对着一次 no-op 催"重算"（假警报）
        really_changed = hit.get("status") != mapped
        if really_changed:
            # ★ 只改**命中的那一条**：`set_registry_status(名字)` 会把同名的全改掉
            #   （报告却写着只改了一条）—— 这里原地改这一条，再走整表替换落盘。
            hit["status"] = mapped
            data.replace_registry(list(data.registry))
        out = {"ok": True, "kind": "deadline", "task": args.task, "status": mapped,
               "date": hit.get("date"), "session_key": hit.get("session_key"),
               "picked": _fingerprint(hit), "matched": 1,
               "changed": 1 if really_changed else 0}
        if want_date:
            out["by"] = {"date": want_date}
        # ★ 状态一变 ⇒ 注册表变了 ⇒ 上一份计划作废（"变更要及时"那个问题）
        if really_changed:
            out.update(_plan_invalidated())
        return out

    all_tasks = sorted({d["task"] for d in data.registry}
                       | {p["task"] for p in sched_store.periodic_tasks})
    # 已滚出的（**按时刻**判定，与视图同口径）不列进 known —— 但**告诉模型有这么一批存在**，
    # 否则用户提一个旧条目时模型会以为"从没有过"，而不是"已滚出"。
    # ★ 这里曾经用 `d.get("date","") < today` —— 那是**第二份活跃判定**（只比日期）：
    #   今天 10:10 的事到了晚上视图里已翻篇，这里的 rolled 却还是 0（实测），
    #   提示里的"另有 N 条已滚出"就少报一条。现在直接用 `_active` 的**差集** ——
    #   活跃判定只允许有一份实现（`_active`）。
    _act_ids = {id(d) for d in _active({"registry": data.registry})}
    rolled = sum(1 for d in data.registry
                 if d.get("status") == "pending" and id(d) not in _act_ids)
    extra = {}
    if rolled:
        extra = {"rolled_out": rolled,
                 "rolled_out_hint": f"另有 {rolled} 条注册已按日期滚出，`history --days N` 可回溯"}
    if any(p["task"] == args.task for p in sched_store.periodic_tasks):
        raise SystemExit(json.dumps({
            "ok": False,
            "error": f"「{args.task}」是周期任务，但注册表里没有它的条目",
            "known_tasks": all_tasks, **extra,
        }, ensure_ascii=False))

    raise SystemExit(json.dumps({
        "ok": False,
        "error": f"未找到任务名完全一致的任务: {args.task!r}",
        "known_tasks": all_tasks, **extra,
    }, ensure_ascii=False))








def _read_payload(args):
    """从 --file 或 stdin 读 JSON。**不走 shell 管道传中文**（控制台代码页会乱码）。"""
    raw = ""
    if getattr(args, "file", ""):
        with open(args.file, "r", encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("需要 --file 或 stdin 提供 JSON")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 解析失败: {exc}")


def cmd_history(args) -> dict:
    """**已滚出的注册**（日期已过）—— 默认不出现在任何模型视图里。

    为什么必须有它：「按日期滚转向前」意味着过期注册对模型**等效隐藏**，
    但**不能等于丢失**。这个命令按日期区间把它们取回来（只给列表视图字段，
    **不含 detail**），用于"上周那件到底交没交""它怎么不见了"这类追问。

    ★ **单次上限 `HISTORY_LIMIT` 条**：历史的长短是无限的（四年几千条），
      而模型视图必须有限。超出只报一个 `more` 计数，要更多得显式提高上限。
    """
    from core.datastore import archive as arch

    dat = _load(DATA_FILE, validate_data)
    today = datetime.now().strftime("%Y-%m-%d")
    days = args.days if args.days is not None else 30
    limit = getattr(args, "limit", None)
    limit = HISTORY_LIMIT if limit is None else max(1, limit)
    since = (datetime.now().date() - timedelta(days=days)).isoformat()
    # 热文件里"滚出但还没搬走"的 + 冷存储里的 = 完整历史
    rolled = [d for d in dat.get("registry", []) if since <= d.get("date", "") < today]
    rolled += arch.registry_between(since, today)   # ★ 只读覆盖到的月份文件
    rolled.sort(key=lambda d: d["date"], reverse=True)
    shown = rolled[:limit]
    return {
        "today": today,
        "since": since,
        "days": days,
        "count": len(rolled),
        "limit": limit,
        "more": max(0, len(rolled) - len(shown)),
        "unfinished": [d["task"] for d in shown if d.get("status") == "pending"],
        "entries": _list_view(shown, urgency=False),
        "note": ("这些注册已按日期滚出活跃集，对模型默认不可见；此处仅为按需回溯，"
                 f"且**单次最多 {limit} 条**（`more` 是还有多少条没给）。"
                 "历史无限、模型视图有限 —— 需要更早的再缩小条件重查。"),
    }


def _apply_collection(current: list, spec, key_fn, label: str,
                      name_of=None) -> tuple[list, dict]:
    """把「整段数组」或「{upsert, remove}」应用到 current，并**返回一份报告**。

    ★ 为什么两种都支持：整段替换适合"录一整周课表"（本来就是全量）；
      增量适合"改一节课的地点" —— 让模型自己挑**它不容易出错**的那种。
      整段替换要求模型把几十条**原样搬回**，那是它的弱项（漏一条、串一行就毁数据）；
      增量只让它写出真正要改的那几条。

    ★★ **报告是合并时最不能丢的东西**：原来的 set-schedule / remove-periodic 在
      "重复 add""删不存在的""名字不一致"时会**报错**。收敛成一个写入口之后，
      若变成静默 no-op，模型就再也发现不了"名字/身份写错了"。所以这里**不拦，但如实报**：
        · `missing`  —— 你说要删的没匹配上（多半名字或身份写错）
        · `replaced` —— 同身份被覆盖，而且**名字变了**（多半误伤了别的东西）

    `remove` 每项可以是**身份字符串**（如 `"2026-10-14|5-8"`）或带身份字段的对象。
    """
    report = {"added": 0, "updated": 0, "removed": 0, "missing": [], "replaced": []}
    if isinstance(spec, list):
        return spec, report          # 整段替换：没有"命中"概念，报告为空
    if not isinstance(spec, dict):
        raise SystemExit(f"{label} 需要数组（整段替换）或 {{upsert, remove}} 对象")
    unknown = sorted(set(spec) - {"upsert", "remove"})
    if unknown:
        raise SystemExit(f"{label} 不认识的键: {unknown}（可用：upsert / remove）")
    out = list(current)
    for r in spec.get("remove", []):
        k = r if isinstance(r, str) else key_fn(r)
        before = len(out)
        out = [x for x in out if key_fn(x) != k]
        if len(out) != before:
            report["removed"] += 1
        else:
            report["missing"].append(k)
    for u in spec.get("upsert", []):
        k = key_fn(u)
        for i, x in enumerate(out):
            if key_fn(x) == k:
                if name_of and name_of(x) != name_of(u):
                    report["replaced"].append(
                        {"id": k, "was": name_of(x), "now": name_of(u)})
                out[i] = u          # 整条替换（不合并：合并会留下过期字段）
                report["updated"] += 1
                break
        else:
            out.append(u)
            report["added"] += 1
    return out, report


def cmd_apply(args) -> dict:
    """写入口的**错误一律结构化**再抛给模型。

    ★ 为什么：插件把内核失败信息解析成 JSON 再交给模型（见 DSH 插件
      的 runKernel）。裸字符串会落在 `output` 里，模型要自己去翻；
      结构化 `{ok:false, error:"…不认识的键…"}` 是**可直接照做**的反馈 ——
      它写错键名时能立刻看懂并改，而不是看到一句"命令失败"。
    """
    try:
        out = _cmd_apply(args)
        # ★ `--sync` 落盘后**立即物化**（课表 → 注册表）。原先只注册了这个 flag、
        #   全篇没读它，而插件描述承诺"落盘后立即物化" —— Agent 以为已物化，brief 却出空规划。
        if getattr(args, "sync", False) and isinstance(out, dict) and out.get("ok", True):
            out = {**out, "sync": cmd_sync(args)}
        return out
    except SystemExit as exc:
        msg = exc.code
        if isinstance(msg, str) and not msg.lstrip().startswith("{"):
            raise SystemExit(json.dumps({"ok": False, "error": msg},
                                        ensure_ascii=False)) from None
        raise


# ★ **声明式写入表**：`apply` 的每一节长什么样，写在这里而不是写三遍代码。
#   节 →（store 取用、文件名、校验函数、直通键、集合表）
#   集合表：模型给的键 →（落盘键、身份函数、名字函数）—— `name_of` 只在需要
#   "同身份被覆盖且名字变了"这个守卫时才给。
APPLY_SECTIONS = {
    "schedule": (
        lambda: _import_store("schedule"), "schedule.json", validate_schedule,
        # ★ 两种写法都在：`dated_sessions` = **整批替换**课表（模型给全量），
        #   `sessions` = **增量**改某几节（只给要改的，见下）。两个都别删 ——
        #   我重写时漏了 `dated_sessions`，4 项用例立刻红（守门用例的价值）。
        # ★ `courses` **只**在下面的集合表里 —— 别再往这里加一份：
        #   直通键会先把 cand["courses"] 设成 {"upsert":…} 这个 dict，集合再当列表遍历 ⇒
        #   'str' object has no attribute 'get'（2026-10-16 自己踩的）。
        #   裸列表照样能整段替换（_apply_collection 对 list 直接 return）。
        ("semester", "periods", "dated_sessions"),
        {"sessions": ("dated_sessions", session_key_of, lambda s: s.get("course", "")),
         # ★ 课程级属性（水课）：按**课程名**身份 upsert —— 标一次，该课所有课次都算
         "courses": ("courses", lambda c: c.get("course", ""),
                     lambda c: c.get("course", "")),
         "periodic_tasks": ("periodic_tasks", lambda p: p.get("task", ""),
                            lambda p: p.get("task", ""))},
    ),
    "reminders": (
        # 提醒（用户 2026-11-08）：**不是任务** —— 不进注册表、不进规划、不受审查搬动。
        #   身份 = 日期 + 时刻 + 原文；到点由管道确定性投递。
        lambda: _import_store("reminders"), "reminders.json", validate_reminders, (),
        {"reminders": ("reminders", reminder_key_of, None)},
    ),
    "rules": (
        # 规则字段（`kind` / `condition` / `body` 之外，**能被机械校验**的只有结构化窗口）：
        #   · `scope`               —— 只影响某个 `task` / `course`（指向不存在的名字 ⇒ anomalies 报）
        #   · `no_slots`            —— **绝对**时段，如 `["19:00-23:59"]`：块与它重叠就违反
        #   · `before_first_class`  —— **相对当天课表**：`00:00 → 当天第一节课开始`，
        #                              且只在第一节课开始 ≤ `max_start`（默认 12:00）时当天生效
        #   · `allow`（写在**快照正文**里，不是规则里）—— 用户明确同意后豁免某条 preference
        # 判据实现在 `_window_hits`（review 与 brief 共用同一份）；`kind` 决定阻塞还是警告。
        lambda: _import_store("rules"), "rules.json", validate_rules, (),
        {"planning_rules": ("planning_rules", lambda r: r.get("id", ""), None)},
    ),
}


def _import_store(name: str):
    """惰性取 store 单例 —— `apply` 是唯一写入口，store 在这里才被需要。"""
    from core import datastore
    return getattr(datastore, name)


# ── 同名字条目的**寻址**（机制，不是语义判断）────────────────────────────
# 注册表按**名字**操作，而同名多条是常态（同一天两门课的同名作业、周期任务的历史发生）。
# 用来**指认是哪一条**的字段（给了就按**全等**匹配）：
#   date                        —— 排期日期
#   anchored_to / occurrence_of —— 周期任务的身份（这条属于哪一次发生）
#   session_key                 —— 课次身份（`date|periods|time`）
# ★ 内核**不猜**"哪条才算重复"：只按显式字段匹配；指认不出唯一一条就**拒绝**。


def _addressing_error(action: str, name: str, same: list, used: dict, why: str) -> SystemExit:
    """同名多条、又指认不出唯一一条 ⇒ **拒绝**（不猜、不全删、不静默只动第一条）。

    ★ 为什么必须拒绝而不是"顺手处理第一条"：静默只动第一条 ⇒ 调用方以为改对了，
      拿着 `ok: true` 继续往下走 —— 这类数据事故（用户 2026-10-15 的两条同名
      「示例课程D作业」）就是它造成的。错误里带 `candidates`，让调用方能自己挑。
    """
    return SystemExit(json.dumps({
        "ok": False,
        "error": f"list.{action} 无法唯一定位「{name}」：同名 {len(same)} 条，{why}",
        "candidates": [_fingerprint(d) for d in same],
        "hint": (f"每项加消歧字段（{' / '.join(DISAMBIGUATORS)}）指认是哪一条；"
                 "内核不猜、也不会只动第一条"),
    }, ensure_ascii=False))


def _resolve_one(action: str, name: str, registry: list, sel: dict,
                 date_is_content: bool = False,
                 single_is_unambiguous: bool = False) -> tuple:
    """从同名候选里挑出**唯一**一条。返回 `(命中条目 | None, 实际生效的消歧字段)`。

    规则（确定性、可复述）：
      · **表里没有同名条目** ⇒ `(None, {})`（调用方：`upsert` 新增 / `remove` 记 missing）
      · `single_is_unambiguous=True`（`upsert` 用）：**同名只有一条** ⇒ 就是它 ——
        此时给出的 `date` / `occurrence_of` / `anchored_to` 都是**内容**（改期 / 补认领），
        不是"指认"；只有同名多条才需要指认（见下）
      · 没给消歧字段：唯一一条 ⇒ 就是它；**同名多条 ⇒ 拒绝**
      · 同名多条 + 给了消歧字段：必须与候选逐字段**全等**，最后剩唯一一条 ⇒ 就是它
      · 同名多条时 `date_is_content=True`（`upsert` 用）：`date` 若跟任何候选都对不上，
        说明调用方正想**改这个日期**，于是忽略它、用别的字段指认
        （`remove` 没有这个豁免：那里的 `date` 就是身份，对不上就拒绝）
      · 同名多条而剩 0 条 / ≥2 条 ⇒ **拒绝**；`remove` 的唯一候选但显式对不上 ⇒ 也拒绝
    """
    same = [d for d in registry if d["task"] == name]
    if not same:
        return None, {}
    if single_is_unambiguous and len(same) == 1:
        return same[0], {}
    if not sel:
        if len(same) == 1:
            return same[0], {}
        raise _addressing_error(action, name, same, {}, "而且没给消歧字段")
    got, used = same, {}
    for k, v in sel.items():
        narrowed = [d for d in got if d.get(k) == v]
        if not narrowed and k == "date" and date_is_content:
            continue                      # 对不上任何候选 ⇒ 它是"要改成的新日期"
        got, used = narrowed, {**used, k: v}
    if len(got) == 1:
        return got[0], used
    raise _addressing_error(action, name, same, used,
                            f"按 {used or '（空）'} 过滤后剩 {len(got)} 条"
                            + ("（消歧字段与候选对不上）" if not got else ""))


def _apply_list(cand: dict, sec: dict) -> dict:
    """注册表那一节：`upsert`（整条替换 + 继承关系字段）/ `remove`（按名字，可消歧）。

    ★ `upsert` 是**整条替换**（不合并内容，避免留下过期字段），但**关系字段必须继承**
      —— 它们不是内容，而是"这条属于什么"：`session_key`（对应课表哪一节）、
      `occurrence_of` / `anchored_to`（属于哪个周期任务、锚在哪次课）。
      实测：改一条周期实例时 upsert 丢了 `occurrence_of` ⇒ 同一条既算周期实例又算
      独立条目，anomalies 立刻报重复、sync 还会再生成一条。

    ★★ **同名多条必须能指认**（见 `_resolve_one` / `DISAMBIGUATORS`）：
       · `upsert` / `remove` 每一项都可以带消歧字段，给了就只作用于**全等**的那条
       · 不给消歧字段而同名多条 ⇒ **拒绝**并报出候选 —— 原先 `remove` 把同名的**全删**、
         `upsert` 只动**第一条**，调用方还看不出来
       · `remove` 的项可以是名字字符串，也可以是 `{"task": …, "date": …}` 这样的对象
       · 报告里的 `matched_by` 说清"按什么字段匹配、命中了哪一条"
    """
    created = updated = 0
    matched_by: list = []

    for entry in sec.get("upsert", []):
        if not isinstance(entry, dict) or "task" not in entry:
            raise SystemExit(f"list.upsert 每项都要是带 task 的对象，收到: {entry!r}")
        entry = {"status": "pending", **entry}      # 不默认填 priority：编出来的轻重会掩盖真实轻重
        name = str(entry["task"])
        sel = {k: entry[k] for k in DISAMBIGUATORS if k in entry}
        hit, used = _resolve_one("upsert", name, cand["registry"], sel,
                                 date_is_content=True, single_is_unambiguous=True)
        if hit is None:
            # `_resolve_one` **只在"表里没有同名条目"时**返回 None（同名多条它直接拒绝）
            # ⇒ 这里就是**新增**：哪怕带了 `date` / `occurrence_of`，那些也是**内容**、
            # 不是"指认"（曾经的回归：把 `date` 当消歧字段 ⇒ 新任务全被拒）。
            cand["registry"].append(entry)
            created += 1
            continue
        for rf in ("session_key", "occurrence_of", "anchored_to"):
            if rf not in entry and rf in hit:      # ★ 关系字段继承（见 docstring）
                entry[rf] = hit[rf]
        idx = next(i for i, d in enumerate(cand["registry"]) if d is hit)
        cand["registry"][idx] = entry
        updated += 1
        if used:
            matched_by.append({"action": "upsert", "task": name, "by": used,
                               "picked": _fingerprint(hit)})

    removed, missing = [], []
    for item in sec.get("remove", []):
        if isinstance(item, dict):
            name = str(item.get("task") or "").strip()
            if not name:
                raise SystemExit(f"list.remove 的对象项要有 task（可带消歧字段），收到: {item!r}")
            sel = {k: item[k] for k in DISAMBIGUATORS if k in item}
        else:
            name, sel = str(item), {}
        hit, used = _resolve_one("remove", name, cand["registry"], sel)
        if hit is None:
            missing.append(name)                    # 没匹配上 → 如实报，不静默
            continue
        cand["registry"] = [d for d in cand["registry"] if d is not hit]
        removed.append(name)
        if used:
            matched_by.append({"action": "remove", "task": name, "by": used,
                               "picked": _fingerprint(hit)})

    return {"added": created, "updated": updated,
            "removed": removed, "missing": missing, "replaced": [],
            "matched_by": matched_by}


def _schedule_diff(old: list, new: list) -> list:
    """课表整段替换前后的**逐格差异**（机械比对，逼 Agent 回读）。

    ★ 用户 2026-11-10：「户外改教室 这些都是随着课表变化的 要求 AGENT 严格按照课表来」。
      内核看不见图，但能如实报"你这次到底改了哪几格" —— 教室从 `校外场地` 变成
      `教学楼 示例教室X1`、节次挪了、整格删了/加了，全都在这里。Agent 必须把这份差异
      念给用户（不念就等于没核对），用户才对得上图。
    身份 = (日期, 节次|时间, 课名)；只比**内容**（教室 / kind / note 变化）。
    """
    def key(s: dict):
        return (str(s.get("date") or ""),
                str(s.get("periods") or ""), str(s.get("time") or ""),
                str(s.get("course") or ""))

    oldmap = {key(s): s for s in (old or [])}
    newmap = {key(s): s for s in (new or [])}
    added = [s for k, s in newmap.items() if k not in oldmap]
    removed = [s for k, s in oldmap.items() if k not in newmap]
    changed = []
    for k, s in newmap.items():
        o = oldmap.get(k)
        if not o:
            continue
        diffs = {}
        for field in ("location", "periods", "time", "kind", "note"):
            if str(o.get(field) or "") != str(s.get(field) or ""):
                if field in ("periods", "time"):
                    continue                      # 节次/时间是身份的一部分，改了就是删+增
                diffs[field] = {"was": str(o.get(field) or ""), "now": str(s.get(field) or "")}
        if diffs:
            changed.append({"date": str(s.get("date") or ""),
                            "periods": str(s.get("periods") or s.get("time") or ""),
                            "course": str(s.get("course") or ""), "fields": diffs})

    def brief(s):
        return {"date": str(s.get("date") or ""),
                "periods": str(s.get("periods") or s.get("time") or ""),
                "course": str(s.get("course") or ""),
                "location": str(s.get("location") or "")}

    out = []
    if added:
        out.append({"kind": "added",
                    "entries": [brief(s) for s in sorted(added, key=lambda x: str(x.get("date")))]})
    if removed:
        out.append({"kind": "removed",
                    "entries": [brief(s) for s in sorted(removed, key=lambda x: str(x.get("date")))]})
    if changed:
        out.append({"kind": "changed", "entries": sorted(changed, key=lambda x: x["date"])})
    return out


def _cmd_apply(args) -> dict:
    """**通用写入端口**：一次调用可以改任意几个文件。

    payload = {"schedule": {...}, "rules": {...}, "list": {"upsert": [...], "remove": [...]}}
    （每节的可用键见 `APPLY_SECTIONS`。）

    ★ 这是「图片 → 结构化 → 改 JSON」的**唯一入口**：模型看完图，把"要改什么"写成
      这份意图送进来 —— 改哪个文件由**内容**决定，不由调用哪个命令决定。

    ★ **先全量校验、再依次落盘**：任一处不合规 ⇒ 一个字节都不写。
      跨文件做不到真事务，但"先校验完再写"挡掉了绝大多数半写状态。
    """
    from core.datastore import data as data_store

    payload = _read_payload(args)
    if not isinstance(payload, dict):
        raise SystemExit("apply 需要 JSON 对象（可含 schedule / rules / list 三节）")
    # ★ 先过"整段替换课表"的保护闸（用户 2026-10-16：导入新课表把本周课次冲没了）
    _guard_schedule_replace(payload)
    unknown = sorted(set(payload) - set(APPLY_SECTIONS) - {"list"})
    if unknown:
        raise SystemExit("apply 不认识这些节: %s（可用：%s）"
                         % (unknown, " / ".join([*APPLY_SECTIONS, "list"])))

    pending: list = []
    report: dict = {}
    diff_before: dict = {}       # 写之前的样子（只为了算出"这次改了哪几格"）

    for name, (get_store, fname, validate, plain, colls) in APPLY_SECTIONS.items():
        if name not in payload:
            continue
        sec = payload[name]
        # ★ 单集合的节（如 `reminders`）允许**省掉一层**：`{"reminders": {"upsert": […]}}`
        #   与 `{"reminders": {"reminders": {"upsert": […]}}}` 等价。模型少写一层就少错一次；
        #   多集合的节（schedule / rules）行为完全不变。
        if len(colls) == 1 and not plain:
            _only = next(iter(colls))
            if isinstance(sec, list) or (isinstance(sec, dict) and set(sec) <= {"upsert", "remove"}):
                sec = {_only: sec}
        if not isinstance(sec, dict):
            raise SystemExit(f"{name} 节需要对象")
        bad = sorted(set(sec) - set(colls) - set(plain))
        if bad:
            raise SystemExit(f"{name} 不认识的键: {bad}"
                             f"（可用：{' / '.join([*plain, *colls])}）")
        store = get_store()
        cand = dict(store._data)
        if name == "schedule":
            diff_before = {"dated_sessions": list(cand.get("dated_sessions") or [])}
        for k in plain:
            if k in sec:
                cand[k] = sec[k]
        rep_file = {}
        for key, (target, ident, name_of) in colls.items():
            if key not in sec:
                continue
            kw = {"name_of": name_of} if name_of else {}
            cand[target], rep_file[target] = _apply_collection(
                cand.get(target, []), sec[key], ident, f"{name}.{key}", **kw)
        validate(cand)
        if rep_file:                       # 只报**这次真的碰过**的集合，空 {} 是噪声
            report[name] = rep_file
        pending.append((store, fname, cand))

    if "list" in payload:
        if not isinstance(payload["list"], dict):
            raise SystemExit("list 节需要对象（可含 upsert / remove）")
        cand = json.loads(json.dumps(data_store._data))      # 深拷贝：先不动盘
        list_report = _apply_list(cand, payload["list"])
        validate_data(cand)
        pending.append((data_store, "data.json", cand))
        report = {"list": list_report, **report}

    # ---- 全部校验通过，才开始动盘 ----
    written = []
    for store, label, cand in pending:
        store._data = cand
        store._save()
        written.append(label)

    # ★ 课表改动**必须**回读（用户 2026-11-10：「严格按照课表来」）：
    #   内核看不出图对不对，但能如实列出"这次加了/删了/改了哪几格"（含教室变化）⇒
    #   Agent 必须把这份差异念给用户；不念就等于没核对。
    _diff_out = {}
    if diff_before:
        _after = next((c for (st, lb, c) in pending if lb == "schedule.json"), None)
        if isinstance(_after, dict):
            _sd = _schedule_diff(diff_before.get("dated_sessions") or [],
                                 _after.get("dated_sessions") or [])
            if _sd:
                _diff_out["schedule_diff"] = _sd

    # ★ 报告收进**一个** `report` 键、按文件分（list / schedule / rules）：
    #   原先 list 的报告单独放顶层，而工具描述说"看 report" ⇒ 只改注册表时
    #   `report` 是空的，模型会以为啥也没写成（实测踩到）。
    # ★ `check_this` 的标签保持**集合级**（`schedule.sessions`）—— 嵌一层之后
    #   遍历顶层会把标签变成"schedule"，模型就看不出是哪个集合出了问题。
    # ★★ `_leaf_reports()` 是**唯一**的"摊平"实现：`check_this` 与"计划是否作废"
    #   都走它。原先"计划作废"那段只看顶层 `rep.get("added")`，而 `report["schedule"]`
    #   是嵌套的（`{"dated_sessions": {…}}`）⇒ **只改课表时 `plan_invalidated` 永不附带**，
    #   一份基于旧课表的规划就被当成"新鲜的"继续用。
    warns = []

    def _leaf_reports() -> list:
        """把 `report` 摊平成 `[(标签, 集合级报告)]`：`list` 一层，`schedule`/`rules` 两层。"""
        leaves = []
        for key, file_rep in report.items():
            if not isinstance(file_rep, dict):
                continue
            if key == "list":
                leaves.append(("list", file_rep))
            else:
                leaves += [(f"{key}.{coll}", rep) for coll, rep in file_rep.items()
                           if isinstance(rep, dict)]
        return leaves

    def _collect(label: str, rep: dict) -> None:
        if rep.get("missing"):
            warns.append(f"{label}.missing={rep['missing']}")
        if rep.get("replaced"):
            warns.append(f"{label}.replaced="
                         f"{[f'{r['was']}→{r['now']}' for r in rep['replaced']]}")

    for label, rep in _leaf_reports():
        _collect(label, rep)
    # ★ 这里原先还有一段 `multi`（"同名命中多条 ⇒ 只替换了第一条"）的警告 ——
    #   现在同名多条要么被消歧字段**唯一定位**（记录在 `report.list.matched_by`），
    #   要么直接**拒绝**（`SystemExit`，一个字节都不写）：不再有"只动第一条"这种事。

    out = {"ok": True, "action": "applied", "written": written, "report": report}
    # ★ 课表改动的**逐格差异**（含"户外 → 教室"这种变化）—— 必须回读给用户（2026-11-10）
    out.update(_diff_out)
    # 真的改了东西 ⇒ 上一份计划作废（事实，写在返回值里；"该重排"是准则 14 的事）
    if any(rep.get("added") or rep.get("updated") or rep.get("removed")
           or rep.get("replaced") for _, rep in _leaf_reports()):
        out.update(_plan_invalidated())
    if warns:
        out["check_this"] = warns
        out["note"] = ("★ 上面 check_this 里的东西**要你自己确认**："
                       "missing = 你要删的没匹配上（名字或身份写错了）；"
                       "replaced = 同身份被覆盖且名字变了（可能误伤了别的东西）。"
                       "★ `report.list.matched_by` 非空 ⇒ 那次操作是**按消歧字段**"
                       "定位到唯一一条的（同名多条时必须有它）。"
                       "★ 同名多条又不给消歧字段时，内核**拒绝**整次写入并列出候选 —— "
                       "不会只动第一条、也不会全删。")
    return out


def cmd_show(args) -> dict:
    """取回注册表里某条的**完整结构体**（含 detail）。

    用途：用户问"这次作业做哪几题""有什么额外要求"时，从 detail 回答 ——
    detail 是模型当初从图上读到并结构化的原文，内核只负责原样还回去。

    ★ **已归档的条目默认不给完整 detail**（用户要求：归档后模型只看 list，
      非必要不看完整信息）。要看需显式带 `cold: true` —— 让"这次是必要的"成为
      一个**可观测的动作**，而不是模型顺手就能拉走全部历史细节。
    ★ 失败时的 `known` **只列活跃注册里的名字**：它们是模型能操作的对象。
      以前并上了整份冷存储的名字，那条**失败提示**会随年头线性膨胀
      （四年 400+ 个名字）—— 而失败提示恰恰最该短、最该可操作。
    """
    from core.datastore import archive as arch

    dat = _load(DATA_FILE, validate_data)
    hit = next((d for d in dat.get("registry", []) if d["task"] == args.task), None)
    archived = False
    if hit is None:
        # 已搬进冷存储的也取得到：过期注册是**默认隐藏**，不是不可查
        hit = arch.find_registry(args.task)
        archived = hit is not None
    if hit is None:
        known = sorted({d["task"] for d in dat.get("registry", [])})
        raise SystemExit(json.dumps({
            "ok": False,
            "error": f"活跃注册里没有名为「{args.task}」的条目（名称需完全一致）",
            "known": known,
            "archived_excluded": ("已归档的旧条目不在 known 里（历史无限、不注入上下文），"
                                  "要回溯用 history 按日期查。"),
        }, ensure_ascii=False))

    has_detail = bool(hit.get("detail"))
    if archived and not getattr(args, "cold", False):
        return {
            "ok": True, "archived": True, "has_detail": has_detail,
            "entry": {k: v for k, v in hit.items() if k != "detail"},
            "note": ("这是**已归档**条目（不在活跃注册里，对模型默认不可见）。"
                     "完整 detail **没有**返回 —— 只有在用户明确追问这一条的细节时，"
                     "才带 `cold: true` 再取一次。"),
        }
    return {"ok": True, "entry": hit, "has_detail": has_detail, "archived": archived}


def cmd_plan_snapshot(args) -> dict:
    """存规划快照。

    **优先用 `--file`**：模型用 write 工具落一个 UTF-8 文件再传路径进来。
    Windows 的 shell 管道按控制台代码页重编码，`echo '<中文JSON>' | python` 会乱码
    （项目已在 .bat/GBK 上栽过一次，不要在编码上再赌一次）。
    """
    from core.datastore import plans

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    else:
        raw = sys.stdin.read().strip()
    if not raw:
        raise SystemExit("plan-snapshot 需要 --file 或 stdin 传入规划 JSON")
    plan = json.loads(raw)
    # ★ 去重：与上一份**完全相同**的正文 ⇒ 不存（打断"重排→存→brief→重排"的空转）
    #   判据是**整份 plan**，不只是 slots —— 否则"slots 不变、只补 allow 豁免"这种修正
    #   会被吃掉，而技能教的正是"用户同意破例后把 allow 写进快照正文再重存"。
    _last = plans.latest() or {}
    if plan and _plan_key(plan) == _plan_key(_last.get("plan")):
        return {"ok": True, "action": "snapshot_unchanged", "stored": False,
                "note": "这份规划与上一份**完全一样**（含 allow/focus/order）—— 没存。改了再来。"}
    item = plans.snapshot(plan, args.triggered_by, inputs=_plan_inputs())
    return {"ok": True, "action": "snapshot_saved", "triggered_by": args.triggered_by,
            "made_at": item["made_at"], "kept": plans.count(),
            "note": f"快照只留最近 {plans.KEEP} 次，更旧的自动淘汰（文件存储，有界）"}


# ────────────────────────── 入口 ──────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="信息中心 · 确定性内核 CLI")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("context", help="输出完整状态上下文（只读）")
    se = sub.add_parser("sessions", help="未来 N 天的课次与待办，按**实际日期**（只读）")
    se.add_argument("--days", type=int, default=14, help="往后看几天（默认 14）")
    se.add_argument("--limit", type=int, default=SESSIONS_LIMIT,
                    help=f"单次最多返回几节（默认 {SESSIONS_LIMIT}；课表可以给一学期，"
                         f"但模型视图有限 —— 超了报 more/capped_at）")
    sub.add_parser("validate", help="校验各数据文件（只读）")

    sub.add_parser("sync", help="物化：把周期任务的「下一件」注册成列表里的单次待办（写操作）")

    ap_ = sub.add_parser("apply",
                         help="★ 通用写入端口：一次改任意几个文件（先全量校验，再落盘）")
    ap_.add_argument("--file", help="改写意图 JSON 文件（schedule / rules / list 三节）")
    ap_.add_argument("--sync", action="store_true", help="落盘后立即物化进 LIST")

    sh = sub.add_parser("show", help="取回某条的完整结构体（含 detail）")
    sh.add_argument("--task", required=True)
    sh.add_argument("--cold", action="store_true",
                    help="已归档条目也返回完整 detail（默认只给列表视图字段 —— "
                         "归档后非必要不看完整信息）")

    hi = sub.add_parser("history", help="已滚出的注册（日期已过）—— 默认对模型隐藏")
    hi.add_argument("--days", type=int, default=30, help="往前查几天（默认 30）")
    hi.add_argument("--limit", type=int, default=HISTORY_LIMIT,
                    help=f"单次最多返回几条（默认 {HISTORY_LIMIT}；历史无限、视图有限）")

    an = sub.add_parser("anomalies", help="确定性异常清单（只读）")

    mk = sub.add_parser("mark", help="标记状态（按名称完全一致；同名多条要用 --date 消歧）")
    mk.add_argument("--task", required=True)
    mk.add_argument("--status", required=True,
                    choices=["done", "pending", "expired", "cancelled"])
    mk.add_argument("--date", help="消歧：只标记 `date` 全等的那一条（同名多条时必给；"
                                   "不给而同名多条会被拒绝并列出候选）")

    ps = sub.add_parser("plan-snapshot", help="存规划快照（优先 --file，避开 shell 编码）")
    ps.add_argument("--file", help="规划 JSON 文件路径（推荐）")
    ps.add_argument("--triggered-by", dest="triggered_by", default="信息更新")

    rv = sub.add_parser("review", help="审查草稿规划：撞课/重叠/漏排（只读，不落盘）")
    rv.add_argument("--file", help="草稿 JSON 文件（{'slots': [...]}；不给则读 stdin）")

    bf = sub.add_parser("brief", help="把三种输出排好版给出来（只读）：plan/schedule/todo")
    bf.add_argument("--kind", default="plan", help="plan（规划·不含课）/ schedule（日程·含课）/ todo（待办两张表）")
    bf.add_argument("--days", type=int, default=3,
                    help="只展开前 N 天（默认 3；计算仍是 7 天）")
    wk = sub.add_parser("week", help="某一周课表的完整回读（只读，含停课/调课与依据）—— 录完逐条念给用户")
    wk.add_argument("--date", default="", help="这一周里的任意一天 YYYY-MM-DD（默认今天）")
    rm = sub.add_parser("reminders", help="提醒清单（只读）—— 独立于任务与规划，不进 slots")
    rm.add_argument("--days", type=int, default=7, help="往后看几天（默认 7）")
    rm.add_argument("--due", action="store_true",
                    help="只给现在该说了的（到点、未投递、迟到不超过 %d 小时）" % REMINDER_LATE_HOURS)
    rf = sub.add_parser("reminder-fired", help="标记提醒已投递（管道用；身份用 --key）")
    rf.add_argument("--key", action="append", default=[], help="reminders 返回的 key（可重复）")
    wl = sub.add_parser("wall", help="大屏只读视图：规划快照完整结构（含每块的 why）")
    wl.add_argument("--days", type=int, default=7, help="只看未来 N 天（默认 7）")
    nt = sub.add_parser("next-tick",
                        help="下一个该唤醒的时刻（只读）：给控制层「跟着规划走」用")
    nt.add_argument("--after", help="当作「现在」的时刻（YYYY-MM-DD HH:MM；默认真现在）")
    nt.add_argument("--anchor", default=ANCHOR_HHMM,
                    help=f"没有可跟的边界时回落到几点（默认 {ANCHOR_HHMM}，次日）")
    return ap


def cmd_review(args) -> dict:
    """审查一份**草稿规划**：撞课 / 时段重叠 / 漏排。**只报，不改，不落盘。**

    ★ 只做**时间算术**：撞课 / 时段重叠 / 课间 / 漏排 / 规则的**结构化窗口**。
      自然语言规则（"晚上不排活"这类）内核不解释 —— 能查的只有结构化的那两类窗口：
        · `no_slots`           —— 绝对时段（固定时刻）
        · `before_first_class` —— 相对当天课表：`00:00 → 当天第一节课开始`，
          且仅当第一节课开始 ≤ `max_start`（默认 12:00）时当天生效
      判据只有一份实现（`_window_hits`），`brief` 走同一份。

    输入：{"slots": [{"date": "YYYY-MM-DD", "time": "HH:MM-HH:MM", "task": "…"}, …]}
    输出：`ok`（无冲突为真）+ `conflicts`（撞课/重叠）+ `unscheduled`（漏排）
    """
    payload = _read_payload(args)
    slots = payload.get("slots") if isinstance(payload, dict) else None
    if not isinstance(slots, list):
        raise SystemExit('review 需要 {"slots": [{"date","time","task"}, …]}')
    sched = _load(SCHEDULE_FILE, validate_schedule)
    dat = _load(DATA_FILE, validate_data)
    rules = _load(RULES_FILE, validate_rules)
    periods = sched.get("periods", {})

    def span(t):
        a, _, b = str(t or "").partition("-")
        return a.strip(), b.strip()

    rows = []
    for i, s in enumerate(slots):
        a, b = span(s.get("time"))
        if not (s.get("date") and a and b):
            raise SystemExit(f"slots[{i}] 需要 date 与 time（HH:MM-HH:MM）：{s!r}")
        rows.append({"date": s["date"], "a": a, "b": b, "task": s.get("task", "")})

    conflicts, warnings = [], []
    # ① 撞课（同期、非停课）
    #    ★ 取课次只走 `_visible_sessions`（停课不算课）—— 与 brief 的日程同一口径。
    for r in rows:
        for c in _visible_sessions(sched):
            if c.get("date") != r["date"]:
                continue
            ca, cb = session_window_of(c, periods)
            cw = _span(f"{ca}-{cb}") if (ca and cb) else None
            rw = _span(f"{r['a']}-{r['b']}")
            if not (cw and rw and rw[0] < cw[1] and cw[0] < rw[1]):
                continue
            if _is_water(sched, c):
                warnings.append({"kind": "water_class", "date": r["date"],
                                 "slot": f"{r['a']}-{r['b']}", "task": r["task"],
                                 "course": c.get("course", ""),
                                 "note": "水课时段可用但不建议：只排不费脑的活"})
            else:
                conflicts.append({"kind": "clashes_class", "date": r["date"],
                                  "slot": f"{r['a']}-{r['b']}", "task": r["task"],
                                  "course": c.get("course", "")})
    # ①d 块的**起点早于**条目的 `start` ⇒ 排了也做不了（用户 2026-10-27）
    #     ★ 只比块的起点：块可以跨过开始时刻，只要起点不早于它。
    start_of = {}
    for _d in _active(dat):
        if _d.get("start"):
            start_of[str(_d.get("task"))] = (str(_d["start"]), str(_d.get("start_time") or ""))
    for r in rows:
        st = start_of.get(r["task"])
        if not st:
            continue
        sdate, stime = st
        if r["date"] < sdate or (r["date"] == sdate and stime and r["a"] < stime):
            conflicts.append({"kind": "before_start", "date": r["date"],
                              "slot": f"{r['a']}-{r['b']}", "task": r["task"],
                              "start": sdate, "start_time": stime,
                              "note": "这件事要到这里才开始，排在之前做不了"})

    # ①e 块的时长比"预留时长"短 ⇒ 排了也做不完（用户 2026-11-08 的 estimate_minutes）
    #     ★ 只警告、不阻塞：拆成两块接力也是合法的，由 Agent/用户决定。
    est_of = {}
    for _d in _active(dat):
        try:
            _n = int(_d.get("estimate_minutes") or 0)
        except (TypeError, ValueError):
            _n = 0
        if _n:
            est_of[str(_d.get("task"))] = _n
    for r in rows:
        est = est_of.get(r["task"])
        if not est:
            continue
        a_m, b_m = _mins(r["a"]), _mins(r["b"])
        if a_m is None or b_m is None:
            continue
        span = b_m - a_m
        if span > 0 and span < est:
            warnings.append({"kind": "slot_shorter_than_estimate", "date": r["date"],
                             "slot": f"{r['a']}-{r['b']}", "task": r["task"],
                             "slot_minutes": span, "estimate_minutes": est,
                             "note": "这一块比预留时长短 —— 要么找更长的整块，要么拆成两段接力"})

    # ①c 算不出时刻的课次 ⇒ 上面的撞课**根本没比**（不能静默）：与 brief 同一实现
    warnings += _unknown_class_time(sched, {r["date"] for r in rows})
    # ①b 课间禁排（**硬约束**）：唯一实现在 _class_gap —— 这里曾经与 brief 各写一份，两份口径不同
    for r in rows:
        g = _class_gap(r["date"], r["a"], r["b"], sched, periods)
        if g:
            conflicts.append({"kind": "in_class_gap", "date": r["date"],
                              "slot": f"{r['a']}-{r['b']}", "task": r["task"],
                              "gap_minutes": g["gap_minutes"], "between": g["between"],
                              "note": "课间禁排（改教室/转场）"})

    # ② 自重叠
    for x in range(len(rows)):
        for y in range(x + 1, len(rows)):
            if rows[x]["date"] == rows[y]["date"] and \
                    rows[x]["a"] < rows[y]["b"] and rows[y]["a"] < rows[x]["b"]:
                conflicts.append({"kind": "overlap", "date": rows[x]["date"],
                                  "a": f"{rows[x]['a']}-{rows[x]['b']} {rows[x]['task']}",
                                  "b": f"{rows[y]['a']}-{rows[y]['b']} {rows[y]['task']}"})

    # ②b 规则窗口：`no_slots`（固定时刻）+ `before_first_class`（相对当天课表）
    #     —— **唯一实现**在 `_window_hits`，`brief` 走同一份
    #     （自然语言的 body 内核不解释 —— 那是语义；这里只比时间）
    for h in _window_hits(rules, sched, [(x["date"], x["a"], x["b"], x["task"]) for x in rows]):
        (conflicts if h["rule_kind"] == "constraint" else warnings).append(h)

    # ②d 排在自己的硬截止之后（**等于没排**）—— 与 brief 共用同一个函数
    _late_hard, _late_soft = _past_deadline(
        [(x["date"], x["a"], x["b"], x["task"]) for x in rows], dat)
    conflicts.extend(_late_hard)
    warnings.extend(_late_soft)

    # ③ 漏排（课次条目不算任务）—— **共用函数**：`brief` 也走它（同一判据只有一份）
    planned = {r["task"] for r in rows}
    unscheduled = _unscheduled(dat, planned)

    return {
        "ok": not conflicts,
        "slots": len(rows),
        "conflicts": conflicts,
        "warnings": warnings,
        "unscheduled": unscheduled,
        "note": ("★ 有 conflicts **或 warnings** 就重排（regen）再交出去 —— "
                 "用户看到的规划必须是干净的。"
                 "warnings = 软偏好（kind=preference）的规则被碰到：**也要消掉**；"
                 "确实排不下才报告（说清卡在哪）。"
                 "unscheduled 里的「无 DDL，可后补」只是提示，不阻塞。"),
    }


WD_CN = ["一", "二", "三", "四", "五", "六", "日"]


def _d_short(d: str) -> str:
    """`2026-10-13` → `10/13`。"""
    return f"{int(d[5:7])}/{int(d[8:10])}"


def _d_label(d: str) -> str:
    """`2026-10-13` → `10/13 周二`。"""
    w = WD_CN[datetime.strptime(d, "%Y-%m-%d").weekday()]
    return f"{_d_short(d)} 周{w}"


def _due_short(t: str) -> str:
    """`due_time` 的短形：取 `（` 之前那段（自然语言尾巴太长）。"""
    return (t or "").split("（")[0].strip()


def cmd_wall(args) -> dict:
    """大屏 / 外部只读视图：规划快照的**完整**结构（含每块的 `why`）。

    ★ **契约（只增不改，外部按这个消费）**：

        {
          "now":    {date, time, weekday, phase, iso},
          "created_at": "YYYY-MM-DD HH:MM:SS",     # 快照存下来的时刻
          "triggered_by": "…",                     # 为什么排的
          "focus":  "…",                           # 这份规划的重心
          "horizon": "…",                          # 排到哪天
          "order":  ["…", …],                      # 优先级次序（注册名）
          "slots": [
            {"date": "YYYY-MM-DD", "time": "HH:MM-HH:MM", "task": "…",
             "passed": false,                      # 这个时段是否已过
             "why":    "…",                        # ★ 为什么排这儿（有就带，没有省略）
             "gist":   "…",                        # 可执行要点（有就带，没有省略）
             "urgency": "urgent|soon|normal|later"} # 该任务的紧迫度档位
          ]
        }

    · **只读、零副作用**（与 `sessions` / `context` 同级承诺）
    · `why` / `gist` 缺省时**省略**，**不写空串**
    · `task` **不截断**（`context.last_plan` 截到 40 字是给模型省 token 的，这里不省）
    · 与 `context.last_plan` 的区别：那里最多 12 块且无 why —— 给模型看的；
      这里全给，给大屏看的。
    """
    from core.datastore import plans

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    horizon_days = max(1, int(getattr(args, "days", 7) or 7))
    end = (now.date() + timedelta(days=horizon_days)).isoformat()

    snap = plans.latest() or {}
    plan = snap.get("plan") or {}

    dat = _load(DATA_FILE, validate_data)
    active = _list_view([d for d in _active(dat, now=now) if not d.get("session_key")],
                        now=now)
    by_task = {d["task"]: d for d in active}

    # 先算一次"哪块越过了自己的硬截止"（与 brief/review 同一个函数），再逐块挂上去。
    #   键用 (任务, 日期, 起始时刻)：`_past_deadline` 的 slot 是 "a-b"，空格写法也不怕。
    _rows, _slot_flags = [], {}
    for s in (plan.get("slots") or []):
        if not isinstance(s, dict) or not s.get("task"):
            continue
        _p = str(s.get("time", "")).split("-")
        _a = _p[0].strip() if _p else ""
        _b = _p[1].strip() if len(_p) > 1 else _a
        _rows.append((str(s.get("date", "")), _a, _b, str(s["task"])))
    _hard, _soft = _past_deadline(_rows, dat, now)
    for _h in _hard + _soft:
        _slot_flags[(_h["task"], _h["date"], _h["slot"].split("-")[0])] = _h["kind"]

    slots = []
    for s in (plan.get("slots") or []):
        if not isinstance(s, dict) or not s.get("task"):
            continue
        date, time = str(s.get("date", "")), str(s.get("time", ""))
        if date and not (today <= date <= end):
            continue
        item = {"date": date, "time": time, "task": str(s["task"])}
        why = s.get("why")
        if why:
            item["why"] = str(why)
        hit = by_task.get(str(s["task"]))
        if hit and hit.get("gist"):
            item["gist"] = str(hit["gist"])
        if hit:
            item["urgency"] = _urgency(hit, now)[1]
        item["passed"] = _passed_at(date, time, now)
        # ★ DDL 与这块的关系（用户 2026-10-15：严重的 DDL/规划冲突要标红；
        #   任务 DDL 进红区时那块要**跟着闪**）。机械事实，判据与 brief/review 同一份。
        if hit and hit.get("date"):
            _m = _due_by_task(dat).get(str(s["task"]))
            if _m:
                item["ddl"] = _m.strftime("%Y-%m-%d %H:%M")
                item["ddl_in_minutes"] = int((_m - now).total_seconds() // 60)
        _flag = _slot_flags.get((str(s["task"]), date, time.split("-")[0].strip()))
        if _flag:
            item["flags"] = [_flag]
        slots.append(item)

    # ★ **排序**：外部唯一消费者（`外部时间面板消费者`）把 `slots[0]`
    #   当"计划表**最上面**那条（最早的时段）"—— 快照的书写顺序不可靠，排序是确定性事实。
    #   无 `date` 的块排最后（它不是合法时段，不该占最上面那行）。
    slots.sort(key=lambda x: (x["date"] or FAR_FUTURE, x["time"] or ""))

    return {
        "now": _now_view(now),
        "created_at": snap.get("created_at", ""),
        "triggered_by": snap.get("triggered_by", ""),
        "focus": plan.get("focus") or plan.get("summary", ""),
        "horizon": plan.get("horizon", ""),
        "order": plan.get("order") or [],
        "slots": slots,
        # ★ 提醒（用户 2026-11-08）：和 slots 平行、**不是时段** —— 大屏拿它显示"几点提醒什么"。
        "reminders": _reminder_view(now, days=2),
        # ★ 整份规划的把关结论（用户 2026-10-15：规划要标红提醒）。
        #   `problems` 与 `brief` 同一口径：**排到还没到的硬截止之后**（改得动 ⇒ 阻塞）
        #   + **今天有 DDL 却没排上**。截止已过的只进 `warnings`（追不回，不阻塞）。
        "problems": _hard + [
            {"kind": "unscheduled", "task": u["task"], "date": u["date"],
             "note": "今天有 DDL 却不在这份规划的任何时段里"}
            for u in _unscheduled(dat, {x["task"] for x in slots if x.get("task")}, now=now)
            if u.get("date") == today],
        "warnings": _soft,
        "ok": not (_hard or [
            u for u in _unscheduled(dat, {x["task"] for x in slots if x.get("task")}, now=now)
            if u.get("date") == today]),
    }


def cmd_next_tick(args) -> dict:
    """**下一个该唤醒的时刻**（只读，纯算术）—— 给控制层「跟着规划走」用。

    用户 2026-10-15：不要单纯用定时器，要跟着规划的下一时间点走
    （「现在三点，PLAN 的下一项是 4-5 点做一个作业，到了 5 点就自动触发更新」）。

    ★ 内核只回答「下一个边界是哪一刻、为什么是它」，**不回答「到了该干什么」** ——
      到点那一轮的语义（默认当作上一块没做完、往后挪、要不要发消息）归准则/技能。

    边界 = 第一块**结束时刻严格晚于 now** 的块的结束时刻（只看今天及以后）：
      · 正在跑的那块 ⇒ 它的结束时刻
      · 两块之间 ⇒ 下一块的结束时刻
    规划里没有可跟的边界（没有快照 / 全是过去的块 / 块的时间不合法）⇒ 回落到锚点。
    """
    now = datetime.now()
    if getattr(args, "after", None):
        try:
            now = datetime.strptime(str(args.after), "%Y-%m-%d %H:%M")
        except ValueError:
            raise SystemExit("--after 需要 YYYY-MM-DD HH:MM（例如 2026-10-15 15:00）")

    from core.datastore import plans as _plans
    snap = _plans.latest() or {}
    slots = (snap.get("plan") or {}).get("slots") or []
    rows, bad = _slot_rows(slots)
    today = now.strftime("%Y-%m-%d")
    now_s = now.strftime("%Y-%m-%d %H:%M")

    ends, starts, passed = [], [], []
    for d, a, b, task in rows:
        if d < today:
            continue                       # 过去的块不再当边界（它们改不了了）
        block = {"date": d, "time": f"{a}-{b}", "task": task}
        if f"{d} {b}" > now_s:
            ends.append((f"{d} {b}", block))
        else:
            passed.append((f"{d} {b}", block))   # 已经结束的块（到点那轮要报「上一块」）
        if f"{d} {a}" > now_s:
            starts.append((f"{d} {a}", block))
    ends.sort(key=lambda x: x[0])
    starts.sort(key=lambda x: x[0])
    passed.sort(key=lambda x: x[0])
    just_ended = passed[-1][1] if passed else None

    # ★ 提醒也是"到点该叫醒"的时刻（用户 2026-11-08）：它**独立于规划** ——
    #   即使规划里没有块、或块离得远，提醒也要准时把这一轮叫起来（管道会直接投递那句话）。
    _rm = [r for r in _reminder_view(now, days=7) if r.get("at", "") > now_s]
    _rm_at = _rm[0]["at"] if _rm else ""

    if (_rm_at and (not ends or _rm_at < ends[0][0])):
        return {"ok": True, "now": now_s, "next_at": _rm_at, "kind": "reminder",
                "why": "提醒「%s」%s" % (_rm[0]["text"][:24], _rm[0]["when_in"]),
                "block": None, "reminder": _rm[0],
                "next_start_at": starts[0][0] if starts else None,
                "next_start_block": starts[0][1] if starts else None,
                "just_ended": just_ended,
                "fallback": False, "slots_total": len(rows), "bad_slots": len(bad),
                "snapshot_at": str(snap.get("made_at") or snap.get("created_at") or ""),
                "note": "控制层拿 next_at 安排下一次唤醒；提醒到点由管道直接投递"}

    if ends:
        at, block = ends[0]
        return {"ok": True, "now": now_s, "next_at": at, "kind": "block_end",
                "why": f"下一块 {block['time']}「{block['task']}」结束",
                "block": block,
                "next_start_at": starts[0][0] if starts else None,
                "next_start_block": starts[0][1] if starts else None,
                "just_ended": just_ended,
                "fallback": False, "slots_total": len(rows), "bad_slots": len(bad),
                "snapshot_at": str(snap.get("made_at") or snap.get("created_at") or ""),
                "note": "控制层拿 next_at 安排下一次唤醒；到点做什么由 Agent 决定"}

    at = f"{(now + timedelta(days=1)).strftime('%Y-%m-%d')} {getattr(args, 'anchor', ANCHOR_HHMM)}"
    return {"ok": True, "now": now_s, "next_at": at, "kind": "anchor",
            "why": ("规划里没有可跟的边界" if not rows else "规划里的块都已经过去了")
                   + f" ⇒ 回落到锚点 {at[-5:]}",
            "block": None, "next_start_at": None, "next_start_block": None,
            "just_ended": just_ended,
            "fallback": True, "slots_total": len(rows), "bad_slots": len(bad),
            "snapshot_at": str(snap.get("made_at") or snap.get("created_at") or ""),
            "note": "控制层拿 next_at 安排下一次唤醒；到点做什么由 Agent 决定"}


def cmd_brief(args) -> dict:
    """把三种输出**排好版**给出来 —— 纯文本，直接发给用户。

    ★ 只做**排版**（确定性）：谁在前谁在后、哪几行、什么分隔符，都由这里定。
      语义仍归 Agent：它拿到 `text` 后**只补一行** `- 建议：…`（见返回值里的 `note`）。

    kind: plan（规划·不含课）/ schedule（日程·含课）/ todo（待办两张表）
    """
    kind = getattr(args, "kind", "plan")
    if kind not in ("plan", "schedule", "todo"):
        raise SystemExit("brief --kind 只能是 plan / schedule / todo")
    # ★ `--days` **不再静默改写**（原先 `max(1, days or 3)`：`0→3`、负数`→1`，
    #   模型以为自己缩小了窗口，其实拿到的是别的天数）。非法值直接报错，
    #   并在返回值里**回显生效天数**。
    brief_days = int(getattr(args, "days", 3))
    if brief_days < 1:
        raise SystemExit(f"brief --days 必须 ≥ 1（收到 {brief_days}）—— "
                         f"内核不把非法值悄悄改成默认值，请显式给一个 ≥1 的天数")
    dat = _load(DATA_FILE, validate_data)
    sched = _load(SCHEDULE_FILE, validate_schedule)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    periods = sched.get("periods", {})
    # ★ 只取**非课次**的活跃注册，并用 `_list_view`（它按紧迫度排好）——
    #   课次条目在 `active` 里也存在（`session_key`），待办不该列课。
    active = _list_view([d for d in _active(dat) if not d.get("session_key")], now=now)
    plan = _plans_of(dat).get("slots") or []

    def gist_of(task: str) -> str:
        return next((d.get("gist", "") for d in active if d["task"] == task), "")

    def slot_blocks(d: str) -> list[tuple[str, list[str]]]:
        """[(开始时刻, [任务行, 要点行…])] —— 成块返回，要点才不会被排到别处。"""
        out = []
        for s in [x for x in plan if x["date"] == d]:
            blk = [f"- {s['time']} {s['task']}"]
            if gist_of(s["task"]):
                blk.append(f"- → {gist_of(s['task'])}")
            out.append((s["time"].split("-")[0], blk))
        return out

    def class_blocks(d: str) -> list[tuple[str, list[str]]]:
        out = []
        for c in _visible_sessions(sched):        # 停课不算课（唯一口径）
            if c.get("date") != d:
                continue
            tm = session_time_of(c, periods)
            if not tm:
                continue
            loc = f" · {c['location']}" if c.get("location") else ""
            mark = "（课·水）" if _is_water(sched, c) else "（课）"
            out.append((tm.split("-")[0], [f"- {tm} {c['course']}{mark}{loc}"]))
        return out

    # ★ **把关**：`brief` 只排版**已存的快照** —— 所以必须自己检查那份快照还成不成立。
    #   快照过期、或 slots 违反规则 ⇒ **不给 text**（只给 problems），
    #   逼出"重排 → 存快照 → 再 brief"这条链（用户看到的东西必须是干净的）。
    unscheduled: list = []          # 漏排（`plan`/`schedule` 在把关段里算；`todo` 不涉及）
    if kind in ("plan", "schedule"):
        problems, warns = [], []
        from core.datastore import plans as _plans2
        snap = _plans2.latest() or {}

        # ★★ 循环闸：最近 30 分钟已存 ≥3 份快照 ⇒ 你已经在反复重排了，停下报告
        _now = datetime.now()
        _recent = 0
        for _h in _plans2.history(limit=20):
            try:
                if (_now - datetime.strptime(_h.get("created_at", ""),
                                             "%Y-%m-%d %H:%M:%S")).total_seconds() < 1800:
                    _recent += 1
            except (ValueError, TypeError):
                continue
        _created = str(snap.get("created_at") or snap.get("made_at") or "")
        _upd = str((dat.get("meta") or {}).get("last_updated") or "")

        def _ts(x):
            for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(x, f)
                except (ValueError, TypeError):
                    continue
            return None

        _c, _u = _ts(_created), _ts(_upd)
        if _c and _u and _u > _c:
            problems.append({"kind": "stale", "created_at": _created, "updated_at": _upd,
                             "note": "注册表在快照之后变过 —— 先重排再 brief"})

        rules = _load(RULES_FILE, validate_rules)
        rows = []
        rows, bad = _slot_rows(plan)
        for x in bad:
            problems.append(x)          # ★ 畸形 slot 进 problems（不给正文），不再静默放行或崩溃
        # ★ 算不出时刻的课次 ⇒ 撞课/课间**没比**（唯一实现在 `_unknown_class_time`，
        #   与 review 同一份）—— 只警告（见 `_anomalies` 的 session_without_time）
        warns += _unknown_class_time(sched, {d for d, _, _, _ in rows})
        for d, a, b, task in rows:                       # 撞课
            for c in _visible_sessions(sched):           # 停课不算课（唯一口径）
                if c.get("date") != d:
                    continue
                ca, cb = session_window_of(c, periods)
                cw = _span(f"{ca}-{cb}") if (ca and cb) else None
                sw = _span(f"{a}-{b}")
                if not (cw and sw and sw[0] < cw[1] and cw[0] < sw[1]):
                    continue
                if _is_water(sched, c):
                    warns.append({"kind": "water_class", "date": d, "slot": f"{a}-{b}",
                                  "task": task, "course": c.get("course", ""),
                                  "note": "水课时段可用但不建议：只排不费脑的活"})
                else:
                    problems.append({"kind": "clashes_class", "date": d, "slot": f"{a}-{b}",
                                     "task": task, "course": c.get("course", "")})
        for i in range(len(rows)):                       # 自重叠
            for j in range(i + 1, len(rows)):
                if rows[i][0] == rows[j][0] and rows[i][1] < rows[j][2] and rows[j][1] < rows[i][2]:
                    problems.append({"kind": "overlap", "date": rows[i][0],
                                     "a": f"{rows[i][1]}-{rows[i][2]} {rows[i][3]}",
                                     "b": f"{rows[j][1]}-{rows[j][2]} {rows[j][3]}"})
        # 用户明确同意过的豁免（写在快照正文里）：`allow: ["rule-004"]`
        # ★ 只对 **preference（警告）** 生效；`constraint`（禁止）**没有豁免**。
        allow = set((snap.get("plan") or {}).get("allow") or [])
        # ★ `allow` 指向不存在的规则 ⇒ **报出来**（原先静默忽略：拼错 id 的"用户已同意"
        #   看着像生效了，其实那条规则根本没被豁免）。
        known_rule_ids = {r.get("id") for r in rules.get("planning_rules", []) if r.get("id")}
        bad_allow = sorted(allow - known_rule_ids)
        if bad_allow:
            warns.append({"kind": "allow_unknown_rule", "allow": bad_allow,
                          "known": sorted(known_rule_ids),
                          "note": "快照里的 allow 指向不存在的规则 id —— 这个豁免不会生效"
                                  "（id 拼错？或规则已删）"})
        # 规则窗口：`no_slots`（固定时刻）+ `before_first_class`（相对当天课表）
        # —— **唯一实现**在 `_window_hits`，`review` 走同一份（同一规则不许有两份实现）
        for h in _window_hits(rules, sched, rows):
            if h["rule_kind"] == "constraint":
                problems.append(h)                     # 禁止：阻塞，无豁免
            elif h["rule"] not in allow:
                warns.append(h)                        # 警告：不阻塞（豁免则不报）
        # ②c 块质量（**警告**，不是禁止）：太短的块、课间块
        for d, a, b, task in rows:
            # 形状检查已经在 _slot_rows 里做过了（坏形状进 problems，这里只会拿到合法的 a/b）
            # ★ 课间禁排（硬约束）：唯一实现在 `_class_gap`。
            #   这里原先拿**同一节课**当"前一节"和"后一节"（要求 a >= 课末 且 课首 >= b，
            #   而课末 > 课首）⇒ 条件恒不成立、是死代码 —— "硬约束"只在 review 一侧成立，
            #   课间块照样发到用户手上（2026-10-15 实测）。
            #   另外：刻意**不检查**"块太短"（用户明确说十几分钟的活可以单列）。
            g = _class_gap(d, a, b, sched, periods)
            if g:
                problems.append({"kind": "in_class_gap", "date": d, "slot": f"{a}-{b}",
                                 "task": task, "gap_minutes": g["gap_minutes"],
                                 "between": g["between"],
                                 "note": "课间禁排（改教室/转场）：整块要落在同一个空档里"})

        # ③ 漏排 —— **与 `review` 同一个函数**（`_unscheduled`，同一判据只有一份）。
        #   这里原先**没有这一步**：实测快照漏掉当天有 DDL 的任务时，review 报
        #   unscheduled=['DUE_TODAY']，而 brief `ok=true` 直接把正文发出去 ——
        #   **最后一道闸放过了最典型的"排漏"**，用户拿到的规划里没有今天要交的东西。
        #   阻塞口径：只有**今天到期**的漏排进 problems（远期 DDL 天然不在本周计划里，
        #   如实报出来即可，不能因此拦下正文）；完整清单始终在 `unscheduled` 字段里。
        unscheduled = _unscheduled(dat, {task for _, _, _, task in rows}, now=now)
        for u in unscheduled:
            if u.get("date") == today:
                problems.append({"kind": "unscheduled", "task": u["task"], "date": u["date"],
                                 "note": "今天有 DDL 却不在这份规划的任何时段里 —— "
                                         "先重排把它排进去，再存快照重发"})

        # ③b 排在自己的硬截止之后（**等于没排**）—— 用户 2026-10-15 报的真问题：
        #     "存在硬截止、但是排不开的情况，应该醒目地给一个报告"。
        #     与 review 共用同一个函数（判据只有一份）。
        _late_hard, _late_soft = _past_deadline(rows, dat, now=now)
        problems.extend(_late_hard)
        warns.extend(_late_soft)

        # ★ 循环闸：放在 problems 之后判 —— 否则"规划其实完全成立、只是用户连提了三次修改"
        #   也会被要求"报排不下"（谎报）。有阻塞才拦；没阻塞就正常给正文 + 一句提示。
        if _recent >= 3 and problems:
            return {"ok": False, "kind": "loop", "recent_snapshots": _recent,
                    "problems": problems[:20], "warnings": warns[:20],
                    "unscheduled": unscheduled[:20],
                    "note": ("★ 最近 30 分钟已存 " + str(_recent) + " 份快照，而这份还有**阻塞**项 —— "
                             "停下：把 problems 里卡住的地方告诉用户，别再改 slots、别再存快照。")}
        if _recent >= 3:
            warns.append({"kind": "loop", "recent_snapshots": _recent,
                          "note": "最近 30 分钟已存 " + str(_recent) + " 份快照（反复重排）—— "
                                  "这次照发，但别再重排了；有不满意的地方问用户要一句话结论。"})
        if problems:
            return {"ok": False, "kind": kind, "problems": problems[:20],
                    "warnings": warns[:20], "unscheduled": unscheduled[:20],
                    "note": ("★ 有**禁止**类的违反（problems）—— 先重排（改 slots）存新快照，"
                             "再调一次 brief。**不要**把 problems 直接发给用户。"
                             "warnings 是软偏好：用户明确同意过的（快照里的 `allow`）已跳过。")}

    if kind in ("plan", "schedule"):
        head = f"{'日程' if kind == 'schedule' else '规划'} · {_d_label(today)} {now:%H:%M}"
        L = [head, "", "————————————", "现在做什么", "————————————", ""]
        cur = next((s for s in plan if s["date"] == today
                    and s["time"].split("-")[0] <= now.strftime("%H:%M")
                    <= s["time"].split("-")[-1]), None)
        nxt = next((s for s in sorted(plan, key=lambda x: (x["date"], x["time"]))
                    if (s["date"], s["time"]) > (today, now.strftime("%H:%M"))), None)
        L.append(f"- 现在 {now:%H:%M} · " + (f"正在 {cur['time']} {cur['task']}" if cur
                                             else f"没有安排（{_now_view(now)['phase']}）"))
        if nxt:
            L.append(f"- 下一块 {_d_label(nxt['date'])} {nxt['time']} {nxt['task']}")
            if gist_of(nxt["task"]):
                L.append(f"- → {gist_of(nxt['task'])}")
        if active:
            L.append(f"- 理由：{active[0]['task']} 最近 —— "
                     f"{_urgency(active[0], now)[2]}")
        L += [""]
        # ★ 日子集合也走 `_visible_sessions`：停课的日期不该出现在日程里
        days = sorted({s["date"] for s in plan} |
                      ({s["date"] for s in _visible_sessions(sched)}
                       if kind == "schedule" else set()))
        _shown, _rest = 0, []
        for d in days:
            if d < today:
                continue
            blocks = (class_blocks(d) + slot_blocks(d)) if kind == "schedule" \
                else slot_blocks(d)
            body = [ln for _, blk in sorted(blocks, key=lambda x: x[0]) for ln in blk]
            if not body:
                continue
            if _shown >= brief_days:
                _rest.append(d)
                continue
            _shown += 1
            label = f"今天 {_d_label(d)}" if d == today else _d_label(d)
            L += ["————————————", label, "————————————", ""] + body + [""]
        if _rest:
            L.append("（后面还有 %d 天有安排，最近 %s；要看更远就说一声）"
                     % (len(_rest), _d_label(_rest[0])))
            L += [""]
        text = "\n".join(L).rstrip() + "\n"

    else:      # todo
        rows = sorted(active, key=lambda x: (x.get("date") or "9999"))
        L = [f"待办 A · 按截止日期（{len(rows)} 件）", ""]
        seen = None
        for x in rows:
            g = x.get("date") or "无截止"
            if g != seen:
                seen = g
                L += [f"—— {_d_label(g)} ——" if g != "无截止" else "—— 无截止 ——", ""]
            due = _due_short(x.get("due_time"))
            L.append(x["task"] + (f" · {due}" if due else ""))
            if x.get("gist"):
                L.append(f"→ {x['gist']}")
            L.append("")
        L += ["", f"待办 B · 按紧迫度（{len(active)} 件）", ""]
        for x in active:
            tag = "[急] " if x.get("urgency") == "urgent" else ""
            L.append(f"- {tag}{x['task']} · {x.get('why', '')}")
        text = "\n".join(L).rstrip() + "\n"

    return {"ok": True, "kind": kind, "text": text,
            # ★ 回显**生效天数**（`--days` 非法时上面已经报错，不再静默改写）
            "days": brief_days,
            "warnings": (warns if kind in ("plan", "schedule") else []),
            # ★ 漏排：与 `review` **同一个函数**、同一形状（远期 DDL 的不阻塞）
            "unscheduled": unscheduled,
            "note": ("★ 若 `warnings` 非空：那是**软偏好**被碰（不阻塞）—— 要么重排避开，"
                     "要么在用户**明确同意**后把 `allow: [规则 id]` 写进快照正文再重存。"
                     "★ 若 `unscheduled` 非空：那是**有 DDL 却没有时段**的条目 ——"
                     "今天到期的已经在存快照时就拦下了（`ok=false`），远期的是提示，不阻塞。"
                     "★ 这是**排好版**的正文，直接发给用户（纯文本，微信能显示）。"
                     "你只需在「现在做什么」块里补一行 `- 建议：…`（你自己的下一步主张），"
                     "**不要改动其余行、不要自己重排、不要往规划里加课**"
                     "（要含课就用 kind=schedule）。")}


def _plans_of(dat: dict) -> dict:
    """最近一份规划的**正文**（快照是 {made_at, triggered_by, plan:{…}}，要取里层）。"""
    from core.datastore import plans
    p = plans.latest() or {}
    return p.get("plan") if isinstance(p.get("plan"), dict) else {}


# ★ **写命令**（会改数据文件的）。它们要拿内核写锁；只读命令不拿。
WRITE_COMMANDS = frozenset({"sync", "apply", "mark", "plan-snapshot", "reminder-fired"})


def _write_lock(timeout: float = 3.0):
    """写命令的**互斥锁** —— 返回已加锁的文件对象（调用方 close 即释放）。

    ★ 与整轮锁 `logs/agent-run.lock` 是**两把不同的锁**，不能合并：
      `外部调度入口` 用 `exec 9>` 持有一整轮，Agent 在**这一轮里**调内核写；
      内核若去拿同一把锁 ⇒ **等自己**（死锁）。两把锁管两件事：
        · 整轮锁：同一时刻只跑一个 Agent 轮（跨通道：墙 / 微信 / 定时）
        · 写锁  ：同一时刻只有一个进程改数据文件
      另一个 agent 建议的"内核写入口也用同一把 flock"方向对、但**粒度选错了**，
      已在 `AGENT/README.md` 里写清为什么。

    ★ 为什么需要：`apply` 是"读 → 校验 → 写"的**读-改-写**。内核落盘是原子的（临时文件 +
      rename），但挡不住 A 读到旧值、B 写完、A 再把旧值+自己的改动写回去 ⇒ **丢更新**。
      实测过的场景：墙上的对话与手工 `qyn.py apply` 同时发生。

    ★ 行为：非阻塞地试，最多等 `timeout` 秒；仍拿不到就**报错退出**（让模型重试），
      绝不在锁被占时"照写不误"。

    ★ 跨平台：内核在 Linux（真机）与 Windows（开发机）上都跑 —— POSIX 用 `fcntl.flock`，
      Windows 用 `msvcrt.locking`（`import fcntl` 在 Windows 上直接 ModuleNotFoundError，实测）。
    """
    import time as _time

    base = os.path.dirname(os.path.realpath(DATA_FILE))      # …/agent/data
    path = os.path.abspath(os.path.join(base, os.pardir, "logs", "qyn-write.lock"))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    fh = open(path, "a+")
    deadline = _time.time() + max(0.0, timeout)
    while True:
        if _try_flock(fh):
            return fh
        if _time.time() >= deadline:
            fh.close()
            raise SystemExit(
                '另一个进程正在写数据（%s 已被占用），等了 %g 秒仍拿不到锁 —— 稍后重试。'
                % (path, timeout))
        _time.sleep(0.1)


def _try_flock(fh) -> bool:
    """非阻塞加排他锁。拿到 True；被占 False。跨平台（见 _write_lock 的说明）。"""
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unflock(fh) -> None:
    """释放 _try_flock 拿到的锁（失败也不抛 —— 进程退出时内核会释放）。"""
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


COMMANDS = {
    "context": cmd_context,
    "sessions": cmd_sessions,
    "sync": cmd_sync,
    "apply": cmd_apply,
    "show": cmd_show,
    "history": cmd_history,
    "anomalies": cmd_anomalies,
    "validate": cmd_validate,
    "mark": cmd_mark,
    "plan-snapshot": cmd_plan_snapshot,
    "review": cmd_review,
    "brief": cmd_brief,
    "wall": cmd_wall,
    "next-tick": cmd_next_tick,
    "reminders": cmd_reminders,
    "reminder-fired": cmd_reminder_fired,
    "week": cmd_week,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lock = _write_lock() if args.command in WRITE_COMMANDS else None
    try:
        print(json.dumps(COMMANDS[args.command](args), ensure_ascii=False, indent=2))
        return 0
    except SystemExit as e:
        # ★ 形状错误（"slots[0] 需要 date 与 time"）是最常见的一类；直接透传裸字符串
        #   会让插件报成"内核输出不是 JSON"，模型就去怀疑解释器而不是改 slots。
        if isinstance(e.code, str) and e.code.strip():
            # ★ stderr —— 与 cmd_apply 同一约定，插件按 stderr 解析结构化错误。
            #   有些错误（mark 的 known_tasks、apply 的已知键清单）**本来就是 JSON 字符串**，
            #   那种原样透传，别再包一层（否则模型读到的是被转义的 JSON 文本）。
            txt = e.code.strip()
            try:
                json.loads(txt)
                print(txt, file=sys.stderr)
            except (ValueError, TypeError):
                print(json.dumps({"ok": False, "error": txt}, ensure_ascii=False),
                      file=sys.stderr)
            raise SystemExit(1)
        raise
    except Exception as exc:  # noqa: BLE001 —— 把失败也变成结构化 JSON 给模型读
        print(json.dumps({"ok": False, "command": args.command,
                          "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    finally:
        # 释放写锁。只读命令 lock 为 None。
        if lock is not None:
            _unflock(lock)
            try:
                lock.close()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
