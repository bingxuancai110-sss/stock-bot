import subprocess
import json
from datetime import datetime

# 1. 這裡放你們篩選全市場股票的邏輯，假設最後篩選出這幾檔
selected_stocks = [
    {"code": "2330", "name": "台積電", "tag": "🔥 創歷史新高，上方無壓力"},
    {"code": "3293", "name": "鈊象", "tag": "🚀 量價齊揚突破前高"}
]

data = {
    "update_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    "stocks": selected_stocks
}

# 2. 自動存成 radar_data.json
with open("radar_data.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=4)

# 3. 用 Python 自動幫你執行 git 推送到 GitHub
subprocess.run(["git", "add", "radar_data.json"])
subprocess.run(["git", "commit", "-m", "自動更新全市場雷達資料"])
subprocess.run(["git", "push", "origin", "main"])
print("全市場雷達更新並上傳成功！")
