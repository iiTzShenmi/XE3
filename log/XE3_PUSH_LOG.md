# XE3 Push Log

這份檔案記錄 XE3 Discord 版從建立到目前為止的重要 push 紀錄，方便之後快速回看某個功能是在哪一批進來的。

## Commit History

| Date | Commit | Summary |
|---|---|---|
| 2026-03-18 | `ffa0999` | Add Discord bot scaffold |
| 2026-03-18 | `0488a8e` | 強化 Discord bot UX 與 service setup |
| 2026-03-18 | `3e9b817` | 改善 Discord selector 與登入流程 |
| 2026-03-18 | `1dcf793` | 細修 Discord 檔案 selector |
| 2026-03-18 | `0281972` | 小型 E3 檔案可直接傳到 Discord |
| 2026-03-18 | `bab8fde` | 改善 Discord 提醒控制 |
| 2026-03-18 | `88749da` | 調整 Discord 檔案分頁數量 |
| 2026-03-18 | `1e365d6` | 課程自動完成與 reminder ownership 強化 |
| 2026-03-21 | `7715668` | 提升 Discord UX 與 E3 scraper coverage |
| 2026-03-24 | `8abb126` | 改善 homework 檔案抓取與 tunnel 穩定性 |
| 2026-03-24 | `c074297` | 細修 Discord 檔案傳送與 homework 附件流程 |
| 2026-03-25 | `9c5d405` | 擴充 E3 scraper coverage |
| 2026-03-25 | `eb5f707` | 整理 E3 event / reminder / response formatting |
| 2026-03-25 | `f2a1470` | 細修 Discord interaction 與檔案短連結 |
| 2026-03-25 | `d691dd9` | 統一 Discord 回應方式、重整課程摘要與提醒 embed |
| 2026-03-25 | `dda738d` | 細修 Discord selector summary 與課程摘要可讀性 |
| 2026-03-25 | `ce0d2d3` | 清理本地快取並拆分 Discord / E3 顯示模組 |
| 2026-04-03 | `970c4f7` | 重構 E3 / Discord payload rendering 與工程規則 |
| 2026-04-03 | `bf103f7` | 拆分 reminder worker / payload 模組 |
| 2026-04-03 | `190f1e0` | 第二輪拆分 Discord views / command helpers |
| 2026-04-03 | `27f931e` | 拆分 Discord delivery / sender 與 E3 timeline/file 顯示模組 |
| 2026-04-03 | `3f503e1` | 修正 Discord `/e3 files` autocomplete key 對齊，補一輪 smoke test |
| 2026-04-03 | `0d119ba` | 改善 Discord selector 版型、成績選課流程與考試去重 |

## Milestones

### 1. Discord 基礎版建立
- 建立 Discord bot scaffold
- 加入 bot service 與基本命令路徑
- 把原本 LINE 導向的核心 E3 流程搬到 Discord 上

### 2. Discord 原生互動完成
- slash command / modal / selector / button 陸續補齊
- `/e3 login` 改成 modal
- 課程、檔案、作業等多筆資料改用 dropdown/select menu
- 小檔案支援直接傳到 Discord

### 3. Reminder 系統接上 Discord
- Discord 端 reminder worker 啟用
- reminder toggle / schedule / test reminder 完成
- reminder 訊息改成較 Discord-native 的顯示方式

### 4. E3 檔案與作業流程強化
- homework page 附件抓取補強
- 作業附件、已繳檔案、教材檔案統一路徑
- 長檔案連結改走短 token file proxy，避免 Discord URL 長度限制

### 5. Scraper 能力提升
- grades 新 schema：`grade_items`、`summary`、`columns`
- timetable / course outline endpoint 打通
- forums 抓取加入 runtime
- course outline / syllabus / exam candidate 的資料結構補齊

### 6. 顯示層與提醒邏輯整理
- timeline / upcoming / 課程詳情 / 成績總覽格式整理
- 部分課綱考試事件改成可進入 timeline / reminder
- grade 通知不再誤 dump 整份 `grades.json`

### 7. Discord 回應一致化與摘要重整
- `/e3 course` 維持 dropdown，但改成先顯示「課程摘要」，再用 `課程詳情` 展開補充資訊
- `課程摘要` 收斂成短版、易讀格式：考試提醒 / 作業 / 行事曆 / 檔案
- `/e3 timeline` 改成只顯示 30 天內事件，避免列表過長
- 提醒與成績更新通知改成 Discord embed 風格
- scheduled reminder 在「沒有事件」時也會送簡潔版提醒
- 一般 `/e3 ...` slash command 改成公開回應，互動元件優先原地更新訊息

### 8. Selector Summary 與摘要版 UI 再整理
- timeline selector 改成分區顯示：作業 / 考試 / 行事曆
- homework 檔案 selector 改成分區顯示：老師附件 / 你的提交
- 其他常用 selector 也改成同一種 grouped summary 風格
- 課程摘要加上更乾淨的段落、空行與完成狀態標記

### 9. 維護性整理
- 清掉本地 `__pycache__`，讓工作目錄更乾淨
- 把 Discord selector / embed summary helper 抽成 `agent/platforms/discord/rendering.py`
- 把課程摘要 / 課程詳情的 Flex card builder 抽成 `agent/features/e3/course_cards.py`
- 讓 `bot.py` / `handler.py` 更專注在流程，而不是同時塞滿顯示細節

### 10. 架構重構與維護規則
- 新增 `docs/ENGINEERING_RULES.md`，把後續重構與 review 規則寫下來
- 把 E3 共用邏輯拆成：
  - `common.py`
  - `file_catalog.py`
  - `course_runtime.py`
  - `payloads.py`
- 把 Discord 訊息解析 / 文字分塊 helper 抽成 `agent/platforms/discord/message_utils.py`
- selector / dropdown 摘要開始優先使用 `xe3_meta` 結構化 metadata，而不是猜字串
- `reminders.py` 改成 façade，實際拆成：
  - `reminder_payloads.py`
  - `reminder_worker.py`

### 11. 第二輪 Discord 維護性整理
- 把 Discord button / select / modal 類別抽成 `agent/platforms/discord/views.py`
- 把 autocomplete / help text 輔助抽成 `agent/platforms/discord/command_helpers.py`
- `bot.py` 現在更集中在 startup、command wiring 與核心流程
- 在 `ENGINEERING_RULES.md` 補上「拆 helper 時要同輪完成 wiring / 清掉 dead duplicate」這條關鍵提醒


### 12. 第三輪模組拆分
- 把 Discord 檔案下載與 payload 傳送邏輯拆成：
  - `agent/platforms/discord/file_delivery.py`
  - `agent/platforms/discord/payload_sender.py`
- `bot.py` 進一步縮小，只保留 startup / command wiring / 薄 wrapper
- 把 E3 timeline / file 顯示 helper 拆成：
  - `agent/features/e3/timeline_views.py`
  - `agent/features/e3/file_views.py`
- `handler.py` 清掉一批 duplicate common helper，改成直接依賴共享模組
- 在 `ENGINEERING_RULES.md` 補上「不要複製既有 common helper」這條提醒

- 補做一輪主要指令 smoke test，確認 `/e3 course`、`/e3 timeline`、`/e3 files`、`/e3 remind` 仍能產生有效 payload
- 修正 Discord `/e3 files` autocomplete 直接拿 raw user key 導致選項為空的問題
- 額外驗證 Discord payload sender 仍能組出：
  - `CommandSelectView`
  - `CommandButtonView`
  - `ReminderToggleView`

### 13. Discord UI 細修與成績流程整理
- `/e3 timeline` 的 selector summary 改成 description-style 排版：
  - `━━━━━━━━━━━━`
  - `🟠 作業 / 🔴 考試 / 🟢 行事曆`
  - 每筆項目用固定三行版型顯示
- `/e3 grades` 不再直接 dump 全部成績，改成先顯示「課程成績」dropdown，再進入單課成績詳情
- homework / file / grouped selector summary 逐步對齊同一種乾淨、短、好掃描的排版
- 課綱考試與 E3 calendar 考試加入跨來源 dedupe，避免同一天同類型考試重複出現

## Notes
- HAR 與本地拆出的 JS 參考檔已加入 `.gitignore`，避免誤推敏感樣本。
- 如果之後功能變更多，可以繼續把新 commit 追加到這份檔案尾端。
