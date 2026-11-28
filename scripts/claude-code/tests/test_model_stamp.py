"""The model a session runs on travels with its writes (Arden's borrow, room 1576023465000181972): read from
PERSISTENCE_MODEL, else the session's transcript, never guessed; and a store built before --model is called again
without it, so a hook deployed ahead of its store costs a label, not the write."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "testguard"))
import no_real_claude  # noqa: E402,F401  first: no test can start a real claude (Arden 1570199499278909491)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hook

SESSION = "5a0b681f-0000-0000-0000-000000000000"


class ModelStamp(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        for p in (mock.patch.object(Path, "home", return_value=self.home),
                  mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("PERSISTENCE_MODEL", None)
        os.environ.pop("PERSISTENCE_SESSION", None)

    def transcript(self, *models):
        folder = self.home / ".claude" / "projects" / "-home-x-work"
        folder.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"type": "user", "message": {"content": "hi"}})]
        lines += [json.dumps({"type": "assistant", "message": {"model": m, "content": []}}) for m in models]
        (folder / f"{SESSION}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_the_newest_assistant_message_names_the_model(self):
        self.transcript("claude-opus-4-8", "claude-opus-5-5", "<synthetic>")
        self.assertEqual(hook.model_of(SESSION), "claude-opus-5-5")

    def test_the_environment_wins_and_nothing_is_guessed_without_either(self):
        self.assertIsNone(hook.model_of(SESSION))
        os.environ["PERSISTENCE_MODEL"] = "claude-sonnet-5-5"
        self.assertEqual(hook.model_of(SESSION), "claude-sonnet-5-5")

    def test_a_write_carries_the_model_and_a_read_does_not(self):
        self.transcript("claude-opus-5-5")
        add, _ = hook.remote_args(["add", "Personal", "--content", "x", "--everywhere"])
        show, _ = hook.remote_args(["show", "12"])
        self.assertEqual(add[add.index("--model") + 1], "claude-opus-5-5")
        self.assertNotIn("--model", show)

    def test_a_store_without_the_option_gets_the_call_again_without_it(self):
        self.transcript("claude-opus-5-5")
        calls = []

        def answer(where, call, stdin, timeout):
            calls.append(call)
            return (1, "", "--model is not an option of any command.\n") if "--model" in call else (0, "added\n", "")

        with mock.patch.object(hook, "remote", return_value={"host": "none"}), \
                mock.patch.object(hook, "served_remote", side_effect=answer):
            code = hook.cli(["add", "Personal", "--content", "x", "--everywhere"])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--model", calls[1])


if __name__ == "__main__":
    unittest.main()
