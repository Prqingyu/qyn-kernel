"""内核的**日期/时刻算术**（S0 从 qyn.py 拆出）。

★ 同一件事只有这一份实现：`HH:MM` 解析、时长、区间、截止时刻、开始时刻、现在视图。
  项目历史上因为"两份实现"翻过车（水课、停课、窗口都有过），所以算术必须集中。
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _hhmm(text: str, last: bool = False) -> tuple[int, int] | None:
    """从自然语言里取一个时刻 HH:MM；取不到返回 None。

    · 默认取**第一个** —— 那是这件事**开始/截止**的那一刻。
      一条规则覆盖三种写法：`'23:59'` / `'上课前（周一 节3-4，10:10 前）'` / `'08:00-09:40'`
    · `last=True` 取**最后一个** —— 时段要的是收尾（`'14:00-16:00'` → 16:00），
      这种场合允许 `24:00`（一天的结束）。
    """
    t = (text or "").replace("：", ":")
    found = None
    for i, ch in enumerate(t):
        if ch != ":" or i == 0 or not t[i - 1].isdigit():
            continue
        j = i - 1
        while j - 1 >= 0 and t[j - 1].isdigit() and i - j < 2:
            j -= 1
        mm = t[i + 1:i + 3]
        if not mm.isdigit():
            continue
        hh, mi = int(t[j:i]), int(mm)
        if 0 <= hh <= (24 if last else 23) and 0 <= mi <= 59:
            if not last:
                return hh, mi
            found = (hh, mi)
    return found

def _mins(x) -> int:
    """`HH:MM` → 一天内的分钟数（时间算术的最小工具；`review` 与 `brief` 共用）。"""
    h, _, m = str(x).partition(":")
    return int(h) * 60 + int(m)

def _span(t) -> tuple[int, int] | None:
    """`HH:MM-HH:MM` → (开始分钟, 结束分钟)；形状不对返回 None。

    ★ 所有重叠判断都该用它按**分钟**比，不要用字符串比 ——
      `"8:00-10:30"`（没补零）与 `"08:00-09:30"` 字符串比较会判成不重叠，静默漏掉撞课。
    """
    a, _, b = str(t or "").partition("-")
    a, b = a.strip(), b.strip()
    if not (a and b):
        return None
    try:
        return _mins(a), _mins(b)
    except (ValueError, TypeError):
        return None

def _due_moment(d: dict):
    """条目的**参考时刻**：日期 + due_time 里的第一个时刻；没有时刻就按当天 23:59。

    ★ 这是"按小时规划"的落点：不给时刻的任务，它的截止就是那一天过完。
    """
    try:
        day = datetime.strptime(d["date"], "%Y-%m-%d")
    except (KeyError, ValueError, TypeError):
        return None
    hm = _hhmm(d.get("due_time", ""))
    return (day.replace(hour=hm[0], minute=hm[1]) if hm
            else day.replace(hour=23, minute=59))

def _fmt_span(hours: float, prefix: str = "还剩 ") -> str:
    """把**剩余**时间说成人话：不到 1 小时说分钟，不到 2 天说小时，再远说天。

    ★ 这里不需要"已过"分支：过点的条目**根本进不了视图**（见 `_active` ——
      "过期的就不要管了，默认完成了"）。以前那句"已过 N 小时（仍未处理）"是旧的
      追问语气的残留，现在是死代码，删掉以免哪天又被当成"该催的事"。
    """
    if hours < 1:
        return f"{prefix}{max(0, int(hours * 60))} 分钟"
    if hours < 48:
        return f"{prefix}{int(hours)} 小时"
    return f"{prefix}{int(hours / 24)} 天"

def _fmt_until(dt, now: datetime) -> str:
    """距某个时刻还有多久（课次用分钟级，更贴切）。档位复用 `_fmt_span`。"""
    if dt is None:
        return ""
    if dt < now:
        return "进行中"
    return _fmt_span((dt - now).total_seconds() / 3600, prefix="") + "后"

def _starts_in(d: dict, now: datetime | None = None) -> tuple:
    """`start`(+`start_time`) 距现在还有多久才能开始 → (人话, 现在能不能开始)。

    ★ 纯算术：只比较日期与时刻，不判断"该不该现在做"（那是规划的事）。
      动机（用户 2026-10-27）："周三开始、周五结束"的任务，没有这个字段时
      Agent 会把 DDL 的日期当开始 —— 现在内核直接告诉它"3 天后才能开始"。
    """
    now = now or datetime.now()
    raw = str(d.get("start") or "")
    if not raw:
        return "", True
    try:
        day = datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return "", True
    delta = (day - now.date()).days
    hm = _hhmm(str(d.get("start_time") or ""))
    start_at = datetime.combine(day, datetime.min.time())
    if hm:
        start_at = start_at.replace(hour=hm[0], minute=hm[1])
    if now >= start_at:
        return "现在就能开始", True
    if delta == 0:
        return "今天 %s 起才能开始" % start_at.strftime("%H:%M"), False
    if delta == 1:
        return "明天才能开始", False
    return "%d 天后才能开始" % delta, False

def _now_view(n: datetime | None = None) -> dict:
    """**现在是几点** —— 每次读取都带上它。

    为什么：规划是**时间相关**的，模型自己"感觉"现在几点会错；而且早上和晚上
    拿到的紧迫度本就不同，它得知道参照点在哪。
    """
    n = n or datetime.now()
    h = n.hour
    phase = ("凌晨" if h < 6 else "早上" if h < 9 else "上午" if h < 12
             else "中午" if h < 14 else "下午" if h < 18 else "晚上" if h < 23
             else "深夜")
    return {
        "date": n.strftime("%Y-%m-%d"),
        "time": n.strftime("%H:%M"),
        "weekday": "一二三四五六日"[n.weekday()],
        "phase": phase,
        "iso": n.astimezone().strftime("%Y-%m-%dT%H:%M%z"),
    }
