# -*- coding: utf-8 -*-
"""Suki Post Studio v2 — 帖檔＋預覽＋修改＋發布（全部離線保存）。
資料正典：post-studio/posts.json（每帖含 status／版本歷史／feedback），
人讀版：posts/diary.md（AGENTS §11A.H）。發布直調 xiaohongshu-mcp。
啟動：start-post-studio.bat → http://127.0.0.1:8765
"""
import base64, hashlib, json, os, random, re, shutil, subprocess, sys, time, threading, traceback, uuid
import xhs_core as xc  # 🔌 小紅書操作抽象層（用戶 2026-11-22：「將小紅書的操作做成抽象層，之後有新方法可以使用」→ §11A.K）
import urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))  # D:\Projects\Suki
POSTS = os.path.join(os.path.dirname(__file__), "posts.json")
SETTINGS = os.path.join(os.path.dirname(__file__), "settings.json")
PUBLOG = os.path.join(ROOT, "posts", "publish-log.txt")
AUTOLOG = os.path.join(os.path.dirname(__file__), "auto-log.txt")

def autolog(msg):
    try:
        with open(AUTOLOG, "a", encoding="utf-8") as f:
            f.write("%s | %s\n" % (now(), msg))
    except Exception:
        pass

PROG = {"active": False, "stage": "空闲", "detail": "", "ts": "", "kind": ""}
_lockp = threading.Lock()

def progress(kind, stage, detail=""):
    with _lockp:
        PROG.update({"active": bool(kind), "stage": stage, "detail": detail[:120], "ts": now(), "kind": kind})
    autolog("… %s｜%s%s" % (kind, stage, ("：" + detail[:80]) if detail else ""))
MCP = "http://localhost:18060/mcp"
PORT = 8765
_lock = threading.Lock()

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def load_posts():
    with _lock:
        try:
            with open(POSTS, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

def save_posts(lst):
    with _lock:
        tmp = POSTS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lst, f, ensure_ascii=False, indent=1)
        os.replace(tmp, POSTS)

def find(lst, pid):
    for p in lst:
        if p.get("id") == pid:
            return p
    return None

def snapshot(p):
    return {"ts": now(), "title": p.get("title", ""), "content": p.get("content", ""),
            "tags": list(p.get("tags", [])), "images": list(p.get("images", []))}

DEFAULT_SETTINGS = {
    "llms": [
        {"id": "claude", "name": "Claude (Anthropic)", "type": "anthropic",
         "base_url": "https://api.anthropic.com", "api_key": "", "model": "claude-sonnet-5-5"},
        {"id": "lmstudio", "name": "LM Studio (本機 OpenAI 相容)", "type": "openai",
         "base_url": "http://127.0.0.1:1234/v1", "api_key": "none", "model": "local-model"},
        {"id": "ollama", "name": "Ollama (本機)", "type": "openai",
         "base_url": "http://127.0.0.1:11434/v1", "api_key": "none", "model": "llama3"},
    ],
    "comfyui": [
        {"id": "local", "name": "本機 ComfyUI", "url": "http://127.0.0.1:8188"},
    ],
    "active_llm": "claude",
    "active_comfy": "local",
    # 工作流管理（用戶拍板 2026-11-21：生成圖片／視頻可選唔同工作流 JSON，路徑喺系統設置管理）
    "workflows": [
        {"id": "img-z", "name": "Z-Image Turbo 人像（內置正典）", "type": "image", "path": "tools/wf_template.json"},
        {"id": "vid-h3", "name": "MiniMax H3 ref2va（Suki 視頻正典，自動帶 REF）", "type": "video", "path": "tools/wf_video_minimax_ref2va.json"},
    ],
    "active_image_wf": "img-z",
    "active_video_wf": "vid-h3",
}

def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    for k, v in DEFAULT_SETTINGS.items():
        s.setdefault(k, json.loads(json.dumps(v)) if isinstance(v, list) else v)
    return s

def http_probe(method, url, headers=None, payload=None, timeout=15):
    req = urllib.request.Request(url, data=(json.dumps(payload).encode() if payload is not None else None),
                                 headers=headers or {}, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "replace")
            return {"ok": True, "reachable": True, "ms": int((time.time() - t0) * 1000), "http": r.status, "detail": body[:200]}
    except urllib.error.HTTPError as e:
        # 有 HTTP 應答＝連得通，但 4xx/5xx 代表 key／model／路徑有問題
        return {"ok": False, "reachable": True, "ms": int((time.time() - t0) * 1000), "http": e.code,
                "detail": ((e.read(200).decode("utf-8", "replace") if e.fp else "") or "")[:200]}
    except Exception as e:
        return {"ok": False, "reachable": False, "ms": int((time.time() - t0) * 1000), "detail": str(e)[:200]}

def test_llm(cfg):
    base = (cfg.get("base_url") or "").rstrip("/")
    key = cfg.get("api_key") or ""
    if cfg.get("type") == "anthropic":
        hdr = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        r = http_probe("GET", base + "/v1/models", hdr)
        if not r["ok"] and r.get("http") in (404, None):
            r = http_probe("POST", base + "/v1/messages", dict(hdr, **{"content-type": "application/json"}),
                           {"model": cfg.get("model") or "claude-sonnet-5-5", "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]})
    else:
        r = http_probe("GET", base + "/models", {"Authorization": "Bearer " + key})
        if not r["ok"]:
            r = http_probe("POST", base + "/chat/completions", {"Authorization": "Bearer " + key, "content-type": "application/json"},
                           {"model": cfg.get("model") or "", "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]})
    return r

def get_wf_path(s, wtype, wf_id=""):
    """工作流管理：按 id（空＝active）攰返 workflow 條目；回 (絕對路徑或空, 錯誤)。"""
    wfs = [w for w in s.get("workflows", []) if w.get("type") == wtype]
    w = find(wfs, str(wf_id or "")) or find(wfs, str(s.get("active_image_wf" if wtype == "image" else "active_video_wf", ""))) or (wfs[0] if wfs else None)
    if not w or not (w.get("path") or "").strip():
        return "", ("未喺「工作流管理／系統設置」配置%s工作流" % ("圖片" if wtype == "image" else "視頻"))
    p = w["path"].strip()
    ap = p if os.path.isabs(p) else os.path.join(ROOT, p)
    if not os.path.isfile(ap):
        return "", "工作流 JSON 唔存在：%s" % p
    return ap, ""


def patch_wf(wf, prompt, label):
    """通用工作流 patch：prompt／text／positive 欄換入提示、seed 隨機、檔名前綴帶 label。回命中數。"""
    hit, seed = 0, (int(time.time() * 1000) ^ hash(label)) % 2 ** 31
    for node in wf.values():
        if not isinstance(node, dict):
            continue
        inp = node.get("inputs")
        if not isinstance(inp, dict):
            continue
        for k, v in list(inp.items()):
            kl = k.lower()
            if isinstance(v, str) and kl in ("prompt", "text", "positive", "positive_prompt"):
                inp[k] = prompt
                hit += 1
            elif kl in ("seed", "noise_seed") and isinstance(v, int):
                inp[k] = seed
            elif kl == "filename_prefix" and isinstance(v, str):
                inp[k] = "suki/gen-%s" % label if not inp[k].startswith("video/") else "video/Suki-%s" % label
    return hit


def comfy_upload(url, ap):
    """上傳本機圖到 Comfy input 目錄，回存咗嘅檔名（視頻 REF 用）。"""
    b = uuid.uuid4().hex
    name = os.path.basename(ap)
    ctype = "image/png" if name.lower().endswith(".png") else "image/jpeg"
    body = ('--%s\r\nContent-Disposition: form-data; name="image"; filename="%s"\r\nContent-Type: %s\r\n\r\n' % (b, name, ctype)).encode()
    with open(ap, "rb") as f:
        body += f.read()
    body += ("\r\n--%s--\r\n" % b).encode()
    req = urllib.request.Request(url.rstrip("/") + "/upload/image?overwrite=true", data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + b})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace")).get("name")


def test_comfy(cfg):
    url = (cfg.get("url") or "").rstrip("/")
    r = http_probe("GET", url + "/system_stats")
    if r["ok"]:
        r["detail"] = "ComfyUI 在線：" + r["detail"][:120]
    return r

#（mcp_call／mcp_raw／CDP 底層已併入抽象層 xhs_core——平台操作一律經 XHS.call(OP_*, …)，見 §11A.K）

# ── 平台刪除（經抽象層：xiaohongshu-mcp 冇刪除工具 → 路由自動用 CDPDriver；失敗回手動指引）──
def xhs_delete(p):
    title = (p.get("title") or "").strip()
    if not title:
        return {"done": False, "detail": "呢帖冇標題，無法喺平台定位——請喺 App 手動刪除"}
    r = XHS.call(xc.OP_DELETE, title=title)
    return {"done": bool(r.get("ok")),
            "detail": (r.get("detail") or r.get("error") or
                       "平台自動刪除未完成——帖已喺本地刪除，請喺小紅書 App／創作平台手動刪除該帖")}

# ────────── 🤝 自動互動引擎（養號＋朋友池入口；用戶 2026-11-21 凌晨：「定時點贊留言回覆訊息，設置好按開始就自動運行，可以停止，如何設計你來弄」→ 正典見 §11A.I） ──────────
# 小紅書避坑原則（寫死喺設計入面）：
#  1) 每日互動設總量上限＋動作之間最少一分幾鐘（過多／機械節奏＝機器人偵測，有限流風險）；
#  2) 回覆自己帖嘅評論最優先（CES 評論權重 ×4，且係人地主動搵嚟、最自然）；
#  3) 評論必須先攞筆記正文、真誠回應內容（唔准模板化／重複文案／純 emoji／求關注）；
#  4) 時段＝角色生活節拍（午間＋晚 21:10 前；21:30–翌日 09:00 永遠唔操作——夜貓會露馬腳）；
#  5) 唔主動作 follow、唔 DM（§12）、敏感題目迴避；同一筆記永遠唔重複互動（seen 保存 30 日）。
ENGAGE_SET = os.path.join(os.path.dirname(__file__), "engage.json")
ENGAGE_ST = os.path.join(os.path.dirname(__file__), "engage-state.json")
ENGAGE_LOG = os.path.join(os.path.dirname(__file__), "engage-log.txt")
DEFAULT_ENGAGE = {
    "windows_weekday": "12:20-13:10,20:30-21:10",
    "windows_weekend": "11:00-13:00,15:30-17:00,20:00-21:00",
    "sleep_min": 25, "sleep_max": 80,            # 兩個動作之間（秒）
    "round_gap_min": 240, "round_gap_max": 520,  # 兩輪之間（秒）
    "session_max_min": 25,                        # 單一時段上限（養號紀律 15–30 分鐘）
    "daily": {"like": 10, "favorite": 4, "comment": 4, "reply": 8},
    "round": {"like": 4, "comment": 2, "reply": 3},
    "comment_ratio": 40, "favorite_ratio": 15, "search_ratio": 40,
    "keywords": "港女 中学生,中学生日常,粤语,二次元 高中生,香港 学生",
    "blacklist": "政治,选举,维权,拆迁,募捐,抽奖",
}
ENG = {"th": None, "stop": None, "running": False, "note": "未啟動"}
_locke = threading.Lock()

def eng_cfg():
    cfg = dict(DEFAULT_ENGAGE)
    try:
        with open(ENGAGE_SET, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg

def eng_cfg_save(patch):
    cfg = eng_cfg()
    for k in DEFAULT_ENGAGE:
        if k in (patch or {}):
            cfg[k] = patch[k]
    try:
        with open(ENGAGE_SET, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return cfg

def eng_state():
    today = time.strftime("%Y-%m-%d")
    st = {"date": today, "counts": {"like": 0, "favorite": 0, "comment": 0, "reply": 0},
          "seen": {}, "sessions": 0}
    try:
        with open(ENGAGE_ST, encoding="utf-8") as f:
            old = json.load(f)
        cutoff = int(time.time()) - 30 * 86400
        st["seen"] = {k: v for k, v in (old.get("seen") or {}).items()
                      if isinstance(v, (int, float)) and v > cutoff}
        st["sessions"] = old.get("sessions", 0)
        if old.get("date") == today:
            st["counts"].update(old.get("counts") or {})
    except Exception:
        pass
    return st

def eng_state_save(st):
    try:
        with open(ENGAGE_ST, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass

def eng_log(msg):
    try:
        with open(ENGAGE_LOG, "a", encoding="utf-8") as f:
            f.write("%s | %s\n" % (now(), msg))
    except Exception:
        pass
    autolog("🤝 " + msg)


def eng_windows(cfg):
    s = cfg.get("windows_weekend") if time.localtime().tm_wday >= 5 else cfg.get("windows_weekday")
    out = []
    for part in re.split(r"[,，;；\s]+", (s or "").strip()):
        m = re.match(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$", part)
        if m:
            out.append((int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))))
    return out

def eng_in_window(cfg):
    wins = eng_windows(cfg)
    t = time.localtime()
    mins = t.tm_hour * 60 + t.tm_min
    return any(a <= mins <= b for a, b in wins), wins

def eng_llm(prompt):
    s = load_settings()
    llm = find(s["llms"], str(s.get("active_llm", ""))) or {}
    if not llm:
        return None, "未配置 active LLM（去「⚙️ 系統設置」加一個再喺表頭揀）"
    return call_llm(llm, prompt)

def eng_clean(t):
    t = re.sub(r'^["“「’\s]+|["”」’\s]+$', "", (t or "").strip())
    t = re.sub(r"\s*\n\s*", " ", t).strip()
    return t[:60]

def eng_redline(t):
    hits = [b for b in BANNED_AUTO if b in (t or "")]
    hits += [b for b in TAG_BAN if b in (t or "").lower()]
    return hits

COMMENT_TASK = """你係 Suki 本人（13 歲香港女中學生，小紅書用嚟交朋友）。你啱啱真係睇完咗以下呢篇筆記，而家要喺佢下面留一條真誠嘅評論。
【你嘅完整人設正典（全部行為語氣依呢度）】
%s
【你睇到嘅筆記】標題：%s
內容節錄：%s
請輸出「一條」評論本體（唔好引號、唔好解釋、唔好多條、唔好 markdown）：
- 港式粵語繁體、10–40 字、口語自然，要同呢篇筆記嘅實際內容真正相關（讚／共鳴／一個輕鬆疑問，三選一），可帶一個 emoji；
- 嚴守私隱紅線：唔提自己學校／班別／年齡／住所，唔出現任何店名路名地區名；
- 唔准「求關注」「互贊」「支持一下」呢類客套；唔准抄錄筆記原文；唔用「hhhh」「1」填充；唔碰政治敏感。"""

REPLY_TASK = """你係 Suki 本人（人設正典附後）。有人喺你小紅書帖子底下留言，你要真誠回覆呢位網友。
【你嘅完整人設正典】
%s
【你篇帖（被評論嘅帖）】%s
【對方昵称】%s
【對方評論】%s
請輸出「一條」回覆本體：港式粵語繁體、8–35 字、同對方講嘅嘢真正有回應、親切自然；嚴守私隱紅線（個人資料／地名店名一律唔提）；可帶一個 emoji；唔「求關注」；唔用「hhhh」「1」填充。"""

MY_NAMES = ("田中suki", "sostupid", "陈苏琪", "陳蘇琪")

def eng_parse_feeds(data):
    out = []
    for f in (data or {}).get("feeds", []) or []:
        nc = f.get("noteCard") or {}
        if not f.get("id") or not f.get("xsecToken"):
            continue
        out.append({"id": f["id"], "tok": f["xsecToken"],
                    "title": (nc.get("displayTitle") or "").strip(),
                    "author": ((nc.get("user") or {}).get("nickname") or "").strip()})
    return out

def eng_sleep(cfg, stop, a=None, b=None):
    lo = int(a if a is not None else cfg["sleep_min"]); hi = int(b if b is not None else cfg["sleep_max"])
    stop.wait(random.randint(max(3, lo), max(4, hi)))

def eng_round(cfg=None, st=None, force=True, dry=False, caps=None, log_prefix=""):
    """一輪互動：①覆自己帖嘅新評論 ②刷 feed／搜索 → 讚（＋按概率認真評論／收藏）。force 默認 True（時段檢查喺 loop 做）。"""
    cfg = cfg or eng_cfg(); st = st or eng_state()
    stop = ENG["stop"] or threading.Event()
    cap = dict(cfg.get("round") or {}); cap.update(caps or {})
    seen = st.setdefault("seen", {})
    counts = st.setdefault("counts", {"like": 0, "favorite": 0, "comment": 0, "reply": 0})
    did = {"like": 0, "comment": 0, "reply": 0, "favorite": 0, "skip": 0}

    def quota(kind):
        return counts.get(kind, 0) < int((cfg.get("daily") or {}).get(kind, 0))

    def xurl(fid):  # 用戶拍板 2026-11-21：互動日誌要顯示帖嘅 URL（方便人工覆核）
        return " https://www.xiaohongshu.com/explore/%s" % fid if fid else ""

    def act(kind, op, kw, label):   # op＝抽象層操作名（xc.OP_*），路由自動揀驅動（§11A.K）
        if not quota(kind):
            eng_log("⏸ %s——今日「%s」配額已滿，留返明日" % (label, kind)); return False
        if dry:
            eng_log("(dry) 將執行 %s：%s" % (kind, label)); return True
        r = XHS.call(op, **kw)
        if r.get("login_bad"):
            ENG["note"] = "登入態失效——請喺 App 重新掃碼（引擎已停止）"; eng_log("⛔ " + ENG["note"]); stop_engage(); return False
        if not r["ok"]:
            eng_log("⚠ %s 失敗：%s" % (label, (r.get("error") or r.get("detail") or "?")[:120])); return False
        counts[kind] = counts.get(kind, 0) + 1; eng_state_save(st)
        eng_log("✔ %s（今日 %s：%s/%s）" % (label, kind, counts[kind], (cfg.get("daily") or {}).get(kind)))
        return True

    canon_txt = None
    def canon():
        nonlocal canon_txt
        if canon_txt is None:
            canon_txt = full_canon()
        return canon_txt

    # ① 回覆自己帖下嘅新評論（價值最高）
    try:
        un = XHS.call(xc.OP_UNREAD)
        mentions = int((un.get("data") or {}).get("mentions") or 0) if un["ok"] else 0
    except Exception:
        mentions = 0
    if mentions > 0 and int(cap.get("reply", 0)) > 0:
        nt = XHS.call(xc.OP_NOTIFS, tab="mentions", limit=10)
        items = (nt.get("data") or {}).get("items") or [] if nt["ok"] else []
        done = 0
        for it in items:
            if done >= int(cap.get("reply", 0)) or not quota("reply"):
                break
            cid = str(it.get("comment_id") or it.get("id") or "")
            if not cid or ("nc:" + cid) in seen or "comment" not in str(it.get("type", "")):
                continue
            my_note = (it.get("feed_title") or "（自己篇帖）").strip()
            who = (it.get("from") or {}).get("nickname") or "網友"
            ctxt = (it.get("comment_text") or "").strip()[:200]
            txt = None
            if not dry:
                out, err = eng_llm(REPLY_TASK % (canon(), my_note, who, ctxt))
                txt = eng_clean(out)
                if not txt or eng_redline(txt) or len(txt) < 4:
                    eng_log("⚠ 回覆草稿唔合格（%s）——跳過呢條：" % (eng_redline(txt) or "空/太短") + (txt or (err or ""))[:60])
                    if not dry:
                        seen["nc:" + cid] = int(time.time()); eng_state_save(st)
                    continue
            if act("reply", xc.OP_REPLY, {"comment_id": cid, "text": txt or "（dry-run 測試回覆）"},
                   "覆 %s：「%s」→「%s」%s" % (who, ctxt[:18], (txt or "dry")[:28], xurl(it.get("feed_id") or it.get("id")))):
                done += 1; did["reply"] += 1
            if not dry:
                seen["nc:" + cid] = int(time.time()); eng_state_save(st)
            eng_sleep(cfg, stop) if not dry else None

    # ② 刷 feed／搜索 → 讚＋認真評論
    src = "search" if random.randint(1, 100) <= int(cfg.get("search_ratio", 40)) else "feed"
    kw = ""
    if src == "search":
        kws = [k.strip() for k in re.split(r"[,，\n]+", cfg.get("keywords") or "") if k.strip()]
        if not kws:
            src = "feed"
        else:
            kw = random.choice(kws)
            d = XHS.call(xc.OP_SEARCH, keyword=kw)
    else:
        d = XHS.call(xc.OP_FEEDS)
    if not d.get("ok"):
        eng_log("⚠ 攞%s失敗：%s" % ("搜索「%s」" % kw if kw else "首頁 feed", (d.get("error") or "?")[:120]))
        return {"ok": False, "msg": "攞候选失敗（見日誌）", "did": did}
    cands = eng_parse_feeds(d.get("data"))
    bl = [b for b in re.split(r"[,，\s]+", (cfg.get("blacklist") or "")) if b]
    cands = [c for c in cands
             if c["id"] not in seen and c["title"]
             and not any(b in c["title"] for b in bl)
             and not any(m in c["author"].lower() for m in MY_NAMES)]
    random.shuffle(cands)
    liked_now = []
    n_like = min(int(cap.get("like", 4)), len(cands))
    for c in cands[:n_like]:
        if act("like", xc.OP_LIKE, {"note_id": c["id"], "xsec_token": c["tok"]},
               "讚「%s」（%s）%s" % (c["title"][:24], c["author"] or "網友", xurl(c["id"]))):
            liked_now.append(c); did["like"] += 1
            if not dry:
                XHS.call(xc.OP_NOTE, note_id=c["id"], xsec_token=c["tok"])  # 睇吓先（都等咗真人阅读時間）
        if not dry:
            seen[c["id"]] = int(time.time()); eng_state_save(st)
            eng_sleep(cfg, stop)
    for c in liked_now:
        roll = random.randint(1, 100)
        want_c = (roll <= int(cfg.get("comment_ratio", 40)) and quota("comment")
                  and did["comment"] < int(cap.get("comment", 0)))
        want_f = (not want_c) and (roll > 100 - int(cfg.get("favorite_ratio", 15))) and quota("favorite")
        if not want_c and not want_f:
            continue
        if dry and want_c:
            eng_log("(dry) 將執行 comment：認真評論「%s」（LLM 以全文人設生成）%s" % (c["title"][:20], xurl(c["id"]))); did["comment"] += 1
            continue
        if want_f:
            if act("favorite", xc.OP_COLLECT, {"note_id": c["id"], "xsec_token": c["tok"]}, "收藏「%s」%s" % (c["title"][:20], xurl(c["id"]))):
                did["favorite"] += 1
            eng_sleep(cfg, stop)
            continue
        det = XHS.call(xc.OP_NOTE, note_id=c["id"], xsec_token=c["tok"]) if not dry else {"ok": True, "data": None}
        nd = (det.get("data") or {})
        note = nd.get("note") or nd
        desc = (note.get("desc") or note.get("content") or note.get("text") or "")[:500] if isinstance(note, dict) else ""
        cms = note.get("comments") if isinstance(note, dict) else None
        cml = cms.get("list") if isinstance(cms, dict) else (cms if isinstance(cms, list) else [])
        cmt_txt = "；".join((x.get("content") or "")[:40] for x in (cml or [])[:2])
        body_txt = (desc + ("\n（其他評論：" + cmt_txt + "）" if cmt_txt else "")).strip() or "（攞唔到正文，就住標題講）"
        out, err = (None, "dry") if dry else eng_llm(COMMENT_TASK % (canon(), c["title"][:80], body_txt[:600]))
        txt = eng_clean(out)
        if not txt or len(txt) < 5 or eng_redline(txt):
            eng_log("⚠ 評論草稿唔合格（%s）——跳過：「%s」" % (eng_redline(txt) or "空/太短", (txt or err or "")[:50]))
            continue
        if act("comment", xc.OP_COMMENT, {"note_id": c["id"], "xsec_token": c["tok"], "text": txt},
               "評論「%s」→「%s」%s" % (c["title"][:20], txt[:30], xurl(c["id"]))):
            did["comment"] += 1
            if random.randint(1, 100) <= int(cfg.get("favorite_ratio", 15)) and quota("favorite"):
                if act("favorite", xc.OP_COLLECT, {"note_id": c["id"], "xsec_token": c["tok"]}, "收藏「%s」%s" % (c["title"][:20], xurl(c["id"]))):
                    did["favorite"] += 1
        eng_sleep(cfg, stop)
    st["sessions"] = st.get("sessions", 0) + 1; eng_state_save(st)
    msg = "一輪完成：讚%s 評%s 覆%s 藏%s" % (did["like"], did["comment"], did["reply"], did["favorite"])
    eng_log("📊 %s%s" % (log_prefix, msg))
    return {"ok": True, "msg": msg, "did": did}

def eng_loop(stop):
    cfg = eng_cfg()
    eng_log("▶ 自動互動引擎啟動｜時段：%s｜日配額：%s" % (
        eng_windows(cfg), cfg.get("daily")))
    sess_end = 0.0
    while not stop.is_set():
        try:
            cfg = eng_cfg()
            if int(time.localtime().tm_hour * 60 + time.localtime().tm_min) < 9 * 60:
                ENG["note"] = "深夜静默（人設 21:30 熄燈）——9:00 後先繼續"; stop.wait(300); continue
            inw, wins = eng_in_window(cfg)
            if not inw:
                nxt = next((w for w in wins if w[0] > time.localtime().tm_hour * 60 + time.localtime().tm_min), None)
                ENG["note"] = "等待下一個時段" + ("（%02d:%02d 開始）" % divmod(nxt[0], 60) if nxt else "（今日已完）")
                stop.wait(120); continue
            if time.time() > sess_end:
                sess_end = time.time() + int(cfg.get("session_max_min", 25)) * 60
                eng_log("🕐 進入時段，本次最長 %s 分鐘（養號紀律）" % cfg.get("session_max_min"))
            ENG["note"] = "進行中——下一輪完成後更新"
            res = eng_round(cfg, force=True)
            ENG["note"] = (res.get("msg") if res.get("ok") else res.get("msg") or "輪次未完成") + "｜" + time.strftime("更新 %H:%M")
            if (cfg.get("dm") or {}).get("on"):          # 💬 自動私訊（白名單＋配額＋生活節拍，見 §11A.J）
                try:
                    if DM_LOCK.acquire(blocking=False):
                        try:
                            dmr = dm_pass(cfg)
                        finally:
                            DM_LOCK.release()
                    else:
                        dmr = {"sent": 0}
                    if dmr.get("sent"):
                        ENG["note"] += "｜私訊覆咗 %s 條" % dmr["sent"]
                except Exception as de:
                    eng_log("[私訊] ❌ 步驟異常：%s" % str(de)[:100])
            gap = random.randint(int(cfg["round_gap_min"]), int(cfg["round_gap_max"]))
            if time.time() + gap > sess_end:
                sess_end = 0.0
                gap = 90  # 本次時段完，等落個時段（時段檢查會接管）
            stop.wait(gap)
        except Exception as e:
            eng_log("❌ 互動循環異常：%s" % (str(e)[:120]))
            stop.wait(60)
    ENG["running"] = False
    ENG["note"] = ENG["note"] if "登入" in ENG["note"] else "已停止"
    eng_log("⏹ 互動引擎停止")

def start_engage():
    if ENG["running"]:
        return False, "已經喺度運行"
    if not XHS.call(xc.OP_LOGIN)["ok"]:
        return False, "MCP／登入狀態唔OK——先喺 tools\\xiaohongshu-mcp\\ 啟動服務或重新掃碼"
    ENG["stop"] = threading.Event(); ENG["running"] = True; ENG["note"] = "已啟動"
    ENG["th"] = threading.Thread(target=eng_loop, args=(ENG["stop"],), daemon=True)
    ENG["th"].start()
    return True, "已啟動（喺時段內即刻開始，時段外自動等待）"

def stop_engage():
    ENG["running"] = False
    if ENG["stop"]:
        ENG["stop"].set()
    return True

# ────────── 💬 自動私訊（逐對像獨立對話檔＋上下文回覆＋詳盡紀錄；用戶 2026-11-22 拍板 → 正典見 §11A.J） ──────────
# xiaohongshu-mcp 冇私信工具（18 tools 已核實）→ 用 §17「帖文編輯法」同一個本地瀏覽器（CDP 9333）。
# 網頁私信結構（2026-11-22 實測）：列表 /chat＝.xhs-im-conv-item（暱稱/時間/預覽/未讀數）；打開後 URL＝/chat/<user_id>；
# 氣泡 .chat-item（content--left＝對方／--right＝自己），文字 .xhs-im-bubble__text，時間 .xhs-im-msg-list__time-divider；
# 輸入＝contenteditable .xhs-im-input-bar-editor（CDP insertText 入字，Enter 發送，唔得再撃最右 .xhs-im-input-bar-action-btn）。
# 紀律（用戶 2026-11-22 要求全部寫死）：白名單制（逐對像喺 UI 剔「自動覆」，新對象一律唔自動）；
# 每日＋逐對像回覆上限（滿數即停、聽日 reset——「一日覆少量就好」）；只喺 Suki 生活節拍時段（上學/睡眠時間永遠靜默）；
# 回覆帶完整上下文（獨立存 post-studio/dm/threads/）＋§12 安全紅線＋紅線掃描；首次建档只做基線唔覆舊帳。
DM_DIR = os.path.join(os.path.dirname(__file__), "dm")
DM_THR = os.path.join(DM_DIR, "threads")
DM_IDX = os.path.join(DM_DIR, "index.json")
DM_ST = os.path.join(DM_DIR, "dm-state.json")
DM_LOCK = threading.Lock()
COOKIES = os.path.join(ROOT, "tools", "xiaohongshu-mcp", "cookies.json")

# ── 🔌 小紅書抽象層實例（§11A.K）：業務碼只經 XHS.call(xc.OP_*, …)；
#    驅動優先序可喺 settings.json "xhs_priority"（預設 ["mcp","cdp"]）——
#    以後 MCP 新版加入私信工具時，佢會自動接手 cdp 嘅 DM 操作，本檔零改動。
XHS_MCP = xc.MCPDriver(MCP)
XHS_CDP = xc.CDPDriver(ROOT, COOKIES)
try:
    _xhs_prio = load_settings().get("xhs_priority") or []
except Exception:
    _xhs_prio = []
_xhs_all = {"mcp": XHS_MCP, "cdp": XHS_CDP}
XHS = xc.XHS([_xhs_all[n] for n in _xhs_prio if n in _xhs_all] +
             [_xhs_all[n] for n in ("mcp", "cdp") if n not in _xhs_prio],
             log=lambda m: autolog(m))
DEFAULT_DM = {"on": False, "daily_max": 3, "thread_daily": 1, "scan_top": 8, "ctx_msgs": 24,
              "gap_min": 40, "gap_max": 110, "skip_names": "官方,客服,系统,系統,通知,小红书,小红書,直播"}

def dm_cfg():
    d = dict(DEFAULT_DM)
    try:
        d.update(eng_cfg().get("dm") or {})
    except Exception:
        pass
    return d

def dm_cfg_save(patch):
    cfg = eng_cfg()
    d = dict(DEFAULT_DM, **(cfg.get("dm") or {}))
    for k in DEFAULT_DM:
        if k in (patch or {}):
            d[k] = patch[k]
    cfg["dm"] = d
    try:
        with open(ENGAGE_SET, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return d

def dm_state():
    today = time.strftime("%Y-%m-%d")
    st = {"date": today, "sent": 0, "threads": {}}
    try:
        with open(DM_ST, encoding="utf-8") as f:
            old = json.load(f)
        if old.get("date") == today:
            st["sent"] = int(old.get("sent") or 0)
            st["threads"] = old.get("threads") or {}
    except Exception:
        pass
    return st

def dm_state_save(st):
    try:
        with open(DM_ST, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass

def dm_log(msg):
    eng_log("[私訊] " + msg)

def dm_index():
    try:
        with open(DM_IDX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"threads": {}}

def dm_index_save(ix):
    os.makedirs(DM_DIR, exist_ok=True)
    with open(DM_IDX, "w", encoding="utf-8") as f:
        json.dump(ix, f, ensure_ascii=False, indent=1)

def _dm_safe(uid):
    return re.sub(r"[^0-9a-zA-Z_-]", "", str(uid))[:48]

def dm_thread_path(uid):
    return os.path.join(DM_THR, _dm_safe(uid) + ".json")

def dm_load(uid):
    try:
        with open(dm_thread_path(uid), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def dm_save(th):
    os.makedirs(DM_THR, exist_ok=True)
    th["updated"] = now()
    with open(dm_thread_path(th["user_id"]), "w", encoding="utf-8") as f:
        json.dump(th, f, ensure_ascii=False, indent=1)

def dm_touch_index(uid, name=None):
    ix = dm_index()
    t = ix["threads"].get(uid) or {"auto": False, "created": now()}
    if name:
        t["name"] = name
    t["file"] = "dm/threads/%s.json" % _dm_safe(uid)
    t["updated"] = now()
    ix["threads"][uid] = t
    dm_index_save(ix)
    return t

def _dm_fp(dir_, text):
    return hashlib.md5(("%s\x00%s" % (dir_, (text or "")[:160])).encode("utf-8")).hexdigest()[:16]

def _dm_trig_ok(text):
    t = (text or "").strip()
    if not t or t.startswith("["):          # [表情]／[筆記] 分享卡唔當「要覆嘅話」
        return False
    for bad in ("相互关注", "開始聊天", "开始聊天"):
        if bad in t:
            return False
    return True

def dm_merge(uid, name, msgs):
    """合併入逐對像對話檔（dir+文字指紋去重）。首次建档只做基線——唔當新訊息、唔觸發回覆。"""
    th = dm_load(uid) or {"user_id": uid, "name": "", "created": now(), "messages": [], "fps": []}
    existing = bool(th["messages"])
    fps = set(th.get("fps") or [])
    new_in = []
    for m in msgs or []:
        d = m.get("dir")
        tx = (m.get("tx") or "").strip()[:300]
        if d not in ("in", "out") or not tx:
            continue
        fp = _dm_fp(d, tx)
        if fp in fps:
            continue
        fps.add(fp)
        th.setdefault("fps", []).append(fp)
        msg = {"dir": d, "text": tx, "ts": (m.get("t") or "").strip(), "seen_at": now(), "via": "sync"}
        if d == "in" and existing:
            msg["pending"] = True
            new_in.append(msg)
        th["messages"].append(msg)
    th["messages"] = th["messages"][-800:]
    th["fps"] = list(fps)[-1200:]
    if name:
        th["name"] = name
    dm_save(th)
    dm_touch_index(uid, th.get("name"))
    return th, (new_in if existing else [])

# ── CDP 執行器（同 xhs_delete 同一個瀏覽器；EVAL_RESULT 逐條解析返嚟）──
#（CDP 底層原語＋小紅書私信 JS 全部已併入抽象層 xhs_core.CDPDriver——本檔 DM 業務邏輯經 XHS.call 操作平台）

def dm_send(uid, text, via="manual", name=None):
    text = (text or "").strip()[:200]
    uid = _dm_safe(uid)
    if not uid or not text:
        return {"ok": False, "error": "缺對話 id 或文字"}
    r = XHS.call(xc.OP_DM_SEND, uid=uid, text=text)   # 抽象層：CDPDriver（他日 MCP 有私信工具則自動改路由）
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or r.get("detail") or "發送失敗（未知原因）"}
    th = dm_load(uid) or {"user_id": uid, "name": name or "", "created": now(), "messages": [], "fps": []}
    if name and not th.get("name"):
        th["name"] = name
    fp = _dm_fp("out", text)
    if fp not in (th.get("fps") or []):
        th.setdefault("fps", []).append(fp)
        th["messages"].append({"dir": "out", "text": text, "ts": time.strftime("%H:%M"), "seen_at": now(), "via": via})
    for m in th["messages"]:
        if m.get("dir") == "in":
            m["pending"] = False
    th["messages"] = th["messages"][-800:]
    th["fps"] = th["fps"][-1200:]
    dm_save(th)
    dm_touch_index(uid, th.get("name"))
    return {"ok": True, "detail": "已發送（經本地瀏覽器）並記入對話紀錄"}

def dm_check(top=None, record_log=True):
    c = dm_cfg()
    top = max(1, min(int(top or c.get("scan_top") or 8), 14))
    r1 = XHS.call(xc.OP_DM_CONV, top=top)
    if not r1.get("ok"):
        return {"ok": False, "error": r1.get("error") or "讀私信列表失敗"}
    lst = r1.get("data") or {}
    r2 = XHS.call(xc.OP_DM_READ, items=lst.get("items") or [])
    if not r2.get("ok"):
        return {"ok": False, "error": r2.get("error") or "讀私信對話失敗"}
    reads = (r2.get("data") or {}).get("reads") or []
    out = []
    for tr in reads:
        uid = _dm_safe(tr.get("uid"))
        if not uid:
            continue
        th, new_in = dm_merge(uid, tr.get("name"), tr.get("msgs"))
        pend = sum(1 for m in (th.get("messages") or []) if m.get("pending") and m.get("dir") == "in")
        out.append({"id": uid, "name": th.get("name") or uid[:8], "new": len(new_in), "pending": pend,
                    "n": len(th.get("messages") or [])})
    if record_log:
        got = [o for o in out if o["new"]]
        dm_log("更新消息：讀取 %s/%s 個会話%s" % (
            len(out), lst.get("count", "?"),
            "；新到：" + "、".join("%s(%s條)" % (g["name"], g["new"]) for g in got) if got else "——冇新私信"))
    return {"ok": True, "checked": len(out), "threads": out, "total_convs": lst.get("count", 0)}

DM_TASK = """你係 Suki 本人（人設正典附後）。小紅書私信有一位網友同你傾緊偈，而家你要覆返佢最新嗰句。
【你嘅完整人設正典（語氣同界線全部依呢度）】
%s
【對方昵稱】%s
【你哋嘅對話紀錄（[我]＝你 Suki，[對方]＝佢；最早喺上）】
%s
【任務】就住「對方」最後嗰句，輸出「一句」私信覆本體（唔好引號／唔好解釋／唔好備用版）：
- 港式粵語繁體、自然口語、6–40 字，可帶一個 emoji；指紋詞（hhhh／1 等）只喺情緒真需要時用；
- 承接成段上下文，真切回應對方講緊嘢；可自然少少講自己近況（同人設／時間線一致，今日 %s）；
- 嚴守私隱紅線：唔提學校名（含簡稱）／班別／年齡／住所／任何店名路名地區站名；唔報全名；被追問就用「屋企話唔准講啦～」「要問下媽咪先」式輕輕帶過；
- 被問「你係咪AI」保持角色；唔約見面（「要問下媽咪allow唔allow先～」）；唔碰政治敏感；唔「求關注」；
- 如果對方明顯係賣嘢／廣告客，客氣一句短覆就得。"""

def dm_ctx_text(th, c):
    msgs = (th.get("messages") or [])[-int(c.get("ctx_msgs") or 24):]
    return "\n".join(("[我] " if m.get("dir") == "out" else "[對方] ") + str(m.get("text") or "")[:200] for m in msgs)

def dm_pass(cfg=None, dry=False):
    """私信一輪：讀平台→合併入檔→只覆「白名單開住＋有新到訊息」嘅對象（配額＋生活節拍由呼叫方保證）。"""
    c = dict(DEFAULT_DM, **((cfg or eng_cfg()).get("dm") or {}))
    skipn = [k for k in re.split(r"[,，]", str(c.get("skip_names") or "")) if k]
    ds = dm_state()
    r = dm_check(top=c.get("scan_top"), record_log=False)
    if not r.get("ok"):
        dm_log("⚠ 檢查失敗：%s" % (r.get("error") or "?")[:120])
        return r
    ix = dm_index()
    stop = ENG["stop"] or threading.Event()
    sent = 0
    plans = []
    for t in r.get("threads") or []:
        uid = t["id"]
        ent = ix["threads"].get(uid) or {}
        th = dm_load(uid) or {}
        pend = [m for m in (th.get("messages") or []) if m.get("pending") and m.get("dir") == "in" and _dm_trig_ok(m.get("text"))]
        if dry:
            if ent.get("auto") and pend:
                why = "今日配額已满" if ds["sent"] >= int(c.get("daily_max") or 3) else (
                    "呢位今日已覆夠" if int((ds["threads"].get(uid) or {}).get("n") or 0) >= int(c.get("thread_daily") or 1) else "將生成並發送覆")
            elif ent.get("auto"):
                why = "冇新到訊息，唔會打擾"
            else:
                why = "未剔「自動覆」——永遠唔會自動覆"
            plans.append("%s（新%s）→%s" % (t["name"], t["pending"], why))
            continue
        if not ent.get("auto"):
            if t["pending"]:
                dm_log("⏸ %s 有新私信但未開自動覆——要喺「💬 自動私訊」剔咗先會覆（白名單制）" % t["name"])
            continue
        if any(k in t["name"] for k in skipn):
            continue
        if ds["sent"] >= int(c.get("daily_max") or 3):
            dm_log("⏸ 今日自動覆私信已達上限（%s/%s）——聽日再續" % (ds["sent"], c.get("daily_max")))
            break
        if int((ds["threads"].get(uid) or {}).get("n") or 0) >= int(c.get("thread_daily") or 1):
            dm_log("⏸ %s 今日已覆夠 %s 條——一日覆少少就好" % (t["name"], c.get("thread_daily")))
            continue
        if not pend:
            continue
        out, e2 = eng_llm(DM_TASK % (full_canon(), t["name"][:20], dm_ctx_text(th, c), time.strftime("%Y-%m-%d (%a)")))
        txt = eng_clean(out)
        hits = eng_redline(txt)
        if not txt or len(txt) < 2 or hits:
            dm_log("⚠ 俾 %s 嘅草稿唔合格（%s）——暫唔覆：%s" % (t["name"], "、".join(hits) or "空/太短", (txt or e2 or "")[:50]))
            continue
        s = dm_send(uid, txt, via="auto", name=t["name"])
        if s.get("ok"):
            ds["sent"] += 1
            e = ds["threads"].setdefault(uid, {})
            e["n"] = int(e.get("n") or 0) + 1
            dm_state_save(ds)
            sent += 1
            dm_log("✔ 自動覆咗 %s：「%s」（今日 %s/%s）" % (t["name"], txt, ds["sent"], c.get("daily_max")))
        else:
            dm_log("⚠ 覆 %s 失敗：%s" % (t["name"], (s.get("error") or "?")[:100]))
        stop.wait(random.randint(int(c.get("gap_min") or 40), int(c.get("gap_max") or 110)))
    if dry:
        dm_log("(dry) 私信計劃：" + ("；".join(plans) if plans else "冇需要覆嘅"))
        return {"ok": True, "dry": True, "plans": plans, "checked": r.get("checked", 0)}
    dm_log("📊 私信步驟完成：%s" % ("覆咗 %s 條" % sent if sent else "冇需要覆嘅／配額內已完成"))
    return {"ok": True, "sent": sent, "checked": r.get("checked", 0)}

# ────────── 一鍵自動設計（讀晒 Suki 全部設定 → active LLM 出文案 → active Comfy 出圖） ──────────
WF_TEMPLATE = os.path.join(ROOT, "tools", "wf_template.json")
BANNED_AUTO = ["呂中", "潤財", "中二", "中一", "中三", "新帳號", "13歲", "旭輝", "陳蘇琪", "SoStupid", "東華三院",
               "新账号", "13岁"]  # 簡體變體：帖子可選簡體輸出（v5），紅線照掃
TAG_BAN = ["將軍澳", "宝琳", "寶琳", "popcity", "坑口", "調景嶺", "觀塘", "順景", "新都城", "慧安", "煜街", "毓雅",
           "将军澳", "调景岭", "观塘"]  # 地區詞＝個人資料：標籤永遠唔准；正文／標題同掃（§11A.G 加嚴條 2026-11-20 深夜）

# ── 畫質正典段（自動設計／媒體庫生成队列共用——§11A.G 圖片三規則）──
FACE_BLOCK = ("13-year-old Hong Kong Chinese schoolgirl looking young for her age, round face with chubby cheeks, "
              "thick straight natural eyebrows, small almond eyes with single eyelids, low nose bridge with rounded tip, "
              "full rounded lips, warm slightly tanned skin, plain ordinary realistic girl-next-door face, NOT pretty like a model, "
              "absolutely no makeup, a tiny mole at the outer corner of her right eye, straight blunt bangs across her forehead, "
              "shoulder-length dark brown straight hair, about 150cm with a healthy slightly plump figure and long legs")
HAND_CLAUSE = "a young teenage girl's small slender hand, soft delicate fingers, short clean nails, smooth skin, no hair, NOT a male hand"
# 單手機紀律（用戶拍板 2026-11-21：Suki 只有一部手機＝前置鏡自拍；鏡自拍先可以見到電話；唔准「手持電話自拍」邏輯錯誤；iPhone 字眼唔准影響畫面）
PHONE_CLAUSE = ("she owns exactly ONE phone (the case is light-blue with an anime teal-twin-tail mascot girl illustration printed on it, pastel sticker style with tiny stars and a small heart, no readable letters on the case, personal item, brand never shown): any selfie is shot with that "
                "phone's FRONT CAMERA, so no phone device may appear in frame, except a mirror selfie where the one and only phone "
                "she holds is legitimately reflected; NEVER show her holding or looking at a second phone or any phone prop while "
                "taking a selfie; never render any brand name or logo")
GEN_STYLE_TAIL = (", the person (if any) is Suki, a 13-year-old Hong Kong Chinese schoolgirl looking young for her age; "
                  "if her face is visible it MUST match exactly: " + FACE_BLOCK +
                  "; if any hand or arm is in frame: " + HAND_CLAUSE +
                  "; " + PHONE_CLAUSE +
                  "; absolutely no readable text anywhere (no signs, menus, letters, numbers, logos, watermarks; paper must be blank); "
                  "school crest NOT visible; realistic phone front-camera quality, grain, flat auto lighting, no bokeh; "
                  "subtle vignette darkening at corners, background slightly out of focus with mild phone-lens edge softness")

# ── 正典參考卡（用戶拍板 2026-11-21：由 AGENTS.md 分類出 canon/*.md，人設檔案頁 TAB 快捷查看／編輯；每次生成同時一併提交）──
CANON_DIR = os.path.join(ROOT, "canon")
CANON_CARDS = [("basics", "基本資料"), ("family", "家庭"), ("school", "學校"), ("friends", "朋友"),
               ("interests", "喜好"), ("food", "美食"), ("travel", "旅遊"), ("posture", "出帖參考")]

def canon_cards_text():
    out = []
    for name, label in CANON_CARDS:
        t = read_text(os.path.join(CANON_DIR, name + ".md"))
        if t.strip():
            out.append("【%s.md · %s（最新操作卡——若與 AGENTS.md 重複，以此卡為準）】\n%s" % (name, label, t))
    return ("\n\n".join(out)) if out else "（canon/ 參考卡未設置）"

# ── 視頻 REF 正典（用戶拍板：視頻工作流用 MINIMAX REF2VA，將所有相關圖片影片做 REF——預設自動帶以下存在者，上限 3 張）──
DEFAULT_VIDEO_REFS = ["media/suki/reference/suki-face-ref-02-card3-final.png",
                      "media/suki/cards/card1_summer_3view_00001_.png",
                      "media/suki/cards/card2_winter_3view_00001_.png"]

def ref_files(refs, limit=6):
    """refs＝用戶指定嘅參考圖／片（相對／絕對路）→ 存在檔案嘅絕對路徑列表。"""
    lst = []
    for r in (refs or []):
        ap = resolve_media(r)
        if os.path.isfile(ap):
            lst.append(ap)
    return lst[:limit]

def video_refs(refs=None):
    """視頻 REF：用戶指定優先；空＝面部正典＋人物卡（max 3）。"""
    return ref_files(refs, 3) or ref_files(DEFAULT_VIDEO_REFS, 3)

# ── 帖子語言／語體（v5：用戶可為每帖揀 繁/簡 × 廣東話/書面語；預設＝繁＋港式粵語口語）──
LANG_REG = {("t", "cy"): "繁體中文＋港式粵語口語（人設預設）",
            ("s", "cy"): "簡體中文＋港式粵語口語（用詞照常廣東話，字形用簡體）",
            ("t", "sw"): "繁體中文＋書面語（保留 Suki 活潑語氣同少量語氣詞，主體用書面語）",
            ("s", "sw"): "簡體中文＋普通話書面語（保留 Suki 活潑語氣，主體用普通話書面語）"}

def lang_directive(post):
    if not post:
        return ""
    k = ((post.get("lang") or "t"), (post.get("register") or "cy"))
    if k == ("t", "cy"):
        return ""  # 預設＝人設既定語體，唔使extra指令
    return ("【語體設定（用戶喺帖子頁選擇，優先於上面嘅語體規則；但私隱紅線、標籤規則、口頭禪紀律照常全部適用）】"
            "本帖輸出＝" + LANG_REG.get(k, LANG_REG[("t", "cy")]))

def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

def http_json(method, url, headers, payload=None, timeout=180):
    req = urllib.request.Request(url, data=(json.dumps(payload).encode() if payload is not None else None),
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %d %s" % (e.code, ((e.read(300).decode("utf-8", "replace") if e.fp else "") or ""))
    except Exception as e:
        return None, str(e)[:250]

def call_llm(cfg, prompt, msgs=None):
    base = (cfg.get("base_url") or "").rstrip("/")
    key = cfg.get("api_key") or ""
    model = cfg.get("model") or ""
    msgs = msgs or [{"role": "user", "content": prompt}]
    if not base:
        return None, "呢個 LLM 未填 base_url"
    if cfg.get("type") == "anthropic":
        if not key:
            return None, "未填 API key → 喺右邊「LLM 管理」呢行嘅 api key 欄輸入再儲存（或者用 LM Studio／Ollama 本地 LLM）"
        if not model:
            model = "claude-sonnet-5-5"
        obj, err = http_json("POST", base + "/v1/messages",
                             {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                             {"model": model, "max_tokens": 3000, "messages": msgs})
        if err:
            return None, err
        try:
            return "".join(b.get("text", "") for b in obj["content"] if b.get("type") == "text"), None
        except Exception:
            return None, "LLM 回覆格式唔識睇：" + json.dumps(obj, ensure_ascii=False)[:200]
    else:
        hdr = {"content-type": "application/json"}
        if key:
            hdr["Authorization"] = "Bearer " + key
        body = {"max_tokens": 3000, "messages": msgs}
        if model:
            body["model"] = model
        obj, err = http_json("POST", base + "/chat/completions", hdr, body)
        if err:
            return None, err
        try:
            msg = obj["choices"][0]["message"]
            txt = (msg.get("content") or "").strip()
            if not txt:
                # 思考型模型（openrouter 部分免費 model）content 係空、內容喺 reasoning 欄——兜底攞返，再唔少就清楚報錯（用戶拍板用 openrouter，要畀到明確指示）
                txt = (msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
            if not txt:
                finish = (obj["choices"][0].get("finish_reason") or "")
                return None, ("模型回覆空白（finish_reason=%s）——常見原因：該 model 唔支援咁長嘅人設 prompt（約 6 萬字）、或免費端點將內容放咗喺思考欄。請換返 LM Studio（本地已實測成功）或 openrouter 一個 ≥64k context 嘅正式 model。" % (finish or "unknown"))
            return txt, None
        except Exception:
            return None, "LLM 回覆格式唔識睇：" + json.dumps(obj, ensure_ascii=False)[:200]

def extract_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        for cand in (t[i:j + 1],):
            try:
                return json.loads(cand)
            except Exception:
                try:
                    return json.loads(re.sub(r",\s*([}\]])", r"\1", cand))  # 尾逗號容錯
                except Exception:
                    pass
    return None

AUTO_TASK = """你係 Suki（當真人對待嘅虛構香港女中學生，小紅書帳號用嚟網上交朋友）嘅文案＋攝影助理。
下面係佢嘅全部設定正典 AGENTS.md、發帖日記 diary.md、以及現有帖檔。請設計「下一帖」小紅書帖，並只輸出一個 JSON 物件（不要任何其他文字、不要 markdown 圍欄）：
{"title":"…","content":"…（換行用 \\n）","tags":["…","…"],"image_prompt":"…","reason":"點解揀呢個主題、點樣承接日記（一句繁體中文）"}
硬性規則（違反任何一條＝不合格）：
1. 承接性：由 diary.md「帖文時間線約束」「呼應鉤子」＋AGENTS.md §15 揀今日合理自然嘅主題；今日係 %s；**先對 AGENTS.md §2A 學校校曆定出今日嘅學期狀態（考試週／等成績／假期／活動日／正常上课週），主題必須與之相符（§11A.H 日期適配，用戶拍板 2026-11-21）——寫錯節拍＝不合格**；講過嘅嘢唔可以矛盾；如有【用戶主題提示】就依住佢。
2. §11A 發帖手冊：標題 ≤20 字（連 emoji，吸 clicks，可含可搜詞但不碰紅線）；正文＝港式粵語**繁體**、多 emoji、「hhhh／1／救命／好嘛」呢類**指紋詞照常自然使用（用戶 2026-11-21 下午拍板「回復 hhhh 等指紋」——唔好為避开而完全唔用）**：放喺情緒真係需要嘅位置最生動，為用而用、每句都塞先至要避免；重點放前 300 字；**長度同結尾要多元化、跟情緒嚟（用戶拍板 2026-11-20 夜：唔好一直用問題作結尾）**——开心／嬲／眼瞓嘅語氣決定長短，短則 2–3 行、長都唔好過 7 行，唔好每篇同一個 template；結尾可以用自語式（「下次一定」「好嘛」）、嘴硬式、動作式（「我去沖茶先」）、分享式，偶爾才邀評——**最多三分之一嘅帖以問題結尾**；標籤 5–7 個全部繁體＝2 個大流量（中學生日常／學生黨呢類）＋最多 1 個城市級（香港）＋3–4 個身份／場景詞；**標籤同正文都唔准出現任何店名／商場名／路名／站名／地區名**（「PopCity」「將軍澳」「寶琳站」等一律唔准——用戶拍板；見紅線第 3 條）。
3. §11A.G 私隱紅線（優先於一切；用戶拍板 2026-11-20 夜・加嚴）：絕對唔可以出現學校名（包括「呂中」）、班別、年齡、年級排名、屋苑名、上課時間表、「新帳號」類宣告、佢嘅中文名；**任何店名／商場名／路名／地鐵站名／地區名一律唔准出現在標題、正文、標籤——「PopCity」「將軍澳」「寶琳站」「寶盈天地」等全部唔准**；地點最多寫到「香港」城市級；場景用通用講法帶過（「放學順路買杯凍奶茶」「書桌前」「窗邊」「樓下」「站外」等），靠生活細節同語氣建立身份，唔靠名字。
4. image_prompt（英文，俾所選圖片工作流出圖）：**單手機紀律（用戶拍板 2026-11-21）：Suki 得一部手機（淺藍 Miku 插畫殼 iPhone）——自拍一定用呢部手機嘅前置鏡頭，所以畫面唔可以見到電話，鏡子自拍除外（鏡中只准得返呢一部）；絕對唔准畫出「手持電話 selfie」或者第二部手機；唔准出現任何品牌名／logo；需要時逐字帶 "she owns exactly ONE phone (the case is light-blue with an anime teal-twin-tail mascot girl illustration printed on it, pastel sticker style with tiny stars and a small heart, no readable letters on the case, personal item, brand never shown): any selfie is shot with that phone's FRONT CAMERA, so no phone device may appear in frame, except a mirror selfie where the one and only phone she holds is legitimately reflected; never render any brand name or logo"——唔好因為 iPhone 字眼畫出廣告感電話產品圖**。構圖選擇：**全域已無任何「不能露面」限制（用戶 2026-11-20 深夜最終拍板：移除全域限制）——露臉與無臉同等合法、無默認傾向，純按內容情緒揀構圖**：(a) 若露臉：逐字嵌入 FACE BLOCK「13-year-old Hong Kong Chinese schoolgirl looking young for her age, round face with chubby cheeks, thick straight natural eyebrows, small almond eyes with single eyelids, low nose bridge with rounded tip, full rounded lips, warm slightly tanned skin, plain ordinary realistic girl-next-door face, NOT pretty like a model, absolutely no makeup, a tiny mole at the outer corner of her right eye, straight blunt bangs across her forehead, shoulder-length dark brown straight hair, about 150cm with a healthy slightly plump figure and long legs」＋校服照 §8 UNIFORM BLOCK＋**校徽絕對唔准可見**（用書包 strap／側身角度遮去，prompt 加 "school crest NOT visible, hidden by backpack strap or angle"）＋"realistic phone front-camera selfie quality, grain, flat auto lighting, no bokeh, slight wide-angle distortion"；(b) 若揀無臉構圖（POV 物件／手部特寫＝同等合法嘅選擇之一，唔係預設）：逐字帶 "POV shot, no face, no people in frame, realistic phone front-camera quality, grain, flat auto lighting"；**畫面入面如果有手／手臂，必須係女仔嘅手**——逐字帶住 "a young teenage girl's small slender hand, soft delicate feminine fingers, short clean nails, smooth skin, no hair, NOT a male hand"（佢係 13 歲女中學生， masculine 手＝出錯，用戶拍板）；結尾必須逐字加 "subtle vignette darkening at corners, background slightly out of focus with mild phone-lens edge softness, only the held object and hand sharp"；**畫面任何位置都唔准出現可閱讀文字**——店招、菜單、paper 上嘅字、字母、數字、logo、校徽、水印、字幕全部唔准（用戶拍板：圖片背景有文字＝唔合格；需要紙品時寫 "blank paper, no visible text or markings on paper"）；唔靚唔修圖、生活 snapshot 感。
5. 你嘅修改／設計一律以下面提交嘅**全本人設正典**為唯一依據（用戶要求：每次修改和設計文案都要將 Suki 整個人設提交）——語氣用 §7 性格、用詞指紋用 §6／§11、场景合理依 §9／§10、承接 §15＋diary。
6. 承接只認**已發布帖**＋diary 時間線（用戶拍板 2026-11-21：未發布嘅草稿唔入參考範圍）；人設以 AGENTS.md＋canon/ 參考卡為唯一依據（卡與 AGENTS 重複時以卡為準——卡係最新操作狀態）。
--- 以下是全部設定 ---
【AGENTS.md＋canon 正典參考卡】
%s
【posts/diary.md】
%s
【帖檔摘要——只列已發布帖（草稿不在參考範圍）】
%s
【用戶主題提示】%s
"""

# ── 媒體庫（用戶 2026-11-20 深夜：媒體庫欄可刪除；刪了＝之後不再納入自動設計參考）──
TRASH = os.path.join(ROOT, "media", ".trash")
MEDIA_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".webm")

def deleted_index():
    try:
        with open(os.path.join(TRASH, "index.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def apply_media_deletions(text):
    """把提及已刪除媒體檔案的行（例如 §14 索引行）從提交畀 LLM 嘅正典中剔除。"""
    names = [d["orig"].split("/")[-1] for d in deleted_index()][-200:]
    if not names:
        return text
    keep = [ln for ln in text.split("\n") if not any(n in ln for n in names)]
    return "\n".join(keep)

def scan_media():
    import glob as _glob
    out = []
    for fp in sorted(sum((_glob.glob(os.path.join(ROOT, "media", "**", "*" + e), recursive=True) for e in MEDIA_EXT), [])):
        rel = os.path.relpath(fp, ROOT).replace("\\", "/")
        if "/.trash/" in "/" + rel:
            continue
        try:
            st = os.stat(fp)
        except OSError:
            continue
        out.append({"rel": rel, "name": os.path.basename(fp), "size": st.st_size, "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out

def media_delete(rel):
    rel = (rel or "").replace("\\", "/").strip("/")
    src = os.path.join(ROOT, rel.replace("/", os.sep))
    if not rel.startswith("media/") or not os.path.isfile(src):
        return False, "檔案不存在或不在 media/ 內：" + rel
    os.makedirs(TRASH, exist_ok=True)
    tn = time.strftime("%Y%m%d-%H%M%S") + "__" + rel.replace("/", "_")
    try:
        shutil.move(src, os.path.join(TRASH, tn))
    except Exception as e:
        return False, "搬移失敗：" + str(e)[:120]
    idx = deleted_index()
    idx.append({"orig": rel, "trash": tn, "ts": now()})
    with open(os.path.join(TRASH, "index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    autolog("media deleted → .trash（不再納入參考）: " + rel)
    return True, "已移入 media/.trash（可還原）。自動設計／人設檔案／提示詞已即時停止參考此檔。"

def media_restore(trash_name):
    idx = deleted_index()
    for d in idx:
        if d.get("trash") == trash_name:
            src = os.path.join(TRASH, trash_name)
            dst = os.path.join(ROOT, d["orig"].replace("/", os.sep))
            if not os.path.isfile(src):
                return False, "回收區內找不到 " + trash_name
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            idx.remove(d)
            with open(os.path.join(TRASH, "index.json"), "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False, indent=1)
            autolog("media restored: " + d["orig"])
            return True, "已還原：" + d["orig"]
    return False, "回收紀錄不存在"

CANON_FILES = {"agents": os.path.join(ROOT, "AGENTS.md"), "diary": os.path.join(ROOT, "posts", "diary.md")}
for _n, _l in CANON_CARDS:  # 正典參考卡（canon/*.md）都可喺人設檔案頁編輯保存
    CANON_FILES[_n] = os.path.join(CANON_DIR, _n + ".md")

def canon_save(target, text):
    path = CANON_FILES.get(target)
    if not path:
        return False, "target 未認識：" + str(target)
    if target not in ("agents", "diary"):
        os.makedirs(CANON_DIR, exist_ok=True)
    if text is None or not str(text).strip():
        return False, "內容為空，拒絕保存"
    if target == "agents" and len(text) < 5000:
        return False, "AGENTS.md 過短（<5000 字），疑似誤清空，拒絕保存"
    bdir = os.path.join(os.path.dirname(__file__), "backup")
    os.makedirs(bdir, exist_ok=True)
    try:
        shutil.copy2(path, os.path.join(bdir, os.path.basename(path) + "." + time.strftime("%Y%m%d-%H%M%S") + ".bak"))
    except Exception:
        pass
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    autolog("canon saved via WebUI: " + target + "（舊版已備份 post-studio/backup/）")
    return True, "已寫回 " + os.path.relpath(path, ROOT).replace("\\", "/") + "（舊版已備份到 post-studio/backup/）"

def published_summary(posts, fields=("id", "status", "title", "published_at")):
    """帖檔摘要＝只列已發布帖（用戶拍板 2026-11-21：草稿唔入參考範圍）。"""
    return json.dumps([{k: p.get(k) for k in fields} for p in posts if p.get("status") == "published"], ensure_ascii=False)

def full_canon():
    """全本人設正典＝AGENTS.md＋canon/*.md 參考卡（卡為最新操作狀態，與 AGENTS 重複時以卡為準）。"""
    return apply_media_deletions(read_text(os.path.join(ROOT, "AGENTS.md"))) + "\n\n" + canon_cards_text()

def build_auto_prompt(hint, posts):
    canon = full_canon()
    diary = apply_media_deletions(read_text(os.path.join(ROOT, "posts", "diary.md")))
    summary = published_summary(posts, ("id", "status", "title", "content", "tags", "published_at"))
    return AUTO_TASK % (time.strftime("%Y-%m-%d %a"), canon, diary, summary, hint or "（無——請依住日記時間線自行揀啱時宜嘅主題）")

def build_refine_prompt(post, fb_texts, posts):
    canon = full_canon()
    diary = apply_media_deletions(read_text(os.path.join(ROOT, "posts", "diary.md")))
    cur = json.dumps({k: post.get(k) for k in ("id", "status", "title", "content", "tags", "images", "note")}, ensure_ascii=False)
    return ("""你係 Suki（當真人對待嘅虛構香港女中學生）嘅文案＋攝影助理。下面係佢嘅全本人設正典 AGENTS.md＋發帖日記。
你而家要**跟用戶嘅修改指示，修改帖 #%s**（保持承接日记時間線，其餘規則一條不變）。
規則（與新創作完全相同，硬性）：
1. 標題 ≤20 字；正文港式粵語繁體、多 emoji，「hhhh／1」等**指紋詞自然使用（用戶 2026-11-21 拍板「回復 hhhh 等指紋」——唔使刻意回避；只避免無情緒需要嘅機械式填充）**；**長度同結尾跟情緒來、要多元化——唔好一直用問題作結尾**（自語／嘴硬／動作／分享式結尾為主的，邀評只間中用）；標籤 5–7 個繁體——**標籤唔准出現地區詞**（將軍澳／寶琳／PopCity 等＝個人資料，用戶拍板）。
2. §11A.G 私隱紅線（加嚴）：唔准學校（含「呂中」）／班別／年齡／屋苑／時間表／「新帳號」；**店名／商場名／路名／站名／地區名一律唔准出現（「PopCity」「將軍澳」等都不行）**——正文改用通用講法（「放學順路」「書桌前」「樓下」），地點最多「香港」級。
3. image_prompt（英文，必需重新出，即使指示只關文字）：**單手機紀律（用戶拍板 2026-11-21）：Suki 得一部手機（淺藍 Miku 插畫殼 iPhone），自拍一定用佢嘅前置鏡頭→畫面唔准見到任何電話（鏡子自拍除外，鏡中只准一部）；唔准「手持電話自拍」或者第二部手機；唔准品牌名／logo；逐字帶 "she owns exactly ONE phone (the case is light-blue with an anime teal-twin-tail mascot girl illustration printed on it, pastel sticker style with tiny stars and a small heart, no readable letters on the case, personal item, brand never shown): any selfie is shot with that phone's FRONT CAMERA, so no phone device may appear in frame, except a mirror selfie where the one and only phone she holds is legitimately reflected; never render any brand name or logo"**。**全域無「不能露面」限制（已彻底移除）——露臉／無臉同等合法、無默認傾向**，跟指示／情緒決定；若露臉→逐字嵌入 FACE BLOCK（見 AGENTS.md §8.6 全文）＋§8 校服＋"school crest NOT visible"＋"realistic phone front-camera selfie quality, grain, flat auto lighting, no bokeh"；若無臉→同新文案場景匹配嘅 POV 物件／手特寫，含 "POV shot, no face, no people in frame, realistic phone front-camera quality, grain, flat auto lighting"；有手就必須逐字帶 "a young teenage girl's small slender hand, soft delicate feminine fingers, short clean nails, smooth skin, no hair, NOT a male hand"（用戶指出舊圖隻手似男仔，唔适配 Suki——呢條最優先修正）；結尾逐字加 "subtle vignette darkening at corners, background slightly out of focus with mild phone-lens edge softness, only the held object and hand sharp"；**畫面任何位置唔准有可閱讀文字**（店招／菜單／紙上字／字母／數字／logo／校徽／水印全部唔准；紙品寫 "blank paper, no visible text"）。
4. 用戶嘅修改指示係最高指示，但唔可以打破上面規則。
只輸出一個 JSON（無其他文字、無 markdown）：
{"title":"…","content":"…（換行用 \\n）","tags":["…"],"image_prompt":"…","reason":"改了咩、點樣跟住指示（一句繁體）"}
【用戶修改指示】
%s
【呢帖現時內容】
%s
--- 全本人設正典 ---
【AGENTS.md】
%s
【posts/diary.md】
%s
【今日】%s（改稿前先對 AGENTS.md §2A 校曆確認學期狀態——日期適配 §11A.H：情境、時令、考試節拍都要同當日一致）
""" % (post.get("id"), "\n".join("- " + t for t in fb_texts), cur, canon, diary, time.strftime("%Y-%m-%d %a")))

def save_and_cover(data, label):
    """落原圖 → PIL 出 3:4 封面（中心裁切＋背景微糊＋淡暈影，§11A.G）。回 (png, cover)"""
    folder = os.path.join(ROOT, "media", "suki", "life")
    os.makedirs(folder, exist_ok=True)
    png = os.path.join(folder, "life-%s-auto-%s.png" % (time.strftime("%Y-%m-%d"), label))
    with open(png, "wb") as f:
        f.write(data)
    cover = png[:-4] + "-cover-3x4.jpg"
    try:
        from PIL import Image, ImageDraw, ImageFilter
        im = Image.open(png).convert("RGB")
        w, h = im.size
        tw, th = (w, int(round(w * 4 / 3))) if h >= w * 4 / 3 else (int(round(h * 3 / 4)), h)
        l, t = (w - tw) // 2, (h - th) // 2
        im = im.crop((l, t, l + tw, t + th)).resize((1080, 1440), Image.LANCZOS)
        blurred = im.filter(ImageFilter.GaussianBlur(4))
        sharp = Image.new("L", (1080, 1440), 0)
        ImageDraw.Draw(sharp).ellipse((-220, -300, 1300, 1740), fill=255)
        sharp = sharp.filter(ImageFilter.GaussianBlur(160))
        out = Image.composite(im, blurred, sharp)
        dark = Image.eval(out, lambda x: int(x * 0.86))
        v = Image.new("L", (1080, 1440), 0)
        ImageDraw.Draw(v).ellipse((-120, -160, 1200, 1600), fill=255)
        v = v.filter(ImageFilter.GaussianBlur(200))
        out = Image.composite(out, dark, v)
        out.save(cover, "JPEG", quality=90)
    except Exception as e:
        return png, None, "封面後處理失敗：%s" % e
    return png, cover, ""

def resolve_media(p):
    """統一解析帖內圖片路徑：絕對路徑直接用；相對（例 media/...）以項目 ROOT 為基準（fix：舊版用 CWD，出帖頁唔顯示圖）。"""
    p = (p or "").strip().replace("\\", "/")
    return os.path.normpath(p if os.path.isabs(p) or ":" in p[:2] else os.path.join(ROOT, p))

def make_cover(path):
    """為任意媒體圖片出 3:4 封面版（§14 流程；已存在直接用，失敗回原圖＋錯誤）。回 (cover_abs, err)。"""
    if re.search(r"-cover-3x4\.(jpg|jpeg|png|webp)$", path, re.I):
        return path, ""  # 已經係封面版：唔好再出多層（fix：-cover-3x4-cover-3x4 bug）
    base = re.sub(r"\.(jpg|jpeg|png|webp)$", "", path, flags=re.I)
    cover = base + "-cover-3x4.jpg"
    if os.path.isfile(cover):
        return cover, ""
    try:
        from PIL import Image, ImageDraw, ImageFilter
        im = Image.open(path).convert("RGB")
        w, h = im.size
        tw, th = (w, int(round(w * 4 / 3))) if h >= w * 4 / 3 else (int(round(h * 3 / 4)), h)
        l, t = (w - tw) // 2, (h - th) // 2
        im = im.crop((l, t, l + tw, t + th)).resize((1080, 1440), Image.LANCZOS)
        blurred = im.filter(ImageFilter.GaussianBlur(4))
        sharp = Image.new("L", (1080, 1440), 0)
        ImageDraw.Draw(sharp).ellipse((-220, -300, 1300, 1740), fill=255)
        sharp = sharp.filter(ImageFilter.GaussianBlur(160))
        out = Image.composite(im, blurred, sharp)
        dark = Image.eval(out, lambda x: int(x * 0.86))
        v = Image.new("L", (1080, 1440), 0)
        ImageDraw.Draw(v).ellipse((-120, -160, 1200, 1600), fill=255)
        v = v.filter(ImageFilter.GaussianBlur(200))
        out = Image.composite(out, dark, v)
        out.save(cover, "JPEG", quality=90)
        return cover, ""
    except Exception as e:
        return path, str(e)[:120]

IMAGEPOST_TASK = """你係 Suki（當真人對待嘅虛構香港女中學生）嘅文案助理。佢而家有一張嘢（已經存在嘅圖），想以呢張圖為首圖發一篇小紅書帖。
【呢張圖】檔案：%s
【正典入面對呢張圖嘅描述（如有）】%s
【用戶提示（最高優先：圖嘅場景＋想表達嘅情緒，文案必須體現。例：「夾公仔相，傷心」→ 就寫夾唔到公仔嗰種不捨得／好失敗嘅細細樣心情）】%s
請只輸出一個 JSON 物件（無其他文字、無 markdown）：
{"title":"…","content":"…（換行用 \\n）","tags":["…"],"reason":"點解咁寫、對應提示入面邊個情緒（一句繁體）"}
硬性規則（與新創作相同，違反＝不合格）：
1. 今日 %s；**先對 AGENTS.md §2A 校曆定出學期狀態，文案節拍必須與之一致（§11A.H 日期適配）**；主題承接 diary.md／§15，唔可以同講過嘅嘢矛盾。
2. 標題 ≤20 字繁體吸引；正文港式粵語**繁體**；「hhhh／1」等**指紋詞自然使用（用戶 2026-11-21 拍板「回復 hhhh 等指紋」；避免嘅只係無意義機械填充）**；長度跟情緒（傷心／悶可以好短、兩三行都得）；**結尾多元化，呢篇首選唔好問題結尾**（用情緒／動作／自語收結，除非提示明係想互動）；標籤 5–7 個繁體＝2 大流量＋最多 1 個「香港」＋身份／場景詞。
3. §11A.G 私隱紅線：唔准學校（含「呂中」）／班別／年齡／屋苑／「新帳號」；**任何店名／商場名／路名／站名／地區名唔准出現在標題、正文、標籤**；場景用通用講法。
4. 圖已經存在，**唔使生成新圖，唔好輸出 image_prompt**；文案唔好描述圖入面嘅文字或水印（規則上圖入面冇可閱讀文字）。
5. 一切以全本人設正典為唯一依據（語氣 §7、指紋 §6／§11、行爲合理 §9／§10）。
--- 全本人設正典 ---
【AGENTS.md＋canon 正典參考卡】
%s
【posts/diary.md】
%s
【帖檔摘要——只列已發布帖（草稿不在參考範圍）】
%s
"""

def build_imagepost_prompt(img_rel, desc, hint, posts):
    canon = full_canon()
    diary = apply_media_deletions(read_text(os.path.join(ROOT, "posts", "diary.md")))
    summary = published_summary(posts)
    return IMAGEPOST_TASK % (img_rel, desc or "（無——以檔名同用戶提示為準）", hint or "（無提示——自行由圖推合理情緒，但仍要貼人設）",
                             time.strftime("%Y-%m-%d %a"), canon, diary, summary)

IMG_TASK = """你係 Suki（當真人對待嘅虛構香港女中學生）嘅攝影師＋prompt 工程師。佢篇帖已寫好，而家要為呢篇帖生成一張{%s}（圖片或 5 秒視頻）。
只輸出一個 JSON 物件（無其他文字、無 markdown）：{"image_prompt":"英文 prompt","reason":"一句繁體中文解釋點樣配合正文"}
【今日】%s（情境要同日期同 §2A 校曆一致）
【帖標題】%s
【帖正文】
%s
【用戶場景提示（最高優先——例如「用呢張圖嘅場景但換做 Suki 張面」「同閨蜜合照」「書枱俯視」）】%s
【用戶選咗嘅參考】%s
硬性規則：
1. prompt 必須同正文情境一致（正文講奶茶唔好畫書枱），並體現用戶場景提示；若係視頻：單一動作、約 5 秒、鏡頭固定、人物動作自然細碎。
2. 畫質紀律逐字帶入 prompt——(a) 露臉→FACE BLOCK 逐字「%s」＋校服依 §8＋"school crest NOT visible, hidden by backpack strap or angle"；(b) 有手／手臂→逐字「%s」；(c) 單手機紀律逐字「%s」；(d) 畫面任何位置唔准有可閱讀文字（紙品 "blank paper, no visible text or markings"）；(e) 結尾逐字 "subtle vignette darkening at corners, background slightly out of focus with mild phone-lens edge softness, only the held object and hand sharp"；(f) realistic phone front-camera quality, grain, flat auto lighting, no bokeh——唔靚唔修圖、生活 snapshot 感。
3. 若用戶指定咗參考圖（換面／保留場景改人物／合照類）：prompt 寫明保留參考嘅構圖邏輯同講明改動（例 "keep the composition and background of the reference image, the girl in frame is Suki herself"）；身份由系統注入嘅 REF 圖負責，唔使描述參考圖細節。
4. 唔准出現店名／商场名／路名／站名／地區名／學校名（畫面亦唔准有可閱讀文字）。
5. 一切以全本人設正典為唯一依據。
--- 全本人設正典（AGENTS.md＋canon 參考卡）---
%s
【已發布帖摘要（草稿唔入參考）】
%s
"""

def refcapable_image_wf_path():
    """喺工作流登記表搵一個「有 LoadImage＝食參考圖」嘅圖片工作流。
    用戶帶住參考圖而 active 工作流（如 Z-Image）唔食圖時，comfy_generate 自動轉用呢個——唔再淨係報錯。"""
    try:
        s = load_settings()
    except Exception:
        return ""
    for w in s.get("workflows", []):
        if w.get("type") != "image":
            continue
        p = (w.get("path") or "").strip()
        if not p:
            continue
        ap = p if os.path.isabs(p) else os.path.join(ROOT, p)
        if not os.path.isfile(ap):
            continue
        try:
            with open(ap, encoding="utf-8") as f:
                g = json.load(f)
        except Exception:
            continue
        if any(isinstance(nd, dict) and nd.get("class_type") == "LoadImage" for nd in g.values()):
            return ap
    return ""


def comfy_generate(url, image_prompt, label, kind="圖", with_cover=True, wf_path="", refs=None):
    """with_cover=True＝出帖流水線（原圖＋3:4 封面）；False＝媒體庫生成隊列（只存原圖，出帖時先至 make_cover）
    wf_path＝所選圖片工作流 JSON（工作流管理；空＝內置 WF_TEMPLATE）。通用 patch：prompt／seed／前綴。
    refs＝用戶選嘅參考圖（換面／合照等，完全照用戶所選順序，唔補默認——用戶拍板 2026-11-21）——
    需要工作流「有 LoadImage 參考輸入」；若所選冇（如 Z-Image 純文字），自動轉用已登記嘅食圖工作流
    （Qwen-Image 2.1 Edit 優先＝面相跟參考人物；H3 Image 後備＝面相近似）並提示。"""
    url = (url or "").rstrip("/")
    if not image_prompt or len(image_prompt) < 60:
        return {"ok": False, "error": "image_prompt 太短／空"}
    wf_path = wf_path or WF_TEMPLATE
    progress(kind, "叫 ComfyUI 出圖", "送圖紙作業（模型首次載入會耐啲）")
    try:
        with open(wf_path, encoding="utf-8") as f:
            wf = json.load(f)
    except Exception as e:
        return {"ok": False, "error": "讀唔到工作流 JSON：%s" % e}
    if patch_wf(wf, image_prompt, label) == 0:
        return {"ok": False, "error": "呢個工作流搵唔到提示詞欄（需要節點 input 名叫 prompt／text／positive）"}
    if refs:
        files = ref_files(refs)
        if not files:
            return {"ok": False, "error": "參考圖檔案搵唔到：" + ", ".join(str(x)[:40] for x in list(refs)[:3])}
        li = sorted([nid for nid, nd in wf.items() if isinstance(nd, dict) and nd.get("class_type") == "LoadImage"],
                    key=lambda x: (len(x), x))
        if not li:
            alt = refcapable_image_wf_path()
            if alt and os.path.abspath(alt) != os.path.abspath(wf_path or ""):
                wfn = None
                try:
                    with open(alt, encoding="utf-8") as f:
                        wfn = json.load(f)
                except Exception:
                    wfn = None
                if wfn and patch_wf(wfn, image_prompt, label):
                    autolog("gen | refwf auto-switch：active 圖片工作流唔食參考圖 → 自動轉用 %s" % os.path.basename(alt))
                    progress(kind, "叫 ComfyUI 出圖", "所選圖片工作流唔食參考圖——已自動轉用參考圖工作流（Qwen-Image 2.1 Edit 優先，面相跟參考人物；仍需校對）")
                    wf, wf_path = wfn, alt
                    li = sorted([nid for nid, nd in wf.items() if isinstance(nd, dict) and nd.get("class_type") == "LoadImage"],
                                key=lambda x: (len(x), x))
        if not li:
            return {"ok": False, "error": "冇任何已登記嘅圖片工作流支援參考圖——請喺「🔀 工作流」添加「Qwen-Image 2.1 Edit 參考圖」（2026-11-21 實測：食參考圖、面相跟參考人物）或「H3 Image 參考編輯」（面相只係近似），或者唔帶參考直接生成"}
        try:
            names = [comfy_upload(url, ap) for ap in files]
        except Exception as e:
            return {"ok": False, "error": "參考圖上傳失敗：%s" % str(e)[:120]}
        for i, nid in enumerate(li):
            if i < len(names):
                wf[nid]["inputs"]["image"] = names[i]
            else:
                del wf[nid]
                for nd in wf.values():
                    inp = nd.get("inputs") if isinstance(nd, dict) else None
                    if isinstance(inp, dict):
                        for k, v in list(inp.items()):
                            if isinstance(v, list) and len(v) == 2 and v[0] == nid:
                                del inp[k]
        # 編輯條件化節點：將參考圖角色講清楚——第一張＝人物本身，其餘只 guide 服裝/場景，唔准引入第二張面
        # （用戶 2026-11-21 拍板：呢段身份锁定指令屬 SUKI 生成設定一部分，所有帶參考路徑自動附加）
        for nd in wf.values():
            if not isinstance(nd, dict):
                continue
            ct = nd.get("class_type")
            if ct in ("MiniMaxH3ImageConditioning", "PH3ImageConditioning"):
                nd["inputs"]["prompt"] = str(nd["inputs"].get("prompt", "")) + (
                    " The girl in the result MUST be the same person as in <Picture 1>: keep her exact face, "
                    "hairstyle, hair color and skin tone. Any additional pictures guide clothing or setting only; "
                    "never borrow another face from them.")
            elif ct == "TextEncodeQwenImage21":
                nd["inputs"]["prompt"] = str(nd["inputs"].get("prompt", "")) + (
                    " If a person appears in <image1>, keep the exact same face and identity as that person — "
                    "same eyes, nose shape, lips, unchanged. Any additional pictures guide clothing or setting "
                    "only; never borrow another face from them.")
                neg = str(nd["inputs"].get("negative_prompt", ""))
                for t in ("different person", "changed face", "changed identity"):
                    if t not in neg:
                        neg = (neg + ", " if neg else "") + t
                nd["inputs"]["negative_prompt"] = neg
        progress(kind, "叫 ComfyUI 出圖", "帶參考：%s（%d 張注入工作流）" % (",".join(os.path.basename(x) for x in files), len(files)))
    obj, err = http_json("POST", url + "/prompt", {"content-type": "application/json"}, {"prompt": wf}, timeout=30)
    if err:
        return {"ok": False, "error": "ComfyUI 交作業失敗：" + err}
    qid = obj.get("prompt_id")
    if not qid:
        return {"ok": False, "error": "ComfyUI 冇回 prompt_id"}
    deadline = time.time() + 600  # 首次要載入模型會超過 5 分鐘
    while time.time() < deadline:
        time.sleep(3)
        try:
            with urllib.request.urlopen(url + "/history/" + qid, timeout=10) as r:
                hist = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            continue
        if qid in hist:
            entry = hist[qid]
            if (entry.get("status") or {}).get("status_str") == "error":
                return {"ok": False, "error": "ComfyUI 生成失敗（見 Comfy 控制台）"}
            for out in (entry.get("outputs") or {}).values():
                for img in out.get("images", []):
                    qs = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                                                 "type": img.get("type", "output")})
                    try:
                        req = urllib.request.Request(url + "/view?" + qs)
                        with urllib.request.urlopen(req, timeout=90) as r:
                            data = r.read()
                    except Exception as e:
                        return {"ok": False, "error": "攞圖失敗：%s" % e}
                    if not with_cover:
                        folder = os.path.join(ROOT, "media", "suki", "life")
                        os.makedirs(folder, exist_ok=True)
                        png = os.path.join(folder, "life-%s-gen-%s.png" % (time.strftime("%Y-%m-%d"), label))
                        with open(png, "wb") as f:
                            f.write(data)
                        progress(kind, "已完成：原圖已存入媒體庫")
                        return {"ok": True, "png": png}
                    progress(kind, "後處理：3:4 封面＋淡暈影＋背景微糊")
                    png, cover, perr = save_and_cover(data, label)
                    if not cover:
                        return {"ok": False, "error": perr or "封面生成失敗", "png": png}
                    return {"ok": True, "png": png, "cover": cover}
    return {"ok": False, "error": "生成逾時（600 秒）"}

def comfy_video(url, prompt, label, wf_path, refs=None):
    """視頻生成：用「工作流管理」所選嘅視頻工作流 JSON。
    Minimax H3 ref2va（用戶拍板 2026-11-21）：圖入面有 MiniMaxH3ReferenceToVideo＋LoadImage 節點時，
    自動將 REF 圖上傳到 Comfy input 再逐張注入（refs＝指定檔案；空＝面部正典＋人物卡）。
    其他工作流走通用 patch（prompt／seed／前綴）。"""
    url = (url or "").rstrip("/")
    try:
        with open(wf_path, encoding="utf-8") as f:
            wf = json.load(f)
    except Exception as e:
        return {"ok": False, "error": "讀唔到視頻工作流 JSON：%s" % str(e)[:100]}
    if "prompt" in wf and isinstance(wf["prompt"], dict) and "class_type" in wf["prompt"]:
        wf = wf["prompt"]  # 兼容 ComfyUI 完整 API 格式（外層包 prompt）
    hit = patch_wf(wf, prompt, label)
    # H3 ref2va：注入 REF 圖
    li_nodes = sorted([nid for nid, nd in wf.items() if isinstance(nd, dict) and nd.get("class_type") == "LoadImage"],
                      key=lambda x: (len(x), x))
    if li_nodes:
        files = video_refs(refs)
        if not files:
            return {"ok": False, "error": "冇可用 REF 圖（面部正典檔案搵唔到，亦冇指定 refs）"}
        try:
            names = [comfy_upload(url, ap) for ap in files]
        except Exception as e:
            return {"ok": False, "error": "REF 上傳失敗：%s" % str(e)[:120]}
        for i, nid in enumerate(li_nodes):
            if i < len(names):
                wf[nid]["inputs"]["image"] = names[i]
            else:  # 多餘 LoadImage 節點：移除並拆走連線
                del wf[nid]
                for nd in wf.values():
                    inp = nd.get("inputs") if isinstance(nd, dict) else None
                    if isinstance(inp, dict):
                        for k, v in list(inp.items()):
                            if isinstance(v, list) and len(v) == 2 and v[0] == nid:
                                del inp[k]
        hit = max(hit, 1)
        progress("視頻", "叫 ComfyUI 出視頻（REF2VA）", "REF：%s（%d 張）排入 Comfy 隊列（H3 模型第一次載入好耐，可能 10–30 分鐘）" % (",".join(os.path.basename(x) for x in files), len(files)))
    if not hit:
        return {"ok": False, "error": "視頻工作流搵唔到可替換嘅提示詞欄位（需要節點 input 名叫 prompt／text／positive）"}
    if not li_nodes:
        progress("視頻", "叫 ComfyUI 出視頻", "排入 Comfy 隊列（影片生成較慢，請等待）")
    obj, err = http_json("POST", url + "/prompt", {"content-type": "application/json"}, {"prompt": wf}, timeout=30)
    if err:
        return {"ok": False, "error": "ComfyUI 交作業失敗：" + err}
    qid = obj.get("prompt_id")
    if not qid:
        return {"ok": False, "error": "ComfyUI 冇回 prompt_id"}
    deadline = time.time() + 1800  # 視頻模型普遍比圖片慢（H3 首次載入更久）
    while time.time() < deadline:
        time.sleep(4)
        try:
            with urllib.request.urlopen(url + "/history/" + qid, timeout=10) as r:
                hist = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            continue
        if qid in hist:
            entry = hist[qid]
            st = entry.get("status") or {}
            if st.get("status_str") == "error":
                return {"ok": False, "error": "ComfyUI 視頻生成失敗（見 Comfy 控制台）"}
            if not st.get("completed"):
                continue  # ComfyUI 一開始執行就會建 history entry（outputs 仲空）——未完唔好當失敗
            for out in (entry.get("outputs") or {}).values():
                # SaveVideo 嘅輸出本機實測（0.45.3）係放喺 images 鍵＋metadata.kind=video，唔係 videos/gifs——三個鍵都要睇
                media_list = list(out.get("videos") or []) + list(out.get("gifs") or [])
                for img in out.get("images") or []:
                    fname = str(img.get("filename") or "").lower()
                    if str(((img.get("metadata") or {}).get("kind")) or "").lower() == "video" or fname.endswith((".mp4", ".webm", ".mov", ".gif")):
                        media_list.append(img)
                for media in media_list:
                    qs = urllib.parse.urlencode({"filename": media["filename"], "subfolder": media.get("subfolder", ""),
                                                 "type": media.get("type", "output")})
                    try:
                        req = urllib.request.Request(url + "/view?" + qs)
                        with urllib.request.urlopen(req, timeout=180) as r:
                            data = r.read()
                    except Exception as e:
                        return {"ok": False, "error": "攞片失敗：%s" % e}
                    ext = (media.get("format") or "").split(";")[0].lower().split("/")[-1]
                    if ext not in ("mp4", "webm", "mov", "gif"):
                        ext = media["filename"].rsplit(".", 1)[-1].lower()
                    if ext == "mp4" or ext not in ("mp4", "webm", "mov", "gif"):
                        ext = "mp4"
                    folder = os.path.join(ROOT, "media", "suki", "life")
                    os.makedirs(folder, exist_ok=True)
                    vp = os.path.join(folder, "video-%s-gen-%s.%s" % (time.strftime("%Y-%m-%d"), label, ext))
                    with open(vp, "wb") as f:
                        f.write(data)
                    progress("視頻", "已完成：視頻已存入媒體庫")
                    return {"ok": True, "video": vp}
            return {"ok": False, "error": "工作流冇輸出視頻（搵唔到 videos/gifs 輸出節點）"}
    return {"ok": False, "error": "視頻生成逾時（1800 秒）"}

# ── 媒體庫生成隊列（用戶 2026-11-20 深夜：提示詞提交後即可再提交下一單，ComfyUI 自行排隊）──
GENQ = []
GENQ_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen-queue.json")
_lockq = threading.Lock()

def genq_persist():
    try:
        with _lockq:
            data = json.dumps(GENQ[:80], ensure_ascii=False, indent=1)
        with open(GENQ_FILE + ".tmp", "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(GENQ_FILE + ".tmp", GENQ_FILE)
    except Exception:
        pass

def genq_boot():
    try:
        with open(GENQ_FILE, encoding="utf-8") as f:
            GENQ.extend(json.load(f))
        for t in GENQ:
            if t.get("status") in ("queued", "generating"):
                t["status"] = "error"
                t["detail"] = "服務重啟，任務中斷（Comfy 可能仍出咗圖，可喺媒體庫重新載入查看）"
    except Exception:
        pass

def optimize_gen_prompt(pr):
    """短片語提示詞 → 交 active LLM 擴展成一段完整畫面描述（用戶 2026-11-21 拍板：「不要限，幫我優化即可」）。
    失敗就回空字串——照用原文，唔阻住生成。"""
    try:
        s = load_settings()
        cfg = next((l for l in s.get("llms", []) if l.get("id") == s.get("active_llm")), None)
        if not cfg:
            return ""
        out, err = call_llm(cfg, "你是圖像/視頻生成提示詞優化器。用戶嘅短片語：%s\n"
                            "請擴展成一段英文畫面描述（40–80 詞）：包含場景、人物（13 歲香港女中學生 Suki）嘅動作與表情、鏡頭角度（手機前置鏡頭質感、寫實、無美顏）；"
                            "唔准出現可閱讀文字、品牌、店名、路名、地區名；只輸出描述本身，唔好解釋、唔好引號、唔好前後綴。")
        if err or not out:
            return ""
        out = out.strip().strip('"').replace("\n", " ")
        return out if 20 < len(out) < 900 else ""
    except Exception:
        return ""


def genq_run(task, cmp_url, video_wf):
    task["status"] = "generating"; task["ts_start"] = now(); genq_persist()
    try:
        s = load_settings()
        prompt = task["prompt"]
        if len(prompt) < 24:  # 短句＝自動優化（用戶拍板 2026-11-21：唔再拒「提示詞太短」）
            opt = optimize_gen_prompt(prompt)
            if opt:
                autolog("genq | prompt-optimize | 原文：%s → 優化：%s…" % (prompt[:60], opt[:80]))
                task["detail"] = "已自動優化短提示詞（原文：%s）" % prompt[:60]
                prompt = opt
        if task["kind"] == "image":
            wfpath, werr = get_wf_path(s, "image", task.get("wf") or "")
            r = comfy_generate(cmp_url, prompt + GEN_STYLE_TAIL, task["id"], kind="媒體隊列", with_cover=False, wf_path=wfpath, refs=task.get("refs") or None) if not werr else {"ok": False, "error": werr}
        else:
            wfpath, werr = get_wf_path(s, "video", task.get("wf") or "")
            if werr and (video_wf or "").strip():  # 舊兼容：ComfyUI 設定內嘅 video_workflow 路徑
                oldp = video_wf.strip()
                wfpath = oldp if os.path.isabs(oldp) else os.path.join(ROOT, oldp)
                werr = "" if os.path.isfile(wfpath) else werr
            if werr:
                r = {"ok": False, "error": werr + "（可喺「🔀 工作流管理／系統設置」新增視頻工作流 JSON）"}
            else:
                r = comfy_video(cmp_url, prompt + GEN_STYLE_TAIL, task["id"], wfpath, refs=task.get("refs"))
        if r.get("ok"):
            p = r.get("video") or r.get("png")
            task["files"] = [os.path.relpath(p, ROOT).replace("\\", "/")]
            task["status"] = "done"
        else:
            task["status"] = "error"; task["detail"] = (r.get("error") or "?")[:200]
    except Exception as e:
        task["status"] = "error"; task["detail"] = str(e)[:200]
    task["ts_done"] = now(); genq_persist()
    autolog("genq %s | %s %s | %s" % (task["status"], task["id"], task["kind"], (task["files"] or task.get("detail", ""))[:120]))

def genq_submit(prompt, kinds, wfs=None, refs=None):
    s = load_settings()
    cmp_ = find(s["comfyui"], str(s.get("active_comfy", ""))) or {}
    if not cmp_.get("url"):
        return None, "未喺「系統設置 → ComfyUI 管理」揀咗圖片生成服務"
    ids = []
    n = int(time.time() * 1000)  # 毫秒——同一秒雙提交唔再撞 task id（2026-11-21 修復：撞 id 會兩單互相覆蓋輸出檔名）
    for i, k in enumerate(kinds):
        task = {"id": "%d-%s%d" % (n, k[0], i), "kind": k, "prompt": prompt, "status": "queued",
                "created": now(), "ts_start": "", "ts_done": "", "detail": "", "files": [],
                "wf": (wfs or {}).get(k, ""), "refs": refs or []}
        with _lockq:
            GENQ.insert(0, task)
        ids.append(task["id"])
        threading.Thread(target=genq_run, args=(task, cmp_.get("url"), cmp_.get("video_workflow") or ""), daemon=True).start()
    genq_persist()
    autolog("genq submit | %s | kinds=%s | %s" % (",".join(ids), ",".join(kinds), prompt[:70]))
    return ids, ""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _fail(self, kind):
        # 全局兜底（用戶實測教訓 2026-11-22：js_finish 舊 bug 炸穿 do_POST→連線無回應→瀏覽器只見到「Failed to fetch」）
        tb = [l for l in traceback.format_exc().strip().splitlines() if l.strip()]
        tail = tb[-1][:180] if tb else "?"
        try:
            autolog("[ERROR] 伺服器異常 %s %s :: %s" % (kind, self.path, " | ".join(tb[-3:])[:400]))
        except Exception:
            pass
        try:
            self._send(500, {"ok": False, "error": "伺服器異常（已記入系統日誌）：" + tail})
        except Exception:
            pass

    def do_GET(self):
        try:
            self._do_GET()
        except Exception:
            self._fail("GET")

    def do_POST(self):
        try:
            self._do_POST()
        except Exception:
            self._fail("POST")

    def _send(self, code, data, ctype="application/json; charset=utf-8"):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode()
        elif isinstance(data, str):
            data = data.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(os.path.dirname(__file__), "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif u.path == "/api/posts":
            out = [{"id": p.get("id"), "status": p.get("status", "draft"), "title": p.get("title", ""),
                    "thumb": (p.get("images") or [""])[0],
                    "snippet": (p.get("content", "") or "")[:120], "lang": p.get("lang", "t"), "register": p.get("register", "cy"),
                    "created": p.get("created", ""), "tags": p.get("tags", []),
                    "updated": p.get("updated", ""), "published_at": p.get("published_at", ""),
                    "scheduled_at": p.get("scheduled_at", ""), "note": (p.get("note") or "")[:60],
                    "tags_n": len(p.get("tags", [])), "images_n": len(p.get("images", [])),
                    "note_type": p.get("note_type", ""),
                    "pending_fb": sum(1 for fb in p.get("feedback", []) if not fb.get("handled"))}
                   for p in load_posts()]
            self._send(200, out)
        elif u.path == "/api/post":
            p = find(load_posts(), (q.get("id") or [""])[0])
            self._send(200 if p else 404, p or {"error": "not found"})
        elif u.path == "/api/mcp":
            self._send(200, {"up": XHS_MCP.online()})
        elif u.path == "/api/xhs/status":
            self._send(200, XHS.status())
        elif u.path == "/api/canon":
            media = [m["rel"] for m in scan_media() if "candidates" not in m["rel"]]  # 淘汰中嘅探索圖唔列入正典頁
            cards = {}
            for name, label in CANON_CARDS:
                cards[name] = {"label": label, "text": read_text(os.path.join(CANON_DIR, name + ".md"))}
            self._send(200, {"root": ROOT.replace("\\", "/"),
                             "agents": read_text(os.path.join(ROOT, "AGENTS.md")),
                             "diary": read_text(os.path.join(ROOT, "posts", "diary.md")),
                             "cards": cards,
                             "media": media,
                             "deleted": [d["orig"] for d in deleted_index()]})
        elif u.path == "/api/medialib":
            self._send(200, {"root": ROOT.replace("\\", "/"),
                             "files": scan_media(),
                             "deleted": deleted_index()})
        elif u.path == "/api/progress":
            with _lockp:
                self._send(200, dict(PROG))
        elif u.path == "/api/logs":
            out = []
            for src, path in (("auto", AUTOLOG), ("pub", PUBLOG)):
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        lines = [l.rstrip() for l in f.readlines()[-60:] if l.strip()]
                    out += ["[%s] %s" % (src, l) for l in lines]
                except Exception:
                    pass
            self._send(200, {"lines": out[-120:]})
        elif u.path == "/api/settings":
            self._send(200, load_settings())
        elif u.path == "/api/workflows":
            s = load_settings()
            self._send(200, {"workflows": s.get("workflows", []),
                             "active_image_wf": s.get("active_image_wf", ""), "active_video_wf": s.get("active_video_wf", "")})
        elif u.path == "/api/engage":
            cfg = eng_cfg(); st = eng_state()
            inw, wins = eng_in_window(cfg)
            tail = []
            try:
                with open(ENGAGE_LOG, encoding="utf-8") as f:
                    tail = f.read().splitlines()[-60:]
            except Exception:
                pass
            self._send(200, {"ok": True, "running": ENG["running"], "note": ENG["note"],
                             "settings": cfg, "state": {"date": st["date"], "counts": st["counts"],
                                                        "sessions": st.get("sessions", 0),
                                                        "seen": len(st.get("seen") or {})},
                             "windows": ["%02d:%02d-%02d:%02d" % (divmod(a, 60) + divmod(b, 60)) for a, b in wins],
                             "in_window": inw, "log": tail})
        elif u.path == "/api/dm/threads":
            c = dm_cfg(); ix = dm_index(); ds = dm_state()
            cfg = eng_cfg(); inw, wins = eng_in_window(cfg)
            ths = []
            for uid, t in (ix.get("threads") or {}).items():
                th = dm_load(uid) or {}
                msgs = th.get("messages") or []
                pend = sum(1 for m in msgs if m.get("pending") and m.get("dir") == "in" and _dm_trig_ok(m.get("text")))
                last = msgs[-1] if msgs else {}
                ths.append({"id": uid, "name": t.get("name") or th.get("name") or uid[:8], "auto": bool(t.get("auto")),
                            "n": len(msgs), "pending": pend, "updated": t.get("updated", ""),
                            "last": (last.get("text") or "")[:36], "last_dir": last.get("dir", ""),
                            "today": int((ds["threads"].get(uid) or {}).get("n") or 0)})
            ths.sort(key=lambda x: x.get("updated") or "", reverse=True)
            self._send(200, {"ok": True, "threads": ths, "settings": c,
                             "state": {"sent": ds["sent"], "threads": ds["threads"], "date": ds["date"]},
                             "windows": ["%02d:%02d-%02d:%02d" % (divmod(a, 60) + divmod(b, 60)) for a, b in wins],
                             "in_window": inw, "running": ENG["running"], "cdp": XHS_CDP.up(), "busy": DM_LOCK.locked()})
        elif u.path == "/api/dm/thread":
            th = dm_load((q.get("id") or [""])[0])
            self._send(200 if th else 404, th or {"error": "未搵到對話（先「🔄 更新私信」由平台讀入）"})
        elif u.path == "/api/gen/queue":
            with _lockq:
                self._send(200, {"tasks": GENQ[:50]})
        elif u.path == "/img":
            ap = resolve_media((q.get("path") or [""])[0])
            if not ap.startswith(ROOT) or not os.path.isfile(ap):
                self._send(404, {"error": "bad path"}); return
            ext = ap.lower().rsplit(".", 1)[-1]
            ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
                     "webp": "image/webp", "svg": "image/svg+xml", "mp4": "video/mp4", "webm": "video/webm",
                     "mov": "video/quicktime"}.get(ext, "application/octet-stream")
            with open(ap, "rb") as f:
                self._send(200, f.read(), ctype)
        else:
            self._send(404, {"error": "no route"})

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def _do_POST(self):
        u = urllib.parse.urlparse(self.path)
        body = self._body()
        posts = load_posts()
        pid = str(body.get("id", ""))
        p = find(posts, pid)

        if u.path == "/api/settings":
            s = load_settings()
            for k in ("llms", "comfyui", "active_llm", "active_comfy", "workflows", "active_image_wf", "active_video_wf"):
                if k in body:
                    s[k] = body[k]
            with _lock:
                tmp = SETTINGS + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(s, f, ensure_ascii=False, indent=1)
                os.replace(tmp, SETTINGS)
            self._send(200, {"ok": True})
        elif u.path == "/api/test/llm":
            s = load_settings()
            cfg = body if body.get("base_url") else (find(s["llms"], str(body.get("id", ""))) or {})
            r = test_llm(cfg)
            r["name"] = cfg.get("name", "?")
            self._send(200, r)
        elif u.path == "/api/test/comfy":
            s = load_settings()
            cfg = body if body.get("url") else (find(s["comfyui"], str(body.get("id", ""))) or {})
            r = test_comfy(cfg)
            r["name"] = cfg.get("name", "?")
            self._send(200, r)
        elif u.path == "/api/post/new":
            nid = "%03d" % (max([int(x.get("id", 0)) for x in posts if str(x.get("id", "")).isdigit()] or [0]) + 1)
            p = {"id": nid, "status": "draft", "title": "", "content": "", "tags": [], "images": [],
                 "note": "（新帖：等 Claude 起草——同佢講『幫我起草第 %s 帖』）" % nid,
                 "history": [], "feedback": [], "created": now(), "updated": now()}
            posts.append(p); save_posts(posts)
            self._send(200, p)
        elif u.path == "/api/post/save":
            if not p:
                self._send(404, {"error": "no post"}); return
            if not body.get("no_history"):  # 拖拉排序／刪图等純排列操作唔入版本歷史（用戶拍板：多媒體可排序可刪）
                p.setdefault("history", []).append(snapshot(p))
            for k in ("title", "content", "note", "status"):
                if k in body:
                    p[k] = body[k]
            if body.get("note_type") in ("", "idea", "long"):  # 帖子類型（寫想法＝冇標題只用正文；寫長文＝標題＋正文）
                p["note_type"] = body["note_type"]
            if body.get("lang") in ("t", "s"):
                p["lang"] = body["lang"]
            if body.get("register") in ("cy", "sw"):
                p["register"] = body["register"]
            if "tags" in body:
                p["tags"] = [t.strip().lstrip("#") for t in body["tags"] if t.strip()] if isinstance(body["tags"], list) else \
                            [t.strip().lstrip("#") for t in re.split(r"[,\s#]+", body["tags"]) if t.strip()]
            if "images" in body:
                p["images"] = [i for i in body["images"] if i.strip()]
            p["updated"] = now(); save_posts(posts)
            self._send(200, {"ok": True, "id": pid})
        elif u.path == "/api/post/feedback":
            if not p:
                self._send(404, {"error": "no post"}); return
            txt = (body.get("text") or "").strip()
            if not txt:
                self._send(400, {"ok": False, "error": "empty"}); return
            p.setdefault("feedback", []).append({"ts": now(), "text": txt, "handled": False})
            p["updated"] = now(); save_posts(posts)
            self._send(200, {"ok": True, "hint": "已記入呢帖嘅修改指示。返 Claude session 講「跟 feedback 改帖 %s」即可。" % pid})
        elif u.path == "/api/post/publish":
            if not p:
                self._send(404, {"error": "no post"}); return
            idea = str(p.get("note_type") or "") == "idea"  # 寫想法帖：只用正文、冇標題、可冇圖（純文字想法）
            if not p.get("content") or (not idea and (not p.get("title") or not p.get("images"))):
                self._send(200, {"ok": False, "error": "寫想法帖至少要正文" if idea else "草稿未完整（title/content/images）"}); return
            sched = (body.get("schedule_at") or "").strip()[:19].replace("T", " ")
            r = XHS.call(xc.OP_PUBLISH, title=("" if idea else p["title"]), text=p["content"],
                         images=[resolve_media(i) for i in p.get("images", [])],
                         tags=p.get("tags", []), schedule_at=sched)
            p.setdefault("publish_ops", []).append({"ts": now(), "schedule": sched, "result": ("ok" if r.get("ok") else "fail"), "detail": (r.get("detail") or r.get("error") or "")[:200]})
            if r.get("ok"):
                if sched:
                    p["status"] = "scheduled"; p["scheduled_at"] = sched
                else:
                    p["status"] = "published"; p["published_at"] = now()
            p["updated"] = now(); save_posts(posts)
            try:
                os.makedirs(os.path.dirname(PUBLOG), exist_ok=True)
                with open(PUBLOG, "a", encoding="utf-8") as f:
                    f.write("%s | post %s | %s | schedule=%s | %s\n" % (now(), pid, p["title"],
                            sched or "-", "OK" if r.get("ok") else (r.get("error") or r.get("detail") or "")[:120]))
            except Exception:
                pass
            self._send(200, r)
        elif u.path == "/api/auto":
            self._auto(body, posts, pid)
        elif u.path == "/api/refine":
            self._refine(body, posts, p)
        elif u.path == "/api/post/with_image":
            self._with_image(body, posts)
        elif u.path == "/api/auto/from_image":
            self._autoimg(body, posts)
        elif u.path == "/api/media/generate":
            pr = (body.get("prompt") or "").strip()
            kinds = [k for k in (body.get("kinds") or ["image"]) if k in ("image", "video")]
            if not pr:
                self._send(200, {"ok": False, "error": "請輸入畫面提示詞（幾個字都得——太短會自動幫你優化）"}); return
            if not kinds:
                self._send(200, {"ok": False, "error": "請至少選擇圖片或視頻其中一種"}); return
            wfs = {"image": str(body.get("wf_image") or ""), "video": str(body.get("wf_video") or "")}
            refs = [str(x) for x in (body.get("refs") or []) if str(x).strip()]
            ids, err = genq_submit(pr, kinds, wfs, refs)
            self._send(200, {"ok": True, "ids": ids} if ids else {"ok": False, "error": err})
        elif u.path == "/api/media/upload":
            name = re.sub(r'[\\/:*?"<>|\s]+', "-", os.path.basename(str(body.get("name") or "file")))
            b64 = str(body.get("data_b64") or "")
            ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext not in MEDIA_EXT:
                self._send(200, {"ok": False, "error": "未支援嘅檔案類型（可傳圖片／mp4／mov／webm）：" + ext}); return
            try:
                data = base64.b64decode(b64)
            except Exception:
                self._send(200, {"ok": False, "error": "檔案解碼失敗"}); return
            if len(data) > 200 * 1024 * 1024:
                self._send(200, {"ok": False, "error": "檔案太大（上限 200MB）"}); return
            folder = os.path.join(ROOT, "media", "suki", "life")
            os.makedirs(folder, exist_ok=True)
            tp = "%s-%03d" % (time.strftime("%Y%m%d-%H%M%S"), int(time.time() * 1000) % 1000)
            stem = re.sub(r"\.+$", "", name.rsplit(".", 1)[0])[:40] if "." in name else ""
            fp = os.path.join(folder, "upload-" + ((stem + "-" + tp) if stem else tp) + ext)
            with open(fp, "wb") as f:
                f.write(data)
            relp = os.path.relpath(fp, ROOT).replace("\\", "/")
            autolog("media uploaded: %s (%d KB)" % (relp, len(data) // 1024))
            self._send(200, {"ok": True, "path": relp})
        elif u.path == "/api/post/gen_image":
            self._gen_image(body, posts, p)
        elif u.path == "/api/post/delete":
            if not p:
                self._send(404, {"error": "no post"}); return
            gone = dict(p)
            platform = {"done": False, "detail": ""}
            if p.get("status") == "published" and body.get("platform", True):
                platform = xhs_delete(p)
            posts.remove(p)
            save_posts(posts)
            try:
                os.makedirs(os.path.dirname(PUBLOG), exist_ok=True)
                with open(os.path.join(ROOT, "posts", "deleted.log"), "a", encoding="utf-8") as f:
                    f.write("%s | deleted post %s | %s | platform=%s %s\n" % (now(), gone.get("id"), (gone.get("title") or "")[:60],
                            platform.get("done"), (platform.get("detail") or "")[:120]))
            except Exception:
                pass
            autolog("post deleted #%s（平台：%s）" % (gone.get("id"), "已刪" if platform.get("done") else "需手動／失敗"))
            self._send(200, {"ok": True, "platform": platform})
        elif u.path == "/api/engage/config":
            cfg = eng_cfg_save(body)
            self._send(200, {"ok": True, "settings": cfg})
        elif u.path == "/api/engage/start":
            eng_cfg_save(body)  # 連設定一頁交齊（UI Start 會帶埋表單）
            ok, msg = start_engage()
            eng_log(("▶ start: " if ok else "✖ start: ") + msg)
            self._send(200, {"ok": ok, "detail": msg})
        elif u.path == "/api/engage/stop":
            stop_engage()
            self._send(200, {"ok": True, "detail": "已停止（隨時可以再按開始）"})
        elif u.path == "/api/engage/round":
            if ENG.get("_round_busy"):
                self._send(200, {"ok": False, "error": "有一輪測試進行中／引擎運行中——等它完成"}); return
            if ENG["running"]:
                self._send(200, {"ok": False, "error": "引擎喺度運行——用「立即試一輪」前先停止，或直接等自動輪"}); return
            ENG["_round_busy"] = True
            try:
                res = eng_round(cfg=eng_cfg(), force=True, dry=bool(body.get("dry")),
                                caps=body.get("caps") or None)
            finally:
                ENG["_round_busy"] = False
            self._send(200, res)
        elif u.path == "/api/dm/config":
            self._send(200, {"ok": True, "settings": dm_cfg_save(body)})
        elif u.path == "/api/dm/auto":
            uid = _dm_safe(body.get("id"))
            ix = dm_index()
            if uid not in (ix.get("threads") or {}):
                self._send(200, {"ok": False, "error": "未搵到對話"}); return
            ix["threads"][uid]["auto"] = bool(body.get("auto"))
            dm_index_save(ix)
            dm_log("%s「自動覆」：%s" % (ix["threads"][uid].get("name") or uid[:8], "開✅" if body.get("auto") else "關"))
            self._send(200, {"ok": True})
        elif u.path == "/api/dm/check":
            if DM_LOCK.locked():
                self._send(200, {"ok": False, "error": "有私訊操作進行中（瀏覽器忙）"}); return
            with DM_LOCK:
                self._send(200, dm_check(body.get("top")))
        elif u.path == "/api/dm/pass":
            if DM_LOCK.locked():
                self._send(200, {"ok": False, "error": "有私訊操作進行中（瀏覽器忙）"}); return
            with DM_LOCK:
                self._send(200, dm_pass(eng_cfg(), dry=bool(body.get("dry"))))
        elif u.path == "/api/dm/send":
            uid = _dm_safe(body.get("id")); text = str(body.get("text") or "")
            if DM_LOCK.locked():
                self._send(200, {"ok": False, "error": "有私訊操作進行中（瀏覽器忙）"}); return
            with DM_LOCK:
                r = dm_send(uid, text, via="manual")
            if r.get("ok"):
                dm_log("✔ 人手（UI）覆咗 %s：「%s」" % ((dm_load(uid) or {}).get("name") or uid[:8], text[:40]))
            self._send(200, r)
        elif u.path == "/api/dm/draft":
            uid = _dm_safe(body.get("id")); th = dm_load(uid); c = dm_cfg()
            if not th:
                self._send(200, {"ok": False, "error": "未搵到對話"}); return
            out, err = eng_llm(DM_TASK % (full_canon(), th.get("name") or "網友", dm_ctx_text(th, c), time.strftime("%Y-%m-%d (%a)")))
            txt = eng_clean(out); hits = eng_redline(txt)
            self._send(200, {"ok": bool(txt) and not hits, "text": txt if (txt and not hits) else "",
                             "error": "" if (txt and not hits) else ("紅線：" + "、".join(hits) if hits else (err or "LLM 冇輸出"))})
        elif u.path == "/api/gen/clear":
            with _lockq:
                GENQ[:] = [t for t in GENQ if t.get("status") in ("queued", "generating")]
            genq_persist()
            self._send(200, {"ok": True})
        elif u.path == "/api/media/delete":
            ok, msg = media_delete(body.get("path", ""))
            self._send(200, {"ok": ok, "detail": msg})
        elif u.path == "/api/media/restore":
            ok, msg = media_restore(str(body.get("trash", "")))
            self._send(200, {"ok": ok, "detail": msg})
        elif u.path == "/api/canon/save":
            ok, msg = canon_save(str(body.get("target", "")), body.get("text"))
            self._send(200, {"ok": ok, "detail": msg})
        else:
            self._send(404, {"error": "no route"})

    def _auto(self, body, posts, pid):
        t0 = time.time()
        hint = (body.get("hint") or "").strip()
        # kinds（v5）：["text","image","video"] 覆選＋順序（用戶拖order）；兼容舊 with_image 參數
        kinds = body.get("kinds")
        if kinds is None:
            kinds = ["text", "image"] if body.get("with_image") else ["text"]
        kinds = [k for k in kinds if k in ("text", "image", "video")] or ["text"]
        want = [k for k in kinds if k in ("image", "video")]  # 生成序＝用戶拖嘅順序
        # 帖子類型（用戶拍板 2026-11-21：小紅書有「寫想法」＝只用正文唔用標題／「寫長文」＝標題＋正文）
        note_mode = str(body.get("note_mode") or "auto")
        if note_mode not in ("auto", "idea", "long"):
            note_mode = "auto"
        tgt0 = find(posts, pid)
        directives = []
        if note_mode == "idea":
            kinds, want = ["text"], []  # 想法帖＝純文字，呢單唔生成媒體
            directives.append('【帖子類型＝寫想法】小紅書「寫想法」帖冇標題："title" 必須輸出空字串 ""，全部文字寫入 "content"（隨手記／心情短講，自然口語，唔好硬凑長度）。')
        elif note_mode == "long":
            directives.append('【帖子類型＝寫長文】必須有標題："title" 必填（≤20 字，見規格）。')
        directives.append("本單需要生成圖片，image_prompt 必填（英文，跟規則 4）。" if "image" in want else "本單唔需要生成圖片：image_prompt 請輸出空字串 \"\"。")
        directives.append('本單需要生成視頻：請喺 JSON 額外輸出 "video_prompt"（英文：單一動作、約 5 秒、鏡頭固定、人物動作自然細碎；人物／手／文字／暈影規格同圖片規則一樣）。' if "video" in want else '本單唔需要視頻：唔好輸出 video_prompt。')
        ld = lang_directive(tgt0)
        if ld:
            directives.append(ld)
        refs_sel = [str(x) for x in (body.get("refs") or []) if str(x).strip()]
        if refs_sel:
            directives.append("【參考】用戶喺工作台選咗參考圖/片：%s——若本單屬「換面／保留場景改人物／合照」類，image_prompt／video_prompt 要保留參考嘅構圖邏輯並寫明畫面主體就係 Suki 本人（keep the composition of the reference, the girl is Suki herself）；身份由系統注入 REF 圖負責，唔使描述參考圖細節。" % "、".join(os.path.basename(x) for x in refs_sel[:3]))
        hint_full = (hint + "\n" if hint else "") + "\n".join(directives)
        try:
            s = load_settings()
            llm = find(s["llms"], str(s.get("active_llm", ""))) or {}
            if not llm:
                self._send(200, {"ok": False, "error": "未喺「LLM 管理」揀咗寫文案嘅 LLM"}); return
            autolog("start | hint=%s | kinds=%s | note_mode=%s | llm=%s" % (hint[:40], "+".join(kinds), note_mode, llm.get("name")))
            progress("新帖", "讀 Suki 全本人設正典", "AGENTS.md＋發帖日記＋帖檔｜本單：" + "+".join(kinds))
            prompt = build_auto_prompt(hint_full, posts)
            progress("新帖", "交畀 %s 度緊下一帖" % (llm.get("name") or "LLM"), "全本人設已提交，%d 字 prompt" % len(prompt))
            text, err = call_llm(llm, prompt)
            if err:
                progress("", "錯誤：" + err[:80])
                autolog("LLM error after %.0fs: %s" % (time.time() - t0, err[:200]))
                self._send(200, {"ok": False, "error": "LLM：%s（詳情 post-studio\\auto-log.txt）" % err}); return
            progress("新帖", "文案回咗（%.0fs）" % (time.time() - t0), "紅線掃描＋入帖檔")
            d = extract_json(text)
            if not (text or "").strip():
                progress("", "錯誤：LLM 輸出空白")
                autolog("empty response after %.0fs | llm=%s" % (time.time() - t0, llm.get("name")))
                self._send(200, {"ok": False, "error": "LLM 輸出空白——呢個 model 冇回應內容（多半係將內容放咗喺思考欄）。建議換 model 或者用 LM Studio（本地）。"}); return
            if not d or not (d.get("title") or d.get("content")):
                # 兜底（用戶實測 2026-11-21：免費模型成日講咗一堆解釋但冇吐 JSON）：把失敗輸出交返模型，要求只重組為純 JSON 再行一次
                progress("", "輸出唔係 JSON——叫模型「只返返個 JSON」再試一次")
                fix_msgs = [{"role": "user", "content": prompt},
                            {"role": "assistant", "content": (text or "")[:1200]},
                            {"role": "user", "content": "你頭先嘅回覆唔係 JSON。而家由 { 呢個字符開始，直接重新輸出完整、合法、單一嘅 JSON 物件（內容可以一樣，唔好任何解釋、思考文字或者 markdown 圍欄）。"}]
                text2, err2 = call_llm(llm, prompt, msgs=fix_msgs)
                d2 = extract_json(text2)
                if d2 and (d2.get("title") or d2.get("content")):
                    text, d = text2, d2
                    autolog("json-fix retry succeeded after %.0fs" % (time.time() - t0))
                    progress("新帖", "模型已補返 JSON（%.0fs）" % (time.time() - t0))
            if not d or not (d.get("title") or d.get("content")):
                progress("", "錯誤：LLM 輸出唔係 JSON")
                autolog("unparseable after %.0fs: %s" % (time.time() - t0, (text or "")[:200]))
                self._send(200, {"ok": False, "error": "LLM 輸出唔係 JSON（原文已記 auto-log.txt）：" + (text or "")[:200]}); return
            whole = (d.get("title", "") or "") + (d.get("content", "") or "")
            banned = [b for b in BANNED_AUTO if b in whole]  # 出稿後再掃一次紅線（雙重把關）
            tags = [str(t).strip().lstrip("#") for t in (d.get("tags") or []) if str(t).strip()]
            banned += ["標籤:" + t for t in tags if any(x in t.lower() for x in TAG_BAN)]
            banned += ["地區名:" + x for x in TAG_BAN if x in whole.lower()]  # TAG 唔准地區詞（用戶拍板）
            filled = None
            tgt = find(posts, pid)
            if tgt and not (tgt.get("title") or tgt.get("content")):
                filled = tgt["id"]  # 開住嘅係空草稿 → 直接填入，唔另開新帖
                tgt.setdefault("history", []).append(snapshot(tgt))
                p = tgt
            else:
                nid = "%03d" % (max([int(x.get("id", 0)) for x in posts if str(x.get("id", "")).isdigit()] or [0]) + 1)
                p = {"id": nid, "history": [], "feedback": [], "created": now()}
                posts.append(p)
            p.update({"status": "ready", "title": ("" if note_mode == "idea" else (d.get("title") or "").strip()),
                      "content": (d.get("content") or "").strip(), "tags": tags,
                      "updated": now()})
            if note_mode in ("idea", "long"):
                p["note_type"] = note_mode
            p["note"] = ("一鍵自動設計 %s · 模型 %s/%s｜揀題理由：%s" % (now(), llm.get("name"), llm.get("model"), (d.get("reason") or "")[:200])).strip()
            img_note = ""
            images = list(p.get("images", []))  # 多媒體：生成一律「加」唔覆蓋（用戶拍板：再按生成會另外增加一張）
            if want:
                p["note"] += "｜文案用時 %.0fs" % (time.time() - t0)
                cmp_ = find(s["comfyui"], str(s.get("active_comfy", ""))) or {}
                arefs = ref_files(body.get("refs") or [])
                wfi, _we = get_wf_path(s, "image", str(body.get("wf_image") or ""))
                wfj, we2 = get_wf_path(s, "video", str(body.get("wf_video") or ""))
                if we2:  # 舊兼容：ComfyUI 設定內嘅 video_workflow
                    old = (cmp_.get("video_workflow") or "").strip()
                    cand = old if os.path.isabs(old) else (os.path.join(ROOT, old) if old else "")
                    if cand and os.path.isfile(cand):
                        wfj, we2 = cand, ""
                for k in want:  # 按用戶拖好嘅順序逐個生成（ComfyUI 自行排队）
                    if k == "image":
                        progress("新帖#" + p["id"], "生成圖片", "本單順序：" + "+".join(want))
                        r = comfy_generate(cmp_.get("url", ""), d.get("image_prompt") or "", p["id"] + "g%d" % len(images), kind="新帖#" + p["id"],
                                          wf_path="" if _we else wfi, refs=arefs or None)
                        if r.get("ok"):
                            images.append(os.path.normpath(r["cover"]).replace("\\", "/"))
                            p["note"] += "｜原圖 " + r["png"].replace("\\", "/")
                            img_note += "🖼 圖已生成（PIL 已加淡暈影＋背景微糊）\n"
                        else:
                            p["note"] += "｜圖失敗：" + (r.get("error") or "")
                            img_note += "⚠️ 圖生成失敗：" + (r.get("error") or "") + "（文案已保存，可再試）\n"
                    elif k == "video":
                        progress("新帖#" + p["id"], "生成視頻", "本單順序：" + "+".join(want))
                        if we2:
                            img_note += "⚠️ 視頻工作流未配置：" + we2 + "\n"
                            p["note"] += "｜視頻未配置"
                        else:
                            r = comfy_video(cmp_.get("url", ""), d.get("video_prompt") or d.get("image_prompt") or "", p["id"] + "v%d" % len(images), wfj, refs=body.get("refs") or None)
                            if r.get("ok"):
                                images.append(os.path.relpath(r["video"], ROOT).replace("\\", "/"))
                                p["note"] += "｜視頻 " + os.path.relpath(r["video"], ROOT).replace("\\", "/")
                                img_note += "🎬 視頻已生成並掛帖（發布影片可能要喺 App 操作）\n"
                            else:
                                p["note"] += "｜視頻失敗：" + (r.get("error") or "")
                                img_note += "⚠️ 視頻生成失敗：" + (r.get("error") or "") + "\n"
            p["images"] = images
            p["note"] += "｜總用時 %.0fs" % (time.time() - t0)
            save_posts(posts)
            progress("", "完成：帖 #%s 已入帖檔（待校對）" % p["id"])
            autolog("done | post %s%s | total %.0fs | banned=%s | image=%s" % (p["id"], "(填入)" if filled else "", time.time() - t0, banned, bool(images)))
            self._send(200, {"ok": True, "id": p["id"], "filled": bool(filled), "banned": banned, "secs": int(time.time() - t0),
                             "reason": d.get("reason", ""), "kinds": kinds, "image": "image" in want, "video": "video" in want,
                             "image_detail": img_note.strip()})
        except Exception as e:
            progress("", "錯誤：服務異常 " + str(e)[:60])
            autolog("EXC after %.0fs: %s" % (time.time() - t0, traceback.format_exc().replace("\n", " | ")[:600]))
            try:
                self._send(200, {"ok": False, "error": "服務異常：%s（詳情睇 post-studio\\auto-log.txt）" % str(e)[:160]})
            except Exception:
                pass

    def _new_slot(self, body, posts):
        """有開住嘅空草稿就填入，否則新建。回 (post, filled_bool)。"""
        tgt = find(posts, str(body.get("id", "")))
        if tgt and not (tgt.get("title") or tgt.get("content")):
            tgt.setdefault("history", []).append(snapshot(tgt))
            return tgt, True
        nid = "%03d" % (max([int(x.get("id", 0)) for x in posts if str(x.get("id", "")).isdigit()] or [0]) + 1)
        q = {"id": nid, "history": [], "feedback": [], "created": now(), "note": ""}
        posts.append(q)
        return q, False

    def _with_image(self, body, posts):
        try:
            rel = (body.get("path") or "").replace("\\", "/").strip("/")
            ap = os.path.join(ROOT, rel.replace("/", os.sep))
            if not rel.startswith("media/") or not os.path.isfile(ap):
                self._send(200, {"ok": False, "error": "檔案不存在：" + rel}); return
            p, filled = self._new_slot(body, posts)
            if re.search(r"\.(mp4|mov|webm)$", rel, re.I):
                apn = os.path.normpath(ap).replace("\\", "/")
                imgs = [apn]
                cover_note = "（影片直接掛帖，出圖封面出街前要喺 App 補圖）"
            else:
                cover, err = make_cover(ap)
                cover_abs = os.path.normpath(cover).replace("\\", "/")
                imgs = [cover_abs]  # 絕對路徑（与他處一致；相對路徑曾導致出帖頁唔顯示圖）
                cover_note = ("｜封面版：" + cover_abs) + ("（" + err + "，改用原圖）" if err else "")
            p["images"] = imgs
            p["status"] = p.get("status") or "draft"
            p["updated"] = now()
            p["note"] = (p.get("note") or "") + "｜🖼 媒體庫「出帖」掛圖 %s %s" % (now(), cover_note)
            save_posts(posts)
            autolog("post with image | #%s %s | %s" % (p["id"], "(填入)" if filled else "(新)", rel))
            self._send(200, {"ok": True, "id": p["id"], "filled": filled, "images": imgs})
        except Exception as e:
            self._send(200, {"ok": False, "error": str(e)[:160]})

    def _autoimg(self, body, posts):
        t0 = time.time()
        rel = (body.get("path") or "").replace("\\", "/").strip("/")
        hint = (body.get("hint") or "").strip()
        ap = os.path.join(ROOT, rel.replace("/", os.sep))
        try:
            if not rel.startswith("media/") or not os.path.isfile(ap):
                self._send(200, {"ok": False, "error": "檔案不存在：" + rel}); return
            s = load_settings()
            llm = find(s["llms"], str(s.get("active_llm", ""))) or {}
            if not llm:
                self._send(200, {"ok": False, "error": "未喺「LLM 管理」揀咗寫文案嘅 LLM"}); return
            autolog("caption-from-image start | %s | hint=%s" % (rel, hint[:60]))
            progress("圖文案", "讀 Suki 全本人設正典", rel)
            name = os.path.basename(rel)
            canon_full = read_text(os.path.join(ROOT, "AGENTS.md"))
            desc = "\n".join([ln.strip() for ln in canon_full.split("\n") if name in ln][:6])
            ld = lang_directive(find(posts, str(body.get("id", ""))))
            if ld:
                hint = (hint + "\n" if hint else "") + ld
            prompt = build_imagepost_prompt(rel, desc, hint, posts)
            progress("圖文案", "交畀 %s 度緊文案" % (llm.get("name") or "LLM"), "提示：" + (hint[:60] or "無"))
            text, err = call_llm(llm, prompt)
            if err:
                progress("", "錯誤：" + err[:80]); autolog("caption LLM error: " + err[:200])
                self._send(200, {"ok": False, "error": "LLM：%s（詳情 auto-log.txt）" % err}); return
            d = extract_json(text)
            if not d or not (d.get("title") or d.get("content")):
                progress("", "錯誤：LLM 輸出唔係 JSON")
                self._send(200, {"ok": False, "error": "LLM 輸出唔係 JSON：" + (text or "")[:200]}); return
            whole = (d.get("title", "") or "") + (d.get("content", "") or "")
            banned = [b for b in BANNED_AUTO if b in whole]
            tags = [str(t).strip().lstrip("#") for t in (d.get("tags") or []) if str(t).strip()]
            banned += ["標籤:" + t for t in tags if any(x in t.lower() for x in TAG_BAN)]
            banned += ["地區名:" + x for x in TAG_BAN if x in whole.lower()]
            progress("圖文案", "文案回咗（%.0fs）" % (time.time() - t0), "出封面＋入帖檔")
            p, filled = self._new_slot(body, posts)
            if re.search(r"\.(mp4|mov|webm)$", rel, re.I):
                imgs = [os.path.normpath(ap).replace("\\", "/")]
            else:
                cover, err2 = make_cover(ap)
                imgs = [os.path.normpath(cover).replace("\\", "/")]
            p.update({"status": "ready", "title": (d.get("title") or "").strip(), "content": (d.get("content") or "").strip(),
                      "tags": tags, "images": imgs, "updated": now()})
            p["note"] = ("🖼 按圖出文案 %s · 圖 %s · 提示：%s｜模型 %s/%s｜%s" % (
                now(), rel, (hint or "無")[:80], llm.get("name"), llm.get("model"), (d.get("reason") or "")[:200]))
            save_posts(posts)
            progress("", "完成：帖 #%s（待校對）" % p["id"])
            autolog("caption-from-image done | #%s | %.0fs | banned=%s" % (p["id"], time.time() - t0, banned))
            self._send(200, {"ok": True, "id": p["id"], "filled": filled, "banned": banned, "secs": int(time.time() - t0),
                             "reason": d.get("reason", ""), "images": imgs})
        except Exception as e:
            progress("", "錯誤：服務異常 " + str(e)[:60])
            autolog("caption EXC: " + traceback.format_exc().replace("\n", " | ")[:500])
            self._send(200, {"ok": False, "error": "服務異常：%s" % str(e)[:160]})

    def _refine(self, body, posts, p):
        t0 = time.time()
        try:
            s = load_settings()
            llm = find(s["llms"], str(s.get("active_llm", ""))) or {}
            if not llm:
                self._send(200, {"ok": False, "error": "未喺「LLM 管理」揀咗寫文案嘅 LLM"}); return
            if not p:
                self._send(200, {"ok": False, "error": "呢帖唔存在（先喺左邊揀一帖）"}); return
            fb_this = (body.get("text") or "").strip()
            pend = [f.get("text", "") for f in p.get("feedback", []) if not f.get("handled")]
            fbs = ([fb_this] if fb_this else []) + [t for t in pend if t and t != fb_this]
            if not fbs:
                self._send(200, {"ok": False, "error": "無修改指示——喺「AI 修改指示」輸入框打字先改得住"}); return
            ld = lang_directive(p)
            if ld:
                fbs.append(ld)  # 帖子頁嘅語言／語體設定（v5）跟随改稿
            with_img = bool(body.get("with_image", True))  # 預設連圖一齊出（用戶拍板：送出指示要改到圖）
            autolog("refine start | post %s | fb=%s | with_image=%s" % (p["id"], fbs[0][:60], with_img))
            kind = "改帖#" + p["id"]
            progress(kind, "讀 Suki 全本人設正典", "連同呢帖現稿＋你嘅指示一齊提交")
            prompt = build_refine_prompt(p, fbs, posts)
            progress(kind, "交畀 %s 跟指示改稿" % (llm.get("name") or "LLM"), "指示：%s" % fbs[0])
            text, err = call_llm(llm, prompt)
            if err:
                progress("", "錯誤：" + err[:80])
                autolog("refine LLM error after %.0fs: %s" % (time.time() - t0, err[:200]))
                self._send(200, {"ok": False, "error": "LLM：%s（詳情 auto-log.txt）" % err}); return
            progress(kind, "新稿回咗（%.0fs）" % (time.time() - t0), "紅線掃描＋入帖檔")
            d = extract_json(text)
            if not d or not (d.get("title") or d.get("content")):
                progress("", "錯誤：LLM 輸出唔係 JSON")
                autolog("refine unparseable after %.0fs: %s" % (time.time() - t0, (text or "")[:200]))
                self._send(200, {"ok": False, "error": "LLM 輸出唔係 JSON（原文已記 auto-log.txt）：" + (text or "")[:200]}); return
            whole = (d.get("title", "") or "") + (d.get("content", "") or "")
            banned = [b for b in BANNED_AUTO if b in whole]
            tags = [str(t).strip().lstrip("#") for t in (d.get("tags") or []) if str(t).strip()]
            banned += ["標籤:" + t for t in tags if any(x in t.lower() for x in TAG_BAN)]
            banned += ["地區名:" + x for x in TAG_BAN if x in whole.lower()]
            p.setdefault("history", []).append(snapshot(p))
            if p.get("status") != "published":
                p["status"] = "ready"
            p["title"] = (d.get("title") or "").strip()
            p["content"] = (d.get("content") or "").strip()
            p["tags"] = tags
            p.setdefault("note", "")
            p["note"] += "｜🪄跟指示改稿 %s（%s）：%s" % (now(), fbs[0][:60], (d.get("reason") or "")[:150])
            for f in p.get("feedback", []):
                f["handled"] = True
            if fb_this and fb_this not in pend:
                p.setdefault("feedback", []).append({"ts": now(), "text": fb_this, "handled": True})
            img_note = ""
            images = p.get("images", [])
            if with_img:
                p["note"] += "｜文案用時 %.0fs" % (time.time() - t0)
                cmp_ = find(s["comfyui"], str(s.get("active_comfy", ""))) or {}
                label = "%sr%d" % (p["id"], len(p["history"]))
                r = comfy_generate(cmp_.get("url", ""), d.get("image_prompt") or "", label, kind=kind,
                                  wf_path=get_wf_path(s, "image", "")[0] or "", refs=ref_files(body.get("refs") or []) or None)
                if r.get("ok"):
                    newimg = os.path.normpath(r["cover"]).replace("\\", "/")
                    idx = next((i for i, im in enumerate(images) if not re.search(r"\.(mp4|mov|webm|gif)$", str(im), re.I)), None)
                    if idx is None:
                        images.append(newimg)
                        img_note = "🖼 新圖已加入本帖（本帖原本冇圖）（淡暈影＋背景微糊自動加）"
                    else:
                        images[idx] = newimg
                        img_note = "🖼 圖已跟新 prompt 重新生成並取代本帖第一張圖（其他媒體保留；淡暈影＋背景微糊自動加）"
                    p["note"] += "｜新原圖 " + r["png"].replace("\\", "/")
                else:
                    p["note"] += "｜圖失敗：" + (r.get("error") or "")
                    img_note = "⚠️ 圖生成失敗：" + (r.get("error") or "") + "（文案已改好保存；可再送出多一次）"
            p["images"] = images
            p["updated"] = now()
            p["note"] += "｜總用時 %.0fs" % (time.time() - t0)
            save_posts(posts)
            progress("", "完成：帖 #%s 已跟指示改好" % p["id"])
            autolog("refine done | post %s | total %.0fs | banned=%s | image=%s" % (p["id"], time.time() - t0, banned, bool(with_img)))
            self._send(200, {"ok": True, "id": p["id"], "banned": banned, "secs": int(time.time() - t0),
                             "reason": d.get("reason", ""), "image": with_img, "image_detail": img_note})
        except Exception as e:
            progress("", "錯誤：服務異常 " + str(e)[:60])
            autolog("refine EXC after %.0fs: %s" % (time.time() - t0, traceback.format_exc().replace("\n", " | ")[:600]))
            try:
                self._send(200, {"ok": False, "error": "服務異常：%s（詳情 auto-log.txt）" % str(e)[:160]})
            except Exception:
                pass

    def _gen_image(self, body, posts, p):
        """工作台「生成媒體」（用戶拍板 2026-11-21 ③⑨）：按本帖正文＋用戶提示出圖／視頻；
        每次按「生成」＝另外增加一個媒體（唔覆蓋舊圖）；可選參考圖／片（換面／合照——工作流有參考輸入先生效）。"""
        t0 = time.time()
        isvid = body.get("kind") == "video"
        kn = "視頻" if isvid else "圖片"
        kind = "生%s#%s" % ("片" if isvid else "圖", p["id"] if p else "?")
        try:
            if not p:
                self._send(200, {"ok": False, "error": "呢帖唔存在（先喺左邊揀一帖）"}); return
            hint = (body.get("hint") or "").strip()
            refs = [str(x) for x in (body.get("refs") or []) if str(x).strip()]
            s = load_settings()
            llm = find(s["llms"], str(s.get("active_llm", ""))) or {}
            cmp_ = find(s["comfyui"], str(s.get("active_comfy", ""))) or {}
            if not cmp_.get("url"):
                self._send(200, {"ok": False, "error": "未喺「系統設置 → ComfyUI」揀咗生成服務"}); return
            if not llm:
                self._send(200, {"ok": False, "error": "未喺「系統設置」揀咗寫 prompt 嘅 LLM"}); return
            wfpath, werr = get_wf_path(s, "video" if isvid else "image", str(body.get("wf") or ""))
            if werr and isvid:
                old = (cmp_.get("video_workflow") or "").strip()
                cand = old if os.path.isabs(old) else (os.path.join(ROOT, old) if old else "")
                if cand and os.path.isfile(cand):
                    wfpath, werr = cand, ""
            if werr:
                self._send(200, {"ok": False, "error": werr}); return
            ref_desc = "無"
            if refs:
                ref_desc = "（最高優先）用戶經媒體庫選咗參考：" + "、".join(os.path.basename(x) for x in refs[:3]) + "——若指示屬「換面／保留場景改人物／合照」類，prompt 保留參考構圖邏輯並寫明畫面主體就係 Suki 本人；身份由系統注入 REF 圖負責，唔使描述參考圖細節"
            progress(kind, "讀人設＋本帖正文 → 出%s prompt" % kn, "提示：%s" % (hint[:60] or "無（按正文情境自行設計）"))
            prompt = IMG_TASK % (kn, time.strftime("%Y-%m-%d %a"), (p.get("title") or "")[:120], (p.get("content") or "")[:900],
                                 hint or "（無——按正文情境同情緒自行設計，構圖露臉與否按情緒）", ref_desc,
                                 FACE_BLOCK, HAND_CLAUSE, PHONE_CLAUSE, full_canon(), published_summary(posts))
            text, err = call_llm(llm, prompt)
            if err:
                progress("", "錯誤：" + err[:80]); autolog("genmedia LLM error: " + err[:200])
                self._send(200, {"ok": False, "error": "LLM：%s" % err}); return
            d = extract_json(text) or {}
            ip = (d.get("image_prompt") or "").strip()
            if len(ip) < 60:
                progress("", "錯誤：prompt 生成失敗")
                self._send(200, {"ok": False, "error": "LLM 冇返回可用 image_prompt：" + (text or "")[:150]}); return
            progress(kind, "ComfyUI 生成中", "呢單可能幾分鐘，完成會自動掛喺本帖媒體" if not isvid else "視頻較慢（H3 首次載入可 10–30 分鐘）")
            label = "%s%s%d" % (p["id"], "v" if isvid else "g", int(time.time()) % 1000000)
            if isvid:
                r = comfy_video(cmp_.get("url", ""), ip, label, wfpath, refs=refs or None)
            else:
                r = comfy_generate(cmp_.get("url", ""), ip, label, kind=kind, with_cover=True, wf_path=wfpath, refs=ref_files(refs) or None)
            if not r.get("ok"):
                p["note"] = (p.get("note") or "") + "｜%s生成失敗 %s：%s" % (kn, now(), (r.get("error") or "")[:120])
                save_posts(posts)
                progress("", "失敗：" + (r.get("error") or "")[:60])
                self._send(200, {"ok": False, "error": r.get("error"), "prompt": ip[:300]}); return
            p.setdefault("history", []).append(snapshot(p))
            images = list(p.get("images") or [])
            added = os.path.normpath(r.get("video") or r.get("cover") or r["png"]).replace("\\", "/")
            images.append(added)  # 加，唔覆蓋（用戶拍板）
            p["images"] = images
            p["note"] = (p.get("note") or "") + "｜➕工作台生%s %s（提示：%s；prompt：%s）%s" % (kn, now(), hint[:50] or "無", ip[:120], "" if not r.get("png") else "｜原圖 " + os.path.normpath(r["png"]).replace("\\", "/"))
            p["updated"] = now(); save_posts(posts)
            progress("", "完成：帖 #%s 已加%s（現共 %d 個媒體）" % (p["id"], kn, len(images)))
            autolog("genmedia done | #%s | %s | %.0fs" % (p["id"], kn, time.time() - t0))
            try:  # 生成已完成先至回話——若瀏覽器此時已刷新／關頁（WinError 10053），唔當服務錯誤（結果早已保存喺帖檔）
                self._send(200, {"ok": True, "id": p["id"], "added": added, "images": images, "secs": int(time.time() - t0),
                                 "reason": (d.get("reason") or "")[:200], "prompt": ip[:300]})
            except OSError:
                autolog("genmedia done | #%s（客戶端斷線，結果已保存）" % p["id"])
        except Exception as e:
            progress("", "錯誤：服務異常 " + str(e)[:60])
            autolog("genmedia EXC: " + traceback.format_exc().replace("\n", " | ")[:500])
            try:
                self._send(200, {"ok": False, "error": "服務異常：%s" % str(e)[:160]})
            except Exception:
                pass

if __name__ == "__main__":
    genq_boot()
    print("Suki Post Studio v5.2 → http://127.0.0.1:%d" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
