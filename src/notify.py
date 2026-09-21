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
    # 统计压成一行:连续两行 ">" 会被钉钉合并,分不开
    lines.append(f"> 本次新增 **{stats.get('new', len(items))}** 条"
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
        lines.append(f"### {head}")
        lines.append("")
        for it in sorted(subset, key=lambda x: -x.get("match", x.get("score", 0)))[:n]:
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

    block("🎯 重点标讯", tenders, 10)
    block("📜 政策与方法学", policies, 5)
    block("📰 市场动态", news, 4)

    # 本次新增里没有标讯,但库里其实有 —— 明确说一句,避免误以为"标讯没抓到"
    if not tenders and stats.get("tenders"):
        lines.append("")
        lines.append(f"> ℹ️ 本次没有新增标讯,但库内已有 **{stats['tenders']} 条标讯**,可在看板中查看。")

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


def push_all(items: list[dict], date_str: str, stats: dict, site_url: str = "") -> None:
    if not items:
        push_heartbeat(date_str, stats, site_url)
        return
    title, md = build_markdown(items, date_str, stats, site_url)
    push_dingtalk(title, md)
    push_wecom(title, md)
    push_feishu(title, md)


def push_heartbeat(date_str: str, stats: dict, site_url: str = "") -> None:
    """当天无新增时发一条简短的平安消息。

    没有这条消息,你无法区分"今天确实没有新标讯"和"抓取脚本已经挂了很多天"。
    对监控类工具来说,沉默是最危险的状态。
    """
    online = stats.get("sources_ok")
    lines = [f"## 碳雷达 · {date_str}", "",
             f"今日**无新增**信息(总库 {stats.get('total', '-')} 条)。",
             f"系统运行正常" + (f",{online} 个数据源在线。" if online else "。")]
    stale = stats.get("stale_sources") or []
    if stale:
        lines.append("")
        lines.append(f"⚠️ 疑似停更: {'、'.join(stale)}")
    if site_url:
        lines.append("")
        lines.append(f"[👉 打开完整看板]({site_url})")
    title = f"碳雷达 · {date_str} 无新增"
    md = "\n".join(lines)
    push_dingtalk(title, md)
    push_wecom(title, md)
    push_feishu(title, md)
