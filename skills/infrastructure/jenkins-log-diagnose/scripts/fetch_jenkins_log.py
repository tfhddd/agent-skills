#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_jenkins_log.py — Jenkins 构建日志窗口式读取工具（token 保护）

日志读取纪律（核心机制，防止大量 token 消耗）：
  第 1 轮：只读取日志最后 200 行（默认）
  分析不出 → 第 2 轮：向上再读 200 行（尾部第 201~400 行）
  ...
  硬上限：累计 1000 行（第 5 轮为最后一轮），超出后脚本拒绝输出

--url 自动探测两种后端（无需手动指定）：
  1. 原生 Jenkins        ：/api/json 返回 JSON（如 ci.openeuler.openatom.cn）
  2. openEuler Log Viewer ：SPA 页面 + /api/build/* 接口
     （如 log-ci.openeuler.openatom.cn/job/<jobPath>/<构建号>）

用法：
  # Jenkins 构建 URL（全量日志自动缓存到本地临时目录，每轮只输出窗口）
  python fetch_jenkins_log.py --url "http://jenkins.example.com/job/myjob/123/"
  python fetch_jenkins_log.py --url "..." --round 2        # 向上再读 200 行
  python fetch_jenkins_log.py --url "..." --info           # 元数据 + 最近构建历史，不读日志

  # 本地日志文件（同样强制窗口读取，禁止直接 Read 全量文件）
  python fetch_jenkins_log.py --file build.log
  python fetch_jenkins_log.py --file build.log --round 3

  # 认证（三种方式任选，仅原生 Jenkins 需要）：
  #   1. --user <用户名> --token <API Token>
  #   2. 环境变量 JENKINS_USER / JENKINS_TOKEN
  #   3. URL 内嵌 http://user:token@jenkins.example.com/...

选项：
  --round N     第几轮读取（默认 1）。第 k 轮输出尾部第 (k-1)*W+1 ~ k*W 行（W=--window）
  --window W    每轮行数（默认 200）
  --max M       累计读取硬上限（默认 1000，不可超过 1000）
  --info        只输出构建元数据 + 最近构建历史（交叉验证用），不读日志
  --drop-blank  取窗口前跳过空行（Jenkins 日志空行多时可提高每轮信息密度）
  --filter      关键字过滤：仅输出特征命中行±2行上下文与尾部5行（压缩 LLM 输入）
  --no-scan     关闭头部特征扫描（默认输出窗口内命中的错误特征，帮助快速定性）
  --fresh       忽略缓存重新下载（构建仍在进行、日志有增长时使用）

依赖：仅 Python 3.8+ 标准库
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

DEFAULT_WINDOW = 200
DEFAULT_MAX = 1000
HARD_CAP = 1000
SCAN_LIMIT = 5
UA = "jenkins-log-diagnose/1.0"

# 错误特征分组：仅用于输出头部的快速定性提示（非全量分析依据）
SIGNATURE_GROUPS = [
    (
        "平台/Agent",
        [
            r"agent (?:went )?offline",
            r"node (?:is )?offline",
            r"remoting channel .*closed",
            r"ChannelClosedException",
            r"EOFException",
            r"jenkins .*(?:shut ?down|restart)",
            r"(?:build|agent) (?:was )?(?:aborted|cancelled|interrupted)",
            r"no (?:nodes|agents|online) .*label",
        ],
    ),
    (
        "资源/存储",
        [
            r"no space left",
            r"disk space",
            r"out ?of ?memory",
            r"OutOfMemoryError",
            r"oom[- ]?kill",
            r"cannot allocate memory",
            r"exit code 137",
            r"\bkilled\b",
            r"too many open files",
        ],
    ),
    (
        "网络/依赖服务",
        [
            r"connection (?:timed out|refused|reset|was broken)",
            r"could not resolve",
            r"name resolution",
            r"ssl (?:handshake|certificate)",
            r"timed? ?out",
            r"\b50[234]\b",
            r"fatal: unable to access",
            r"could not (?:connect|fetch|transfer)",
            r"(?:unreachable|ECONNREFUSED|EHOSTUNREACH)",
            r"registry .*?(?:unreachable|timeout|error)",
        ],
    ),
    (
        "业务/代码",
        [
            r"compilation error",
            r"cannot find symbol",
            r"\.java:\d+",
            r"Traceback \(most recent call last\)",
            r"AssertionError",
            r"NullPointerException",
            r"(?:IndexOutOfBounds|Type|Value|Key)Error",
            r"tests? run.*?failures?: [1-9]",
            r"<<< (?:FAILURE|ERROR)!",
            r"task :.*?failed",
        ],
    ),
]

COMPILED_GROUPS = [
    (name, [re.compile(p, re.IGNORECASE) for p in pats]) for name, pats in SIGNATURE_GROUPS
]

# 关键字过滤的补充模式（--filter 用,比分类特征更宽泛:宁可多保留,不可漏掉关键行）
FILTER_EXTRA = [re.compile(p, re.IGNORECASE) for p in [
    r"error", r"exception", r"\bfail", r"fatal", r"caused by", r"traceback",
    r"not found", r"\b40[1345]\b", r"\b50[0234]\b", r"refused", r"denied",
    r"killed", r"offline", r"no space", r"timed? ?out", r"unable to",
]]

FINISHED_RE = re.compile(r"Finished:\s*(SUCCESS|UNSTABLE|FAILURE|NOT_BUILT|ABORTED)", re.IGNORECASE)

# URL 尾部需要剔除的段落（把 console/api 等子页面还原为构建页面）
_URL_TAIL_JUNK = {
    "consoletext", "console", "consolefull", "api", "json", "replay",
    "log", "config", "configure", "workspace", "artifacts", "changes",
}
_URL_CUT_MARKERS = ("/execution/", "/parsedconsole", "/parsed/", "/workflow/", "/tab/")


def normalize_build_url(raw: str) -> str:
    """清理 URL：去掉 query/fragment、子页面路径，返回构建页面基础 URL"""
    url = raw.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    for marker in _URL_CUT_MARKERS:
        i = url.find(marker)
        if i != -1:
            url = url[:i].rstrip("/")
    changed = True
    while changed:
        changed = False
        seg = url.rsplit("/", 1)[-1].lower()
        if seg in _URL_TAIL_JUNK:
            url = url[: url.rfind("/")]
            changed = True
    return url


def extract_userinfo(url: str):
    """从 URL 中拆出 user:token@（如有），返回 (干净URL, user, token)"""
    parts = urlsplit(url)
    if parts.username is not None:
        at = parts.netloc.rfind("@")
        if at != -1:
            netloc = parts.netloc[at + 1:]  # 只保留 host:port
            clean = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
            return clean, parts.username, parts.password or ""
    return url, None, None


def http_get(url: str, user: str, token: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if user and token:
        cred = base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", "Basic " + cred)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch(url: str, user: str, token: str, timeout: int = 180):
    """带错误提示的 GET，返回 (bytes|None, error|None)"""
    try:
        return http_get(url, user, token, timeout), None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, f"认证失败（HTTP {e.code}）：请通过 --user/--token 或 JENKINS_USER/JENKINS_TOKEN 提供凭据"
        if e.code == 404:
            return None, f"资源不存在（HTTP 404）：请确认 URL 是构建页面（…/job/<Job名>/<构建号>/）"
        return None, f"HTTP 错误 {e.code}"
    except urllib.error.URLError as e:
        return None, f"无法连接 Jenkins：{e.reason}"
    except Exception as e:
        return None, f"请求失败：{e}"


def cache_paths(key: str):
    d = Path(tempfile.gettempdir()) / "jenkins-diagnose"
    d.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return d / f"{key}.log", d / f"{key}.meta.json"


# ─────────────────────────────────────────────────────────────
# 数据源：原生 Jenkins / openEuler Log Viewer 双后端自动探测
# ─────────────────────────────────────────────────────────────

def parse_viewer_url(url: str):
    """解析 openEuler Log Viewer URL：<origin>/job/<jobPath>/<构建号>

    jobPath 为 /job/ 之后、最后一个纯数字段之前的所有路径段。
    返回 (origin, jobPath, build) 或 (None, None, None)
    """
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    while segs and segs[-1].lower() in _URL_TAIL_JUNK:
        segs.pop()
    if len(segs) >= 3 and segs[0] == "job" and segs[-1].isdigit():
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        return origin, "/".join(segs[1:-1]), segs[-1]
    return None, None, None


def resolve_source(raw_url: str, user: str, token: str):
    """探测 URL 属于哪种后端，返回 (source dict, 错误)

    source:
      {"kind": "jenkins", "base": ..., "user": ..., "token": ...}
      {"kind": "viewer", "origin": ..., "job_path": ..., "build": ..., "user": ..., "token": ...}
    """
    base, u, t = extract_userinfo(normalize_build_url(raw_url))
    user, token = user or u or "", token or t or ""

    # 1) 原生 Jenkins 探测：/api/json 返回 JSON
    tree = "tree=building,result,displayName,fullDisplayName,number,duration,timestamp,builtOn,url"
    data, err = fetch(f"{base}/api/json?{tree}", user, token, timeout=30)
    if data is not None:
        try:
            json.loads(data.decode("utf-8", errors="replace"))
            return {"kind": "jenkins", "base": base, "user": user, "token": token}, None
        except Exception:
            pass  # 200 但非 JSON（HTML SPA）→ 继续探测 viewer

    # 2) openEuler Log Viewer 探测：/api/build/info
    origin, job_path, build = parse_viewer_url(raw_url)
    if origin:
        q = quote(job_path, safe="")
        vdata, _ = fetch(f"{origin}/api/build/info?job={q}&build={build}", user, token, timeout=30)
        if vdata is not None:
            try:
                json.loads(vdata.decode("utf-8", errors="replace"))
                return {"kind": "viewer", "origin": origin, "job_path": job_path,
                        "build": build, "user": user, "token": token}, None
            except Exception:
                pass

    # 3) 都失败：给出可操作的错误
    if err and "认证失败" in err:
        return None, err + "（已按原生 Jenkins 识别）"
    return None, err or "无法识别的 Jenkins URL：既不是原生 Jenkins（/api/json），也不是 openEuler Log Viewer（/api/build/info）"


def _norm_history_jenkins(builds):
    out = []
    for b in builds or []:
        out.append({
            "number": b.get("number"),
            "result": b.get("result") or ("IN_PROGRESS" if b.get("building") else "UNKNOWN"),
            "timestamp_ms": b.get("timestamp") or 0,  # 原生 Jenkins timestamp 已是毫秒
            "duration_ms": b.get("duration"),
        })
    return out


def _norm_history_viewer(builds):
    out = []
    for b in builds or []:
        out.append({
            "number": b.get("buildNumber"),
            "result": b.get("result") or ("IN_PROGRESS" if b.get("building") else "UNKNOWN"),
            "timestamp_ms": b.get("timestampMs"),
            "duration_ms": b.get("durationMs"),
        })
    return out


def get_meta(source: dict, fresh: bool, with_history: bool = True):
    """获取构建元数据（含最近构建历史），缓存到本地。返回 (meta, 错误)"""
    if source["kind"] == "jenkins":
        cache_key = source["base"]
        _, meta_cache = cache_paths(cache_key)
        if meta_cache.exists() and not fresh:
            try:
                return json.loads(meta_cache.read_text(encoding="utf-8")), None
            except Exception:
                pass
        tree = "tree=building,result,displayName,fullDisplayName,number,duration,timestamp,builtOn,url"
        data, err = fetch(f"{source['base']}/api/json?{tree}", source["user"], source["token"])
        if err:
            return None, err
        try:
            meta = json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            return None, "构建元数据解析失败：URL 可能不是 Jenkins 构建页面"
        if with_history:
            # job 级 URL = 构建页面去掉尾部的构建号
            job_url = source["base"]
            if job_url.rsplit("/", 1)[-1].isdigit():
                job_url = job_url.rsplit("/", 1)[0]
            htree = "tree=builds[number,result,building,timestamp,duration]{0,6}"
            hdata, _ = fetch(f"{job_url}/api/json?{htree}", source["user"], source["token"], timeout=30)
            if hdata is not None:
                try:
                    meta["_history"] = _norm_history_jenkins(
                        json.loads(hdata.decode("utf-8", errors="replace")).get("builds"))
                except Exception:
                    pass
    else:
        q = quote(source["job_path"], safe="")
        cache_key = f"{source['origin']}|viewer|{source['job_path']}|{source['build']}"
        _, meta_cache = cache_paths(cache_key)
        if meta_cache.exists() and not fresh:
            try:
                return json.loads(meta_cache.read_text(encoding="utf-8")), None
            except Exception:
                pass
        data, err = fetch(f"{source['origin']}/api/build/info?job={q}&build={source['build']}",
                          source["user"], source["token"])
        if err:
            return None, err
        try:
            meta = json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            return None, "构建元数据解析失败（viewer /api/build/info 返回非 JSON）"
        if with_history:
            hdata, _ = fetch(f"{source['origin']}/api/build/list?job={q}&limit=6",
                             source["user"], source["token"], timeout=30)
            if hdata is not None:
                try:
                    meta["_history"] = _norm_history_viewer(
                        json.loads(hdata.decode("utf-8", errors="replace")).get("builds"))
                except Exception:
                    pass

    try:
        meta_cache.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return meta, None


def load_log_lines(source: dict, fresh: bool):
    """拉取全量日志到缓存文件，返回 (行列表, 错误)"""
    if source["kind"] == "jenkins":
        cache_key, url = source["base"], f"{source['base']}/consoleText"
    else:
        q = quote(source["job_path"], safe="")
        cache_key = f"{source['origin']}|viewer|{source['job_path']}|{source['build']}"
        url = f"{source['origin']}/api/build/log/download?job={q}&build={source['build']}"
    log_cache, _ = cache_paths(cache_key)
    if log_cache.exists() and not fresh:
        return log_cache.read_text(encoding="utf-8-sig", errors="replace").splitlines(), None
    data, err = fetch(url, source["user"], source["token"])
    if err:
        return None, err
    try:
        log_cache.write_bytes(data)
    except Exception:
        pass
    return data.decode("utf-8", errors="replace").splitlines(), None


def pick_window(lines, round_no: int, window: int, drop_blank: bool):
    """取第 round_no 轮窗口。返回 ((行号, 行文本) 列表 | None, 可选行总数)"""
    indexed = list(enumerate(lines, start=1))
    if drop_blank:
        indexed = [t for t in indexed if t[1].strip()]
    total = len(indexed)
    end = total - window * (round_no - 1)
    start = end - window
    if end <= 0:
        return None, total
    if start < 0:
        start = 0
    return indexed[start:end], total


def scan_window(window_items):
    """扫描窗口内的错误特征行，返回 [(组名, [(行号, 文本), ...]), ...]"""
    result = []
    for name, pats in COMPILED_GROUPS:
        hits = []
        for ln, text in window_items:
            if any(p.search(text) for p in pats):
                hits.append((ln, text.strip()))
                if len(hits) >= SCAN_LIMIT:
                    break
        result.append((name, hits))
    return result


def filter_window(window_items):
    """关键字过滤：仅保留特征命中行 ±2 行上下文 + 尾部 5 行，压缩 LLM 输入（token 保护）

    返回 (过滤后 [(行号, 行文本), ...], 保留行数)
    """
    n = len(window_items)
    keep = set()
    for i, (_, text) in enumerate(window_items):
        if (any(p.search(text) for _, pats in COMPILED_GROUPS for p in pats)
                or any(p.search(text) for p in FILTER_EXTRA)):
            keep.update(range(max(0, i - 2), min(n, i + 3)))
    keep.update(range(max(0, n - 5), n))
    return [window_items[i] for i in sorted(keep)], len(keep)


def find_finished(lines):
    """在日志末尾找 Finished: XXX 收尾行（Jenkins 正常结束必有）"""
    for _, text in [t for t in enumerate(lines[-10:]) if t[1].strip()]:
        m = FINISHED_RE.search(text)
        if m:
            return m.group(1).upper()
    return None


def fmt_ts(ms):
    if not ms:
        return "?"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))
    except Exception:
        return str(ms)


def fmt_duration(ms):
    if ms is None:
        return "?"
    s = int(ms) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分{sec}秒"
    if m:
        return f"{m}分{sec}秒"
    return f"{sec}秒"


def print_meta(meta):
    print("── 构建信息 " + "─" * 30)
    name = (f"{meta.get('jobPath')} #{meta.get('buildNumber')}" if meta.get("jobPath")
            else meta.get("fullDisplayName") or meta.get("displayName") or "N/A")
    print(f"  Job/构建 : {name}")
    building = meta.get("building")
    result = "进行中" if building else (meta.get("result") or "UNKNOWN")
    print(f"  结果     : {result}")
    ts = meta.get("timestampMs") or meta.get("timestamp")
    dur = meta.get("durationMs") if meta.get("durationMs") is not None else meta.get("duration")
    if "builtOn" in meta:  # 原生 Jenkins 才有节点字段（viewer 无）
        print(f"  节点     : {meta['builtOn'] or '(built-in/master)'}")
    print(f"  开始时间 : {fmt_ts(ts)}")
    print(f"  耗时     : {fmt_duration(dur)}")
    if meta.get("isPipeline") or meta.get("stages") or meta.get("children"):
        print(f"  Pipeline : 是（{len(meta.get('stages') or [])} 阶段, "
              f"{len(meta.get('children') or [])} 子构建）")
    if building:
        print("  提示     : 构建仍在进行中，日志可能不完整")

    history = meta.get("_history") or []
    if history:
        print("  最近构建 :")
        cur = meta.get("buildNumber") or meta.get("number")
        for b in history[:6]:
            mark = " ← 当前" if str(b.get("number")) == str(cur) else ""
            ts_s = fmt_ts(b.get("timestamp_ms"))
            dur_s = fmt_duration(b.get("duration_ms"))
            print(f"    #{b.get('number')} {b.get('result') or '?':12} {ts_s} ({dur_s}){mark}")


def print_report(source, lines, window_items, total, round_no, window, max_lines,
                 max_rounds, meta, scan_result, finished, filtered_count=None):
    sep = "=" * 46
    print(sep)
    print(f" Jenkins 日志窗口读取 | 第 {round_no}/{max_rounds} 轮")
    print(sep)
    if meta:
        print_meta(meta)
        print("─" * 46)
    print(f"  来源       : {source}")
    print(f"  日志总行数 : {total}")
    win_start = window_items[0][0]
    win_end = window_items[-1][0]
    if filtered_count is None:
        print(f"  本轮窗口   : 第 {win_start}~{win_end} 行（从尾部向上第 "
              f"{window * (round_no - 1) + 1}~{window * round_no} 行区段）")
    else:
        print(f"  关键字过滤 : 开启（第 {win_start}~{win_end} 行中保留 {filtered_count} 行）"
              f" — 仅特征命中±2行上下文与尾部5行")
    acc = min(window * round_no, total)
    print(f"  累计已读   : {acc}/{max_lines} 行（硬上限）")
    if finished:
        print(f"  收尾标记   : Finished: {finished}")
    else:
        print("  收尾标记   : 未找到（日志被截断，Agent 掉线/平台强杀的信号）")

    if scan_result is not None:
        print("─" * 46)
        print("  本轮特征扫描（窗口内命中，帮助定性）：")
        any_hit = False
        for name, hits in scan_result:
            for ln, text in hits:
                any_hit = True
                text = text if len(text) <= 160 else text[:157] + "..."
                print(f"    [{name}] L{ln}: {text}")
        if not any_hit:
            print("    （窗口内未命中已知特征）")

    print("─" * 46)
    if round_no == max_rounds:
        print("  提示: 已是最后一轮。若仍无法定性，输出“无法定性”报告，禁止继续读取。")
        print("─" * 46)
    print()
    for ln, text in window_items:
        print(f"[L{ln}] {text}")


def main():
    parser = argparse.ArgumentParser(
        description="Jenkins 构建日志窗口式读取（第 k 轮 = 尾部向上第 (k-1)*W+1~k*W 行）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("依赖：")[0] if __doc__ else None,
    )
    parser.add_argument("--url", help="Jenkins 构建页面 URL（…/job/<Job名>/<构建号>/）")
    parser.add_argument("--file", help="本地日志文件路径")
    parser.add_argument("--round", dest="round_no", type=int, default=1,
                        help="第几轮读取（默认 1）")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help=f"每轮行数（默认 {DEFAULT_WINDOW}）")
    parser.add_argument("--max", dest="max_lines", type=int, default=DEFAULT_MAX,
                        help=f"累计读取硬上限（默认 {DEFAULT_MAX}）")
    parser.add_argument("--info", action="store_true",
                        help="只输出构建元数据，不读日志（仅 --url 模式）")
    parser.add_argument("--user", default=os.environ.get("JENKINS_USER", ""),
                        help="Jenkins 用户名（或环境变量 JENKINS_USER）")
    parser.add_argument("--token", default=os.environ.get("JENKINS_TOKEN", ""),
                        help="Jenkins API Token（或环境变量 JENKINS_TOKEN）")
    parser.add_argument("--fresh", action="store_true",
                        help="忽略缓存重新下载")
    parser.add_argument("--drop-blank", action="store_true",
                        help="取窗口前跳过空行")
    parser.add_argument("--no-scan", action="store_true",
                        help="关闭头部特征扫描")
    parser.add_argument("--filter", action="store_true",
                        help="关键字过滤：仅输出特征命中行±2行上下文与尾部5行（压缩 LLM 输入）")
    args = parser.parse_args()

    # ── 参数校验 ─────────────────────────────────────────────
    if not args.url and not args.file:
        parser.error("必须提供 --url 或 --file")
    if args.url and args.file:
        parser.error("--url 和 --file 只能选一个")
    if args.max_lines > HARD_CAP:
        parser.error(f"累计读取硬上限为 {HARD_CAP} 行，防止大量 token 消耗")
    if args.window < 1:
        parser.error("--window 必须 >= 1")
    window = min(args.window, args.max_lines)
    max_rounds = max(1, args.max_lines // window)
    if not (1 <= args.round_no <= max_rounds):
        parser.error(
            f"--round 必须在 1~{max_rounds} 之间（每轮 {window} 行 × {max_rounds} 轮 "
            f"= 累计 {args.max_lines} 行硬上限，已拒绝继续读取）"
        )

    # ── --info 模式：只输出元数据 ─────────────────────────────
    if args.info:
        if not args.url:
            parser.error("--info 仅支持 --url 模式")
        source, err = resolve_source(args.url, args.user, args.token)
        if err:
            print(f"错误: {err}", file=sys.stderr)
            sys.exit(1)
        meta, merr = get_meta(source, args.fresh)
        if merr:
            print(f"错误: {merr}", file=sys.stderr)
            sys.exit(1)
        print(f"  后端类型 : {'原生 Jenkins' if source['kind'] == 'jenkins' else 'openEuler Log Viewer'}")
        print_meta(meta)
        return

    # ── 加载日志 ──────────────────────────────────────────────
    meta = None
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            print(f"错误: 文件不存在 {path}", file=sys.stderr)
            sys.exit(1)
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        source_desc = str(path)
    else:
        src, err = resolve_source(args.url, args.user, args.token)
        if err:
            print(f"错误: {err}", file=sys.stderr)
            sys.exit(1)
        kind_desc = f"（{'原生 Jenkins' if src['kind'] == 'jenkins' else 'openEuler Log Viewer'}）"
        meta, meta_err = get_meta(src, args.fresh)
        if meta_err:
            print(f"警告: 构建元数据获取失败 — {meta_err}", file=sys.stderr)
        lines, err = load_log_lines(src, args.fresh)
        if err:
            print(f"错误: {err}", file=sys.stderr)
            sys.exit(1)
        source_desc = (src["base"] if src["kind"] == "jenkins"
                       else f"{src['origin']} job={src['job_path']} #{src['build']}") + kind_desc

    # ── 取窗口 ────────────────────────────────────────────────
    window_items, total = pick_window(lines, args.round_no, window, args.drop_blank)
    if window_items is None:
        acc = min(window * (args.round_no - 1), total)
        print(f"已到日志开头：日志共 {total} 行，此前已读 {acc} 行，无更多内容可读。")
        print("提示：请在已有内容基础上定性，或输出“无法定性”报告。")
        return

    filtered_count = None
    if args.filter:
        window_items, filtered_count = filter_window(window_items)
    scan_result = None if args.no_scan else scan_window(window_items)
    finished = find_finished(lines)
    print_report(source_desc, lines, window_items, total, args.round_no, window,
                 args.max_lines, max_rounds, meta, scan_result, finished, filtered_count)

if __name__ == "__main__":
    main()
