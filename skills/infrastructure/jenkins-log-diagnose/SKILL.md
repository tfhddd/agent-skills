---
name: jenkins-log-diagnose
description: Jenkins 构建失败自动诊断，通过分析构建日志判断故障属于业务侧（代码 Bug / 编译错误 / 测试失败）还是基础设施侧（Jenkins 平台 / Agent 节点 / 网络 / 依赖服务 / 存储资源）。当用户提到 Jenkins 挂了、Jenkins 构建失败、流水线报错、job 失败、帮忙看下 Jenkins 日志、判断是代码问题还是环境问题等情况时立即触发。支持 Jenkins 构建 URL、本地日志文件、粘贴日志三种输入。日志读取采用窗口式渐进策略防止大量 token 消耗：先只读最后 200 行，无法定性再向上读 200 行，累计最多 1000 行。
compatibility:
  tools:
    - Bash
    - Read
    - Write
    - Glob
    - Grep
---

# Jenkins 构建日志诊断 Skill

## 使用场景

- Jenkins 构建失败，需要快速判断是**业务侧问题**（代码 Bug / 编译错误 / 测试失败）还是**基础设施侧问题**（Jenkins 平台 / Agent 节点 / 网络依赖 / 存储资源）
- 区分故障责任方：基础设施团队 vs 业务团队，避免无效流转

## 前置要求

- Python 3.8+（脚本仅依赖标准库）
- URL 模式：脚本自动探测两种后端
  - **原生 Jenkins**（`/api/json`）：可能需要认证凭据（用户名 + API Token）
  - **openEuler Log Viewer**（如 `log-ci.openeuler.openatom.cn`）：SPA + `/api/build/*` 接口，通常匿名可读
- 本地文件 / 粘贴日志模式：无额外依赖

## 核心原则

### 日志读取纪律（token 保护，最高优先级）

Jenkins 构建日志可达数百 MB，**读取方式必须严格遵守以下纪律**：

| 规则 | 内容 |
|------|------|
| 第 1 轮 | 只读取日志**最后 200 行**（`--round 1`，默认） |
| 继续条件 | 第 1 轮无法定性时，向上再读 200 行（`--round 2`），以此类推 |
| 硬上限 | 累计读取 **1000 行**（`--round 5` 为最后一轮），脚本会拒绝超限请求 |
| 超限处理 | 1000 行内仍无法定性 → 输出"无法定性"报告（见 Step 5），**禁止继续读取** |

**禁止行为**：
- 禁止一次性下载 / 输出全量日志
- 禁止用 Read / Grep 工具直接读取日志文件（本地文件也**必须**走脚本，脚本会强制窗口）
- 禁止跳轮（必须按 1 → 2 → 3 → 4 → 5 顺序读取）
- 禁止绕过 `--max 1000` 硬上限调大读取量

**例外**：用户直接粘贴的日志内容不受此限制（token 已消耗），直接分析，但优先关注尾部。

### 其他原则

- **静默执行**：只读操作（脚本调用、日志读取）直接执行，不逐步询问权限
- **先收集后输出**：定性后一次性输出完整报告，不描述诊断过程
- **责任明确**：每个问题必须标注责任方（基础设施团队 / 业务团队）
- **尾部优先**：Jenkins 失败的关键信息几乎总在日志尾部（`Finished: FAILURE` 之上），这正是尾部 200 行起步的原因

---

## Step 0：解析用户输入

| 输入类型 | 示例 | 处理方式 |
|---------|------|---------|
| Jenkins 构建 URL（原生） | `http://jenkins/job/foo/123/`、`https://ci.openeuler.openatom.cn/job/a/job/b/4/` | 走脚本 `--url`，自动探测后端（原生 `/api/json`），全量日志缓存到本地临时目录，每轮只输出窗口 |
| Jenkins 构建 URL（Log Viewer） | `https://log-ci.openeuler.openatom.cn/job/<jobPath>/<构建号>` | 走脚本 `--url`，自动探测后端（SPA + `/api/build/*`），日志经 `/api/build/log/download` 下载后同样窗口读取 |
| 本地日志文件 | `build.log` / `/tmp/console.log` | 走脚本 `--file`（同样强制窗口读取） |
| 粘贴的日志 | 直接贴的文本 | 直接分析，不触发日志读取；日志很长时优先分析尾部 |

> **重要**：无论输入形式如何，都不要试图读取全量日志。判定是否需要更多内容的标准是"能否定性"，不是"是否还有未读内容"。

---

## Step 1：收集构建信息 + 第 1 轮日志（尾部 200 行）

**URL 模式** —— 先看元数据（结果 / 节点 / 耗时 / **最近构建历史**，不读日志）：

```bash
python <skill目录>/scripts/fetch_jenkins_log.py --url "<构建URL>" --info
# 脚本自动探测后端：原生 Jenkins（/api/json）或 openEuler Log Viewer（/api/build/*）
# 原生 Jenkins 认证三选一：--user <用户> --token <token>；环境变量 JENKINS_USER/JENKINS_TOKEN；
#                          URL 内嵌 http://user:token@jenkins.example.com/job/foo/123/
# openEuler Log Viewer（log-ci.openeuler.openatom.cn/job/<jobPath>/<构建号>）通常匿名可读
```

**第 1 轮日志读取**（URL 模式和文件模式相同机制）：

```bash
# URL 模式
python <skill目录>/scripts/fetch_jenkins_log.py --url "<构建URL>"

# 文件模式
python <skill目录>/scripts/fetch_jenkins_log.py --file build.log
```

**脚本输出内容**：
- 构建元数据：结果、Agent 节点（仅原生模式）、开始时间、耗时（URL 模式）
- **最近构建历史**（最近 6 次：编号、结果、时间、耗时，标记当前构建）—— 交叉验证的关键输入，见 Step 2
- 收尾标记：`Finished: FAILURE/SUCCESS/ABORTED` 是否存在（**缺失 = 日志被截断，Agent 掉线/平台强杀的强信号**）
- 本轮特征扫描：窗口内命中的错误特征行（平台/Agent、资源/存储、网络/依赖服务、业务/代码 四组，帮助快速定性）
- 本轮窗口：带行号的原始日志片段（默认尾部 200 行）

> Jenkins 日志空行较多时，可加 `--drop-blank` 跳过空行，提高每轮信息密度（仍受 1000 行硬上限约束）。
> 加 `--filter` 可进一步做关键字过滤：仅输出特征命中行 ±2 行上下文与尾部 5 行（实测 142 行日志 → ~40 行），压缩 LLM 输入；流水线模式必须开启。

---

## Step 2：尾部快速分诊 + 历史交叉验证

根据第 1 轮窗口内容做初步定性（结合脚本头部的特征扫描）：

| 尾部现象 | 初步定性 |
|---------|---------|
| `remoting channel closed` / `Agent went offline` / `EOFException` | 基础设施 — 平台/Agent |
| 无 `Finished:` 收尾行，日志静默截断 | 基础设施 — Agent 掉线 / 平台强杀 |
| `No space left on device` / `OutOfMemoryError` / exit 137 / Killed | 基础设施 — 资源 |
| 依赖下载超时/拒绝（`Could not transfer` / `fatal: unable to access` / 502-504） | 基础设施 — 网络/依赖服务 |
| 构建启动阶段 git clone 环境仓库失败（业务代码尚未开始构建） | 基础设施 — 网络/依赖服务 |
| `[ERROR] Xxx.java:[行,列] cannot find symbol` / `COMPILATION ERROR` | 业务侧 — 编译错误 |
| `Tests run: X, Failures: Y (Y>0)` / `FAILED tests/...::...` / Traceback | 业务侧 — 测试失败 |
| groovy / Jenkinsfile 报错 / `No such property` | 配置错误 |
| `Finished: ABORTED` | 需确认：人工取消 / 上游抢占 / 超时中断 |

**历史交叉验证**（利用 `--info` 输出的最近构建历史，不定性时必须做）：

| 历史模式 | 定性含义 |
|---------|---------|
| 多次连续失败且耗时相近、错误相同 | 持续性故障（非瞬时抖动），看错误指向定性 |
| 之前成功过完整构建（耗时长）、现在快速失败 | 任务本身可正常运行 → 倾向环境/依赖变化 |
| 历史上间歇性失败（成功失败交替） | 倾向 flaky / 概率性（类型 E） |
| 从某次变更起由绿转红并持续 | 倾向业务侧引入（对比该时间点的代码变更） |

> **多个 Job 同步以相同方式失败 → 强烈的基础设施信号**（单一代码问题不会同时出现在不相关的 Job 上）。
> **快速失败 vs 正常时长失败**：耗时远小于历史正常值（如 3 分钟 vs 11 分钟）通常意味着失败在早期环境准备阶段，业务代码尚未执行。

---

## Step 3：无法定性时，渐进向上读取

第 1 轮窗口不足以定性时（如只见 `BUILD FAILURE` 汇总但无具体错误），依次向上读：

```bash
python <skill目录>/scripts/fetch_jenkins_log.py --url "<构建URL>" --round 2
# 文件模式同理：--file build.log --round 2
```

**规则**：
- 每轮向上 200 行，脚本头部会显示"累计已读 N/1000 行"
- 每读一轮必须评估一次：**可定性 → 立即停止读取，进入 Step 4**
- 第 5 轮（累计 1000 行）为最后一轮；脚本会自动拒绝第 6 轮及任何超限请求
- 1000 行后仍无法定性 → 停止读取，按 Step 5 输出"无法定性"报告

**向上读取的优先目标**（按序）：
1. 第一个 `[ERROR]` / `Traceback` / `FAILURE!`（真正的根因往往在失败汇总行的上方）
2. 依赖解析阶段的报错（版本不存在 / 源不可达，容易被后续级联错误淹没）
3. 构建启动阶段的报错（Agent 分配、workspace 创建、git clone）

---

## Step 4：根因分类

### 类型 A：基础设施侧（责任方：基础设施团队）

| 子类 | 典型信号 |
|------|---------|
| A1 平台 / Agent | `Agent went offline`、`remoting channel closed`、`ChannelClosedException`、`Jenkins is going to shut down`、无收尾行、Agent 标签不存在 |
| A2 资源 / 存储 | `No space left on device`、`OutOfMemoryError`（环境层）、exit code 137 / `Killed`、`too many open files` |
| A3 网络 / 依赖服务 | Maven/npm/pip 仓库超时（`Could not transfer ... Connection timed out`）、git 服务器不可达（`fatal: unable to access`）、DNS 解析失败、Docker registry 限流/不可达、制品库（Nexus/Artifactory）5xx |

### 类型 B：业务侧（责任方：业务团队）

| 子类 | 典型信号 |
|------|---------|
| B1 编译错误 | `COMPILATION ERROR`、`cannot find symbol`、指向业务源码文件的报错 |
| B2 单元测试失败 | `Tests run: X, Failures: Y>0`、`FAILED tests/...::...`、指向业务代码的 `AssertionError` / `NullPointerException` |
| B3 代码门禁 | checkstyle / PMD / spotbugs / eslint / pylint 违规拦截 |

### 类型 C：Jenkinsfile / 流水线配置（责任方：业务侧 CI 维护者）

信号：groovy 编译错误、`No such property`、`Missing required parameter`、`Expected to find 'xxx'`、pipeline 语法报错

### 类型 E：不确定 / 概率性

信号：无异常堆栈静默挂死、重跑有时通过、1000 行内无明确根因

**详细模式速查**：`references/common-patterns.md`

---

## Step 5：输出报告

直接在对话中输出诊断结果，格式如下：

```
# Jenkins 构建故障诊断报告

**Job**: [Job名称] #[构建号]
**节点**: [Agent 节点名，仅原生模式有]
**时间**: [开始时间]（耗时 [X]）
**历史**: [最近构建结果摘要，如 "#4 FAILURE(3分21秒) / #3 FAILURE(3分13秒) ← 当前 / #2 SUCCESS(10分52秒)"]
**日志读取**: 尾部 [N]/1000 行（第 [k] 轮定性）

## 故障定性

- **定性**: 业务侧 / 基础设施侧 / 配置错误 / 不确定
- **子类**: 编译错误 | 单测失败 | 代码门禁 | 平台/Agent | 资源存储 | 网络依赖服务
- **根因**: [一句话直接原因]
- **关键标识**: `[最关键的一行错误，带行号，如 L531: fatal: unable to access '...' Connection timed out]`
- **交叉验证**: [历史/重试/耗时维度的佐证，如 "间隔4.8小时的重试 #4 同样错误，非瞬时抖动；#2 曾完整构建成功"]
- **责任方**: 业务团队 / 基础设施团队
- **建议**: [修复 XX 文件 XX 行 / 重试构建 / 上报基础设施团队检查 XX]
```

**无法定性时的报告格式**（1000 行耗尽仍无结论）：

```
## 结论：无法定性（1000 行内未发现明确根因）

- **已排除**: [已确认不存在的信号，如"未见 Agent 掉线/磁盘/网络类特征"]
- **已发现线索**: [值得人工跟进的疑点]
- **建议**: [人工分析完整日志 / 请用户提供失败阶段截图或变更说明 / 联系平台侧核查节点状态]
```

**输出精简原则**：
- 不粘贴大段原始日志，只引用最关键的一行错误标识（带行号）
- 不描述诊断过程，直接给结论
- 同一 Job 多个问题根因相同时合并说明

**机器调用契约（流水线模式必须遵守）**：报告最后**单独一行**固定输出机器可读的分类结论：

```
CLASSIFICATION: INFRASTRUCTURE
```

取值：`INFRASTRUCTURE`（基础设施侧）/ `BUSINESS`（业务侧）/ `CONFIG`（流水线配置）/ `UNCERTAIN`（无法定性）。Keep workflow 的 action 级 `if` 条件依赖此行触发邮件/自动关闭。

---

## 机器调用模式（告警流水线集成）

Keep/Holmes 告警流水线以 headless 方式调用本 skill（输入 = 告警携带的 build URL）：

**输入**：
- 构建页面 URL（如 `https://ci.openeuler.openatom.cn/job/multiarch/job/openeuler/job/custom/job/scanoss-job/1292/`），来自告警 `labels.build_url`
- 凭据：环境变量 `JENKINS_USER` / `JENKINS_TOKEN`（不落盘、不回显）

**调用流程**（两步，输出即 LLM 输入）：

```bash
# 第 1 步：元数据 + 最近构建历史（交叉验证输入，不读日志）
python <skill目录>/scripts/fetch_jenkins_log.py --url "<build_url>" --info
# 第 2 步：关键字过滤后的第 1 轮日志
python <skill目录>/scripts/fetch_jenkins_log.py --url "<build_url>" --round 1 --filter
```

**流水线 token 纪律（比交互模式更严）**：
- 输入到 LLM 的日志内容 **≤200 行**：默认只执行第 1 轮 + `--filter`（实测通常 ~40-80 行）
- 仅当第 1 轮 `UNCERTAIN` 时，允许追加 1 轮（`--round 2 --filter`，累计 ≤400 行）；**禁止**在流水线模式用满 5 轮 / 读取全量日志
- 报告必须包含：**定性（基础设施/业务/配置/不确定）+ 子类 + 根因 + 关键标识 + 责任方 + 处理建议**，末行 `CLASSIFICATION: XXX`（见 Step 5 契约）

**实测样例（scanoss-job #1292）**：元数据显示连续 6 天准点失败、全部 0 秒耗时（定时任务启动即挂）；过滤后日志含 `JSONException ... <html>` + `404 Not Found/nginx`——插件 getToken 拿到 404 HTML，判依赖服务故障（非表面 fastjson 堆栈暗示的代码问题）。142 行日志 → 过滤输出 ~40 行。

---

## 常见误判与规避

### 1. `BUILD FAILURE` 是汇总标记，不是根因
Maven 日志尾部的 `BUILD FAILURE` 只表示构建失败，真正的根因在其上方的第一个 `[ERROR]`。同理 `Tests run: 10, Failures: 8` 需要向上找 `<<< FAILURE!` 的具体测试与断言信息。

### 2. `Could not transfer` ≠ `Could not find`
- `Could not transfer artifact ... Connection timed out` → **网络/镜像问题，类型 A3（基础设施）**
- `Could not find artifact xxx:yyy:1.2.3` → **版本不存在，类型 B/C（业务侧写错版本或依赖未发布）**

### 3. exit code 137 ≠ exit code 1
- 137 = SIGKILL（OOM Killer / 平台强杀）→ 倾向基础设施 / 资源不足
- 1 = 进程正常退出码，业务失败 → 业务侧

### 4. 业务进程 OOM 不一定是基础设施
`OutOfMemoryError` 出现在业务测试 JVM 中：若历史稳定、仅本次出现 → 可能是业务代码内存泄漏（业务侧）；若普遍伴随节点资源紧张 → 基础设施。结合该 Job 的历史成功率判断。

### 5. 测试全绿但构建失败
`Tests run: X, Failures: 0` 之后构建仍失败 → 检查收尾步骤：制品上传（Nexus/Artifactory）、归档（archiveArtifacts）、通知等。失败在收尾步骤 → 倾向基础设施（依赖服务）或流水线配置。

### 6. `Finished: ABORTED` 需要确认来源
可能是一次人工取消（不算故障）、上游并发抢占（平台调度）、或超时中断（timeout 配置或资源不足）。结合元数据中的耗时与用户描述判断。

---

## 使用示例

### 示例 1：通过 Jenkins 构建 URL 诊断

```
/jenkins-log-diagnose http://jenkins.example.com/job/order-service/1234/
```

Skill 自动执行 `--info` 获取元数据，再 `--round 1` 读尾部 200 行，进入完整诊断流程；无法定性时按轮向上读取。

### 示例 2：通过本地日志文件诊断

```
/jenkins-log-diagnose 日志文件在 /tmp/build-5678.log
```

Skill 通过脚本 `--file` 读取（不会直接 Read 全量文件），第 1 轮只读尾部 200 行，按需渐进。

### 示例 3：直接粘贴日志

```
/jenkins-log-diagnose
[INFO] Building order-service 1.2.0
...
[ERROR] Failed to execute goal ... (Connection refused)
Finished: FAILURE
```

Skill 直接分析粘贴内容，定性后输出报告，不触发日志读取。

### 示例 4：openEuler Log Viewer URL

```
/jenkins-log-diagnose https://log-ci.openeuler.openatom.cn/job/multiarch/src-openeuler/x86-64/qt6-qtlanguageserver/3
```

Skill 自动识别为 openEuler Log Viewer 后端（原生 Jenkins 探测返回 HTML SPA 时自动切换），通过 `/api/build/info` + `/api/build/list` 获取元数据与历史，通过 `/api/build/log/download` 下载日志后按窗口读取。平台细节见 `references/openeuler-log-ci.md`。

---

## 注意事项

- 本 skill 仅做故障**定性与根因分析**，不执行修复或重试操作
- URL 模式下凭据只用于本次请求，不落盘；日志缓存写入系统临时目录 `jenkins-diagnose/`，可用 `--fresh` 强制重新下载
- 构建仍在进行中（`building: true`）时日志不完整，提示用户待构建结束后再诊断，或用 `--fresh` 重新拉取
- 参考资料位于 `references/` 目录：Jenkins 常见错误模式速查、openEuler log-ci 平台专项

## 相关 Skills

- [github-action-diagnose](../github-action-diagnose/SKILL.md) — GitHub Actions CI 诊断（同类流程，昇腾 NPU 集群专项）

---

## 更新日志

### v1.2.0 (2026-10-13)
- 新增 `--filter` 关键字过滤：仅输出特征命中行 ±2 行上下文与尾部 5 行（宽泛补充词表覆盖 error/exception/404/502 等，宁可多留不可漏关键行），实测 142 行 → ~40 行
- 新增"机器调用模式（告警流水线集成）"：headless 两步调用（`--info` → `--round 1 --filter`）、流水线 token 纪律（LLM 输入 ≤200 行、仅 UNCERTAIN 追加 1 轮）
- 新增机器调用输出契约：报告末行固定 `CLASSIFICATION: INFRASTRUCTURE|BUSINESS|CONFIG|UNCERTAIN`
- 实测打通 openEuler CI（ci.openeuler.openatom.cn）网关（深路径 `/job/*/api/json`、`consoleText` basic auth 可达）

### v1.1.0 (2026-10-09)
- `--url` 双后端自动探测：原生 Jenkins（`/api/json`）+ openEuler Log Viewer（`log-ci.openeuler.openatom.cn` 的 SPA + `/api/build/*`），viewer URL 无需任何手动适配
- `--info` 输出最近构建历史（最近 6 次，编号/结果/时间/耗时，标记当前构建），支撑历史交叉验证（持续性故障 vs 瞬时抖动、快速失败 vs 正常时长失败）
- Step 2 新增历史交叉验证表与环境准备阶段失败模式（git clone 环境仓库失败 ≠ 业务问题）
- 报告格式新增"历史"与"交叉验证"字段
- 新增 `references/openeuler-log-ci.md`（openEuler log-ci 平台专项：双入口 URL、API 契约、典型故障模式）

### v1.0.0 (2026-10-09)
- 初始版本：5 步诊断流程（输入解析 → 元数据 + 尾部 200 行 → 快速分诊 → 渐进上探 → 输出报告）
- 核心机制：窗口式渐进日志读取（尾部 200 行起步，每轮上探 200 行，累计 1000 行硬上限），防止大量 token 消耗
- 支持 Jenkins URL / 本地文件 / 粘贴日志三种输入
- 故障四分类：基础设施侧（平台/Agent、资源、网络依赖）/ 业务侧（编译、测试、门禁）/ 配置 / 不确定

## 作者

- v1.0.0/v1.1.0: Infrastructure Team

## 最后更新

2026-10-13
