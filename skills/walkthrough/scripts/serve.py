#!/usr/bin/env python3
"""
Serve a walkthrough session so the page drives the walk.

Binds loopback on an ephemeral port and writes the URL to <session-dir>/serve.json.
The page polls /state, swaps in /fragment when the fingerprint moves, and POSTs
decisions to /decide. /await blocks until the next decision, which is how the
terminal side waits on a click without spinning.

Loopback only, and no path is ever taken from the request: every read and write is
a fixed name inside the session directory.

Exit 1  usage error, or the port could not be bound
"""
import argparse
import json
import os
import re
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import util as importlib_util
from pathlib import Path

HERE = Path(__file__).resolve().parent

_spec = importlib_util.spec_from_file_location("render_report", HERE / "render-report.py")
rr = importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

# accept and drop resolve an open flag; next only advances the walk.
RESOLVE = {"accept": "accepted", "drop": "dropped"}
ACTIONS = (*RESOLVE, "next")
MAX_BODY = 64 * 1024
AWAIT_TIMEOUT = 900.0


class Session:
    """Session state plus the condition the awaiters park on."""

    def __init__(self, root, css_path):
        self.root = root
        self.css_path = css_path
        self.decisions = root / "decisions.jsonl"
        self.cond = threading.Condition()
        self.seq = sum(1 for _ in self.decisions.open(encoding="utf-8")) if self.decisions.exists() else 0

    def load(self):
        return rr.load(self.root, self.css_path)

    def fingerprint(self):
        """Cheap change signal: which beats exist, their mtimes, and the decision count."""
        stamps = sorted(
            (p.name, p.stat().st_mtime_ns) for p in (self.root / "beats").glob("*.json")
        )
        return f"{self.seq}:{hash(tuple(stamps))}"

    def decide(self, n, action, note):
        """Flip a beat to its resolved state and record the reviewer's words."""
        if action in RESOLVE:
            path = self.root / "beats" / f"{n:02d}.json"
            if not path.exists():
                raise ValueError(f"no beat {n}")
            beat = json.loads(path.read_text(encoding="utf-8"))
            if beat.get("state") != "flag":
                raise ValueError(f"beat {n} is {beat.get('state')}, not an open flag")

            beat["state"] = RESOLVE[action]
            if note:
                beat["call"] = note
            path.write_text(json.dumps(beat, indent=2) + "\n", encoding="utf-8")

        with self.cond:
            self.seq += 1
            record = {"seq": self.seq, "n": n, "action": action, "note": note}
            with self.decisions.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            self.cond.notify_all()
        return record

    def wait(self, after, timeout):
        """Block until a decision newer than `after` lands. None on timeout."""
        with self.cond:
            if self.seq > after:
                return self.tail(after)
            self.cond.wait(timeout)
            return self.tail(after) if self.seq > after else None

    def tail(self, after):
        if not self.decisions.exists():
            return None
        rows = [json.loads(l) for l in self.decisions.read_text(encoding="utf-8").splitlines() if l.strip()]
        newer = [r for r in rows if r.get("seq", 0) > after]
        return newer[0] if newer else None


class Handler(BaseHTTPRequestHandler):
    session = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # the terminal belongs to the walk, not to an access log

    def send(self, code, body, ctype="application/json"):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def route(self):
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def query(self, key, default=0):
        match = re.search(rf"[?&]{key}=(\d+)", self.path)
        return int(match.group(1)) if match else default

    def do_GET(self):
        route = self.route()
        try:
            if route == "/":
                session, beats, css, problems, _ = self.session.load()
                page = rr.SHELL + rr.render(session, beats, css, problems, live=True)
                return self.send(200, page, "text/html")
            if route == "/fragment":
                session, beats, _css, problems, _ = self.session.load()
                return self.send(200, rr.body_html(session, beats, problems, True), "text/html")
            if route == "/state":
                _s, beats, _c, _p, problems = self.session.load()
                return self.send(200, json.dumps({
                    "rev": self.session.fingerprint(),
                    "beats": len(beats),
                    "seq": self.session.seq,
                    "problems": problems,
                }))
            if route == "/await":
                found = self.session.wait(self.query("after"), AWAIT_TIMEOUT)
                return self.send(200, json.dumps(found or {"timeout": True}))
            if route == "/favicon.ico":
                return self.send(204, b"", "image/x-icon")
        except (OSError, json.JSONDecodeError) as err:
            return self.send(500, json.dumps({"error": str(err)}))
        self.send(404, json.dumps({"error": "no such route"}))

    def do_POST(self):
        if self.route() != "/decide":
            return self.send(404, json.dumps({"error": "no such route"}))
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.send(413, "body too large", "text/plain")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            action = payload.get("action")
            if action not in ACTIONS:
                raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
            n = payload.get("n")
            record = self.session.decide(
                int(n) if n is not None else None, action, (payload.get("note") or "").strip()
            )
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as err:
            return self.send(400, str(err), "text/plain")
        except OSError as err:
            return self.send(500, str(err), "text/plain")
        self.send(200, json.dumps(record))


def main():
    ap = argparse.ArgumentParser(description="Serve a walkthrough session.")
    ap.add_argument("session_dir", help="directory holding session.json and beats/")
    ap.add_argument("--port", type=int, default=0, help="default 0, an ephemeral port")
    ap.add_argument("--css", help="override assets/report.css")
    args = ap.parse_args()

    root = Path(args.session_dir).expanduser()
    if not (root / "session.json").exists():
        sys.exit(f"serve: no session.json in {root}")

    css_path = Path(args.css).expanduser() if args.css else rr.default_css()
    Handler.session = Session(root, css_path)

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as err:
        sys.exit(f"serve: {err}")

    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    (root / "serve.json").write_text(
        json.dumps({"url": url, "pid": os.getpid()}, indent=2) + "\n", encoding="utf-8"
    )
    print(url, flush=True)

    # A supervising shell kills this with SIGTERM, which would otherwise skip the
    # cleanup below and leave a serve.json pointing at a dead port.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        httpd.server_close()
        (root / "serve.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
