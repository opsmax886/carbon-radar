#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
碳雷达 · 机器人推送
============================================================
已实现:钉钉群机器人(默认)、企业微信群机器人、飞书群机器人。
只需要在环境变量里填 webhook 地址即可,没填就自动跳过推送。

钉钉加签说明:
  若机器人安全设置选了"加签",把密钥填到 DINGTALK_SECRET;
  若选的是"自定义关键词",建议设为"碳雷达",并把标题带上该词。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request


def _http_post(url: str, payload: dict, timeout: int = 20) -> str:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def _sign_dingtalk(url: str, secret: str) -> str:
    # 防护:钉钉部分页面会直接给出"已经带签名"的完整地址。
    # 如果地址里已经有 timestamp 和 sign,就不要再签一次,
    # 否则会变成 ?...&timestamp=A&sign=B&timestamp=C&sign=D 导致推送失败。
    if "timestamp=" in url and "sign=" in url:
        print("  · 检测到 Webhook 已自带签名,跳过重复加签")
        return url
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    h = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(h))
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}timestamp={ts}&sign={sign}"


def build_markdown(items: list[dict], date_str: str, stats: dict, site_url: str = "") -> tuple[str, str]:
    """生成推送正文。返回 (标题, markdown文本)。"""
    title = f"碳雷达 · {date_str}"
    lines: list[str] = []
    tenders = [i for i in items if i.get("category") == "tender"]
    policies = [i for i in items if i.get("category") in ("policy", "methodology")]
    news = [i for i in items if i.get("category") == "news"]

    lines.append(f"## 碳雷达 · {date_str}")
    # 统计压成一行:连续两行 ">" 会被钉钉合并,分不开。
    # 有推送窗口时写明是"近 N 天",否则你无法判断这条消息覆盖了多长时间。
    win = stats.get("push_window_days")
    scope = f"近 {win} 天" if win else "本次"
    _cnt = stats.get("push_count", stats.get("new", len(items)))
    lines.append(f"> {scope}新增 **{_cnt}** 条"
                 f"(标讯 {len(tenders)} · 政策/方法学 {len(policies)} · 动态 {len(news)})"
                 f" ｜ 库内累计 {stats.get('total', '-')} 条")
    lines.append("")

    def block(head: str, subset: list[dict], n: int = 8) -> None:
        """每条信息压成一行,条与条之间用空行隔开。

        为什么不用多行?钉钉对 markdown 支持很有限,单个换行会被折叠成空格,
        导致所有文字挤成一坨。用空行分隔是唯一在多端都可靠的做法。
        格式:🔥 [86分] [标题](链接) ｜ 地区 ｜ 来源 ｜ 日期
        """
        if not subset:
            return
        lines.append("")
        lines.append(f"### {head}(共 {len(subset)} 条)" if len(subset) > n else f"### {head}")
        lines.append("")
        for it in sorted(subset, key=lambda x: (x.get("date") or "", -x.get("match", x.get("score", 0))),
                         reverse=True)[:n]:
            score = it.get("match", it.get("score", 0))
            star = "🔥" if score >= 80 else ("⭐" if score >= 60 else "·")
            region = it.get("region", "")
            if region in ("未分类", ""):
                region = ""
            meta = " ｜ ".join(x for x in [region, it.get("source_name", ""), it.get("date") or ""] if x)
            line = f"{star} [{it['title']}]({it['url']})"
            if meta:
                line += f" ｜ {meta}"
            lines.append(line)
            lines.append("")   # 空行分段,这是钉钉下唯一可靠的换行方式

        if len(subset) > n:
            lines.append(f"> 另有 {len(subset) - n} 条同类信息,可在看板查看")
            lines.append("")

    block("🎯 重点标讯", tenders, 10)
    block("📜 政策与方法学", policies, 5)
    block("📰 市场动态", news, 4)

    # 本次新增里没有标讯,但库里其实有 —— 明确说一句,避免误以为"标讯没抓到"
    if not tenders and stats.get("tenders"):
        lines.append("")
        lines.append(f"> ℹ️ 本次没有新增标讯,但库内已有 **{stats['tenders']} 条标讯**,可在看板中查看。")

    # AI 失效必须显式告警:否则摘要退化成"标题截断"、match 退化成规则分,
    # 页面和钉钉都看着正常,用户会以为 AI 一直在工作。
    if stats.get("ai_enabled") and not stats.get("ai_ok"):
        lines.append("")
        lines.append(f"> ⚠️ **AI 摘要未生效**(成功 0 批 / 失败 {stats.get('ai_failed', 0)} 批),"
                     f"以下摘要为规则兜底。请检查 DeepSeek 余额或 API Key。")

    stale = stats.get("stale_sources") or []
    if stale:
        lines.append("---")
        lines.append(f"⚠️ 以下来源疑似停止更新,请留意: {'、'.join(stale)}")
    if site_url:
        lines.append("")
        lines.append(f"[👉 打开完整看板]({site_url})")
    return title, "\n".join(lines)


def push_dingtalk(title: str, markdown: str) -> bool:
    url = os.environ.get("DINGTALK_WEBHOOK", "").strip()
    if not url:
        print("  · 未配置 DINGTALK_WEBHOOK,跳过钉钉推送")
        return False
    secret = os.environ.get("DINGTALK_SECRET", "").strip()
    if secret:
        url = _sign_dingtalk(url, secret)
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": markdown}}
    try:
        body = _http_post(url, payload)
        ok = '"errcode":0' in body.replace(" ", "")
        print(f"  {'✓' if ok else '✗'} 钉钉推送{'成功' if ok else '失败: ' + body[:150]}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 钉钉推送异常: {str(e)[:120]}")
        return False


def push_wecom(title: str, markdown: str) -> bool:
    url = os.environ.get("WECOM_WEBHOOK", "").strip()
    if not url:
        return False
    try:
        body = _http_post(url, {"msgtype": "markdown", "markdown": {"content": markdown}})
        print(f"  ✓ 企业微信推送返回: {body[:120]}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 企业微信推送异常: {str(e)[:120]}")
        return False


def push_feishu(title: str, markdown: str) -> bool:
    url = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not url:
        return False
    try:
        body = _http_post(url, {"msg_type": "text", "content": {"text": f"{title}\n{markdown}"}})
        print(f"  ✓ 飞书推送返回: {body[:120]}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 飞书推送异常: {str(e)[:120]}")
        return False


def push_all(items: list[dict], date_str: str, stats: dict, site_url: str = "") -> bool:
    """推送并返回"是否至少有一个渠道成功"。

    原来忽略返回值,导致 webhook 失效时 Actions 依然全绿、钉钉彻底静默,
    而这些条目已经被记为"推过了",再也不会补推。
    """
    if not items:
        return push_heartbeat(date_str, stats, site_url)
    title, md = build_markdown(items, date_str, stats, site_url)
    channels = ("DINGTALK_WEBHOOK", "WECOM_WEBHOOK", "FEISHU_WEBHOOK")
    if not any(os.environ.get(k) for k in channels):
        # 一个渠道都没配置 = 用户选择不推送,不算失败(本地调试时就是这样)
        push_dingtalk(title, md)
        return True
    results = [push_dingtalk(title, md), push_wecom(title, md), push_feishu(title, md)]
    ok = any(results)
    if not ok:
        print("  ✗ 所有推送渠道都失败了 —— 请检查 webhook / 加签密钥是否失效")
    return ok


def push_heartbeat(date_str: str, stats: dict, site_url: str = "") -> bool:
    """当天无新增时发一条简短的平安消息。

    没有这条消息,你无法区分"今天确实没有新标讯"和"抓取脚本已经挂了很多天"。
    对监控类工具来说,沉默是最危险的状态。
    """
    online = stats.get("sources_ok")
    lines = [f"## 碳雷达 · {date_str}", "",
             f"今日**无新增**信息(库内 {stats.get('total', '-')} 条)。",
             f"抓取正常" + (f",{online} 个数据源有返回。" if online else "。")]
    if stats.get("stale_sources"):
        lines.append("")
        lines.append(f"⚠️ 有 {len(stats['stale_sources'])} 个源异常,详见看板「数据源状态」。")
    stale = stats.get("stale_sources") or []
    if stale:
        lines.append("")
        lines.append(f"⚠️ 疑似停更: {'、'.join(stale)}")
    if site_url:
        lines.append("")
        lines.append(f"[👉 打开完整看板]({site_url})")
    title = f"碳雷达 · {date_str} 无新增"
    md = "\n".join(lines)
    channels = ("DINGTALK_WEBHOOK", "WECOM_WEBHOOK", "FEISHU_WEBHOOK")
    if not any(os.environ.get(k) for k in channels):
        # 一个渠道都没配置 = 用户选择不推送,不算失败(本地调试就是这样)
        push_dingtalk(title, md)
        return True
    results = [push_dingtalk(title, md), push_wecom(title, md), push_feishu(title, md)]
    if not any(results):
        print("  ✗ 所有推送渠道都失败了 —— 请检查 webhook / 加签密钥是否失效")
    return any(results)
