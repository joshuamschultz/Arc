# Google accounts — connect, reconnect, and stop the weekly expiry

> **Runbooks** · Operate · **For** operators connecting Gmail, Calendar and Drive

Each Google account is its own connection of the **Google Workspace** bundle.
Arc drives the `gog` program on the Arc host to sign each one in from the
browser, and checks each one with a real read-only Gmail call made as that
account. The refresh token stays in `gog`'s keyring on the host; Arc never sees
it.

## What the card tells you

| Card says | What it means | What to do |
|---|---|---|
| **Working** | A read of this account's inbox label succeeded just now. | Nothing. |
| **Reconnect needed** | A token is stored but Google no longer accepts it (`invalid_grant`). | Click **Reconnect** and sign in again. See [why it keeps happening](#stop-the-weekly-expiry). |
| **Not signed in** | No token for this account on the host. | Click **Sign in**. |
| **Not installed** | `gog` is not on the host's `PATH`. | Use **Install** on the bundle card, or follow its manual steps. |

`gog auth list` is no longer the check. It only proves a token is *stored*: an
account whose token Google revoked lists exactly like a working one.

## Add an account

Do [the durable fix](#stop-the-weekly-expiry) first, once per host, so you have
an OAuth client name (below: `arc`).

1. Open **Connections**. On the **Google Workspace** bundle card click **Add an
   account**.
2. Fill in:

    | Field | Type exactly | Meaning |
    |---|---|---|
    | name | `hello` | The connection's short name. |
    | account | `hello@joshuaschultz.com` | The Google address this connection reads. |
    | client | `arc` | Your OAuth client's name on this host. |
    | read_only | `yes` | Read mail, calendar and Drive only (recommended). Choose `no` only if an agent must draft or send mail. |

    Choose which agents may use it. Save.
3. On the new connection's card click **Sign in**, then **Open Google sign-in**.
4. In the Google page, sign in **as that exact address** and click **Allow**.
5. The browser lands on a `http://127.0.0.1:…/oauth2/callback?…` page that fails
   to load. That is expected. Copy the **whole** address from the browser bar.
6. Paste it into the card and click **Finish sign-in**. The card re-checks the
   account and shows **Working**.

Repeat for every account. Accounts are independent: `blackarc`
(`josh@blackarcindustrial.com`), `systems` (`josh@blackarcsystems.com`) and
`hello` (`hello@joshuaschultz.com`) each have their own connection, their own
sign-in and their own check.

What the sign-in asks Google for:

| read_only | Google permissions requested |
|---|---|
| `yes` (default) | Gmail read-only, Calendar read-only, Drive read-only |
| `no` | Gmail full (read, draft, send), Calendar, Drive read-only |

Nothing else — no Chat, Photos, Ads or other services. Changing `read_only`
takes effect at the next **Reconnect**. With `yes`, the draft and send tools
fail with a permission error, by design.

Rules the card enforces:

- One Google sign-in may be waiting at a time on a host. Finish it (or wait
  about nine minutes for it to expire) before starting another account.
- The pasted address must be the loopback callback from *this* sign-in. An
  address from an older attempt, another host, or with extra parts is refused
  before anything runs.
- If Google signs in a different address than the connection names, the
  sign-in is refused ("authorized as X, expected Y"). Start again and pick the
  right account in Google's account chooser.
- A pasted address is single-use. After a failed finish, click **Open Google
  sign-in** again.

## Reconnect an account

Click **Reconnect** on the card and follow steps 3–6 above. Knowledge sync for
that account resumes on its own: a source stopped by a dead credential is
rechecked about once an hour. To sync at once, use **Sync now** for the source
under **Knowledge → Connections**.

## How agents use several accounts

An agent granted several Google connections gets **one** set of Google tools,
not one per account. Each call names the account it is for:

- The tools take an optional `account` argument (the address).
- An agent granted exactly one Google account may leave it out.
- An agent granted several must name one. If it does not, the call fails and
  lists the accounts that agent may use.
- The address must match a connection **granted to that agent**. Case does not
  matter; spaces, look-alike letters, gog aliases and connection names do not
  match. An address the agent was not granted is refused and audited
  (`connector.account.denied`).
- The call always runs as the matched connection's own account and OAuth
  client. No tool can pass `--account`, `--client` or `--home` to gog; a value
  that tries is refused before gog runs.
- Every answer says which connection and account it came from, and the audit
  record (`connector.account.routed`) names the same.

To keep an agent from writing at all, leave the connection's `read_only` at
`yes`: every draft, label, trash and send tool then refuses with a sentence.
To stop one agent from using a single tool (for example
`google_gmail_send`) while another may, add it to that agent's tool policy deny
list in its `arcagent.toml`:

```toml
[tools.policy]
deny = ["google_gmail_send", "google_gmail_forward"]
```

Every write and send also still waits for your approval, unless you relaxed
the connection's approval mode.

### The tools

| Tool | What it does | Kind |
|---|---|---|
| `google_gmail_labels`, `google_gmail_label` | List labels; read one label with counts | read |
| `google_gmail_search` | Search threads with Gmail search syntax (1-100 per page) | read |
| `google_gmail_messages` | List messages for a query (also used by Knowledge sync) | read |
| `google_gmail_message` | Read one message: headers, text, attachment names | read |
| `google_gmail_thread`, `google_gmail_thread_attachments` | Read a whole thread; list its attachments | read |
| `google_gmail_history` | Changes since a history id (also used by Knowledge sync) | read |
| `google_gmail_attachment` | Download one attachment (max 25 MB) into `downloads/google_workspace/<connection>/` in the agent's workspace | read |
| `google_gmail_drafts`, `google_gmail_draft_get` | List drafts; read one | read |
| `google_gmail_draft`, `google_gmail_draft_update`, `google_gmail_draft_delete` | Create, change, delete a draft | write |
| `google_gmail_modify`, `google_gmail_thread_modify` | Add or remove labels (star, archive, read state) | write |
| `google_gmail_mark_read`, `google_gmail_mark_unread`, `google_gmail_archive` | Common label changes | write |
| `google_gmail_trash`, `google_gmail_untrash` | Move to Trash and back | write |
| `google_gmail_send`, `google_gmail_draft_send`, `google_gmail_reply`, `google_gmail_reply_all`, `google_gmail_forward` | Send mail | send (egress) |
| `google_drive_list` | List or filter Drive files | read |
| `google_calendar_list`, `google_calendar_events`, `google_calendar_freebusy` | Calendars, events, availability | read |

Mail, thread, draft, search, Drive and Calendar results are marked as untrusted
content: an agent must never follow instructions found in them.

## After an update: Approve each Google connection

Arc pins each connection's tool list (its "contract"). When an update adds or
changes Google tools, those tools stay switched off for a connection until you
approve them once:

- **Arc web:** Connections → the Google connection's card → **Approve** (with
  operator controls on). Repeat for each Google connection.
- **Terminal:** `arc connector approve <connection>`, for example
  `arc connector approve blackarc`.

The agents pick the change up on their next reconcile or restart. Until you
approve, the audit log shows `connector.tool.contract.unapproved` (a tool that
is new) or `connector.tool.contract.suspend` (a tool that changed) for that
connection; a call routed to it for a switched-off tool is refused with
"approve it with Approve on that connection's card". An approved contract is
not reported again on restart.

## Stop the weekly expiry

**Why tokens die every week.** Google expires refresh tokens after seven days
for an OAuth app whose consent screen is in **Testing**. `gog`'s client, or any
client you created and left in Testing, has this limit. The fix is your own
Google Cloud OAuth client with its app **published to "In production"**.

### 1. Create the client (Google Cloud console, once)

1. Go to <https://console.cloud.google.com/> and create a project (for example
   `arc-google`).
2. **APIs & Services → Library**: enable **Gmail API**, **Google Calendar API**
   and **Google Drive API**.
3. **Google Auth Platform → Branding** (older consoles: **OAuth consent
   screen**): set an app name and your support email.
4. **Audience**: choose **External**, then click **Publish app** so the status
   reads **In production**. Do not leave it in Testing.
5. **Data access**: add the Gmail, Calendar and Drive scopes (the sign-in asks
   for Gmail, Calendar, and Drive read-only).
6. **Clients → Create client**: application type **Desktop app**. Download the
   JSON file (`client_secret_….json`).

If every account lives in ONE Google Workspace organisation, you can instead set
the audience to **Internal**: no warning screen, no user cap, no weekly expiry.
That does not work across different domains or for a personal @gmail.com
address.

### 2. Store it on the Arc host (terminal on the host, once)

Copy the JSON to the host, then, as the user that runs Arc, with the same
keyring settings as the Arc service (`GOG_KEYRING_PASSWORD` set,
`DBUS_SESSION_BUS_ADDRESS=/dev/null` on a headless host):

```bash
gog auth credentials set ~/client_secret.json --client arc
gog auth credentials list        # shows the "arc" client
shred -u ~/client_secret.json    # the copy gog stored is the one that is used
```

`gog auth credentials set - --client arc` reads the JSON from stdin instead of a
file. `arc` is the client name; any lowercase name (letters, digits, `-`, `_`)
works — type the same name into each connection's **client** field.

### 3. Point each connection at it (Arc)

On each Google connection card click **Edit details**, set **client** to `arc`,
save, then **Reconnect**. The sign-in runs `gog auth add <account> --client=arc
--readonly=yes --services gmail,calendar,drive --drive-scope readonly …`; the
token is stored under the `arc` client and every `gog` call for that connection
uses it (`GOG_CLIENT=arc`).

### Legacy fallback: no client

A connection with a blank **client** uses `gog`'s built-in client. Its sign-ins
expire after about 7 days. The card shows that warning, and signing in without
a client needs an explicit **Sign in with the built-in client anyway**. Use it
only until your own client exists.

## Honest limits

- **Warning screen.** An unverified External app shows "Google hasn't verified
  this app". Click **Advanced → Go to <app name> (unsafe)**. It is your own app.
  A Workspace admin can remove the warning for their domain by marking the app
  **Trusted** under **Admin console → Security → API controls**.
- **100-user cap.** An unverified External app may be used by at most 100
  distinct Google accounts, ever. Plenty for a few operator mailboxes.
- **Restricted Gmail scopes.** Full Gmail access is a *restricted* scope. Google
  requires verification and a security assessment only to lift the warning and
  the cap for a public app; personal use under the cap works unverified.
- **Tokens can still die.** In production a token no longer expires weekly, but
  Google still revokes it when the account's password changes, when access is
  removed at <https://myaccount.google.com/permissions>, after six months unused,
  or when more than 100 tokens exist for one client and account. The card will
  show **Reconnect needed**; reconnect as above.
- **Service accounts do not help personal accounts.** `gog` supports service
  accounts only with Workspace domain-wide delegation. A personal @gmail.com
  address has no domain to delegate from, so the browser sign-in above is the
  only way in.

## Troubleshooting

| Message | Fix |
|---|---|
| "a sign-in for X is still waiting" | Finish that sign-in, or wait for it to expire. |
| "no sign-in is waiting …" | The begun sign-in expired or was spent. Click **Open Google sign-in** again. |
| "that address does not point at this computer" | Copy the address of the page that failed to load, not the Google page. |
| "authorized as X, expected Y" | Sign in as the connection's address; use Google's account chooser. |
| "this connection's account is not a plain email address" | **Edit details** and fix the account. |
| Reconnect needed again after a week | The connection is not using a published client. Do [the durable fix](#stop-the-weekly-expiry). |
| "this agent holds several google_workspace connections; pass account …" | Name the account in the call, or grant the agent only one. |
| "account does not name a connection this agent may use" | Grant that connection to the agent, or use one of the listed accounts. |
| "… is signed in read-only, so … cannot run" | Set `read_only` to `no` with **Edit details**, then **Reconnect**. |
| "… is not approved for the connection …" | Click **Approve** on that connection's card. |
