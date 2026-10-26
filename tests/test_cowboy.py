"""Tests for scripts/cowboy. Run from the repo root: python3 -m unittest discover tests"""

import argparse
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cowboy"


def load_cowboy(home):
    """Import scripts/cowboy (no .py extension) with CLAUDE_COWBOY_HOME pointing at home."""
    with mock.patch.dict(os.environ, {"CLAUDE_COWBOY_HOME": str(home)}):
        loader = SourceFileLoader("cowboy", str(SCRIPT))
        spec = importlib.util.spec_from_loader("cowboy", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
    return mod


class CowboyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cb = load_cowboy(self.tmp.name)
        self.cfg = self.cb.load_config()
        self.now = 1_000_000.0
        self.herdr = "working"
        self.prompts = []
        patches = [
            mock.patch.object(self.cb, "herdr_state", lambda agent: self.herdr),
            mock.patch.object(self.cb, "run", self.fake_run),
            mock.patch.object(self.cb, "require_herdr", lambda: None),
            mock.patch.object(self.cb.time, "time", lambda: self.now),
            mock.patch.object(self.cb.time, "sleep", self.fake_sleep),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_run(self, cmd, cwd=None, check=True, input=None):
        if "prompt" in cmd:
            self.prompts.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_sleep(self, secs):
        self.now += secs
        self.sleeps += 1
        if self.sleeps > 3:
            raise TimeoutError("wait kept polling")

    def make_agent(self, **over):
        agent = {
            "id": "abcd1234", "task_name": "t", "branch": "cow-t-abcd1234", "worktree": self.tmp.name,
            "repo": self.tmp.name, "base": "main", "harness": "claude", "template": None,
            "status": "running", "created_at": self.now, "turn": 1, "prompted": True,
            "herdr_name": "cow-abcd1234", "pane_id": "p1",
        }
        d = self.cb.agent_dir(agent)
        agent["prompt_file"] = str(d / "prompt.md")
        agent["report"] = str(d / "report.md")
        agent.update(over)
        Path(agent["prompt_file"]).write_text("do it")
        self.cb.save(agent)
        return agent

    def write(self, path, text="done", age=0):
        Path(path).write_text(text)
        os.utime(path, (self.now - age, self.now - age))

    def check(self, agent_id):
        a = self.cb.load(agent_id)
        return self.cb.check_settled(a, self.cfg), self.cb.load(agent_id)

    def steer(self, agent_id, message="more"):
        with redirect_stdout(io.StringIO()):
            self.cb.cmd_steer(argparse.Namespace(ids=[agent_id], message=message))

    def wait(self, agent_id):
        """Run cmd_wait; returns its output, or None if it was still polling after a few rounds."""
        self.sleeps = 0
        out = io.StringIO()
        with mock.patch.object(self.cb, "print_completion", lambda a: print("COMPLETED", a["report"])), \
                redirect_stdout(out):
            try:
                self.cb.cmd_wait(argparse.Namespace(ids=[agent_id], timeout=None, any=False))
            except TimeoutError:
                return None
        return out.getvalue()

    # a. steer mid-turn
    def test_steer_mid_turn_waits_for_new_turn_report(self):
        a = self.make_agent()
        self.steer(a["id"])
        a = self.cb.load(a["id"])
        self.assertEqual(a["turn"], 2)
        self.assertEqual(Path(a["report"]).name, "report.turn2.md")
        self.assertIn(a["report"], self.prompts[-1])

        # Turn 1 finishes (to the old path) while the agent goes straight on to the steer.
        self.write(Path(a["report"]).with_name("report.md"), age=120)
        self.herdr = "working"
        settled, a = self.check(a["id"])
        self.assertFalse(settled)
        self.assertEqual(a["status"], "running")
        self.assertIsNone(self.wait(a["id"]))

        # Even the new-turn report doesn't settle it while herdr still says working.
        self.write(a["report"], "turn 2 done", age=120)
        settled, a = self.check(a["id"])
        self.assertFalse(settled)
        self.assertEqual(a["status"], "running")

        self.herdr = "idle"
        settled, a = self.check(a["id"])
        self.assertTrue(settled)
        self.assertEqual(a["status"], "settled")
        self.assertIn("report.turn2.md", self.wait(a["id"]))

    def test_steer_blocked_at_startup_stays_turn_one(self):
        a = self.make_agent(status="blocked", prompted=False, note="blocked at startup")
        self.steer(a["id"], "go")
        a = self.cb.load(a["id"])
        self.assertEqual(a["turn"], 1)
        self.assertEqual(Path(a["report"]).name, "report.md")
        self.assertTrue(a["prompted"])
        self.assertIn("do it", self.prompts[-1])
        self.assertIn(f"write your report to {a['report']}", self.prompts[-1])
        self.assertTrue(self.prompts[-1].rstrip().endswith("go"))

        self.write(a["report"], "task done")
        self.herdr = "idle"
        settled, a = self.check(a["id"])
        self.assertTrue(settled)
        self.assertEqual(a["status"], "settled")
        self.assertNotIn("note", a)
        self.assertIn("report.md", self.wait(a["id"]))

    # b. settled agent that is working again
    def test_settled_agent_working_flips_back_to_running(self):
        a = self.make_agent(status="settled", settled_at=self.now - 5, note="went idle")
        self.herdr = "working"
        self.assertIsNone(self.wait(a["id"]))
        a = self.cb.load(a["id"])
        self.assertEqual(a["status"], "running")
        self.assertNotIn("settled_at", a)
        self.assertNotIn("note", a)

    def test_status_revives_settled_agent(self):
        a = self.make_agent(status="settled", settled_at=self.now)
        with redirect_stdout(io.StringIO()):
            self.cb.cmd_status(argparse.Namespace(no_pump=True, all=False, json=True))
        self.assertEqual(self.cb.load(a["id"])["status"], "running")

    def test_terminal_states_are_not_resurrected(self):
        for status in ("stopped", "exited", "cleaned"):
            a = self.make_agent(status=status)
            self.herdr = "working"
            self.check(a["id"])
            self.assertEqual(self.cb.load(a["id"])["status"], status)

    # c. normal single-turn settle
    def test_single_turn_settle(self):
        a = self.make_agent()
        self.write(a["report"])
        self.herdr = "working"
        self.assertFalse(self.check(a["id"])[0])
        self.herdr = "idle"
        settled, a = self.check(a["id"])
        self.assertTrue(settled)
        self.assertEqual(a["status"], "settled")
        self.assertIn("report.md", self.wait(a["id"]))

    def test_blocked_report(self):
        a = self.make_agent()
        self.write(a["report"], "BLOCKED: which db?")
        self.herdr = "done"
        settled, a = self.check(a["id"])
        self.assertTrue(settled)
        self.assertEqual(a["status"], "blocked")

    def test_unknown_herdr_state_uses_mtime_fallback(self):
        a = self.make_agent()
        self.herdr = None
        self.write(a["report"], age=5)
        self.assertFalse(self.check(a["id"])[0])
        self.write(a["report"], age=60)
        self.assertTrue(self.check(a["id"])[0])

    def test_gone_without_report_exits(self):
        a = self.make_agent()
        self.herdr = "gone"
        settled, a = self.check(a["id"])
        self.assertTrue(settled)
        self.assertEqual(a["status"], "exited")

    # d. legacy state file
    def test_legacy_state_file(self):
        # Written by the old steer: turn 1 archived as report.turn1.md, turn 2 reports to
        # report.md, and no per-turn report path.
        a = self.make_agent(turn=2, status="running")
        legacy = json.loads((self.cb.STATE / f"{a['id']}.json").read_text())
        self.assertTrue(legacy["report"].endswith("/report.md"))
        self.write(Path(a["report"]).with_name("report.turn1.md"), "turn 1")
        self.write(a["report"], "turn 2", age=60)
        self.herdr = "idle"
        settled, a = self.check(a["id"])
        self.assertTrue(settled)
        self.assertIn("report.md", self.wait(a["id"]))

        self.steer(a["id"])
        a = self.cb.load(a["id"])
        self.assertEqual(a["status"], "running")
        self.assertEqual(Path(a["report"]).name, "report.turn3.md")
        self.assertEqual(Path(a["report"]).with_name("report.md").read_text(), "turn 2")

    def test_legacy_state_without_turn(self):
        a = self.make_agent()
        state = self.cb.STATE / f"{a['id']}.json"
        data = json.loads(state.read_text())
        del data["turn"]
        state.write_text(json.dumps(data))
        self.steer(a["id"])
        self.assertEqual(Path(self.cb.load(a["id"])["report"]).name, "report.turn2.md")


if __name__ == "__main__":
    unittest.main()
