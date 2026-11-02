"""Versioned configuration for the US → Taiwan industry research radar."""

ALGORITHM_VERSION = "us-rotation-v1"
SOURCE_NAME = "Yahoo Finance chart endpoint (unofficial; redistribution terms require review)"
BENCHMARK = "SPY"

# Eleven Select Sector SPDR sector proxies plus a separately labelled semiconductor overlay.
# XSD overlaps XLK and is intentionally excluded from the equal-peer sector ranking.
INDUSTRIES = [
    {"id": "technology", "name": "資訊科技", "ticker": "XLK", "gics": "Information Technology"},
    {"id": "semiconductors", "name": "半導體", "ticker": "XSD", "gics": "IT 子產業觀察", "overlay": True},
    {"id": "financials", "name": "金融", "ticker": "XLF", "gics": "Financials"},
    {"id": "healthcare", "name": "醫療保健", "ticker": "XLV", "gics": "Health Care"},
    {"id": "industrials", "name": "工業", "ticker": "XLI", "gics": "Industrials"},
    {"id": "consumer_discretionary", "name": "非必需消費", "ticker": "XLY", "gics": "Consumer Discretionary"},
    {"id": "consumer_staples", "name": "必需消費", "ticker": "XLP", "gics": "Consumer Staples"},
    {"id": "energy", "name": "能源", "ticker": "XLE", "gics": "Energy"},
    {"id": "utilities", "name": "公用事業", "ticker": "XLU", "gics": "Utilities"},
    {"id": "materials", "name": "原物料", "ticker": "XLB", "gics": "Materials"},
    {"id": "real_estate", "name": "房地產", "ticker": "XLRE", "gics": "Real Estate"},
    {"id": "communication_services", "name": "通訊服務", "ticker": "XLC", "gics": "Communication Services"},
]

WEIGHTS = {"relative_5d": 0.25, "relative_20d": 0.30, "relative_60d": 0.20,
           "relative_volume": 0.15, "breadth": 0.10}

CHAIN_CATEGORIES = [
    "AI 伺服器", "散熱", "PCB", "CCL", "半導體", "記憶體", "電源供應",
    "重電及電網", "被動元件", "生技醫療", "工業自動化", "消費電子",
]

# Seed records are deliberately limited to product relationships supported by company sources.
# A product-category match does not prove a named US customer/supplier relationship.
SEED_SUPPLY_CHAIN = [
    {"map_key": "it-ai-server-2382", "us_industry": "technology", "chain": "AI 伺服器",
     "code": "2382", "name": "廣達", "product": "AI 伺服器、雲端基礎設施產品",
     "role": "系統設計與製造", "reason": "公司公開資料列有 AI 伺服器及雲端基礎設施產品。",
     "evidence_url": "https://www.quantatw.com/Quanta/english/esg/ESG2024/ch01.html",
     "evidence_date": "2024", "confidence": "產品關聯已驗證；特定客戶關係未確認"},
    {"map_key": "it-ai-server-6669", "us_industry": "technology", "chain": "AI 伺服器",
     "code": "6669", "name": "緯穎", "product": "資料中心伺服器、AI／雲端運算平台",
     "role": "雲端資料中心伺服器設計與製造", "reason": "公司產品資料與年報列出 AI／資料中心伺服器產品。",
     "evidence_url": "https://www.wiwynn.com/products",
     "evidence_date": "2026-11-01", "confidence": "產品關聯已驗證；特定客戶關係未確認"},
    {"map_key": "it-cooling-power-2308", "us_industry": "technology", "chain": "散熱／電源供應",
     "code": "2308", "name": "台達電", "product": "資料中心液冷、氣冷與電源方案",
     "role": "資料中心電源與熱管理方案", "reason": "公司公開產品資料直接列出 AI／HPC 資料中心冷卻與電力方案。",
     "evidence_url": "https://www.deltaww.com/en-US/products/data-center-cooling",
     "evidence_date": "2026-11-01", "confidence": "產品關聯已驗證；個別專案關係依公告確認"},
    {"map_key": "semiconductor-foundry-2330", "us_industry": "semiconductors", "chain": "半導體",
     "code": "2330", "name": "台積電", "product": "晶圓代工、先進封裝",
     "role": "半導體同業／供應鏈比較基準", "reason": "公司年報說明先進製程與封裝服務支援 HPC／AI 應用。",
     "evidence_url": "https://investor.tsmc.com/static/annualReports/2025/english/index.html",
     "evidence_date": "2025", "confidence": "產品及產業關聯已驗證；不代表與特定 ETF 成分公司存在供貨關係"},
    {"map_key": "semiconductor-foundry-2303", "us_industry": "semiconductors", "chain": "半導體",
     "code": "2303", "name": "聯電", "product": "邏輯與特殊製程晶圓代工",
     "role": "半導體同業比較基準", "reason": "公司公開資料說明其晶圓代工與特殊製程業務。",
     "evidence_url": "https://www.umc.com/en/About/about_overview",
     "evidence_date": "2026-11-01", "confidence": "產業關聯已驗證；不代表與特定 ETF 成分公司存在供貨關係"},
    {"map_key": "semiconductor-memory-2344", "us_industry": "semiconductors", "chain": "記憶體",
     "code": "2344", "name": "華邦電", "product": "Specialty DRAM、Flash 記憶體",
     "role": "記憶體產品公司", "reason": "公司公開產品資料列有 DRAM 與 Flash 記憶體產品；與美股半導體 ETF 是同產業觀察，不代表特定客戶供應關係。",
     "evidence_url": "https://www.winbond.com.tw/hq/about-winbond/csr-new/corporate-governance/innovation/?__locale=en",
     "evidence_date": "2026-11-01", "confidence": "產品及產業關聯已驗證；特定客戶關係未確認"},
    {"map_key": "industrials-automation-2049", "us_industry": "industrials", "chain": "工業自動化",
     "code": "2049", "name": "上銀", "product": "工業機器人、線性運動及自動化零組件",
     "role": "自動化設備與零組件", "reason": "公司年報及產品頁列出機器人、半導體及自動化設備應用。",
     "evidence_url": "https://www.hiwin.tw/stock/annual_report/2024_Annual_report.pdf",
     "evidence_date": "2024", "confidence": "產品關聯已驗證；美國終端客戶關係未確認"},
]

# A verified steel business example for XLB's metals/materials subset, not a US customer claim.
SEED_SUPPLY_CHAIN.append({
    "map_key": "materials-steel-2002", "us_industry": "materials", "chain": "鋼鐵材料",
    "code": "2002", "name": "中鋼", "product": "鋼材及鋼鐵產品應用",
    "role": "鋼鐵材料製造；對照 XLB 的金屬材料部分",
    "reason": "公司官網列有鋼材應用、產品業務營收與鋼品研發；與美股原物料中的金屬材料作業務比較，不代表與整個 XLB 同漲跌。",
    "evidence_url": "https://www.csc.com.tw/index.html", "evidence_date": "2026-11-02",
    "confidence": "公司鋼鐵業務已查核；美國特定客戶與訂單關係未確認",
})
