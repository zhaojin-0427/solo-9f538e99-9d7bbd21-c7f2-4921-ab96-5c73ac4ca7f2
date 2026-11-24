# 疏浚泥沙合样级配系统

疏浚组用合样级配比较施工前后的泥沙粗细。FastAPI 提供接口，SQLite 保存日样与汇总。

## 快速启动

```bash
docker compose up --build
```

服务监听 `8000` 端口，数据保存在命名卷 `sediment-data`（容器内 `/data/sediment.db`）。

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

按断面和日期提交各筛级干重（非负整数克，恰好 11 档，顺序与 `GET /grades` 一致）。
同一断面同日再次提交**整份替换**旧样。`GET /docs` 仍是 Swagger 交互文档，可直接在页面里 Try it out。

```bash
curl -X POST http://localhost:8000/docs \
  -H 'Content-Type: application/json' \
  -d '{"section":"S1","date":"2026-03-15","weights":[10,20,30,40,50,30,20,10,5,3,2]}'
```

回执（补录/更正时列出实写汇总份数）：

```json
{"section":"S1","date":"2026-03-15","replaced":true,"summaries_written":17,"sample_total_g":220}
```

### 查询级配：`GET /query`

任选同一自然年内的起止日（含首尾）和 1～4 个断面（重复传参或逗号分隔均可）：

```bash
curl 'http://localhost:8000/query?start=2026-01-05&end=2026-03-31&sections=S1&sections=S2'
```

每个断面返回：各档合计干重、由细到粗的累计过筛率（%）、D50（从细到粗累计首次达到
总干重一半的筛级上限）、实读汇总份数。总干重为零时 `message` 为 **"无样"**，
D50 与累计过筛率为 `null`。

### 其他

- `GET /grades`：查看内置统一筛级（11 档上限：0.063～64 mm）。
- `GET /healthz`：健康检查。

## 存储与一致性设计

**日样表** `daily_samples(section, date, g0..g10)`：主键（断面, 日期），重交整份替换。

**汇总表** `summaries(section, year, month, day, g0..g10)`：每份汇总记录一个断面在
连续日段内的各档合计——具体为"当月 1 日至当日"的月内前缀；某月最后一日的前缀即
该月合计。不变式：某（月, 日）无行 ⟺ 当月该日及之前没有任何已提交日样，查询按零计。

**查询只读汇总，每断面最多读 16 份**：日段 [a, b] 分解为——
- a、b 同月：`P(b) − P(a−1)`，≤ 2 份；
- 跨月：左月（月总 − P(a−1)，a 为 1 日则只读月总）+ 中间整月各 1 份月总（同一年内
  ≤ 10 份）+ 右月 `P(b)`，合计 ≤ 13 份。

整次查询（全部断面）包在一个只读事务里，WAL 模式下取自同一已提交状态。响应列出
各断面实读的份数。

**补录或更正**：只读取变动的那份原始日样求差量 δ，日样 upsert 后，把当月
[当日, 月末] 的每份前缀汇总用一条 upsert 整体加 δ（已存在的行加 δ，未存在的行以 δ
建行——由不变式其旧值必为零）。最多写 31 份（≤ 32），回执列出实写份数。

**日样和汇总一起生效**：两者在同一 SQLite 事务中提交；写入中途失败整体回滚，
原结果仍可查。

## 目录结构

```
app/
  main.py       FastAPI 路由与请求/响应模型
  store.py      日样写入（事务）与汇总查询
  db.py         SQLite 连接、建表、WAL
  gradation.py  内置筛级、累计与 D50 计算
tests/test_app.py
Dockerfile / docker-compose.yml
```
