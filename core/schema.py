"""
schema.py — 示例课程D的严格定义与校验

三个文件，职责单一：
  rules.json     规则层：全局规划偏好 / 约束（**不再含课表**）
  schedule.json  周期层：作息时间 + **带日期的**每一节课 + 作业/考试周期
  data.json      LIST 层：物化后的待办清单，规划与提醒的**唯一权威**

★ 核心约束（用户明确要求）：**不使用周计算时间**。
  所以这里**没有** ISO_WEEK、WEEK_PATTERN、anchor_week 之类的周概念 ——
  schedule.json 里每一节课都必须带**具体日期**，LIST 里每一条的日期都来自外部给定
  （截图里的日期 / 用户口述 / 日期加减），内核不做任何周推算。
  理由：学校会调休、停课、调课、换教室，"第N周周三"推不出"10月14日"，硬推必错。

★ 但**口语里的"这周/下周/周三"是允许的** —— 区别在于它们是哪一类"周"：
  · **周次**（第N周 / 教学周）＝ 计数系统 ⇒ 禁止：不存字段、不做算术、不接受这类输入
  · **周几**（周三 ＝ 一周里的第三天）＝ 日历属性 ⇒ 允许：`every`+`weekday`（ISO 1-7）
    就是它；内核据它**逐日推进**找出下一个具体日期，从不乘 7
  · **相对指针**（这周/下周/上周）＝ 只活在对话里 ⇒ 由 Agent 在登记那一刻解析成
    **具体日期**才写进来；内核永远收不到"下周"这种词
  一句话：**内核只认 `YYYY-MM-DD`**；把口语翻译成日期是 Agent 的语义工作。

设计原则：
  - 每个字段有明确的类型 / 枚举 / 正则约束
  - additionalProperties: false —— 拒绝未知字段，防止漂移
  - 写入前校验：违规立即抛异常，不静默写入脏数据
"""
import jsonschema
from jsonschema import Draft202012Validator, FormatChecker

# ======================== 公共定义 ========================

# ★ `format: "date"` —— 光有 pattern 只验**形状**，实测收下 `2026-13-45`，
#   随后 `brief --kind todo` 在 `_d_label` 里抛 `ValueError` 直接崩（读命令被脏数据打死）。
#   形状 + 日历合法性都要：`_validate` 传了 `FormatChecker()`（见文件末尾），
#   所以这里必须写 `format`，否则它只是注释。
#   ⚠️ 空串本来就被 pattern 拒掉（`^\d{4}-\d{2}-\d{2}$`），改成 format 不会多拒任何合法值。
ISO_DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$", "format": "date"}
# ★ 曾经有过 `ISO_WEEK = ^\d{4}-W\d{2}$` —— 已删除。**周次不是数据模型的一部分**：
#   档期靠具体日期，周几靠 `weekday`（ISO 1-7）。留着这个模式，迟早有人拿它存"第N周"。
# ★ **结束时刻允许 `24:00`**（= 次日 00:00，一天的结束）：
#   `_hhmm(last=True)` 本来就允许它（见 qyn.py 的 docstring），`_passed_at` 也按
#   "次日 00:00" 算 —— 但这条正则原先只认 `00:00-23:59`，于是用户把睡前窗口写成
#   `23:30-24:00` 时 `validate_rules` 直接拒绝（实测）。口径统一在这里：
#   开始时刻 00:00-23:59，结束时刻 00:00-23:59 **或** 24:00。
#   为什么不做成"两个 pattern 二选一"：一个字段一条正则，读的人一眼看全。
TIME_RANGE = {
    "type": "string",
    "pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]-(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)$",
}
PERIODS = {
    "description": "节次：单节 '3'，连续 '3-4' 或 '5-8'",
    "type": "string",
    "pattern": r"^\d{1,2}(-\d{1,2})?$",
}
# 单个时刻 `HH:MM`（**不是**区间）—— 用于"阈值"这类字段，如 `before_first_class.max_start`。
# ★ 与 `TIME_RANGE` 分开：区间允许结束 `24:00`，而阈值是"一天的某个时刻"，最大 23:59。
TIME_OF_DAY = {
    "type": "string",
    "pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]$",
}
SESSION_KIND = {
    "description": "对应课表 App 的图例：normal=正常上课, moved=调课, cancelled=停课, exam=考试",
    "enum": ["normal", "moved", "cancelled", "exam"],
}
DETAIL = {
    "description": (
        "★ **AGENT 自由填充的结构体**：图上/话里读到的完整信息都放这里 —— "
        "题号、页码、教材版本、提交方式、解题要求、合作规则、来源……"
        "任意嵌套、任意键值，内核**不解释也不校验内容**，只原样存取。"
        "为什么必须有它：把结构化信息硬压成一句 hints 是在浪费模型的智能，"
        "而用户之后会问'做哪几题''有什么额外要求'—— 那些只能从 detail 回答。"
        "读取分两层：`state` 给列表视图（不含 detail，只给 has_detail），"
        "`show --task <名>` 取回完整结构体。"
        "★ 它是**唯一**的自由载荷字段（旧的 `workload` 已并入它）："
        "以前 workload 只收扁平字符串、detail 收嵌套结构，两个字段表达同一件事，"
        "结果同一份信息有两种放法、读取方还要两边都找。现在只有 detail。"
    ),
    "type": "object",
}

PLANNING_RULE = {
    "type": "object",
    "description": (
        "全局时间规划规则。kind 是结构性标签（软/硬两种处理模式），"
        "不是语义枚举；condition/body 用自然语言由 Agent 理解。"
    ),
    "required": ["kind", "condition", "body"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "kind": {
            "description": "preference=软偏好(规划时尽量)，constraint=硬约束(规划时必须遵守)",
            "enum": ["preference", "constraint"],
        },
        "no_slots": {
            "description": (
                "**不排任务的时段**（结构化，供 `qyn_review_plan` 机械校验）："
                "如 `['19:00-23:59', '00:00-06:00']`。"
                "★ 写'某时段不排'这类规则时**必须**填 —— 自然语言的 body 内核不解释，"
                "只有这里能被检查到。格式 `HH:MM-HH:MM`（结束时刻允许 `24:00`）。"
            ),
            "type": "array",
            "items": TIME_RANGE,
        },
        "before_first_class": {
            "description": (
                "**相对当天课表的窗口**（结构化）：`00:00 → 当天第一节课开始` 不排任务"
                "（用户要睡觉）。与 `no_slots` 并列参与校验与分级（preference ⇒ 警告，"
                "constraint ⇒ 阻塞）。\n"
                "★ **只有当天第一节课**（`kind != cancelled`、且节次算得出时刻）的开始时刻 "
                "≤ `max_start`（默认 12:00）时，这条规则**当天才生效** —— 这就是"
                "「当天上午有课」的表达方式，阈值必须显式写出、内核不猜'上午'是什么意思。\n"
                "★ 当天没课 / 只有停课 / 节次查不出时刻 ⇒ **不生效，也不报错**"
                "（只是那天不管用）。\n"
                "★ 与 `no_slots` 的分工：那个是**绝对**固定时段，这个是**随课表每天变**。"
            ),
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "max_start": {
                    "description": (
                        "生效阈值（`HH:MM`，默认 `12:00`）：当天第一节课的开始时刻 ≤ 它，"
                        "规则当天才生效。写 `07:00` 表示'只有清早第一节的课'，"
                        "写 `23:59` 表示'当天有课就生效'。"
                    ),
                    **TIME_OF_DAY,
                },
            },
        },
        "scope": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task": {"type": "string", "description": "只影响该任务，缺省=所有"},
                "course": {"type": "string", "description": "只影响关联该课程的任务"},
            },
        },
        "condition": {
            "type": "string",
            "description": "触发条件（自然语言），如'次日有 09:00 前的课程'",
        },
        "body": {
            "type": "string",
            "description": "规则内容（自然语言），如'当晚任务最晚到 00:00'",
        },
        "source": {"type": "string"},
        "created_at": dict(ISO_DATE),
    },
}

# ======================== 规则层：rules.json ========================

RULES_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Rules Store",
    "description": "规则层：低频变动，用户声明一次长期有效。只放规则，不放课表。",
    "type": "object",
    "required": ["planning_rules"],
    "additionalProperties": False,
    "properties": {
        "planning_rules": {
            "type": "array",
            "description": "全局时间规划偏好与约束",
            "items": PLANNING_RULE,
        },
    },
}

# ======================== 周期层：schedule.json ========================

SCHEDULE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Schedule Store",
    "description": (
        "周期层。★ 只认**具体日期**，不认周次 —— 用户明确要求不用周计算时间。"
        "内容来源：多模态录入（课表截图 / 教务导出）或用户口述。"
    ),
    "type": "object",
    "required": ["semester", "periods", "dated_sessions", "periodic_tasks"],
    "additionalProperties": False,
    "properties": {
        "semester": {
            "description": "学期标识，如 '2026-1'。仅作标记，不参与日期计算。",
            "type": "string",
        },
        "periods": {
            "description": (
                "作息时间表：节次 → 时间区间。**只在课表按节次排版时才有** —— "
                "有些课表直接给时钟时间，那就没有这张表，会话用 time 字段即可。"
            ),
            "type": "object",
            "additionalProperties": TIME_RANGE,
        },
        "dated_sessions": {
            "type": "array",
            "description": (
                "**带日期的**每一节课。一周一份截图 → 录进来的就是这批。"
                "同一 (date, periods|time) 只应有一条；地点每周可不同，以本条为准。"
            ),
            "items": {
                "type": "object",
                # ★ 只要求 date + course，**刻意不假定"节次"这个版式**。
                #   通用图片识别要能收各种课表：按节次排的（periods + 上面的作息表）、
                #   直接给时钟时间的（time）、以及只有课名没时间的（都能记）。
                #   时间字段二选一，二者都给也可以（time 优先）。
                "required": ["date", "course"],
                "anyOf": [{"required": ["periods"]}, {"required": ["time"]}],
                "additionalProperties": False,
                "properties": {
                    "date": ISO_DATE,
                    "periods": PERIODS,
                    "time": TIME_RANGE,
                    "course": {"type": "string", "minLength": 1},
                    "location": {"type": "string"},
                    "kind": SESSION_KIND,
                    "water": {
                        "description": (
                            "**水课**：不需要专注听的课（如示例课程G）。课上可以拿来做"
                            "**作业性**任务 —— 排期时把它当可用时段。"
                            "怎么用（哪些任务能放、放宽顺序）由 Agent 按 SKILL 判断。"
                        ),
                        "type": "boolean",
                    },
                    "note": {"type": "string"},
                },
            },
        },
        "courses": {
            "type": "array",
            "description": (
                "**课程**级属性（与具体哪一次课无关）。目前只有 `water`（水课）——"
                "水课是**课程**的性质：在这里标一次，该课**所有**课次（含以后新加进来的）都算水课。"
                "课次上的 `water` 仍然认（历史数据与个别例外），二者是「或」的关系。\n"
                "★ 用户 2026-10-16：「这种明显要跟着课程不是课次」——"
                "按课次标的话，以后每来一次课都得再标一遍。"
            ),
            "items": {
                "type": "object",
                "required": ["course"],
                "additionalProperties": False,
                "properties": {
                    "course": {"type": "string", "minLength": 1},
                    "water": {
                        "description": "这门课是水课：课上可以做**作业性**任务，排期时当可用时段。",
                        "type": "boolean",
                    },
                    "note": {"type": "string"},
                },
            },
        },
        "periodic_tasks": {
            "type": "array",
            "description": (
                "作业 / 考试等周期任务。**两种模式，二选一**：\n"
                "· `follow_course` —— 日期锚到那门课**当周实际那节课**（日期来自录入的 "
                "dated_sessions，不是算出来的）。适合「上课前交」「当天布置的作业」。\n"
                "· `every` + `weekday` —— **日历周期**：每周几、几点。适合「每周三晚上 23:59 "
                "截止」这类**与上课无关**的规律。内核据此算出**具体日期**，仍然不涉及周次。\n"
                "两者都没给 → 内核**不猜**，明确报出来让 Agent 回询用户。"
            ),
            "items": {
                "type": "object",
                "required": ["task"],
                "additionalProperties": False,
                "properties": {
                    "task": {"type": "string", "minLength": 1},
                    "estimate_minutes": {
                        "description": (
                            "**这类任务每次要预留多久**（分钟）。用户 2026-11-08："
                            "「示例课程F以后预留一小时、预习报告预留半小时」。\n"
                            "它是**规划辅助事实**（用户给的），不是建议时段 —— 物化时"
                            "会原样带到那一条上，排期时按它找足够的整块，"
                            "`qyn_review` 也会把「块比预留时长短」报出来。\n"
                            "**不写** = 没有估计（不要瞎填）。"
                        ),
                        "type": "integer", "minimum": 5, "maximum": 600,
                    },
                    "follow_course": {
                        "description": (
                            "锚定到哪门课：取该课每次 dated_session 的日期作为本次截止日。"
                            "留空表示「没有可锚定的课」，此时内核**不会猜日期**，"
                            "会明确报出来让 Agent 回询用户。"
                        ),
                        "type": "string",
                    },
                    "every": {
                        "description": (
                            "★ **日历周期**（与 follow_course 二选一；都给了就用 follow_course）："
                            "'weekly' 每周 / 'daily' 每天。用户在说**与上课无关的固定规律**"
                            "时用它（「每周三 23:59 交」「每天早上」）。"
                        ),
                        "enum": ["weekly", "daily"],
                    },
                    "weekday": {
                        "description": (
                            "`every='weekly'` 时必填：**周几**。ISO 编号 **1=周一 … 7=周日**"
                            "（周三写 3）。别写 0-6，也别写中文。"
                        ),
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 7,
                    },
                    "offset_days": {
                        "description": "相对锚点日期偏移几天（0=当天，1=次日）。默认 0。",
                        "type": "integer",
                        "minimum": -30,
                        "maximum": 30,
                    },
                    "due_time": {
                        "description": "截止的具体时间（自然语言，如 '23:59' 或 '上课前'）",
                        "type": "string",
                    },
                    "priority": {"enum": ["high", "medium", "low"]},
                    "gist": {
                        "description": (
                            "一行可执行要点（≤60 字）—— 同条目的 gist，物化发生时复制过去。"
                            "规律固定、要点也固定的作业（如每周同一本书的习题）写在这里最省事。"
                        ),
                        "type": "string", "maxLength": 60,
                    },
                    "detail": DETAIL,
                    "notes": {"type": "string"},
                },
            },
        },
    },
}

# ======================== LIST 层：data.json ========================

DATA_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "List Store",
    "description": (
        "LIST 层 = **注册表**：把任务 / 作业 / 提醒 / 课程等一律总结成一个个 JSON 结构体"
        "送进来，作为规划与提醒的唯一权威。"
        "★ 分工：**内核只管它必须管的**（身份、日期、状态、以及课表物化需要的关系字段），"
        "其余**全部交给 AGENT 自由填 `detail`** —— 内核不解释、不校验 detail 的内容，"
        "只原样存取。这样任何种类的事都能进同一个表，不必为每种事改 schema。"
    ),
    "type": "object",
    "required": ["registry", "meta"],
    "additionalProperties": False,
    "properties": {
        "registry": {
            "type": "array",
            "description": (
                "注册表条目。三类：① AGENT 登记的（可带 detail 结构体）；"
                "② 来自课表的**上课条目**（带 session_key / course / location）；"
                "③ 周期任务物化出的条目（带 occurrence_of / anchored_to）。"
            ),
            "items": {
                "type": "object",
                # ★★ **分层示例课程D**（用户定的）：注册表只记**权威事实**，
                #   其中 `date`/`due_time` 是**DDL（硬截止）** —— 有就记，**没有就不记**。
                #   Agent 的**建议时段**不在这里，它是另一张**随时可变**的表
                #   （计划快照的 `slots`，见 plans.json）。
                #   为什么必须这样分（实测踩到的逻辑环）：`date` 原先是**必填**，
                #   于是"没有截止的事"也被逼着填一个日期 —— Agent 顺手把自己排的
                #   建议时段填了进去，内核拿它当死线 ⇒ 紧迫度随时间升高 ⇒
                #   一件本来不急的事**自己浮到清单最前面**。那是"给自己排的活打补丁"，
                #   不是分层。现在：没有 DDL 就不写这两个字段，什么时候做只看建议表。
                "required": ["task", "status"],
                "additionalProperties": False,
                "properties": {
                    "task": {"type": "string", "minLength": 1},
                    "date": {
                        "description": (
                            "**DDL（硬截止）的日期** —— 只来自外部给定：用户说的、截图给的、"
                            "课次推出来的。\n"
                            "★ **没有硬截止就不要写这个字段**（别拿建议时段占位）——"
                            "没写 = 没有 DDL，什么时候做只看计划的 `slots`，"
                            "而且它**不参与紧迫度排序**（排最后）。"
                        ),
                        **ISO_DATE,
                    },
                    "due_time": {
                        "description": "截止/上课的具体时间（自然语言，如'23:59'或'上课前'）",
                        "type": "string",
                    },
                    "start": {
                        "description": (
                            "**最早可以开始的日期**（不是 DDL，是「什么时候才允许开始」）。\n"
                            "★ 用户 2026-10-27 提出：周一登记「周三开始、周五结束」的任务时，"
                            "只有 `date` 一个日期会被当成开始、或者干脆丢掉开始。\n"
                            "什么时候写：材料周三才发、老师周三才布置、实验课做完才能写……\n"
                            "写法：`\"周三开始周五结束\"` ⇒ `start=\"<周三日期>\"` + `date=\"<周五日期>\"`。\n"
                            "**不写** = 现在就能开始（不设下限）。"
                        ),
                        **ISO_DATE,
                    },
                    "start_time": {
                        "description": "开始的具体时刻（自然语言，如'08:00'或'课上布置后'）；只在 start 存在时有意义",
                        "type": "string",
                    },
                    "priority": {
                        "description": (
                            "**可选**：只有用户真的明确标过重要性时才给。"
                            "别默认填 medium —— 那是**编出来的信号**，会掩盖真实的轻重。"
                            "展示用的是内核**算出来**的 `urgency`，你标的重要性已并进它的理由。"
                        ),
                        "enum": ["high", "medium", "low"],
                    },
                    "estimate_minutes": {
                        "description": (
                            "**这件事要预留多久**（分钟）—— 规划辅助事实，由用户给。"
                            "周期任务物化时会自动带上；单次任务也可直接写。"
                            "旧的 `detail.estimated_minutes` 仍然认（兼容），但新数据写这一层。"
                        ),
                        "type": "integer", "minimum": 5, "maximum": 600,
                    },
                    "lead_days": {
                        "description": (
                            "**可选**：这件事需要提前几天准备（如报告/论文/复习）。"
                            "语义由你判断，算术由内核算 —— 它据此把紧迫度调高并写进理由。"
                        ),
                        "type": "integer", "minimum": 0, "maximum": 30,
                    },
                    "gist": {
                        "description": (
                            "★ **一行可执行要点**（≤60 字）：题号 / 页码 / 交法 / 在哪交。"
                            "由 Agent 在**登记时**顺手写，内核只搬运、不生成。\n"
                            "为什么要有它：`detail` 故意不进默认视图（太大），于是"
                            "「现在该做什么」的回答只能给出任务名 —— 用户拿到「示例课程A作业」"
                            "没法直接动手，还得再问一次题号。有了 `gist`，默认视图（`active`）"
                            "**自带**这行要点，不必为每条再取一次 `detail`。\n"
                            "写什么：能直接照做的信息。例：`P93 第 4/5/7/8/10 题，上课交`。"
                            "**不写**背景描述（那是 `detail` 的活）。"
                        ),
                        "type": "string", "maxLength": 60,
                    },
                    "status": {
                        "enum": ["pending", "completed", "expired", "cancelled"]
                    },
                    "detail": DETAIL,
                    # -- 来自课表的条目 --
                    "session_key": {
                        "description": "课表条目的身份：'<date>|<periods>'。同一节课只登记一次。",
                        "type": "string",
                    },
                    "course": {
                        "description": "课表条目：课程名（与 task 一致，便于直接展示）",
                        "type": "string",
                    },
                    "location": {
                        "description": "课表条目：地点。**每周可能不同**，以当周录入为准。",
                        "type": "string",
                    },
                    "kind": SESSION_KIND,
                    # -- 周期任务物化出的条目 --
                    "occurrence_of": {
                        "description": "由哪个周期任务物化而来（填任务名）。",
                        "type": "string",
                    },
                    "anchored_to": {
                        "description": (
                            "★ 这条实例**锚在哪一天**（物化时记下，改期时保持不变）。"
                            "身份（anchored_to）与排期（date）必须分开 —— "
                            "否则改期之后 sync 认不出'这天的已经登记过'，会重复登记。"
                        ),
                        **ISO_DATE,
                    },
                },
            },
        },
        "meta": {
            "type": "object",
            "required": ["last_updated"],
            "additionalProperties": False,
            "properties": {
                # ★ 这里曾经有个 `current_week`（"最后更新时是第几周"，用 isocalendar 算）。
                #   已删除：它没有任何读者，却是**唯一一处周号计算**，也是"拿它去推算周次"
                #   的入口。用户的要求是"程序不以周作为计数方法"—— 那就连标记都不留。
                "last_updated": {
                    "type": "string",
                    # ★ 必须允许**可选秒**：`_touch_meta` 现在写秒，只到分钟的话
                    #   "同一分钟内改了数据"判不出过期（快照带秒、meta 不带秒 ⇒ stale 永远为假）。
                    "pattern": r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?)?$",
                },
            },
        },
    },
}


# ======================== 模板（首次创建 / 重置时用） ========================

# ── 提醒（用户 2026-11-08）──────────────────────────────────────────
# 用户原话：「比如我想提醒明天中午取快递这种东西 带有"提醒我"这样的 不算任务但是算代办的
# 应该独立于基础的系统……但是由于审查系统被扔到下午了」。
# ⇒ 提醒**只有**一个时刻（date + time），没有 DDL、没有时长、没有优先级、
#   不排进 slots、不参与 review 的撞课/课间/整块校验。
#   `text` 是用户自己的话，原样投给他 —— 内核不解释、不改写。
DEFAULT_REMINDER_TIME = "09:00"   # 只说了「明天」没说几点 ⇒ 用这个（内核定，不猜「上午」）
REMINDER_ITEM = {
    "type": "object",
    "required": ["date", "text"],
    "additionalProperties": False,
    "properties": {
        "date": ISO_DATE,
        "time": {
            "description": (
                "提醒的时刻 HH:MM。省略 = "
                + DEFAULT_REMINDER_TIME
                + "（用户只说「明天」没说几点时用这个，别自己编别的）"
            ),
            "type": "string",
            "pattern": r"^\d{1,2}:\d{2}$",
        },
        "text": {
            "description": "用户要提醒的事，用**他自己的话**（例如「取快递」），不改写。",
            "type": "string", "minLength": 1, "maxLength": 200,
        },
        "status": {"enum": ["pending", "done"]},
        "fired_at": {"description": "管道投递成功的时刻（内核写，模型别填）", "type": "string"},
        "created_at": {"type": "string"},
        "note": {"type": "string"},
    },
}
REMINDERS_SCHEMA = {
    "type": "object",
    "required": ["reminders"],
    "additionalProperties": False,
    "properties": {"reminders": {"type": "array", "items": REMINDER_ITEM}},
}
REMINDERS_TEMPLATE = {"reminders": []}

RULES_TEMPLATE = {"planning_rules": []}

SCHEDULE_TEMPLATE = {
    "semester": "",
    "periods": {},
    "courses": [],          # 课程级属性（水课在这里标一次，该课所有课次都算）
    "dated_sessions": [],
    "periodic_tasks": [],
}

DATA_TEMPLATE = {
    "registry": [],
    "meta": {"last_updated": ""},
}


# ======================== 校验入口 ========================

def _validate(instance, schema, label: str):
    """校验数据，违规抛 ValueError 并列出所有错误。

    ★ `format_checker` 必须传：JSON Schema 的 `format` 默认只是**注释**，
      不传 checker 的话 `ISO_DATE` 的 `format: "date"` 形同虚设 ——
      `2026-13-45` 会被收下（实测），随后读命令在日期解析处崩。
    """
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda e: e.path)
    if errors:
        lines = [f"{label} 数据不符合 Schema:"]
        for err in errors:
            path = ".".join(str(p) for p in err.path) or "(root)"
            lines.append(f"  - [{path}] {err.message}")
        raise ValueError("\n".join(lines))
    return instance


# ======================== 冷存储：archive/YYYY-MM.json ========================

ARCHIVE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Archive Month",
    "description": (
        "**冷存储（按月一个文件）**：已过期的注册与课次，从热文件里搬出来。\n"
        "为什么必须有它：日期滚转只是**视图过滤** —— 看不见不等于没占地方，"
        "而 `state` 会把 `dated_sessions` 全量交给模型，于是上下文随使用时间"
        "**线性增长**。搬进冷存储后热文件保持常数大小，`history` 仍查得回来（不丢历史）。\n"
        "为什么**按月分文件**：归档是唯一只增不减的东西 —— 单文件会无限膨胀。"
        "分月之后每个文件有界，而且查历史时**只读覆盖到的月份**。\n"
        "归月依据是条目的**业务日期**（`date` 的前 7 位），不是搬运时间。"
    ),
    "type": "object",
    "required": ["month", "updated", "registry", "sessions"],
    "additionalProperties": False,
    "properties": {
        "month": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}$"},
        "updated": {"type": "string"},
        "registry": {
            "type": "array",
            "items": DATA_SCHEMA["properties"]["registry"]["items"],
        },
        "sessions": {
            "type": "array",
            "items": SCHEDULE_SCHEMA["properties"]["dated_sessions"]["items"],
        },
    },
}

ARCHIVE_TEMPLATE = {"month": "", "updated": "", "registry": [], "sessions": []}


# ======================== 规划快照：plans.json ========================

PLANS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Plan Snapshots",
    "description": (
        "**规划快照**：每次排出来的时间轴存档，供 `last_plan` 读回与 `plan_history` 回溯。"
        "★ 为什么要从 SQLite 搬到这里（原先是 `早期 SQLite 实现` 的 "
        "`timeline_plans` 表）：\n"
        "  · 红线是「文件作为存储」—— 别的状态都在 `AGENT/data/*.json`，快照独独在库里，\n"
        "    既不进版本控制（`*.db` 被 gitignore），也和其余数据不是一回事\n"
        "  · 它**只增不减**（实测已 27 行）—— 而「不能无限膨胀」是这个项目的硬要求；\n"
        "    这里显式收口成**只留最近 N 次**，旧的自然淘汰\n"
        "  · 读它要开 sqlite 连接，而读取命令必须**零副作用**；JSON 读没有这个问题"
    ),
    "type": "object",
    "required": ["plans"],
    "additionalProperties": False,
    "properties": {
        "plans": {
            "type": "array",
            "maxItems": 20,
            "description": "**最新的在最后**；超过上限时从最旧的开始丢。",
            "items": {
                "type": "object",
                "required": ["made_at", "triggered_by", "plan"],
                "additionalProperties": False,
                "properties": {
                    "made_at": {"type": "string", "description": "存下来的时刻（含时区）"},
                    "inputs": {
                        "type": "object",
                        "description": (
                            "存这份快照时，**三个输入文件**的内容指纹（data / schedule / rules）。"
                            "`stale` 靠它判断「这份规划之后输入又变过吗」——"
                            "只改课表或规则时 `data.json` 的 `meta.last_updated` 不会动，"
                            "只看它会把基于旧课表的规划当成新鲜的（2026-10-16 实测踩到）。"
                            "老快照没有这个键 ⇒ 退回时间戳口径。"
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                    "triggered_by": {"type": "string", "description": "为什么排的（自然语言）"},
                    "plan": {"type": "object", "description": "规划正文（Agent 自己定的形状）"},
                },
            },
        },
    },
}

PLANS_TEMPLATE = {"plans": []}


def validate_plans(data) -> dict:
    return _validate(data, PLANS_SCHEMA, "plans.json")


def validate_archive(data, name: str = "archive/YYYY-MM.json") -> dict:
    """校验**一个月**的冷存储文件。`name` 只用于报错信息（分文件后文件名是月名）。"""
    return _validate(data, ARCHIVE_SCHEMA, name)


def validate_rules(data) -> dict:
    return _validate(data, RULES_SCHEMA, "rules.json")


def validate_schedule(data) -> dict:
    return _validate(data, SCHEDULE_SCHEMA, "schedule.json")


def validate_data(data) -> dict:
    return _validate(data, DATA_SCHEMA, "data.json")


def validate_reminders(data) -> dict:
    return _validate(data, REMINDERS_SCHEMA, "reminders.json")
