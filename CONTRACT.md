# 嵌套数组加工库

Python 3.11 标准库组件，为仓内批处理保存嵌套数组加工定义及任务。
本版本正常运行于 SDK1 与 SQLite v1；sdk2.py 是可单独运行的离线新依赖，
尚未接入目录或任务服务。没有计划转换器或存储升级入口。

## 使用

在项目根目录执行：

```sh
python3.11 -B -m unittest discover -s tests -v
python3.11 -B cli.py demo
```

demo 在测试临时目录构造历史库、更新同名计划、执行固定旧修订的任务，
关闭并重开后核对结果；另行运行两个 SDK2 原生端口样例。
它不执行升级。所有示例数据库都由调用者指定或位于 TemporaryDirectory。

```python
from storage import Database
from catalog import Catalog
from jobs import Jobs
from samples import offset

with Database("batch.sqlite") as db:
    catalog, jobs = Catalog(db), Jobs(db)
    revision = catalog.save("daily_offset", offset())
    job = jobs.enqueue("daily_offset", {"rows": [2, None, 2], "base": 7})
    assert jobs.run(job) == "done"
    assert jobs.result(job) == [9, None, 9]
```

Database 默认创建一个独立的内存库；传入路径则创建或打开磁盘库。
Database 实例持有一个连接，Catalog/Jobs 共享其事务所有权。
一个维护工作者使用一个连接，不允许嵌套事务；不承诺在线迁移或多工作者调度。

## 数据与 SDK1

业务值仅为 null、精确 Python 整数和递归数组；布尔、浮点数、对象及字符串
均不属于业务值。整数运算不截断。数组允许嵌套、重复、空数组及 null 元素。
输入是有限、无循环的 JSON 树，大小及嵌套层级须能由 Python 标准 JSON 和
递归调用栈处理。本组件不执行用户代码或通用 SQL。

计划文档为 {"sdk":1,"parameters":["rows","base"],"body":表达式}。
parameters 是无重复的有序非空名字列表（列表本身可以为空）。
execute(plan, parameters) 与 validate(plan) 是 sdk1 的公开入口。
参数必须是字典且键与声明完全一致；参数值在执行和接纳时递归复制。
每个表达式必须恰好具有下表字段：

| op | 其他字段 | 行为 |
| --- | --- | --- |
| const | value | 合法业务常量 |
| var | name | 当前词法环境中的名字 |
| add | left, right | 可空整数相加 |
| if | test, then, else | 非零整数选 then，0/null 选 else |
| map | array, param, body | 对源数组依次绑定 param 求 body |
| fold | array, initial, acc, item, body | 从左至右归约；acc 与 item 必须不同名 |

map 的源在外层环境求值；body 在每个元素的新词法层求值。
fold 的源及 initial 在外层求值，每轮 body 同时绑定上一累加值和当前元素。
新层可以遮蔽任意外层同名变量，不改变外层环境。body 可以返回任意业务值，
包括数组；fold 不要求满足交换律或结合律。

求值顺序属于契约：

* add 先求 left，再求 right，再检查两者类型。任一非法类型报 type；
  两者均为整数或 null 时，任一 null 使结果为 null。
* if 只求 test 和选中分支。数组不能作条件。
* map 先完整求源并检查数组类型。源为 null 返回 null，源为空返回 []，
  均不执行 body。否则按数组顺序完整执行各元素 body。
* fold 先完整求源并检查类型。null 直接返回 null，不求 initial 或 body。
  非 null 源先求 initial；空数组返回该值，不执行 body。
  map 作为 fold 的源时，整个 map 在 initial 之前完成。
* 未绑定名字是运行期 unbound。validate 只检查结构，不因某个未选分支含有
  未绑定名字而拒绝计划；任何分支中的非法 AST 结构仍在 validate 时拒绝。

samples.nested() 的 rows=[[10,20],[],[null,10]], base=7 对应
[[37,57],[],[null,37]]。最内层 fold 的 row 遮蔽外层 row，仍捕获 x 与 base。

## 目录和任务公开接口

* Catalog(db).save(name, plan) -> 正整数修订。名字非空；同名每次保存都产生
  新的不可变修订，更新 head；相同内容再次保存也产生新修订。
* get(name, revision=None) -> 独立计划文档。缺省读 head；显式修订读历史。
  revision(name) 返回当前修订。
* Jobs(db).enqueue(name, parameters) -> 正整数任务 ID。
  在同一事务中读取 head、校验并复制参数、固定 (name,revision)。
  后续更新计划、修改调用者参数或重开数据库均不能重绑定此任务。
* run(id) -> queued/done/failed 中的终态 done 或 failed。
  执行已固定修订，业务错误持久化为 failed；重复运行终态任务不再执行。
* status(id) -> {id,name,revision,state} 的独立字典。
* result(id) -> 独立业务值；queued 报 pending；failed 重抛原 code/message。

run 当前一次性解析完整参数、执行完整数组并序列化完整结果，在一个事务中
发布终态。未优化的整数组路径正确，但内存随输入、数组中间值和结果增长。
业务错误也在该事务中发布；其他异常回滚，任务保持 queued，不发布半个结果。
关闭连接后读取结果需重新打开 Database。

WarehouseError 提供 code 与 message。invalid_plan 表示计划结构或名字无效；
invalid_value 表示业务值越界；invalid_params 表示参数集合/位置数量错误；
unbound、type 表示运行错误；not_found 表示计划或任务不存在；
pending 表示任务未完成；storage_version 表示存储版本不受当前应用支持。
原生 SQLite 错误和非业务程序异常不伪装为 WarehouseError。
业务值 null 的成功任务存储 output='null'，与 queued 的 SQL NULL 不同。

## SQLite v1 与冻结旧库

legacy_format.py 是固定旧格式，独立于 Catalog/Jobs 和运行时。
meta 的唯一记录持有版本；plans_v1 以 (name,revision) 为主键，payload 为完整
SDK1 文档；heads_v1 引用当前修订。jobs_v1 保存固定修订、命名参数 JSON、
状态及结果/错误 JSON，外键连接真实修订。程序没有只存名字再解析 head 的路径。

samples.create_frozen(path) 直接使用 SQLite、固定旧 DDL 和有限历史行构库，
不执行任何 SDK，不调用目录或队列；拒绝有表的目标。frozen_rows() 提供可检验
的原始历史内容：

| 任务 | 固定修订 | 接纳状态 | 结果或错误 |
| --- | --- | --- | --- |
| 1 | offset/1，head 已为 2 | queued | [9,9,null] |
| 2 | nested/1 | queued | [[37,57],[],[null,37]] |
| 3 | offset/1 | done | [] |
| 4 | lazy/1 | failed | unbound: missing |
| 5 | lazy/1 | queued | 5 |
| 6 | offset/1 | done | null |

Database.transaction(before_commit=None) 提供 BEGIN IMMEDIATE 事务。
钩子在所有修改之后、commit 之前接收真实连接；任意异常（含 BaseException）
回滚 DDL、数据和版本。此设施不负责计划转换或重绑定。
磁盘回归核对提交前异常后的重开和重试；没有杀进程恢复承诺或伪造的终止证据。

## SDK2 独立端口

sdk2.execute(program, arguments) 接受 {"sdk":2,"arity":N,"body":表达式}
和位置参数数组；sdk2.validate 检查完整结构。数据及算术语义与 SDK1 相同。
SDK2 不提供名称查找、隐式父环境、SDK1 编译或迁移能力。

| op | 其他字段 | 语义 |
| --- | --- | --- |
| literal | value | 业务常量 |
| local | slot | 当前调用的零起始参数槽 |
| capture | slot | 当前闭包的零起始捕获槽 |
| plus | left, right | 与 SDK1 add 同序 |
| choose | test, then, else | 惰性条件 |
| closure | arity, captures, body | captures 为表达式列表，创建时依次求值并固定 |
| call | fn, args | 先求 fn，再依次求参数，检查闭包及实参数量，调用 |
| array_map | array, fn | 先求完整源；每个元素求 fn 并以一个参数调用 |
| array_fold | array, initial, fn | 先源、后初值；每轮求 fn 并以累加值、元素调用 |
| pack | items | 依次求表达式，构成不可变语义的环境数组 |
| item | array, index | 静态非负索引取环境元素，null/越界报 type |
| error | code, message | 延迟抛出 unbound 或 type |

closure 的 body 只看自己的 local 和 capture，不继承创建者的 local。
捕获值在执行期间没有修改指令。局部或捕获槽越界报 unbound；
闭包参数数量不符报 type。最外层的 capture 集合为空。
闭包只用于运行时内部，公开 execute 结果必须为业务值。

array_map/array_fold 不提前创建 fn：null/空源不求无用 fn；
null fold 也不求初值。此规则避免空数组仅因不用的捕获而报错。
choose 的未选分支不创建其中的闭包。任意位置传入的坏 AST 仍被 validate 拒绝。

samples.native_program() 和 native_program(True) 是手写的两个 SDK2 原生
端口样例，分别使用标量捕获与环境数组，均计算数组加偏移量。
tests/test_runtime.py 还独立测试多层捕获、两个 fold 参数、闭包无隐式父层、
显式调用与创建时捕获错误顺序。它们不接纳 SDK1 AST，不生成迁移代码。

schema_v2.sql 是离线目标端口格式，不由当前应用安装。
plans_v2 保存 SDK2 program 和参数名字顺序 parameter_order（JSON 数组）；
顺序与 program.arity 必须一致。jobs_v2.arguments 是按该顺序排列的值数组，
并以同一 (name,revision) 引用计划；head 独立存储。终态约束与 v1 一致。
meta.version=2 可作为将来完整切换的存储标记；DDL 不自行改版本。
两代表示尚无适配关系，v1 表也尚未被改为审计用途。
