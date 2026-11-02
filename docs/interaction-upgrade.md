# 全站視覺回饋升級

範圍：Flask HTML、局部切頁與瀏覽器呈現層；不更改分析、TWR、持股計算、資料來源、認證與 API 契約。

1. 共用 `static/ui-feedback.css` / `static/ui-feedback.js`：按壓 110ms、展開 220ms、頁面 180ms、數字 400ms；骨架有結束／取消／失敗狀態，表單有處理中／成功／錯誤與防重送，導覽即時回饋。
2. 持股：在既有快照、SVG 座標與顯示值上加入數字過渡及圖表追蹤；圓環、圖例與持股卡片雙向選取。
3. 排行：利用既有三榜 HTML 與實際排名變化提示，加入本機切換及圖表追蹤；其餘頁面的載入與展開沿用共用層。
4. 回歸：實際 renderer + 共用資源 + 合成帳號／行情；手機與桌面、觸控／鍵盤、快速切頁、取消／失敗／逾時、重入頁面、reduced-motion、請求數與手機效能。另跑既有 Python 測試、語法與差異檢查。

原則：不攔截或重送全域 fetch；不為動畫額外抓資料；資料值立即更新，動畫只是 aria-hidden 視覺層；不偽造進度或排名變化；頁面離開取消動畫／計時器。保留紅漲綠跌。觸控圖表提供追蹤模式，預設保留原本水平捲動與垂直頁面捲動。

原生 Safari 必須有可用 Safari 裝置／執行環境才能聲稱實機驗證；Chromium 行動模擬與 Playwright WebKit 結果分別記錄。

## 完成功能

- `main.py`：僅修改呈現與瀏覽器事件；所有正式頁面載入同一份資源。初次載入採頁內骨架，切頁保留舊內容、支援取消與失敗恢復；快速切頁忽略過時回應。
- `static/ui-feedback.css`：集中按壓、展開、頁面及數字動畫時長／緩動、淺色選取狀態、骨架、圖表追蹤列、焦點及減少動態效果。
- `static/ui-feedback.js`：單次初始化、委派事件與共用狀態。數值原文立即更新，最多 12 個可見數字做 aria-hidden 過渡；相同數值、缺值、背景分頁不動畫。捲動、隱私遮罩與離頁即清理。
- 原生 details 與首頁自訂展開共用高度動畫；非同步內容改變高度也可平滑調整，結束後恢復自然高度。
- 持股圓環、圖例及持股卡片雙向選取，重設同步清除；股票列表提供明確選取入口。
- 持股與排行榜 SVG 使用既有資料／座標提供觸控追蹤、讀值、資料點高亮與鍵盤左右追蹤；預設保留原水平捲動，期間切換只用已載入內容。
- 排行榜沿用真實排名變化文字及三榜 HTML，新增切換完成回饋與有上限的進場動畫。
- 原生表單／新增及賣出流程有處理中、成功、錯誤與長等待未確認狀態。HTTP 200 中的業務驗證錯誤不誤當成功；不自動重送寫入。回應過久可收起提示，晚到的結果不覆蓋已離開的頁面。
- 修正持股頁既有 JavaScript 換行正規表示式轉義問題；確認事件只綁一次，離頁停止舊頁的行情計時器／可見性事件。
- 移除工作台為展示動畫而刻意保留的 700ms／180ms 等待；資料完成即呈現。
- 底部導覽立即顯示目的地等待狀態；成功才提交 aria-current，失敗維持原頁。保留紅漲綠跌；列印不保留虛擬圖表高度。

## 驗證結果

- 61 個既有 Python 測試通過。
- Chromium 87 項、WebKit 87 項瀏覽器檢查通過，無 JavaScript 錯誤。
- 360／375／390／430／1280 寬度、觸控／鍵盤、正常／reduced-motion、快速展開收合、相同數值／缺值、動畫清理與上限。
- 初次載入及切頁骨架的成功、失敗、逾時、取消、手動重試入口；快速切頁僅採用最後一次回應。
- HTTP 200 驗證失敗、成功只送一次、重入頁面只有一個確認視窗、未知寫入結果不宣稱成功。
- 本機排行榜切換／圓環選取不增加 API；沒有全域 fetch 攔截或為動畫增加行情請求。
- 排除顯示字串／圖表座標 metadata 後，持股處理與排行圖表的 Python AST 邏輯相同；TWR、損益、持股新增及目標配置儲存邏輯相同。
- Python 編譯、共享 JavaScript 與實際 HTML 產生的內嵌腳本語法、diff 空白檢查通過。

測試皆使用真實 renderer／共用資源與明確的合成持股、行情及會員資料，HTTP 回覆全部攔截於 fixture.test；未存取使用者登入帳號或發送真實寫入。WebKit 是實際瀏覽器引擎測試，不能稱為 iPhone Safari 實機驗證。

## 重跑方式

正式服務不新增任何套件或付費服務。瀏覽器測試僅需開發／CI 環境安裝 Playwright：

```sh
python -m pip install pytest playwright
python -m playwright install chromium webkit
python tests/browser/fixtures.py
python tests/browser/run_ui_feedback.py chromium
python tests/browser/run_ui_feedback.py webkit
python -m pytest -q tests
```

Linux 需要對應瀏覽器系統函式庫。可用 `STOCKBOT_CHROMIUM_EXECUTABLE` 指定現有 Chromium；`STOCKBOT_UI_ARTIFACTS` 指定 HTML／截圖／JSON 結果目錄，預設 `/tmp/stockbot-ui-validation`。測試程式不 import main，避免啟動正式背景工作。

四階段狀態：共用點擊／載入／操作狀態、持股圖表／圓環／數字、排行榜／其他頁面、行動效能／回歸均已完成。慢寫入另外驗證 HTTP 失敗、單次送出、未確認狀態、離頁後晚到回應不覆蓋新頁面，並提供完成通知。
