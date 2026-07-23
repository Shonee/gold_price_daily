# -*- coding: utf-8 -*-
"""
每日金价页面生成脚本（单脚本）。

职责：
1. 多源抓取品牌金店零售价、现货/T+D/期货/铂金等行情（源解析失败自动轮换下一个）。
2. 维护 gold_data.json（不存在则自动初始化），按日期去重追加历史快照。
3. 把数据注入 html/template.html，生成可直接打开 / 发布到 GitHub Pages 的 index.html。

运行：python3 py/gold/generate_gold_page.py
仅依赖 requests。
"""

import json
import os
import re
from datetime import datetime

import requests

# =============================================================================
# 路径与常量
# =============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "..", "html"))
TEMPLATE_PATH = os.path.join(OUT_DIR, "template.html")
DATA_PATH = os.path.join(OUT_DIR, "gold_data.json")
INDEX_PATH = os.path.join(OUT_DIR, "index.html")
# 仓库根 README（展示金价数据）
README_PATH = os.path.normpath(os.path.join(BASE_DIR, "..", "..", "README.md"))

HISTORY_DAYS = 30  # 走势图历史保留天数

# 品牌展示顺序（需与数据源中的名称一致）
BRANDS = ["周大福", "六福珠宝", "菜百首饰", "金至尊", "老凤祥", "周生生", "老庙黄金", "中国黄金"]

# 品牌详情页（大水贝 cngoldprice.com）
BRAND_SLUG = {
    "周大福": "chow-tai-fook", "六福珠宝": "luk-fook", "菜百首饰": "cb",
    "金至尊": "3dgold", "老凤祥": "lao-feng-xiang", "周生生": "chow-sang-sang",
    "老庙黄金": "lao-miao-gold", "中国黄金": "china-gold",
}
BRAND_MORE_URL = "https://cngoldprice.com/"

# 上海黄金交易所行情详情/列表页（参考 shangjiaosuo.html）
SGE_LIST_URL = "https://www.cngold.org/img_date/shangjiaosuo.html"
# 水贝黄金详情页（大水贝 / 金投网移动端）
SHUIBEI_URL = "https://m.cngold.org/quote/gjs/swhj_shuibei.html"

# 各行情详情/更多跳转链接（参考金投网 cngold.org / 雅虎财经 / 大水贝）
LINKS = {
    "intl_gold": "https://hk.finance.yahoo.com/quote/GC%3DF/",
    "intl_silver": "https://hk.finance.yahoo.com/quote/SI%3DF/",
    "au_td": "https://www.cngold.org/gold_td/",
    "ag_td": "https://ag.cngold.org/bytd/",
    "au_9999": SGE_LIST_URL,
    "au_9995": SGE_LIST_URL,
    "platinum": SGE_LIST_URL,
    "au100g": SGE_LIST_URL,
    "au_qh": "https://www.cngold.org/qihuo/",
    "ag_qh": "https://ag.cngold.org/",
    "more_intl": "https://www.cngold.org/quote/",
    "more_sge": SGE_LIST_URL,
    "more_shfe": "https://www.cngold.org/img_date/gold_qh.html",
}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari/604.1"
)

OZ = 31.1035  # 1 金衡盎司 = 31.1035 克

# 东方财富实时行情图（<img> 直接引用，不受 CORS 限制）
EASTMONEY_CHARTS = [
    {"key": "intl_gold", "label": "国际金价", "nid": "122.XAU"},
    {"key": "intl_silver", "label": "国际银价", "nid": "122.XAG"},
    {"key": "sh_gold", "label": "上海金", "nid": "118.AU9999"},
    {"key": "sh_silver", "label": "上海银", "nid": "118.AG9999"},
    {"key": "comex_gold", "label": "COMEX黄金", "nid": "113.GC00Y"},
    {"key": "comex_silver", "label": "COMEX白银", "nid": "113.SI00Y"},
]


# =============================================================================
# 通用 HTTP
# =============================================================================
def http_get(url, timeout=12, extra_headers=None):
    headers = dict(DEFAULT_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def _f(arr, idx):
    """安全取数组下标并转 float，失败返回 None。"""
    try:
        v = float(arr[idx])
        return v
    except (ValueError, TypeError, IndexError):
        return None


# =============================================================================
# 品牌金店价数据源（有序，轮换）
# =============================================================================
def parse_brand_huangjinjiage():
    """金价网 各品牌黄金价格一览表。"""
    text = http_get("http://www.huangjinjiage.cn/pinpaijinjia.html")

    m = re.search(r"今日数据更新于：(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        update_time = f"{int(m.group(1))}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    else:
        update_time = datetime.now().strftime("%Y-%m-%d")

    rows = []
    for brand in BRANDS:
        retail = None
        for mm in re.finditer(re.escape(brand), text):
            ctx = re.sub(r"<[^>]+>", " ", text[mm.start(): mm.start() + 300])
            # 价格是紧跟“元/克”的数字（跳过 999 等纯度前缀、排除铂金低价）
            for num in re.findall(r"(\d{3,4}(?:\.\d+)?)\s*元/克", ctx):
                value = float(num)
                if 800 <= value <= 3000:  # 黄金零售价区间
                    retail = value
                    break
            if retail is not None:
                break
        slug = BRAND_SLUG.get(brand)
        rows.append({
            "brand": brand,
            "retail": retail,
            "buyback": None,  # 该源无稳定换购价，降级为 -
            "update_time": update_time,
            "url": f"https://cngoldprice.com/brand/{slug}/today-gold-price" if slug else BRAND_MORE_URL,
        })

    if sum(1 for r in rows if r["retail"]) < 2:
        return None
    return rows


BRAND_SOURCES = [
    {"name": "金价网(huangjinjiage)", "enabled": True, "parser": parse_brand_huangjinjiage},
]


def fetch_shuibei_price():
    """水贝黄金价：优先水贝首饰金报价，兵底 Au99.99 现货。返回 (price, tag)。"""
    try:
        text = http_get(SHUIBEI_URL, extra_headers={"User-Agent": MOBILE_UA})
        m = re.search(r"水贝首饰金报\s*(\d+(?:\.\d+)?)\s*元/克", text)
        if m:
            return float(m.group(1)), "水贝首饰金"
    except Exception as err:
        print(f"[水贝][警告] 页面解析失败({err})，兵底 Au99.99")
    try:
        text = http_get("https://hq.sinajs.cn/list=gds_AU9999",
                        extra_headers={"Referer": "https://finance.sina.com.cn"})
        arr = _parse_sina_block(text).get("gds_AU9999")
        v = _f(arr, 0) if arr else None
        if v:
            return v, "Au99.99基准"
    except Exception:
        pass
    return None, ""


def fetch_brands():
    rows = None
    for src in BRAND_SOURCES:
        if not src.get("enabled", True):
            continue
        try:
            rows = src["parser"]()
        except Exception as err:
            print(f"[品牌][失败] {src['name']}: {err}，轮换下一个")
            rows = None
            continue
        if rows:
            print(f"[品牌][成功] {src['name']}: 命中 {sum(1 for r in rows if r['retail'])}/{len(rows)} 个品牌")
            break
        print(f"[品牌][无效] {src['name']}: 无有效数据，轮换下一个")
    if not rows:
        print("[品牌][降级] 所有源失败，全部显示 -")
        now = datetime.now().strftime("%Y-%m-%d")
        rows = [{"brand": b, "retail": None, "buyback": None, "update_time": now,
                 "url": BRAND_MORE_URL} for b in BRANDS]
    # 追加水贝黄金（名称 + 价格简要信息）
    price, tag = fetch_shuibei_price()
    print(f"[水贝][{'成功' if price else '降级'}] 水贝黄金: {price} ({tag})")
    rows.append({
        "brand": "水贝黄金", "retail": price, "buyback": None,
        "update_time": datetime.now().strftime("%Y-%m-%d"), "url": SHUIBEI_URL,
    })
    return rows


# =============================================================================
# 行情分组（新浪，已验证）：国际 / 上海金交所 T+D / 上海期货 / 其他
# 统一 quote 结构：名称/最新/涨跌/涨跌幅/开盘/昨收/买/卖/最高/最低/换算/时间/链接
# =============================================================================
def _parse_sina_block(text):
    result = {}
    for line in text.strip().split("\n"):
        m = re.search(r'hq_str_(\w+)="(.*?)"', line)
        if m:
            result[m.group(1)] = m.group(2).split(",")
    return result


def _make_quote(name, unit, last, prev, open_, bid, ask, high, low, dt, url, convert=None):
    change = round(last - prev, 2) if (last is not None and prev) else None
    pct = round((last - prev) / prev * 100, 2) if (last is not None and prev) else None
    return {
        "name": name, "unit": unit,
        "last": last, "change": change, "change_pct": pct,
        "open": open_, "prev_close": prev, "bid": bid, "ask": ask,
        "high": high, "low": low, "convert_cny": convert,
        "update_time": dt, "url": url,
        "up": (change is None or change >= 0),
    }


def _quote_gds_hf(name, unit, arr, url, rate=None):
    """gds_ / hf_ 通用：last=0 bid=2 ask=3 high=4 low=5 time=6 prevclose=7 open=8 date=12。"""
    last = _f(arr, 0)
    dt = f"{arr[12]} {arr[6]}" if len(arr) > 12 else ""
    convert = round(last * rate / OZ, 2) if (rate and last) else None
    return _make_quote(name, unit, last, _f(arr, 7), _f(arr, 8),
                       _f(arr, 2), _f(arr, 3), _f(arr, 4), _f(arr, 5), dt, url, convert)


def _quote_nf(name, unit, arr, url):
    """nf_ 期货：last=8 open=2 high=3 low=4 bid=6 ask=7 prevsettle=10 time=1(HHMMSS) date=17。"""
    t = arr[1] if len(arr) > 1 else ""
    tt = f"{t[:2]}:{t[2:4]}:{t[4:6]}" if len(t) >= 6 else t
    dt = f"{arr[17]} {tt}" if len(arr) > 17 else ""
    return _make_quote(name, unit, _f(arr, 8), _f(arr, 10), _f(arr, 2),
                       _f(arr, 6), _f(arr, 7), _f(arr, 3), _f(arr, 4), dt, url)


def parse_quote_groups_sina():
    codes = ("hf_XAU,hf_XAG,gds_AUTD,gds_AGTD,nf_AU0,nf_AG0,"
             "gds_AU9999,gds_AU9995,gds_PT9995,gds_AU100G,fx_susdcny")
    text = http_get(f"https://hq.sinajs.cn/list={codes}",
                    extra_headers={"Referer": "https://finance.sina.com.cn"})
    d = _parse_sina_block(text)
    if "hf_XAU" not in d and "gds_AUTD" not in d:
        return None

    rate = _f(d.get("fx_susdcny", []), 1)  # 美元兑人民币

    def q_gds_hf(code, name, unit, url, use_rate=False):
        arr = d.get(code)
        if not arr:
            return None
        return _quote_gds_hf(name, unit, arr, url, rate if use_rate else None)

    def q_nf(code, name, unit, url):
        arr = d.get(code)
        if not arr:
            return None
        return _quote_nf(name, unit, arr, url)

    groups = [
        {
            "title": "国际金价", "more_url": LINKS["more_intl"],
            "items": [
                q_gds_hf("hf_XAU", "现货黄金", "美元/盎司", LINKS["intl_gold"], use_rate=True),
                q_gds_hf("hf_XAG", "现货白银", "美元/盎司", LINKS["intl_silver"], use_rate=True),
            ],
        },
        {
            "title": "上海黄金交易所行情", "more_url": LINKS["more_sge"],
            "items": [
                q_gds_hf("gds_AUTD", "黄金T+D", "元/克", LINKS["au_td"]),
                q_gds_hf("gds_AGTD", "白银T+D", "元/千克", LINKS["ag_td"]),
                q_gds_hf("gds_AU9999", "黄金9999", "元/克", LINKS["au_9999"]),
                q_gds_hf("gds_AU9995", "黄金9995", "元/克", LINKS["au_9995"]),
                q_gds_hf("gds_PT9995", "铂金Pt99.95", "元/克", LINKS["platinum"]),
                q_gds_hf("gds_AU100G", "黄金Au100g", "元/克", LINKS["au100g"]),
            ],
        },
        {
            "title": "上海期货交易所", "more_url": LINKS["more_shfe"],
            "items": [
                q_nf("nf_AU0", "黄金期货", "元/克", LINKS["au_qh"]),
                q_nf("nf_AG0", "白银期货", "元/千克", LINKS["ag_qh"]),
            ],
        },
    ]
    # 清理组内 None 项
    for g in groups:
        g["items"] = [it for it in g["items"] if it]
    groups = [g for g in groups if g["items"]]
    return groups or None


QUOTE_SOURCES = [
    {"name": "新浪财经(Sina)", "enabled": True, "parser": parse_quote_groups_sina},
]


def fetch_quote_groups():
    for src in QUOTE_SOURCES:
        if not src.get("enabled", True):
            continue
        try:
            groups = src["parser"]()
        except Exception as err:
            print(f"[行情][失败] {src['name']}: {err}，轮换下一个")
            continue
        if groups:
            total = sum(len(g["items"]) for g in groups)
            print(f"[行情][成功] {src['name']}: {len(groups)} 组 / {total} 条")
            return groups
        print(f"[行情][无效] {src['name']}: 无有效数据，轮换下一个")
    print("[行情][降级] 所有源失败，行情分组为空")
    return []


# =============================================================================
# 历史与走势
# =============================================================================
def load_store():
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                store = json.load(f)
            if isinstance(store, dict) and "snapshots" in store:
                return store
        except Exception as err:
            print(f"[历史][警告] 读取 {DATA_PATH} 失败({err})，重新初始化")
    return {"snapshots": []}


def update_history(store, brand_rows, quote_groups):
    today = datetime.now().strftime("%Y-%m-%d")
    spot = {}
    for g in quote_groups:
        for it in g["items"]:
            if it.get("last") is not None:
                spot[it["name"]] = it["last"]
    snapshot = {
        "date": today,
        "brands": {r["brand"]: r["retail"] for r in brand_rows if r["retail"]},
        "spot": spot,
    }
    snaps = [s for s in store.get("snapshots", []) if s.get("date") != today]
    snaps.append(snapshot)
    store["snapshots"] = snaps[-HISTORY_DAYS:]
    return store


def build_trend(store):
    snaps = store.get("snapshots", [])
    dates = [s["date"] for s in snaps]
    series = []
    for brand in BRANDS:
        data = [s.get("brands", {}).get(brand) for s in snaps]
        if any(v is not None for v in data):
            series.append({"name": brand, "data": data})
    return {"dates": dates, "series": series}


# =============================================================================
# 组装与渲染
# =============================================================================
def build_chart_urls():
    charts = []
    base = "https://webquotepic.eastmoney.com/GetPic.aspx"
    for c in EASTMONEY_CHARTS:
        charts.append({
            "key": c["key"], "label": c["label"],
            "intraday": f"{base}?nid={c['nid']}&imageType=r&type=&unitWidth=-6&ef=&formatType=&AT=1",
            "kline": f"{base}?nid={c['nid']}&imageType=KXL&type=&unitWidth=-6&AT=1",
        })
    return charts


def render(gold_data):
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()
    payload = json.dumps(gold_data, ensure_ascii=False)
    html = template.replace("/*__GOLD_DATA__*/", payload)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html)


def _md(v):
    return v if v is not None else "-"


def build_readme(gold_data):
    """生成仓库根 README.md，用 Markdown 表格/图片展示当前金价数据。"""
    date = gold_data["date"]
    L = []
    L.append("# 🥇 今日金价 Daily Gold Price")
    L.append("")
    L.append(f"> 数据更新时间：**{gold_data['generated_at']}** （由 GitHub Action 定时自动生成）")
    L.append("")
    L.append("数据来源：新浪财经 · 金价网 · 金投网 · 大水贝 · 雅虎财经 · 东方财富（仅供参考，不构成投资建议）")
    L.append("")

    # 品牌金店金价
    L.append("## 品牌金店金价（元/克）")
    L.append("")
    L.append("| 金店名称 | 黄金零售价 | 更新时间 |")
    L.append("| :-- | :--: | :--: |")
    for r in gold_data["brands"]["items"]:
        name = f"[{r['brand']}]({r['url']})" if r.get("url") else r["brand"]
        L.append(f"| {name} | {_md(r.get('retail'))} | {_md(r.get('update_time'))} |")
    L.append("")

    # 行情分组
    for g in gold_data["quote_groups"]:
        more = f"（[更多]({g['more_url']})）" if g.get("more_url") else ""
        L.append(f"## {g['title']}{more}")
        L.append("")
        L.append("| 名称 | 最新价 | 涨跌 | 开盘 | 昨收 | 买价 | 卖价 | 最高 | 最低 | 报价时间 |")
        L.append("| :-- | :--: | :--: | :--: | :--: | :--: | :--: | :--: | :--: | :--: |")
        for it in g["items"]:
            name = f"[{it['name']}]({it['url']})" if it.get("url") else it["name"]
            if it.get("unit"):
                name += f" <sub>{it['unit']}</sub>"
            last = _md(it.get("last"))
            if it.get("convert_cny") is not None:
                last = f"{last}<br><sub>≈{it['convert_cny']}元/克</sub>"
            chg = it.get("change")
            if chg is None:
                chg_s = "-"
            else:
                arrow = "🔺" if it.get("up") else "🔻"
                pct = f" ({it['change_pct']}%)" if it.get("change_pct") is not None else ""
                chg_s = f"{chg} {arrow}{pct}"
            L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                name, last, chg_s, _md(it.get("open")), _md(it.get("prev_close")),
                _md(it.get("bid")), _md(it.get("ask")), _md(it.get("high")),
                _md(it.get("low")), _md(it.get("update_time"))))
        L.append("")

    # 国际实时行情图（分时），按日附加参数避开 GitHub 图片缓存
    L.append("## 国际实时行情图")
    L.append("")
    for c in gold_data["charts"]:
        img = c["intraday"] + "&_d=" + date
        L.append(f"**{c['label']}**")
        L.append("")
        L.append(f"![{c['label']}]({img})")
        L.append("")

    L.append("---")
    L.append("")
    L.append("*本文档由脚本 `py/gold/generate_gold_page.py` 自动生成，完整页面见 GitHub Pages。*")

    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    brand_rows = fetch_brands()
    quote_groups = fetch_quote_groups()

    store = load_store()
    store = update_history(store, brand_rows, quote_groups)

    gold_data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "brands": {"items": brand_rows, "more_url": BRAND_MORE_URL},
        "quote_groups": quote_groups,
        "trend": build_trend(store),
        "charts": build_chart_urls(),
    }

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)

    render(gold_data)
    build_readme(gold_data)
    print(f"[完成] 已生成 {INDEX_PATH}")
    print(f"[完成] 已生成 {README_PATH}")
    print(f"[完成] 历史快照 {len(store['snapshots'])} 天，数据文件 {DATA_PATH}")


if __name__ == "__main__":
    main()
