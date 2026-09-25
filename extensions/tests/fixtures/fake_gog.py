"""A stand-in for the ``gog`` CLI (v0.34.1), for tests that drive Arc's real runner.

Only the verbs Arc's Google sign-in touches, with gog's real shapes, measured
against its source and the deployment:

* ``gog --version``
* ``gog auth add <email> --remote --step 1 ...`` prints ``auth_url<TAB><link>``.
  Like gog, the stored step-one state is keyed by ``state`` only, NOT by email,
  and step two refuses a state whose flags or OAuth client differ.
* ``gog auth add <email> --remote --step 2 ... --auth-url <url>`` exchanges it.
  The test's "Google" encodes who signed in into the code as
  ``code-for:<email>``; like gog, a sign-in as a different address is refused
  ("authorized as X, expected Y").
* ``gog auth list``
* ``gog gmail labels get INBOX ...`` — the sign-in check. Answers as GOG_ACCOUNT
  from the token bucket GOG_CLIENT selects: a working token prints ``id<TAB>INBOX``;
  none prints gog's "No auth for gmail"; one marked ``expired`` prints the
  ``invalid_grant`` refresh error measured on the deployment.

State lives under ``$FAKE_GOG_HOME``. Every invocation's argv and the
GOG_ACCOUNT / GOG_CLIENT it saw are appended to ``calls.jsonl`` so a test can
assert exactly what reached the process. A file named ``hang`` there makes every
call sleep, for timeout tests.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

HOME = Path(os.environ["FAKE_GOG_HOME"])
HOME.mkdir(parents=True, exist_ok=True)
ARGS = sys.argv[1:]
CLIENT = os.environ.get("GOG_CLIENT", "") or "default"
ACCOUNT = os.environ.get("GOG_ACCOUNT", "")


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")


def _log() -> None:
    with (HOME / "calls.jsonl").open("a", encoding="utf-8") as calls:
        calls.write(json.dumps({"argv": ARGS, "account": ACCOUNT, "client": CLIENT}) + "\n")


def _tokens() -> dict[str, str]:
    path = HOME / "tokens.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _fail(message: str, code: int = 1) -> None:
    sys.stderr.write(message + "\n")
    sys.exit(code)


def _flags_without(step_flags: list[str]) -> list[str]:
    """The flags a step-one state is matched on: everything but the step and address."""
    kept: list[str] = []
    skip = False
    for flag in step_flags:
        if skip:
            skip = False
            continue
        if flag in ("--step", "--auth-url"):
            skip = True
            continue
        kept.append(flag)
    return kept


def _auth_add() -> None:
    email, flags = ARGS[2], ARGS[3:]
    if "--remote" not in flags or "--step" not in flags:
        _fail("fake gog only knows the remote flow", 2)
    step = flags[flags.index("--step") + 1]
    matched = _flags_without(flags)
    if step == "1":
        state = secrets.token_urlsafe(12)
        port = 40000 + len(list(HOME.glob("state-*.json")))
        (HOME / f"state-{state}.json").write_text(
            json.dumps({"flags": matched, "client": CLIENT, "port": port})
        )
        redirect = quote(f"http://127.0.0.1:{port}/oauth2/callback", safe="")
        _say(
            "auth_url\thttps://accounts.google.com/o/oauth2/auth?access_type=offline"
            f"&client_id=fake.apps.googleusercontent.com&redirect_uri={redirect}"
            f"&response_type=code&scope=email&state={state}"
        )
        _say("state_reused\tfalse")
        sys.stderr.write("Run again with the same root flags and --remote --step 2\n")
        sys.exit(0)
    url = flags[flags.index("--auth-url") + 1]
    query = parse_qs(urlsplit(url).query)
    state, code = query["state"][0], query["code"][0]
    stored = HOME / f"state-{state}.json"
    if not stored.exists():
        _fail("manual auth state not found or expired; run step 1 again")
    recorded = json.loads(stored.read_text())
    if recorded["flags"] != matched or recorded["client"] != CLIENT:
        _fail("manual auth state does not match this client or these scopes")
    if urlsplit(url).port != recorded["port"]:
        _fail("redirect uri mismatch")
    stored.unlink()
    if not code.startswith("code-for:"):
        _fail('oauth2: "invalid_grant" "Malformed auth code."')
    who = code.split(":", 1)[1]
    if who != email:
        _fail(f"authorized as {who}, expected {email}")
    tokens = _tokens()
    tokens[f"{CLIENT}:{email}"] = "good"
    (HOME / "tokens.json").write_text(json.dumps(tokens))
    _say(f"email\t{email}\nclient\t{CLIENT}")
    sys.exit(0)


def _labels() -> None:
    tokens = _tokens()
    account = ACCOUNT
    if not account:
        mine = [key for key in tokens if key.startswith(f"{CLIENT}:")]
        if len(mine) != 1:
            _fail("missing --account (or set GOG_ACCOUNT)")
        account = mine[0].split(":", 1)[1]
    token = tokens.get(f"{CLIENT}:{account}")
    if token is None:
        _fail(f"No auth for gmail {account}.\n\nOAuth (browser flow):\n  gog auth add {account}")
    if token == "expired":
        _fail('refresh access token: oauth2: "invalid_grant" "Bad Request"')
    _say("id\tINBOX\nname\tINBOX\ntype\tsystem\nmessages_total\t10\nmessages_unread\t1")
    sys.exit(0)


def main() -> None:
    _log()
    if (HOME / "hang").exists():
        time.sleep(120)
    if ARGS == ["--version"]:
        _say("v0.34.1 (fake)")
        sys.exit(0)
    if ARGS[:2] == ["auth", "add"] and len(ARGS) > 2:
        _auth_add()
    if ARGS[:2] == ["auth", "list"]:
        tokens = _tokens()
        if not tokens:
            _say("No tokens stored")
        for key in tokens:
            client, email = key.split(":", 1)
            _say(f"{email}\t{client}\tgmail,calendar,drive")
        sys.exit(0)
    if ARGS[:4] == ["gmail", "labels", "get", "INBOX"]:
        _labels()
    _fail(f"fake gog: unknown command {ARGS!r}", 2)


if __name__ == "__main__":
    main()
