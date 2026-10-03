"""Fake Telegram Bot API transport so real handlers run end-to-end without network."""
import itertools
import json
import time

from telegram import Update
from telegram.request import BaseRequest


class FakeRequest(BaseRequest):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.files: dict[str, bytes] = {}
        self._mid = itertools.count(1000)

    @property
    def read_timeout(self):
        return 5

    async def initialize(self): pass
    async def shutdown(self): pass

    async def do_request(self, url, method, request_data=None, read_timeout=None, write_timeout=None,
                         connect_timeout=None, pool_timeout=None):
        if method == "GET" and "/file/bot" in url:
            return 200, self.files[url.rsplit("/", 1)[-1]]
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        if request_data is not None and request_data.contains_files:
            params["__files__"] = [(v[0], v[1]) for v in request_data.multipart_data.values()]
        self.calls.append((name, params))
        chat = {"id": int(params.get("chat_id", 0) or 0), "type": "private"}
        base = {"message_id": next(self._mid), "date": int(time.time()), "chat": chat}
        if name == "getMe":
            res = {"id": 123, "is_bot": True, "first_name": "bot", "username": "tabot"}
        elif name in ("sendMessage", "editMessageText"):
            res = {**base, "text": params.get("text", "")}
            if name == "editMessageText":
                res["message_id"] = int(params.get("message_id", res["message_id"]))
        elif name == "sendDocument":
            res = {**base, "document": {"file_id": "d", "file_unique_id": "d"}}
        elif name == "getFile":
            fid = params["file_id"]
            res = {"file_id": fid, "file_unique_id": fid, "file_size": len(self.files[fid]),
                   "file_path": f"https://api.telegram.org/file/bot123:ABC/{fid}"}
        else:
            res = True
        return 200, json.dumps({"ok": True, "result": res}).encode()

    # -- helpers for tests --
    def texts(self, chat_id=None):
        return [p.get("text", "") for n, p in self.calls if n in ("sendMessage", "editMessageText")
                and (chat_id is None or int(p.get("chat_id", 0)) == chat_id)]

    def doc_names(self, chat_id=None):
        out = []
        for n, p in self.calls:
            if n == "sendDocument" and (chat_id is None or int(p["chat_id"]) == chat_id):
                out.extend(f[0] for f in p["__files__"])
        return out

    def last_buttons(self):
        for n, p in reversed(self.calls):
            if n == "sendMessage" and p.get("reply_markup"):
                rm = p["reply_markup"]
                rm = rm.to_dict() if hasattr(rm, "to_dict") else (rm if isinstance(rm, dict) else json.loads(rm))
                return [b["callback_data"] for row in rm["inline_keyboard"] for b in row if "callback_data" in b]
        return []


class Client:
    """Simulates one Telegram user."""
    def __init__(self, app, req, uid, name="TA"):
        self.app, self.req, self.uid, self.name, self._n = app, req, uid, name, itertools.count(1)
        self.user = {"id": uid, "is_bot": False, "first_name": name}
        self.chat = {"id": uid, "type": "private"}

    def _msg(self, **extra):
        return {"message_id": next(self._n) + self.uid * 1000, "date": int(time.time()), "chat": self.chat, "from": self.user, **extra}

    async def _send(self, payload):
        await self.app.process_update(Update.de_json({"update_id": next(self._n) + self.uid * 100000, **payload}, self.app.bot))

    async def say(self, text, reply_to_text=None):
        extra = {"text": text}
        if text.startswith("/"):
            extra["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if reply_to_text:
            extra["reply_to_message"] = {"message_id": 1, "date": 1, "chat": self.chat, "text": reply_to_text,
                                         "from": {"id": 123, "is_bot": True, "first_name": "bot"}}
        await self._send({"message": self._msg(**extra)})

    async def tap(self, data):
        await self._send({"callback_query": {"id": str(next(self._n)), "from": self.user, "chat_instance": "1", "data": data,
                                             "message": {"message_id": 5, "date": 1, "chat": self.chat, "text": "x"}}})

    async def send_file(self, name, data):
        fid = f"f{next(self._n)}"
        self.req.files[fid] = data
        await self._send({"message": self._msg(document={"file_id": fid, "file_unique_id": fid, "file_name": name, "file_size": len(data)})})
