# suki_socialui · Post Studio（小紅書帖務工作台）

本地 Web 工作台，用小紅書帳號做「設計帖 → 校對 → 發布 → 養號 → 私信」全流程：

- 🪄 **一鍵自動設計**：讀全文人設（AGENTS.md ＋ posts 日記 ＋ 已發布承接）→ LLM 出標題／正文／標籤／出圖提示，雙重紅線掃描後入帖檔待校對
- 📝 **帖檔正典 `posts.json`**：離線保存全部帖（草稿／待校對／已發布／定時），版本歷史、修改指示、小紅書式手機預覽
- 🖼 **媒體生成**：接 ComfyUI 出圖／片（含 3:4 封面自動裁切＋淡暈影後處理）、媒體庫、提示詞生成隊列
- 🤝 **養號引擎**：按配額與生活節拍自動点赞／收藏／真誠短評／回覆自己帖下評論（LLM 逐條生成＋紅線把關）
- 💬 **自動私訊**：白名單制、每日配額、逐對像對話獨立保存、按上下文起草
- 📤 **發布**：即時／定時發布經 xiaohongshu-mcp；已發布帖可經創作平台嘗試刪除

## 啟動

需要 Python 3.8+（**純標準庫，零依賴**）。雙擊 `start-post-studio.bat` 或：

```
python server.py        # 開啟 http://127.0.0.1:8765
```

## 架構

| 檔案 | 角色 |
|---|---|
| `server.py` | 後端與全部業務邏輯（配額、白名單、承接、紅線掃描） |
| `xhs_core.py` | **小紅書操作抽象層**：統一操作目錄（發布／搜索／讚／評／覆／私信…）＋可插拔驅動＋優先序路由與統一日誌。新增渠道（官方 API、新版 MCP 工具等）＝加一個 Driver，業務碼零改動 |
| `index.html` | 前端（多頁導航；由磁碟直接 serve，改檔即生效） |

兩個內建驅動：`mcp`＝[xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp)（`localhost:18060`，負責 feed／搜索／互動／發布）；`cdp`＝本地 Chrome 除錯埠 `:9333` 模擬人手操作（負責私信讀寫與刪除——MCP 未提供的能力）。「⚙️ 系統設置」頁有即時能力矩陣（🔌 小紅書操作抽象層）。

## 依賴與目錄假設

本目錄假設放在項目根之下（`<項目根>/post-studio/`），路徑以項目根解析：

- `tools/cdp_tool.py` ＋ `tools/xiaohongshu-mcp/cookies.json` —— 私信／刪除功能（CDP 驅動）
- `media/` —— 生成圖落夾與媒體庫
- LLM／ComfyUI 端點喺「⚙️ 系統設置」管理；**沒有 `settings.json` 時自動用內置默認值**，可照樣運行。要預先配置可複製 `settings.example.json` → `settings.json`（填入 api_key 等）

## 不入庫的運行時檔案

`settings.json`（含 API keys）、`dm/`（真實私信對話）、`posts/`（人讀日記與發布日誌）、`engage-state.json`／`gen-queue.json`（狀態）、各流水日誌——見 `.gitignore`。
