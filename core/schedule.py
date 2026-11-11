"""内核的**课表/注册表只读判据**（S0 从 qyn.py 拆出）。

★ 全是纯函数：给定 (sched, data, now) 算出可见课次 / 是否水课 / 停课 / 活跃条目。
  **唯一口径**：`_visible_sessions` 排除停课 —— brief / review / sessions / wall 都走它。
"""
from __future__ import annotations

from datetime import datetime

from core.io import _load
from core.paths import SCHEDULE_FILE
from core.schema import validate_schedule
from core.session import session_key_of
from core.timeaxis import _due_moment


def _is_cancelled(s: dict) -> bool:
    """这节课是不是停课（`kind == "cancelled"`）—— **"什么算停课"只在这里定义一次**。

    ★ 停课的处置有三处：读视图不算课、撞课/课间不比、**写路径不物化** ——
      但判据只有这一份（`_visible_sessions` 与 `cmd_sync` 都用它），
      免得哪天改了一处（比如加个 `"stopped"` 别名）另一处不知道。
    """
    return s.get("kind") == "cancelled"

def _is_water(sched: dict, s: dict) -> bool:
    """这节课算不算**水课** —— **跟着课程**（用户 2026-10-16：「这种明显要跟着课程不是课次」）。

    判据只有一份：课次自己标了 `water` **或**它所属课程在 `schedule.courses` 里标了 `water`。
    按课次标的话，以后课表里每来一次课都得再标一遍（实测：标了 10/22 那次，下次还得标）。

    ★ 水课的含义不变：不需要专注听的课，课上可以拿来做**作业性**任务 —— 排期时当可用时段。
      怎么用（哪些任务能放）仍由 Agent 按 SKILL 判断。
    """
    if s.get("water"):
        return True
    name = str(s.get("course") or "")
    if not name:
        return False
    for c in (sched.get("courses") or []):
        if isinstance(c, dict) and str(c.get("course") or "") == name and c.get("water"):
            return True
    return False

def _visible_sessions(sched: dict) -> list[dict]:
    """**可上课次**（唯一口径）：排除 `kind == "cancelled"` 的停课。

    ★ 为什么必须有它（实测）：`_upcoming` / `_sessions_between` 原先都不看 `kind`，
      而 `brief` 的 `class_blocks` 与 `review` 的撞课段却都排除 cancelled —— 同一件事
      两种口径。于是停掉的课被只读视图当成"下一节"发给模型
      （实测 `next_class = {course:"CANCELLED_CLASS", kind:"cancelled"}`、`class_count=2`、
      `sessions` 也列出它），而 `brief --kind schedule` 里根本没有它 ⇒
      模型可能把停课报成"下一节"。

    停课 = 这节课**不上**：它不是课、不排期、不撞课、不算"下一节"。
    """
    return [s for s in sched.get("dated_sessions", []) if not _is_cancelled(s)]

def _active(dat: dict, today: str = "", now: datetime | None = None) -> list[dict]:
    """**活跃注册**：未终结、且**还没到点**。

    ★ 「注册系统按日期滚转向前」的落点：日期一过就滚出活跃集，对模型**等效隐藏**。
      没有这一步，过期旧条目会永远挤占上下文 —— 实测：一条逾期 50 天的论文
      每次都出现在 context 里。

    ★★ **过点即默认完成**（用户要求："过期的就不要管了，默认完成了"）：
      判定精确到**时刻**，不只是日期 —— 今天 10:10 截止的事，到了晚上就不该再算活跃，
      更不该以"已过 13 小时（仍未处理）"的形式追着模型。**不服**（真正逾期的提醒）
      这个系统里不存在；过点 = 翻篇。

      注意：这里**不改写存储**（`status` 仍是 pending）—— 状态是用户/生命周期的信息，
      内核不替用户宣布"你做完了"。改动只在**视图与判断**层：不进任何默认视图、
      不报逾期、不追问。真要回顾（"上周那件交没交"）用 `history` / `show` 显式取。

    ★ 纯函数、按"现在"计算 —— 不写盘、零副作用，时刻一到自动滚转。
    """
    now = now or datetime.now()
    today = today or now.strftime("%Y-%m-%d")
    out = []
    for d in dat.get("registry", []):
        if d.get("status") != "pending":
            continue
        # ★★ **没有 DDL 的条目，只有"完成 / 取消"能让它消失**（用户定的口径）：
        #   "任务只有在达到 deadline 或者被我宣布完成后才应该被删除" ——
        #   没有 DDL ⇒ 它就一直挂在清单上（排最后，不催不闹），直到用户说做完了。
        #   **分层**：`date` 只装 DDL，Agent 的建议时段在另一张随时可变的表（计划 slots）。
        if not d.get("date"):
            out.append(d)
            continue
        if d.get("date", "") < today:
            continue
        due = _due_moment(d)
        if due is not None and due < now:
            continue                      # ★ 过点 = 翻篇（有 deadline 的才算数）
        out.append(d)
    return out

def _guard_schedule_replace(payload: dict) -> None:
    """整段替换课表时，**不许悄悄删掉今天及以后的课次**（用户 2026-10-16 报的严重问题）。

    实测：导入**新一周**课表（10/19 起）时用了 `dated_sessions` —— 那是整段替换 ⇒
    把本周（10/16–10/18）的课次整批冲掉 ⇒ "今天明明有课，却被认为没课"。

    ★ 只做**机械**判断：哪几条会被删掉、它们的日期是不是今天或以后。语义（要不要导入新课表、
      旧课是不是不用了）仍归 Agent。拦下来是为了让它**换一种写法**，而不是替它决定：
      · 导入新课表 ⇒ `sessions.upsert`（只加不删）
      · 确实要整段替换（含删掉近处课次）⇒ 显式写 `"replace_future": true`
    删除**已过去**的课次不拦（那是历史整理，正常操作）。
    """
    sec = payload.get("schedule")
    if not isinstance(sec, dict):
        return
    force = bool(sec.pop("replace_future", False))      # 消费掉：别把它写进文件
    new = sec.get("dated_sessions")
    if not isinstance(new, list) or force:
        return
    cur = _load(SCHEDULE_FILE, validate_schedule)
    keep = {session_key_of(s) for s in new if isinstance(s, dict)}
    today = datetime.now().strftime("%Y-%m-%d")
    lost = [s for s in cur.get("dated_sessions", [])
            if isinstance(s, dict) and str(s.get("date") or "") >= today
            and session_key_of(s) not in keep]
    if not lost:
        return
    names = "、".join(f"{s.get('date')} {s.get('periods') or s.get('time') or ''} "
                      f"{str(s.get('course') or '')}" for s in lost[:6])
    more = "…" if len(lost) > 6 else ""
    raise SystemExit(
        f"整段替换课表会删掉 {len(lost)} 条**今天及以后**的课次（{names}{more}）。"
        f"导入新课表请改用 sessions.upsert（只加不删）："
        f'{{"schedule": {{"sessions": {{"upsert": [{{"date": "…", "periods": "…", '
        f'"course": "…"}}]}}}}}}；确实要整段替换，就把 "replace_future": true 一起写上。')
