#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
碳雷达 · DeepSeek 智能增强
============================================================
作用:给每条信息生成
  · 一句话摘要(≤40字,让你不用点开就知道是什么)
  · AI 业务匹配度(0-100,结合你的六条业务线判断)
  · 一句话理由(说明为什么值得看)

关键设计:优雅降级
  没有 API Key、网络不通、额度用完、返回格式错误 —— 任何一种情况都
  不会让流程失败,而是自动退回"规则打分 + 标题截断"模式。
  也就是说:这个系统在完全没有 AI 的情况下也能正常跑。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

API_URL = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

SYSTEM_PROMPT = """你是碳排放领域的资深招投标与政策分析助手,服务于一家双碳咨询公司。
该公司六大业务线:碳核查/温室气体排放核查、碳足迹/碳标签、CCER/自愿减排项目、
碳中和规划/零碳园区、绿色金融/ESG、碳市场/碳交易。

对每条信息,你要输出:
1. summary: 一句话摘要,不超过40个汉字,说清"谁、做什么、多少钱/什么结果",不要客套话
2. match: 业务匹配度 0-100 的整数。判断标准:
   - 90-100 是明确可投标的采购/招标项目,且属于上述业务线
   - 70-89  是相关项目线索或高价值政策
   - 40-69  是行业动态,有参考价值但无直接商机
   - 0-39   与业务关系弱或是纯资讯
3. reason: 不超过18个汉字,说明判断理由(如"直接可投的核查项目""方法学更新,影响项目开发")

只输出 JSON,格式:{"items":[{"i":0,"summary":"...","match":85,"reason":"..."}]}
不要输出任何解释文字。"""


def _post(payload: dict, api_key: str, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _fallback(item: dict) -> dict:
    """无 AI 时的规则兜底:用标题截断做摘要。"""
    raw = item.get("summary") or ""
    title = item.get("title", "")
    brief = (raw[:60] if raw else title[:60]).strip()
    if len(brief) >= 60:
        brief += "…"
    return {"summary": brief, "match": int(item.get("score", 0)),
            "reason": item.get("track_label", "") or "规则匹配", "ai": False}


def enrich(items: list[dict], batch_size: int = 12, max_items: int = 80,
           min_score: int = 15) -> list[dict]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("  · 未配置 DEEPSEEK_API_KEY,使用规则打分兜底(功能完全可用)")
        return [{**it, **{k: v for k, v in _fallback(it).items() if k not in it or k in ("summary", "match", "reason", "ai")}}
                for it in items]

    # 只对值得花 token 的条目调用 AI:按分数排序取前 max_items
    order = sorted(range(len(items)), key=lambda i: -items[i].get("score", 0))
    targets = [i for i in order if items[i].get("score", 0) >= min_score][:max_items]
    target_set = set(targets)
    print(f"  · AI 增强 {len(targets)}/{len(items)} 条(其余用规则兜底,控制成本)")

    for i, it in enumerate(items):
        if i not in target_set:
            items[i] = {**it, **_fallback(it)}

    for start in range(0, len(targets), batch_size):
        chunk = targets[start:start + batch_size]
        lines = []
        for n, idx in enumerate(chunk):
            it = items[idx]
            # 手动投稿(如微信公众号)带正文摘录,喂给模型能让摘要准确得多
            extra = ""
            snip = (it.get("summary") or "").strip()
            if snip and not it["title"].startswith(snip[:20]):
                extra = f'\n    正文摘录:{snip[:160]}'
            lines.append(f'[{n}] 标题:{it["title"]}\n    来源:{it.get("source_name","")} 日期:{it.get("date") or "未知"} '
                         f'规则命中:{",".join(it.get("matched", [])[:5]) or "无"}{extra}')
        user = "请分析以下 %d 条信息:\n\n%s" % (len(chunk), "\n".join(lines))
        try:
            resp = _post({
                "model": MODEL,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": user}],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
                "max_tokens": 2000,
            }, api_key)
            content = resp["choices"][0]["message"]["content"]
            parsed = _extract_json(content) or {}
            got = {int(o["i"]): o for o in parsed.get("items", []) if "i" in o}
            for n, idx in enumerate(chunk):
                o = got.get(n)
                if o:
                    items[idx] = {**items[idx],
                                  "summary": str(o.get("summary", ""))[:120] or _fallback(items[idx])["summary"],
                                  "match": max(0, min(100, int(o.get("match", items[idx].get("score", 0))))),
                                  "reason": str(o.get("reason", ""))[:40],
                                  "ai": True}
                else:
                    items[idx] = {**items[idx], **_fallback(items[idx])}
            print(f"  · 已处理 {min(start + batch_size, len(targets))}/{len(targets)} 条")
        except Exception as e:  # noqa: BLE001
            print(f"  · AI 批次失败({str(e)[:70]}),该批改用规则兜底")
            for idx in chunk:
                items[idx] = {**items[idx], **_fallback(items[idx])}
    return items


def build_digest(items: list[dict], top_n: int = 12) -> str:
    """把当日高价值条目转成适合 AI/推送使用的紧凑文本。"""
    top = sorted(items, key=lambda x: -x.get("match", x.get("score", 0)))[:top_n]
    return "\n".join(
        f'- [{it.get("match", it.get("score"))}分] {it["title"]} ({it.get("region","")}, {it.get("date") or "无日期"})'
        for it in top)
