#!/usr/bin/env python3
"""drift-cli — memory for the web, built for AI agents.

Snapshot a web page's clean text once (`watch`), then ask later what
actually changed (`check`) and get one honest report: added lines,
removed lines, changed lines — with timestamps, session ids and other
noise filtered out BEFORE the diff, and secrets redacted before
anything is stored.

Exit codes (check): 0 = NO CHANGE, 1 = CHANGED, 2 = ERROR.
Python 3 standard library only. No pip installs, no API keys.
"""

import argparse
import difflib
import hashlib
import http.client
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

VERSION = "0.1.0"
TIMEOUT = 20
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 drift-cli/" + VERSION)


class DriftError(Exception):
    pass


# ---------------------------------------------------------------- fetch
def _ssl_context():
    # Some edges drop TLS handshakes that offer no ALPN; naming
    # http/1.1 explicitly is the difference between a 200 and a
    # silently closed connection (seen live on real sites).
    ctx = ssl.create_default_context()
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except (NotImplementedError, AttributeError):
        pass
    return ctx


def fetch(url):
    """Return (text, content_type) or raise DriftError."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/json,text/plain,*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=_ssl_context()) as resp:
            raw = resp.read()
            ctype = resp.headers.get_content_type()
            charset = resp.headers.get_content_charset() or "utf-8"
    except http.client.IncompleteRead as e:
        # Some bot-mitigation edges truncate the body for
        # non-browser TLS clients. A partial page would fake a
        # diff, so this is an error, never a snapshot.
        raise DriftError(
            f"fetch failed for {url}: truncated body "
            f"({len(e.partial)} of {len(e.partial) + e.expected} "
            f"bytes) — the site's edge closed the connection early")
    except (urllib.error.URLError, OSError,
            http.client.HTTPException) as e:
        raise DriftError(f"fetch failed for {url}: {e}")
    try:
        return raw.decode(charset, errors="replace"), ctype
    except (LookupError, UnicodeError):
        return raw.decode("utf-8", errors="replace"), ctype


# ------------------------------------------------------------- extract
SKIP_TAGS = {"script", "style", "nav", "footer", "aside", "form", "svg",
             "noscript", "iframe", "button", "select", "textarea"}
BLOCK_TAGS = {"p", "div", "section", "article", "li", "ul", "ol", "tr",
              "table", "blockquote", "pre", "main", "header", "td", "th",
              "figure", "figcaption", "hr", "br"}
HEADINGS = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ",
            "h5": "##### ", "h6": "###### "}


class TextExtractor(HTMLParser):
    """Reduce an HTML page to clean markdown-ish lines."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.lines = []
        self.buf = ""
        self.prefix = ""

    def _flush(self):
        text = re.sub(r"\s+", " ", self.buf).strip()
        self.buf = ""
        if text:
            line = self.prefix + text
            if not self.lines or self.lines[-1] != line:
                self.lines.append(line)
        self.prefix = ""

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in HEADINGS:
            self._flush()
            self.prefix = HEADINGS[tag]
        elif tag == "li":
            self._flush()
            self.prefix = "- "
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag in HEADINGS or tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self.skip:
            self.buf += data


def extract_lines(text, ctype):
    """Page text (HTML or JSON or plain) -> raw content lines."""
    stripped = text.lstrip()
    if ctype == "application/json" or stripped[:1] in "{[":
        try:
            obj = json.loads(text)
            pretty = json.dumps(obj, indent=2, sort_keys=True,
                                ensure_ascii=False)
            return [ln.rstrip() for ln in pretty.splitlines() if ln.strip()]
        except (json.JSONDecodeError, ValueError):
            pass  # fall through and treat as plain text
    if "<" in text[:2000].lower() and "html" in text[:2000].lower() \
            or "<body" in text.lower():
        parser = TextExtractor()
        parser.feed(text)
        parser.close()
        parser._flush()
        return parser.lines
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


# ------------------------------------------------------- redact + noise
SECRET_RES = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{10,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{10,}"),
]


def redact(line):
    for rx in SECRET_RES:
        line = rx.sub(lambda m: (m.group(1) + "<redacted>")
                      if m.lastindex else "<redacted>", line)
    return line


NOISE_RES = [
    # ISO dates and datetimes: 2026-10-24, 2026-10-24T10:11:12Z, ...
    (re.compile(r"\b\d{4}-\d{2}-\d{2}"
                r"([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?"
                r"(Z|[+-]\d{2}:?\d{2})?)?\b"), "<date>"),
    # bare times: 10:11, 10:11:12, 9:05 PM
    (re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\s?([AaPp][Mm])?\b"), "<time>"),
    # relative times: 3 minutes ago, an hour ago, just now
    (re.compile(r"\b(\d+|a|an)\s+(second|minute|hour|day|week|month)s?"
                r"\s+ago\b"), "<rel-time>"),
    (re.compile(r"\bjust now\b"), "<rel-time>"),
    # UUIDs
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    # long hex tokens (session ids, hashes) >= 24 chars
    (re.compile(r"\b[0-9a-fA-F]{24,}\b"), "<hex>"),
    # session/csrf/token-ish query values inside URLs in the text
    (re.compile(r"(?i)([?&](session|sessionid|csrf|csrftoken|token|nonce|"
                r"sig|signature|auth|apikey|api_key)=)[^&\s\"'<>]+"),
     r"\1<redacted>"),
]


def normalize(line):
    """Return (normalized_line, changed?) — noise only, never prices."""
    out = line
    for rx, repl in NOISE_RES:
        out = rx.sub(repl, out)
    return out, out != line


def clean_lines(text, ctype):
    """extract -> redact -> normalize. Returns (lines, noise_count)."""
    lines, noise = [], 0
    for raw in extract_lines(text, ctype):
        norm, changed = normalize(redact(raw))
        if changed:
            noise += 1
        if norm:
            lines.append(norm)
    return lines, noise


# ---------------------------------------------------------------- store
def store_dir():
    path = os.environ.get("DRIFT_HOME") or os.path.expanduser("~/.drift-cli")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def slug_for(url):
    parsed = urlparse(url)
    base = re.sub(r"[^A-Za-z0-9]+", "-", parsed.netloc + parsed.path)
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"{base.strip('-')}-{digest}.json"


def snapshot_path(url):
    return os.path.join(store_dir(), slug_for(url))


def save_snapshot(url, lines, noise):
    snap = {
        "url": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lines": lines,
        "noise_filtered": noise,
        "sha": hashlib.sha256("\n".join(lines).encode()).hexdigest(),
    }
    fd = os.open(snapshot_path(url), os.O_WRONLY | os.O_CREAT
                 | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(snap, f, indent=2, ensure_ascii=False)
    return snap


def load_snapshot(url):
    path = snapshot_path(url)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def all_snapshots():
    snaps = []
    for name in sorted(os.listdir(store_dir())):
        if name.endswith(".json"):
            try:
                with open(os.path.join(store_dir(), name)) as f:
                    snaps.append(json.load(f))
            except (OSError, json.JSONDecodeError):
                continue
    return snaps


def display_url(url):
    """origin + path only — query strings never reach a report."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}" if p.scheme else url


# ----------------------------------------------------------------- diff
def diff_lines(old, new):
    """Return (added, removed, changed_pairs)."""
    added, removed, changed = [], [], []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        olds, news = old[i1:i2], new[j1:j2]
        if tag == "replace":
            used = set()
            for o in olds:
                best, best_ratio = None, 0.8
                for k, n in enumerate(news):
                    if k in used:
                        continue
                    r = difflib.SequenceMatcher(None, o, n).ratio()
                    if r >= best_ratio:
                        best, best_ratio = k, r
                if best is not None:
                    used.add(best)
                    changed.append((o, news[best]))
                else:
                    removed.append(o)
            added.extend(n for k, n in enumerate(news) if k not in used)
        elif tag == "delete":
            removed.extend(olds)
        elif tag == "insert":
            added.extend(news)
    return added, removed, changed


# ------------------------------------------------------------- commands
def do_watch(url):
    text, ctype = fetch(url)
    lines, noise = clean_lines(text, ctype)
    snap = save_snapshot(url, lines, noise)
    return (f"watching {display_url(url)}\n"
            f"snapshot saved: {len(lines)} lines, "
            f"noise filtered: {noise}, sha: {snap['sha'][:12]}")


def check_one(url):
    """Return a result dict for one URL."""
    snap = load_snapshot(url)
    if snap is None:
        return {"url": url, "status": "error",
                "error": "not watched — run `drift watch` first"}
    try:
        text, ctype = fetch(url)
    except DriftError as e:
        return {"url": url, "status": "error", "error": str(e)}
    lines, noise = clean_lines(text, ctype)
    if lines == snap["lines"]:
        return {"url": url, "status": "no_change", "added": [],
                "removed": [], "changed": [], "noise_filtered": noise,
                "snapshot_at": snap["fetched_at"],
                "line_count": len(lines)}
    added, removed, changed = diff_lines(snap["lines"], lines)
    return {"url": url, "status": "changed", "added": added,
            "removed": removed, "changed": changed,
            "noise_filtered": noise, "snapshot_at": snap["fetched_at"],
            "line_count": len(lines)}


def build_report(results):
    statuses = {r["status"] for r in results}
    overall = ("ERROR" if "error" in statuses else
               "CHANGED" if "changed" in statuses else "NO CHANGE")
    out = ["# drift report", "",
           f"Status: **{overall}**",
           f"Checked: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
           ""]
    for r in results:
        out.append(f"## {display_url(r['url'])}")
        if r["status"] == "error":
            out += [f"ERROR: {r['error']}", ""]
            continue
        out.append(f"Snapshot: {r['snapshot_at']} · {r['line_count']} lines · "
                   f"noise lines filtered on this fetch: "
                   f"{r['noise_filtered']}")
        out.append("")
        if r["status"] == "no_change":
            out += ["No changes.", ""]
            continue
        if r["changed"]:
            out.append("### Changed")
            for old, new in r["changed"]:
                out.append(f"- {old}")
                out.append(f"+ {new}")
            out.append("")
        if r["added"]:
            out.append("### Added")
            out.extend(f"+ {ln}" for ln in r["added"])
            out.append("")
        if r["removed"]:
            out.append("### Removed")
            out.extend(f"- {ln}" for ln in r["removed"])
            out.append("")
    return "\n".join(out).rstrip() + "\n", overall


def do_check(urls):
    results = [check_one(u) for u in urls]
    report, overall = build_report(results)
    code = 2 if overall == "ERROR" else 1 if overall == "CHANGED" else 0
    return results, report, overall, code


def resolve_targets(url_arg):
    if url_arg:
        if load_snapshot(url_arg) is None:
            raise DriftError(f"not watched: {url_arg} "
                             f"(run `drift watch {url_arg}` first)")
        return [url_arg]
    snaps = all_snapshots()
    if not snaps:
        raise DriftError("nothing watched yet — run `drift watch <url>`")
    return [s["url"] for s in snaps]


# ------------------------------------------------------------------ MCP
def mcp_server():
    """Minimal stdio MCP server exposing drift_watch + drift_check."""
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    tools = [
        {"name": "drift_watch",
         "description": "Snapshot a web page's clean text now, so a "
                        "later drift_check can report what changed.",
         "inputSchema": {"type": "object",
                         "properties": {"url": {"type": "string"}},
                         "required": ["url"]}},
        {"name": "drift_check",
         "description": "Compare a watched page (or all watched pages) "
                        "against its snapshot; returns the drift "
                        "report: NO CHANGE, or added/removed/changed "
                        "lines with noise filtered and secrets "
                        "redacted.",
         "inputSchema": {"type": "object",
                         "properties": {"url": {"type": "string"}}}},
    ]
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method, rid = req.get("method"), req.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "drift-cli", "version": VERSION}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}})
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                if name == "drift_watch":
                    text = do_watch(args["url"])
                elif name == "drift_check":
                    urls = resolve_targets(args.get("url"))
                    _, text, _, _ = do_check(urls)
                else:
                    text = f"error: unknown tool {name}"
            except (DriftError, KeyError) as e:
                text = f"error: {e}"
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "text": text}]}})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "method not found"}})


# ------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="drift",
        description="Memory for the web: snapshot pages, then ask "
                    "what actually changed.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch", help="Fetch a page and snapshot its "
                                     "clean text")
    w.add_argument("url")
    c = sub.add_parser("check", help="Diff watched pages vs snapshots; "
                                     "exit 0 NO CHANGE, 1 CHANGED, "
                                     "2 ERROR")
    c.add_argument("url", nargs="?", help="One watched URL "
                                         "(default: all watched)")
    c.add_argument("--out", default="drift-report.md",
                   help="Report path (default: ./drift-report.md)")
    c.add_argument("--json", action="store_true",
                   help="Print a machine-readable verdict as JSON")
    sub.add_parser("list", help="List watched URLs and snapshots")
    f = sub.add_parser("forget", help="Remove a page's snapshot")
    f.add_argument("url")
    sub.add_parser("mcp", help="Run the stdio MCP server")
    args = ap.parse_args(argv)

    if args.cmd == "mcp":
        mcp_server()
        return 0
    try:
        if args.cmd == "watch":
            print(do_watch(args.url))
            return 0
        if args.cmd == "list":
            snaps = all_snapshots()
            if not snaps:
                print("nothing watched yet")
                return 0
            for s in snaps:
                print(f"{s['url']}  ·  {s['fetched_at']}  ·  "
                      f"{len(s['lines'])} lines")
            return 0
        if args.cmd == "forget":
            path = snapshot_path(args.url)
            if not os.path.exists(path):
                print(f"error: not watched: {args.url}", file=sys.stderr)
                return 2
            os.remove(path)
            print(f"forgot {display_url(args.url)}")
            return 0
        # check
        urls = resolve_targets(args.url)
        results, report, overall, code = do_check(urls)
        with open(args.out, "w") as fh:
            fh.write(report)
        if args.json:
            print(json.dumps({
                "status": overall.lower().replace(" ", "_"),
                "results": [{
                    "url": display_url(r["url"]),
                    "status": r["status"],
                    "added": len(r.get("added", [])),
                    "removed": len(r.get("removed", [])),
                    "changed": len(r.get("changed", [])),
                    "noise_filtered": r.get("noise_filtered", 0),
                    "error": r.get("error"),
                } for r in results]}, indent=2))
        else:
            for r in results:
                if r["status"] == "error":
                    print(f"ERROR  {display_url(r['url'])}: {r['error']}")
                elif r["status"] == "no_change":
                    print(f"NO CHANGE  {display_url(r['url'])}")
                else:
                    print(f"CHANGED  {display_url(r['url'])}: "
                          f"+{len(r['added'])} -{len(r['removed'])} "
                          f"~{len(r['changed'])}")
            print(f"wrote {args.out}")
        return code
    except DriftError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
