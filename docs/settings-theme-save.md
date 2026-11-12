# 配色設定保存與片段修正

設定頁原先在 fragment=1 請求仍直接 render_page 回完整殼層，會在既有主內容再次插入頁首與腳本。配色表單也共用一般 POST 防重送與30秒未確認回饋；無獨立可確認的保存結果，可能長期停留處理中。

本次設定頁沿用 respond_page：局部導航只回內容片段，直接開頁仍回完整頁。配色表單由獨立、一次綁定的 settings-theme.js 接管，僅攔截 data-settings-theme-form，先於舊 document handler 執行；買賣與其他表單的處理不變。儲存中立即回饋、防連點，任何結果皆恢復按鈕；若寫入回應遺失，先 GET 讀回已保存值，絕不自動重送 POST。讀回也失敗時，按鈕改為「確認目前配色」，只執行讀取，不能把 HTTP200 HTML 當成功。

GET／POST /web/api/settings/theme 沿用現有登入與 settings 功能開關，POST沿用綁定使用者的 CSRF。資料庫僅更新已登入 user_id 的 user_profile.ui_theme 與 updated_at；沒有接受外部 user_id、沒有新資料表。獨立短連線及查詢／鎖定逾時避免長時間佔用共用池，RETURNING與commit成功後才確認保存。API讀取失敗不回預設色冒充資料庫現值。

成功後更新 html[data-ui-theme] 與瀏覽器 theme-color，既有三組配色、漲跌色與其他功能不改。靜態資源新版本 settings-theme.js?v=20261112-1。無新增付費服務或依賴。

驗證：8項Python測試通過，包括實際設定renderer的片段／完整頁、身分隔離、CSRF與非法配色、commit失敗、讀取失敗。Chromium與WebKit各17項操作檢查通過，涵蓋實際設定renderer與原共用CSS／舊submit handler、保存後重整、實際底色、遺失回應、離線確認不重送、HTTP200假回應、防連點、反覆局部導航及桌面。所有fixture.test請求攔截，帳號為隔離測試狀態，不寫正式帳號；WebKit不等於iPhone實機。

正式驗證界線：本機瀏覽器連線不可用，原生Chrome操作亦遭URL辨識安全限制停止；不以測試圖片宣稱已登入使用者正式帳號操作。部署後核對live版本、靜態資源與未登入API仍為401。

重跑：python -m pytest -q tests/test_settings_theme.py；python tests/browser/run_settings_theme.py chromium；python tests/browser/run_settings_theme.py webkit。Windows執行器需要本機loopback權限，瀏覽器所有請求仍限制在測試攔截範圍。產物可由STOCKBOT_UI_ARTIFACTS指定，預設test-artifacts/settings-theme。

回滾：revert本次提交並由Render部署；既有mist／warm／stone資料值與舊版相容，不需要資料還原、遷移或更動持股／損益。
