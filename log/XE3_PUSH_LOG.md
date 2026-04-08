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
| 2026-04-03 | `da8286d` | 新增 `/e3 today`、`/e3 week` 與 Phase 1 embed / 週視圖版型 |

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

### 14. Phase 1 便利功能：Today / Week
- 新增 `/e3 today`
  - 顯示今天的作業、考試與課程事件
  - 空狀態也統一改成 embed / card 風格
- 新增 `/e3 week`
  - 顯示未來 7 天內事件
  - 改成按日期分段，而不是單純事件流水
- Discord slash command 與 help 文案同步補上：
  - `/e3 today`
  - `/e3 week`
- Phase 1 新增功能統一走 embed/flex 顯示，和現有 Discord 回應保持一致

## Notes
- HAR 與本地拆出的 JS 參考檔已加入 `.gitignore`，避免誤推敏感樣本。
- 如果之後功能變更多，可以繼續把新 commit 追加到這份檔案尾端。

### 15. Phase 2 公告 / Forum 與維運靜默刷新
- 新增 `/e3 news`
  - 支援近期公告 / forum discussion 的統一總覽
  - summary 改成依課程分段，避免流水帳難以閱讀
- 新增 `公告詳情` 流程
  - 顯示課程、作者、時間、摘要與 `開啟 E3` 按鈕
- 新增公告附件入口
  - 公告附件沿用既有 Discord 直接下載 / proxy fallback 流程
- 新增 `/e3 news` 篩選
  - `course`
  - `recent_days`（3 / 7 / 14）
  - 文字指令也支援 `course` / `recent` 任意順序
- 所有 dropdown 加入 `↩️ 上一頁`
  - 可回到前一個結果頁，不用重新輸入指令
- 強化檔名清理
  - 對下載到 Discord 的檔名做 URL decode、非法字元替換與保留字防護，減少奇怪檔名造成的失敗
- 新增 owner-only `/e3 refresh`
  - 只允許 bot owner 使用
  - 靜默刷新所有已儲存的 E3 帳號
  - 只回覆 owner 一則摘要，不會對其他使用者推播垃圾訊息
- 在 `ENGINEERING_RULES.md` 補上維護型指令規則：owner-only，且預設不得廣播操作輸出

### 16. XE3 Workspace Rename 與第二輪結構一致化
- XE3 專案根目錄由 `~/server` 改名為 `~/xe3`
- HomeVault 相關專案搬出 XE3：
  - `~/homevault/IoT`
  - `~/homevault/XiaoMiBot`
- 暫時性 / prototype 工作區由 `~/temp` 改名為 `~/lab`
- `lab/exmas` 更正為 `lab/exams`
- XE3 新增 `apps/` 作為清楚的服務入口：
  - `apps/web/main.py`
  - `apps/discord/main.py`
  - `apps/line/main.py`
- XE3 新增 `agent/core/`：
  - `config.py`
  - `system_status.py`
- Weather feature 收成一致結構：
  - `service.py`
  - `data/`
  - `services/`
- E3 feature 第二輪一致化：
  - `data/`
  - `views/`
  - `services/`
  - `reminder/`
  - `scraper/`
  - `references/`
- 保留舊模組路徑的 thin wrapper，避免一次性搬移破壞既有 import
- 更新 README 與 systemd template 路徑，讓 `~/xe3` 成為新的 canonical root

### 17. Workspace 收尾與 Canonical Import 一致化
- XE3 第二輪結構整理繼續往前推進：
  - 新增 `agent/features/e3/utils/`，把共用 E3 helper 收到 `utils/common.py`
  - `handler.py`、Discord 平台、LINE 平台、scripts、reminder、services 全面改用新的 canonical import 路徑
- `README.md` 更新成目前真實結構：
  - `data/`
  - `services/`
  - `reminder/`
  - `views/`
  - `utils/`
  - 並明確標示哪些舊檔案只是 compatibility wrapper
- 補上 workspace 說明文件（local only，不在 XE3 git repo 內）：
  - `~/homevault/README.md`
  - `~/homevault/XiaoMiBot/README.md`
  - `~/lab/README.md`
  - `~/lab/exams/README.md`
- 驗證：
  - `py_compile` 通過
  - import smoke test 通過
  - `discord-bot.service` / `xe3-web.service` 重啟後正常運作
- 額外修正：清掉仍佔用 5000 port 的舊 `/home/eason/server/app.py` 進程，恢復 `xe3-web.service`

### 18. Tunnel Watchdog 改走 Discord DM
- `scripts/tunnel_watchdog.py` 的自動狀態通知由 LINE push 改為 Discord DM
- 新增 `DISCORD_NOTIFY_USER_ID` 設定，讓 watchdog 只私訊指定 Discord 使用者
- 保留既有 watchdog 健康檢查與狀態檔機制，只替換通知出口
- `README.md` 同步更新為 Discord DM 通知說明
- 驗證：
  - `py_compile` 通過
  - `cloudflared-watchdog.service` 重啟後正常運作

### 19. Reminder 排除已繳作業
- 修正 12 小時倒數提醒與定時 digest 會把「已經提交的作業」也一起推送的問題
- `services/events.py` 現在在抽取 `calendar_upcoming` 類型的 homework 事件時，會先對照目前作業完成狀態，避免把已繳作業寫進 `events_cache`
- `reminder/worker.py` 再補一層發送前過濾：即使資料庫裡還留著舊事件，也會依 runtime 內最新作業狀態排除已繳 homework
- `build_test_reminder_payloads()` 也套用同樣規則，避免測試提醒時看到已繳作業
- `utils/common.py` 新增共用標題正規化 helper，讓 homework 標題比對更穩定
- 驗證：
  - `py_compile` 通過
  - 以現有 runtime/DB 實測，已繳 homework 會在 reminder 過濾掉
  - `discord-bot.service` 重啟後正常運作

### 20. Reminder 發送前輕量同步
- homework reminder 現在在真正送出前，會先檢查該使用者最近是否已同步
- 若最近 10 分鐘內沒有成功同步，而且這次提醒候選中包含 homework，worker 會先做一次輕量 sync，再重新判斷事件是否仍需提醒
- 這層 pre-sync 只在需要時觸發，不會每次 poll 都對所有使用者刷新，避免不必要負載
- pre-sync 若失敗，不會把使用者的 `login_status` 汙染成 `error`，避免因短暫網路問題把整條提醒鏈停掉
- `Test Reminder` 也會先做同樣的 pre-sync，再產生測試 payload
- 驗證：
  - `py_compile` 通過
  - `discord-bot.service` 重啟後正常運作

### 21. Selenium Cleanup 與成績試算
- 調整 `scraper/get_course/get_user_data.py` 的 Selenium driver lifecycle：
  - 不再使用固定 `--remote-debugging-port=9222`
  - 改成在 `try` 內建立 driver
  - `finally` 中先判斷 `driver is not None` 再 `quit()`
  - 避免 driver 初始化或登入流程異常時殘留背景 Chrome process
- 新增 `services/grade_calculator.py`
  - 解析 E3 成績中的 `weight` / `range` / `score`
  - 計算目標總成績所需的剩餘平均與各項目估算分數
  - 若老師未提供足夠配分資料，會明確回覆「暫時無法可靠試算」
- `handler.py` 新增：
  - `e3 成績試算 <課號或課名> <目標分數>`
  - `e3 passcalc <課號或課名> <目標分數>`
- Discord slash command 新增：
  - `/e3 passcalc`
  - 支援課程 autocomplete 與目標分數輸入
- help 文案同步更新
- 驗證：
  - `py_compile` 通過
  - 本地 smoke test 驗證 `成績試算` / `passcalc` 指令可正常回應
  - `discord-bot.service` 重啟後正常運作

### 22. 清理非 Discord 舊帳號資料
- `data/db.py` 的 `delete_user_data()` 補上完整刪除：
  - 會一併刪除 `discord_delivery_targets`
  - 最後也會刪除 `users` 主表列，避免留下孤兒帳號
- 本機資料清理：
  - 移除舊的 LINE-style 使用者資料與 runtime 工作目錄
  - `/e3 refresh` 的掃描名單現在只剩 Discord 使用者
- 備註：
  - 這次屬於本機資料清理，實際刪掉的 user/runtime 不會反映在 Git 內

### 23. 修正 `.gitignore` 誤傷 source module
- 將 `.gitignore` 中過於寬鬆的 `data/` 規則改成只忽略 repo 根目錄的 `/data/`
- 讓 `agent/features/e3/data/` 與 `agent/features/weather/data/` 這些 canonical source module 可以正常被 Git 追蹤
- 避免出現「本機有新結構，但遠端 repo 少了實際模組檔」的風險

### 24. 成績試算補上「未標配分平均分攤」
- `/e3 passcalc` / `e3 成績試算` 現在支援老師未填配分時的估算模式
- 規則：
  - 若全部項目都沒標配分，預設平均分攤 100%
  - 若部分項目已有配分，先保留已知配分，剩餘百分比再平均分給未標配分項目
- 輸出會清楚標示這個假設，避免讓使用者誤以為是 E3 官方配分
- 若已知配分總和已經超過 100%，則保守停止試算並回報資料異常
- 驗證：
  - `py_compile` 通過
  - 本地 smoke test 驗證「全未標配分」課程可以正常試算

### 25. 成績試算卡片式段落
- `passcalc` / `成績試算` 的結果卡改成更清楚的段落式呈現
- 上半部保留整體摘要：
  - 目標總成績
  - 已辨識配分
  - 已完成配分
  - 剩餘配分
  - 目前已拿到加權
- 下半部的「剩餘項目估算」改成獨立小卡段落：
  - 每個項目會單獨顯示名稱、目標分數、配分
  - 在 Discord 上比較像卡片列表，手機閱讀更舒服
