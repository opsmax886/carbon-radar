#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
碳雷达 · 主程序
============================================================
执行流程:
  抓取 → 关键词过滤 → 业务匹配度打分 → 去重 → AI 摘要精排
       → 标记新增 → 写入历史 → 生成前端数据 → 钉钉推送

用法:
  python src/main.py                  # 完整流程
  python src/main.py --no-ai          # 不调 AI(省 token,纯规则)
  python src/main.py --no-push        # 不推送(只更新网页数据)
  python src/main.py --all-sources    # 连禁用的源也试一遍(排查用)
  python src/main.py --limit 50       # 每天最多输出多少条
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ai as ai_mod          # noqa: E402
import notify as notify_mod  # noqa: E402
import radar                 # noqa: E402
from radar import DATA_DIR, WEB_DIR, log, now_cst, today_str  # noqa: E402

HISTORY_DIR = os.path.join(DATA_DIR, "history")
SEEN_PATH = os.path.join(DATA_DIR, "seen.json")
PUSHED_PATH = os.path.join(DATA_DIR, "pushed.json")   # 已成功推送的条目(失败时可补推)
KEEP_DAYS_MERGE = 3    # "滚动库"合并最近几天的快照
KEEP_DAYS_WEB = 14     # 前端日期切换保留多少天(只有最新一天是全量,其余是当天增量)
KEEP_DAYS_DISK = 180   # 磁盘历史保留多少天
SEEN_TTL_DAYS = 120    # 去重记忆保留多少天
MANUAL_CACHE_DAYS = 180  # 手动链接缓存保留多少天
MAX_PAYLOAD_KB = 800   # 前端数据体积上限,超过就截断(护栏,正常不会触发)


def item_key(title: str) -> str:
    return hashlib.md5(radar._norm_title(title).encode("utf-8")).hexdigest()[:16]


def load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def mark_new(items: list[dict]) -> tuple[list[dict], int]:
    """标记哪些是首次出现。

    is_new   = 今天首次见到(网页上显示"今日新增"徽章,当天保持)
    is_fresh = 本次运行才第一次发现(用于决定推不推送,避免同一天重复推送)
    """
    today = today_str()
    seen = load_json(SEEN_PATH, {})
    fresh = 0
    for it in items:
        k = item_key(it["title"])
        if k in seen:
            it["is_new"] = seen[k] == today
            it["is_fresh"] = False
            it["first_seen"] = seen[k]
        else:
            seen[k] = today
            it["is_new"] = True
            it["is_fresh"] = True
            it["first_seen"] = today
            fresh += 1
    # 清理过期记忆
    from datetime import datetime, timedelta
    cutoff = (now_cst() - timedelta(days=SEEN_TTL_DAYS)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    save_json(SEEN_PATH, seen)
    return items, fresh


def compute_stale_sources(report: list[dict], threshold_days: int) -> list[str]:
    """找出疑似停止更新的源 —— 这是本系统很关键的一个诚实功能。"""
    from datetime import datetime, timedelta
    stale = []
    limit = (now_cst() - timedelta(days=threshold_days)).strftime("%Y-%m-%d")
    for s in report:
        if not s.get("enabled"):
            continue
        if s.get("consecutive_failures", 0) >= 3:
            stale.append(f"{s['name']}(连续失败{s['consecutive_failures']}次)")
            continue
        # 连续多次"抓到 0 条"同样是失效信号:
        # 站点改版后 link_filter 一条都匹配不上,HTTP 200 但内容为空
        if s.get("zero_streak", 0) >= 2:
            stale.append(f"{s['name']}(连续{s['zero_streak']}次抓到 0 条)")
            continue
        newest = s.get("newest_item_date")
        if newest and newest < limit:
            stale.append(f"{s['name']}(最新内容停在{newest})")
    return stale


def is_healthy(s: dict) -> bool:
    """源是否健康。0 条软失败也要算不健康,否则心跳消息会误报"全部正常"。"""
    if not s.get("enabled"):
        return False
    return not s.get("consecutive_failures") and s.get("zero_streak", 0) < 2


def slim_item(it: dict) -> dict:
    """只保留前端真正会用的字段,丢弃内部字段以缩小体积。

    index.html 用到的字段就是下面这些;其余(source_status / signals /
    _dated / is_fresh / matched_kw …)是流程内部用的,传过去纯属浪费流量。
    """
    keep = ("title", "url", "date", "summary", "reason", "match", "score",
            "category", "kind", "region", "locality", "track", "track_label",
            "matched", "also_on", "is_new", "source_id", "source_name")
    out = {k: it[k] for k in keep if k in it}
    # matched 最多留 6 个,摘要最多 100 字 —— 这两项占了单条体积的大头
    if isinstance(out.get("matched"), list):
        out["matched"] = out["matched"][:6]
    if isinstance(out.get("summary"), str):
        out["summary"] = out["summary"][:100]
    # 空值不传
    return {k: v for k, v in out.items() if v not in ("", None, [], {})}


def build_web_data(today_items: list[dict], report: list[dict], stats: dict) -> dict:
    """组装前端数据。

    ⚠️ 关键设计:history 里每天存的是【完整库快照】(114 条左右),
    如果把最近 30 天全部合并进前端数据,就是 30 倍冗余 ——
    实测会长到 2.9MB,导致 git 仓库约 1GB/年、手机首屏下载 2.9MB。

    所以这里区分开:
      · 最新一天  = 完整库(前端主视图就是它)
      · 更早的日期 = 只放"当天首次出现"的条目(增量,通常几条),供日期切换用
    这样前端数据体积稳定在"一个库"的量级,不再随天数膨胀。
    """
    days = []
    files = sorted(glob.glob(os.path.join(HISTORY_DIR, "*.json")), reverse=True)[:KEEP_DAYS_WEB]

    # "滚动库":把最近 KEEP_DAYS_MERGE 天的快照按标题去重合并。
    # 为什么需要:days[0] 原来是"今天抓到的东西",万一某个源今天挂了,
    # 它昨天抓到的条目既不在今天的快照里、也不在更早日期的增量里,会从看板上凭空消失。
    merged: dict[str, dict] = {}
    for _fp in files[:KEEP_DAYS_MERGE]:
        _snap = load_json(_fp, None) or {}
        for _x in _snap.get("items", []):
            _k = item_key(_x.get("title", ""))
            if _k not in merged:
                merged[_k] = _x
    rolling = sorted(merged.values(),
                     key=lambda x: (x.get("date") or "", -x.get("score", 0)),
                     reverse=True) if merged else []

    for i, fp in enumerate(files):
        d = load_json(fp, None)
        if not d:
            continue
        date = os.path.basename(fp).replace(".json", "")
        its = d.get("items", [])
        if i == 0:
            sel, label = (rolling or its), "当前库"
        else:
            # 只留 first_seen 正好是这一天的条目(=当天新增)
            sel = [x for x in its if x.get("first_seen") == date]
            label = "当日新增"
        days.append({"date": date, "label": label,
                     "items": [slim_item(x) for x in sel[:500]]})
    return {
        "generated_at": now_cst().strftime("%Y-%m-%d %H:%M"),
        "today": today_str(),
        "days": days,
        "sources": report,
        "stats": stats,
    }


def prune_history() -> None:
    from datetime import datetime, timedelta
    cutoff = (now_cst() - timedelta(days=KEEP_DAYS_DISK)).strftime("%Y-%m-%d")
    for fp in glob.glob(os.path.join(HISTORY_DIR, "*.json")):
        if os.path.basename(fp).replace(".json", "") < cutoff:
            try:
                os.remove(fp)
            except OSError:
                pass


def site_url() -> str:
    explicit = os.environ.get("SITE_URL", "").strip()
    if explicit:
        return explicit
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner}.github.io/{name}/"
    return ""


def run_probe(cfg_sources: dict) -> int:
    """源探测:在真实运行环境里逐个测试【所有】源,包括已关闭的候选源。

    为什么必须专门做这件事:
      中文政务站对境外 IP 的表现差异极大。实测同一批源,GitHub Actions 云端
      11/11 全部成功,而本地探测却偶发 502/超时/连接中断。所以"一个源能不能用"
      必须在真正的运行环境(云端)里判定,本地结论不可信。

    探测结果会:
      1. 打印到日志
      2. 写入 data/probe_report.json
      3. 推送到钉钉(方便你不用翻日志)
    """
    settings = cfg_sources.get("settings") or {}
    fetcher = radar.Fetcher(settings)
    sources = cfg_sources.get("sources") or []
    log(f"开始探测 {len(sources)} 个源(含已关闭的候选源)…")

    results = []
    for src in sources:
        if src.get("adapter") == "manual_links":
            continue
        sid = src.get("id")
        rec = {"id": sid, "name": src.get("name", sid), "url": src.get("url", ""),
               "enabled": bool(src.get("enabled"))}
        try:
            if src.get("url_template") and src.get("keywords"):
                items = radar.fetch_template_source(src, fetcher)
                page_len = 0
            else:
                page = fetcher.get(src["url"], src.get("encoding") or None)
                page_len = len(page)
                items = radar.parse_html_list(page, src, src.get("base") or src["url"])
            hits = [i for i in items if any(k in i["title"] for k in PROBE_TENDER_KW)]
            dated = [i for i in items if i.get("date")]
            rec.update({"ok": True, "bytes": page_len, "items": len(items),
                        "tender_hits": len(hits), "dated": len(dated),
                        "samples": [i["title"] for i in (hits or items)[:3]],
                        "sample_urls": [i["url"] for i in (hits or items)[:1]]})
            flag = "✅" if hits else ("△" if items else "○")
            log(f"  {flag} {rec['name']:<26s} {page_len:>8d}字节  条目{len(items):>4d}  含招标{len(hits):>4d}")
        except Exception as e:  # noqa: BLE001
            rec.update({"ok": False, "error": str(e)[:180], "items": 0, "tender_hits": 0})
            log(f"  ✗ {rec['name']:<26s} 失败: {str(e)[:70]}")
        results.append(rec)

    ok = [r for r in results if r.get("ok")]
    usable = [r for r in ok if r.get("tender_hits", 0) > 0]
    save_json(os.path.join(DATA_DIR, "probe_report.json"),
              {"generated_at": now_cst().strftime("%Y-%m-%d %H:%M"), "results": results})

    log("=" * 62)
    log(f"探测完成:{len(results)} 个源 | 可访问 {len(ok)} | 其中抓到招标类内容 {len(usable)}")
    for r in usable:
        log(f"   可用: {r['name']}  ({r['tender_hits']} 条招标)  {r['url']}")
    log("=" * 62)

    # 推送到钉钉 —— 免得你去翻日志
    if not os.environ.get("NO_PUSH"):
        lines = ["## 碳雷达 · 源探测报告", "",
                 f"> 共测 {len(results)} 个源 ｜ 可访问 **{len(ok)}** 个 ｜ 抓到招标内容 **{len(usable)}** 个", ""]
        if usable:
            lines += ["### ✅ 确认可用(可以启用)", ""]
            for r in usable:
                lines.append(f"· **{r['name']}** ｜ 条目 {r['items']} · 招标 {r['tender_hits']} ｜ {r['url']}")
                lines.append("")
                if r.get("samples"):
                    lines.append(f"　样例:{r['samples'][0][:52]}")
                    lines.append("")
        dead = [r for r in results if not r.get("ok")]
        if dead:
            lines += ["### ❌ 无法访问", ""]
            for r in dead:
                lines.append(f"· {r['name']} ｜ {r.get('error', '')[:60]}")
                lines.append("")
        lines += ["---", "", "把上面「确认可用」的源告诉 AI,即可接入。"]
        notify_mod.push_dingtalk(f"碳雷达 · 源探测报告", "\n".join(lines))
    return 0


PROBE_TENDER_KW = ("招标", "采购", "中标", "成交", "询比", "磋商", "比选", "投标", "公告")


def main() -> int:
    ap = argparse.ArgumentParser(description="碳雷达 · 双碳招投标与资讯聚合")
    ap.add_argument("--no-ai", action="store_true", help="跳过 AI 增强")
    ap.add_argument("--no-push", action="store_true", help="跳过机器人推送")
    ap.add_argument("--all-sources", action="store_true", help="包含已禁用的源")
    ap.add_argument("--limit", type=int, default=200, help="每日最多输出条数")
    ap.add_argument("--min-score", type=int, default=15, help="低于此分不输出")
    ap.add_argument("--probe", action="store_true",
                    help="源探测模式:测试所有源(含已关闭候选)的可用性,不发正常推送")
    args = ap.parse_args()

    # ---- 源探测模式:只测可用性,不跑正常流程 ----
    if args.probe:
        log("=" * 62)
        log("碳雷达 · 源探测模式")
        log("=" * 62)
        return run_probe(radar.load_yaml("sources.yml"))

    log("=" * 62)
    log("碳雷达启动")
    log("=" * 62)

    today = today_str()   # 全程只用这一个"今天",避免跨零点时各处口径不一致
    cfg_sources = radar.load_yaml("sources.yml")
    kw = radar.load_yaml("keywords.yml")
    threshold = int((cfg_sources.get("settings") or {}).get("stale_alert_days", 14))

    result = radar.build(cfg_sources, kw, force_all=args.all_sources)
    items = result["items"]

    # 打分过滤
    before = len(items)
    items = [i for i in items if i.get("score", 0) >= args.min_score]
    if len(items) < before:
        log(f"按最低分 {args.min_score} 过滤掉 {before - len(items)} 条")
    items.sort(key=lambda x: -x.get("score", 0))
    items = items[: args.limit]

    # AI 增强
    if not args.no_ai:
        log("AI 摘要与精排…")
        items, ai_stats = ai_mod.enrich(items)
        if ai_stats.get("ai_enabled") and not ai_stats.get("ai_ok"):
            log("⚠ AI 完全没生效(0 批成功)—— 请检查 DeepSeek 余额或 API Key")
    else:
        log("已跳过 AI 增强(使用规则打分)")
        ai_stats = {"ai_enabled": False, "ai_skipped": len(items)}
        items = [{**i, "summary": i["title"][:60], "match": i.get("score", 0),
                  "reason": i.get("track_label", ""), "ai": False} for i in items]

    # 标记新增
    items, fresh = mark_new(items)
    log(f"其中首次出现(新增) {fresh} 条")

    # 写历史
    save_json(os.path.join(HISTORY_DIR, f"{today}.json"),
              {"date": today, "items": items, "generated_at": result["generated_at"]})
    prune_history()

    stale = compute_stale_sources(result["sources"], threshold)
    stats = {
        "today_total": len(items),
        "new": fresh,
        "raw_count": result["raw_count"],
        "dropped_count": result["dropped_count"],
        "total": len(items),
        "stale_sources": stale,
        "sources_ok": len([s for s in result["sources"] if is_healthy(s)]),
        "ai_enabled": bool(ai_stats.get("ai_enabled")),
        "ai_ok": ai_stats.get("ai_ok", 0),
        "ai_failed": ai_stats.get("ai_failed", 0),
        "tenders": len([i for i in items if i.get("category") == "tender"]),
        "policies": len([i for i in items if i.get("category") in ("policy", "methodology")]),
        "news": len([i for i in items if i.get("category") == "news"]),
    }

    # 生成前端数据:同时输出 data.json(接口用)和 data.js(file:// 双击也能用)
    payload = build_web_data(items, result["sources"], stats)

    # 体积护栏:正常不会触发(最新一天全量 + 其余增量,约 100KB 量级)。
    # 万一将来条目暴涨,这里兜底截断,避免把几百 KB 甚至 MB 级数据推给手机。
    def _size_kb(p) -> float:
        return len(json.dumps(p, ensure_ascii=False).encode("utf-8")) / 1024

    size = _size_kb(payload)
    if size > MAX_PAYLOAD_KB and payload["days"]:
        log(f"⚠ 前端数据 {size:.0f}KB 超过上限 {MAX_PAYLOAD_KB}KB,开始截断")
        keep = payload["days"][:1]
        while keep and _size_kb({**payload, "days": keep}) > MAX_PAYLOAD_KB:
            keep[0]["items"] = keep[0]["items"][: int(len(keep[0]["items"]) * 0.8)] or []
            if not keep[0]["items"]:
                break
        payload["days"] = keep
        payload["stats"]["truncated"] = True
        size = _size_kb(payload)
        log(f"  截断后 {size:.0f}KB")

    save_json(os.path.join(WEB_DIR, "data.json"), payload)
    os.makedirs(WEB_DIR, exist_ok=True)
    with open(os.path.join(WEB_DIR, "data.js"), "w", encoding="utf-8") as f:
        f.write("// 自动生成,请勿手工编辑\nwindow.__CARBON_DATA__ = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";\n")

    log(f"数据已写入 web/data.js 与 web/data.json({size:.0f}KB,{len(payload['days'])} 天)")

    # 推送窗口:只推"时间窗内 + 本次新发现"的条目。
    #
    # 为什么要时间窗:首次运行(或清空记忆后)会把库里所有内容都当"新增",
    # 而库里可能存着 120 天内的旧标讯,一次推几十上百条没法看。
    # 加上窗口后效果正好是用户要的:
    #   首次运行 → 推近 7 天的全部内容(回填)
    #   之后每天 → 只推当天真正新出现的(增量)
    # 无日期的条目不受窗口限制(否则它们永远推不出去)。
    push_window = int((cfg_sources.get("settings") or {}).get("push_max_age_days", 7) or 0)
    stats["push_window_days"] = push_window

    push_failed = False
    if not args.no_push:
        log("推送机器人…")
        # 用 pushed.json 而不是 is_fresh 判断"该不该推":
        # is_fresh 只说明"今天第一次见到",一旦落盘就再也推不出去;
        # 如果当天推送失败(webhook 失效/网络抖动),那些条目就永久丢了。
        # 改成记录"已成功推送",失败时下次自动补推。
        from datetime import timedelta as _td
        pushed = load_json(PUSHED_PATH, {})
        push_items = [i for i in items if item_key(i["title"]) not in pushed]
        if push_window > 0:
            _cutoff = (now_cst() - _td(days=push_window)).strftime("%Y-%m-%d")
            _before = len(push_items)
            push_items = [i for i in push_items
                          if not i.get("date") or i["date"] >= _cutoff]
            if _before > len(push_items):
                log(f"  按「近 {push_window} 天」窗口过滤掉 {_before - len(push_items)} 条旧信息")
        push_items.sort(key=lambda x: (x.get("date") or "", -x.get("match", x.get("score", 0))),
                        reverse=True)
        # 头部要显示"本条消息包含多少条",而不是"今天新发现多少条" ——
        # 两者在窗口过滤后会不一致(比如新发现 96 条、窗口内只有 43 条)
        stats["push_count"] = len(push_items)
        if push_items:
            ok = notify_mod.push_all(push_items, today, stats, site_url())
        elif (cfg_sources.get("settings") or {}).get("push_heartbeat", True):
            # 没有新增也要报个平安 —— 否则你无法区分"今天真没标讯"和"系统挂了"。
            # 但如果今天已经成功推送过(说明是备用时段又被触发了一次),
            # 就不要再发,免得你一天收到两条消息。
            if any(v == today for v in pushed.values()):
                ok, push_items = True, []
                log("  今日已推送过,跳过平安消息(备用时段重复运行)")
            else:
                ok = notify_mod.push_heartbeat(today, stats, site_url())
        else:
            ok, push_items = True, []
            log("今日无新增,按配置不推送")

        if ok and push_items:
            for i in push_items:
                pushed[item_key(i["title"])] = today
            _pcut = (now_cst() - _td(days=180)).strftime("%Y-%m-%d")
            pushed = {k: v for k, v in pushed.items() if v >= _pcut}
            save_json(PUSHED_PATH, pushed)
            log(f"  已记录 {len(push_items)} 条为已推送(下次不会重复)")
        elif not ok:
            push_failed = True
            log("✗ 推送失败 —— 本次不记录已推送,下次运行会自动补推")
    else:
        log("已跳过推送")

    log("=" * 62)
    log(f"完成 | 今日 {len(items)} 条 | 新增 {fresh} 条 | "
        f"标讯 {stats['tenders']} 政策 {stats['policies']} 动态 {stats['news']}")
    if stale:
        log("⚠ 疑似停更的源: " + "；".join(stale))
    log("=" * 62)
    # 推送失败让 workflow 变红 —— 否则钉钉静默而 Actions 全绿,没人会发现
    return 1 if push_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
