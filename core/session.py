"""纯几何：一节课的**身份**与**时刻**。零副作用，谁都能 import。

★ 为什么要单独一个模块：这些规则读路径与写路径都要用 ——
  · 读命令（`context` / `sessions`）必须**零副作用**，不能为了算个时间就 import
    `core.datastore`（它在模块级就建好 store，会读盘、可能写盘）
  · 写路径（`core.datastore` 的 `ScheduleStore`）也要同一套规则

  于是同一套规则曾经被实现了两遍（qyn 里一份、datastore 里一份），第 3 步复审时
  发现两处逐行等价 —— 这种重复迟早漂移。提到这里，两边共用。
  （与 `core/paths.py` 同一理由：能被读路径安全 import。）

本模块**只允许** import 标准库。
"""
from __future__ import annotations


def session_key_of(session: dict) -> str:
    """一节课的身份：`<date>|<periods 或 time>`。改期、换地点，身份不变。

    ★ **不假定有"节次"**：不同版式的课表给的东西不同 —— 有节次的用 periods，
    只给时钟时间的用 time。两者都没有时退化到课名（至少不会互相覆盖）。
    """
    # 容错：`date` / `course` 在写入路径由 schema 保证；但**增量改课表**时模型
    # 只给 `{date, periods}` —— 所以这里用 .get，缺了也不炸（同一条公式，一处实现）。
    marker = session.get("periods") or session.get("time") or session.get("course", "")
    return f"{session.get('date', '')}|{marker}"


def session_window_of(session: dict, periods: dict) -> tuple[str, str]:
    """课次的 (开始, 结束) 时刻字符串。

    优先用课表**直接给的** `time`；否则按节次查作息表（`'5-8'` → `('14:20','18:10')`，
    跨节取首节开始、末节结束）。都没有 → `("", "")`：**内核不编时间**。
    """
    t = session.get("time") or ""
    if "-" in t:
        a, _, b = t.partition("-")
        return a.strip(), b.strip()
    p = session.get("periods", "")
    if p:
        first, _, last = p.partition("-")
        a, b = periods.get(first), periods.get(last or first)
        if a and b:
            return a.split("-")[0], b.split("-")[1]
    return "", ""


def session_time_of(session: dict, periods: dict) -> str:
    """课次的时钟区间 `'HH:MM-HH:MM'`；算不出来就空串（**绝不编**）。"""
    start, end = session_window_of(session, periods)
    return f"{start}-{end}" if start and end else ""
