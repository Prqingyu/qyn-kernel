"""
datastore.py — 文件持久化

  rules.json     规则层：全局规划偏好/约束（低频，用户声明一次长期有效）
  schedule.json  周期层：作息时间 + **带日期的**每一节课 + 作业/考试周期
  data.json      LIST 层：物化后的待办清单 —— 规划与提醒的**唯一权威**
  plans.json     规划快照（有界：只留最近 N 次）

★ 全部状态都在**文件**里 —— 这是红线。原先规划快照存在 SQLite
  （`早期 SQLite 实现` 的 `timeline_plans` 表），既不进版本控制，
  又只增不减；已搬到 `plans.json`（见 `PlansStore`）。

★ 不做周推算：日期一律来自"外部给定"（截图/口述/显式日期加减）。
  用户明确要求：图片里有日期，不要用周计算时间。
  理由：学校会调休、停课、调课、换教室，"第N周周三"推不出"10月14日"，硬推必错。
"""
import json
import os

from core.paths import (DATA_DIR, RULES_FILE, SCHEDULE_FILE, DATA_FILE, ARCHIVE_FILE, REMINDERS_FILE,
                        ARCHIVE_DIR, PLANS_FILE)
from core.schema import (validate_rules, validate_schedule, validate_data,
                         validate_archive, validate_plans, validate_reminders,
                         RULES_TEMPLATE, SCHEDULE_TEMPLATE, DATA_TEMPLATE,
                         ARCHIVE_TEMPLATE, PLANS_TEMPLATE, REMINDERS_TEMPLATE)
# 纯几何（身份 / 时刻）在 core.session：读路径也要用，而那个模块零副作用。
# 这里转出，让既有 `datastore.session_key_of(...)` 的调用方不用改。
from core.session import (session_key_of as _session_key_of,
                          session_time_of as _session_time_of,
                          session_window_of as _session_window_of)


# ======================== 读写 ========================

def _read_json(path, template, validator):
    """读取 JSON 并做 Schema 校验。文件缺失时按模板创建。"""
    if not os.path.isfile(path):
        _write_json(path, template, validator)
        return template
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # 补充缺失的顶层字段，然后严格校验
    for k, v in template.items():
        if k not in data:
            data[k] = v
    validator(data)
    return data


def _write_json(path, data, validator):
    """写入前严格校验，违规拒绝写入。**先写临时文件再原子替换**。

    ★ 为什么必须原子：四个文件都是"整份重写"，而 `BUSY`/断电/被杀 出现在
      `json.dump` 中途 → 文件被截断成半截 JSON ⇒ **整份内容没了**。
      冷存储最惨（它装着全部历史，一次 sync 就要整体重写一遍）。
      `os.replace` 在同一文件系统上是原子的：要么是旧内容，要么是新内容，
      不存在"半截"。校验放在最前面，所以"不合规就一个字节都不动"这条性质不变。
    """
    validator(data)
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    try:
        # ★ `newline="\n"`：不让平台改写行尾。默认在 Windows 上会把 \n 翻成 \r\n，
        #   于是每次写盘都产生"整份文件都变了"的噪声 diff（内容其实没变）。
        #   统一写 LF，Windows / Linux 两边一致（目标机是 Linux）。
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())      # 落盘再替换，避免"替换成空文件"
        os.replace(tmp, path)
    except BaseException:
        # ★ 失败要**擦干净**：否则每次写失败都留一个 .tmp，攒着本身就是膨胀
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def session_key_of(session: dict) -> str:
    """一节课的身份（实现在 `core.session`，纯模块；这里转出以便既有调用方不变）。"""
    return _session_key_of(session)


# ======================== 规则层 ========================

class RuleStore:
    """规则层：只放规则（课表已移到 schedule.json）。"""

    def __init__(self):
        self._data = _read_json(RULES_FILE, RULES_TEMPLATE, validate_rules)
        self.file_path = RULES_FILE

    def _save(self):
        _write_json(RULES_FILE, self._data, validate_rules)

    @property
    def planning_rules(self) -> list:
        return self._data["planning_rules"]




# ======================== 提醒层 ========================

class ReminderStore:
    """提醒（用户 2026-11-08）：**独立于任务与规划**。

    为什么单独一层：提醒没有 DDL、没有时长、不需要排期 —— 把它塞进注册表就只能
    变成一个"没有截止的永远活跃任务"，规划器必然按课间/整块规则去搬它
    （用户实测：「我想明天中午取快递 但是由于审查系统被扔到下午了」）。
    所以：一个时刻 + 一句话，到点由管道投递，投完记 fired_at。
    """

    def __init__(self):
        self._data = _read_json(REMINDERS_FILE, REMINDERS_TEMPLATE, validate_reminders)
        self.file_path = REMINDERS_FILE

    def _save(self):
        _write_json(REMINDERS_FILE, self._data, validate_reminders)

    @property
    def items(self) -> list:
        return self._data["reminders"]


# ======================== 周期层 ========================

class ScheduleStore:
    """周期层：作息时间 + 带日期的课表 + 作业/考试周期。

    这一层**只认日期**：`dated_sessions` 里每一节课都自带 date。
    一周一份截图录进来，就是新增一批 dated_sessions。
    """

    def __init__(self):
        self._data = _read_json(SCHEDULE_FILE, SCHEDULE_TEMPLATE, validate_schedule)
        self.file_path = SCHEDULE_FILE

    def _save(self):
        _write_json(SCHEDULE_FILE, self._data, validate_schedule)

    # -- 基本视图 --
    @property
    def semester(self) -> str:
        return self._data["semester"]

    @property
    def periods(self) -> dict:
        return self._data["periods"]

    @property
    def dated_sessions(self) -> list:
        return self._data["dated_sessions"]

    @property
    def periodic_tasks(self) -> list:
        return self._data["periodic_tasks"]

    def session_key(self, session: dict) -> str:
        """一节课的身份（见模块级 `session_key_of`）。"""
        return session_key_of(session)

    def session_time(self, session: dict) -> str:
        """这节课的时钟区间（规则见 `core.session.session_time_of`，这里补上作息表）。"""
        return _session_time_of(session, self._data.get("periods", {}))

    # -- 写入 --
    def import_bundle(self, payload: dict) -> dict:
        """**整批导入**：payload 可含 `semester` / `periods` / `dated_sessions`（任选）。

        这是「课表截图 → 结构化 → 落盘」的**通用入口**：不关心截图长什么样、
        来自哪个 App，只要求调用方把内容整理成这几个字段。

        **原子**：先校验整份候选，过了才落盘 —— 校验不过一个字节都不写。
        """
        candidate = dict(self._data)
        for key in ("semester", "periods", "dated_sessions"):
            if key in payload:
                candidate[key] = payload[key]
        validate_schedule(candidate)
        self._data = candidate
        self._save()
        return {"added": len(candidate.get("dated_sessions", [])), "replaced": 0,
                "total": len(candidate.get("dated_sessions", []))}









# ======================== LIST 层 ========================

class DataStore:
    """LIST 层：待办清单。规划与提醒只看这里。"""

    def __init__(self):
        self._data = _read_json(DATA_FILE, DATA_TEMPLATE, validate_data)
        self.file_path = DATA_FILE
        # 只更新**内存里**的 meta；**不要在这里 _save()**。
        #
        # 为什么：`__init__` 一写盘，就等于"实例化 DataStore"这个动作本身有副作用。
        # 而写命令的实现是"先 import datastore，再校验参数" —— 于是
        # **一次被拒绝的操作（比如名称不匹配的 mark）也会改磁盘上的文件**：
        # 内容没变，但 meta.last_updated 被刷成当下，git 里凭空出现 diff。
        # 真实踩到：demo 跑完后 data.json 莫名改动，追查到这里。
        #
        # 不写盘是安全的：任何真正的写操作都会走 _save()，那时 meta 一并落盘。
        self._touch_meta()

    def _touch_meta(self):
        # ★ 这里曾经还写一个 `current_week`（isocalendar 算出的周号）—— 已删除。
        #   它没有任何读者，却是内核里**唯一一处周号计算**，也是"拿它推周次"的入口。
        #   用户的要求是"程序不以周作为计数方法"：那就连标记都不留。
        from datetime import datetime
        # ★ 写**秒**：快照带秒（plans 的 made_at），只到分钟会让"同一分钟内改了数据"
        #   判不出过期（实测：快照 17:22:17 → 17:22:40 改数据 ⇒ stale=False，旧规划照发）。
        self._data["meta"]["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _save(self):
        self._touch_meta()
        _write_json(DATA_FILE, self._data, validate_data)

    @property
    def registry(self) -> list:
        return self._data["registry"]

    def replace_registry(self, entries: list) -> None:
        """整表替换（compact 搬走过期条目后用）。"""
        validate_data({**self._data, "registry": entries})
        self._data["registry"] = entries
        self._save()




    def add_entry(self, entry: dict):
        """新增一条 LIST 条目（entry 需已是 schema 合法形状）。

        先校验整份候选、再赋值：要么整条进去，要么什么都没发生。
        """
        candidate = [*self._data["registry"], entry]
        validate_data({**self._data, "registry": candidate})
        self._data["registry"] = candidate
        self._save()

    def add_registry_entry(self, task: str, date: str, priority: str = "",
                     due_time: str = "", detail: dict | None = None,
                     occurrence_of: str = "", anchored_to: str = "",
                     gist: str = "", estimate_minutes: int | None = None):
        """一次性任务 / 周期任务物化条目的便捷入口。

        ★ `priority` 默认**不写**：随手填 medium 是编出来的信号（会掩盖真实轻重）。
        只有调用方真的给了一个重要性，才把它落到条目上 —— 展示用的是内核算出的 `urgency`。
        ★ `detail` 是自由载荷（大，**不进默认视图**）；`gist` 是**一行可执行要点**
        （题号/页码/交法，≤60 字）—— 它**进默认视图**，所以"现在该做什么"的回答里
        直接带着能照做的信息，不必为每条再取一次 detail。
        """
        entry = {"task": task, "date": date, "status": "pending"}
        if priority:
            entry["priority"] = priority
        if due_time:
            entry["due_time"] = due_time
        if gist:
            entry["gist"] = gist
        if estimate_minutes:
            entry["estimate_minutes"] = int(estimate_minutes)
        if detail:
            entry["detail"] = detail
        if occurrence_of:
            entry["occurrence_of"] = occurrence_of
            entry["anchored_to"] = anchored_to or date
        self.add_entry(entry)

    def add_session_entry(self, session: dict, time_range: str = "",
                          status: str = "pending"):
        """把一节课变成 LIST 里的一条（上课条目）。

        status：**已经过去的课传 "completed"** —— 课都上完了，不该在清单里
        变成"逾期未交"的噪音；留 completed 是为了保留历史可回看。
        time_range：时钟区间；没有就空着（内核不编时间）。
        ★ 不写 `priority`：课次的轻重由 `urgency` 算（按日期与固定时刻），不是填出来的。
        """
        entry = {
            "task": session["course"], "date": session["date"],
            "status": status,
            "session_key": session_key_of(session),
            "course": session["course"],
            "kind": session.get("kind", "normal"),
        }
        if time_range:
            entry["due_time"] = time_range
        if session.get("location"):
            entry["location"] = session["location"]
        self.add_entry(entry)

    # ★ 这里曾有 `set_registry_status(task, status)` —— **已删除**（2026-10-15）。
    #   它的实现是"把所有同名条目一起改"，而它的唯一调用方 `cmd_mark` 却在返回值里
    #   写"本次只改了第一条、其余没有改动" ⇒ 报告与事实相反，而且是静默的。
    #   现在 `cmd_mark` 用"按身份（`--date`）指出唯一一条 + `replace_registry` 落盘"，
    #   同名多条又不给身份时**直接拒绝**（见 `qyn.py` 的 `_resolve_one`）。
    #   留着这个"按名字改全部"的入口，就是给下一个调用方埋雷。



# ======================== 冷存储：按月分文件 ========================
#
#  `data/archive/YYYY-MM.json`，按条目的**业务日期**归月（不是搬运时间）。
#  为什么分文件：归档是唯一只增不减的东西 —— 单文件会无限膨胀。分月之后
#  ① 每个文件有界；② 查历史时**只读覆盖到的月份**（`registry_between`）。
#
#  ★ 全部是模块级函数 + 一个门面对象 `archive`（保持 `from core.datastore import archive`
#    的用法不变）。**没有**"导入即写盘"：文件不存在时读成空，只有 absorb 才落盘。


def _archive_path(month: str) -> str:
    return os.path.join(ARCHIVE_DIR, f"{month}.json")


def _archive_month_of(item: dict) -> str:
    """条目归到哪个月 —— 取**业务日期**的前 7 位；没有日期就归 `unknown`。"""
    d = str(item.get("date") or "")
    return d[:7] if len(d) >= 7 else "unknown"


def _archive_key(kind: str, item: dict) -> str:
    """身份去重键 —— 幂等：重复搬运不会重复累积。"""
    if kind == "sessions":
        marker = item.get("periods") or item.get("time") or ""
        return f"{item['date']}|{marker}|{item['course']}"
    return f"{item['task']}|{item['date']}|{item.get('session_key', '')}"


def _archive_read(month: str) -> dict:
    p = _archive_path(month)
    if os.path.isfile(p):
        return _read_json(p, json.loads(json.dumps(ARCHIVE_TEMPLATE)),   # 深拷贝
                          lambda d: validate_archive(d, f"archive/{month}.json"))
    # ★ 必须**深拷贝**模板：`{**ARCHIVE_TEMPLATE}` 是浅拷贝，里面的**列表会被共享** ——
    #   往里 append 会污染模板，下一次读就"已经有这些条目了"（实测：迁移一条都搬不动）。
    d = json.loads(json.dumps(ARCHIVE_TEMPLATE))
    d["month"] = month
    return d


def archive_months() -> list[str]:
    """已有的月份文件（升序）。目录不存在就是空 —— **不创建**。"""
    if not os.path.isdir(ARCHIVE_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(ARCHIVE_DIR) if f.endswith(".json"))


def _archive_months_spanning(since: str, until: str) -> list[str]:
    """`[since, until)` 覆盖到的月份 —— 查历史只读这些文件。"""
    try:
        y, mo = int(since[:4]), int(since[5:7])
    except (ValueError, TypeError):
        return archive_months()
    end = until[:7]
    out = []
    cur = f"{y:04d}-{mo:02d}"
    while cur <= end and len(out) < 600:          # 上限：50 年，防手滑死循环
        out.append(cur)
        mo += 1
        if mo > 12:
            y, mo = y + 1, 1
        cur = f"{y:04d}-{mo:02d}"
    return out


class _Archive:
    """冷存储门面（保持旧调用点不变：`arch.absorb` / `arch.registry` / `arch.sessions`）。"""

    def absorb(self, sessions: list, registry: list) -> dict:
        """并入冷存储（按月分文件）。返回各加入了几条（已存在的跳过）。

        归月 = 条目的**业务日期**前 7 位。只写**真的有新增**的月份（不凭空造空文件）。
        """
        added = {"sessions": 0, "registry": 0}
        by_month: dict[str, list] = {}
        for kind, items in (("sessions", sessions), ("registry", registry)):
            for x in items:
                by_month.setdefault(_archive_month_of(x), []).append((kind, x))

        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for month, pairs in by_month.items():
            d = _archive_read(month)
            have = {(k, _archive_key(k, x))
                    for k in ("sessions", "registry") for x in d[k]}
            grew = 0
            for kind, x in pairs:
                key = (kind, _archive_key(kind, x))
                if key in have:
                    continue
                have.add(key)
                d[kind].append(x)
                grew += 1
                added[kind] += 1
            if grew:
                d["updated"] = now
                os.makedirs(ARCHIVE_DIR, exist_ok=True)
                _write_json(_archive_path(month), d,
                            lambda dd, m=month: validate_archive(dd, f"archive/{m}.json"))
        return added

    def registry_between(self, since: str, until: str) -> list:
        """`[since, until)` 的归档注册 —— **只读覆盖到的月份**（分文件的意义所在）。"""
        out = []
        for m in _archive_months_spanning(since, until):
            p = _archive_path(m)
            if not os.path.isfile(p):
                continue
            out += [d for d in _archive_read(m)["registry"]
                    if since <= d.get("date", "") < until]
        return out

    def find_registry(self, task: str) -> dict | None:
        """按名字找一条归档注册（新月份优先）。`show` 用它取回已滚出的条目。"""
        for m in reversed(archive_months()):
            for d in _archive_read(m)["registry"]:
                if d.get("task") == task:
                    return d
        return None

    def sessions_between(self, since: str, until: str) -> list:
        """`[since, until)` 的归档课次（校验与用例用）。"""
        out = []
        for m in _archive_months_spanning(since, until):
            p = _archive_path(m)
            if not os.path.isfile(p):
                continue
            out += [d for d in _archive_read(m)["sessions"]
                    if since <= d.get("date", "") < until]
        return out

    @property
    def registry(self) -> list:
        """全部归档注册（不分月份）—— 只在确实要全量时用；区间查询请用 `registry_between`。"""
        return [d for m in archive_months() for d in _archive_read(m)["registry"]]

    @property
    def sessions(self) -> list:
        return [d for m in archive_months() for d in _archive_read(m)["sessions"]]


class PlansStore:
    """规划快照 —— **有界**：只留最近 `KEEP` 次，旧的自然淘汰。

    ★ 为什么从 SQLite 搬到这里（原 `早期 SQLite 实现` 的 `timeline_plans`）：
      · 红线是「文件作为存储」：别的状态都在 `AGENT/data/*.json`，快照独独在库里，
        还因为 `*.db` 被 gitignore 而**不进版本控制**
      · 它**只增不减**（实测已 27 行）—— "不能无限膨胀"是这个项目的硬要求，这里显式收口
      · 读它要开 sqlite 连接，而读取命令必须**零副作用**；JSON 读没这个问题

    惰性读取：文件不存在时**不创建**（导入即写盘是这个项目明确避免的行为）。
    """

    KEEP = 20                                  # 与 PLANS_SCHEMA 的 maxItems 对齐

    def __init__(self, path: str = PLANS_FILE):
        self.path = path
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            if os.path.isfile(self.path):
                self._data = _read_json(self.path, PLANS_TEMPLATE, validate_plans)
            else:
                self._data = json.loads(json.dumps(PLANS_TEMPLATE))
        return self._data

    def snapshot(self, plan: dict, triggered_by: str = "", inputs: dict | None = None) -> dict:
        """存一次规划快照。返回存下来的那条（含 made_at）。

        ★ 时间戳带**秒**：`_plan_view` 靠它判断"这份规划之后数据又变过吗"（stale）。
          原先写成 `%H:%M%z`，而 `datetime.now()` 是 **naive** 的 —— `%z` 输出空串，
          于是精度实际只有**分钟**：同一分钟内"存了快照又改了数据"会被判成没过时。
          而迁移来的老快照是带秒的（`02:54:01`），两套精度混着比更危险。
        """
        from datetime import datetime
        d = self._load()
        item = {"made_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "triggered_by": triggered_by or "", "plan": plan,
                # ★ 三个输入文件的指纹：`stale` 靠它（只改课表/规则时 data.json 的时间戳不动）
                "inputs": inputs or {}}
        d["plans"].append(item)
        if len(d["plans"]) > self.KEEP:        # ★ 有界：从最旧的开始丢
            del d["plans"][:len(d["plans"]) - self.KEEP]
        _write_json(self.path, d, validate_plans)
        return item

    def latest(self) -> dict | None:
        """最近一次快照 —— 形状与旧的 `database.latest_plan()` 一致，方便换掉调用方。"""
        plans = self._load()["plans"]
        if not plans:
            return None
        p = plans[-1]
        return {"plan": p["plan"], "triggered_by": p.get("triggered_by", ""),
                "created_at": p.get("made_at", ""), "inputs": p.get("inputs") or {}}

    def count(self) -> int:
        """当前留了几份（有界，上限 `KEEP`）。"""
        return len(self._load()["plans"])

    def history(self, limit: int = 5) -> list:
        """历史 —— **只给元信息，不给正文**，免得上下文被历史撑爆。"""
        plans = self._load()["plans"]
        return [{"triggered_by": p.get("triggered_by", ""), "created_at": p.get("made_at", "")}
                for p in reversed(plans[-limit:])]


# 全局单例
rules = RuleStore()
schedule = ScheduleStore()
data = DataStore()
plans = PlansStore()
reminders = ReminderStore()
archive = _Archive()
