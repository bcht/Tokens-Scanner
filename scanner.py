"""采集 Claude Code / Claude 桌面版 / DeepSeek Harness / Codex 的 token 用量。

拆成三步,是为了让缓存能按文件粒度工作:
    发现文件 -> 解析单个文件(可缓存) -> 聚合

一条记录长这样:
    [来源, 模型, 日期, 小时, 输入, 输出, 缓存读, 缓存写, 会话标识, 消息id]

来源写在记录里,因为 Claude Code 和桌面版共用同一批文件、同一套解析,
只有逐条的 entrypoint 能区分它们。
日期取本地天、另存一个本地小时(0-23):热力图按天,时段分布按小时。
小时为 HOUR_UNKNOWN 表示未知 —— 升级前存下的旧记录没有这个字段、补不出来,
它们照常计入日统计,只是不进时段分布。
消息 id 留着是为了去重:Claude 的同一条 assistant 消息会在同一个文件里被原样
写多遍,必须全局去重,否则用量翻倍。

路径都可用环境变量覆盖:CLAUDE_CONFIG_DIR / DSH_HOME / CODEX_HOME。
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import logging
import os
import time
from collections import defaultdict

from cache import HOUR_UNKNOWN, session_key_for

log = logging.getLogger("tokens.scanner")

SOURCES = [
    ("claude_code", "Claude Code"),
    ("claude_desktop", "Claude Desktop"),
    ("dsh", "DeepSeek Harness"),
    ("codex", "Codex"),
]
SOURCE_LABELS = dict(SOURCES)

F_SOURCE, F_MODEL, F_DAY, F_HOUR, F_IN, F_OUT, F_READ, F_WRITE, F_SESSION, F_MSG = range(10)


# --------------------------------------------------------------------- 工具


def _home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def _iter_json_lines(path):
    """逐行读 JSONL,坏行直接跳过而不是让整个文件失败。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (ValueError, TypeError):
                    continue
    except OSError:
        return


def _read_jsonl_auto(path):
    """DSH 的会话是 zstd 压的 JSONL,但也容忍未压缩的版本。"""
    if path.endswith(".zstd"):
        try:
            from compression import zstd  # Python 3.14 标准库
        except ImportError:  # pragma: no cover - 老版本 Python 的回退
            import zstandard as zstd  # type: ignore

        try:
            with open(path, "rb") as fh:
                raw = zstd.decompress(fh.read())
        except Exception:
            return []
        out = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except (ValueError, TypeError):
                continue
        return out
    return list(_iter_json_lines(path))


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone()


def _ms_to_dt(value):
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(value / 1000.0, tz=dt.timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def _as_int(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _day(moment):
    return moment.strftime("%Y-%m-%d")


# ----------------------------------------------------------------- 发现文件


def claude_files():
    """~/.claude/projects/<项目>/<会话>.jsonl —— 两个来源共用"""
    root = os.environ.get("CLAUDE_CONFIG_DIR")
    projects = os.path.join(root, "projects") if root else _home(".claude", "projects")
    return glob.glob(os.path.join(projects, "*", "*.jsonl"))


def dsh_files():
    """~/.dsh/sessions/<项目>/<会话>/session.v4.jsonl.zstd"""
    root = os.environ.get("DSH_HOME") or _home(".dsh")
    return glob.glob(os.path.join(root, "sessions", "*", "*", "session*.jsonl*"))


def codex_files():
    """~/.codex/sessions/**/*.jsonl"""
    root = os.environ.get("CODEX_HOME") or _home(".codex")
    return glob.glob(os.path.join(root, "sessions", "**", "*.jsonl"), recursive=True)


# ----------------------------------------------------------------- 解析文件


def parse_claude_file(path):
    session = os.path.splitext(os.path.basename(path))[0]
    out = []
    for entry in _iter_json_lines(path):
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        model = message.get("model") or "未知"
        if model == "<synthetic>":
            continue
        when = _parse_iso(entry.get("timestamp"))
        if when is None:
            continue
        source = (
            "claude_desktop"
            if entry.get("entrypoint") == "claude-desktop-3p"
            else "claude_code"
        )
        out.append([
            source,
            model,
            _day(when),
            when.hour,
            _as_int(usage.get("input_tokens")),
            _as_int(usage.get("output_tokens")),
            _as_int(usage.get("cache_read_input_tokens")),
            _as_int(usage.get("cache_creation_input_tokens")),
            session,
            message.get("id"),
        ])
    return out


def parse_dsh_file(path):
    """usage 挂在 assistant/message 上,是单次 API 调用的增量值,直接相加即可。

    子代理会话是独立文件、与父会话无内容重叠,所以跨文件累加不会重复计。
    """
    session = os.path.basename(os.path.dirname(path))
    out = []
    model = "未知"
    for event in _read_jsonl_auto(path):
        kind = event.get("type")
        if kind == "request/header":
            header = (event.get("data") or {}).get("header") or {}
            config = header.get("config") or {}
            if config.get("model"):
                model = config["model"]
        elif kind == "assistant/message":
            usage = (event.get("data") or {}).get("usage")
            if not isinstance(usage, dict):
                continue
            when = _ms_to_dt(event.get("time")) or _parse_iso(event.get("createdAt"))
            if when is None:
                continue
            out.append([
                "dsh",
                model,
                _day(when),
                when.hour,
                _as_int(usage.get("inputTokens")),
                _as_int(usage.get("outputTokens")),
                _as_int(usage.get("cacheReadTokens")),
                _as_int(usage.get("cacheWriteTokens")),
                session,
                None,
            ])
    return out


def _codex_usage(usage):
    """Codex 的 input_tokens 已包含 cached_input_tokens,拆开避免重复计。"""
    raw_input = _as_int(usage.get("input_tokens"))
    cached = _as_int(usage.get("cached_input_tokens"))
    return max(0, raw_input - cached), _as_int(usage.get("output_tokens")), cached, 0


def parse_codex_file(path):
    """本机没装 Codex,这段按已知的 token_count 事件格式写的,未经真实数据验证。

    优先用 last_token_usage(单轮增量);没有的话对总累计值求前后差值,
    求和后总量仍然正确。
    """
    session = os.path.splitext(os.path.basename(path))[0]
    out = []
    model = "未知"
    previous = None
    for entry in _iter_json_lines(path):
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("model"):
            model = payload["model"]
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        when = _parse_iso(entry.get("timestamp")) or _parse_iso(payload.get("timestamp"))
        if when is None:
            continue

        last = info.get("last_token_usage")
        if isinstance(last, dict):
            delta = _codex_usage(last)
        else:
            total = info.get("total_token_usage")
            if not isinstance(total, dict):
                continue
            current = _codex_usage(total)
            delta = current if previous is None else tuple(
                max(0, c - p) for c, p in zip(current, previous)
            )
            previous = current

        out.append([
            "codex", model, _day(when), when.hour,
            delta[0], delta[1], delta[2], delta[3], session, None,
        ])
    return out


# ------------------------------------------------------------------- 聚合


def _blank():
    return [0, 0, 0, 0, 0]  # 输入 / 输出 / 缓存读 / 缓存写 / 调用次数


def aggregate(records):
    totals = defaultdict(_blank)
    daily = defaultdict(lambda: defaultdict(_blank))
    model_daily = defaultdict(lambda: defaultdict(_blank))
    # 时段分布另开一张:键是 (天, 小时),前端才能跟着时间范围裁
    hour_daily = defaultdict(lambda: defaultdict(_blank))
    days_seen = defaultdict(set)

    seen_message_ids = set()
    for record in records:
        message_id = record[F_MSG]
        if message_id:
            # 同一条消息会被写进多个文件,只认第一次见到的
            if message_id in seen_message_ids:
                continue
            seen_message_ids.add(message_id)

        source = record[F_SOURCE]
        day = record[F_DAY]
        hour = record[F_HOUR]
        model = record[F_MODEL]
        values = (record[F_IN], record[F_OUT], record[F_READ], record[F_WRITE])

        buckets = [daily[source][day], model_daily[source][(model, day)], totals[source]]
        if hour >= 0:
            buckets.append(hour_daily[source][(day, hour)])
        for bucket in buckets:
            for index, value in enumerate(values):
                bucket[index] += value
            bucket[4] += 1

        days_seen[source].add(day)

    sources = []
    for source_id, label in SOURCES:
        total = totals[source_id]
        days = sorted(days_seen[source_id])
        sources.append({
            "id": source_id,
            "label": label,
            "calls": total[4],
            "active_days": len(days),
            "first": days[0] if days else None,
            "last": days[-1] if days else None,
        })

    all_days = sorted({day for source_id, _ in SOURCES for day in days_seen[source_id]})

    return {
        "sources": sources,
        "daily": {sid: dict(sorted(daily[sid].items())) for sid, _ in SOURCES},
        "modelDaily": {
            sid: [
                [model, day] + bucket
                for (model, day), bucket in sorted(
                    model_daily[sid].items(), key=lambda kv: (kv[0][1], kv[0][0])
                )
            ]
            for sid, _ in SOURCES
        },
        "hourDaily": {
            sid: [
                [day, hour] + bucket
                for (day, hour), bucket in sorted(hour_daily[sid].items())
            ]
            for sid, _ in SOURCES
        },
        "totals": {sid: totals[sid] for sid, _ in SOURCES},
        "firstDay": all_days[0] if all_days else None,
        "lastDay": all_days[-1] if all_days else None,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "warnings": [],
    }


# ------------------------------------------------------------------- 入口


def collect(cache=None):
    """扫一遍所有来源并聚合。传了 cache 就按文件增量走。

    存档里的记录会全部参与统计,包括那些磁盘上已经不存在的会话文件 ——
    否则你清理掉旧会话之后,那些天的用量会从图上凭空消失。

    返回 (payload, stats);stats 里是命中情况,给界面显示用。
    """
    stats = {
        "total_files": 0,
        "reused": 0,
        "parsed": 0,
        "archived": 0,
        "seconds": 0.0,
        "cache_path": None,
        "cache_error": None,
        "db_backup": None,
        "db_migrated": 0,
    }
    started = dt.datetime.now()

    cached = {}
    if cache is not None:
        if cache.conn is None:
            cache.open()
        stats["cache_path"] = cache.path
        stats["cache_error"] = cache.error
        # 版本升级时的另存与搬迁。只报一次,免得每次轮询都刷
        stats["db_backup"] = getattr(cache, "archived_to", None)
        stats["db_migrated"] = getattr(cache, "migrated", 0)
        cache.archived_to = None
        cache.migrated = 0
        cached = cache.entries()

    files = [
        (parse_claude_file, path) for path in claude_files()
    ] + [
        (parse_dsh_file, path) for path in dsh_files()
    ] + [
        (parse_codex_file, path) for path in codex_files()
    ]
    stats["total_files"] = len(files)
    log.debug("发现 %d 个会话文件", len(files))

    records = []
    errors = []
    seen = set()

    for parser, path in files:
        try:
            stat = os.stat(path)
        except OSError:
            log.debug("读不到 %s,跳过", path)
            continue

        key = session_key_for(parser.__name__, path)
        seen.add(key)

        hit = cached.get(key)
        if (hit and hit[0] == path
                and hit[1] == stat.st_mtime and hit[2] == stat.st_size):
            stats["reused"] += 1
            records.extend(hit[3])
            log.debug("复用 %s(%d 条记录)", path, len(hit[3]))
            continue

        started_file = time.perf_counter()
        try:
            parsed = parser(path)
        except Exception as exc:  # 单个文件出错不该拖垮整个程序
            errors.append(f"{os.path.basename(path)} 解析失败:{exc}")
            log.warning("解析失败 %s: %s", path, exc, exc_info=True)
            continue

        stats["parsed"] += 1
        if cache is not None:
            cache.put(key, path, parser.__name__, stat.st_mtime, stat.st_size, parsed)
        records.extend(parsed)
        log.debug(
            "新解析 %s(%d 条记录,%.1f ms)",
            path, len(parsed), (time.perf_counter() - started_file) * 1000,
        )

    # 存档里那些磁盘上已经没有的会话,记录照样参与统计
    for key, (_path, _mtime, _size, archived) in cached.items():
        if key not in seen:
            stats["archived"] += 1
            records.extend(archived)
            log.debug("已归档 %s(%d 条记录,源文件不在了)", key, len(archived))

    if cache is not None:
        cache.commit()

    payload = aggregate(records)
    payload["warnings"] = errors
    payload["cache"] = {
        "files": stats["total_files"],
        "reused": stats["reused"],
        "parsed": stats["parsed"],
        "archived": stats["archived"],
        "from_cache": stats["reused"] > 0,
    }
    stats["seconds"] = (dt.datetime.now() - started).total_seconds()

    log.info(
        "扫描完成: %.3fs  记录 %d 条  会话文件 %d(复用 %d / 新解析 %d / 已归档 %d)"
        "  合计 %s tokens",
        stats["seconds"], len(records), stats["total_files"],
        stats["reused"], stats["parsed"], stats["archived"],
        format(sum(sum(payload["totals"][s][:4]) for s in payload["totals"]), ","),
    )
    for warning in errors:
        log.warning("解析告警: %s", warning)
    return payload, stats


if __name__ == "__main__":
    import sys

    # Windows 控制台默认用 GBK 输出,中文会被写坏,重定向到文件时尤其明显
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    from cache import Cache

    if "--json" in sys.argv:
        payload, _ = collect(Cache())
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    else:
        payload, info = collect(Cache())
        grand = 0
        for source in payload["sources"]:
            values = payload["totals"][source["id"]]
            subtotal = sum(values[:4])
            grand += subtotal
            print(
                f"{source['label']:<18} {subtotal:>14,}  "
                f"调用 {source['calls']:>6,}  活跃 {source['active_days']:>4} 天"
            )
        print("-" * 78)
        print(f"{'合计':<18} {grand:>14,}")
        print(
            f"文件 {info['total_files']}  复用 {info['reused']}  新解析 {info['parsed']}"
            f"  已归档 {info['archived']}  耗时 {info['seconds']:.3f}s"
        )
        print("数据库:", info["cache_path"])
        for warning in payload["warnings"]:
            print("警告:", warning)
