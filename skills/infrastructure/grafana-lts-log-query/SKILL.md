---
name: grafana-lts-log-query
description: 社区服务日志查询（华为云 LTS，经 Grafana 查询接口）。当用户要查社区服务日志、看某个服务的报错、查 LTS 日志、看 Grafana 日志大盘、按服务名/级别/时间范围检索日志、按请求 ID 追踪一次调用、查 squid 访问日志、分析线上服务错误日志等情况时立即触发。支持集群/服务/级别/时间范围/关键字过滤与大盘 URL 直接转查询，先量级探测后读明细，明细行数与时间窗口有硬上限（token 保护）。
compatibility:
  tools:
    - Bash
---

# 社区服务日志查询 Skill（LTS via Grafana）

## 使用场景

- 排查线上服务故障时查日志：某服务（如 `om-webserver`、`repo-watcher`、`cve-sa-backend`）最近报了什么错
- 按时间范围 + 级别（error/warn）过滤日志，定位报错内容与发生频率
- 按 `request_id` / `trace_id` 追踪一次请求的完整日志链
- 统计某服务的错误类型分布（按 `msg` 聚合）
- 查 squid 代理访问日志（cn12-001 集群）
- 用户提供《社区服务日志（LTS）》大盘链接，需要按同样条件取数分析
- 告警分析（Keep→Holmes 的告警根因请求，或人工问"XX 服务为什么挂了"）——见下方"告警驱动用法"

## 告警驱动用法

告警分析时参数直接取告警标签，不询问用户：

- `--cluster` 用告警的 `cluster` 标签（**Prometheus 指标口径**，如 `ascend-guiyang-prod-cluster`、`mindspore-cn-north1-new-cluster`——脚本已做指标口径→LTS 流的别名映射，直接传即可）
- `--service` 用告警的 `service` 标签
- 时间窗默认告警前 60 分钟（`--minutes 60`），按告警时间调整
- 完整排查流程（Down 告警的 Agent 断流交叉验证、request_id 追踪、已知错误模式速查、输出格式）见 `microservice-alert-rca` skill 的 runbook

## 前置要求

- Python 3.8+（脚本仅标准库）
- 本 skill 的标准部署路径（Holmes/K8s 部署的 sparse clone 挂载点，命令一律用绝对路径）：
  `/etc/holmes/skills-git/skills/infrastructure/grafana-lts-log-query`
  （非 Holmes 环境使用时，把下述命令里的该前缀替换为实际 skill 目录）
- 凭据（环境变量，不落盘、不回显，**禁止在输出中 echo 凭据**）：
  - `HOLMES_GRAFANA_TOKEN`：Grafana Service Account Token（优先，Bearer 认证）
  - 或 `HOLMES_GRAFANA_USER`（默认 admin）+ `HOLMES_GRAFANA_PASSWORD`（Basic 兜底）
- `GRAFANA_URL`：Grafana 基地址，默认集群内
  `http://community-grafana.infra-monitoring-community.svc.cluster.local:3000`；
  公网入口 `https://monitoring.osinfra.cn/grafana` 在 CloudWAF 后面，脚本已内置浏览器 UA
- 数据链路：脚本 → Grafana `/api/ds/query` → LTS 管道符插件后端（代为 AK/SK 签名）→ 华为云 LTS。
  **调用方全程不接触 LTS 的 AK/SK**，只需要 Grafana token。

## 可查数据源（6 条流）

| 集群（--cluster 取值） | 数据源 uid | Region | 内容 | 结构化字段可查边界 |
|---|---|---|---|---|
| `openeuler-community` / `bj` | community-lts-openeuler | cn-north-4 | 12 个机器人 | 2026-10-14 起 |
| `openeuler-hk-cce-x86` / `hk` | community-lts-hk | ap-southeast-1 | 香港 26 服务集群 stdout | 2026-10-15 10:00（北京时间）起 |
| `infra-hk-preview-cluster-003` / `preview` | community-lts-hk-preview | ap-southeast-1 | 多社区预览（hotopic-data-clean / oss-map） | 接入时起 |
| `ascend-guiyang-prod-cluster-001` / `guiyang` | community-lts-guiyang | cn-southwest-2 | Ascend/CANN（robot-issue-manage） | 接入时起 |
| `bj1-infra-cluster` / `bj1` | community-lts-bj1 | cn-north-1 | MindSpore/Infra（cla-backend） | 接入时起 |
| `cn12-001` / `cn12` / `squid` | community-lts-cn12 | cn-north-12 | CI 集群共享 stdout（squid 访问日志在此，脚本自动加 `msg='squid_access'`） | 接入时起 |

脚本支持 uid / 集群名 / 别名任意一种；新流未收录时用 `--uid <uid> --group-id <gid> --stream-id <sid>`。

## 查询纪律（token 保护，最高优先级）

| 规则 | 内容 |
|------|------|
| 第 1 步永远先 `--count` | 量级探测最便宜；先知道有多少条，再决定怎么查明细 |
| 明细默认 limit 100 | 硬上限 1000（脚本拒绝超限），一般 100 条足够定性 |
| 时间窗口默认 60 分钟 | 按需放大 60m → 6h → 24h → 7d，**禁止一上来就 7d**；硬上限 7d |
| 禁止 `select *` | 明细用默认精简投影；自定义投影也只选需要的列 |
| 字段值截断 300 字符 | 默认开启（`--no-trunc` 仅在确认需要完整堆栈时用） |
| 聚合用 `msg` 不用 `error` | `error` 列取值逐次不同（含地址/URL/trace_id）不可聚合 |

**例外**：用户直接粘贴的日志内容不受此限制（token 已消耗），直接分析。

## Step 0：解析用户输入

| 输入形式 | 示例 | 处理 |
|---------|------|------|
| 服务名 + 现象 | "om-webserver 最近老是 502" | `--cluster hk --service om-webserver`（集群未知时先问或按服务归属推断） |
| 大盘 URL | `https://monitoring.osinfra.cn/grafana/d/community-service-logs/...?var-cluster=community-lts-hk&var-service=om-webserver&var-level=%25` | 走 `--url`，自动解析集群/服务/级别/时间范围 |
| 请求 ID | "查一下这个请求为什么失败：6f9c2e11-..." | `--extra "coalesce(request_id, trace_id) like '<id>'"` |
| 只知道现象 | "香港集群今天有报错吗" | 先 `--list-services` 或按时间窗 `--count` |

## Step 1：量级探测（必做）

```bash
python3 /etc/holmes/skills-git/skills/infrastructure/grafana-lts-log-query/scripts/query_lts_logs.py --cluster hk --service om-webserver --level error --count
# → 单行数字。0 条：放宽级别/时间窗再试，或该服务是纯文本流（见"已知坑"3）
```

## Step 2：错误类型分布（可选，量大时优先于明细）

```bash
python3 /etc/holmes/skills-git/skills/infrastructure/grafana-lts-log-query/scripts/query_lts_logs.py --cluster hk --service om-webserver --level error --top-msg --minutes 360
# → msg + 条数 Top20，一眼看出主要错误类型
```

## Step 3：明细查询

```bash
# 最新 100 条（默认最近 60 分钟，按需 --minutes 放大）
python3 /etc/holmes/skills-git/skills/infrastructure/grafana-lts-log-query/scripts/query_lts_logs.py --cluster hk --service om-webserver --level error

# 大盘 URL 直接转查询（窗口取 URL 里的 from）
python3 /etc/holmes/skills-git/skills/infrastructure/grafana-lts-log-query/scripts/query_lts_logs.py --url "<大盘URL>"

# 按请求 ID 追踪完整链路
python3 /etc/holmes/skills-git/skills/infrastructure/grafana-lts-log-query/scripts/query_lts_logs.py --cluster hk \
  --extra "coalesce(request_id, trace_id) like '6f9c2e11-47c9-11f1-946a-0255ac100abc'"

# squid 访问日志（cn12 流自动带 msg='squid_access'）
python3 /etc/holmes/skills-git/skills/infrastructure/grafana-lts-log-query/scripts/query_lts_logs.py --cluster cn12 --extra "url like '%openeuler.org%'"
```

输出头部 `# ...` 行携带集群/窗口/模式元数据；正文 TSV 首行为列名；末行 `LTS_QUERY: OK rows=N`（机器可读契约）。

## Step 4：输出报告

直接在对话中给结论，**不粘贴大段原始日志**，只引用最关键的 1-3 行（时间 + 级别 + msg/error 摘要），并给出：

- 错误类型（msg 维度）与量级（count/时间窗）
- 时间分布（集中爆发 or 持续低频）
- 责任方向初判（业务代码 / 依赖服务 / 网关 / 平台）
- 建议下一步（结合其他 skill：kubernetes/logs 查容器事件、jenkins-log-diagnose 查构建等）

## 机器调用模式（Keep/Holmes 告警流水线集成）

headless 单命令，凭据来自环境变量：

```bash
python3 /etc/holmes/skills-git/skills/infrastructure/grafana-lts-log-query/scripts/query_lts_logs.py --cluster <cluster> --service <service> --level error --count   # 第 1 步
python3 /etc/holmes/skills-git/skills/infrastructure/grafana-lts-log-query/scripts/query_lts_logs.py --cluster <cluster> --service <service> --level error --minutes 60  # 第 2 步（仅 count>0 时）
```

- 输入到 LLM 的日志内容 **≤100 行**（明细 limit 默认 100 即上限）
- 退出码 0 且末行 `LTS_QUERY: OK rows=N` 为成功；`LTS_QUERY: EMPTY` 为无数据；stderr + 非 0 为失败
- 禁止在此模式下 `--no-trunc` / `--limit 1000`

## 已知坑与噪声（避免误判）

| # | 坑 | 说明 |
|---|---|------|
| 1 | 结构化字段有时间边界 | 北京流 2026-10-14 起、香港流 2026-10-15 10:00 起才有 level/msg/service 等列；更早的行这些列是**空的**，不是没打也不是解析失败 |
| 2 | 双日志器字段方言 | 北京流：框架 logrus 写 `component`/`trace_id`/`logger`，obs-sdk 写 `service`/`request_id`——脚本已用 coalesce 合并，手写 SQL 时也必须 coalesce |
| 3 | 香港纯文本服务 | `doc-search-deployment` / `api-cve` / `cve-manager`（量最大）**没有任何结构化字段**，按服务过滤恒空——空结果不一定是筛选错了 |
| 4 | 已知噪声 | 北京流 error 的九成以上是 `not a excepted url: /metrics`（机器人框架把 Prometheus 抓取记成 error），**不是业务故障** |
| 5 | cost_ms 列 | 香港有、北京没有；投影带上北京流整个查询 400（LTS.2022）。脚本默认投影不含它 |
| 6 | level 方言 | 北京只打 `warning`，香港 `warn`/`warning` 都有——脚本 `--level warn` 自动展开 `warn%` |
| 7 | debug/fatal | 北京流无 debug；香港的 fatal 没进默认级别，需 `--extra "level = 'fatal'"` |
| 8 | 服务改名/迁移 | 过滤按 service 字段/容器名，改名后静默变空——结合 `--list-services` 确认 |
| 9 | LTS 列序不稳定 | 插件返回列顺序随机（Go map 遍历），脚本按字段名对齐，不受影响 |
| 10 | cn12 共享流 | 混有 volcano/npu-exporter/pypi-cache 等，查 squid 必须带 `msg='squid_access'`（脚本自动） |
| 11 | 大窗口慢 | 24h 聚合扫描 15-20s 属正常，脚本超时 120s；避免无意义的大窗口全量扫 |
| 12 | `__time` 是毫秒 | 手写 SQL 时 `from_unixtime(__time/1000)` |

## 使用示例

### 示例 1：查服务最近报错

```
/grafana-lts-log-query 香港集群 om-webserver 最近一小时有什么报错
```

Skill 执行：`--count` → `--top-msg` → 明细（默认投影），引用关键错误行输出结论。

### 示例 2：大盘链接转查询

```
/grafana-lts-log-query https://monitoring.osinfra.cn/grafana/d/community-service-logs/7fb54dc?orgId=1&from=now-24h&to=now&var-cluster=community-lts-hk&var-service=om-webserver&var-level=%25
```

Skill 走 `--url` 解析出 cluster=hk / service=om-webserver / level=% / 窗口 24h，先 `--top-msg` 看分布再抽明细。

### 示例 3：按请求 ID 追踪

```
/grafana-lts-log-query 帮我查这个请求的日志：request_id 6f9c2e11-47c9-11f1-946a-0255ac100abc，香港集群
```

Skill 用 `--extra "coalesce(request_id, trace_id) like '...'"` 拉全链路日志（级别不限）。

### 示例 4：squid 访问日志

```
/grafana-lts-log-query squid 上最近一小时访问 openeuler.org 的 5xx 请求
```

Skill 执行 `--cluster cn12 --extra "url like '%openeuler.org%' and status like '5%'"`。

## 注意事项

- 本 skill 只做**日志检索**；定性诊断配合 jenkins-log-diagnose（CI 构建）/ kubernetes/logs（集群内容器）使用
- 凭据仅从环境变量读取，任何输出（包括调试信息）不得包含 token/口令
- 数据源清单与大盘（helm-chart-value `common/grafana/prod/values-dashboards.yaml`）同步维护：新增流时更新脚本 `STREAMS` 表
- 查询语法为 LTS 管道符 SQL：`* | SELECT ... WHERE ... GROUP BY ... ORDER BY ... LIMIT ...`（不是 LogQL）

## 相关 Skills

- [jenkins-log-diagnose](../jenkins-log-diagnose/SKILL.md) — Jenkins 构建失败定性（同类 token 纪律）
- [github-action-diagnose](../github-action-diagnose/SKILL.md) — GitHub Actions CI 诊断

---

## 更新日志

### v1.0.0 (2026-11-10)
- 初始版本：经 Grafana `/api/ds/query` 查华为云 LTS 管道符 SQL，内置 6 条流对照表（uid/groupId/streamId）
- 五种模式：`--count` 量级 / `--top-msg` 错误类型分布 / `--list-services` 服务枚举 / 明细 / `--sql` 自定义
- 大盘 URL 直接转查询（解析 var-cluster/var-service/var-level/from-to）
- token 保护纪律：明细 1000 行硬上限、时间窗 7d 硬上限、字段 300 字符截断、禁 select *
- Bearer token（Service Account）优先、Basic 兜底；凭据不落盘不回显
- 沉淀大盘运维已知的 12 个坑（结构化时间边界、双日志器方言、纯文本服务、/metrics 噪声等）
