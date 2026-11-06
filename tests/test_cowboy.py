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


class BackendDetectionTest(unittest.TestCase):
    def backend(self, env):
        keep = {k: v for k, v in os.environ.items()
                if k not in ("HERDR_ENV", "HC_TERMINAL_ID", "CLAUDE_COWBOY_BACKEND")}
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {**keep, **env}, clear=True):
            return load_cowboy(home).BACKEND

    def test_detection(self):
        self.assertEqual(self.backend({}), "herdr")
        self.assertEqual(self.backend({"HERDR_ENV": "1"}), "herdr")
        self.assertEqual(self.backend({"HC_TERMINAL_ID": "t4"}), "hc")
        # terminal Herdr nested in a desktop-app pane: the inner one is what the user sees
        self.assertEqual(self.backend({"HC_TERMINAL_ID": "t4", "HERDR_ENV": "1"}), "herdr")
        self.assertEqual(self.backend({"HERDR_ENV": "1", "CLAUDE_COWBOY_BACKEND": "hc"}), "hc")


class HcBackendTest(unittest.TestCase):
    """The desktop-app (herdr-core) backend, against a scripted fake `hc` / `herdr-app`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cb = load_cowboy(self.tmp.name)
        self.cb.BACKEND = "hc"
        self.cfg = self.cb.load_config()
        self.calls = []
        self.status = {"state": "working"}  # what `hc get` reports
        self.screen = ""
        self.finished = False
        self.now = 1_000_000.0
        patches = [
            mock.patch.object(self.cb, "run", self.fake_run),
            mock.patch.object(self.cb.shutil, "which", lambda name: "/bin/" + name),
            mock.patch.object(self.cb.time, "time", lambda: self.now),
            mock.patch.object(self.cb.time, "sleep", self.fake_sleep),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_sleep(self, secs):
        self.now += secs

    def fake_run(self, cmd, cwd=None, check=True, input=None):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        tool, sub = Path(cmd[0]).name, cmd[1]
        out, rc = "", 0
        if tool == "herdr-app" and sub == "ls":
            out = json.dumps({"spaces": [{"id": "sp1", "name": "🐄 repo", "tabs": [
                {"id": "tb1", "name": "shell", "terminals": [{"terminal": "t5"}]},
                {"id": "tb2", "name": "2", "terminals": [{"terminal": "t9"}]}]}]})
        elif tool == "herdr-app" and sub == "do":
            out = json.dumps({"space": "sp1", "tab": "tb1", "terminal": "t5"} if cmd[2] == "space.new" else {"ok": True})
        elif sub == "run":
            out = json.dumps({"address": "t9", "name": cmd[cmd.index("--name") + 1], "state": "live"})
        elif sub == "get":
            out = json.dumps({"state": "finished" if self.finished else "live", "status": self.status})
        elif sub == "read":
            out = self.screen
        elif sub == "keys":
            self.screen = ""
            self.status = {"state": "working"}
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    def hc_calls(self, sub):
        return [c for c in self.calls if Path(c[0]).name == "hc" and c[1] == sub]

    def make_agent(self, harness="claude", **over):
        agent = {
            "id": "abcd1234", "task_name": "fix it", "branch": "cow-fix-it-abcd1234",
            "worktree": self.tmp.name, "repo": self.tmp.name, "base": "main", "harness": harness,
            "template": None, "status": "queued", "created_at": self.now, "turn": 1,
        }
        d = self.cb.agent_dir(agent)
        agent["prompt_file"] = str(d / "prompt.md")
        agent["report"] = str(d / "report.md")
        agent.update(over)
        Path(agent["prompt_file"]).write_text("do the thing")
        self.cb.save(agent)
        return agent

    def test_launch_claude_into_repo_space(self):
        a = self.cb.launch(self.make_agent(), self.cfg)
        self.assertEqual(a["status"], "running")
        self.assertEqual((a["backend"], a["hc_address"], a["space_id"], a["tab_id"]), ("hc", "t9", "sp1", "tb2"))
        run = self.hc_calls("run")[0]
        self.assertEqual(run[2], "--json")  # before `--`, so hc parses it
        for flag in ("--bg", "--outlive-parent", "--no-notify"):
            self.assertIn(flag, run)
        self.assertIn("herdr.space=sp1", run)
        self.assertIn("cowboy.id=abcd1234", run)
        argv = run[run.index("--") + 1:]
        self.assertEqual(argv[0], "claude")
        self.assertIn("do the thing", argv[-1])  # task on the command line
        self.assertEqual(self.hc_calls("send"), [])
        renames = [c for c in self.calls if c[1:3] == ["do", "tab.rename"]]
        self.assertEqual(json.loads(renames[0][3]), {"id": "tb2", "name": "🐄 fix it (abcd1234)"})
        self.assertEqual(json.loads((Path(self.tmp.name) / "hc-spaces.json").read_text()), {self.tmp.name: "sp1"})

    def test_existing_space_is_reused(self):
        (Path(self.tmp.name) / "hc-spaces.json").write_text(json.dumps({self.tmp.name: "sp1"}))
        self.cb.launch(self.make_agent(), self.cfg)
        self.assertFalse([c for c in self.calls if c[1:3] == ["do", "space.new"]])

    def test_trust_dialog_accepted_when_repo_trusted(self):
        self.status, self.screen = None, "Do you trust this folder?"
        with mock.patch.object(self.cb, "repo_is_trusted", lambda repo: True):
            a = self.cb.launch(self.make_agent(), self.cfg)
        self.assertEqual(a["status"], "running")
        self.assertEqual(self.hc_calls("keys")[0][-2:], ["down", "enter"])

    def test_trust_dialog_left_for_user(self):
        self.status, self.screen = None, "Do you trust this folder?"
        with mock.patch.object(self.cb, "repo_is_trusted", lambda repo: False):
            a = self.cb.launch(self.make_agent(), self.cfg)
        self.assertEqual(a["status"], "blocked")
        self.assertTrue(a["prompted"])  # claude's task is already in its argv
        self.assertIn("🐄 fix it (abcd1234)", a["note"])
        self.assertEqual(self.hc_calls("keys"), [])

    def test_other_harness_gets_task_typed_in(self):
        self.status = {"state": "idle"}
        a = self.cb.launch(self.make_agent(harness="codex"), self.cfg)
        self.assertEqual(a["status"], "running")
        self.assertTrue(a["prompted"])
        send = self.hc_calls("send")[0]
        self.assertEqual(send[2], "t9")
        self.assertIn("do the thing", send[3])
        self.assertNotIn("do the thing", " ".join(self.hc_calls("run")[0]))

    def test_no_window_runs_hidden(self):
        with mock.patch.object(self.cb, "app_layout", lambda: None):
            a = self.cb.launch(self.make_agent(), self.cfg)
        self.assertEqual(a["status"], "running")
        self.assertIsNone(a["space_id"])
        self.assertFalse([x for x in self.hc_calls("run")[0] if x.startswith("herdr.space=")])

    def test_coordinated_uses_given_space(self):
        with mock.patch.dict(os.environ, {"CLAUDE_COWBOY_HC_SPACE": "spRemote"}):
            self.cb.launch(self.make_agent(), self.cfg)
        self.assertIn("herdr.space=spRemote", self.hc_calls("run")[0])
        self.assertFalse([c for c in self.calls if c[1:3] == ["do", "space.new"]])

    def test_queued_agent_keeps_its_space(self):
        # Spawned with a space but started later by a pump that has no CLAUDE_COWBOY_HC_SPACE.
        self.cb.launch(self.make_agent(hc_space="spRemote"), self.cfg)
        self.assertIn("herdr.space=spRemote", self.hc_calls("run")[0])

    def test_state_mapping(self):
        a = self.make_agent(backend="hc", hc_address="t9", status="running")
        for status, want in (({"state": "idle"}, "idle"), ({"state": "done"}, "done"),
                             ({"state": "error"}, "blocked"), (None, "starting")):
            self.status = status
            self.assertEqual(self.cb.herdr_state(a), want)
        self.finished = True
        self.assertEqual(self.cb.herdr_state(a), "gone")

    def test_steer_stop_cleanup(self):
        a = self.make_agent(backend="hc", hc_address="t9", status="settled", prompted=True, herdr_name="cow-abcd1234")
        with redirect_stdout(io.StringIO()):
            self.cb.cmd_steer(argparse.Namespace(ids=[a["id"]], message="one more thing"))
            self.assertEqual(self.hc_calls("send")[0][2:3], ["t9"])
            self.assertIn("one more thing", self.hc_calls("send")[0][3])
            self.cb.cmd_stop(argparse.Namespace(ids=[a["id"]]))
            self.assertEqual(self.hc_calls("interrupt")[0][2], "t9")
            with mock.patch.object(self.cb, "git", lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")):
                self.cb.cmd_cleanup(argparse.Namespace(ids=[a["id"]], force=True, delete_unmerged=False))
        self.assertEqual(self.hc_calls("kill")[0][2], "t9")
        self.assertEqual(self.hc_calls("rm")[0][2], "t9")


if __name__ == "__main__":
    unittest.main()
