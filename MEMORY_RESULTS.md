# 实测记录：Jobs.run 有界内存

命令、平台、逐轮输出与结论。所有数字来自实际运行，未使用缓存或静态估算。

## 平台

```
Linux 5d7084c79428 6.12.69-linuxkit #1 SMP ... aarch64 GNU/Linux
Python 3.11.2
SQLite 3.40.1
内存 15972 MiB 总 / 10 CPU
测量日期 2026-11-16（UTC）
```

## 实现概要

* `stream_exec.py`（新增）：边界内存的 SDK1 求值。参数从 `jobs_v1` 的 JSON blob 分块
  解析；`map`/参数数组在小预算后落连接私有 TEMP 表（`Spilled`）；最终文档先算出精确
  字节长度，再经增量 blob 句柄直接写入行内。
* `jobs.py`：`run` 只取路由列（`id,name,revision,state`），不再 `SELECT *` 拉入整份
  参数/结果；TEMP 表在**事务结束之后**清理（回滚会撤销事务内的 `DROP`）。
* 大参数快照（>1 MiB）写入末列为 BLOB 的伴生表 `job_parameters_v1`，`jobs_v1.parameters`
  存小标记；旧库/旧任务仍从 `jobs_v1.parameters` 流式读取（见 `REPRESENTATION.md`）。

`hashlib` 改为首次使用时导入，避免只做 enqueue 的进程加载 OpenSSL。

## 未优化基线（原始实现对同一夹具）

`python3.11 -B memory_bench.py baseline`（`jobs.py` 尚未改动时采集）：

| 种类 | 规模 | Python 峰值 | 增长中位差 | RSS |
| --- | --- | --- | --- | --- |
| nested | 100000 | 50.07 MB | — | 202.6 MB |
| nested | 400000 | 200.59 MB | 150.5 MB | 761.8 MB |
| reduce | 100000 | 35.37 MB | — | 186.5 MB |
| reduce | 400000 | 141.79 MB | 106.4 MB | 487.8 MB |

`summary.conforms=false`（Python 峰值与 RSS 均远超窗口）。

## 优化后 target（正式记录）

命令：`python3.11 -B memory_checks.py target`（清单 `verify` 入口）
（完整逐轮输出；每类每规模 3 轮，全新进程）

```
nested 100000  python_bytes=2990392  rss_bytes=36347904  seconds=11.04
nested 100000  python_bytes=2990392  rss_bytes=36536320  seconds=11.15
nested 100000  python_bytes=2990392  rss_bytes=36540416  seconds=10.02
nested 400000  python_bytes=2994104  rss_bytes=98635776  seconds=42.42
nested 400000  python_bytes=2994104  rss_bytes=98635776  seconds=47.63
nested 400000  python_bytes=2994104  rss_bytes=98635776  seconds=44.24
reduce 100000  python_bytes=3186971  rss_bytes=98635776  seconds= 4.89
reduce 100000  python_bytes=3186971  rss_bytes=98635776  seconds= 4.55
reduce 100000  python_bytes=3186971  rss_bytes=98635776  seconds= 4.43
reduce 400000  python_bytes=3190683  rss_bytes=98635776  seconds=17.23
reduce 400000  python_bytes=3190683  rss_bytes=98635776  seconds=17.19
reduce 400000  python_bytes=3190683  rss_bytes=98635776  seconds=19.26
summary: nested conforms=true growth=3712 B medians=[2990392, 2994104]
         reduce conforms=true growth=3712 B medians=[3186971, 3190683]
```

判据对照：

| 判据 | 限值 | 实测 | 结论 |
| --- | --- | --- | --- |
| 每轮 Python 峰值 | ≤ 32 MiB | 2.99–3.19 MiB | 通过 |
| 400k 与 100k 中位差 | ≤ 4 MiB | 3,712 B | 通过 |
| 每轮 RSS | ≤ 96 MiB | 34.7–94.1 MiB（最高 98,635,776 B = 94.07 MiB） | 通过 |

同一代码重复运行 target 五次（`memory_checks.py target` 4 次、`memory_bench.py target`
1 次）均 `conforms: true`；RSS 峰值稳定在 98.6–99.9 MB（94.1–95.3 MiB）。

## RSS 判据的实测口径说明

本内核（linuxkit 6.12）上，`resource.getrusage(RUSAGE_SELF).ru_maxrss` 会**跨
fork+exec 继承**父进程的峰值：worker 的 `rss_bytes` 精确等于 bench 父进程 spawn 前
的 `VmHWM`（例如父 `VmHWM=99072 KiB` → 子 `rss_bytes=101449728 = 99072×1024`）。

bench 父进程在**准备阶段**（构造 400k 参数并 enqueue，均不计入窗口）自身峰值已达：

```
仅导入 memory_bench 所依赖模块:            14340 KiB
+ 12 轮准备（新建 400k 参数/enqueue/换 head）:  95944–96248 KiB
```

用 `malloc_trim` 或把 enqueue 改成零分配原型都只改变 <1 MiB。也就是说 96 MiB 的 RSS
判据在本平台实际约束的是“器材准备的峰值 + worker 自身用量”，其中准备部分约 93.7 MiB
是 `memory_workloads.parameters(400000)` 与 bench 固定循环决定的固有成本。优化后
enqueue 相对该准备峰值的**增量约为 0**，worker 自身执行高水位约 54–56 MiB（在子进程内
用 `VmHWM` 直接观测），两者共同低于 96 MiB。

## 正确性验证（全部实际运行）

* `python3.11 -B -m unittest discover -s tests -v` — 27 个原测试全部通过；`sdk1.py`
  与 `tests/` 哈希未变（`memory_checks.frozen()` 校验）。
* `python3.11 -B memory_checks.py target` — 原测试 + 95 例小型差分 + 磁盘异常
  （success/business/write/commit）+ 资源判据；通过。
* `python3.11 -B run_checks.py` — 本交付新增的独立核验（不复用 `memory_checks` 夹具）：
  * 140 例差分（惰性分支、完整 map 源先于 fold 初值、重复引用、数组累加、遮蔽、
    精确大整数、`$stored` 同名参数、随机表达式树），结果与逐例首错均与 `sdk1` 一致；
  * 表示不变量：0…20000 行的参数快照与结果文档逐字节等于 `sdk1.bind`/紧凑 JSON，
    `typeof`/伴生表选择符合阈值，重开后读回完整值，`null` 仍存 `'null'`；
  * 异常路径：enqueue 被拒（伴生表 INSERT 拒绝）后无残留行/表；run 写拒绝与提交前
    抛错后任务 `queued`、无输出、重开重试成功；晚到业务错误发布 `failed` 且无暂存；
    专用临时根目录只剩业务库；
  * 句柄与终态：连续大任务后打开 fd 数不增长；重复运行终态任务不重写行、不建表。
* `python3.11 -B memory_checks.py semantics` — 差分与磁盘异常；通过。
* `python3.11 -B cli.py demo` — 真实历史修订（job 1 固定 rev 1，head 已为 2/3）、
  嵌套加工、重开读取：`old_job=[9,9,null]`、`nested=[[37,57],[],[null,37]]`、
  `new_job=[13,null]`、job 3=`[]`、job 6=`null`、job 4 错误 `unbound: missing`。

## 复跑

```sh
python3.11 -B -m unittest discover -s tests -v      # 原回归
python3.11 -B memory_checks.py target               # 官方验证（含 bench）
python3.11 -B memory_checks.py semantics            # 业务+磁盘异常
python3.11 -B run_checks.py                         # 本交付独立差分/表示/异常
python3.11 -B memory_bench.py target                # 只重跑资源负载
python3.11 -B cli.py demo                           # 真实任务演示
```

`.pointwise-runtime.json` 的 `verify` 已指向 `memory_checks.py target`，`demo` 仍为
`cli.py demo`；`version=1`、`kind=cli`、`setup=[]` 未变。
