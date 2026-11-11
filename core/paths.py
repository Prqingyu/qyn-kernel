"""paths.py — 数据文件路径常量（**无副作用**）

为什么单独一个模块：`core.datastore` 在**导入时**就实例化 `DataStore` 单例，
而 `DataStore.__init__` 会刷新 `meta` 并**写盘**。于是任何只想拿路径的消费者
（例如 `scripts/check_data.py`）一 import 就会改写 `data.json` —— 已实测确认。

更麻烦的是它会**掩盖"数据多久没维护"这个信号**：`meta.last_updated` 被刷成当下，
"数据陈旧"永远检测不出来。所以路径常量必须放在一个不触发任何写入的地方，
让只读消费者不必碰 datastore。

> 结论：判断数据是否陈旧**不能靠 `meta.last_updated`**（任何一次 import 都会刷新它），
> 只能靠业务记录本身（最新的截止日期）。见 `qyn.py` 的 `dormant_data`。
"""
import os

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(AGENT_DIR, "data")
RULES_FILE = os.path.join(DATA_DIR, "rules.json")
SCHEDULE_FILE = os.path.join(DATA_DIR, "schedule.json")
DATA_FILE = os.path.join(DATA_DIR, "data.json")
# 冷存储：已过期的注册与课次搬到这里（滚转只是视图过滤，热文件不搬就会越用越大）
# ★ 现在**按月分文件**：`data/archive/YYYY-MM.json`（按条目的**业务日期**归月）。
#   为什么分：归档是唯一只增不减的东西 —— 单文件会无限膨胀。分月之后
#   ① 每个文件有界；② 查历史时**只读覆盖到的月份**（见 datastore 的 registry_between）。
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")
# 旧的单文件冷存储（迁移用；读得到就说明还没搬完）
ARCHIVE_FILE = os.path.join(DATA_DIR, "archive.json")
# 规划快照：原先在 SQLite（早期 SQLite 实现 的 timeline_plans 表），已搬到文件里
PLANS_FILE = os.path.join(DATA_DIR, "plans.json")
# 提醒（用户 2026-11-08）：「提醒我明天中午取快递」—— **不是任务**，不进注册表、
# 不进规划、不受审查搬动。只认一个时刻，到点由管道确定性投递。
REMINDERS_FILE = os.path.join(DATA_DIR, "reminders.json")
