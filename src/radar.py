#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
碳雷达 · 抓取与筛选引擎
============================================================
职责:
  1. 按 config/sources.yml 抓取各源(限速、重试、编码自适应)
  2. 通用 HTML 列表解析(优先从 URL 提取日期 —— 最抗改版)
  3. 关键词过滤 + 可解释的业务匹配度打分 + 地区识别
  4. SimHash 去重(跨源同一标讯合并)
  5. 源健康监控(哪个源挂了、哪个源停止更新)

设计原则:任何单个源失败都不能影响其他源,全部失败也要正常产出。
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    import yaml
except ImportError:
    print("缺少依赖 PyYAML,请先执行: pip install -r requirements.txt", file=sys.stderr)
    raise

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")
WEB_DIR = os.path.join(ROOT, "web")

CST = timezone(timedelta(hours=8))  # 北京时间


# ---------------------------------------------------------------- 基础工具

def now_cst() -> datetime:
    return datetime.now(CST)


def today_str() -> str:
    return now_cst().strftime("%Y-%m-%d")


def log(msg: str) -> None:
    print(f"[{now_cst().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_yaml(name: str) -> dict:
    path = os.path.join(CONFIG_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_json_file(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def save_json_file(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 抓取层

_IPV4_PATCHED = False


def _prefer_ipv4() -> None:
    """让 DNS 解析优先返回 IPv4 地址。

    为什么需要:部分政务站点只解析出 IPv6(AAAA 记录),而 GitHub Actions 的
    runner 没有 IPv6 出口,于是报 "Network is unreachable" —— 源探测报告里
    贵州/辽宁/黑龙江/湖北/内蒙古的公共资源交易平台都属于这一类。
    优先取 A 记录即可绕过;取不到 IPv4 时仍回退原结果,不会弄坏别的站点。
    """
    global _IPV4_PATCHED
    if _IPV4_PATCHED:
        return
    import socket as _socket
    _orig = _socket.getaddrinfo

    def _ipv4_first(*args, **kwargs):
        res = _orig(*args, **kwargs)
        v4 = [r for r in res if r[0] == _socket.AF_INET]
        return v4 or res

    _socket.getaddrinfo = _ipv4_first
    _IPV4_PATCHED = True


class Fetcher:
    """带限速与重试的抓取器。同一域名强制间隔,避免触发反爬。"""

    def __init__(self, settings: dict):
        self.settings = settings
        self.delay = float(settings.get("request_delay_seconds", 3))
        self.timeout = float(settings.get("timeout_seconds", 25))
        self.retries = int(settings.get("retries", 3))
        self.ua = settings.get("user_agent", "Mozilla/5.0")
        self._last_hit: dict[str, float] = {}
        self.ctx = ssl.create_default_context()
        # 部分政务站点证书链不完整,放宽校验但仅用于只读公开页面
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        # 兼容性修复 1:优先 IPv4 —— 解决 runner 无 IPv6 出口导致的 "Network is unreachable"
        _prefer_ipv4()
        # 兼容性修复 2:指定常见椭圆曲线 —— 个别站点证书会触发 "SSL: BAD_ECPOINT"
        try:
            self.ctx.set_ecdh_curve("prime256v1")
        except (AttributeError, ValueError, ssl.SSLError):
            pass

    def _throttle(self, url: str) -> None:
        host = urllib.parse.urlparse(url).netloc
        last = self._last_hit.get(host, 0.0)
        wait = self.delay - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.time()

    def get(self, url: str, encoding: str | None = None) -> str:
        return self.request(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml,*/*;q=0.8"})

    def request(self, url: str, method: str = "GET", headers: dict | None = None,
                data: bytes | None = None, encoding: str | None = None) -> str:
        """通用请求:支持自定义方法/请求头/请求体,供 JSON 接口类数据源使用。"""
        h = {"User-Agent": self.ua, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        if headers:
            h.update(headers)
        last_err = None
        for attempt in range(1, self.retries + 1):
            self._throttle(url)
            try:
                req = urllib.request.Request(url, data=data, headers=h, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                    raw = r.read()
                text = decode_body(raw, encoding, r.headers.get("Content-Type", ""))
                if is_blocked(text):
                    raise RuntimeError("命中反爬拦截页(访问频繁/验证码)")
                return text
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < self.retries:
                    backoff = 2 ** attempt
                    time.sleep(backoff)
        raise RuntimeError(f"请求失败: {last_err}")


def decode_body(raw: bytes, encoding: str | None, content_type: str) -> str:
    """中文政务站点编码混乱,这里做自适应解码。"""
    candidates = []
    if encoding:
        candidates.append(encoding)
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        candidates.append(m.group(1))
    # 从 meta 标签嗅探
    head = raw[:2048].decode("ascii", errors="ignore")
    m = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    if m:
        candidates.append(m.group(1))
    candidates += ["utf-8", "gb18030", "gbk", "big5"]

    best, best_bad = None, 1 << 30
    for enc in candidates:
        try:
            t = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        bad = t.count("\ufffd")
        if bad < best_bad:
            best, best_bad = t, bad
        if bad == 0:
            return t
    return best if best is not None else raw.decode("utf-8", errors="ignore")


BLOCK_MARKERS = ["频繁访问", "访问过于频繁", "请开启JavaScript", "验证码", "安全验证",
                 "请输入验证码", "您的访问", "request blocked", "Access Denied"]


def is_blocked(text: str) -> bool:
    head = text[:3000]
    return any(m in head for m in BLOCK_MARKERS)


# ---------------------------------------------------------------- 日期提取

DATE_PATTERNS = [
    (re.compile(r"t(\d{4})(\d{2})(\d{2})[_-]"), "{}-{}-{}"),          # 生态环境部式
    (re.compile(r"/(\d{4})/(\d{2})(\d{2})/"), "{}-{}-{}"),            # 碳排放交易网式
    (re.compile(r"/(\d{4})-(\d{2})-(\d{2})[/.]"), "{}-{}-{}"),
    (re.compile(r"/(\d{4})(\d{2})/(\d{2})/"), "{}-{}-{}"),
]


def date_from_url(url: str) -> str | None:
    for pat, fmt in DATE_PATTERNS:
        m = pat.search(url)
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3)
            if "1900" < y < "2100" and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                return fmt.format(y, mo, d)
    return None


TEXT_DATE_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")


def date_from_text(text: str) -> str | None:
    m = TEXT_DATE_RE.search(text)
    if not m:
        return None
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    if 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y}-{mo:02d}-{d:02d}"
    return None


# ---------------------------------------------------------------- HTML 列表解析

NAV_WORDS = {"更多", "更多>>", "首页", "上一页", "下一页", "末页", "登录", "注册", "返回",
             "网站地图", "联系我们", "关于我们", "设为首页", "加入收藏", "详细", "详情"}
TAG_RE = re.compile(r"<[^>]+>")


def clean_text(s: str) -> str:
    s = TAG_RE.sub("", s)
    s = html_mod.unescape(s)
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"(详细|详情|查看|>>|>|»|…)+$", "", s).strip()
    return s


def parse_html_list(page: str, source: dict, base_url: str) -> list[dict]:
    """通用列表页解析:靠 link_filter 正则筛选链接,不依赖脆弱的 CSS 选择器。"""
    raw_filter = source.get("link_filter") or ""
    filters = raw_filter if isinstance(raw_filter, list) else [raw_filter]
    kept: list[dict] = []
    seen_urls: set[str] = set()

    for m in re.finditer(r'<a\s[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, re.S | re.I):
        href, inner = m.group(1), m.group(2)
        if href.lower().startswith(("javascript:", "mailto:", "#")):
            continue
        url = urllib.parse.urljoin(base_url, href)
        if filters and filters[0] and not any(re.search(f, url) for f in filters if f):
            continue
        title = clean_text(inner)
        if len(title) < 8 or not re.search(r"[\u4e00-\u9fa5]", title):
            continue
        if title in NAV_WORDS:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # 日期:URL 优先(最稳),其次看链接后 300 字符内的文本
        date = date_from_url(url)
        if not date:
            tail = page[m.end(): m.end() + 300]
            date = date_from_text(clean_text(tail))
        kept.append({"title": title, "url": url, "date": date, "source_id": source["id"],
                     "source_name": source.get("name", source["id"]), "kind": source.get("kind", "news")})

    limit = int(source.get("max_items") or 0)
    if limit:
        kept = kept[:limit]
    return kept


# ---------------------------------------------------------------- 关键词过滤与打分

class Scorer:
    def __init__(self, kw: dict):
        self.kw = kw
        self.tracks = kw.get("tracks") or {}
        # 触发词 = 所有业务线关键词 + 补充触发词
        triggers: list[str] = list(kw.get("triggers_extra") or [])
        for tr in self.tracks.values():
            triggers += list(tr.get("keywords") or [])
        self.triggers = sorted(set(t for t in triggers if t), key=len, reverse=True)
        self.excludes = sorted(set(kw.get("exclude") or []), key=len, reverse=True)
        self.boosts = kw.get("signal_boost") or {}
        self.region_map: list[tuple[str, str]] = []
        for region, aliases in (kw.get("regions") or {}).items():
            for a in aliases:
                self.region_map.append((a, region))
        self.region_map.sort(key=lambda x: len(x[0]), reverse=True)

    def is_relevant(self, text: str) -> bool:
        low = text.lower()
        if not any(t.lower() in low for t in self.triggers):
            return False
        # 命中排除词且没命中任何业务线关键词 -> 丢弃
        hit_ex = [e for e in self.excludes if e.lower() in low]
        if hit_ex:
            hit_track = any(k.lower() in low for tr in self.tracks.values()
                            for k in (tr.get("keywords") or []))
            if not hit_track:
                return False
        return True

    def detect_region(self, text: str, default: str = "未分类") -> str:
        for alias, region in self.region_map:
            if alias in text:
                return region
        loc = self.detect_locality(text)
        return loc or default

    # 地名兜底:省级没命中时,抓一个市/县名当标签。
    # 比"未分类"有用得多 —— 你能一眼看出是哪的项目。
    # 但必须严防误切:"赋能县域经济"里切出"转型金融赋能县"就是典型错误。
    _LOCALITY_RE = re.compile(r"[\u4e00-\u9fa5]{2,6}?(?:省|市|县|自治州|自治县|自治区|盟)")
    _LOCALITY_BAD = ("园区", "新区", "示范区", "开发区", "高新区", "保税区", "试验区",
                     "旅游区", "景区", "校区", "厂区", "城区", "地区", "市区", "辖区",
                     "小区", "社区", "街区", "片区", "库区", "山区", "灾区", "展区",
                     "赛区", "城市", "都市", "上市", "市场", "集市", "超市", "股市")
    # 名称部分(去掉"省/市/县"后缀)若含这些词,一定不是地名
    _LOCALITY_STOP = ("全国", "碳市", "市场", "金融", "赋能", "经济", "全域", "全市",
                      "县域", "区域", "转型", "绿色", "低碳", "数字", "智慧", "示范",
                      "试点", "重点", "相关", "行业", "产业", "企业", "项目", "服务",
                      "建设", "管理", "技术", "平台", "中心", "基地", "工程", "方案",
                      "规划", "政策", "标准", "体系", "评价", "认证", "核查", "交易",
                      "资产", "排放", "气候", "能源", "环境", "生态", "发展", "改革",
                      "创新", "合作", "国际", "国内", "本", "该", "全", "各", "跨")

    # 后缀后紧跟这些字,说明"市/县/省"只是词的一部分(市场/市长/县域/省级…),不是地名
    _LOCALITY_NEXT_BAD = set("场长民区政容级域内界外值辖界")

    def detect_locality(self, text: str) -> str | None:
        for m in self._LOCALITY_RE.finditer(text):
            cand = m.group(0)
            # 地名通常出现在标题开头("九江市…"/"关于九江市…"/"采购预算31万元!宜宾市…")
            if m.start() > 6:
                continue
            if any(b in cand for b in self._LOCALITY_BAD):
                continue
            # "中国碳市场"里切出的"中国碳市"必须拦掉
            nxt = text[m.end()] if m.end() < len(text) else ""
            if nxt in self._LOCALITY_NEXT_BAD:
                continue
            name = cand[:-1] if cand.endswith(("省", "市", "县", "盟")) else cand
            if any(w in name for w in self._LOCALITY_STOP):
                continue
            if not (2 <= len(name) <= 3):
                continue
            return cand
        return None

    def score(self, title: str, body: str = "") -> dict:
        """返回业务匹配度 + 命中的业务线与关键词,分数完全可解释。"""
        t_low, b_low = title.lower(), (body or "").lower()
        best_score, best_track, best_hits = 0.0, None, []
        all_hits: set[str] = set()

        for key, tr in self.tracks.items():
            kws = tr.get("keywords") or []
            th = [k for k in kws if k.lower() in t_low]
            bh = [k for k in kws if k.lower() in b_low]
            weight = float(tr.get("weight", 1.0))
            s = (len(th) * 25 + len(bh) * 8) * weight
            if s > best_score:
                best_score, best_track, best_hits = s, key, th or bh
            all_hits.update(th)
            all_hits.update(bh)

        boost = 0
        reasons: list[str] = []
        for name, cfg in self.boosts.items():
            if any(k.lower() in t_low or k.lower() in b_low for k in cfg.get("keywords", [])):
                boost += cfg.get("boost", 0)
                reasons.append(name)

        final = max(0, min(100, round(best_score + boost)))
        label = (self.tracks.get(best_track) or {}).get("label", "") if best_track else ""
        return {"score": final, "track": best_track, "track_label": label,
                "matched": sorted(all_hits, key=len, reverse=True)[:8], "signals": reasons}


# ---------------------------------------------------------------- SimHash 去重

def _simhash(text: str, bits: int = 64) -> int:
    grams = [text[i:i + 2] for i in range(max(1, len(text) - 1))]
    if not grams:
        grams = [text]
    v = [0] * bits
    for g in grams:
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest()[:16], 16)
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    return sum(1 << i for i in range(bits) if v[i] > 0)


def _norm_title(t: str) -> str:
    t = re.sub(r"[【】\[\]（）()《》<>\"'“”‘’·、,，。.：:；;!！?？\-—_/\\|]", "", t)
    t = re.sub(r"(公开招标|招标公告|采购公告|中标公告|成交公告|结果公告|竞争性磋商|询价公告|公告|公示|项目|采购|服务)$", "", t)
    return re.sub(r"\s+", "", t)


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dedupe(items: list[dict], threshold: int = 3) -> list[dict]:
    """跨源同一标讯合并。保留分数最高的一条,并记录其他来源。"""
    items = sorted(items, key=lambda x: (-x.get("score", 0), x.get("date") or ""))
    kept: list[dict] = []
    kept_hashes: list[tuple[int, int]] = []  # (simhash, index)

    for it in items:
        h = _simhash(_norm_title(it["title"]))
        dup_of = None
        for oh, idx in kept_hashes:
            if _hamming(h, oh) <= threshold:
                dup_of = idx
                break
        if dup_of is None:
            it["also_on"] = []
            kept.append(it)
            kept_hashes.append((h, len(kept) - 1))
        else:
            other = it["source_name"]
            if other != kept[dup_of]["source_name"] and other not in kept[dup_of]["also_on"]:
                kept[dup_of]["also_on"].append(other)
    return kept


# ---------------------------------------------------------------- 源健康监控

def load_health() -> dict:
    path = os.path.join(DATA_DIR, "sources_health.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_health(health: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "sources_health.json"), "w", encoding="utf-8") as f:
        json.dump(health, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 主流程

def collect(cfg_sources: dict, force_all: bool = False) -> tuple[list[dict], list[dict]]:
    """抓取全部启用源。返回 (条目列表, 源健康报告)。"""
    settings = cfg_sources.get("settings") or {}
    fetcher = Fetcher(settings)
    health = load_health()
    global_limit = int(settings.get("max_items_per_source", 60))
    rows: list[dict] = []
    report: list[dict] = []

    for src in cfg_sources.get("sources") or []:
        sid = src["id"]
        if not src.get("enabled") and not force_all:
            continue
        entry = health.setdefault(sid, {"consecutive_failures": 0})
        entry["last_attempt"] = today_str()
        try:
            # 手动链接池是特殊源:不是单个列表页,而是读文件里的一批文章链接
            if src.get("adapter") == "manual_links":
                items = collect_manual_links(cfg_sources, fetcher)
                if items:
                    log(f"  ✓ {src.get('name', sid):<28s} 抓到 {len(items):3d} 条")
                else:
                    log(f"  · {src.get('name', sid):<28s} 暂无链接")
            elif src.get("url_template") and src.get("keywords"):
                # 按关键词模板逐个请求的站点(如中国政府采购网)
                items = fetch_template_source(src, fetcher)
                # 每个关键词各自限流,这里再做一个总量上限,避免条数失控
                cap = int(src.get("total_max") or global_limit or 100)
                items = items[:cap]
            else:
                if src.get("adapter") == "json_api":
                    items = fetch_json_api(src, fetcher)
                else:
                    page = fetcher.get(src["url"], src.get("encoding"))
                    if src.get("adapter") == "rss":
                        items = parse_rss(page, src)
                    else:
                        items = parse_html_list(page, src, src.get("base") or src["url"])
                items = items[: global_limit or None]
            for it in items:
                it["source_status"] = src.get("status", "")
            rows.extend(items)
            entry.update({"last_success": today_str(), "last_count": len(items),
                          "consecutive_failures": 0, "last_error": None,
                          "newest_item_date": max([i["date"] for i in items if i.get("date")], default=None)})
            if src.get("adapter") != "manual_links":
                log(f"  ✓ {src.get('name', sid):<28s} 抓到 {len(items):3d} 条")
        except Exception as e:  # noqa: BLE001
            entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
            entry["last_error"] = str(e)[:200]
            log(f"  ✗ {src.get('name', sid):<28s} 失败: {str(e)[:80]}")
        report.append({"id": sid, "name": src.get("name", sid), "enabled": bool(src.get("enabled")),
                       "status": src.get("status", ""), "note": src.get("note", ""), **entry})

    save_health(health)
    return rows, report


def fetch_template_source(src: dict, fetcher: "Fetcher") -> list[dict]:
    """按关键词模板逐个请求。

    有些站点(如中国政府采购网)必须"一个关键词一次请求"才能搜到,
    所以在配置里写 url_template + keywords,由这里循环调用:
      url_template: "https://search.ccgp.gov.cn/bxsearch?...&kw={kw}&..."
      keywords: [碳核查, 温室气体, 碳足迹]
    关键词会自动 URL 编码,你直接写中文即可。
    """
    tpl = src.get("url_template") or ""
    kws = src.get("keywords") or []
    if not tpl or not kws:
        return []
    old_delay = fetcher.delay
    if src.get("request_delay"):
        fetcher.delay = float(src["request_delay"])   # 该类站点要放慢,避免被封
    items: list[dict] = []
    try:
        for kw in kws:
            url = tpl.replace("{kw}", urllib.parse.quote(str(kw)))
            try:
                page = fetcher.get(url, src.get("encoding"))
                got = parse_html_list(page, src, src.get("base") or url)
                for g in got:
                    g["matched_kw"] = str(kw)
                items.extend(got)
                log(f"    · 关键词「{kw}」→ {len(got)} 条")
            except Exception as e:  # noqa: BLE001
                log(f"    ! 关键词「{kw}」失败: {str(e)[:60]}")
    finally:
        fetcher.delay = old_delay
    return items



def parse_rss(page: str, source: dict) -> list[dict]:
    """极简 RSS/Atom 解析,避免额外依赖。"""
    out = []
    for m in re.finditer(r"<item[\s>](.*?)</item>|<entry[\s>](.*?)</entry>", page, re.S | re.I):
        block = m.group(1) or m.group(2) or ""
        t = re.search(r"<title[^>]*>(.*?)</title>", block, re.S | re.I)
        l = re.search(r"<link[^>]*>(.*?)</link>", block, re.S | re.I) or \
            re.search(r'<link[^>]*href=["\']([^"\']+)["\']', block, re.I)
        d = re.search(r"<(?:pubDate|updated|published)[^>]*>(.*?)</", block, re.S | re.I)
        if not (t and l):
            continue
        title = clean_text(t.group(1))
        url = clean_text(l.group(1))
        date = date_from_text(clean_text(d.group(1))) if d else date_from_url(url)
        if title and url:
            out.append({"title": title, "url": url, "date": date, "source_id": source["id"],
                        "source_name": source.get("name", source["id"]), "kind": source.get("kind", "news")})
    return out


# ---------------------------------------------------------------- 通用 JSON 接口
# 商业标讯 API(剑鱼、千里马、我要标讯等)都提供 JSON 接口。
# 有了这个适配器,接入任何一家都只需在 sources.yml 里写配置,不用改代码。

def _env_subst(v):
    """把配置里的 ${ENV_NAME} 替换成环境变量值 —— 密钥不写进配置文件。

    在 GitHub Actions 里,把密钥配成 Secrets;本地则设成环境变量。
    取不到值时替换成空串,并会在下面报"密钥未配置"的友好错误。
    """
    if isinstance(v, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), v)
    if isinstance(v, dict):
        return {k: _env_subst(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_env_subst(x) for x in v]
    return v


def _dig(obj, path: str):
    """按 a.b.c 或 list.0.field 的路径取值。"""
    cur = obj
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _missing_env(obj, acc=None) -> set:
    """找出配置里引用了、但环境变量并不存在的名字。

    必须在 _env_subst 之前检查 —— 替换会把 ${X} 变成空串,
    之后就再也看不出"用户其实没配置"了,只会发一个注定失败的请求。
    """
    acc = acc if acc is not None else set()
    if isinstance(obj, str):
        for m in re.finditer(r"\$\{(\w+)\}", obj):
            if not os.environ.get(m.group(1)):
                acc.add(m.group(1))
    elif isinstance(obj, dict):
        for v in obj.values():
            _missing_env(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _missing_env(v, acc)
    return acc


def _norm_date(v) -> str | None:
    """把各种日期表示统一成 YYYY-MM-DD。支持时间戳(秒/毫秒)与常见字符串。"""
    if v is None or v == "":
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d{10,13}", s):
        ts = int(s)
        if ts > 10 ** 12:
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return None
    return date_from_text(s)


def fetch_json_api(src: dict, fetcher: "Fetcher") -> list[dict]:
    """按 YAML 配置调用 JSON 接口并映射字段。

    sources.yml 示例:
      adapter: json_api
      method: POST
      url: https://gate.gov-bid.com/outer-gateway/xxx/search
      headers:
        Authorization: "Bearer ${WOYAOBID_API_KEY}"
      body: {keyword: 碳核查, pageNo: 1, pageSize: 50}
      items_path: data.records
      title_field: title
      url_field: detailUrl
      date_field: publishTime
      summary_field: content
    """
    url = _env_subst(src["url"])
    headers = {"Accept": "application/json, text/plain, */*"}
    headers.update(_env_subst(src.get("headers") or {}))
    method = (src.get("method") or "GET").upper()
    params = _env_subst(src.get("params") or {})
    body = _env_subst(src.get("body") or {})

    # 先检查密钥是否配好,避免发一个注定失败的请求(必须在 _env_subst 之前判断)
    missing = _missing_env(src.get("url", "")) | _missing_env(src.get("headers") or {}) \
        | _missing_env(src.get("body") or {}) | _missing_env(src.get("params") or {})
    if missing:
        raise RuntimeError(f"密钥未配置:环境变量 {'、'.join(sorted(missing))} 不存在"
                           f"(请在 GitHub 仓库 Settings → Secrets 里添加)")
    if src.get("auth_required") and not headers.get("Authorization"):
        raise RuntimeError("密钥未配置:该接口需要 Authorization 请求头")

    data = None
    if method == "POST":
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json;charset=UTF-8")
    elif params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)

    text = fetcher.request(url, method=method, headers=headers, data=data)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"返回的不是合法 JSON(前 120 字符): {text[:120]}")

    rows = _dig(payload, src.get("items_path", "data"))
    if not isinstance(rows, list):
        # 兜底:自动在顶层找一个"看起来像列表"的字段
        for k, v in (payload.items() if isinstance(payload, dict) else []):
            if isinstance(v, list) and v:
                rows = v
                break
    if not isinstance(rows, list):
        raise RuntimeError(f"没找到条目数组,请检查 items_path。返回顶层字段: "
                           f"{list(payload.keys())[:8] if isinstance(payload, dict) else type(payload).__name__}")

    tf, uf = src.get("title_field", "title"), src.get("url_field", "url")
    df, sf = src.get("date_field", "publishTime"), src.get("summary_field", "")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = clean_text(str(_dig(r, tf) or ""))
        link = str(_dig(r, uf) or "").strip()
        if not title or len(title) < 6:
            continue
        out.append({
            "title": title,
            "url": link or url,
            "date": _norm_date(_dig(r, df)),
            "source_id": src["id"],
            "source_name": src.get("name", src["id"]),
            "kind": src.get("kind", "tender"),
            "summary": clean_text(str(_dig(r, sf) or ""))[:400] if sf else "",
        })
    return out



# ---------------------------------------------------------------- 手动链接池
# 为什么需要它:微信公众号【只开放单篇文章页】,不开放任何文章列表接口。
# 实测:profile_ext 返回 {"ret":-3,"errmsg":"no session"};album 接口返回 ret:10004;
#      搜狗微信的搜索结果是会话绑定跳转 + JS 反爬,解不出真实链接。
# 所以把链接贴进 config/manual_links.txt 是当下唯一稳定且免费的办法。

MANUAL_CACHE_PATH = os.path.join(DATA_DIR, "manual_cache.json")
VALID_KINDS = ("tender", "policy", "methodology", "news")
TENDER_HINTS = ("招标", "中标", "采购", "成交", "询比", "询价", "竞争性磋商",
                "竞争性谈判", "比选", "投标", "申报", "征集")
METHOD_HINTS = ("方法学", "核算指南", "核算标准", "技术规范", "MRV")
POLICY_HINTS = ("通知", "办法", "实施方案", "条例", "意见", "规划", "标准",
                "指南", "政策", "印发", "公告")


def _first(text: str, *pats):
    for p in pats:
        m = re.search(p, text, re.S | re.I)
        if m:
            v = html_mod.unescape(m.group(1)).strip()
            if v:
                return v
    return None


def _og(prop: str):
    """匹配 <meta property="X" content="Y">,兼容属性顺序颠倒的情况。"""
    p = re.escape(prop)
    return (rf'<meta[^>]+property=["\']{p}["\'][^>]+content=["\']([^"\']*)["\']',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{p}["\']')


def fetch_article_meta(url: str, fetcher: "Fetcher", encoding: str | None = None) -> dict:
    """抓取单篇文章页,提取标题/来源/发布时间/正文片段。

    微信公众号文章页是公开可读的(无需登录),这是"手动链接池"能成立的基础。
    对普通网页也兼容(靠标准 og 标签)。
    """
    page = fetcher.get(url, encoding)
    title = _first(page, *_og("og:title"),
                   r'var\s+msg_title\s*=\s*["\'](.*?)["\']',
                   r'<h1[^>]*class="rich_media_title"[^>]*>(.*?)</h1>',
                   r"<title>(.*?)</title>")
    source = _first(page, *_og("og:article:author"),
                    r'var\s+nickname\s*=\s*["\'](.*?)["\']',
                    r'id="js_name"[^>]*>(.*?)<',
                    *_og("og:site_name"))
    date = None
    ct = _first(page, r'var\s+ct\s*=\s*"?(\d{10})"?', r'"publish_time"\s*:\s*"?(\d{10})')
    if ct and ct.isdigit():
        date = datetime.fromtimestamp(int(ct), CST).strftime("%Y-%m-%d")
    if not date:
        raw = _first(page, *_og("article:published_time"), *_og("og:release_date"))
        if raw:
            date = date_from_text(raw) or (raw[:10] if re.match(r"\d{4}-\d{2}-\d{2}", raw) else None)
    if not date:
        date = date_from_url(url)

    snippet = _first(page, r'<div[^>]+class="rich_media_content[^"]*"[^>]*>(.*?)</div>',
                     r'<div[^>]+id="js_content"[^>]*>(.*?)</div>')
    if not snippet:
        snippet = _first(page, *_og("og:description"),
                         r'<meta[^>]+name="description"[^>]+content=["\']([^"\']*)["\']')
    if snippet:
        snippet = clean_text(snippet)[:1500]
    return {"title": title, "source": source, "date": date, "snippet": snippet or ""}


# 标题里出现这些词,说明这是一条"招投标公告",而不是市场资讯
TENDER_TITLE_KW = ("招标公告", "采购公告", "中标公告", "成交公告", "竞争性磋商",
                   "竞争性谈判", "询比", "询价", "比选", "征集公告", "公开招标",
                   "邀请招标", "结果公告", "中标结果", "更正公告", "单一来源",
                   "框架协议采购", "招标", "采购项目", "项目采购")


def refine_category(kind: str, title: str) -> str:
    """按标题内容判定这条到底是不是招投标信息。

    为什么需要:有些源(如碳排放交易网的"碳交易/碳金融"栏目)整体标成 tender,
    但里面大部分是市场资讯,直接混进"重点标讯"会很干扰阅读。
    规则:
      · 标题含招投标字眼 → 归为 tender(不管来源)
      · 标着 tender 但标题毫无招投标字眼 → 其实是资讯,降为 news
      · policy / methodology 保持不动(政策文件里出现"招标"不代表它是标讯)
    """
    has = any(k in title for k in TENDER_TITLE_KW)
    if kind in ("tender", "news"):
        return "tender" if has else "news"
    return kind


def guess_kind(text: str, default: str = "news") -> str:
    """从标题+正文片段猜归类,猜不准就用默认值。"""
    if any(h in text for h in METHOD_HINTS):
        return "methodology"
    if any(h in text for h in TENDER_HINTS):
        return "tender"
    if any(h in text for h in POLICY_HINTS):
        return "policy"
    return default


def collect_manual_links(cfg_sources: dict, fetcher: "Fetcher") -> list[dict]:
    """读取 config/manual_links.txt,逐条抓取文章元信息。

    语法:一行一个链接,以 # 开头为注释;可在行尾加 #tender 等标签指定归类。
    抓过的链接缓存在 data/manual_cache.json,不重复抓取(微信文章页有 3MB+,不必每天重抓)。
    """
    src = next((s for s in (cfg_sources.get("sources") or [])
                if s.get("adapter") == "manual_links"), None)
    if not src or not src.get("enabled"):
        return []
    path = os.path.join(ROOT, src.get("file", "config/manual_links.txt"))
    if not os.path.exists(path):
        log(f"  ! 找不到手动链接文件: {path}")
        return []

    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    cache = load_json_file(MANUAL_CACHE_PATH, {})
    items: list[dict] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        # tag 默认 None:只有用户显式写了 #tender 这类标签才覆盖自动识别,
        # 否则裸链接会被默认值短路,永远归成 news。
        tag, url = None, line
        m = re.match(r"^(https?://\S+)\s*#(\w+)\s*$", line)
        if m:
            url, tag = m.group(1), m.group(2)
        if not url.startswith("http"):
            continue
        try:
            if url in cache and cache[url].get("title"):
                meta = cache[url]
            else:
                meta = fetch_article_meta(url, fetcher, src.get("encoding"))
                meta["fetched_at"] = today_str()
                cache[url] = meta
            if not meta.get("title"):
                log(f"  ! 手动链接抓不到标题,已跳过: {url[:70]}")
                continue
            blob = f"{meta['title']} {meta.get('snippet', '')[:400]}"
            kind = tag if tag in VALID_KINDS else guess_kind(blob, src.get("kind", "news"))
            items.append({
                "title": meta["title"],
                "url": url,
                "date": meta.get("date"),
                "source_id": src["id"],
                "source_name": meta.get("source") or src.get("name", "手动投稿"),
                "kind": kind,
                "summary": meta.get("snippet", "")[:400],
            })
        except Exception as e:  # noqa: BLE001
            log(f"  ! 手动链接失败({str(e)[:50]}): {url[:60]}")
    save_json_file(MANUAL_CACHE_PATH, cache)
    return items


def build(cfg_sources: dict, kw: dict, force_all: bool = False) -> dict:
    scorer = Scorer(kw)
    settings = cfg_sources.get("settings") or {}
    max_age = int(settings.get("max_age_days", 0) or 0)
    # 源默认地区:全国性政策源没有地名时,归为"全国"而不是"未分类"
    default_regions = {s["id"]: s.get("default_region", "未分类")
                       for s in (cfg_sources.get("sources") or [])}

    log("抓取数据源…")
    raw, report = collect(cfg_sources, force_all=force_all)
    log(f"共抓取 {len(raw)} 条原始条目,开始关键词过滤…")

    cutoff = None
    if max_age > 0:
        cutoff = (now_cst() - timedelta(days=max_age)).strftime("%Y-%m-%d")

    kept, dropped_ex, dropped_old = 0, 0, 0
    filtered: list[dict] = []
    for it in raw:
        # 丢弃过期条目:过期标讯对"每日雷达"是负价值
        if cutoff and it.get("date") and it["date"] < cutoff:
            dropped_old += 1
            continue
        blob = f"{it['title']} {it.get('summary', '')}"
        if not scorer.is_relevant(blob):
            dropped_ex += 1
            continue
        sc = scorer.score(it["title"], it.get("summary", ""))
        it.update(sc)
        it["region"] = scorer.detect_region(it["title"], default_regions.get(it["source_id"], "未分类"))
        it["locality"] = scorer.detect_locality(it["title"]) or ""
        # 按标题内容重新判定是不是真的招投标公告(源头 kind 不够准)
        it["category"] = refine_category(it.get("kind", "news"), it["title"])
        # 无日期的条目排后面
        it["_dated"] = 1 if it.get("date") else 0
        filtered.append(it)
    kept = len(filtered)
    log(f"过滤后保留 {kept} 条(丢弃 {dropped_ex} 条不相关, {dropped_old} 条过期)")

    deduped = dedupe(filtered)
    log(f"去重后 {len(deduped)} 条")

    return {"items": deduped, "sources": report, "raw_count": len(raw),
            "dropped_count": dropped_ex, "dropped_old": dropped_old,
            "generated_at": now_cst().isoformat()}
