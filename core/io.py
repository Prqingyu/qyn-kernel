"""内核的**读 / 写 / 指纹**（S0 从 qyn.py 拆出）。

★ 这一层只做"把 JSON 读进来 → 跑校验 → 算身份指纹"，**不含任何业务判断**。
   `_plan_inputs` / `_plan_invalidated` 是"这份计划快照是否还对应现在的数据"的机械判据。
"""
from __future__ import annotations

import hashlib
import json
import os

from core.paths import (ARCHIVE_DIR, ARCHIVE_FILE, DATA_FILE, PLANS_FILE,  # noqa: F401
                        RULES_FILE, SCHEDULE_FILE)

# 条目的身份字段（消歧用）：报告 / 候选清单按这个顺序列出来
DISAMBIGUATORS = ("date", "anchored_to", "occurrence_of", "session_key")


def _load(path: str, validator) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    validator(payload)
    return payload

def _fingerprint(d: dict) -> dict:
    """条目的**身份指纹**（报告 / 候选清单用）：只列身份字段，原样给出来。"""
    out = {"task": d.get("task"), "status": d.get("status")}
    for f in DISAMBIGUATORS:
        if d.get(f):
            out[f] = d[f]
    return out

def _plan_inputs() -> dict:
    """规划的**三个输入文件**的内容指纹（data / schedule / rules）—— 存快照时记，读回时比。

    ★ 为什么不能用 `data.json` 的 `meta.last_updated` 一个时间戳当判据：课表
      （schedule.json）与规则（rules.json）改动时它**不动**，于是"基于旧课表排的规划"
      会被当成新鲜的。实测（2026-10-16）：课表被整段替换冲掉、恢复后
      `apply` 回了 `plan_invalidated: true`，而 `context.last_plan.stale` 仍是 false
      ⇒ Agent 判定"计划没动"⇒ **不重排**，旧规划继续用。
    """
    import hashlib
    out = {}
    for name, path in (("data", DATA_FILE), ("schedule", SCHEDULE_FILE), ("rules", RULES_FILE)):
        try:
            with open(path, "rb") as fh:
                out[name] = hashlib.sha256(fh.read()).hexdigest()[:16]
        except OSError:
            out[name] = ""
    return out

def _plan_invalidated() -> dict:
    """写操作真的改了注册表时，附带的**一条事实**：上一份计划快照已不是当前数据的派生物。

    ★ 只给事实，**不给指令** —— "该重排了、怎么重排"是准则 14 与技能的事。
      内核里塞指令是越界（用户指出后把 `next` 那段话删掉了）。
      只在**真的改了东西**时附带（no-op 的写不该报）。
    """
    return {"plan_invalidated": True}
