"""Explain business scope; sector resemblance is not a confirmed customer link."""
INDUSTRY_GUIDES = {
 'technology': dict(us='軟體、半導體、電腦與科技硬體；不是全部都在做 AI 伺服器。', tw='分開看晶片、伺服器與電源散熱。下列伺服器公司只對應其中的硬體題材。', limit='美國軟體股上漲，不等於台灣伺服器訂單增加。', relation='部分業務／硬體題材'),
 'semiconductors': dict(us='美國半導體設計、製造與設備公司；XSD 是半導體 ETF。', tw='晶圓代工、記憶體、封裝等不同環節，需分別確認需求。', limit='台積電、聯電是代工；華邦電是記憶體。產品與景氣循環不同，不能視為同一種受惠。', relation='同產業、不同環節'),
 'financials': dict(us='銀行、保險、資產管理與金融服務。', tw='台灣銀行、保險與金控，可比較相近的金融業務。', limit='兩國利率、放款與保險制度不同；同業比較不代表供應鏈。', relation='金融同業比較'),
 'healthcare': dict(us='藥品、醫材、生技，也包含醫療保險與醫療服務。', tw='先分辨是製藥、醫材還是醫療服務，再找相同業務。', limit='美國醫療保險股走強，不能直接推論台灣生技藥品受惠。', relation='需先拆分醫療業務'),
 'industrials': dict(us='航太國防、機械設備、運輸與工業服務。', tw='自動化、機械零組件等設備業務；上銀只對應自動化這一部分。', limit='美國航空或國防股走強，不能直接套用到台灣自動化公司。', relation='部分設備業務相近'),
 'consumer_discretionary': dict(us='電商、汽車、零售與休閒等可延後的消費。', tw='汽車零件、品牌零售、休閒等，依實際產品分開研究。', limit='不是「消費電子」的同義詞；電商平台與台灣電子代工的獲利來源不同。', relation='需按產品細分'),
 'consumer_staples': dict(us='食品飲料、家庭日用品與必需品零售。', tw='食品飲料與民生日用品等相近業務。', limit='品牌、原料與銷售市場不同；不能當作美國公司的供應商名單。', relation='民生消費同業比較'),
 'energy': dict(us='石油、天然氣開採、煉油與油田服務。', tw='先看油品、天然氣或能源服務的實際曝險。', limit='XLE 不是綠能 ETF；太陽能、風電與重電不能直接對應。', relation='油氣業務參考'),
 'utilities': dict(us='電力、瓦斯與供水等公用事業營運商。', tw='電力與公用服務需求可作背景；電網設備屬另一個環節。', limit='美國電力公司不是台灣重電設備商；須有資本支出或訂單證據，才談受惠。', relation='間接需求題材'),
 'materials': dict(us='化學、金屬礦業、建材、紙業與包裝材料。', tw='分開看鋼鐵、石化、建材與包材；依公司實際產品對照。', limit='不是所有原物料都一起漲；鋼價、油價與化工原料的影響可能不同。', relation='材料同業、產品需細分'),
 'real_estate': dict(us='以不動產投資信託（REITs）與不動產管理為主。', tw='要區分收租資產與建案開發；台灣建商的獲利模式不同。', limit='美國 REITs 上漲，不能直接視為台灣營建股買進訊號。', relation='資產類型參考'),
 'communication_services': dict(us='網路平台、媒體娛樂與電信服務。', tw='電信服務或內容業務可分項比較，需先確認是哪一種。', limit='XLC 不是網通硬體 ETF；不能因「通訊」兩字就對應路由器、交換器代工。', relation='需拆分平台、媒體與電信'),
}


def public_industry_guides():
    return {key: {**value, 'source_url': ('https://www.ssga.com/us/en/individual/etfs/spdr-sp-semiconductor-etf-xsd'
                                       if key == 'semiconductors' else 'https://www.sectorspdrs.com/')}
            for key, value in INDUSTRY_GUIDES.items()}
