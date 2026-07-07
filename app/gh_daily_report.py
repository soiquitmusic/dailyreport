#!/usr/bin/env python3
import json, os, sys, time
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen

TZ_BJ = timezone(timedelta(hours=8))
FEISHU_URL = os.environ.get("FEISHU_URL", "").strip()
DS_KEY = (os.environ.get("DEEPSEEK_API_KEY", "") or "").strip()
DS_MODEL = "deepseek-chat"

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def push(body):
    if not FEISHU_URL: return log("无 FEISHU_URL")
    try:
        payload = json.dumps({"msg_type": "text", "content": {"text": body}}, ensure_ascii=False).encode('utf-8')
        r = json.loads(urlopen(Request(FEISHU_URL, data=payload, headers={"Content-Type":"application/json"}, method="POST"), timeout=15).read())
        log(f"飞书: code={r.get('code')}")
    except Exception as e:
        log(f"飞书失败: {e}")

def load_prompts():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"prompts.json失败: {e}")
        return None

import yfinance as yf

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache.json")

def load_cache():
    try:
        with open(CACHE_FILE, "r") as f:
            c = json.load(f)
            if c.get("ts", 0) > time.time() - 86400:  # 24h内有效
                return c.get("data", {})
    except: pass
    return {}

def save_cache(data):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"ts": time.time(), "data": data}, f)
    except: pass

def yf_web(ticker):
    """用 Yahoo Finance v8 API 直接查询（备用源）"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        d = json.loads(urlopen(req, timeout=10).read())
        closes = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if len(closes) < 2: return None
        c2, c1 = float(closes[-2]), float(closes[-1])
        return {"price": round(c1,2), "change": round(c1-c2,2), "change_pct": round((c1-c2)/c2*100,2)}
    except: return None

def get_market_data(tickers):
    """三级获取：yfinance -> 缓存 -> web直查"""
    cache = load_cache()
    result = {}

    # 先从缓存加载
    from_cache = []
    for t in tickers:
        if t in cache:
            result[t] = cache[t]
            from_cache.append(t)

    # yfinance 获取未缓存的
    need = [t for t in tickers if t not in result]
    if need:
        for i in range(0, len(need), 3):
            batch = need[i:i+3]
            try:
                data = yf.download(batch, period="5d", progress=False, auto_adjust=True)
                for t in batch:
                    try:
                        h = data[t] if len(batch) > 1 and t in data.columns.levels[0] else data
                        if len(h) < 2: continue
                        c2, c1 = float(h["Close"].iloc[-2]), float(h["Close"].iloc[-1])
                        result[t] = {"price": round(c1,2), "change": round(c1-c2,2), "change_pct": round((c1-c2)/c2*100,2)}
                    except: continue
            except: pass
            time.sleep(3)

    # web 直查兜底
    still_need = [t for t in tickers if t not in result]
    for t in still_need:
        d = yf_web(t)
        if d:
            result[t] = d
            time.sleep(0.5)

    # 所有都失败了就用缓存
    if not result:
        result = cache
        log("全部数据源失败，使用缓存")

    save_cache(result)
    return result

FOREIGN_FEEDS = [
    ("CNBC","https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("Reuters","https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best&best-sectors=markets"),
    ("Bloomberg","https://feeds.bloomberg.com/markets/news.rss"),
]

DOMESTIC_FEEDS = [("36氪","https://36kr.com/feed"), ("新浪财经","https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=10")]

BRIEFING_FEEDS = [
    # 美国
    ("CNBC","https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("WSJ","https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    # 亚洲
    ("日经","https://asia.nikkei.com/rss/feed/nar"),
    ("东财","https://push2.eastmoney.com/api/rpt/list?reportName=RPT_NEWS_CATEGORY&type=1"),
    # 欧洲
    ("FT","https://www.ft.com/rss/markets"),
    # 全球
    ("Reuters","https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best&best-sectors=markets"),
]

import html, xml.etree.ElementTree as ET

def fetch_rss(feeds, n=5):
    h = []
    for l, u in feeds:
        try:
            req = Request(u, headers={"User-Agent":"Mozilla/5.0"})
            tree = ET.fromstring(urlopen(req, timeout=10).read())
            for item in (tree.findall(".//item") or tree.findall(".//entry")):
                t = item.find("title")
                if t is not None and t.text:
                    tx = html.unescape(t.text.strip())
                    if tx and len(tx) > 8 and tx not in [x[1] for x in h]:
                        h.append((l, tx))
                    if len(h) >= n: return h
        except: continue
    return h

def load_recent_logs():
    """读取最近3天的早报/晚报/简报log，为持仓分析提供上下文"""
    recent = []
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reports")
    for folder in ["am", "pm", "briefing"]:
        folder_path = os.path.join(log_dir, folder)
        if not os.path.isdir(folder_path): continue
        files = sorted([f for f in os.listdir(folder_path) if f.endswith('.md')], reverse=True)[:3]
        for f in files:
            try:
                with open(os.path.join(folder_path, f), "r", encoding="utf-8") as fp:
                    content = fp.read()[:1500]  # 每篇取前1500字
                    if len(content) > 100:
                        recent.append(content)
            except: pass
    log(f"加载了 {len(recent)} 篇最近日志")
    return "\n\n---\n\n".join(recent) if recent else "(暂无最近报告)"

def analyze_portfolio():
    # 优先从文件读取（避免环境变量传大JSON被截断）
    portfolio_file = os.environ.get("PORTFOLIO_FILE","")
    if portfolio_file:
        try:
            with open(portfolio_file, "r", encoding="utf-8") as f:
                raw = f.read()
            log(f"从文件读取持仓: {portfolio_file}")
        except Exception as e:
            log(f"读取PORTFOLIO_FILE失败: {e}")
            return None
    else:
        raw = os.environ.get("PORTFOLIO","")
        if not raw:
            log("⚠️ PORTFOLIO环境变量为空")
            return None
    log(f"PORTFOLIO数据长度: {len(raw)}")
    try:
        d = json.loads(raw)
        p = d if isinstance(d, list) else d.get("holdings", d)
        log(f"持仓数量: {len(p)}")
    except Exception as e:
        log(f"PORTFOLIO JSON解析失败: {e}")
        return None
    ind = get_market_data(["^GSPC","^IXIC","^SOX"])
    st = get_market_data(["NVDA","MU","AMD","AVGO","TSM"])
    cm = get_market_data(["CL=F","GC=F"])
    recent_logs = load_recent_logs()
    prompts = load_prompts()
    if not prompts:
        log("prompts加载失败")
        return None
    log("prompts加载成功")
    now = datetime.now(TZ_BJ)
    prompt = prompts["portfolio"]["prompt"].format(
        framework=prompts.get("framework",""),
        date=now.strftime("%Y-%m-%d %H:%M"),
        market_data=json.dumps({"indices":ind,"stocks":st,"commodities":cm}, ensure_ascii=False, indent=2),
        portfolio=json.dumps(p, ensure_ascii=False, indent=2),
        recent_reports=recent_logs)
    try:
        req = Request("https://api.deepseek.com/chat/completions",
            data=json.dumps({"model":DS_MODEL,"messages":[
                {"role":"system","content":prompts["portfolio"]["system"]},
                {"role":"user","content":prompt}
            ],"max_tokens":3000,"temperature":0.3}).encode(),
            headers={"Content-Type":"application/json","Authorization":f"Bearer {DS_KEY}"}, method="POST")
        text = json.loads(urlopen(req, timeout=90).read())["choices"][0]["message"]["content"].strip()
        log(f"持仓分析 ({len(text)}字)")
        return text
    except Exception as e:
        log(f"LLM失败: {e}")
        return None

def llm_analyze(ctx, key="daily_am"):
    if not DS_KEY: return None
    prompts = load_prompts()
    if not prompts: return None
    prompt = prompts[key]["prompt"].format(date=ctx["date"], data=json.dumps(ctx.get("data",{}), ensure_ascii=False, indent=2))
    try:
        req = Request("https://api.deepseek.com/chat/completions",
            data=json.dumps({"model":DS_MODEL,"messages":[
                {"role":"system","content":prompts[key]["system"]},
                {"role":"user","content":prompt}
            ],"max_tokens":3000,"temperature":0.3}).encode(),
            headers={"Content-Type":"application/json","Authorization":f"Bearer {DS_KEY}"}, method="POST")
        text = json.loads(urlopen(req, timeout=60).read())["choices"][0]["message"]["content"].strip()
        log(f"LLM分析 ({len(text)}字)")
        return text
    except Exception as e:
        log(f"LLM失败: {e}")
        return None

def build_data():
    now = datetime.now(TZ_BJ)
    wd = ["周一","周二","周三","周四","周五","周六","周日"][now.weekday()]
    ctx = {"date": f"{now.year}/{now.month}/{now.day}（{wd}）", "data":{}}
    idx = {"^GSPC":"S&P500","^IXIC":"纳斯达克","^DJI":"道指","^SOX":"SOX半导体"}
    r = get_market_data(list(idx.keys()))
    ctx["data"]["indices"] = {idx.get(k,k):v for k,v in r.items()}
    ctx["data"]["stocks"] = get_market_data(["NVDA","MU","AMD","AVGO","TSM","MSFT","GOOGL","AMZN","META"])
    cm = {"CL=F":"WTI原油","BZ=F":"Brent原油","GC=F":"黄金"}
    ctx["data"]["commodities"] = {cm.get(k,k):v for k,v in get_market_data(list(cm.keys())).items()}
    ctx["data"]["headlines"] = [f"[{l}] {t}" for l,t in fetch_rss(FOREIGN_FEEDS, 4)]
    ctx["data"]["domestic"] = [f"[{l}] {t}" for l,t in fetch_rss(DOMESTIC_FEEDS, 2)]

    # 摩根士丹利研究（Google News RSS）
    try:
        ms_url = "https://news.google.com/rss/search?q=Morgan+Stanley+research+stock+market+2026&hl=en-US"
        req = Request(ms_url, headers={"User-Agent":"Mozilla/5.0"})
        tree = ET.fromstring(urlopen(req, timeout=10).read())
        ms_items = []
        for item in (tree.findall(".//item") or [])[:5]:
            t = item.find("title")
            if t is not None and t.text:
                ms_items.append(html.unescape(t.text.strip()))
        ctx["data"]["morgan_stanley"] = ms_items
    except: pass

    return ctx

def build_asia_data():
    """晚报用：A股+亚洲市场数据（东方财富免费API + yfinance兜底）"""
    now = datetime.now(TZ_BJ)
    wd = ["周一","周二","周三","周四","周五","周六","周日"][now.weekday()]
    ctx = {"date": f"{now.year}/{now.month}/{now.day}（{wd}）", "data":{}}

    # 东方财富API：上证/深证/创业板/科创50/恒生/恒生科技
    em_secids = "1.000001,0.399001,0.399006,1.000688,100.HSI,100.HSTECH"
    try:
        em_url = f"https://push2.eastmoney.com/api/qt/ulist.np/get?fields=f2,f3,f4,f12,f14&secids={em_secids}"
        em_data = json.loads(urlopen(Request(em_url, headers={"User-Agent":"Mozilla/5.0"}), timeout=10).read())
        em_map = {"1.000001":"上证指数","0.399001":"深证成指","0.399006":"创业板指","1.000688":"科创50","100.HSI":"恒生指数","100.HSTECH":"恒生科技"}
        for item in em_data.get("data",{}).get("diff",[]):
            name = em_map.get(item.get("f12",""), item.get("f14",""))
            ctx["data"][name] = {"price": item.get("f2"), "change_pct": item.get("f3"), "change": item.get("f4")}
    except Exception as e:
        log(f"东方财富API:{e}")

    # 北向/南向资金
    try:
        nb_url = "https://push2.eastmoney.com/api/qt/kamt.kline/get?fields=f1,f2,f3,f4&klt=101"
        nb_data = json.loads(urlopen(Request(nb_url, headers={"User-Agent":"Mozilla/5.0"}), timeout=10).read())
        ctx["data"]["北向资金净流入"] = nb_data.get("data",{}).get("s2n",{}).get("f1","--")
        ctx["data"]["南向资金净流入"] = nb_data.get("data",{}).get("n2s",{}).get("f1","--")
    except: pass

    # A股AI/半导体龙头股（东方财富）
    leaders_ids = "0.300308,1.688256,1.688981,0.002371,1.688041,0.603501,0.603986,1.688012,0.002049,0.000977"
    try:
        ld_url = f"https://push2.eastmoney.com/api/qt/ulist.np/get?fields=f2,f3,f4,f12,f14&secids={leaders_ids}"
        ld_data = json.loads(urlopen(Request(ld_url, headers={"User-Agent":"Mozilla/5.0"}), timeout=10).read())
        ld_map = {"300308":"中际旭创","688256":"寒武纪","688981":"中芯国际","002371":"北方华创","688041":"海光信息","603501":"韦尔股份","603986":"兆易创新","688012":"中微公司","002049":"紫光国微","000977":"浪潮信息"}
        ctx["data"]["A股龙头"] = {}
        for item in ld_data.get("data",{}).get("diff",[]):
            name = ld_map.get(item.get("f12",""), item.get("f14",""))
            ctx["data"]["A股龙头"][name] = {"price": item.get("f2"), "change_pct": item.get("f3")}
    except: pass

    # A股AI/半导体板块热度（东方财富板块）
    try:
        pl_url = "https://push2.eastmoney.com/api/qt/clist/get?fields=f2,f3,f4,f12,f14&pn=1&pz=10&po=1&np=1&fltt=2&invt=2&fs=m:90+t3!bk:BK0988,BK0477,BK0478,BK0483"
        pl_data = json.loads(urlopen(Request(pl_url, headers={"User-Agent":"Mozilla/5.0"}), timeout=10).read())
        ctx["data"]["A股板块"] = [{"name":i.get("f14"),"change_pct":i.get("f3")} for i in pl_data.get("data",{}).get("diff",[])]
    except: pass

    # 日经/韩国：yfinance兜底
    ctx["data"]["asia_indices"] = get_market_data(["^N225", "^KS11"])

    # 美股期货
    ctx["data"]["us_futures"] = get_market_data(["ES=F", "NQ=F"])

    # 大宗商品
    cm = {"CL=F":"WTI原油","BZ=F":"Brent原油","GC=F":"黄金"}
    ctx["data"]["commodities"] = {cm.get(k,k):v for k,v in get_market_data(list(cm.keys())).items()}

    # 国内新闻
    ctx["data"]["domestic_news"] = [f"[{l}] {t}" for l,t in fetch_rss(DOMESTIC_FEEDS, 5)]
    ctx["data"]["foreign_news"] = [f"[{l}] {t}" for l,t in fetch_rss(FOREIGN_FEEDS, 2)]

    return ctx

def build_briefing_data():
    """9点报：纯新闻聚合，全球多区域"""
    now = datetime.now(TZ_BJ)
    wd = ["周一","周二","周三","周四","周五","周六","周日"][now.weekday()]
    ctx = {"date": f"{now.year}/{now.month}/{now.day}（{wd}）", "data":{}}
    ctx["data"]["headlines"] = [f"[{l}] {t}" for l,t in fetch_rss(BRIEFING_FEEDS, 15)]
    return ctx

def build_ms_data():
    """大摩周报：Morgan Stanley 研报+新闻深度聚合"""
    now = datetime.now(TZ_BJ)
    wd = ["周一","周二","周三","周四","周五","周六","周日"][now.weekday()]
    ctx = {"date": f"{now.year}/{now.month}/{now.day}（{wd}）", "data":{}}

    # MS专属RSS+Google News搜索
    ms_feeds = [
        ("MS Research", "https://news.google.com/rss/search?q=Morgan+Stanley+research+stock+AI+semiconductor+2026&hl=en-US"),
        ("MS Upgrade", "https://news.google.com/rss/search?q=Morgan+Stanley+upgrade+downgrade+target+price&hl=en-US"),
        ("MS Tech", "https://news.google.com/rss/search?q=Morgan+Stanley+technology+AI+chip+forecast&hl=en-US"),
        ("MS Macro", "https://news.google.com/rss/search?q=Morgan+Stanley+outlook+strategy+Fed+2026&hl=en-US"),
        ("MS China", "https://news.google.com/rss/search?q=Morgan+Stanley+China+A-share+Asia+strategy&hl=en-US"),
    ]
    all_ms = {}
    for label, url in ms_feeds:
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            tree = ET.fromstring(urlopen(req, timeout=10).read())
            items = []
            for item in (tree.findall(".//item") or [])[:4]:
                t = item.find("title")
                if t is not None and t.text:
                    items.append(html.unescape(t.text.strip()))
            all_ms[label] = items
        except: pass
    ctx["data"]["ms_feeds"] = all_ms

    # 近期市场报告引用（从reports目录加载最近的早报/晚报做参考）
    try:
        reports = []
        for folder in ["am", "pm", "briefing"]:
            rdir = f"reports/{folder}"
            if os.path.exists(rdir):
                files = sorted(os.listdir(rdir))[-3:]
                for fname in files:
                    with open(f"{rdir}/{fname}", "r", encoding="utf-8") as f:
                        content = f.read()[:1500]
                        reports.append(content)
        ctx["data"]["recent_reports"] = reports
    except: pass

    # 市场数据兜底
    ctx["data"]["indices"] = get_market_data(["^GSPC", "^IXIC", "^SOX"])
    ctx["data"]["stocks"] = get_market_data(["NVDA", "MU", "AMD", "AVGO", "TSM", "MSFT", "META"])
    return ctx

def main():
    now = datetime.now(TZ_BJ)
    mode = os.environ.get("REPORT_MODE","am")
    if mode == "briefing":
        ctx = build_briefing_data()
        report = llm_analyze(ctx, "daily_briefing")
        prefix = "9点简报-全球速览"
        folder = "briefing"
    elif mode == "weekly_ms":
        ctx = build_ms_data()
        report = llm_analyze(ctx, "weekly_ms")
        prefix = "大摩周报"
        folder = "weekly_ms"
    elif mode == "portfolio":
        report = analyze_portfolio()
        prefix = "周报-持仓分析"
        folder = "portfolio"
    elif mode == "pm":
        ctx = build_asia_data()
        report = llm_analyze(ctx, "daily_pm")
        prefix = "晚报-A股/亚洲"
        folder = "pm"
    else:
        ctx = build_data()
        report = llm_analyze(ctx, "daily_am")
        prefix = "早报-美股/全球"
        folder = "am"
    if not report:
        report = f"{prefix} - 分析暂不可用"
    date_line = now.strftime('%Y%m%d')
    header = f"{prefix} | {now.strftime('%Y/%m/%d %H:%M')}"
    full_msg = f"{header}\n\n{report}"
    push(full_msg)

    if folder != "portfolio":
        os.makedirs(f"reports/{folder}", exist_ok=True)
        fname = f"reports/{folder}/{date_line}_{now.strftime('%H%M')}.md"
        try:
            with open(fname, "w", encoding="utf-8") as f:
                f.write(full_msg)
            log(f"日志: {fname}")
        except Exception as e:
            log(f"日志保存失败: {e}")

if __name__ == "__main__":
    main()
