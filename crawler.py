# -*- coding: utf-8 -*-
"""
新聞蒐集核心模組
================

輸入目標公司名稱 -> 自動展開關鍵字 -> 多來源蒐集新聞 -> 解析全文 -> 回傳 Article 清單。

來源
----
1. Google News RSS  (news.google.com)  ── 涵蓋面最廣，含經濟日報、工商時報、自由財經、
   Yahoo、鉅亨、TechNews 等；RSS 連結為 Google 轉址，本模組會還原成原始網址。
2. 鉅亨網 cnyes 搜尋 API              ── 補強財經類新聞，且可直接取得全文。

所有網路請求皆為公開頁面的一般讀取，並內建節流（預設每次請求間隔 0.8 秒）。
"""

from __future__ import annotations

__version__ = "1.5.7"

import html as _html
import json
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, List, Optional

import feedparser
import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"}

TPE = timezone(timedelta(hours=8))

# 常見公司名稱後綴／地區前綴，用來裁出「核心字號」
_SUFFIXES = [
    "股份有限公司", "有限公司", "公司", "企業社", "工業社",
    "股份有限公司台灣分公司", "台灣分公司", "臺灣分公司", "分公司",
]
_PREFIXES = [
    "台灣", "臺灣", "香港商", "新加坡商", "薩摩亞商", "大陸商", "美商", "日商",
    "英屬維京群島商", "英商", "德商", "法商", "荷商", "韓商", "開曼群島商",
]


# --------------------------------------------------------------------------- #
# 資料結構
# --------------------------------------------------------------------------- #
@dataclass
class Article:
    title: str
    url: str = ""
    source: str = ""                     # 新聞來源（媒體名稱）
    published: Optional[datetime] = None
    author: str = ""                     # 記者
    subtitle: str = ""                   # 副標
    paragraphs: List[str] = field(default_factory=list)   # 內文段落
    origin: str = ""                     # 由哪個來源蒐集到（google / cnyes / official）
    matched: List[str] = field(default_factory=list)      # 命中的公司關鍵字
    topics: List[str] = field(default_factory=list)       # 命中的關注議題
    entity: str = ""                     # 對應到哪一家（受訪公司／母公司／相關企業）
    fetch_error: str = ""

    @property
    def date_str(self) -> str:
        return self.published.strftime("%Y-%m-%d") if self.published else ""

    @property
    def body(self) -> str:
        return "\n".join(self.paragraphs)

    @property
    def char_count(self) -> int:
        return sum(len(p) for p in self.paragraphs)

    @classmethod
    def from_dict(cls, d: dict) -> "Article":
        """由 to_dict() 產生的 JSON 還原成 Article。"""
        pub = None
        if d.get("published"):
            try:
                pub = datetime.strptime(str(d["published"])[:10], "%Y-%m-%d")
            except ValueError:
                pub = None
        return cls(
            title=d.get("title", ""), url=d.get("url", ""),
            source=d.get("source", ""), published=pub,
            author=d.get("author", ""), subtitle=d.get("subtitle", ""),
            paragraphs=list(d.get("paragraphs", [])),
            origin=d.get("origin", ""), matched=list(d.get("matched", [])),
            topics=list(d.get("topics", [])), entity=d.get("entity", ""),
            fetch_error=d.get("fetch_error", ""))

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published": self.date_str,
            "author": self.author,
            "subtitle": self.subtitle,
            "paragraphs": self.paragraphs,
            "origin": self.origin,
            "matched": self.matched,
            "topics": self.topics,
            "entity": self.entity,
            "fetch_error": self.fetch_error,
        }


# --------------------------------------------------------------------------- #
# 關鍵字展開
# --------------------------------------------------------------------------- #
def make_keywords(company: str, extra: Optional[Iterable[str]] = None) -> List[str]:
    """由公司全名展開檢索關鍵字。

    例：「台灣禾邦電子有限公司」-> ['台灣禾邦電子有限公司', '台灣禾邦電子', '禾邦電子', '禾邦']
    """
    name = (company or "").strip()
    out: List[str] = []

    def push(s: str) -> None:
        s = s.strip()
        if len(s) >= 2 and s not in out:
            out.append(s)

    push(name)

    core = name
    for suf in sorted(_SUFFIXES, key=len, reverse=True):
        if core.endswith(suf):
            core = core[: -len(suf)]
            break
    push(core)

    stripped = core
    for pre in sorted(_PREFIXES, key=len, reverse=True):
        if stripped.startswith(pre):
            stripped = stripped[len(pre):]
            break
    push(stripped)

    # 再去掉「電子／科技／半導體／國際／實業…」等泛用尾字，取字號
    for tail in ["電子", "科技", "半導體", "國際", "實業", "工業", "生技",
                 "光電", "材料", "資訊", "投資", "開發", "控股", "精密", "化學"]:
        if stripped.endswith(tail) and len(stripped) - len(tail) >= 2:
            push(stripped[: -len(tail)])
            break

    for e in (extra or []):
        push(e)

    return out


_COLUMN_TAIL = re.compile(r"\s*[-|｜–—]\s*[^-|｜–—]{1,6}$")


def clean_title(title: str, source: str = "") -> str:
    """清掉 Google News 標題尾端的媒體名與版面欄目，例如「…優選- 日報」。"""
    t = (title or "").strip()
    if source and t.endswith(source):
        t = _COLUMN_TAIL.sub("", t).strip()
    for _ in range(2):
        m = _COLUMN_TAIL.search(t)
        if not m:
            break
        tail = m.group(0).lstrip(" -|｜–—").strip()
        if tail in {"日報", "商情", "財經", "產業", "焦點", "要聞", "生活", "頭條",
                    "股市", "科技", "證券", "國際", "兩岸", "地方", "工商時報",
                    "經濟日報", "自由時報", "中時新聞網", "聯合新聞網", "鉅亨網",
                    "Yahoo奇摩股市", "MoneyDJ理財網"} or tail == source:
            t = t[: m.start()].strip()
        else:
            break
    return t


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", "", s).lower()


def match_keywords(text: str, keywords: Iterable[str]) -> List[str]:
    t = _norm(text)
    return [k for k in keywords if _norm(k) and _norm(k) in t]


# --------------------------------------------------------------------------- #
# 主題分類：訪視報告關注的議題
# --------------------------------------------------------------------------- #
TOPIC_KEYWORDS = {
    "美國加徵關稅": ["加徵關稅", "對等關稅", "懲罰性關稅", "關稅", "課稅", "反傾銷",
                 "貿易戰", "232條款", "301條款", "貿易救濟"],
    "地緣政治": ["地緣政治", "台海", "兩岸情勢", "中美角力", "美中對抗", "制裁",
               "去風險", "紅色供應鏈", "脫鉤"],
    "科技戰": ["科技戰", "晶片戰", "技術封鎖", "技術管制", "卡脖子", "先進製程管制"],
    "國家安全": ["國家安全", "國安", "敏感科技", "關鍵技術", "投審會", "陸資審查",
               "外資審查", "營運總部審查"],
    "資安": ["資安", "資訊安全", "網路安全", "個資", "駭客", "營業秘密", "資料外洩"],
    "出口管制": ["出口管制", "實體清單", "entity list", "出口許可", "管制清單",
               "禁售", "禁令", "EAR"],
    "電動車／車用": ["電動車", "車用", "車規", "自駕", "動力電池", "充電樁",
                 "車電", "EV"],
    "供應鏈": ["供應鏈", "產業鏈", "轉單", "分散生產", "去中化", "在地生產",
             "產能移轉", "第二供應來源", "斷鏈"],
    "國產化": ["國產化", "自主可控", "進口替代", "本土化", "國產替代", "在地化"],
    "AI": ["人工智慧", "生成式", "大模型", "算力", "AI伺服器", "資料中心", "AI"],
    "機器人": ["機器人", "人形機器人", "協作機器人", "自動化產線"],
    "量子科技": ["量子電腦", "量子運算", "量子通訊", "量子科技", "量子"],
    "能源與減碳": ["減碳", "淨零", "碳費", "碳關稅", "CBAM", "綠電", "再生能源",
                "儲能", "節能", "ESG", "太陽能", "風電"],
    "數位化": ["數位轉型", "數位化", "智慧製造", "工業4.0", "智慧工廠", "上雲"],
}

# 供應鏈轉移：地區詞 + 移轉動作詞同時出現才算，避免「美國」這種泛用詞誤判
SUPPLY_CHAIN_REGIONS = {
    "東南亞": ["東南亞", "越南", "泰國", "馬來西亞", "印尼", "菲律賓", "新加坡",
             "檳城", "新南向"],
    "北美": ["美國", "墨西哥", "加拿大", "北美", "德州", "亞利桑那", "俄亥俄"],
    "歐洲": ["歐洲", "歐盟", "德國", "波蘭", "捷克", "匈牙利", "荷蘭", "英國",
            "斯洛伐克"],
    "印度": ["印度"],
}
_MOVE_WORDS = ["設廠", "建廠", "新廠", "擴廠", "擴產", "遷廠", "移轉", "轉移",
               "布局", "產能", "投資設立", "落地", "設立子公司", "生產基地",
               "在地生產", "西進", "南向", "產線"]


def _kw_pattern(word: str) -> re.Pattern:
    """英數關鍵字加詞界（避免 AI 命中 said／chain），中文用單純比對。"""
    w = word.strip()
    if re.fullmatch(r"[A-Za-z0-9.\- ]+", w):
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(w.lower()) + r"(?![A-Za-z0-9])")
    return re.compile(re.escape(w))


_TOPIC_PATTERNS = {name: [_kw_pattern(w) for w in words]
                   for name, words in TOPIC_KEYWORDS.items()}
_REGION_PATTERNS = {name: [_kw_pattern(w) for w in words]
                    for name, words in SUPPLY_CHAIN_REGIONS.items()}
_MOVE_PATTERNS = [_kw_pattern(w) for w in _MOVE_WORDS]


def match_topics(text: str) -> List[str]:
    """回傳文章命中的關注議題；供應鏈轉移另標出地區。"""
    t = _norm(text)
    hits: List[str] = []
    for name, pats in _TOPIC_PATTERNS.items():
        if any(p.search(t) for p in pats):
            hits.append(name)
    if any(p.search(t) for p in _MOVE_PATTERNS):
        for region, pats in _REGION_PATTERNS.items():
            if any(p.search(t) for p in pats):
                hits.append("供應鏈轉移（%s）" % region)
    return hits


# 給 Google News 用的主題檢索詞組（分兩組，避免單一 query 過長）
TOPIC_QUERY_GROUPS = [
    ["關稅", "地緣政治", "科技戰", "出口管制", "國家安全", "資安", "制裁"],
    ["供應鏈", "設廠", "產能", "國產化", "AI", "機器人", "量子", "減碳",
     "數位轉型", "電動車"],
]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Fetcher:
    """帶節流與重試的 HTTP 取用器。"""

    def __init__(self, delay: float = 0.8, timeout: int = 25, retries: int = 2):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0

    def _wait(self) -> None:
        gap = time.time() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.time()

    def get(self, url: str, **kw) -> requests.Response:
        last_err: Optional[Exception] = None
        for i in range(self.retries + 1):
            self._wait()
            try:
                r = self.session.get(url, timeout=self.timeout, **kw)
                r.raise_for_status()
                return r
            except Exception as e:      # noqa: BLE001
                last_err = e
                time.sleep(0.6 * (i + 1))
        raise last_err  # type: ignore[misc]

    def post(self, url: str, **kw) -> requests.Response:
        self._wait()
        r = self.session.post(url, timeout=self.timeout, **kw)
        r.raise_for_status()
        return r


# --------------------------------------------------------------------------- #
# Google News
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 來源清單與分級（依「新聞來源彙整.xlsx」實際使用統計）
# --------------------------------------------------------------------------- #
# 網域 -> 正式媒體名稱，讓輸出的來源標示與既有報告一致
DOMAIN_SOURCE = {
    "money.udn.com": "經濟日報",
    "udn.com": "聯合報",
    "ctee.com.tw": "工商時報",
    "news.cnyes.com": "鉅亨網",
    "cnyes.com": "鉅亨網",
    "moneydj.com": "MoneyDJ",
    "ltn.com.tw": "自由時報",
    "chinatimes.com": "中時新聞網",
    "cna.com.tw": "中央社",
    "technews.tw": "科技新報",
    "digitimes.com.tw": "DIGITIMES",
    "wealth.com.tw": "財訊",
    "businesstoday.com.tw": "今周刊",
    "businessweekly.com.tw": "商業周刊",
    "cmoney.tw": "CMoney",
    "investing.com": "Investing.com",
    "ithome.com.tw": "iThome",
    "bnext.com.tw": "數位時代",
    "gvm.com.tw": "遠見雜誌",
    "ctwant.com": "CTWANT",
    "setn.com": "三立新聞網",
    "ebc.net.tw": "東森新聞",
    "nownews.com": "NOWnews",
    "tvbs.com.tw": "TVBS新聞網",
    "mirrormedia.mg": "鏡週刊",
    "rti.org.tw": "中央廣播電臺",
    "sina.com.cn": "新浪財經",
    "sina.com.tw": "新浪財經",
    "eastmoney.com": "東方財富網",
    "stcn.com": "證券時報網",
    "zqrb.cn": "證券日報網",
    "yahoo.com": "Yahoo奇摩股市",
    "mops.twse.com.tw": "公開資訊觀測站",
    "twse.com.tw": "臺灣證券交易所",
    "tpex.org.tw": "證券櫃檯買賣中心",
}

# 第一優先來源：定向補搜，並在排序時優先納入
PRIORITY_DOMAINS = ["money.udn.com", "ctee.com.tw", "news.cnyes.com"]
PRIORITY_NAMES = ["經濟日報", "工商時報", "鉅亨網", "公司官網"]

# 第二優先：既有報告中經常出現的台灣財經／科技媒體
SECOND_NAMES = ["MoneyDJ", "自由時報", "聯合報", "中央社", "科技新報", "DIGITIMES",
                "財訊", "今周刊", "商業周刊", "CMoney", "中時新聞網", "數位時代",
                "iThome", "遠見雜誌", "公開資訊觀測站", "Investing.com"]


def normalize_source(url: str, fallback: str = "") -> str:
    """由網域判定正式媒體名稱；判不出來就沿用 Google News 給的名稱。"""
    host = urllib.parse.urlparse(url or "").netloc.lower()
    for dom, name in DOMAIN_SOURCE.items():
        if host.endswith(dom) or dom in host:
            return name
    return fallback or host


def source_tier(article: "Article", official_hosts: Optional[Iterable[str]] = None) -> int:
    """1 = 經濟日報／工商時報／鉅亨網／官網，2 = 常用財經媒體，3 = 其他。"""
    host = urllib.parse.urlparse(article.url or "").netloc.lower()
    for oh in (official_hosts or []):
        if oh and oh.lower().lstrip("www.") in host:
            return 1
    if article.origin == "official" or article.source.endswith("官網"):
        return 1
    if article.source in PRIORITY_NAMES:
        return 1
    if article.source in SECOND_NAMES:
        return 2
    return 3


# 優先來源的「新鮮度加權」天數：第一優先等於自動年輕 180 天，第二優先 60 天。
# 用加權而非硬性分層，優先來源會排前面，但不會把其他媒體整批擠掉。
TIER_BOOST_DAYS = {1: 180, 2: 60, 3: 0}


def rank_score(article: "Article", now: datetime,
               official_hosts: Optional[Iterable[str]] = None,
               boost_days: Optional[dict] = None,
               topic_boost: int = 120) -> float:
    """分數越小越前面。

    以「幾天前」為基準，再做三項調整：優先來源扣天數、命中關注議題扣天數
    （最多算兩個議題）、標題沒命中公司關鍵字加罰一年。
    """
    boost = boost_days or TIER_BOOST_DAYS
    days = ((now - article.published).days
            if article.published else 3650)      # 沒日期的排最後
    days -= boost.get(source_tier(article, official_hosts), 0)
    days -= min(len(article.topics), 2) * topic_boost   # 最多計兩項，避免加權失控
    if not article.matched:
        days += 365
    return days


GNEWS_RSS = "https://news.google.com/rss/search"


def _gnews_query(keywords: List[str], since: datetime, until: datetime,
                 site: str = "", topics: Optional[Iterable[str]] = None) -> str:
    kw = " OR ".join('"%s"' % k for k in keywords)
    q = "(%s)" % kw
    tl = [t for t in (topics or []) if t]
    if tl:
        q += " (%s)" % " OR ".join('"%s"' % t for t in tl)
    if site:
        q += " site:%s" % site
    return "%s after:%s before:%s" % (
        q, since.strftime("%Y-%m-%d"), (until + timedelta(days=1)).strftime("%Y-%m-%d"))


def search_google_news(keywords: List[str], since: datetime, until: datetime,
                       fetcher: Optional[Fetcher] = None,
                       hl: str = "zh-TW", gl: str = "TW",
                       ceid: str = "TW:zh-Hant", site: str = "",
                       topics: Optional[Iterable[str]] = None) -> List[Article]:
    f = fetcher or Fetcher()
    q = _gnews_query(keywords, since, until, site, topics)
    url = "%s?q=%s&hl=%s&gl=%s&ceid=%s" % (
        GNEWS_RSS, urllib.parse.quote(q), hl, gl, urllib.parse.quote(ceid))
    resp = f.get(url)
    feed = feedparser.parse(resp.text)

    out: List[Article] = []
    for e in feed.entries:
        title = _html.unescape(e.get("title", "")).strip()
        src = ""
        if isinstance(e.get("source"), dict):
            src = e["source"].get("title", "")
        # Google 會在標題尾端附上「 - 媒體名」，移除之
        if src and title.endswith(" - " + src):
            title = title[: -(len(src) + 3)].strip()

        pub = None
        if e.get("published_parsed"):
            pub = (datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                   .astimezone(TPE).replace(tzinfo=None))

        out.append(Article(title=clean_title(title, src), url=e.get("link", ""),
                           source=src, published=pub,
                           origin="google-site" if site else "google"))
    return out


def resolve_google_url(gnews_url: str, fetcher: Optional[Fetcher] = None) -> str:
    """把 news.google.com/rss/articles/... 還原成原始新聞網址。

    作法：讀取該頁的 c-wiz 簽章（data-n-a-id / ts / sg），再呼叫 Google 內部的
    batchexecute (garturlreq) 取回真實網址。失敗時回傳原網址。
    """
    if "news.google.com" not in gnews_url:
        return gnews_url
    f = fetcher or Fetcher()
    try:
        page = f.get(gnews_url).text
        m = re.search(r'data-n-a-id="([^"]+)"', page)
        aid = m.group(1) if m else None
        m = re.search(r'data-n-a-ts="(\d+)"', page)
        ts = int(m.group(1)) if m else None
        m = re.search(r'data-n-a-sg="([^"]+)"', page)
        sg = m.group(1) if m else None
        if not aid:
            # 舊版 RSS 連結的 base64 段落即是 id
            m = re.search(r"/articles/([^?]+)", gnews_url)
            aid = m.group(1) if m else None
        if not (aid and ts and sg):
            return gnews_url

        inner = json.dumps([
            "garturlreq",
            [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
              None, None, None, None, None, 0, 1],
             "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
            aid, ts, sg])
        body = {"f.req": json.dumps([[["Fbv4je", inner]]])}
        r = f.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                   data=body,
                   headers={"Content-Type":
                            "application/x-www-form-urlencoded;charset=UTF-8"})
        m = re.search(r'garturlres.{0,10}(https?://[^"\\\s]+)', r.text)
        if m:
            return m.group(1)
    except Exception:      # noqa: BLE001
        pass
    return gnews_url


# --------------------------------------------------------------------------- #
# 鉅亨網 cnyes
# --------------------------------------------------------------------------- #
CNYES_SEARCH = "https://api.cnyes.com/media/api/v1/search/news"
CNYES_DETAIL = "https://api.cnyes.com/media/api/v1/news/%s"


def search_cnyes(keyword: str, since: datetime, until: datetime,
                 fetcher: Optional[Fetcher] = None, pages: int = 2) -> List[Article]:
    f = fetcher or Fetcher()
    out: List[Article] = []
    for page in range(1, pages + 1):
        try:
            r = f.get(CNYES_SEARCH, params={"q": keyword, "limit": 30, "page": page})
            data = r.json().get("items", {})
        except Exception:      # noqa: BLE001
            break
        rows = data.get("data") or []
        if not rows:
            break
        for it in rows:
            ts = it.get("publishAt")
            pub = (datetime.fromtimestamp(ts, TPE).replace(tzinfo=None)
                   if ts else None)
            if pub and not (since <= pub <= until):
                continue
            nid = it.get("newsId")
            out.append(Article(
                title=_html.unescape(it.get("title", "")).strip(),
                url="https://news.cnyes.com/news/id/%s" % nid,
                source="鉅亨網",
                published=pub,
                origin="cnyes",
            ))
        if page >= (data.get("last_page") or 1):
            break
    return out


def fetch_cnyes_body(article: Article, fetcher: Optional[Fetcher] = None) -> bool:
    m = re.search(r"/id/(\d+)", article.url)
    if not m:
        return False
    f = fetcher or Fetcher()
    try:
        r = f.get(CNYES_DETAIL % m.group(1))
        item = r.json().get("items", {})
        soup = BeautifulSoup(item.get("content", ""), "html.parser")
        paras = [p.get_text(" ", strip=True) for p in soup.find_all(["p", "h2", "h3"])]
        article.paragraphs = [p for p in paras if len(p) >= 12]
        return bool(article.paragraphs)
    except Exception:      # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 編碼判斷
# --------------------------------------------------------------------------- #
_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)

# 中文網站常見編碼；big5 一律用 cp950（微軟擴充版）、gb2312 用 gb18030 才不會缺字
_ENC_ALIAS = {
    "big5": "cp950", "big5-hkscs": "big5hkscs", "ms950": "cp950",
    "gb2312": "gb18030", "gbk": "gb18030", "utf8": "utf-8",
}
_ENC_CANDIDATES = ["utf-8", "cp950", "gb18030", "big5hkscs", "utf-16"]


def _mojibake_score(text: str) -> float:
    """回傳亂碼程度 0~1：解碼失敗字元與拉丁擴充區字元的比例。"""
    if not text:
        return 1.0
    sample = text[:4000]
    bad = 0
    for ch in sample:
        o = ord(ch)
        # U+FFFD 代表解碼失敗；U+0080~U+00FF 是 Big5／GB 被誤當 latin-1 的典型徵狀
        if o == 0xFFFD or 0x80 <= o <= 0xFF:
            bad += 1
    return bad / len(sample)


def decode_html(content: bytes, header_charset: str = "") -> str:
    """依「HTTP 標頭 → HTML meta → 逐一試解」順序決定編碼，並用亂碼比例挑最好的。

    requests 遇到沒有標明 charset 的 text/html 會預設 ISO-8859-1，
    Big5／GB 網頁因此整篇變亂碼；這裡不依賴那個預設值。
    """
    order: List[str] = []

    def push(enc: Optional[str]) -> None:
        if not enc:
            return
        e = _ENC_ALIAS.get(enc.strip().lower(), enc.strip().lower())
        if e and e not in order and e != "iso-8859-1":
            order.append(e)

    push(header_charset)
    m = _META_CHARSET.search(content[:4096])
    if m:
        push(m.group(1).decode("ascii", "ignore"))
    for e in _ENC_CANDIDATES:
        push(e)

    best, best_score = "", 1.1
    for enc in order:
        try:
            text = content.decode(enc, errors="replace")
        except (LookupError, UnicodeError):
            continue
        score = _mojibake_score(text)
        if score < 0.005:            # 幾乎沒有可疑字元，直接採用
            return text
        if score < best_score:
            best, best_score = text, score
    return best or content.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# 公司官網
# --------------------------------------------------------------------------- #
_NEWS_NAV = re.compile(
    r"(新聞|最新消息|消息|公告|媒體|報導|訊息|動態|news|press|media|release)", re.I)
_DATE_IN_TEXT = re.compile(
    r"(20\d{2})[./年-]\s?(\d{1,2})[./月-]\s?(\d{1,2})")


def _abs(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)


def find_official_news_pages(base_url: str, fetcher: Optional[Fetcher] = None,
                             limit: int = 5) -> List[str]:
    """從官網首頁找出「最新消息／新聞中心」之類的列表頁。"""
    f = fetcher or Fetcher()
    try:
        r = f.get(base_url)
    except Exception:      # noqa: BLE001
        return []
    host = urllib.parse.urlparse(r.url).netloc
    soup = BeautifulSoup(decode_html(r.content), "html.parser")
    pages, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        text = a.get_text(" ", strip=True)
        if not (_NEWS_NAV.search(text) or _NEWS_NAV.search(href)):
            continue
        url = _abs(r.url, href)
        if urllib.parse.urlparse(url).netloc != host:
            continue
        if url in seen or url.rstrip("/") == r.url.rstrip("/"):
            continue
        seen.add(url)
        pages.append(url)
        if len(pages) >= limit:
            break
    return pages


def search_official_site(base_url: str, company: str,
                         since: datetime, until: datetime,
                         fetcher: Optional[Fetcher] = None,
                         max_items: int = 12) -> List[Article]:
    """抓公司官網的新聞／最新消息列表，回傳候選 Article（尚未取全文）。"""
    if not base_url:
        return []
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    f = fetcher or Fetcher()
    label = "%s官網" % (company or urllib.parse.urlparse(base_url).netloc)

    pages = [base_url] + find_official_news_pages(base_url, f)
    out: List[Article] = []
    seen = set()
    for page in pages:
        try:
            r = f.get(page)
        except Exception:      # noqa: BLE001
            continue
        host = urllib.parse.urlparse(r.url).netloc
        soup = BeautifulSoup(decode_html(r.content), "html.parser")
        for a in soup.find_all("a", href=True):
            text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            if len(text) < 8 or len(text) > 80:
                continue
            url = urllib.parse.urldefrag(_abs(r.url, str(a["href"]))).url
            page_url = urllib.parse.urldefrag(r.url).url
            if urllib.parse.urlparse(url).netloc != host or url in seen:
                continue
            if url.rstrip("/") == page_url.rstrip("/"):
                continue

            # 日期：先看連結文字與周邊，再看網址
            ctx = text
            parent = a.find_parent(["li", "div", "tr", "article"])
            if parent is not None:
                ctx = re.sub(r"\s+", " ", parent.get_text(" ", strip=True))[:120]
            m = _DATE_IN_TEXT.search(ctx) or _DATE_IN_TEXT.search(url)
            pub = None
            if m:
                try:
                    pub = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except ValueError:
                    pub = None
            if pub and not (since <= pub <= until):
                continue
            if not pub and not re.search(r"(news|press|release|detail|article|\d{4})",
                                         url, re.I):
                continue

            seen.add(url)
            title = _DATE_IN_TEXT.sub("", text).strip(" .-|｜")
            out.append(Article(title=title or text, url=url, source=label,
                               published=pub, origin="official"))
            if len(out) >= max_items:
                break
        if len(out) >= max_items:
            break

    # 官網新聞列表通常都有日期；若已有足夠帶日期的項目，就丟掉沒日期的導覽連結
    dated = [a for a in out if a.published]
    return dated if len(dated) >= 3 else out


# --------------------------------------------------------------------------- #
# 全文擷取
# --------------------------------------------------------------------------- #
_SITE_SELECTORS = [
    ("money.udn.com", "#article_body"),
    ("udn.com", "section.article-content__editor"),
    ("ctee.com.tw", "div.entry-content"),
    ("news.cnyes.com", "main"),
    ("technews.tw", "div.indent"),
    ("ltn.com.tw", "div.text"),
    ("chinatimes.com", "div.article-body"),
    ("news.tvbs.com.tw", "div.article_content"),
    ("ettoday.net", "div.story"),
    ("setn.com", "div#Content1"),
    ("moneydj.com", "#MainContent"),
    ("digitimes.com.tw", "div.article-content"),
    ("bnext.com.tw", "div.article-content"),
    ("wealth.com.tw", "div.article-content"),
    ("cw.com.tw", "div.article-content"),
    ("gvm.com.tw", "div.article-content"),
    ("yahoo.com", "div.caas-body"),
    ("nownews.com", "div.article-content"),
    ("news.pts.org.tw", "div.post-article"),
    ("rti.org.tw", "div.article-content"),
    ("techorange.com", "div.elementor-widget-theme-post-content"),
    ("buzzorange.com", "div.elementor-widget-theme-post-content"),
]

# 「相關文章／推薦閱讀」區塊。這些是版面元件不是內文，卻常和正文包在同一個容器裡，
# 而且標題多半是 <h3>——_paragraphs_from() 會把它們當成小標收進來。實測 TechOrange
# 一篇會多帶四則推薦新聞標題，而且那些標題夠長（40 字以上），事後靠字數與標點猜測
# 的 _trim_tail_noise() 砍不掉。解析前整塊移除，比事後猜可靠得多。
_NOISE_SELECTORS = [
    ".elementor-posts-container",      # Elementor 文章列表元件，WordPress 站台常見
    ".elementor-post__title",
    ".related-posts", ".related-post", ".related-articles", ".related-news",
    ".recommend-list", ".recommended-posts", ".popular-posts", ".hot-posts",
    "#related-posts", "#related_posts", "#recommend",
]

_DROP_PAT = re.compile(
    r"(延伸閱讀|更多.{0,6}報導|相關新聞|不用抽|不用搶|立即下載|加入.{0,4}粉絲團|"
    r"責任編輯|本文.{0,8}授權|原文出處|訂閱|廣告|Advertisement|留言|分享至|"
    r"剪貼簿|複製到|請點擊|看更多|熱門推薦|推薦閱讀|你可能也想看|字級設定|"
    r"版權所有|未經授權|免責聲明|投資有風險)")


def strip_noise(soup: BeautifulSoup) -> BeautifulSoup:
    """就地移除版面元件（相關文章／推薦閱讀等），回傳同一個 soup。"""
    for sel in _NOISE_SELECTORS:
        for bad in soup.select(sel):
            bad.decompose()
    return soup


def _trim_tail_noise(paras: List[str], max_drop: int = 6) -> List[str]:
    """砍掉文末的「推薦新聞」標題列：連續數則短句、無標點者視為連結清單。"""
    out = list(paras)
    dropped = 0
    while out and dropped < max_drop:
        last = out[-1]
        if len(last) <= 34 and not re.search(r"[。；]", last):
            out.pop()
            dropped += 1
        else:
            break
    return out


def _from_jsonld(soup: BeautifulSoup) -> tuple[str, str, str, str]:
    """回傳 (articleBody, headline, datePublished, author)。"""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:      # noqa: BLE001
            continue
        cands = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and "@graph" in data:
            cands = data["@graph"]
        for d in cands:
            if not isinstance(d, dict):
                continue
            t = d.get("@type", "")
            types = t if isinstance(t, list) else [t]
            if not any(str(x).endswith(("Article", "NewsArticle", "BlogPosting"))
                       for x in types):
                continue
            author = d.get("author")
            if isinstance(author, dict):
                author = author.get("name", "")
            elif isinstance(author, list) and author:
                a0 = author[0]
                author = a0.get("name", "") if isinstance(a0, dict) else str(a0)
            return (str(d.get("articleBody", "") or ""),
                    str(d.get("headline", "") or ""),
                    str(d.get("datePublished", "") or ""),
                    str(author or ""))
    return "", "", "", ""


# 出現以下字樣即代表正文結束，其後多為推薦新聞、免責聲明
_END_PAT = re.compile(
    r"(延伸閱讀|更多.{0,10}報導|相關新聞|相關報導|責任編輯|原文出處|看更多|"
    r"你可能也想看|熱門推薦|推薦閱讀|更多內容|免責聲明|未經授權|版權所有|"
    r"本資料僅供參考|投資人應獨立判斷|不代表本網立場|加入.{0,4}粉絲團)")


def _link_density(el) -> tuple:
    """回傳 (連結文字佔比, 連結數)。整段都是連結文字者，多半是推薦新聞清單。"""
    total = len(re.sub(r"\s+", "", el.get_text(" ", strip=True)))
    if not total:
        return 0.0, 0
    links = el.find_all("a")
    link_len = sum(len(re.sub(r"\s+", "", a.get_text(" ", strip=True))) for a in links)
    return link_len / total, len(links)


# 連結文字佔比達這個門檻，就當成「整段都是連結」
_LINKY = 0.9


def _paragraphs_from(node) -> List[str]:
    """把節點裡的段落抓出來，順便濾掉推薦新聞的連結清單。

    連結清單有兩種長相，分別對應下面兩道處理：

    1. 好幾則擠在同一段，用「▪」之類的符號隔開（實測聯合新聞網）。這種段落的
       連結文字佔比接近 1、連結數 ≥ 2，出現在文中任何位置都可以直接丟。
    2. 一則一段掛在文末（實測經濟日報）。單一連結不能光看佔比就丟——正文引用
       某個標題也可能整段是連結——但**出現在文末**就幾乎可以確定是推薦區，
       所以只砍文末連續的純連結段落。

    這比靠字數與標點猜測可靠：實測聯合新聞網那段有 137 字、經濟日報那段 42 字，
    都超過 `_trim_tail_noise()` 的 34 字門檻，靠字數是砍不掉的。
    """
    paras: List[str] = []
    linky: List[bool] = []          # 與 paras 對齊：該段是否整段都是連結文字
    for el in node.find_all(["p", "h2", "h3", "h4"]):
        txt = el.get_text(" ", strip=True)
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt:
            continue
        if _END_PAT.search(txt) and len(paras) >= 2:
            break                      # 正文到此為止
        if len(txt) < 12 or _DROP_PAT.search(txt):
            continue
        if paras and txt == paras[-1]:
            continue
        dens, n_links = _link_density(el)
        if dens >= _LINKY and n_links >= 2:
            continue                   # 情況 1：多則連結擠成一段
        paras.append(txt)
        linky.append(dens >= _LINKY and n_links >= 1)

    while paras and linky[-1]:         # 情況 2：文末的純連結段落
        paras.pop()
        linky.pop()
    return paras


def _densest_block(soup: BeautifulSoup) -> List[str]:
    best, best_len = None, 0
    for node in soup.find_all(["article", "div", "section", "main"]):
        ps = node.find_all("p", recursive=True)
        if len(ps) < 3:
            continue
        length = sum(len(p.get_text(strip=True)) for p in ps)
        if length > best_len:
            best, best_len = node, length
    return _paragraphs_from(best) if best is not None else []


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    for pat, fmt in [
        (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "%Y-%m-%dT%H:%M:%S"),
        (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "%Y-%m-%d %H:%M:%S"),
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
        (r"\d{4}/\d{2}/\d{2}", "%Y/%m/%d"),
    ]:
        m = re.search(pat, s)
        if m:
            try:
                return datetime.strptime(m.group(0), fmt)
            except ValueError:
                continue
    return None


def fetch_article_body(article: Article, fetcher: Optional[Fetcher] = None) -> Article:
    """抓取並解析單篇新聞全文，就地寫回 article。"""
    f = fetcher or Fetcher()

    if "news.cnyes.com" in article.url and fetch_cnyes_body(article, f):
        return article

    try:
        resp = f.get(article.url)
    except Exception as e:      # noqa: BLE001
        article.fetch_error = "取得網頁失敗：%s" % e
        return article

    ctype = resp.headers.get("content-type", "")
    hdr_enc = ""
    mm = re.search(r"charset=([\w\-]+)", ctype, re.I)
    if mm:
        hdr_enc = mm.group(1)
    soup = BeautifulSoup(decode_html(resp.content, hdr_enc), "html.parser")
    for bad in soup(["script", "style", "noscript", "iframe", "figure", "aside",
                     "nav", "header", "footer", "form"]):
        bad.decompose()
    strip_noise(soup)

    body, headline, date_s, author = _from_jsonld(soup)

    paras: List[str] = []
    host = urllib.parse.urlparse(resp.url).netloc
    for dom, sel in _SITE_SELECTORS:
        if dom in host:
            node = soup.select_one(sel)
            if node is not None:
                paras = _paragraphs_from(node)
            break

    if not paras and body:
        paras = [p.strip() for p in re.split(r"\n+|(?<=。)\s{2,}", body) if len(p.strip()) >= 12]
    if not paras:
        paras = _densest_block(soup)

    article.paragraphs = _trim_tail_noise(paras)
    paras = article.paragraphs
    if not article.title and headline:
        article.title = headline
    if not article.published:
        meta = soup.find("meta", property="article:published_time")
        mc = str(meta.get("content", "")) if meta is not None else ""
        article.published = _parse_dt(date_s) or _parse_dt(mc)
    if not article.author and author:
        article.author = str(author)[:30]
    if not article.source:
        st = soup.find("meta", property="og:site_name")
        article.source = str(st.get("content", "")) if st is not None else host
    if not paras:
        article.fetch_error = article.fetch_error or "無法解析內文（可能為付費牆或動態載入）"
    return article


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _reserve_slots(cand: List[Article], max_articles: int,
                   reserve: int) -> List[Article]:
    """把名額尾端保留給「未命中關注議題」的新聞。

    議題定向檢索會讓候選池充滿命中議題的新聞，再加上議題加權，一般新聞幾乎排不進
    名額——那等於把「優先某些議題」做成「限定某些議題」。

    作法是照原排序逐一取用，但議題新聞取滿 `max_articles - reserve` 篇之後就先擱置，
    把名額讓給後面的非議題新聞；被擱置的接在後面，不會被丟掉。原本排在最前面的
    非議題新聞不會因此被往後推。
    """
    if reserve <= 0 or max_articles <= 0:
        return cand
    if all(a.topics for a in cand):
        return cand

    topic_cap = max(max_articles - reserve, 0)
    picked: List[Article] = []
    deferred: List[Article] = []
    taken_topic = 0
    for a in cand:
        if len(picked) >= max_articles:
            deferred.append(a)
            continue
        if a.topics:
            if taken_topic >= topic_cap:
                deferred.append(a)
                continue
            taken_topic += 1
        picked.append(a)
    return picked + deferred


def _tag(articles: List[Article], entity: str) -> List[Article]:
    """標記這批結果是為哪一家對象檢索到的。"""
    for a in articles:
        if not a.entity:
            a.entity = entity
    return articles


def _dedup(articles: List[Article]) -> List[Article]:
    """去重：同網址、同標題，以及「一則標題是另一則的前綴」的同稿異版。

    同一則新聞常同時出現在媒體與公司官網，標題可能只差幾個字（例如結尾多了
    「領域」兩字），純比對前 40 字會漏掉，因此再做一次前綴包含檢查。
    """
    seen_u: set = set()
    keys: List[str] = []
    out: List[Article] = []
    for a in articles:
        kt = _norm(a.title)
        ku = re.sub(r"[?#].*$", "", a.url or "")
        if not kt or (ku and ku in seen_u):
            continue
        dup = False
        for k in keys:
            if k == kt:
                dup = True
                break
            short, long = (k, kt) if len(k) <= len(kt) else (kt, k)
            if len(short) >= 12 and long.startswith(short):
                dup = True
                break
        if dup:
            continue
        keys.append(kt)
        if ku:
            seen_u.add(ku)
        out.append(a)
    return out


def collect_news(company: str,
                 extra_keywords: Optional[Iterable[str]] = None,
                 years: float = 2.0,
                 since: Optional[datetime] = None,
                 until: Optional[datetime] = None,
                 max_articles: int = 20,
                 fetch_body: bool = True,
                 use_cnyes: bool = True,
                 official_url: str = "",
                 related_companies: Optional[Iterable[str]] = None,
                 priority_domains: Optional[Iterable[str]] = None,
                 priority_boost_days: Optional[dict] = None,
                 topic_boost_days: int = 120,
                 require_topic: bool = False,
                 other_quota_ratio: float = 0.25,
                 min_chars: int = 80,
                 progress: Optional[Callable[[str, float], None]] = None,
                 delay: float = 0.8) -> tuple[List[Article], List[str]]:
    """蒐集某公司近 N 年新聞。回傳 (成功清單, 使用的關鍵字)。

    **不限來源**：任何 Google News 收錄的媒體都會蒐集。依「新聞來源彙整.xlsx」的
    實際使用統計，經濟日報、工商時報、鉅亨網、公司官網為第一優先——除全網檢索外
    另做定向檢索，並在排序時享有 180 天的新鮮度加權（第二優先 60 天）。這是加權
    不是門檻，其他媒體只要夠新、夠相關一樣會排進來。

    **對象**：受訪公司之外，`related_companies` 可填母公司（投資人）與相關企業／
    轉投資公司，各自展開關鍵字檢索，結果會標明屬於哪一家（`Article.entity`）。

    **議題**：對每一家對象另做兩組主題檢索（關稅、地緣政治、科技戰、出口管制、
    國安、資安／供應鏈、設廠、國產化、AI、機器人、量子、減碳、數位轉型、電動車），
    議題同樣是**加權不是門檻**：命中議題的新聞每項扣 `topic_boost_days` 天（最多
    算兩項）往前排，未命中議題的新聞照樣蒐集，並由 `other_quota_ratio` 保留一定
    比例的名額（預設 25%），避免議題加權把一般新聞整批擠掉。只有把
    `require_topic` 設為 True 才會真的過濾掉非議題新聞。
    """
    until = until or datetime.now()
    since = since or (until - timedelta(days=int(365.25 * years)))
    keywords = make_keywords(company, extra_keywords)
    doms = list(priority_domains) if priority_domains is not None else list(PRIORITY_DOMAINS)
    boost = priority_boost_days or TIER_BOOST_DAYS
    f = Fetcher(delay=delay)

    def say(msg: str, pct: float) -> None:
        if progress:
            progress(msg, pct)

    found: List[Article] = []

    # 檢索對象：受訪公司 + 母公司（投資人）+ 相關企業／轉投資
    entities: List[tuple] = [(company, keywords)]
    for rc in (related_companies or []):
        rc = (rc or "").strip()
        if rc:
            entities.append((rc, make_keywords(rc)))

    say("以關鍵字檢索 Google News：%s" % "、".join(keywords), 0.03)
    try:
        found += _tag(search_google_news(keywords, since, until, f), company)
    except Exception as e:      # noqa: BLE001
        say("Google News 檢索失敗：%s" % e, 0.04)

    # 母公司與相關企業
    for n, (name, kws) in enumerate(entities[1:]):
        say("檢索關聯企業：%s" % name, 0.05 + 0.01 * n)
        try:
            found += _tag(search_google_news(kws, since, until, f), name)
        except Exception:      # noqa: BLE001
            pass

    # 議題定向檢索：每一家對象 × 兩組主題詞
    for n, (name, kws) in enumerate(entities):
        core = kws[1] if len(kws) > 1 else name
        for gi, group in enumerate(TOPIC_QUERY_GROUPS):
            say("檢索 %s 的關注議題（%d/%d）" % (name, gi + 1, len(TOPIC_QUERY_GROUPS)),
                0.07 + 0.01 * (n * len(TOPIC_QUERY_GROUPS) + gi))
            try:
                found += _tag(search_google_news(
                    [core], since, until, f, topics=group), name)
            except Exception:      # noqa: BLE001
                pass

    # 第一優先來源定向補搜
    for n, dom in enumerate(doms):
        name = DOMAIN_SOURCE.get(dom, dom)
        say("定向檢索 %s…" % name, 0.12 + 0.01 * n)
        try:
            found += _tag(search_google_news(keywords, since, until, f, site=dom),
                          company)
        except Exception:      # noqa: BLE001
            pass

    if use_cnyes:
        say("檢索鉅亨網…", 0.14)
        for kw in keywords[:2]:
            try:
                found += _tag(search_cnyes(kw, since, until, f), company)
            except Exception:      # noqa: BLE001
                pass

    short_name = keywords[1] if len(keywords) > 1 else company
    official_label = "%s官網" % short_name if short_name else "公司官網"

    official_hosts: List[str] = []
    if official_url:
        say("檢索公司官網…", 0.18)
        official_hosts.append(urllib.parse.urlparse(
            official_url if official_url.startswith("http")
            else "https://" + official_url).netloc)
        got = []
        try:
            got = _tag(search_official_site(official_url, short_name,
                                            since, until, f), company)
        except Exception:      # noqa: BLE001
            got = []
        if not got:
            # 官網若為 JS 動態渲染，靜態抓不到列表，改用 Google 定向檢索該網域
            try:
                got = _tag(search_google_news(keywords, since, until, f,
                                              site=official_hosts[0]), company)
                for g in got:
                    g.origin = "official"
                    g.source = official_label
            except Exception:      # noqa: BLE001
                got = []
        found += got

    # 所有對象的關鍵字合起來當作相關性判準
    all_keywords: List[str] = []
    for _, kws in entities:
        for k in kws:
            if k not in all_keywords:
                all_keywords.append(k)

    # 日期範圍與關鍵字命中過濾
    cand: List[Article] = []
    for a in found:
        if a.published:
            pdate = a.published.replace(tzinfo=None)
            if not (since <= pdate <= until):
                continue
        a.matched = match_keywords(a.title, all_keywords)
        a.topics = match_topics(a.title)
        cand.append(a)

    cand = _dedup(cand)
    # 排序：不限來源，但第一／第二優先來源享有新鮮度加權，因此同期新聞會排在前面
    cand.sort(key=lambda x: rank_score(x, until, official_hosts, boost,
                                       topic_boost_days))
    # 先保留名額給非議題新聞，再截斷候選池，否則截斷會把它們先砍掉
    cand = _reserve_slots(cand, max_articles,
                          int(max_articles * max(other_quota_ratio, 0.0)))
    cand = cand[: max(max_articles * 3, max_articles)]
    say("初步取得 %d 篇候選新聞" % len(cand), 0.22)

    if not fetch_body:
        return cand[:max_articles], keywords

    ok: List[Article] = []
    total = len(cand)
    for i, a in enumerate(cand, 1):
        if len(ok) >= max_articles:
            break
        say("解析第 %d/%d 篇：%s" % (i, total, a.title[:28]),
            0.22 + 0.73 * i / max(total, 1))
        if "news.google.com" in a.url:
            a.url = resolve_google_url(a.url, f)
        fetch_article_body(a, f)
        a.source = normalize_source(a.url, a.source)
        if a.origin == "official":
            a.source = official_label
        if a.char_count < min_chars:
            continue
        a.topics = match_topics(a.title + a.body)
        if a.origin != "official":
            a.matched = match_keywords(a.title + a.body, all_keywords)
            if not a.matched:
                continue
        if require_topic and not a.topics:
            continue
        ok.append(a)

    ok.sort(key=lambda x: x.published or datetime.min, reverse=True)
    say("完成，共 %d 篇可用新聞" % len(ok), 1.0)
    return ok, keywords
