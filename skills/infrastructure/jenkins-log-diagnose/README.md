# Jenkins 构建日志诊断 Skill

> 通过分析 Jenkins 构建日志，快速判断故障属于**业务侧**（代码 Bug / 编译错误 / 测试失败）还是**基础设施侧**（Jenkins 平台 / Agent 节点 / 网络 / 依赖服务 / 存储资源），并标注责任方。

## 功能

- 支持 Jenkins 构建 URL、本地日志文件、粘贴日志三种输入
- `--url` **双后端自动探测**：原生 Jenkins（`/api/json`，如 `ci.openeuler.openatom.cn`）+ openEuler Log Viewer（SPA + `/api/build/*`，如 `log-ci.openeuler.openatom.cn`），viewer URL 无需手动适配
- **窗口式渐进日志读取（核心，token 保护）**：先只读最后 200 行，无法定性再向上读 200 行，累计最多 1000 行，脚本层面强制拒绝超限
- 输出头部附带：构建元数据（结果/节点/耗时）、**最近构建历史**（交叉验证）、`Finished:` 收尾标记检测（缺失 = 日志截断/Agent 掉线信号）、四组错误特征扫描（平台/资源/网络/业务）
- 故障四分类：基础设施侧（A1 平台/Agent、A2 资源、A3 网络依赖）/ 业务侧（B1 编译、B2 测试、B3 门禁）/ 配置（C）/ 不确定（E）
- 每个问题标注责任方：基础设施团队 / 业务团队

## 快速开始

### 前置要求

- Python 3.8+（脚本仅标准库，无第三方依赖）
- 原生 Jenkins 入口可能需要认证凭据；openEuler Log Viewer 通常匿名可读

### 1. 查看构建元数据与历史（不读日志）

```bash
python scripts/fetch_jenkins_log.py --url "http://jenkins.example.com/job/myjob/123/" --info
# openEuler viewer URL 同样直接支持：
python scripts/fetch_jenkins_log.py --url "https://log-ci.openeuler.openatom.cn/job/multiarch/src-openeuler/x86-64/foo/3" --info
# 原生 Jenkins 认证：--user <用户> --token <token>，或环境变量 JENKINS_USER / JENKINS_TOKEN，
#                    或 URL 内嵌 http://user:token@jenkins.example.com/...
```

### 2. 第 1 轮：读取尾部 200 行

```bash
python scripts/fetch_jenkins_log.py --url "http://jenkins.example.com/job/myjob/123/"
# 本地文件模式：
python scripts/fetch_jenkins_log.py --file build.log
```

### 3. 无法定性时，向上再读 200 行

```bash
python scripts/fetch_jenkins_log.py --url "..." --round 2
# 最多 --round 5（累计 1000 行），脚本拒绝任何超限读取
```

## Token 保护机制

```
第 1 轮（--round 1）: 日志尾部第   1 ~  200 行
第 2 轮（--round 2）: 日志尾部第 201 ~  400 行
第 3 轮（--round 3）: 日志尾部第 401 ~  600 行
第 4 轮（--round 4）: 日志尾部第 601 ~  800 行
第 5 轮（--round 5）: 日志尾部第 801 ~ 1000 行  ← 硬上限，超出拒绝
```

- 失败的关键信息几乎总在日志尾部（`Finished: FAILURE` 之上），尾部起步命中率高
- 全量日志（可达数百 MB）只在后台缓存到本地临时目录，**永远不会完整输出到对话**
- 短日志（< 200 行）第 1 轮即全部读出，第 2 轮会提示"已到日志开头"
- 空行多的日志可加 `--drop-blank` 提高每轮信息密度

## 故障分类

| 类型 | 子类 | 责任方 |
|------|------|--------|
| A 基础设施侧 | A1 平台/Agent、A2 资源/存储、A3 网络/依赖服务 | 基础设施团队 |
| B 业务侧 | B1 编译错误、B2 单测失败、B3 代码门禁 | 业务团队 |
| C 配置 | Jenkinsfile / 流水线语法 | 业务侧 CI 维护者 |
| E 不确定 | 概率性 / 1000 行内无根因 | 视线索而定 |

## 文件结构

```
├── SKILL.md                      # 主诊断流程（AI 执行入口）
├── README.md                     # 本文件
├── references/
│   ├── common-patterns.md        # Jenkins 常见错误模式速查 + 交叉验证表
│   └── openeuler-log-ci.md       # openEuler log-ci 平台专项（双入口 URL、API 契约、典型故障）
├── scripts/
│   └── fetch_jenkins_log.py      # 窗口式日志读取脚本（双后端探测，Python 标准库）
└── evals/
    └── evals.json                # 评测用例
```

## 脚本参数

| 参数 | 说明 |
|------|------|
| `--url <URL>` | 构建页面 URL：原生 Jenkins（…/job/\<Job\>/\<构建号\>/）或 openEuler Log Viewer（…/job/\<jobPath\>/\<构建号\>），自动探测 |
| `--file <路径>` | 本地日志文件（同样强制窗口读取） |
| `--round N` | 第几轮读取（默认 1），第 k 轮 = 尾部第 (k-1)\*200+1 ~ k\*200 行 |
| `--window W` | 每轮行数（默认 200） |
| `--max M` | 累计读取硬上限（默认 1000，不可超过） |
| `--info` | 只输出构建元数据 + 最近构建历史，不读日志 |
| `--drop-blank` | 取窗口前跳过空行 |
| `--no-scan` | 关闭头部特征扫描 |
| `--fresh` | 忽略缓存重新下载（构建进行中日志有增长时使用） |
| `--user/--token` | 原生 Jenkins 认证（或 JENKINS_USER / JENKINS_TOKEN 环境变量） |
