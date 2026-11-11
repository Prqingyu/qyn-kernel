"""
check_data.py — 数据合规检查（**人读版**）

用法：
  python scripts/check_data.py            # 只报合规
  python scripts/check_data.py --full     # 合规时顺带打印文件内容

★ 校验逻辑**只有一份**：内核的 `validate_report()`（= `qyn.py validate`）。
  本脚本只负责**排版给人看** —— 以前这里和内核各写了一遍"哪些文件、哪个 validator"，
  改一处就会漂移（功能重合复审第 5 步）。

用途：
  - 手动改过 JSON 后确认是否合规
  - 部署到新设备后确认数据完整
  - 排查"加载失败"
"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ★ 只 import 内核模块（qyn.py 本身零副作用：它只从 core.paths / core.schema /
#   core.session 取东西）。**不要** import core.datastore —— 那会在导入时实例化
#   store 单例并写盘（刷新 meta.last_updated），等于"检查数据"这个动作自己改了数据，
#   还会掩盖"数据陈旧"这个信号。
import qyn  # noqa: E402

FILES = {"rules.json": qyn.RULES_FILE,
         "schedule.json": qyn.SCHEDULE_FILE,
         "data.json": qyn.DATA_FILE,
         "plans.json": qyn.PLANS_FILE,
         "archive/": qyn.ARCHIVE_DIR}          # 冷存储按月分文件：archive/YYYY-MM.json


def main(show_full: bool = False) -> int:
    report = qyn.validate_report()
    ok = report.pop("all_ok")

    for label, result in report.items():
        print(f"== {label} ==")
        path = FILES.get(label, "")
        if path:
            print(f"  文件: {path}")
        if result.startswith("ok"):      # ★ 不能严格比 "ok"：archive 返回 "ok（2 个月：…）"
            print("  [OK] 符合 Schema")
        elif result.startswith("skip"):
            print(f"  [跳过] {result[5:]}")
        else:
            print("  [错误] 不符合 Schema:")
            for line in result.replace("FAIL: ", "").split("\n")[1:]:
                print(f"    {line}")

    if ok and show_full:
        for label, path in FILES.items():
            if not os.path.isfile(path):
                continue
            print(f"\n{'=' * 50}\n{label} 内容\n{'=' * 50}")
            with open(path, encoding="utf-8") as f:
                print(json.dumps(json.load(f), ensure_ascii=False, indent=2))

    print(f"\n{'全部通过' if ok else '存在违规'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(show_full="--full" in sys.argv))
