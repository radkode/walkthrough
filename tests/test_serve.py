#!/usr/bin/env python3
"""Tests for the server, which is the half of the tool that has state and threads.

    python3 -m unittest discover -s tests

Covers what a file on disk cannot show: that two clients cannot resolve one flag
two ways, that a beat is never left half-written, that a park wakes when someone
acts and not before, and that a malformed request gets an answer instead of
dropping the connection.
"""
import importlib.util
import json
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "underwrite" / "scripts"


def load(stem):
    """Import a script whose filename is not a legal module name."""
    spec = importlib.util.spec_from_file_location(
        stem.replace("-", "_"), SCRIPTS / (stem + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


serve = load("serve")

FLAG = {
    "n": 1, "tier": "core", "state": "flag", "claim": "unpinned", "where": "a.py:1",
    "slots": {"what": "x", "proof": "a.py:1", "risk": "r", "fix": "pin it"},
}
CLEAN = {
    "n": 2, "tier": "core", "state": "clean", "claim": "load-bearing", "where": "b.py:1",
    "slots": {"what": "x", "proof": "b.py:1"},
}


class SessionTest(unittest.TestCase):
    """A session directory per test: beat 1 an open flag, beat 2 already clean."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "beats").mkdir()
        (self.root / "session.json").write_text(
            json.dumps({"repo": "acme/widget", "number": 1, "cursor": 2}), encoding="utf-8"
        )
        for beat in (FLAG, CLEAN):
            self.put(beat)
        self.session = self.open_session()

    def open_session(self):
        session = serve.Session(self.root, serve.rr().default_css())
        self.addCleanup(session.stop.set)
        return session

    def put(self, beat):
        self.path(beat["n"]).write_text(json.dumps(beat), encoding="utf-8")

    def path(self, n):
        return self.root / "beats" / ("%02d.json" % n)

    def beat(self, n):
        return json.loads(self.path(n).read_text(encoding="utf-8"))

    def decisions(self):
        log = self.root / "decisions.jsonl"
        if not log.exists():
            return []
        return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]


class ResolvingAFlag(SessionTest):
    def test_accept_resolves_it_and_records_the_words(self):
        record = self.session.act(1, "accept", "yes, pin it")
        self.assertEqual(record["seq"], 1)
        self.assertEqual(self.beat(1)["state"], "accepted")
        self.assertEqual(self.beat(1)["call"], "yes, pin it")

    def test_a_flag_resolves_only_once(self):
        self.session.act(1, "accept", "")
        with self.assertRaises(ValueError):
            self.session.act(1, "drop", "")
        self.assertEqual(self.beat(1)["state"], "accepted")
        self.assertEqual(self.session.seq, 1)
        self.assertEqual(len(self.decisions()), 1)

    def test_a_clean_beat_cannot_be_accepted(self):
        with self.assertRaises(ValueError):
            self.session.act(2, "accept", "")
        self.assertEqual(self.beat(2)["state"], "clean")

    def test_a_beat_that_does_not_exist_is_rejected(self):
        with self.assertRaises(ValueError):
            self.session.act(9, "accept", "")

    def test_a_note_records_words_without_resolving_anything(self):
        self.session.act(2, "note", "worth watching")
        self.assertEqual(self.beat(2)["state"], "clean")
        self.assertEqual(self.beat(2)["call"], "worth watching")

    def test_a_walk_action_touches_no_beat(self):
        before = self.path(1).read_bytes()
        self.session.act(None, "next", "")
        self.assertEqual(self.path(1).read_bytes(), before)
        self.assertEqual(self.decisions()[0]["action"], "next")


class Concurrency(SessionTest):
    def test_two_clients_cannot_resolve_one_flag_two_ways(self):
        """Read, guard and write used to sit outside the lock, so both passed the
        guard: 101 of 200 rounds had accept and drop each return 200."""
        results, ready = {}, threading.Barrier(2)

        def go(action, i):
            ready.wait()
            try:
                results[i] = self.session.act(1, action, "")
            except ValueError as err:
                results[i] = err

        threads = [threading.Thread(target=go, args=a) for a in (("accept", 0), ("drop", 1))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5.0)

        self.assertEqual(len([r for r in results.values() if isinstance(r, dict)]), 1)
        self.assertEqual(len([r for r in results.values() if isinstance(r, ValueError)]), 1)
        self.assertEqual(len(self.decisions()), 1)
        self.assertIn(self.beat(1)["state"], ("accepted", "dropped"))

    def test_concurrent_actions_get_consecutive_seqs(self):
        threads = [threading.Thread(target=self.session.act, args=(None, "next", ""))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5.0)
        self.assertEqual(sorted(r["seq"] for r in self.decisions()), list(range(1, 9)))


class AtomicWrites(SessionTest):
    def test_a_failed_rename_leaves_the_beat_byte_identical(self):
        before = self.path(1).read_bytes()
        with mock.patch.object(serve.os, "replace", side_effect=OSError("no space")):
            with self.assertRaises(OSError):
                self.session.act(1, "accept", "")
        self.assertEqual(self.path(1).read_bytes(), before)

    def test_a_failed_rename_leaves_no_temp_file_behind(self):
        with mock.patch.object(serve.os, "replace", side_effect=OSError("no space")):
            with self.assertRaises(OSError):
                self.session.act(1, "accept", "")
        self.assertEqual(sorted(p.name for p in (self.root / "beats").iterdir()),
                         ["01.json", "02.json"])

    def test_a_normal_write_leaves_no_temp_file_behind(self):
        self.session.act(1, "accept", "")
        self.assertEqual(sorted(p.name for p in (self.root / "beats").iterdir()),
                         ["01.json", "02.json"])

    def test_a_temp_file_is_never_globbed_as_a_beat(self):
        """The fingerprint and the renderer's loader both glob *.json, and pathlib
        globs dotfiles too, so the temp name must not end in .json."""
        (self.root / "beats" / "01.json.9.9.tmp").write_text("{ half written")
        self.assertEqual(sorted(p.name for p in (self.root / "beats").glob("*.json")),
                         ["01.json", "02.json"])


class Parking(SessionTest):
    def test_it_returns_the_oldest_unseen_action(self):
        for _ in range(3):
            self.session.act(None, "next", "")
        self.assertEqual(self.session.wait(0, 0.1)["seq"], 1)
        self.assertEqual(self.session.wait(1, 0.1)["seq"], 2)

    def test_it_waits_the_whole_timeout_when_nobody_acts(self):
        started = time.monotonic()
        self.assertIsNone(self.session.wait(0, 0.2))
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_a_blank_line_in_the_log_does_not_fake_a_timeout(self):
        """seq came from a line count while tail skipped blanks, so a single blank
        line put seq past the highest record and every park answered instantly.
        The agent is told that means nobody acted, so the walk span."""
        (self.root / "decisions.jsonl").write_text(
            json.dumps({"seq": 1, "n": None, "action": "next", "note": ""}) + "\n\n",
            encoding="utf-8",
        )
        session = self.open_session()
        self.assertEqual(session.seq, 1)
        started = time.monotonic()
        self.assertIsNone(session.wait(1, 0.2))
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_nobody_parked_means_nobody_is_listening(self):
        self.assertFalse(self.session.snapshot()["listening"])

    def test_a_parked_caller_is_visible_to_the_page(self):
        """The page used to infer this from the agent's own status text, which an agent
        that died mid-walk leaves standing, so a click went nowhere and looked broken."""
        seen = []
        waiter = threading.Thread(target=lambda: self.session.wait(0, 5.0))
        waiter.start()
        for _ in range(100):
            if self.session.snapshot()["listening"]:
                seen.append(True)
                break
            time.sleep(0.01)
        self.session.act(None, "next", "")
        waiter.join(5.0)
        self.assertEqual(seen, [True])
        self.assertFalse(self.session.snapshot()["listening"])

    def test_a_park_that_times_out_stops_listening(self):
        self.session.wait(0, 0.05)
        self.assertFalse(self.session.snapshot()["listening"])

    def test_an_already_satisfied_park_never_claims_to_listen(self):
        """seq is already ahead, so wait returns without parking at all."""
        self.session.act(None, "next", "")
        self.assertIsNotNone(self.session.wait(0, 5.0))
        self.assertFalse(self.session.snapshot()["listening"])

    def test_it_wakes_as_soon_as_someone_acts(self):
        woke = []
        waiter = threading.Thread(target=lambda: woke.append(self.session.wait(0, 5.0)))
        waiter.start()
        time.sleep(0.05)
        self.session.act(None, "next", "")
        waiter.join(5.0)
        self.assertEqual(woke[0]["action"], "next")




class ATornLog(SessionTest):
    """ENOSPC or a power loss during the append leaves a half-written last line.
    Refusing to parse it stranded the session the log exists to preserve."""

    TORN = '{"seq": 1, "n": null, "action": "next", "note": ""}\n{"seq": 2, "n'

    def tear(self):
        (self.root / "decisions.jsonl").write_text(self.TORN, encoding="utf-8")

    def test_the_readable_records_still_load(self):
        self.tear()
        session = self.open_session()
        self.assertEqual([r["seq"] for r in session.records()], [1])
        self.assertEqual(session.seq, 1)

    def test_a_park_answers_instead_of_raising(self):
        """A live server used to answer every /await with a 500 once this landed."""
        self.tear()
        session = self.open_session()
        self.assertIsNone(session.wait(1, 0.05))

    def test_the_next_decision_does_not_fuse_onto_it(self):
        """The torn line stays where it is. What matters is that the next record gets
        a line of its own, rather than being appended onto the stump and lost too."""
        self.tear()
        session = self.open_session()
        session.act(None, "next", "")
        self.assertEqual([r["seq"] for r in session.records()], [1, 2])
        lines = (self.root / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[1], '{"seq": 2, "n')
        self.assertEqual(json.loads(lines[2])["seq"], 2)

    def test_a_log_ending_cleanly_gains_no_blank_line(self):
        session = self.open_session()
        session.act(None, "next", "")
        session.act(None, "next", "")
        self.assertNotIn(
            "\n\n", (self.root / "decisions.jsonl").read_text(encoding="utf-8"))
class Served(SessionTest):
    """A live server on an ephemeral port, plus the three ways to talk to it."""

    def setUp(self):
        super().setUp()
        handler = type("Handler", (serve.Handler,), {"session": self.session})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        # Poll faster than the 0.5s default purely so teardown does not dominate the
        # suite: shutdown() waits for serve_forever to come round again.
        thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port

    def post(self, route, body):
        request = urllib.request.Request(
            self.url + route, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.read().decode()

    def get(self, route):
        try:
            with urllib.request.urlopen(self.url + route, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.read().decode()

    def raw(self, request):
        """Shapes urllib will not send, like a Content-Length that is not a number."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(request.encode())
            return sock.recv(4096).decode(errors="replace").split("\r\n")[0]
        finally:
            sock.close()


class Requests(Served):
    def test_an_accept_over_http_resolves_the_flag(self):
        status, body = self.post("/act", {"n": 1, "action": "accept", "note": "yes"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["seq"], 1)
        self.assertEqual(self.beat(1)["state"], "accepted")

    def test_a_content_length_that_is_not_a_number_answers_400(self):
        self.assertIn("400", self.raw(
            "POST /status HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: abc\r\n\r\n"))

    def test_a_json_body_that_is_not_an_object_answers_400(self):
        for route in ("/status", "/act"):
            with self.subTest(route=route):
                self.assertEqual(self.post(route, [])[0], 400)

    def test_an_oversized_body_answers_413(self):
        self.assertIn("413", self.raw(
            "POST /act HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: %d\r\n\r\n"
            % (serve.MAX_BODY + 1)))

    def test_an_unknown_action_answers_400(self):
        status, body = self.post("/act", {"action": "bogus"})
        self.assertEqual(status, 400)
        self.assertIn("action must be one of", body)

    def test_a_beat_number_that_is_not_a_number_answers_400(self):
        self.assertEqual(self.post("/act", {"n": "abc", "action": "accept"})[0], 400)

    def test_a_beat_that_does_not_exist_answers_400(self):
        status, body = self.post("/act", {"n": 99, "action": "accept"})
        self.assertEqual(status, 400)
        self.assertIn("no beat 99", body)

    def test_status_round_trips(self):
        status, body = self.post("/status", {"phase": "parked", "text": "waiting on you"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["phase"], "parked")
        self.assertEqual(json.loads(self.get("/state")[1])["status"]["phase"], "parked")

    def test_the_live_page_carries_the_controls(self):
        status, page = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('data-action="accept"', page)

    def test_state_reports_the_beats_it_found(self):
        self.assertEqual(json.loads(self.get("/state")[1])["beats"], 2)

    def test_an_unknown_route_answers_404(self):
        self.assertEqual(self.get("/nope")[0], 404)

    def test_a_traversing_path_answers_404_without_opening_anything(self):
        self.assertEqual(self.get("/../../../etc/passwd")[0], 404)


class Forgery(Served):
    """Binding loopback is not authentication. Both attacks reach a local port
    from a page the reviewer merely has open, so both are shapes urllib will not
    send and every one of these goes over a raw socket."""

    def send_raw(self, method, route, headers, body=""):
        lines = ["%s %s HTTP/1.1" % (method, route)]
        lines += ["%s: %s" % kv for kv in headers.items()]
        if body:
            lines.append("Content-Length: %d" % len(body))
        return self.raw("\r\n".join(lines) + "\r\n\r\n" + body)

    def act_body(self):
        return json.dumps({"n": 1, "action": "accept", "note": "not the reviewer"})

    def test_a_rebound_host_cannot_read_the_session(self):
        """DNS rebinding: the browser sends the name it navigated to, so refusing
        anything but loopback is what makes the read endpoints unreadable."""
        for route in ("/", "/state", "/fragment"):
            with self.subTest(route=route):
                answer = self.send_raw("GET", route, {"Host": "evil.example.com"})
                self.assertIn("403", answer)

    def test_a_missing_host_is_refused(self):
        self.assertIn("403", self.send_raw("GET", "/state", {}))

    def test_the_walk_reads_with_a_loopback_host(self):
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port):
            with self.subTest(host=host):
                self.assertIn("200", self.send_raw("GET", "/state", {"Host": host}))

    def test_a_cross_origin_post_cannot_resolve_a_flag(self):
        """A CORS simple request needs no preflight, so text/parse checks are not a
        defense; the Origin is what gives it away."""
        answer = self.send_raw(
            "POST", "/act",
            {"Host": "127.0.0.1:%d" % self.port, "Origin": "https://evil.example",
             "Content-Type": "text/plain;charset=UTF-8"},
            self.act_body(),
        )
        self.assertIn("403", answer)
        self.assertEqual(self.beat(1)["state"], "flag")
        self.assertEqual(self.decisions(), [])

    def test_a_cross_origin_post_cannot_spoof_the_status(self):
        answer = self.send_raw(
            "POST", "/status",
            {"Host": "127.0.0.1:%d" % self.port, "Origin": "https://evil.example",
             "Content-Type": "text/plain"},
            json.dumps({"phase": "parked", "text": "safe to click"}),
        )
        self.assertIn("403", answer)
        self.assertEqual(self.session.status["phase"], "starting")

    def test_the_page_resolves_a_flag_from_its_own_origin(self):
        host = "127.0.0.1:%d" % self.port
        answer = self.send_raw(
            "POST", "/act",
            {"Host": host, "Origin": "http://" + host,
             "Content-Type": "application/json"},
            self.act_body(),
        )
        self.assertIn("200", answer)
        self.assertEqual(self.beat(1)["state"], "accepted")

    def test_the_walk_acts_with_no_origin_at_all(self):
        """curl sends none, and the agent drives the same routes the page does."""
        self.assertEqual(self.post("/act", {"n": 1, "action": "accept"})[0], 200)


class Lifecycle(unittest.TestCase):
    """main() is unreachable in-process, and the skill depends on all of it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_it_prints_a_url_writes_serve_json_and_cleans_up_on_sigterm(self):
        (self.root / "beats").mkdir()
        (self.root / "session.json").write_text(
            json.dumps({"repo": "acme/widget"}), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "serve.py"), str(self.root)],
            stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.kill)
        url = proc.stdout.readline().strip()
        self.assertTrue(url.startswith("http://127.0.0.1:"), url)

        served = json.loads((self.root / "serve.json").read_text(encoding="utf-8"))
        self.assertEqual(served["url"], url)
        self.assertEqual(served["pid"], proc.pid)

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((self.root / "serve.json").exists())

    def test_a_sigterm_racing_startup_still_cleans_up(self):
        """The handler used to be armed after serve.json was written and the URL
        printed, so a SIGTERM in that window took the default disposition, exited 143,
        and left the file behind for the next walk to read and curl. Repeated because
        the window is small: CI hit it, one local run in forty hit it."""
        (self.root / "beats").mkdir()
        (self.root / "session.json").write_text(
            json.dumps({"repo": "acme/widget"}), encoding="utf-8")

        for attempt in range(6):
            with self.subTest(attempt=attempt):
                proc = subprocess.Popen(
                    [sys.executable, str(SCRIPTS / "serve.py"), str(self.root)],
                    stdout=subprocess.PIPE, text=True,
                )
                self.addCleanup(proc.stdout.close)
                self.addCleanup(proc.kill)
                proc.stdout.readline()
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)
                self.assertEqual(proc.returncode, 0)
                self.assertFalse((self.root / "serve.json").exists())

    def test_a_directory_with_no_session_exits_1(self):
        done = subprocess.run(
            [sys.executable, str(SCRIPTS / "serve.py"), str(self.root)],
            capture_output=True, text=True,
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("no session.json", done.stderr)


if __name__ == "__main__":
    unittest.main()
