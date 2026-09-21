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
KEEP_DAYS_WEB = 30     # 前端保留最近多少天
KEEP_DAYS_DISK = 180   # 磁盘历史保留多少天
SEEN_TTL_DAYS = 120    # 去重记忆保留多少天


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
        newest = s.get("newest_item_date")
        if newest and newest < limit:
            stale.append(f"{s['name']}(最新内容停在{newest})")
    return stale


def build_web_data(today_items: list[dict], report: list[dict], stats: dict) -> dict:
    """把最近 KEEP_DAYS_WEB 天的历史合并成前端数据。"""
    days = []
    files = sorted(glob.glob(os.path.join(HISTORY_DIR, "*.json")), reverse=True)[:KEEP_DAYS_WEB]
    for fp in files:
        d = load_json(fp, None)
        if not d:
            continue
        days.append({"date": os.path.basename(fp).replace(".json", ""),
                     "items": d.get("items", [])[:500]})
    payload = {
        "generated_at": now_cst().strftime("%Y-%m-%d %H:%M"),
        "today": today_str(),
        "days": days,
        "sources": report,
        "stats": stats,
    }
    return payload


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
        items = ai_mod.enrich(items)
    else:
        log("已跳过 AI 增强(使用规则打分)")
        items = [{**i, "summary": i["title"][:60], "match": i.get("score", 0),
                  "reason": i.get("track_label", ""), "ai": False} for i in items]

    # 标记新增
    items, fresh = mark_new(items)
    log(f"其中首次出现(新增) {fresh} 条")

    # 写历史
    today_items = [i for i in items if i.get("first_seen") == today_str()]
    save_json(os.path.join(HISTORY_DIR, f"{today_str()}.json"),
              {"date": today_str(), "items": items, "generated_at": result["generated_at"]})
    prune_history()

    stale = compute_stale_sources(result["sources"], threshold)
    stats = {
        "today_total": len(items),
        "new": fresh,
        "raw_count": result["raw_count"],
        "dropped_count": result["dropped_count"],
        "total": len(items),
        "stale_sources": stale,
        "sources_ok": len([s for s in result["sources"]
                           if s.get("enabled") and not s.get("consecutive_failures")]),
        "tenders": len([i for i in items if i.get("category") == "tender"]),
        "policies": len([i for i in items if i.get("category") in ("policy", "methodology")]),
        "news": len([i for i in items if i.get("category") == "news"]),
    }

    # 生成前端数据:同时输出 data.json(接口用)和 data.js(file:// 双击也能用)
    payload = build_web_data(items, result["sources"], stats)
    save_json(os.path.join(WEB_DIR, "data.json"), payload)
    os.makedirs(WEB_DIR, exist_ok=True)
    with open(os.path.join(WEB_DIR, "data.js"), "w", encoding="utf-8") as f:
        f.write("// 自动生成,请勿手工编辑\nwindow.__CARBON_DATA__ = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";\n")

    log(f"数据已写入 web/data.js 与 web/data.json(共 {len(payload['days'])} 天历史)")

    # 推送:只推"本次运行新发现"的,避免同一天多次运行重复打扰
    if not args.no_push:
        log("推送机器人…")
        push_items = [i for i in items if i.get("is_fresh")]
        if push_items:
            notify_mod.push_all(push_items, today_str(), stats, site_url())
        elif (cfg_sources.get("settings") or {}).get("push_heartbeat", True):
            # 没有新增也要报个平安 —— 否则你无法区分"今天真没标讯"和"系统挂了"
            notify_mod.push_heartbeat(today_str(), stats, site_url())
        else:
            log("今日无新增,按配置不推送")
    else:
        log("已跳过推送")

    log("=" * 62)
    log(f"完成 | 今日 {len(items)} 条 | 新增 {fresh} 条 | "
        f"标讯 {stats['tenders']} 政策 {stats['policies']} 动态 {stats['news']}")
    if stale:
        log("⚠ 疑似停更的源: " + "；".join(stale))
    log("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
