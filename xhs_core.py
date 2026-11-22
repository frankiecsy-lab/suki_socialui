# -*- coding: utf-8 -*-
"""xhs_core · 小紅書操作抽象層（用戶 2026-11-22：「將小紅書的操作做成抽象層，之後有新方法可以使用」）

概念（三層）：
  · 操作目錄（本檔 OP_*）＝平台「動作」的統一名字，唔關業務、唔關渠道；
  · Driver＝一個實作渠道：
      - MCPDriver：xiaohongshu-mcp API（localhost:18060）——feed／搜索／筆記詳情／讚／藏／評／覆評／未讀／通知／發布；
      - CDPDriver：本地瀏覽器（Chrome :9333，DOM 模擬人手）——私信讀／覆、創作平台刪除（呢啲 MCP 冇嘅能力）。
  · XHS（路由器）：按「能力 + 優先序」揀 driver，全部調用統一日誌＋異常兜底。

之後有新方法／新渠道（官方 API、xiaohongshu-mcp 新版 DM 工具、Appium 真機⋯）
  ＝ 新增一個 Driver（宣告支援嘅 OP_*），server.py 業務碼零改動；
  同一操作有多个 driver 支援時，按優先序取第一（可喺 settings.json "xhs_priority" 改序）。
正典見 AGENTS §11A.K。純 Python 標準庫，零依賴。"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ────────── 操作目錄（capability names + 中文說明，UI 能力矩陣用） ──────────
OP_LOGIN   = "check_login"
OP_FEEDS   = "list_feeds"
OP_SEARCH  = "search_feeds"
OP_NOTE    = "note_detail"
OP_LIKE    = "like_note"
OP_COLLECT = "collect_note"
OP_COMMENT = "comment_note"
OP_REPLY   = "reply_comment"
OP_UNREAD  = "unread_count"
OP_NOTIFS  = "list_notifications"
OP_PUBLISH = "publish_note"
OP_DELETE  = "delete_note"
OP_DM_CONV = "dm_conversations"
OP_DM_READ = "dm_read_conversations"
OP_DM_SEND = "dm_send"

OP_LABEL = {
    OP_LOGIN:   "檢查登入態",
    OP_FEEDS:   "首頁 feed",
    OP_SEARCH:  "搜索筆記",
    OP_NOTE:    "筆記詳情（含評論）",
    OP_LIKE:    "點讚",
    OP_COLLECT: "收藏",
    OP_COMMENT: "評論筆記",
    OP_REPLY:   "回覆評論",
    OP_UNREAD:  "未讀數量",
    OP_NOTIFS:  "通知列表",
    OP_PUBLISH: "發布圖文",
    OP_DELETE:  "刪除筆記",
    OP_DM_CONV: "私信會話列表",
    OP_DM_READ: "私信對話讀取",
    OP_DM_SEND: "私信發送",
}
KNOWN_OPS = list(OP_LABEL.keys())


class Driver(object):
    """驅動基類：子类宣告 name/label，實現 ops()（OP → callable）與 online()。"""
    name = "?"
    label = "?"

    def ops(self):
        return {}

    def supports(self, op):
        return op in self.ops()

    def online(self):
        return False


# ═══════════════ Driver 1：xiaohongshu-mcp（JSON-RPC · localhost:18060） ═══════════════
class MCPDriver(Driver):
    name = "mcp"
    label = "xiaohongshu-mcp（API）"

    def __init__(self, endpoint="http://localhost:18060/mcp"):
        self.url = endpoint

    def online(self):
        try:
            urllib.request.urlopen(self.url, timeout=3)
            return True
        except urllib.error.HTTPError:
            return True          # 有 HTTP 應答（即使 4xx）＝服務活著
        except Exception:
            return False

    def rpc(self, tool, arguments, timeout=150):
        """tools/call 通用執行（原 server.mcp_raw，行为一致）：回覆 {ok,data,detail,login_bad[,dead,error]}"""
        body = json.dumps({"jsonrpc": "2.0", "id": int(time.time() * 1000) % 10 ** 9,
                           "method": "tools/call",
                           "params": {"name": tool, "arguments": arguments}}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except Exception as e:
            return {"ok": False, "dead": True,
                    "error": "MCP 連線失敗：%s → 先喺 tools\\xiaohongshu-mcp\\ 啟動 xiaohongshu-mcp-fixed-windows-amd64.exe -headless=false" % str(e)[:90]}
        payload = None
        if raw.lstrip().startswith("{"):
            try:
                payload = json.loads(raw)
            except Exception:
                pass
        if payload is None:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    try:
                        payload = json.loads(line[5:].strip())
                    except Exception:
                        pass
        if payload is None or "error" in payload:
            return {"ok": False, "error": (json.dumps(payload.get("error"), ensure_ascii=False)[:200]
                                           if payload else raw[:150])}
        res = payload.get("result", {})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        data = None
        try:
            data = json.loads(text)
        except Exception:
            pass
        login_bad = ("未登录" in text) or ("请先登录" in text) or ("登录已失效" in text) or ("登录失效" in text)
        failed = ("失败" in text[:40]) or ("失敗" in text[:40])
        return {"ok": (not res.get("isError", False)) and not login_bad and not failed,
                "data": data, "detail": text[:250], "login_bad": login_bad}

    def ops(self):
        def _fid(note_id, tok=None):
            a = {"feed_id": note_id}
            if tok:
                a["xsec_token"] = tok
            return a

        def op_note(note_id, xsec_token=None, **kw):
            return self.rpc("get_feed_detail", _fid(note_id, xsec_token), timeout=90)

        def op_like(note_id, xsec_token=None, **kw):
            return self.rpc("like_feed", _fid(note_id, xsec_token))

        def op_collect(note_id, xsec_token=None, **kw):
            return self.rpc("favorite_feed", _fid(note_id, xsec_token))

        def op_comment(note_id, text, xsec_token=None, **kw):
            a = _fid(note_id, xsec_token)
            a["content"] = text
            return self.rpc("post_comment_to_feed", a)

        def op_reply(comment_id, text, **kw):
            return self.rpc("reply_notification", {"comment_id": comment_id, "content": text})

        def op_search(keyword, **kw):
            return self.rpc("search_feeds", {"keyword": keyword}, timeout=180)

        def op_notifs(tab="mentions", limit=10, **kw):
            return self.rpc("list_notifications", {"tab": tab, "limit": int(limit)})

        def op_publish(title="", text="", images=None, tags=None, schedule_at="", **kw):
            a = {"title": title or "", "content": text, "images": images or [], "tags": tags or []}
            if schedule_at:
                a["schedule_at"] = str(schedule_at).replace("T", " ")[:19]
            return self.rpc("publish_content", a, timeout=180)

        return {
            OP_LOGIN:   lambda **kw: self.rpc("check_login_status", {}, timeout=30),
            OP_FEEDS:   lambda **kw: self.rpc("list_feeds", {}, timeout=120),
            OP_SEARCH:  op_search,
            OP_NOTE:    op_note,
            OP_LIKE:    op_like,
            OP_COLLECT: op_collect,
            OP_COMMENT: op_comment,
            OP_REPLY:   op_reply,
            OP_UNREAD:  lambda **kw: self.rpc("get_unread_count", {}, timeout=60),
            OP_NOTIFS:  op_notifs,
            OP_PUBLISH: op_publish,
        }


# ═══════════════ Driver 2：本地瀏覽器 CDP（Chrome :9333 · tools/cdp_tool.py） ═══════════════
# 小紅書網頁結構正典（2026-11-22 實測）：列表 /chat＝.xhs-im-conv-item（暱稱/時間/預覽/未讀）；
# 打開後 URL＝/chat/<user_id>；氣泡 content--left＝對方／--right＝自己，文字 .xhs-im-bubble__text；
# 輸入＝contenteditable .xhs-im-input-bar-editor（insertText 入字，Enter 發送，再撃 .xhs-im-input-bar-action-btn）。
# 刪除：創作平台「筆記管理」按標題 hover→刪除→確認。

JS_CONV_LIST = (
    "(()=>{const its=[...document.querySelectorAll('.xhs-im-conv-item')];"
    "return JSON.stringify({count:its.length,items:its.slice(0,14).map((e,i)=>{"
    "const l=(e.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean);"
    "let unread=0;if(l.length>2&&/^\\d+$/.test(l[l.length-1])){unread=parseInt(l.pop(),10)||0}"
    "return{i,name:l[0]||'',time:l[1]||'',prev:l.slice(2).join(' ').slice(0,80),unread}})})})()")

def js_click_conv(i):
    return ("(()=>{const its=[...document.querySelectorAll('.xhs-im-conv-item')];"
            "if(!its[%d])return JSON.stringify({err:'NO_ITEM'});its[%d].click();return JSON.stringify({ok:1})})()") % (i, i)

JS_READ_THREAD = (
    "(async()=>{await new Promise(r=>setTimeout(r,1500));"
    "const m=location.href.match(/\\/chat\\/([0-9a-zA-Z_-]+)/);"
    "const nm=(document.querySelector('.xhs-im-chat-window__header-name')||{}).innerText||'';"
    "const wrap=document.querySelector('.xhs-im-msg-list');const msgs=[];let t='';"
    "if(wrap){for(const el of wrap.children){const cls=String(el.className||'');"
    "if(/time-divider/.test(cls)){t=(el.innerText||'').trim();continue}"
    "const right=!!(el.querySelector('[class*=\"content--right\"]')||/content--right/.test(cls));"
    "const has=el.matches('[class*=chat-item]')||!!el.querySelector('[class*=chat-item__content]');"
    "if(!has)continue;"
    "let tx='';const tb=el.querySelector('.xhs-im-bubble__text');"
    "if(tb)tx=(tb.innerText||'').trim();"
    "if(!tx&&el.querySelector('.xhs-im-bubble__emoji'))tx='[表情]';"
    "if(!tx)tx=(el.innerText||'').trim();"
    "msgs.push({dir:right?'out':'in',t:t.slice(0,20),tx:tx.slice(0,300)})}}"
    "return JSON.stringify({uid:m?m[1]:'',name:nm.trim().slice(0,30),msgs:msgs.filter(o=>o.tx)})})()")

JS_PREPARE = (
    "(()=>{const e=document.querySelector('.xhs-im-input-bar-editor');if(!e)return JSON.stringify({err:'NO_EDITOR'});"
    "e.scrollIntoView();e.click();const r=document.createRange();r.selectNodeContents(e);r.collapse(false);"
    "const s=getSelection();s.removeAllRanges();s.addRange(r);e.focus();return JSON.stringify({ok:1})})()")

def js_finish(expected_json):
    return (
        "(async()=>{const wait=ms=>new Promise(r=>setTimeout(r,ms));"
        "const e=document.querySelector('.xhs-im-input-bar-editor');"
        "let left=((e&&e.innerText)||'').trim();"
        "if(left){const mk=t=>new KeyboardEvent(t,{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true});"
        "if(e){e.dispatchEvent(mk('keydown'));e.dispatchEvent(mk('keyup'))}await wait(1600);left=((e&&e.innerText)||'').trim()}"
        "if(left){const bs=[...document.querySelectorAll('.xhs-im-input-bar-action-btn')].filter(b=>{const r=b.getBoundingClientRect();return r.width>4&&r.height>4});"
        "if(bs.length){bs[bs.length-1].click();await wait(1600);left=((e&&e.innerText)||'').trim()}}"
        "const ms=[...document.querySelectorAll('.xhs-im-msg-list [class*=\"content--right\"] .xhs-im-bubble__text')].map(x=>(x.innerText||'').trim());"
        "return JSON.stringify({sent:!left,last:(ms[ms.length-1]||'').slice(0,40),left:left.slice(0,40),expect:" + expected_json + "})})()")

JS_NOTE_DELETE = [
    "(function(){var t=%s;var els=[...document.querySelectorAll('div,span,a,h3,p')].filter(e=>!e.children.length&&(e.textContent||'').trim()===t);if(!els.length)return 'NOTFOUND';var c=els[0];for(var i=0;i<9&&c;i++){c=c.parentElement;if(c&&/note|card|item|content/i.test(c.className||''))break;}c=c||els[0];['pointerover','mouseover','mouseenter','mousemove'].forEach(n=>c.dispatchEvent(new MouseEvent(n,{bubbles:true})));return 'HOVERED';})()",
    "(function(){var bs=[...document.querySelectorAll('button,span,a,div,svg')].filter(e=>((e.getAttribute&&(e.getAttribute('title')||e.getAttribute('aria-label'))||'')==='删除'||(e.childElementCount===0&&(e.textContent||'').trim()==='删除')));if(!bs.length)return 'NODELETEBTN';bs[bs.length-1].click();return 'CLICKEDDEL';})()",
    "(function(){var bs=[...document.querySelectorAll('button,div[class*=btn],span')].filter(b=>/^(确定|确认|确认删除|删除)$/.test((b.textContent||'').trim()));if(!bs.length)return 'NOCONFIRM';bs[bs.length-1].click();return 'CONFIRMED';})()",
]


class CDPDriver(Driver):
    name = "cdp"
    label = "本地瀏覽器（CDP :9333）"

    def __init__(self, root, cookies_path):
        self.root = root
        self.cookies = cookies_path

    # ── 底層原語（原 server.cdp_up/cdp_boot/cdp_evals，行为一致）──
    def up(self):
        try:
            json.load(urllib.request.urlopen("http://localhost:9333/json", timeout=3))
            return True
        except Exception:
            return False

    def online(self):
        return self.up()

    def boot(self):
        if self.up():
            return True
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "xiaohongshu-mcp", "browser")
        try:
            vers = sorted(os.listdir(base), reverse=True)
        except Exception:
            return False
        for v in vers:
            exe = os.path.join(base, v, "browser", "chrome.exe")
            if os.path.isfile(exe):
                try:
                    subprocess.Popen([exe, "--remote-debugging-port=9333",
                                      "--user-data-dir=" + os.path.join(self.root, "tools", ".chrome-edit"),
                                      "--no-first-run", "--no-default-browser-check"])
                except Exception:
                    return False
                for _ in range(25):
                    if self.up():
                        return True
                    time.sleep(0.8)
                return False
        return False

    def evals(self, steps, timeout=240):
        """行 cdp_tool.py steps → (vals, err)。併發安全：每次用獨立臨時 steps 檔。"""
        if not self.boot():
            return None, "CDP 瀏覽器（Chrome 9333）未啟動且自動開啟失敗——啟動方法見 AGENTS §17"
        sf = os.path.join(self.root, "tools", "_cdp_%d_%d.json" % (os.getpid(), int(time.time() * 1000) % 10 ** 6))
        try:
            with open(sf, "w", encoding="utf-8") as f:
                json.dump(steps, f, ensure_ascii=False)
            out = subprocess.run([sys.executable, "-I", os.path.join(self.root, "tools", "cdp_tool.py"), sf],
                                 capture_output=True, text=True, timeout=timeout)
            blob = (out.stdout or "") + "\n" + (out.stderr or "")
        except Exception as e:
            return None, "CDP 執行失敗：%s" % str(e)[:110]
        finally:
            try:
                os.remove(sf)
            except Exception:
                pass
        if "endpoint not found" in blob:
            return None, "Chrome 9333 冇反應——請先啟動瀏覽器（§17）"
        vals = []
        for line in blob.splitlines():
            if not line.startswith("EVAL_RESULT:"):
                continue
            raw = line[len("EVAL_RESULT:"):].strip()
            try:
                v = json.loads(raw)
                if isinstance(v, str) and v.strip()[:1] in "{[":
                    try:
                        v = json.loads(v)
                    except Exception:
                        pass
                vals.append(v)
            except Exception:
                vals.append(raw)
        return vals, None

    def _head(self, url):
        return [{"action": "set_cookies", "file": self.cookies},
                {"action": "goto", "url": url}, {"action": "wait", "s": 6}]

    # ── 操作實作 ──
    def op_dm_conversations(self, top=14, **kw):
        vals, err = self.evals(self._head("https://www.xiaohongshu.com/chat") +
                               [{"action": "eval", "expr": JS_CONV_LIST}])
        if err:
            return {"ok": False, "error": err}
        lst = next((v for v in (vals or []) if isinstance(v, dict) and "items" in v), None)
        if not lst:
            return {"ok": False, "error": "讀唔到私信列表（未登入／結構變咗）——試重新掃碼（§11A.E）"}
        items = (lst.get("items") or [])[:max(1, min(int(top or 8), 14))]
        return {"ok": True, "data": {"count": lst.get("count", len(items)), "items": items}}

    def op_dm_read(self, items=None, **kw):
        steps = self._head("https://www.xiaohongshu.com/chat")
        for it in (items or []):
            steps += [{"action": "eval", "expr": js_click_conv(it.get("i", 0))}, {"action": "wait", "s": 4},
                      {"action": "eval", "expr": JS_READ_THREAD}]
        vals, err = self.evals(steps, timeout=110 + 30 * max(1, len(items or [])))
        if err:
            return {"ok": False, "error": err}
        reads = [v for v in (vals or []) if isinstance(v, dict) and "msgs" in v]
        return {"ok": True, "data": {"reads": reads}}

    def op_dm_send(self, uid, text, **kw):
        steps = self._head("https://www.xiaohongshu.com/chat/%s" % uid) + [
            {"action": "eval", "expr": JS_PREPARE}, {"action": "wait", "s": 1.0},
            {"action": "insert_text", "text": text}, {"action": "wait", "s": 1.0},
            {"action": "eval", "expr": js_finish(json.dumps(text, ensure_ascii=False))}]
        vals, err = self.evals(steps)
        if err:
            return {"ok": False, "error": err}
        if any(isinstance(v, dict) and v.get("err") == "NO_EDITOR" for v in (vals or [])):
            return {"ok": False, "error": "輸入框未搵到（未登入／頁面結構變咗）"}
        chk = next((v for v in (vals or []) if isinstance(v, dict) and "sent" in v), None) or {}
        if not chk.get("sent"):
            return {"ok": False, "error": "發送未確認（輸入框仍剩「%s」）——未寫入紀錄，試喺 App 手動覆"
                                           % (chk.get("left") or "?")[:30]}
        return {"ok": True, "data": chk}

    def op_delete(self, title, **kw):
        steps = [{"action": "set_cookies", "file": self.cookies},
                 {"action": "goto", "url": "https://creator.xiaohongshu.com/new/note-manager"},
                 {"action": "wait", "s": 7},
                 {"action": "eval", "expr": JS_NOTE_DELETE[0] % json.dumps(title, ensure_ascii=False)},
                 {"action": "wait", "s": 1.8},
                 {"action": "eval", "expr": JS_NOTE_DELETE[1]},
                 {"action": "wait", "s": 1.8},
                 {"action": "eval", "expr": JS_NOTE_DELETE[2]}]
        vals, err = self.evals(steps, timeout=150)
        if err:
            return {"ok": False, "detail": "%s（請喺小紅書 App／創作平台手動刪除）" % err}
        blob = json.dumps(vals, ensure_ascii=False) if vals is not None else ""
        done = "CONFIRMED" in blob
        return {"ok": done,
                "detail": ("已喺創作平台嘗試刪除（CONFIRMED）——請稍後喺 App 核實" if done
                           else "平台自動刪除未完成（%s）——請喺小紅書 App／創作平台手動刪除該帖" % blob[-160:])}

    def ops(self):
        return {
            OP_DELETE:  self.op_delete,
            OP_DM_CONV: self.op_dm_conversations,
            OP_DM_READ: self.op_dm_read,
            OP_DM_SEND: self.op_dm_send,
        }


# ═══════════════ 路由器 ═══════════════
class XHS(object):
    """統一入口：XHS.call(OP_*, **kw)。揀 driver＝優先序 × 能力；全部調用寫日志＋異常兜底。"""

    def __init__(self, drivers, log=None):
        self.drivers = list(drivers)
        self._log = log or (lambda m: None)

    def by_name(self, name):
        return next((d for d in self.drivers if d.name == name), None)

    def route(self, op):
        for d in self.drivers:
            if d.supports(op):
                return d
        return None

    def can(self, op):
        return self.route(op) is not None

    def call(self, op, **kw):
        d = self.route(op)
        if d is None:
            self._log("[xhs] %s ✗ 冇任何驅動支持呢個操作" % op)
            return {"ok": False, "driver": None,
                    "error": "冇驅動支持操作「%s」——新渠道請喺 xhs_core 加 Driver（§11A.K）" % op}
        t0 = time.time()
        try:
            r = d.ops()[op](**kw)
        except Exception as e:
            r = {"ok": False, "error": "%s 驅動執行「%s」異常：%s" % (d.name, op, str(e)[:160])}
        if not isinstance(r, dict):
            r = {"ok": bool(r), "data": r}
        r.setdefault("driver", d.name)
        dt = time.time() - t0
        self._log("[xhs] %s via=%s %s（%.1fs）" % (op, d.name,
                    "ok" if r.get("ok") else "err：" + str(r.get("error") or r.get("detail") or "?")[:130], dt))
        return r

    def status(self):
        onl = {d.name: bool(d.online()) for d in self.drivers}   # 每個驅動只探測一次
        out = {"ok": True,
               "priority": [d.name for d in self.drivers],
               "op_label": dict(OP_LABEL),
               "drivers": [{"name": d.name, "label": d.label, "online": onl[d.name],
                            "ops": sorted(d.supported())} for d in self.drivers],
               "routes": [{"op": op,
                           "driver": (self.route(op).name if self.route(op) else None),
                           "online": (onl.get(self.route(op).name) if self.route(op) else None)}
                          for op in KNOWN_OPS]}
        return out


def supported(self):
    return sorted(self.ops().keys())


Driver.supported = supported
