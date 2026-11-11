# AGENT —— 数据工具层（内核）

`qyn.py` 是**唯一**的数据写入口：存 / 算 / 校验 / 滚转都在这里，**不做语义判断**
（要不要提前准备、算不算同一件事 —— 那是 Agent 的事）。

```
AGENT/
  qyn.py              内核（命令行；每轮由插件当工具调用，也可直接跑）
  core/               schema / 存储 / 课表时刻 / 归档
  scripts/            smoke_qyn.py（内核回归）、check_data.py（数据合规）
  dsh/                DSH 插件 qyn-time、演练 rehearsal
```

只读命令：`context` `sessions` `show` `history` `anomalies` `validate` `review` `brief` `wall` `next-tick`
写命令：  `sync` `apply` `mark` `plan-snapshot`

## ★ 锁的契约（两条，别合并）

所以有两把锁，管的是两件事：


`qyn_apply` 等写命令 —— 内核若去拿整轮锁就是**等自己**（死锁）。两种粒度必须分开。

**为什么需要写锁**：`apply` 是「读 → 校验 → 写」的读-改-写。落盘虽然是原子的（临时文件 + rename），
但挡不住 A 读到旧值 → B 写完 → A 再把旧值+自己的改动写回去 ⇒ **丢更新**。

**新增入口必须遵守**：任何**会改数据**的入口，要么走 `qyn.py` 的写命令（自动拿写锁），
要么自己按上面的约定拿写锁。**只读**入口不要拿写锁（会被写操作无谓阻塞）。

> **第四个入口直接调 `qyn.py` 写数据时不会拿任何锁** ⇒ 内核写入口补了上面那把独立的写锁。
> 它建议"内核写入口也用同一把 flock"——方向对，但**粒度选错了**（会死锁），故用独立锁。

## 跑测试

```bash
python scripts/smoke_qyn.py     # 内核回归（只写临时目录，不碰 data/）
python scripts/check_data.py    # 数据合规
```

本仓 `.ps1` 一律带 **UTF-8 BOM**（Windows PowerShell 5.1 没 BOM 会按 GBK 读、整脚本解析失败）。
