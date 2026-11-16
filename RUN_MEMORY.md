# Run 阶段内存优化契约

本题沿用 CONTRACT.md 的业务值、SDK1 求值规则、目录/任务接口和 SQLite v1 历史库。现有实现正确；本轮仅降低 Jobs.run 的内存，不接入 SDK2，不增加暂停续跑、多工作者或算子。sdk2.py 与 schema_v2.sql 是已有离线材料，不是本题目标。

## 公开交付边界

保持 Catalog、Jobs、sdk1 的公开调用及所有既有合法计划的值或首个业务错误。sdk1.py 保持原字节，作为小规模正确参照；可由 Jobs 调用新增的内部执行实现。原 tests/、legacy_format.py、samples.py、memory_workloads.py、memory_bench.py 和 memory_checks.py 保留。其余必要内部实现和存储适配由交付者选择，不能要求重新 enqueue 旧任务或改绑修订。

业务值依旧是 null、精确整数和递归数组。数组可以被变量重复引用，fold 的初值、累加值和结果都可能是数组。词法遮蔽、顺序、重复元素、null、空数组和 if 的未选分支语义不变。特别是完整 map 源必须在 fold 初值之前完成，add 两边求值完成后才检查加法类型。错误须保留 WarehouseError 的 code/message。

run 要承担持久参数读取、完整求值、输出持久化和自有临时资源清理；不得提前在 enqueue 计算结果，不得在 result 补做求值，不得移至未计量进程。enqueue/result 仍以完整业务值工作，不要求其有界内存。任务一旦 done，关闭并重开即可读取完整结果；重复运行终态任务不再执行。沿用单工作者及现有事务，业务错误发布 failed；其他异常原样传播并回滚为 queued，没有部分可读输出。新增暂存资源在成功、业务错误、普通读写错误和提交前异常退出时均须释放，不改动调用方文件。这里不要求杀进程恢复或持久续算。

允许运行期间完整遍历或重新计算；不要求特定外存格式、索引、块大小或算法。使用有限工作块解决总载荷驻留，不能只把载荷移出 Python 计数而耗尽进程内存。资源阈值仅针对下面的固定大负载，不宣称任意巨大整数、计划或深度都能在该上限内执行；其他合法小规模计划仍须保持原语义。

## 固定负载与独立期望

memory_workloads.py 给出两类固定大小计划，每类有 100000、400000 个三元素子数组。第 i 行为 [i%997, null（i%7=0）否则 -i, 2**70+i]，base=7。

* nested：执行 samples.nested()，每个非空标量 x 输出 2*x+17，null 保持 null，原序及三元素结构不变。
* reduce：先 map 每行的可空整数求和（null 作为 0），再标量 fold 求总和；精确结果是全部非 null 输入整数之和。map 仍须完整先行，不能以融合改变异常顺序。

例如 i=0 行为 [0,null,1180591620717411303424]；nested 对应 [17,null,2361183241434822606865]。两行 reduce 结果为 2361183241434822606849。夹具逐项核对完整输出，不只比较长度、摘要或状态。

## 资源窗口和判据

memory_bench.py 每类、每规模各执行三轮。父进程创建磁盘库、enqueue 并将 head 改为第二修订；每轮另起新进程，执行仍固定第一修订的已入队任务。测量自新进程导入业务模块、打开 Database 之前开始，直至 run 返回并关闭连接；tracemalloc 捕获 Python 分配峰值，resource.getrusage 捕获该进程截至该时刻的峰值 RSS（含解释器启动）。记录固定后才在窗口外重开、result 和逐项检查。禁止把延迟工作藏进关闭或结果读取；测量窗口不含父进程准备。

每一轮 Python 分配峰值均须 ≤32MiB，RSS 均须 ≤96MiB。同类 400000 与 100000 行的三轮 Python 峰值中位数之差须 ≤4MiB。没有新增耗时指标；夹具照实记录 seconds，不能用缓存固定答案或识别样例替代业务执行。

运行：

* python3.11 -B memory_checks.py baseline：保留并运行全部原测试，核对 SDK1 和原测试哈希，执行小型差分、磁盘异常及重开检查，记录未优化大负载；资源超目标只记录，不作为正确基线失败。
* python3.11 -B memory_checks.py target：相同业务验证与负载，额外强制上述资源判据。
* python3.11 -B memory_checks.py semantics：只运行新增差分和普通异常检查。
* python3.11 -B memory_bench.py baseline（或 target）：单独重跑完整资源负载。
* python3.11 -B cli.py demo：演示真实历史修订、嵌套加工和重开结果。

baseline 的 summary.conforms=false 表示优化缺口，不是业务故障。任一业务断言或子进程失败必须查明，不得当作成功的基准数据。正式实测应保留命令、各轮输出和平台信息，不能把作者静态估算当实测。

## 普通异常与说明

memory_checks.py 用固定 SDK1 比较小型计划的完整结果或首错，并检查修订固定、重复引用、数组累加、遮蔽及惰性分支；另在真实磁盘库核验成功、晚到业务错误、SQLite 拒绝终态写入和现有事务钩子提交前抛错。后两种须 queued、无输出、重开重试成功。专用临时根目录只允许留下业务库。

夹具是最低回归，交付者仍须针对自己新增的临时文件、临时表、句柄和异常交接补充验证；说明如何在读取或写入途中注入异常、如何确认没有遗留资源。不能删改原断言来适配实现，也不能把 set_authorizer 的拒绝写入称为物理磁盘故障，或声称已验证进程终止。

交付兼容代码、可重跑独立差分和必要边界验证、MEMORY_RESULTS.md 实测记录及简短数组表示/完成边界/所有权说明。优化后把现有 .pointwise-runtime.json 的 verify 接到 memory_checks.py target；保留 version=1、kind=cli、setup 为 argv 数组列表、verify/demo 为 argv 数组，demo 继续展示真实业务结果。初始清单的 verify 运行 baseline，以便先确认正确未优化版本。
