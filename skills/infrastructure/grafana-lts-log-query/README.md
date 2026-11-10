# 社区服务日志查询 Skill（LTS via Grafana）

> 经 Grafana `/api/ds/query` 查询华为云 LTS 上的社区服务日志（管道符 SQL），调用方只需 Grafana Service Account Token，不接触 LTS AK/SK。

## 功能

- 内置 6 条 LTS 流对照表（openeuler-community / openeuler-hk-cce-x86 / hk-preview / guiyang / bj1 / cn12-squid），`--cluster` 按名或别名选流
- 五种查询模式：`--count` 量级探测、`--top-msg` 错误类型分布、`--list-services` 服务枚举、明细查询、`--sql` 自定义管道符 SQL
- 《社区服务日志（LTS）》大盘 URL 直接转查询（解析 var-cluster / var-service / var-level / from-to）
- token 保护纪律（脚本层面强制）：明细 1000 行硬上限、时间窗 7d 硬上限、字段 300 字符截断、必须带 limit
- 凭据：`HOLMES_GRAFANA_TOKEN`（Bearer）优先，`HOLMES_GRAFANA_USER`/`HOLMES_GRAFANA_PASSWORD`（Basic）兜底，不落盘不回显
- 规避 LTS 插件列序不稳定（按字段名对齐）、cn12 共享流自动加 `msg='squid_access'`、`warn` 自动展开 `warn%`

## 快速开始

```bash
# 量级探测（永远的第一步）
python3 scripts/query_lts_logs.py --cluster hk --service om-webserver --level error --count

# 错误类型分布
python3 scripts/query_lts_logs.py --cluster hk --service om-webserver --level error --top-msg --minutes 360

# 明细（最新 100 条，默认最近 60 分钟）
python3 scripts/query_lts_logs.py --cluster hk --service om-webserver --level error

# 大盘 URL 直接转查询
python3 scripts/query_lts_logs.py --url "https://monitoring.osinfra.cn/grafana/d/community-service-logs/7fb54dc?orgId=1&from=now-24h&to=now&var-cluster=community-lts-hk&var-service=om-webserver&var-level=%25"
```

依赖：仅 Python 3.8+ 标准库。完整用法、查询纪律与已知坑见 [SKILL.md](SKILL.md)。

## 维护

数据源对照表（`STREAMS`）与大盘配置同步维护：helm-chart-value 仓 `common/grafana/prod/values-dashboards.yaml` 新增 LTS 流时，同步更新脚本里的 uid / groupId / streamId。
