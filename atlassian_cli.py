#!/usr/bin/env python3
"""Atlassian CLI — browser-cookie-backed Jira/Confluence access.

A thin, predictable command-line wrapper over the same browser-cookie auth
the MCP server uses (atlassian_browser_auth.create_browser_session). It talks
straight to the Jira/Confluence Server/DC REST APIs with the authenticated
requests.Session, so there is no MCP transport or upstream mcp-atlassian layer
to break.

Auth: reuses cookies captured by the Chrome extension (see chrome-extension/)
via `import`. If cookies are missing or expired, export them with the extension
and run `import`, then retry. No browser is ever opened by this tool.

Env (required, no defaults — same as the rest of this project):
  JIRA_URL         e.g. <your-jira-host>
  CONFLUENCE_URL   e.g. <your-confluence-host>

Examples:
  atlassian-cli import ~/Downloads/atlassian-cookies.json
  atlassian-cli jira get PROJ-123 --comments
  atlassian-cli jira search 'project = PROJ AND status = "In Progress"' --max 10
  atlassian-cli confluence get 123456789 --markdown -o page.md
  atlassian-cli confluence search 'release process' --space DEV
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# JIRA_URL / CONFLUENCE_URL are required env vars (no hardcoded defaults — see
# BrowserAuthConfig.from_env, which raises if they are unset). Set them in your
# shell or .mcp.json for your Atlassian instance.

from atlassian_browser_auth import (  # noqa: E402
    BrowserAuthConfig,
    _cookie_matches_base_url,
    create_browser_session,
    probe_live,
    write_storage_state,
)


def _eprint(*a: Any) -> None:
    print(*a, file=sys.stderr, flush=True)


def _base(service: str) -> str:
    cfg = BrowserAuthConfig.from_env()
    return cfg.service_base(service)


def _session(service: str):
    """Build an authenticated session for the service (jira|confluence)."""
    return create_browser_session(service, _base(service))


def _api_error_messages(r) -> list[str]:
    """Extract Atlassian REST error strings (errorMessages + errors map)."""
    try:
        data = r.json()
    except ValueError:
        return []
    out = list(data.get("errorMessages") or [])
    errors = data.get("errors")
    if isinstance(errors, dict):
        out += [f"{k}: {v}" for k, v in errors.items()]
    return out


def _get_json(service: str, path: str, params: dict | None = None) -> Any:
    s = _session(service)
    # requests timeouts are in SECONDS: (connect, read).
    r = s.get(f"{_base(service)}{path}", params=params or {}, timeout=(10, 30))
    if r.status_code != 200:
        _eprint(f"HTTP {r.status_code} for {path}")
        ctype = r.headers.get("Content-Type", "?")
        # A structured JSON error (errorMessages/errors) is safe to show and far
        # more actionable than a byte count — surface it. For non-JSON bodies
        # (e.g. an HTML SSO page that could leak CSRF tokens / internal hosts)
        # keep to a hint instead of dumping the body.
        if "application/json" in ctype:
            msgs = _api_error_messages(r)
            for m in msgs:
                _eprint(f"  - {m}")
            if not msgs:
                _eprint(f"(JSON error, {len(r.content)} bytes)")
        else:
            _eprint(
                f"(response {len(r.content)} bytes, content-type={ctype}; "
                "if this looks like an SSO page, re-export cookies with the "
                "extension and run: atlassian-cli import <file>)"
            )
        sys.exit(2)
    return r.json()


# ---- HTML -> Markdown (best-effort, dependency-free) ----------------------
def _markdownify_fallback(html: str) -> str:
    """Crude tag-stripping fallback when the markdownify lib is unavailable."""
    import re
    from html import unescape

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def _html_to_markdown(html: str) -> str:
    try:
        from markdownify import markdownify as md  # type: ignore
    except ImportError:
        return _markdownify_fallback(html)
    try:
        return md(html, heading_style="ATX")
    except (ValueError, TypeError) as exc:
        _eprint(f"(markdownify failed: {exc}; using plain tag-strip fallback)")
        return _markdownify_fallback(html)


# ---- commands -------------------------------------------------------------
def cmd_import(args: argparse.Namespace) -> None:
    """Load cookies exported by the browser extension into the per-service jars.

    Splits the export by matching each cookie against JIRA_URL / CONFLUENCE_URL
    and writes each service's jar, then probes the REST API so the user gets
    immediate confirmation the imported session is live. On success (jars
    written), deletes the source export JSON so live cookies do not linger.
    """
    try:
        with open(args.file) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _eprint(f"cannot read cookie export {args.file}: {exc}")
        sys.exit(2)
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        _eprint(f"invalid export {args.file}: missing a 'cookies' list")
        sys.exit(2)

    services = [args.service] if args.service else ["jira", "confluence"]
    any_live = False
    any_matched = False
    for svc in services:
        cfg = BrowserAuthConfig.from_env(svc)
        base = cfg.service_base(svc)
        matched = [c for c in cookies if _cookie_matches_base_url(c, base)]
        if not matched:
            _eprint(f"{svc}: no cookies in the export match {base} — skipped")
            continue
        any_matched = True
        write_storage_state(matched, cfg.storage_state)
        status = probe_live(base, svc, matched, cfg.user_agent)
        if status == 200:
            any_live = True
            print(
                f"{svc}: imported {len(matched)} cookies -> HTTP 200 (live)  "
                f"[{cfg.storage_state.name}]"
            )
        else:
            print(
                f"{svc}: imported {len(matched)} cookies -> HTTP {status} "
                f"(NOT live; sign into {svc} in your browser and re-export)"
            )

    if not any_matched:
        _eprint(
            "No cookies matched your JIRA_URL / CONFLUENCE_URL hosts. Check the "
            "export file and that those env vars point at the right instance."
        )
        sys.exit(2)

    # Export holds live session cookies — remove it once jars are written so it
    # does not linger in Downloads (SECURITY.md). Keep the file only when import
    # never consumed it (parse error / no host match above).
    export_path = Path(args.file)
    try:
        export_path.unlink(missing_ok=True)
        print(f"removed export {export_path}")
    except OSError as exc:
        _eprint(f"warning: could not remove export {export_path}: {exc}")

    if not any_live:
        sys.exit(2)


def cmd_jira_get(args: argparse.Namespace) -> None:
    fields = args.fields or "summary,status,assignee,labels,description,comment"
    params = {"fields": fields, "expand": "renderedFields"}
    data = _get_json("jira", f"/rest/api/2/issue/{args.key}", params)
    if args.raw:
        print(json.dumps(data, indent=2))
        return
    f = data.get("fields", {})
    print(f"{data.get('key')}: {f.get('summary')}")
    print(f"Status:   {(f.get('status') or {}).get('name')}")
    print(f"Assignee: {(f.get('assignee') or {}).get('displayName')}")
    print(f"Labels:   {f.get('labels')}")
    print("\n--- Description ---")
    print(f.get("description") or "(empty)")
    if args.comments:
        cs = (f.get("comment") or {}).get("comments", [])
        print(f"\n--- Comments ({len(cs)}) ---")
        for c in cs:
            who = (c.get("author") or {}).get("displayName", "?")
            print(f"\n[{who} @ {c.get('created','?')}]")
            print(c.get("body", ""))


def cmd_jira_search(args: argparse.Namespace) -> None:
    # Jira Cloud removed GET /rest/api/2/search (410 Gone) in 2025; use the
    # enhanced-JQL endpoint. It requires a BOUNDED jql and an explicit fields
    # list, and returns {issues, nextPageToken, isLast} — no total/startAt
    # (token pagination). An unbounded query surfaces a clear 400 via _get_json.
    params = {"jql": args.jql, "maxResults": args.max,
              "fields": args.fields or "summary,status,assignee"}
    data = _get_json("jira", "/rest/api/2/search/jql", params)
    if args.raw:
        print(json.dumps(data, indent=2))
        return
    issues = data.get("issues", [])
    for it in issues:
        f = it.get("fields", {})
        st = (f.get("status") or {}).get("name", "?")
        who = (f.get("assignee") or {}).get("displayName", "-")
        print(f"  {it['key']:14} [{st:14}] {who:22} {f.get('summary','')}")
    more = "" if data.get("isLast", True) else "  (more available — raise --max)"
    print(f"\n  shown: {len(issues)}{more}")


def cmd_conf_get(args: argparse.Namespace) -> None:
    params = {"expand": "body.storage,version,space,ancestors"}
    data = _get_json("confluence", f"/rest/api/content/{args.page_id}", params)
    title = data.get("title", "")
    html = (((data.get("body") or {}).get("storage") or {}).get("value")) or ""
    ver = (data.get("version") or {}).get("number")
    space = (data.get("space") or {}).get("key")
    out = _html_to_markdown(html) if args.markdown else html
    header = f"# {title}\n\n> space={space} id={args.page_id} version={ver}\n\n"
    body = header + out
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(body)
        _eprint(f"wrote {len(body)} bytes to {args.out}  (v{ver}, space={space})")
    else:
        print(body)


def cmd_conf_search(args: argparse.Namespace) -> None:
    cql = args.cql
    if args.space and "space" not in cql.lower():
        cql = f'space = "{args.space}" AND text ~ "{args.cql}"'
    elif "~" not in cql and "=" not in cql:
        cql = f'text ~ "{args.cql}"'
    data = _get_json("confluence", "/rest/api/content/search",
                     {"cql": cql, "limit": args.limit})
    for r in data.get("results", []):
        print(f"  {r.get('id'):12} [{r.get('type')}] {r.get('title')}")
    print(f"\n  size: {data.get('size')}  cql: {cql}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atlassian-cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("import", help="import cookies exported by the browser extension")
    pi.add_argument("file", help="path to atlassian-cookies.json from the extension")
    pi.add_argument(
        "--service",
        choices=["jira", "confluence"],
        help="only import this service (default: both)",
    )
    pi.set_defaults(func=cmd_import)

    pj = sub.add_parser("jira", help="Jira commands").add_subparsers(dest="jcmd", required=True)
    g = pj.add_parser("get", help="get an issue")
    g.add_argument("key")
    g.add_argument("--comments", action="store_true")
    g.add_argument("--fields")
    g.add_argument("--raw", action="store_true")
    g.set_defaults(func=cmd_jira_get)
    s = pj.add_parser("search", help="JQL search")
    s.add_argument("jql")
    s.add_argument("--max", type=int, default=20)
    s.add_argument("--fields")
    s.add_argument("--raw", action="store_true")
    s.set_defaults(func=cmd_jira_search)

    pc = sub.add_parser("confluence", help="Confluence commands").add_subparsers(dest="ccmd", required=True)
    cg = pc.add_parser("get", help="get a page by id")
    cg.add_argument("page_id")
    cg.add_argument("--markdown", action="store_true")
    cg.add_argument("-o", "--out")
    cg.set_defaults(func=cmd_conf_get)
    cs = pc.add_parser("search", help="search pages (CQL or text)")
    cs.add_argument("cql")
    cs.add_argument("--space")
    cs.add_argument("--limit", type=int, default=10)
    cs.set_defaults(func=cmd_conf_search)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
