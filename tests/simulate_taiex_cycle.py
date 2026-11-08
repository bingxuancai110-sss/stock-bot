"""Isolated synthetic-price simulation. No database, network, or LINE delivery."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_taiex_cycle import walk


def main():
    prices = [115, 114, 113, 100, 101, 103, 104.5, 105]
    states = walk(prices)
    labels = {2: "第2日預警", 3: "第3日確認，配置降至50%", 30: "反彈3%，回補至75%", 45: "反彈4.5%，回補至100%"}
    print("隔離模擬；全部價格與交易日曆均為測試輸入，沒有寫入正式行情、持股或發送LINE。")
    print("基準：此前27個模擬交易日收盤皆120，起始收盤位於MA20上方。")
    print("日期 | 模擬收盤 | 提醒 | 預定執行日")
    for close, state in zip(prices, states):
        emitted = state["events"]
        print(f"{state['as_of']} | {close:g} | " +
              ("；".join(labels[e["level"]] for e in emitted) if emitted else "無新提醒") +
              " | " + ("、".join(e["next_day"] for e in emitted if e["level"] != 2) or "—"))
    assert [event["level"] for state in states for event in state["events"]] == [2, 3, 30, 45]
    print("驗證：2日／3日／3%／4.5%均觸發一次，執行日在訊號日的下一個模擬交易日。")


if __name__ == "__main__":
    main()
