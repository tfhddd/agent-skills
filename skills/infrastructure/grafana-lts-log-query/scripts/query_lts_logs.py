#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
query_lts_logs.py — 华为云 LTS 日志查询工具（经 Grafana /api/ds/query，管道符 SQL）

链路：本脚本 → Grafana 标准查询接口 /api/ds/query → LTS 管道符插件后端（代为 AK/SK
签名）→ 华为云 LTS 新引擎。调用方只需要 Grafana 的 Service Account Token（或 admin
口令），**全程不接触 LTS 的 AK/SK**。

查询纪律（token 保护，脚本层面强制）：
  1. 先 --count 探量级，再查明细（明细默认 limit 100）
  2. 时间窗口默认 60 分钟，按需放大（60m → 6h → 24h → 7d），最大 7d
  3. 明细行数硬上限 1000（--limit 不可超过）
  4. 明细默认精简投影（--fields 可自定义），禁止 select *
  5. 字段值默认截断到 300 字符（--no-trunc 关闭）

用法：
  # 量级探测（最便宜，永远先跑这个）
  python query_lts_logs.py --cluster community-lts-hk --service om-webserver --count
  python query_lts_logs.py --cluster community-lts-hk --service om-webserver --level error --count --minutes 360

  # 明细（最新 100 条，默认最近 60 分钟）
  python query_lts_logs.py --cluster community-lts-hk --service om-webserver --level error
  python query_lts_logs.py --cluster openeuler-community --service repo-watcher --minutes 1440 --limit 50

  # 大盘 URL 直接转查询（解析 var-cluster / var-service / var-level / from-to）
  python query_lts_logs.py --url "https://monitoring.osinfra.cn/grafana/d/community-service-logs/7fb54dc?orgId=1&from=now-24h&to=now&var-cluster=community-lts-hk&var-service=om-webserver&var-level=%25"

  # 枚举某集群有哪些服务（列出打了结构化字段的服务）
  python query_lts_logs.py --cluster community-lts-hk --list-services

  # 错误类型分布（按 msg 常量列聚合，error 列取值逐次不同不可聚合）
  python query_lts_logs.py --cluster community-lts-hk --service om-webserver --top-msg

  # 按请求 ID 追踪一次调用链
  python query_lts_logs.py --cluster community-lts-hk --extra "coalesce(request_id, trace_id) like '6f9c2e11-47c9-11f1-946a-0255ac100abc'"

  # 自定义管道符 SQL（必须以 `* |` 开头且带 limit）
  python query_lts_logs.py --cluster community-lts-hk --sql "* | select level, count(*) as cnt where msg is not null group by level order by cnt desc limit 20"

  # squid 访问日志（cn12-001 集群级共享流，脚本自动加 msg='squid_access' 过滤）
  python query_lts_logs.py --cluster cn12 --extra "url like '%openeuler.org%'" --minutes 60

凭据（环境变量，不落盘、不回显）：
  HOLMES_GRAFANA_TOKEN              Grafana Service Account Token（优先，Bearer）
  HOLMES_GRAFANA_USER / HOLMES_GRAFANA_PASSWORD   Basic 认证兜底（默认 admin）
  GRAFANA_URL    Grafana 基地址（默认集群内：
                 http://community-grafana.infra-monitoring-community.svc.cluster.local:3000；
                 公网入口为 https://monitoring.osinfra.cn/grafana，经 CloudWAF，脚本已带浏览器 UA）

输出：
  头部 `# ...` 注释行携带查询元数据；正文为 TSV（首行列名）；末行机器契约
  `LTS_QUERY: OK rows=N` / `LTS_QUERY: EMPTY` / `LTS_QUERY: ERROR <原因>`

依赖：仅 Python 3.8+ 标准库
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_GRAFANA_URL = "http://community-grafana.infra-monitoring-community.svc.cluster.local:3000"
PUBLIC_GRAFANA_URL = "https://monitoring.osinfra.cn/grafana"
# CloudWAF 拦截默认 curl/urllib UA，公网入口必须带浏览器 UA
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DATASOURCE_TYPE = "hw-hws-lts-grafana-datasource-plugin-pipe"

HARD_ROW_CAP = 1000        # 明细行数硬上限（与 jenkins-log-diagnose 的 1000 行纪律对齐）
HARD_WINDOW_CAP = 7 * 24 * 60  # 时间窗硬上限 7d（分钟）
DEFAULT_LIMIT = 100
DEFAULT_MINUTES = 60
DEFAULT_MAX_FIELD_CHARS = 300

# 社区服务日志（LTS）可查的 6 条流。键 = Grafana 数据源 uid。
# 与 helm-chart-value common/grafana/prod/values-dashboards.yaml 的数据源定义同步维护。
# structured_since: 结构化字段（level/msg/service/...）可查的时间边界，更早的行这些列是空的。
STREAMS = {
    "community-lts-openeuler": {
        "aliases": ["openeuler-community", "bj", "beijing", "机器人流"],
        "cluster": "openeuler-community",
        "region": "cn-north-4",
        "groupId": "84ef9408-c1cc-49ba-9a19-0350e828ae3d",
        "streamId": "7147e18a-b399-4b06-9413-05ce094e14db",
        "note": "cn-north-4 机器人流（12 个机器人，logrus component / obs-sdk service 双日志器方言）",
        "structured_since": "2026-10-14",
    },
    "community-lts-hk": {
        "aliases": ["openeuler-hk-cce-x86", "hk"],
        "cluster": "openeuler-hk-cce-x86",
        "region": "ap-southeast-1",
        "groupId": "82afc380-9a38-421f-8bc7-9f504ad87a35",
        "streamId": "b0005844-51f3-4ec7-91cf-c711bae5b84c",
        "note": "香港 26 服务集群 stdout（10 个带结构化字段；doc-search/api-cve/cve-manager 为纯文本不可按字段过滤）",
        "structured_since": "2026-10-15 10:00 +08",
    },
    "community-lts-hk-preview": {
        "aliases": ["infra-hk-preview-cluster-003", "hk-preview", "preview"],
        "cluster": "infra-hk-preview-cluster-003",
        "region": "ap-southeast-1",
        "groupId": "b207546c-290e-4e09-b6aa-a851be545617",
        "streamId": "d21e6c93-8e7f-49c8-9185-52038d7db261",
        "note": "香港多社区预览账号（hotopic-data-clean / oss-map）",
        "structured_since": None,
    },
    "community-lts-guiyang": {
        # "ascend-guiyang-prod-cluster" = Prometheus 指标里的 cluster 标签值（告警携带它），
        # LTS 流的 cluster 字段则带 -001 后缀——两个口径都收，告警→日志直查不断链
        "aliases": ["ascend-guiyang-prod-cluster-001", "ascend-guiyang-prod-cluster", "guiyang"],
        "cluster": "ascend-guiyang-prod-cluster-001",
        "region": "cn-southwest-2",
        "groupId": "c6f14fba-548c-4774-92de-0674c572bce2",
        "streamId": "06080cd6-c5f6-431b-b395-1a986763bcb8",
        "note": "Ascend/CANN 账号（om-webserver/OneID、robot-issue-manage 等；无 pod 列，服务级过滤即可）",
        "structured_since": None,
    },
    "community-lts-bj1": {
        "aliases": ["bj1-infra-cluster", "bj1"],
        "cluster": "bj1-infra-cluster",
        "region": "cn-north-1",
        "groupId": "31692633-169b-4ed4-bd14-e36f65297b16",
        "streamId": "12e3b23c-232c-4fb1-807d-6a9adb6abede",
        "note": "MindSpore/Infra 账号（cla-backend）",
        "structured_since": None,
    },
    "community-lts-cn12": {
        "aliases": ["cn12-001", "cn12", "squid"],
        "cluster": "cn12-001",
        "region": "cn-north-12",
        "groupId": "e802ceda-aec4-41ba-a016-4c19da94dc4e",
        "streamId": "3b4dc063-4f0c-4481-a81e-1eab3ca13ba8",
        "note": "CI 参与型集群共享 stdout 流（squid 访问日志在此，查询自动带 msg='squid_access'）",
        "structured_since": None,
    },
}

# 服务日志明细默认投影（ASCII 别名，避开 LTS 插件中文列名在 shell 里的转义问题；
# 投影只用两条主流量流都在索引里的列 —— cost_ms 北京流没有，带上整个查询 400 LTS.2022）
DEFAULT_SERVICE_FIELDS = (
    "date_format(from_unixtime(__time/1000, 'Asia/Shanghai'), '%Y-%m-%d %H:%i:%s') as time, "
    "level, "
    "msg, "
    "coalesce(service, component, appName) as service, "
    "coalesce(request_id, trace_id) as request_id, "
    "coalesce(logger, func) as location, "
    "error, "
    "path, "
    "status"
)
# squid 流（cn12）默认投影：结构化字段与服务日志那套完全不同
SQUID_FIELDS = (
    "date_format(from_unixtime(__time/1000, 'Asia/Shanghai'), '%Y-%m-%d %H:%i:%s') as time, "
    "duration_ms, cache_result, status, bytes, http_method, client_ip, url, username"
)


def die(msg, code=2):
    print("LTS_QUERY: ERROR %s" % msg, file=sys.stderr)
    sys.exit(code)


def sql_str(value):
    """SQL 单引号字符串转义（' → ''）"""
    return "'" + str(value).replace("'", "''") + "'"


def resolve_stream(cluster):
    """按 uid / 集群名 / 别名解析数据源，返回 (uid, info)。"""
    if cluster in STREAMS:
        return cluster, STREAMS[cluster]
    low = str(cluster).strip().lower()
    for uid, info in STREAMS.items():
        if low in [a.lower() for a in info["aliases"]] or low == info["cluster"].lower():
            return uid, info
    die("未知集群/数据源 %r。可用：%s（或其别名）" % (
        cluster, ", ".join(sorted(STREAMS))))


def parse_grafana_time(token, now_ms):
    """解析 Grafana 时间语法：now / now-24h / now-30m / now-7d / 绝对 epoch ms。"""
    token = str(token).strip()
    if token in ("now", ""):
        return now_ms
    if token.startswith("now-"):
        body = token[4:]
        try:
            num = int(body[:-1])
            unit = body[-1]
        except (ValueError, IndexError):
            die("无法解析时间 %r（支持 now / now-Ns|Nm|Nh|Nd|Nw / epoch ms）" % token)
        scale = {"s": 1000, "m": 60 * 1000, "h": 3600 * 1000,
                 "d": 24 * 3600 * 1000, "w": 7 * 24 * 3600 * 1000}
        if unit not in scale:
            die("无法解析时间 %r（单位仅支持 s/m/h/d/w）" % token)
        return now_ms - num * scale[unit]
    try:
        return int(token)
    except ValueError:
        die("无法解析时间 %r（支持 now / now-Ns|Nm|Nh|Nd|Nw / epoch ms）" % token)


def parse_dashboard_url(url):
    """从社区服务日志大盘 URL 提取 var-cluster / var-service / var-level / from / to。"""
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
    except Exception:
        die("无法解析大盘 URL: %s" % url)
    cluster = params.get("var-cluster", [None])[0]
    service = params.get("var-service", [None])[0]
    level = params.get("var-level", [None])[0]
    return cluster, service, level, params.get("from", [None])[0], params.get("to", [None])[0]


def build_auth_headers():
    """Bearer（Service Account Token）优先，Basic（admin 口令）兜底。凭据不回显。"""
    headers = {"Content-Type": "application/json", "User-Agent": BROWSER_UA}
    token = os.environ.get("HOLMES_GRAFANA_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
        return headers
    user = os.environ.get("HOLMES_GRAFANA_USER", "admin").strip() or "admin"
    password = os.environ.get("HOLMES_GRAFANA_PASSWORD", "")
    if password:
        raw = ("%s:%s" % (user, password)).encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return headers
    die("缺少凭据：请设置 HOLMES_GRAFANA_TOKEN（Service Account Token，推荐），"
        "或 HOLMES_GRAFANA_USER + HOLMES_GRAFANA_PASSWORD")


def post_query(base_url, payload, timeout):
    """POST /api/ds/query（Grafana 对该接口正常时也返回 207，需按 results 内的 error 判断）。"""
    url = base_url.rstrip("/") + "/api/ds/query"
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in (1, 2):  # 超时/5xx 重试一次
        req = urllib.request.Request(url, data=data, headers=build_auth_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            if e.code in (401, 403):
                die("认证失败（HTTP %d）：token/口令无效或已过期。%s" % (e.code, body))
            if e.code < 500 and e.code != 207:
                die("Grafana 返回 HTTP %d：%s" % (e.code, body))
            last_err = "HTTP %d %s" % (e.code, body)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = str(e)
        if attempt == 1:
            print("# 重试一次（%s）" % last_err, file=sys.stderr)
    die("查询失败：%s" % last_err)


def extract_rows(resp):
    """解析 /api/ds/query 响应（results.A.frames，Arrow-JSON：schema.fields 名 + data.values 列主序）。

    返回 [(fields, rows), ...]，每个 frame 一个表（列主序 → 行主序）。
    LTS 插件返回列的顺序不稳定（Go map 遍历），本函数按字段名对齐，不受列序影响；
    frame 之间是独立结果表，不跨 frame 合并（同名列属于不同行）。
    """
    results = (resp or {}).get("results") or {}
    result = results.get("A")
    if result is None:
        # refId 理论上固定为 A；兜底取第一个
        if results:
            result = next(iter(results.values()))
        else:
            die("响应中没有 results（响应体异常）")
    if result.get("error"):
        die("查询执行失败：%s" % str(result.get("error"))[:500])
    tables = []
    for frame in result.get("frames") or []:
        fields = [f.get("name", "col%d" % i)
                  for i, f in enumerate((frame.get("schema") or {}).get("fields") or [])]
        values = (frame.get("data") or {}).get("values") or []
        ncols = len(fields)
        rows = []
        for i in range(ncols):
            col = values[i] if i < len(values) else []
            while len(rows) < len(col):
                rows.append([""] * ncols)
            for r in range(len(col)):
                rows[r][i] = "" if col[r] is None else str(col[r])
        tables.append((fields, rows))
    return tables


def build_sql(args, stream_uid, info):
    """组装管道符 SQL。返回 (sql, description)。"""
    is_squid = stream_uid == "community-lts-cn12"

    if args.sql:
        sql = args.sql.strip()
        if not sql.startswith("* |") and not sql.startswith("*|"):
            die("自定义 SQL 必须以 `* |` 开头（管道符语法）")
        lowered = sql.lower()
        if "limit" not in lowered:
            die("自定义 SQL 必须带 limit 子句（token 保护）")
        return sql, "custom"

    if args.list_services:
        if is_squid:
            die("cn12-001 是 squid 访问日志流，没有服务维度（--list-services 仅适用于服务日志流）")
        return ("* | select coalesce(service, component) as svc "
                "where coalesce(service, component) is not null "
                "group by svc order by count(*) desc limit 200"), "list-services"

    where = []
    if is_squid:
        # cn12 是集群级共享 stdout 流（混有 volcano / npu-exporter / pypi-cache 等），
        # 查 squid 访问日志必须带 msg='squid_access' 过滤
        where.append("msg = 'squid_access'")
    else:
        if args.service:
            where.append("coalesce(service, component, appName) like %s" % sql_str(args.service))
        level = (args.level or "%").strip() or "%"
        if level.lower() in ("all", "全部", "全部级别"):
            level = "%"
        elif level.lower() == "warn":
            level = "warn%"  # 北京流只打 warning，香港流 warn/warning 都有
        where.append("level like %s" % sql_str(level))
    if args.msg:
        where.append("msg like %s" % sql_str("%" + args.msg + "%"))
    if args.extra:
        where.append("(%s)" % args.extra)
    where_sql = (" where " + " and ".join(where)) if where else ""

    if args.count:
        return "* | select count(*) as cnt%s" % where_sql, "count"
    if args.top_msg:
        return ("* | select msg, count(*) as cnt%s group by msg "
                "order by cnt desc limit 20" % where_sql), "top-msg"

    fields = args.fields or (SQUID_FIELDS if is_squid else DEFAULT_SERVICE_FIELDS)
    return ("* | select %s%s order by __time desc limit %d"
            % (fields, where_sql, args.limit)), "detail"


def main():
    ap = argparse.ArgumentParser(
        description="华为云 LTS 日志查询（经 Grafana /api/ds/query，管道符 SQL）")
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--cluster", help="集群名 / 数据源 uid / 别名（hk, bj, guiyang, cn12, ...）")
    target.add_argument("--uid", help="Grafana 数据源 uid（配合 --group-id/--stream-id 可查未收录的新流）")
    target.add_argument("--url", help="社区服务日志大盘 URL（解析 var-* 变量与时间范围）")
    ap.add_argument("--service", help="服务名（coalesce(service, component, appName) like 匹配）")
    ap.add_argument("--level", default="%", help="日志级别：info/error/warn/all（默认 all；warn 自动展开 warn%%）")
    ap.add_argument("--msg", help="msg 字段子串过滤（如定位已知错误类型）")
    ap.add_argument("--extra", help="额外的 SQL where 谓词（原样拼接，如 request_id 追踪）")
    ap.add_argument("--fields", help="自定义 select 投影（覆盖默认精简投影）")
    ap.add_argument("--minutes", type=int, default=DEFAULT_MINUTES,
                    help="时间窗口（分钟，默认 60；上限 %d）" % HARD_WINDOW_CAP)
    ap.add_argument("--from", dest="from_ms", help="起始时间（epoch ms 或 now-24h）")
    ap.add_argument("--to", dest="to_ms", help="结束时间（epoch ms 或 now，默认 now）")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help="明细行数上限（默认 %d，硬上限 %d）" % (DEFAULT_LIMIT, HARD_ROW_CAP))
    ap.add_argument("--count", action="store_true", help="只查命中行数（量级探测，最便宜）")
    ap.add_argument("--top-msg", action="store_true", help="按 msg 聚合的错误类型 Top20")
    ap.add_argument("--list-services", action="store_true", help="枚举该集群带结构化字段的服务")
    ap.add_argument("--sql", help="自定义管道符 SQL（必须 `* |` 开头且带 limit）")
    ap.add_argument("--group-id", help="配合 --uid：日志组 ID")
    ap.add_argument("--stream-id", help="配合 --uid：日志流 ID")
    ap.add_argument("--timeout", type=int, default=120, help="HTTP 超时秒数（默认 120）")
    ap.add_argument("--no-trunc", action="store_true", help="不截断长字段")
    ap.add_argument("--max-field-chars", type=int, default=DEFAULT_MAX_FIELD_CHARS,
                    help="字段值截断长度（默认 %d）" % DEFAULT_MAX_FIELD_CHARS)
    args = ap.parse_args()

    import time
    now_ms = int(time.time() * 1000)

    # 输入解析：大盘 URL 优先填充缺省项
    if args.url:
        cluster, service, level, t_from, t_to = parse_dashboard_url(args.url)
        args.cluster = args.cluster or cluster
        if not args.service and service:
            args.service = urllib.parse.unquote(service)
        if level and args.level == "%":
            args.level = urllib.parse.unquote(level)
        if t_from and not args.from_ms:
            args.from_ms = t_from
        if t_to and not args.to_ms:
            args.to_ms = t_to
        # 大盘时间范围换算成窗口；默认只保留窗口上限，不放大
        if args.from_ms and args.minutes == DEFAULT_MINUTES:
            start = parse_grafana_time(args.from_ms, now_ms)
            window_min = max(1, (now_ms - start) // 60000)
            args.minutes = min(window_min, HARD_WINDOW_CAP)

    if args.uid:
        if not (args.group_id and args.stream_id):
            die("--uid 需要同时给 --group-id 和 --stream-id（或改用 --cluster 走内置对照表）")
        stream_uid, info = args.uid, {
            "cluster": args.uid, "region": "?", "groupId": args.group_id,
            "streamId": args.stream_id, "note": "custom", "structured_since": None,
            "aliases": []}
    else:
        stream_uid, info = resolve_stream(args.cluster)

    # —— 纪律校验（token 保护，脚本层面强制）——
    if args.limit > HARD_ROW_CAP:
        die("limit %d 超过硬上限 %d（明细行数纪律）" % (args.limit, HARD_ROW_CAP))
    if args.minutes > HARD_WINDOW_CAP:
        die("时间窗口 %d 分钟超上限 %d 分钟（7d）" % (args.minutes, HARD_WINDOW_CAP))
    if (args.service or args.msg or args.extra) and args.list_services:
        die("--list-services 不能与过滤条件并用")

    to_ms = parse_grafana_time(args.to_ms, now_ms) if args.to_ms else now_ms
    if args.from_ms:
        from_ms = parse_grafana_time(args.from_ms, now_ms)
    else:
        from_ms = to_ms - args.minutes * 60 * 1000

    sql, mode = build_sql(args, stream_uid, info)
    payload = {
        "from": str(int(from_ms)),
        "to": str(int(to_ms)),
        "queries": [{
            "refId": "A",
            "datasource": {"type": DATASOURCE_TYPE, "uid": stream_uid},
            "groupId": info["groupId"],
            "streamId": info["streamId"],
            "query": sql,
            "queryText": sql,
        }],
    }

    base_url = os.environ.get("GRAFANA_URL", DEFAULT_GRAFANA_URL).strip() or DEFAULT_GRAFANA_URL
    window_min = max(1, (to_ms - from_ms) // 60000)
    print("# cluster=%s (%s, %s) window=%dm [%s ~ %s] mode=%s limit=%s"
          % (info["cluster"], stream_uid, info["region"], window_min,
             time.strftime("%Y-%m-%d %H:%M", time.localtime(from_ms / 1000)),
             time.strftime("%Y-%m-%d %H:%M", time.localtime(to_ms / 1000)),
             mode, args.limit if mode == "detail" else "-"))
    if info.get("structured_since"):
        print("# 注意：该流结构化字段自 %s 起可查，更早的行这些列为空" % info["structured_since"])

    resp = post_query(base_url, payload, args.timeout)
    tables = extract_rows(resp)

    total = sum(len(rows) for _, rows in tables)
    if not total:
        print("# 无数据（检查过滤条件/时间窗/结构化字段时间边界，纯文本服务不支持字段过滤）")
        print("LTS_QUERY: EMPTY")
        return

    for fi, (fields, rows) in enumerate(tables):
        if len(tables) > 1:
            print("# -- frame %d (%d 行) --" % (fi + 1, len(rows)))
        out = ["\t".join(fields)]
        for row in rows:
            cells = []
            for cell in row:
                cell = (cell or "").replace("\t", " ").replace("\n", "\\n").replace("\r", "")
                if not args.no_trunc and len(cell) > args.max_field_chars:
                    cell = cell[:args.max_field_chars] + "..."
                cells.append(cell)
            out.append("\t".join(cells))
        print("\n".join(out))
    print("# rows=%d" % total)
    print("LTS_QUERY: OK rows=%d" % total)


if __name__ == "__main__":
    main()
