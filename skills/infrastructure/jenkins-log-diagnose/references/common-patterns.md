# Jenkins 常见错误模式速查

分类体系与 SKILL.md 保持一致：**类型 A**（基础设施侧）/ **类型 B**（业务侧）/ **类型 C**（配置）/ **类型 E**（不确定）

---

## A1 平台 / Agent（基础设施）

### Agent 掉线（构建中途）
```
hudson.remoting.ChannelClosedException: Channel "hudson.remoting.Channel@...": java.io.EOFException
Agent went offline during the build
Connection was broken: java.io.EOFException
```
- **定性**：类型 A1
- **背景**：Agent 节点与 Jenkins master 之间的 remoting 通道断开（节点重启、网络抖动、Agent 进程 OOM）
- **判断依据**：日志在构建中途**静默截断**，无 `Finished:` 收尾行
- **处理**：上报基础设施团队检查该 Agent 节点；重试构建通常会调度到其他节点

### Jenkins 维护重启
```
Jenkins is going to shut down
Shutting down JVM
```
- **定性**：类型 A1
- **背景**：构建期间 Jenkins master 因维护/升级重启，所有运行中的构建被中断
- **处理**：重试构建

### 无可用节点
```
There are no nodes with the label [xxx]
Still waiting to schedule this build / waiting for executor
```
- **定性**：类型 A1（若 label 本身不存在则为类型 C 配置错误）
- **区分**：`no nodes with the label` → label 拼写/节点池配置问题（C）；`waiting for executor` → 资源排队（A）
- **处理**：检查 Agent 标签配置；资源不足时上报扩容

### 构建被中止
```
Finished: ABORTED
Build was aborted
```
- **定性**：需确认来源
- **可能**：人工取消（非故障）/ 上游并发抢占（平台调度，A1）/ timeout 中断（配置或资源，A2/C）

---

## A2 资源 / 存储（基础设施）

### 磁盘空间不足
```
No space left on device
Disk space is too low! ...
hudson.util.IOException2: Failed to create a temp directory
ERROR: Error cloning remote repo 'origin' (workspace 创建失败)
```
- **定性**：类型 A2
- **典型位置**：任意阶段（git clone / 编译 / 测试 / 归档）
- **处理**：上报基础设施团队清理该节点磁盘（Jenkins workspace / Maven 本地仓库 / Docker 镜像残留是常见占用源）

### OOM / 被强杀
```
java.lang.OutOfMemoryError: Java heap space   ← 环境层 JVM
Killed
Process exited with exit code 137
```
- **定性**：倾向类型 A2
- **⚠️ 注意区分**：若 OOM 出现在**业务测试进程**中且该 Job 历史稳定 → 可能是业务代码内存泄漏（类型 B），结合历史成功率判断
- exit 137 = SIGKILL（OOM Killer / 平台强杀）；exit 1 = 业务失败

### 文件句柄耗尽
```
Too many open files
```
- **定性**：类型 A2
- **处理**：上报调整节点 `ulimit -n`；排查是否有构建进程泄漏句柄

---

## A3 网络 / 依赖服务（基础设施）

### Maven 依赖下载失败（网络型）
```
Could not transfer artifact junit:junit:4.13.2 from/to nexus (...): Connection timed out
... Central is unreachable
```
- **定性**：类型 A3
- **区分**：`Could not transfer`（网络）≠ `Could not find`（版本不存在，业务侧）
- **处理**：重试；频繁出现则上报检查 Nexus/镜像源状态

### Git 服务器不可达
```
fatal: unable to access 'http://git.example.com/...': Failed to connect ... Connection refused
fatal: the remote end hung up unexpectedly / early EOF
```
- **定性**：类型 A3
- **处理**：重试；上报检查 Git 服务状态

### 构建启动阶段 clone 环境仓库失败（⚠️ 不是业务问题）
```
+ git clone --depth=1 https://atomgit.com/.../lkp-tests.git /home/jenkins/ccb1
fatal: unable to access '...': Failed to connect to xxx port 443 after 128608 ms: Connection timed out
```
- **定性**：类型 A3
- **判据**：失败发生在拉取**测试框架/环境仓库**（业务代码尚未开始构建）；构建耗时远小于历史正常值（快速失败信号）
- **⚠️ 易误判**：clone 的仓库不是业务仓库时，与业务代码质量完全无关，不要归为业务侧
- **处理**：结合构建历史交叉验证（多次快速失败 = 持续性网络故障）；上报基础设施团队检查节点出口网络

### DNS / TLS 故障
```
Could not resolve host: nexus.example.com
Temporary failure in name resolution
SSLHandshakeException / PKIX path building failed
```
- **定性**：类型 A3
- **背景**：内网 DNS 抖动、证书过期/更换
- **处理**：上报基础设施团队

### Docker registry 故障
```
toomanyrequests: You have reached your pull rate limit    ← 限流（A3）
manifest unknown / pull access denied for xxx              ← tag 不存在或无权限（业务/配置）
Error response from daemon: Get ...: dial tcp ...: connect: connection refused  ← registry 不可达（A3）
```

### 制品库（Nexus/Artifactory）上传失败（收尾阶段）
```
Failed to deploy artifacts: Could not transfer artifact ... Return code is: 502
```
- **定性**：类型 A3
- **⚠️ 易误判**：测试已全部通过，仅收尾上传失败，不要定性为业务问题

---

## B1 编译错误（业务侧）

### Java 编译失败
```
[ERROR] COMPILATION ERROR
[ERROR] /workspace/src/main/java/com/demo/OrderService.java:[42,15] cannot find symbol
[ERROR] BUILD FAILURE
```
- **定性**：类型 B1
- **关键**：`cannot find symbol` 等编译错误**指向业务源码文件** → 业务侧；若指向生成的代码/第三方库 → 需进一步判断（可能是依赖版本不兼容）

### Python 导入/语法错误
```
Traceback (most recent call last):
  File "app/main.py", line 10, in <module>
ImportError: cannot import name 'xxx' from 'yyy'
SyntaxError: invalid syntax
```
- **定性**：类型 B1（指向业务代码路径时）

---

## B2 单元测试失败（业务侧）

### Maven Surefire
```
[ERROR] Tests run: 24, Failures: 3, Errors: 1, Skipped: 0
[ERROR]   OrderServiceTest.testCreateOrder:56 expected: <true> but was: <false>
[ERROR] <<< FAILURE!
```
- **定性**：类型 B2
- **关键**：测试名 + 断言信息指向业务逻辑

### pytest
```
FAILED tests/test_order.py::test_create_order - AssertionError: assert 404 == 200
```

### 业务代码运行时异常
```
java.lang.NullPointerException
    at com.demo.service.OrderService.create(OrderService.java:88)
```
- **定性**：类型 B2（堆栈指向业务包名时）

---

## B3 代码门禁（业务侧）

```
[ERROR] Failed to execute goal ... checkstyle ... found X errors
eslint: max warnings exceeded
pylint: E1234 ... (count > threshold)
```
- **定性**：类型 B3
- **背景**：静态检查阈值拦截，通常由业务代码变更引入

---

## C Jenkinsfile / 流水线配置（业务侧 CI 维护者）

```
org.codehaus.groovy.control.MultipleCompilationErrorsException
java.lang.NoSuchMethodError: No such DSL method 'xxx'
Missing required parameter: [artifactId]
No such property: BRANCH for class: groovy.lang.Binding
Expected to find 'xxx' but found 'yyy'
```
- **定性**：类型 C
- **关键**：报错位置在 Jenkinsfile / .groovy / pipeline DSL 中，而非业务源码
- **处理**：业务侧 CI 维护者修改 Jenkinsfile

---

## E 不确定 / 概率性

```
# 无异常堆栈，进程静默挂起，最终被 timeout 终止
# 重跑有时通过
```
- **定性**：类型 E
- **常见**：flaky test（历史失败率低）、外部依赖偶发抖动、死锁
- **处理**：重跑确认概率性；记录失败点，若集中在同一测试/阶段则按 B 类深挖

---

## 定性辅助：多维度交叉验证

| 维度 | 基础设施信号 | 业务侧信号 |
|------|------------|-----------|
| 多 Job 同时失败 | ✅ 同因失败 | 通常只影响相关 Job |
| 重跑结果 | 通过 | 稳定失败 |
| 错误指向 | 环境/平台/网络/资源 | 业务源码文件/测试名 |
| 历史成功率 | 突发性下降（本来都过） | 从某次变更起持续失败 |
| 日志截断 | 无 Finished 收尾行 | 有完整的 FAILED + Finished: FAILURE |

> 单一信号不足以下结论时，至少结合两个维度交叉验证；仍无法确定时输出"无法定性"报告，不要强行归类。
