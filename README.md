# 疏浚泥沙合样级配系统

疏浚组用合样级配比较施工前后的泥沙粗细。FastAPI 提供接口，SQLite 保存日样与汇总。

换筛具后，部分日样只能以"相邻内置档合并"的粒度上报。本系统同时接受标准 11 档
与合并档两种格式，两种格式可互换，同断面同日重交整份替换。

## 快速启动

```bash
docker compose up --build
```

服务监听 `8000` 端口，数据保存在命名卷 `sediment-data`（容器内 `/data/sediment.db`）。
**旧版 SQLite 卷可直接挂载继续使用**：启动时自动补出新增列（见下文"存储与迁移"）。

本地开发（无需 Docker）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

运行测试：

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

## 接口

### 提交日样：`POST /docs`

`GET /docs` 仍是 Swagger 交互文档，可直接在页面里 Try it out。两种上报格式二选一。

**标准格式** `weights`：各筛级干重（非负整数克，恰好 11 档，顺序与 `GET /grades` 一致）。

```bash
curl -X POST http://localhost:8000/docs \
  -H 'Content-Type: application/json' \
  -d '{"section":"S1","date":"2026-03-15","weights":[10,20,30,40,50,30,20,10,5,3,2]}'
```

**合并档格式** `bins`：相邻内置档合并后的各档，每档给 `upper_mm`（档上限，必须取
某个内置档上限，按升序排列，最粗一档上限为 **64 mm**）与 `grams`（非负整数克）。
第一个档隐式从最细内置档（0.063 mm）起覆盖。例如下例三档分别覆盖
(..0.25]、(0.25..2.0]、(2.0..64]：

```bash
curl -X POST http://localhost:8000/docs \
  -H 'Content-Type: application/json' \
  -d '{"section":"S1","date":"2026-03-15",
       "bins":[{"upper_mm":0.25,"grams":30},
               {"upper_mm":2.0,"grams":70},
               {"upper_mm":64.0,"grams":100}]}'
```

合并档的克重记在其档上限所属的内置档列上；非零合并档若覆盖多个内置档，就
"跨过"了这些内置档之间的档界（零重量档可自由拆分，不计跨界）。

同一断面同日再次提交**整份替换**旧样，标准格式与合并档格式可以互换。回执：

```json
{"section":"S1","date":"2026-03-15","replaced":true,"summaries_written":17,
 "sample_total_g":200,"merged":true}
```

### 查询级配：`GET /query`

任选同一自然年内的起止日（含首尾）和 1～4 个断面（重复传参或逗号分隔均可）：

```bash
curl 'http://localhost:8000/query?start=2026-01-05&end=2026-03-31&sections=S1&sections=S2'
```

每个断面返回：实读汇总份数、总干重，以及——

- **档界全部确定**（所选日样没有任何非零合并档跨过内置档界）：照常给出各档合计
  干重、由细到粗的累计过筛率（%）、D50（从细到粗累计首次达到总干重一半的筛级
  上限），`crossed_boundaries_mm` 为空数组，`message` 为 `null`。
- **存在跨档合并档**：暂停该断面的标准级配与 D50——`grade_totals_g`、
  `cumulative_passing_percent`、`d50_mm` 均为 `null`，`crossed_boundaries_mm`
  按升序列出被所选日样的非零合并档跨过的内置档界（mm），`message` 说明暂停。
  总干重始终有效（合并不影响总量）。
- **总干重为零**（无样或所选日样全零）：`message` 为 **"无样"**，级配字段为 `null`。

### 其他

- `GET /grades`：查看内置统一筛级（11 档上限：0.063～64 mm）、10 个内部档界与最粗上限。
- `GET /healthz`：健康检查。

## 存储、一致性与迁移设计

**日样表** `daily_samples(section, date, g0..g10, mask)`：主键（断面, 日期），重交
整份替换。`g` 列存各内置档克重（合并档克重落在其上限档列）；`mask` 是 10 位掩码，
记录该日样的非零合并档跨过了哪些内部档界（标准 11 档上报时为 0）。

**汇总表** `summaries(section, year, month, day, g0..g10, c0..c9)`：每份汇总记录一个
断面在"当月 1 日至当日"连续日段内的各档合计（`g`，月内前缀），以及各内部档界被
非零合并档跨过的累计**次数**（`c`，每份日样对其掩码置位档界贡献 1）。某月最后一日
的前缀即该月合计。不变式：某（月, 日）无行 ⟺ 当月该日及之前没有任何已提交日样，
查询按零计。

**查询只读汇总，每断面最多读 16 份**：日段 [a, b] 分解为——
- a、b 同月：`P(b) − P(a−1)`，≤ 2 份；
- 跨月：左月（月总 − P(a−1)，a 为 1 日则只读月总）+ 中间整月各 1 份月总（同一年内
  ≤ 10 份）+ 右月 `P(b)`，合计 ≤ 13 份。

档界判定用的 `c` 列就在这些汇总行内随 `g` 列一起读出，**不额外产生读写份数**。
整次查询（全部断面）包在一个只读事务里，WAL 模式下取自同一已提交状态。响应列出
各断面实读的份数。

**补录或更正**：只读取变动的那份原始日样求差量 δ（克重之差与跨界次数之差），日样
upsert 后，把当月 [当日, 月末] 的每份前缀汇总用一条 upsert 整体加 δ（已存在的行加
δ，未存在的行以 δ 建行——由不变式其旧值必为零）。最多写 31 份（≤ 32），回执列出
实写份数。判定档界所用的汇总同样计入这 32 份写预算。

**日样和汇总一起生效**：两者在同一 SQLite 事务中提交；写入中途失败整体回滚，
原结果仍可查。

**旧卷迁移**：旧版表只有 `g` 列。启动时对 `daily_samples` 补 `mask` 列、对
`summaries` 补 `c0..c9` 列（`ALTER TABLE ADD COLUMN ... DEFAULT 0`，只补缺失列）。
旧数据掩码/计数均为 0，即视为标准 11 档、不跨任何档界，语义不变，可正常查询。

## 目录结构

```
app/
  main.py       FastAPI 路由与请求/响应模型（两种格式、档界暂停）
  store.py      日样写入（事务）与汇总查询（克重前缀 + 跨界计数前缀）
  db.py         SQLite 连接、建表、WAL 与旧卷迁移
  gradation.py  内置筛级、合并档解析/掩码、累计与 D50 计算
tests/test_app.py
Dockerfile / docker-compose.yml
```
