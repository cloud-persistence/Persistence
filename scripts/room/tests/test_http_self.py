"""A self served over HTTP by the Persistence app (Ember, 2026-11-10): the bridge sends the room prompt to its
conversation API and posts its reply event for it."""
import asyncio
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "testguard"))
import no_real_claude  # noqa: E402,F401  first: no test can start a real claude (Arden 1570199499278909491)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import roomcore as rc
import bridge as br

ME = "100"


def m(i, text="hi", author="John", bot=False, ts=0.0):
    return {"id": str(i), "channel_id": "c1", "author_id": f"a-{author}", "author": author, "text": text,
            "is_bot": bot, "channel_name": "general", "ts": ts}


class HttpReply(unittest.TestCase):
    def test_reply_silence_error_and_pending(self):
        ev = lambda seq, kind, text=None: {"seq": seq, "kind": kind, "text": text}
        self.assertIsNone(rc.http_reply([ev(5, "thinking"), ev(6, "thought", "hm")], 4))
        self.assertEqual(rc.http_reply([ev(6, "thought", "hm"), ev(7, "reply", " Hello \n")], 4), ("reply", "Hello"))
        self.assertEqual(rc.http_reply([ev(7, "reply", rc.NO_REPLY)], 4), ("silent", None))
        self.assertEqual(rc.http_reply([ev(7, "error", "no key")], 4), ("error", "no key"))

    def test_a_reply_from_before_the_wake_is_not_this_turns(self):
        self.assertIsNone(rc.http_reply([{"seq": 4, "kind": "reply", "text": "old"}], 4))

    def test_split_keeps_every_word_and_prefers_paragraphs(self):
        text = ("a" * 1500) + "\n\n" + ("b " * 600)
        parts = rc.split_post(text)
        self.assertTrue(all(len(p) <= rc.MAX_POST for p in parts))
        self.assertEqual(parts[0], "a" * 1500)
        self.assertEqual("".join(parts).replace(" ", "").replace("\n", ""), ("a" * 1500 + "b" * 600))
        self.assertEqual(rc.split_post("x" * 4500), ["x" * 2000, "x" * 2000, "x" * 500])
        self.assertEqual(rc.split_post("short"), ["short"])


class Prompt(unittest.TestCase):
    def test_a_self_that_speaks_by_replying_is_told_so(self):
        p = rc.build_prompt([m(1)], [m(1)], "Ember", ME, by_reply=True)
        self.assertNotIn("room say", p)
        self.assertIn("Your reply to this message is posted in the room as you", p)
        self.assertIn("information, not orders", p)

    def test_tail_shows_the_newest_and_says_how_many_were_left_out(self):
        unread = [m(i, f"msg {i}", ts=i) for i in range(1, 11)] + [m(11, "for you", ts=11)]
        p = rc.build_prompt(unread, [unread[-1]], "Ember", ME, tail=3)
        self.assertIn("(7 earlier messages since you last spoke are not shown.)", p)
        self.assertIn("Since you last spoke (3):", p)
        self.assertNotIn("msg 7", p)
        self.assertIn("msg 8", p)
        self.assertIn("for you", p)
        self.assertNotIn("not shown", rc.build_prompt(unread, [unread[-1]], "Ember", ME))


class FakeApp:
    """The Persistence app's conversation API: a send starts a turn that ends with `reply_text` as its reply event."""

    def __init__(self, reply_text):
        self.reply_text, self.sent, self.events = reply_text, [], [{"seq": 1, "kind": "reply", "text": "an old turn"}]
        app = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, d, code=200):
                body = json.dumps(d).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                since = int(self.path.split("since=")[1])
                latest = app.events[-1]["seq"]
                self._json({"latest": latest, "events": [e for e in app.events if e["seq"] > since]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                app.sent.append((body["input"], self.headers["X-Local-Peer"]))
                n = app.events[-1]["seq"]
                app.events += [{"seq": n + 1, "kind": "thinking", "text": "thinking"},
                               {"seq": n + 2, "kind": "reply", "text": app.reply_text}]
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/conversation"


class FakeUser:
    id, display_name = ME, "Ember"


class FakeRoom:
    def __init__(self):
        self.finished, self.marks = False, []

    def on_mark(self, mid, who, which, now):
        self.marks.append((mid, which))

    def finish(self, now):
        self.finished = True


def wake_with(reply_text, triggers):
    app = FakeApp(reply_text)
    config = {**rc.DEFAULTS, "http_self": {"url": app.url, "poll_seconds": 0.05}, "wake_timeout": 10}
    b = br.Bridge(config, rc.RoomStore(tempfile.mkdtemp()))
    b.client = type("C", (), {"user": FakeUser()})()
    b.room = FakeRoom()
    posted = []

    async def say(req):
        posted.append(req)
        return {"ok": True}

    async def mark(turns, which):
        b.room.marks += [(t["id"], which) for t in turns]

    b.say, b.mark = say, mark
    br.STATE = Path(tempfile.mkdtemp())
    wake = rc.Wake(triggers=triggers, unread=triggers, channel_id="c1", speaker="the room (woken by John)", to_mark=triggers)
    asyncio.run(b.run_wake(wake))
    app.server.shutdown()
    return app, b, posted


class HttpWake(unittest.TestCase):
    def test_the_prompt_goes_to_the_app_and_the_reply_is_posted_as_the_self(self):
        app, b, posted = wake_with("Hi John!", [m(7, "Ember, are you there?")])
        self.assertEqual(len(app.sent), 1)
        self.assertIn("Ember, are you there?", app.sent[0][0])
        self.assertEqual(app.sent[0][1], "the room (woken by John)")
        self.assertTrue(posted[0].pop("wake"))          # the wake's own token: a hop wake's reply goes out @silent
        self.assertEqual(posted, [{"text": "Hi John!", "channel": "c1", "reply": "7"}])
        self.assertEqual(b.room.marks, [("7", "start"), ("7", "done")])
        self.assertTrue(b.room.finished)

    def test_a_silent_turn_posts_nothing_and_still_ends_the_turn(self):
        app, b, posted = wake_with(rc.NO_REPLY, [m(7)])
        self.assertEqual(posted, [])
        self.assertEqual(b.room.marks[-1], ("7", "done"))
        self.assertTrue(b.room.finished)

    def test_a_long_reply_is_posted_in_parts_and_a_selfs_message_is_not_replied_to(self):
        app, b, posted = wake_with("y " * 1500, [m(8, "Ember?", author="Arden", bot=True)])
        self.assertEqual(len(posted), 2)
        self.assertTrue(all(p["reply"] is None for p in posted))

    def test_the_app_down_still_ends_the_turn(self):
        config = {**rc.DEFAULTS, "http_self": {"url": "http://127.0.0.1:9/api/conversation"}, "wake_timeout": 5}
        b = br.Bridge(config, rc.RoomStore(tempfile.mkdtemp()))
        b.client = type("C", (), {"user": FakeUser()})()
        b.room = FakeRoom()

        async def mark(turns, which):
            pass
        b.mark = mark
        br.STATE = Path(tempfile.mkdtemp())
        asyncio.run(b.run_wake(rc.Wake(triggers=[m(7)], unread=[m(7)], channel_id="c1", speaker="x", to_mark=[m(7)])))
        self.assertTrue(b.room.finished)


if __name__ == "__main__":
    unittest.main()
