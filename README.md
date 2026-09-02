# 出量突破掃描（台股・日終版）

每個交易日收盤後掃描台股全市場約 1,950 檔普通股，找出「突破前 20 日高點且明顯出量」
的個股，並依可重算的技術條件算出交易劇本價位。

網頁：https://fengsh2003-cpu.github.io/breakout-scan/

## 這不是什麼

- **不是盤中即時訊號**。資料為官方每日收盤行情，訊號在收盤後才成立，
  最快隔日開盤才能進場。
- **沒有逐筆成交明細（tick）**，因此不提供「25 張以上大單買超」「買賣成交比」
  「大單區間」這類欄位——那需要券商 API（例如永豐 Shioaji）。
- **不重製任何第三方選股服務的私有演算法**。本專案的規則全部寫在
  `breakout_scan.py` 的常數區，可自行重算與驗證。
- **不是投資建議**。門檻為初始設定值，**尚未經回測校準**，不等於已驗證的勝率。

## 命中條件（須同時成立）

| 條件 | 值 |
|---|---|
| 收盤價 > 前 N 日最高收盤 | N = 20 |
| 當日量 ≥ 前 5 日均量 × 倍數 | 2.0 |
| 當日漲幅 | ≥ 3.0% |
| 前 5 日均量（流動性） | ≥ 500 張 |
| 收盤價 | ≥ 10 元 |

## 價位計算依據

- 突破確認線 = 前 20 日最高收盤
- 箱高 = 突破確認線 − 前 20 日最低價
- 防守／型態失效 = 突破確認線 − 0.5 × ATR(14)
- 目標1 = 突破確認線 + 0.5 × 箱高；目標2 = 突破確認線 + 1 × 箱高（量測滿足點）
- R/R =（下一個仍在進場價上方的目標 − 進場價）÷（進場價 − 防守價）

追價與回測承接兩種進場分別列出 R/R。漲停鎖死的個股收盤常已越過目標1，
其追價 R/R 會很差——**那是真實資訊，不是計算錯誤**。

## 資料來源（皆為官方公開端點）

| 來源 | 用途 | 成本 |
|---|---|---|
| TWSE `MI_INDEX?type=ALLBUT0999` | 上市全市場單日 OHLCV | 1 次請求／日 |
| TPEx `afterTrading/otc` | 上櫃全市場單日 OHLCV | 1 次請求／日 |

`data/*_equities.csv` 的代號→產業別對照，取自 [mlouielu/twstock](https://github.com/mlouielu/twstock)
（MIT 授權）的靜態快照，只保留 code/name/group 三欄。

## 本機執行

```bash
python breakout_scan.py backfill 90   # 建立/補齊全市場日K快取
python breakout_scan.py scan          # 掃描並列印
python breakout_scan.py export        # 產生 breakout_export.json
python breakout_scan.py prune 70      # 刪除過舊快取
```

只用 Python 標準庫，不需安裝任何套件、不需 API token。

## 自動更新

`.github/workflows/refresh.yml` 於台北時間平日 14:30 與 17:00 各跑一次，
資料有變動才 commit，觸發 GitHub Pages 重新部署。
