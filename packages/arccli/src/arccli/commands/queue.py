"""Account-authenticated ArcUI queue controls for the live coordinator."""

from __future__ import annotations

import argparse
import getpass
import json
from typing import Any, NoReturn
from urllib.parse import urlsplit

import httpx

from arccli.commands._shared import err, print_json, print_table, write


def _fail(message: str) -> NoReturn:
    err(f"arc queue: {message}")
    raise SystemExit(1)


def _url(raw: str) -> str:
    parsed = urlsplit(raw)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        _fail("server URL must contain only scheme and host")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        _fail("remote queue control requires HTTPS")
    return raw.rstrip("/")


def _request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = client.request(method, path, **kwargs)
    except httpx.HTTPError:
        _fail("operator server is unavailable")
    try:
        body = response.json()
    except ValueError:
        _fail("operator server returned an invalid response")
    if not isinstance(body, dict):
        _fail("operator server returned an invalid response")
    if response.status_code >= 400:
        _fail(str(body.get("error", "request refused")))
    return body


def _run(args: argparse.Namespace) -> None:
    url = _url(args.url)
    password = getpass.getpass("Arc account password: ")
    if not password:
        _fail("account password is required")
    with httpx.Client(base_url=url, timeout=10, follow_redirects=False, trust_env=False) as client:
        login = _request(
            client,
            "POST",
            "/api/auth/login",
            json={"email": args.email, "password": password},
        )
        token = login.get("token")
        if not isinstance(token, str) or not token:
            _fail("operator session is unavailable")
        client.headers["Authorization"] = f"Bearer {token}"
        try:
            if login.get("role") != "operator":
                _fail("an operator account is required")
            result = _operation(client, args)
        finally:
            try:
                client.post("/api/auth/logout")
            except httpx.HTTPError:
                pass
    if args.json:
        print_json(result)
    elif args.action == "jobs":
        print_table(
            ["Call", "State", "Owner", "Agent", "Version"],
            [
                [
                    job["call_id"],
                    job["state"],
                    job["owner_id"],
                    job.get("agent_id") or "",
                    job["version"],
                ]
                for job in result["jobs"]
            ],
        )
        if result.get("next_cursor"):
            write(f"Next cursor: {result['next_cursor']}")
    else:
        write(json.dumps(result, sort_keys=True))


def _operation(client: httpx.Client, args: argparse.Namespace) -> dict[str, Any]:
    if args.action == "jobs":
        params = {"limit": args.limit}
        if args.owner_id:
            params["owner_id"] = args.owner_id
        if args.state:
            params["state"] = args.state
        if args.cursor:
            params["cursor"] = args.cursor
        return _request(client, "GET", "/api/queue/jobs", params=params)
    if args.action == "status":
        return _request(client, "GET", "/api/queue/control")
    if args.action in {"pause", "resume"}:
        return _request(
            client,
            "POST",
            f"/api/queue/{args.action}",
            json={"expected_revision": args.expected_revision},
        )
    if args.action == "limits":
        return _request(
            client,
            "PUT",
            "/api/queue/limits",
            json={
                "expected_revision": args.expected_revision,
                "max_concurrent": args.max_concurrent,
                "max_queued": args.max_queued,
                "wait_timeout": args.wait_timeout,
                "history_limit": args.history_limit,
            },
        )
    return _request(
        client,
        "POST",
        "/api/queue/cancel",
        json={"call_id": args.call_id, "expected_version": args.expected_version},
    )


def queue_handler(argv: list[str]) -> None:
    """Run one queue command through the authenticated operator HTTP API."""
    parser = argparse.ArgumentParser(prog="arc queue")
    parser.add_argument("--url", default="http://127.0.0.1:8420")
    parser.add_argument("--email", required=True)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="action", required=True)
    jobs = sub.add_parser("jobs")
    jobs.add_argument("--limit", type=int, default=100)
    jobs.add_argument("--owner-id")
    jobs.add_argument("--state")
    jobs.add_argument("--cursor")
    sub.add_parser("status")
    for action in ("pause", "resume"):
        sub.add_parser(action).add_argument("--expected-revision", type=int, required=True)
    limits = sub.add_parser("limits")
    limits.add_argument("--expected-revision", type=int, required=True)
    limits.add_argument("--max-concurrent", type=int, required=True)
    limits.add_argument("--max-queued", type=int, required=True)
    limits.add_argument("--wait-timeout", type=float, required=True)
    limits.add_argument("--history-limit", type=int, required=True)
    cancel = sub.add_parser("cancel")
    cancel.add_argument("call_id")
    cancel.add_argument("--expected-version", type=int, required=True)
    args = parser.parse_args(argv)
    if args.action == "jobs" and not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    if args.action in {"pause", "resume", "limits"} and args.expected_revision < 0:
        parser.error("--expected-revision must be nonnegative")
    if args.action == "cancel" and args.expected_version < 0:
        parser.error("--expected-version must be nonnegative")
    _run(args)


__all__ = ["queue_handler"]
