"""Fake Telegram Bot API transport so real handlers run end-to-end without network."""
import itertools
import json
import time

from telegram import Update
from telegram.request import BaseRequest


class FakeRequest(BaseRequest):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.sent: list[tuple[int, int, str, str]] = []   # (message_id, chat_id, method, text/caption) for new messages only
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
        if name in ("sendMessage", "sendDocument"):
            self.sent.append((base["message_id"], chat["id"], name, params.get("text") or params.get("caption") or ""))
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

    @staticmethod
    def _buttons_of(p):
        rm = p.get("reply_markup")
        if not rm:
            return []
        rm = rm.to_dict() if hasattr(rm, "to_dict") else (rm if isinstance(rm, dict) else json.loads(rm))
        return [b["callback_data"] for row in rm["inline_keyboard"] for b in row if "callback_data" in b]

    def last_buttons(self):
        """Buttons of the most recent message that carried a keyboard (new or edited)."""
        for n, p in reversed(self.calls):
            if n in ("sendMessage", "sendDocument", "editMessageText") and p.get("reply_markup"):
                return self._buttons_of(p)
        return []

    def buttons_of_message(self, message_id):
        """Current keyboard of a message (latest send/edit wins)."""
        out = []
        for n, p in self.calls:
            if n == "editMessageText" and int(p.get("message_id", 0)) == message_id:
                out = self._buttons_of(p)
        return out

    def msg_id(self, fragment, chat_id=None, nth=-1):
        """Id of a sent (new) message whose text/caption contains `fragment`."""
        hits = [m for m in self.sent if fragment in m[3] and (chat_id is None or m[1] == chat_id)]
        return hits[nth][0]

    def count_sent(self, chat_id=None, since=0):
        """Number of NEW bot messages (sendMessage + sendDocument) after the first `since` entries of `sent`."""
        return len([m for m in self.sent[since:] if chat_id is None or m[1] == chat_id])

    def card_text(self, message_id):
        """Latest text of a message (initial send, then any edits)."""
        text = next((m[3] for m in self.sent if m[0] == message_id), "")  # (messages the tests only "tap" have no send)
        for n, p in self.calls:
            if n == "editMessageText" and int(p.get("message_id", 0)) == message_id:
                text = p.get("text", text)
        return text


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

    async def say(self, text, reply_to_text=None, reply_to_caption=None):
        extra = {"text": text}
        if text.startswith("/"):
            extra["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if reply_to_text:
            extra["reply_to_message"] = {"message_id": 1, "date": 1, "chat": self.chat, "text": reply_to_text,
                                         "from": {"id": 123, "is_bot": True, "first_name": "bot"}}
        elif reply_to_caption:  # a delivered item is a document: its tag is in the caption
            extra["reply_to_message"] = {"message_id": 1, "date": 1, "chat": self.chat, "caption": reply_to_caption,
                                         "document": {"file_id": "d", "file_unique_id": "d"},
                                         "from": {"id": 123, "is_bot": True, "first_name": "bot"}}
        await self._send({"message": self._msg(**extra)})

    async def tap(self, data, msg_id=5):
        """Tap an inline button. `msg_id` is the message the button belongs to (matters for cards edited in place)."""
        await self._send({"callback_query": {"id": str(next(self._n)), "from": self.user, "chat_instance": "1", "data": data,
                                             "message": {"message_id": msg_id, "date": 1, "chat": self.chat, "text": "x"}}})

    async def send_file(self, name, data):
        fid = f"f{next(self._n)}"
        self.req.files[fid] = data
        await self._send({"message": self._msg(document={"file_id": fid, "file_unique_id": fid, "file_name": name, "file_size": len(data)})})
