# openEuler log-ci 平台专项

openEuler 社区 CI 的 Jenkins 日志有两种入口，`jenkins-log-diagnose` 脚本的 `--url` 会自动探测后端类型，无需手动指定。

---

## 双入口 URL

| 入口 | URL 形态 | 后端类型 | 认证 |
|------|---------|---------|------|
| 公网原生 Jenkins | `https://ci.openeuler.openatom.cn/job/<a>/job/<b>/.../job/<job>/<构建号>/` | 原生 Jenkins（`/api/json`） | 匿名可读 |
| Log Viewer | `https://log-ci.openeuler.openatom.cn/job/<jobPath>/<构建号>` | SPA + `/api/build/*` | 匿名可读 |

**注意**：
- 两个 URL 的路径格式不同：原生 Jenkins 每个目录段前都要 `/job/`；Viewer 的 `/job/` 后直接跟完整 jobPath（如 `multiarch/src-openeuler/x86-64/qt6-qtlanguageserver`）
- Viewer 页面本身是单页应用（所有路径返回 index.html），**不能**直接在其后拼 `consoleText` / `api/json`
- Viewer 构建元数据中的 `url` 字段可能指向集群内网地址（`jenkins.jenkins.svc.cluster.local`），不可外部访问；构建历史中的 `url` 字段指向公网原生入口

## Viewer API 契约（`log-ci.openeuler.openatom.cn`）

| 端点 | 用途 | 关键参数 |
|------|------|---------|
| `GET /api/build/info?job=<jobPath>&build=<n>` | 构建元数据（result/building/timestampMs/durationMs/isPipeline/stages/children） | job 需 URL 编码 |
| `GET /api/build/list?job=<jobPath>&limit=<n>` | 构建历史（buildNumber/result/timestampMs/durationMs） | — |
| `GET /api/build/log/download?job=<jobPath>&build=<n>` | 完整日志下载 | 脚本用它拉全量日志到本地缓存 |
| `GET /api/build/log?job=...&build=...&start=<offset>` | 分块日志 | 前端增量加载用 |
| `GET /api/build/stage/log?job=...&build=...&node=<nodeId>` | Pipeline 阶段日志 | Pipeline 任务用 |

## 构建日志结构（compass-ci 环境）

典型 freestyle 构建日志阶段顺序：

1. **系统信息**（`Welcome to ... kernel`、内存/磁盘/负载 echo）— 正常输出，跳过
2. **环境配置**（`The ebs configuration is complete.`、`cat .config/cli/defaults/config.yaml`、`update ccb`）— 环境准备
3. **git clone 测试框架**（`git clone https://atomgit.com/openeuler-customization/lkp-tests.git`）— 常见失败点
4. **下载/构建软件包**（业务阶段）
5. **测试执行**（lkp-tests / compass-ci 跑测试）
6. **收尾**（`Finished: SUCCESS/FAILURE`）

## 典型故障模式

### 环境准备阶段 clone 超时（类型 A3，实测案例）
```
+ git clone --depth=1 https://atomgit.com/openeuler-customization/lkp-tests.git /home/jenkins/ccb1
Cloning into '/home/jenkins/ccb1'...
fatal: unable to access 'https://atomgit.com/openeuler-customization/lkp-tests.git/':
Failed to connect to atomgit.com port 443 after 128608 ms: Connection timed out
Build step 'Execute shell' marked build as failure
Finished: FAILURE
```
- **定性**：基础设施侧 — 网络/依赖服务（构建节点无法建立到 atomgit.com:443 的 TCP 连接）
- **关键判据**：
  - 失败发生在 `update ccb`（拉取 lkp-tests 测试框架），**业务软件包尚未开始构建**
  - 构建耗时远小于历史正常值（如 3 分钟 vs 11 分钟）→ 快速失败信号
  - 间隔数小时的重试构建以相同错误失败 → 持续性网络故障，非瞬时抖动
- **外部旁证**：atomgit.com 从外部可达 → 指向构建集群出口网络/代理配置
- **责任方**：基础设施团队

### 日志中的敏感信息
构建日志可能包含 `PASSWORD: ****`（配置回显，已掩码）、内网 IP（如 `SRV_HTTP_REPOSITORIES_HOST: 172.16.x.x`）。诊断报告中不要展开内网拓扑，引用行号和关键错误即可。

## 交叉验证要点（openEuler 场景）

1. **构建历史**：`--info` 已输出最近 6 次构建（结果 + 耗时）。快速失败 + 连续失败 + 历史上有完整成功 → 环境准备阶段故障
2. **多 Job 横向对比**：同节点池（如 `k8s-x86-*`）的其他 job 同期是否同样失败 → 集群级网络故障信号
3. **双入口互验**：viewer 不可用时尝试原生入口（或反之），两者数据同源
