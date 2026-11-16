# 数组表示、完成边界与所有权

本文件说明 `Jobs.run` 的有界内存实现在**业务语义不变**前提下的内部表示、持久化格式、
完成判定和临时资源所有权。业务契约见 `CONTRACT.md`，资源窗口与固定负载见
`RUN_MEMORY.md`。

## 1. 求值期的值表示

`stream_exec` 中，求值期的一个业务值只可能是以下三种之一：

| 形态 | Python 表示 | 何时出现 |
| --- | --- | --- |
| null | `None` | 常量、参数、算术结果 |
| 整数 | `int`（精确，不截断） | 同上 |
| 小数组 | `list`（元素递归同构） | 元素数 ≤ `SMALL_ELEMENTS`(2048) 且编码字符 ≤ `SMALL_CHARS`(1 MiB) |
| 大数组 | `Spilled` | 超过上一条预算的 map 结果/参数数组 |

`Spilled` 是**不可变、可重复迭代**的完整数组：
`__iter__` 按 `seq` 顺序扫描 TEMP 表，逐块 `json.loads` 出元素；`__len__` 返回元素
个数。它只在同一次 `run` 连接内有效（表是连接私有 TEMP 表）。

这与 `sdk1` 的语义差异只在**表示**，不在可观察行为：

* `add` 仍先求 `left` 再求 `right`，**两边都求完之后**才检查类型；数组（含 `Spilled`）
  参与加法一律 `type`。
* `if` 只求 `test` 和被选分支；`Spilled` 不能作条件（`type`）。
* `map` 先完整求源并检查数组类型；源为 `null` 返回 `null`，源为空返回 `[]`，都不执行
  `body`；否则按顺序对每个元素在新词法层求 `body`，结果元素逐个交给
  `ArrayBuilder`（超预算即落 TEMP）。
* `fold` 先完整求源（若是 `map`，**整个 map 在 `initial` 之前完成**），`null` 直接
  返回而不求 `initial`/`body`；否则先求 `initial`，空数组直接返回它。
* 累加值、初值、结果都可以是数组（含 `Spilled`）；`fold` 不要求交换律/结合律。
* 新词法层用 `dict` 复制并覆盖同名键，外层环境不受影响；任意层都可遮蔽同名变量。
* 未绑定名字是运行期 `unbound`；首个业务错误的 `code`/`message` 与 `sdk1` 完全一致。

`validate` 结构检查完全复用 `sdk1.validate`，因此非法 AST 的拒绝时机与错误文本不变。

## 2. 参数快照的持久化表示

`enqueue` 必须存下"参数在执行和接纳时递归复制"的独立副本。旧格式把整个 JSON 放在
`jobs_v1.parameters`（TEXT）。本实现按快照大小二选一：

| 快照编码长度 | `jobs_v1.parameters` | 参数正文 |
| --- | --- | --- |
| ≤ `SPILL_SNAPSHOT`(1 MiB) | 完整 JSON 文本（与旧格式逐字节一致） | 同左 |
| > 1 MiB | 标记文本 `{"$stored":<字节数>}` | `job_parameters_v1.data` 单行 BLOB |

`job_parameters_v1(job_id INTEGER PRIMARY KEY REFERENCES jobs_v1(id), data BLOB NOT NULL)`
的 `data` 是**记录最后一列**的 BLOB，因此可用 `zeroblob` 原地分配、再经增量
blob 句柄分块写入。`jobs_v1.parameters` 若不是记录末列，SQLite 会把整值物化后再写，
峰值随取值线性增长；这是选择伴生表的唯一原因。

读取时以**伴生行是否存在**为准（而不是标记文本的形状），因此：
* 旧库/旧任务（含 `samples.create_frozen`）仍从 `jobs_v1.parameters` 直接流式读取；
* 计划若真的声明了名为 `$stored` 的参数，小快照仍走普通文本路径并能正确读回。

快照仍在 `enqueue` 的同一事务内写入并校验长度；失败则连同伴生表与行一起回滚，
调用方看不到半条记录。

## 3. 输出结果的持久化表示

`run` 先算出结果值的**精确 JSON 长度**（`encoded_length`，与 `values.dumps` 的紧凑
编码一致），再二选一：

| 输出长度 | `jobs_v1.output` |
| --- | --- |
| ≤ `SMALL_OUTPUT`(1 MiB) | 普通 TEXT，一次写入并回读比对 |
| > 1 MiB | `zeroblob(n)` 原地分配 + 增量 blob 分块写入，行内 `typeof` 为 `blob` |

`jobs_v1.output` 的值字节与 `values.dumps(value)` 完全相同（紧凑、`sort_keys` 对数组
和整数无影响）；`result()` 用 `json.loads`，它同时接受 `str`/`bytes`。`null` 的成功
任务仍存字符串 `null`（与 queued 的 SQL NULL 不同），由 `output IS NOT NULL` 的既有
CHECK 保证。

**完成边界**：只有在完整输出已写入行内、并通过一个**新的只读 blob 句柄**整段回读、
长度与 SHA-256 均校验通过，且 `typeof`/长度复核无误之后，事务才会提交，`state` 才
是 `done`。任一步失败都抛普通异常 → 回滚 → 任务保持 `queued`、无输出。`done` 一旦
提交，关闭并重开即可读取完整结果。

## 4. 临时资源所有权与清理

一次 `run` 用到的临时资源：

| 资源 | 归属 | 生命周期 |
| --- | --- | --- |
| TEMP 分块表 `run_mem_stage_N` | `stream_exec.Session` | 事务结束后 `DROP`，每个退出路径都执行 |
| `PRAGMA temp_store=FILE` | `Session` | 结束时恢复原值 |
| 增量 blob 句柄（读参数/写输出） | 各函数 `try/finally` | 函数返回或抛错前关闭 |
| 输出行内 BLOB 的中间态 | 事务 | 失败随回滚消失，任务仍 `queued` |

清理放在**事务结束之后**（`Jobs.run` 的 `finally`），因为事务回滚会一并撤销 TEMP 表的
`DROP`。成功、业务失败、普通读写异常、提交前异常退出都走同一路径。

`Jobs.enqueue` 的大快照写入期间临时下调 `PRAGMA cache_size`（结束后恢复），避免分块
写入把页缓存堆高。

## 5. 计量边界

`memory_bench` 在子进程中从导入业务模块、打开 `Database` 起计，到 `run` 返回并关闭
连接止；`result()` 与逐项核对在窗口外。实现没有把求值推迟到 `result`、`close` 或另起
进程；`enqueue`/`result` 仍以完整业务值工作，不计入有界窗口。
