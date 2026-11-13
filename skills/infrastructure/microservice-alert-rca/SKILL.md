---
name: microservice-alert-rca
description: 微服务 Prometheus 告警根因分析 runbook（Keep ask-holmes 自动触发或人工询问）。流程：先查 LTS 日志（grafana-lts-log-query skill 的脚本）拿证据，Down 类告警必须交叉验证 Agent 断流 vs 真宕机，5xx 用 request_id 关联单请求，关联发布/重启事件，输出根因/影响/修复建议/置信度。适用于 OmWebserverDown、AppClaServerDown、Http5xxRateHigh、ErrorLogSpike、HeapUsageHigh、PodRecentlyRestarted 等微服务告警，以及"XX 服务挂了/为什么一直报错"类人工问题定位。
compatibility:
  tools:
    - Bash
---

# 微服务告警根因分析 Runbook

## 何时使用

- Keep→Holmes 的告警分析请求（source=prometheus，中心 Prometheus 规则触发的微服务告警）
- 人工问题定位："ascend 的 om-webserver 为什么 5xx / 挂了 / 一直报错"

## 从告警上下文提取参数

| 参数 | 来源 | 示例 |
|---|---|---|
| cluster | 告警 labels.cluster | `ascend-guiyang-prod-cluster` |
| service | 告警 labels.service | `om-webserver` / `app-cla-server` |
| community | 告警 labels.community | `ascend` / `infrastructure` |
| pod / uri / path | 可选，定位具体实例或接口 | `omwebserver-f879d7bdb-2fz66` |
| 时间窗 | 告警时间往前 30~60 分钟 | `--minutes 60` |

## 排查步骤（按序执行）

### 1. 查 LTS 日志（必做，先拿证据再下结论）

```
python3 /etc/holmes/skills-git/skills/infrastructure/grafana-lts-log-query/scripts/query_lts_logs.py \
  --cluster <cluster> --service <service> --level error --minutes 60
```

- `--cluster` 直接用告警标签（脚本已做指标口径→LTS 流映射：`ascend-guiyang-prod-cluster`→guiyang 流、`mindspore-cn-north1-new-cluster`→bj1 流）
- error 量级大时先加 `--top-msg` 聚合看错误类型分布，再抽明细
- `location` 列是代码位置（Java/Go `文件:行号`），`error` 列是堆栈
- 5xx 告警：从日志取一条异常请求的 `request_id`，`--extra "request_id like 'xxx'"` 追踪完整调用链
- 注意：guiyang 流无 pod 列（服务级过滤）；时间窗按告警时间调整

### 2. Down（存活实例=0）告警必须交叉验证——series 全消失 ≠ 宕机

用 prometheus/metrics 工具集查**同集群其他服务**的指标是否同时消失：

- 其他服务指标正常 → 真宕机：查该服务 pod 状态/事件（有权限时），或建议人工 `kubectl -n <ns> get pod` 排查
- 同集群其他服务指标也消失 → **Agent remote_write 断流**：根因在监控链路（查 Prometheus Agent），不是业务故障

❌ 不做交叉验证直接说"服务宕机" = 误报根因。

### 3. 关联发布与重启

- 告警时间点附近有 PodRecentlyRestarted / process_uptime 骤降 → 大概率发布引入，对比日志时间与重启时间
- ErrorLogSpike + HeapUsageHigh 同现 → 疑内存泄漏或大结果集查询，关注 GC 与大对象

### 4. 已知错误模式速查（实测沉淀）

| 服务 | 日志模式 | 判定 |
|---|---|---|
| om-webserver | `getSdkInfo diag ... NoHttpResponseException: id1.cloud.huawei.com:443 failed to respond` | 华为云账号服务外部依赖网络问题（查依赖，非本服务代码） |
| om-webserver | `hostname verification warn: id1/oauth-login.cloud.huawei.com` | 常态噪声，与故障无关 |
| om-webserver | 400 on `/oneid/hwaccount/getSdkInfo` | 常态客户端错误，不构成故障 |
| app-cla-server | `send cla notify email failed ... admin name is null` | 企业管理员数据质量问题（补数据，非基础设施故障） |

## 输出格式（分析结论必须四段俱全）

```
根因: 一句话结论 + 证据（引用具体日志行/指标值）
影响: 影响哪些功能/用户，当前是否已恢复
修复建议: 可执行动作（重启/扩容/改配置/联系依赖方/修数据）
置信度: 高/中/低
```

证据不足时明确输出"无法定位根因，建议人工查 XXX"——**不要编造根因**。
