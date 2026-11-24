import json
import os
import time

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.web import request as web_request
    from astrbot.api.web import json_response, error_response
except Exception:  # older/newer API differences: fall back to quart
    web_request = None
    json_response = None
    error_response = None

PLUGIN_NAME = "work_bridge"
DATA_DIR = os.path.join("data", "plugin_data", PLUGIN_NAME)
QUEUE_PATH = os.path.join(DATA_DIR, "queue.json")
MAX_RESULT = 8000


@register(PLUGIN_NAME, "wistar", "干活桥：聊天里 /work 派活，外部工人（.82 上的 opencode）轮询取活干完回传结果", "1.0.0")
class WorkBridge(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        if not self._cfg("work_token"):
            try:
                path = os.path.join("data", "config", PLUGIN_NAME + "_config.json")
                with open(path, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
            except Exception:
                pass
        self._queue = None
        try:
            self.context.register_web_api(
                "/" + PLUGIN_NAME + "/poll", self.api_poll, ["GET", "POST"], "工人取一件待办任务"
            )
            self.context.register_web_api(
                "/" + PLUGIN_NAME + "/done", self.api_done, ["POST"], "工人回传任务结果"
            )
            self.context.register_web_api(
                "/" + PLUGIN_NAME + "/status", self.api_status, ["GET", "POST"], "最近任务状态"
            )
        except Exception as e:
            print("[work_bridge] register_web_api failed:", e)

    def _cfg(self, key):
        try:
            value = self.config.get(key, "")
        except Exception:
            value = ""
        return (value or "").strip()

    # ---------- queue storage ----------
    def _load(self):
        if self._queue is None:
            try:
                with open(QUEUE_PATH, "r", encoding="utf-8") as f:
                    self._queue = json.load(f)
            except Exception:
                self._queue = []
        return self._queue

    def _save(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(QUEUE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._queue or [], f, ensure_ascii=False, indent=1)
        except Exception as e:
            print("[work_bridge] save queue failed:", e)

    def _enqueue(self, text, umo, sender):
        q = self._load()
        task = {
            "id": int(time.time() * 1000),
            "text": text,
            "umo": umo,
            "sender": sender,
            "status": "pending",
            "result": "",
            "created_at": int(time.time()),
            "finished_at": 0,
        }
        q.append(task)
        if len(q) > 100:
            del q[: len(q) - 100]
        self._save()
        return task

    # ---------- permission ----------
    def _sender_id(self, event):
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    def _is_allowed(self, event):
        allowed = [s.strip() for s in self._cfg("admin_senders").split(",") if s.strip()]
        sid = self._sender_id(event)
        if allowed:
            return sid in allowed
        try:
            f = getattr(event, "is_admin", None)
            if callable(f):
                return bool(f())
        except Exception:
            pass
        return False

    # ---------- chat commands ----------
    @filter.command("work")
    async def work(self, event: AstrMessageEvent):
        """派活给 .82 的工人：/work 要干的事"""
        raw = ""
        try:
            raw = event.message_str or ""
        except Exception:
            raw = ""
        text = raw.strip()
        if text.startswith("/work"):
            text = text[len("/work"):].strip()
        if not self._is_allowed(event):
            yield event.plain_result(
                "未授权派活。你的发送者ID是 " + (self._sender_id(event) or "(未知)")
                + "，让管理员把它加进 work_bridge 插件配置的 admin_senders 后再试。"
            )
            return
        if not text:
            yield event.plain_result("用法：/work 要干的事（例如 /work 把 C:\\opencode\\work 下的日志打包）")
            return
        task = self._enqueue(text, event.unified_msg_origin, self._sender_id(event))
        yield event.plain_result(
            "已派活 #" + str(task["id"]) + "：" + text[:80] + "\n工人干完会把结果发回这里。"
        )

    @filter.command("work_status")
    async def work_status(self, event: AstrMessageEvent):
        """看最近派活的进度"""
        q = self._load()
        if not q:
            yield event.plain_result("还没有派过活。用 /work 要干的事 派第一件。")
            return
        lines = ["最近任务（新→旧）："]
        for t in q[-6:][::-1]:
            line = "- #" + str(t["id"]) + "｜" + t["status"] + "｜" + t["text"][:40]
            if t["status"] in ("done", "failed") and t.get("result"):
                line += "\n  结果：" + t["result"][:120].replace("\n", " ")
            lines.append(line)
        yield event.plain_result("\n".join(lines))

    # ---------- web api ----------
    async def _read_body(self):
        params = {}
        headers = {}
        req = web_request
        try:
            from quart import request as qreq
            req = qreq
        except Exception:
            pass
        if req is not None:
            try:
                headers = req.headers or {}
            except Exception:
                headers = {}
            try:
                args = req.args or {}
                for k in args.keys():
                    params[k] = args.get(k)
            except Exception:
                pass
            try:
                form = await req.form
                for k in form.keys():
                    params[k] = form.get(k)
            except Exception:
                pass
            data = None
            try:
                data = await req.get_json(force=False, silent=True)
            except Exception:
                data = None
            if not isinstance(data, dict):
                try:
                    raw = await req.get_data()
                    if raw:
                        data = json.loads(raw.decode("utf-8", "ignore"))
                except Exception:
                    data = None
            if isinstance(data, dict):
                params.update(data)
        return params, headers

    def _ok(self, payload):
        if json_response is not None:
            return json_response(payload)
        return payload

    def _err(self, msg, status=400):
        if error_response is not None:
            try:
                return error_response(msg, status=status)
            except Exception:
                return error_response(msg)
        return {"ok": False, "error": msg}

    def _check_token(self, body, headers):
        token = self._cfg("work_token")
        given = ""
        try:
            given = headers.get("X-Work-Token", "") or ""
        except Exception:
            given = ""
        if not given:
            given = str(body.get("token", "") or "")
        if not token:
            return "plugin token not configured"
        if given != token:
            return "invalid token"
        return ""

    async def api_poll(self):
        body, headers = await self._read_body()
        err = self._check_token(body, headers)
        if err:
            return self._err(err, status=403)
        q = self._load()
        for t in q:
            if t["status"] == "pending":
                t["status"] = "running"
                t["started_at"] = int(time.time())
                self._save()
                return self._ok({"ok": True, "task": {"id": t["id"], "text": t["text"]}})
        return self._ok({"ok": True, "task": None})

    async def api_done(self):
        body, headers = await self._read_body()
        err = self._check_token(body, headers)
        if err:
            return self._err(err, status=403)
        try:
            tid = int(body.get("id", 0) or 0)
        except Exception:
            tid = 0
        result = str(body.get("result", "") or "")[:MAX_RESULT]
        ok = body.get("ok", True)
        q = self._load()
        task = None
        for t in q:
            if t["id"] == tid:
                task = t
                break
        if task is None:
            return self._err("task not found: " + str(tid), status=404)
        task["status"] = "done" if ok else "failed"
        task["result"] = result
        task["finished_at"] = int(time.time())
        self._save()
        umo = task.get("umo") or ""
        if umo:
            head = "✅ 活干完了 #" if task["status"] == "done" else "⚠️ 活没干成 #"
            msg = (
                head + str(task["id"]) + "：" + task["text"][:80]
                + "\n—— 结果 ——\n" + (result[:3000] if result else "(无输出)")
            )
            try:
                await self.context.send_message(umo, MessageChain().message(msg))
            except Exception as e:
                print("[work_bridge] push result failed:", e)
        return self._ok({"ok": True, "id": tid, "status": task["status"]})

    async def api_status(self):
        body, headers = await self._read_body()
        err = self._check_token(body, headers)
        if err:
            return self._err(err, status=403)
        q = self._load()
        items = []
        for t in q[-8:][::-1]:
            items.append({
                "id": t["id"],
                "status": t["status"],
                "text": t["text"][:80],
                "result": (t.get("result") or "")[:200],
                "sender": t.get("sender", ""),
            })
        return self._ok({"ok": True, "tasks": items})
