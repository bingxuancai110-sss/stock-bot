# 月線提醒正式站故障修正（2026/11/10）

## 已取得的證據

1. GitHub run `42651312481` 的 Queue close check 回403；run `42653156314` 也明確回報 authentication rejected。不是當日無訊號，而是請求未通過驗證。GitHub有設定CRON_SECRET，但正式站不接受，不能把排隊或手動測試成功當成排程正常。
2. Render 11/10 13:45及16:00（台灣時間）回報「指數歷史有交易日缺漏或非交易日資料」。逐日核對找到10/23：Yahoo及TWSE官方10月月收盤均無這天，但原固定週日日曆把它算成開市日。不推測缺少原因，也不補假收盤。
3. Render亦有 `SSL error: ssl/tls alert bad record mac` 與 `decryption failed or bad record mac`，發生在共用長生命週期DB連線。未證明所有SSL錯誤源自同一機制；本次月線模組改為自己的短生命週期SSL連線，不重用網站池中的舊SSL串流。

## 修正

- GitHub workflow使用短效OIDC簽章（固定RS256、GitHub issuer、公鑰驗簽、專屬audience、repository與不可變repository_id、main ref與指定workflow路徑、事件種類及有效期限）。拒絕別的專案、fork/PR與其他工作流程。原CRON_SECRET驗證保留，其他cron-job.org任務不需換密鑰；Google/LINE/帳號登入不改。
- 採TWSE官方最近4個月的完整月收盤，使用官方回傳的實際交易日驗證MA20序列。前月完整資料持久快取，當月每天只更新一次；缺當日官方收盤仍明確失敗，不用昨日或估算值。歷史內的下一交易日也依已驗證官方交易日修正；未來仍用既有台股日曆。
- 月線自身的訂閱、靜音、功能總開關、狀態與快照改用短連線；SQL讀寫語意與既有開關相同。網站共用池、持股與損益不變。連線失敗不降級成同意推播。
- 月線有一條獨立且有界的輕量背景執行緒，不必等待全市場預熱佇列。跨實例仍使用原DB工作租約，推播仍使用原固定retry key／訊息。
- POST回傳真正可追蹤的run UUID；新增受相同驗證保護的GET `/cron/taiex-reduction/status?id=...`。只保存此任務狀態，不公開持股或user_id。工作結果保存到既有shared_data_snapshots，任務專屬歷史保留30日。
- GitHub不再只檢查202，而是最多7分鐘輪詢對應run的終態；官方資料失敗、DB失敗、LINE未確認及超時會真的失敗。403/404/503部署切換有有限次重試。workflow於本次相關程式合併main後自動執行，提供部署後端到端驗證，仍保留原13:40/14:10/15:10排程。

## 驗證

- 實際TWSE資料：67筆收盤，截至2026/11/10，MA20計算完成；10/23未出現在官方交易日集合。過程未發LINE或寫正式持股。
- 新測試覆蓋真實RSA簽章與錯誤claim/簽章/演算法、官方月報快取與缺資料、202不等於完成、有限輪詢、工作錯誤與短生命週期SSL連線。
- 原2日/3日、舊走勢不補發、3%/4.5%回補、LINE卡片、權限、去重規則均回歸。
- 正式站驗收以本次GitHub run的完成結果為準；部署live與HTTP202本身不算月線工作完成。

## 部署／回滾

沿用原main自動部署，不更動Render計費方案或環境密鑰。新增免費PyJWT crypto依賴（固定2.15.1）與GitHub工作身份權限，無付費行情或AI API。回滾前暫停此workflow，再revert本次PR；原表均未改結構。原CRON_SECRET不需還原，但舊GitHub密鑰不同步問題將重新存在，不可宣稱回滾後排程正常。
