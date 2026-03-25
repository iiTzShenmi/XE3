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

## Notes
- HAR 與本地拆出的 JS 參考檔已加入 `.gitignore`，避免誤推敏感樣本。
- 如果之後功能變更多，可以繼續把新 commit 追加到這份檔案尾端。
