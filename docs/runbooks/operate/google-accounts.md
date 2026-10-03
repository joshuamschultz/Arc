# Google accounts — set up once, connect in one click

> **Runbooks** · Operate · **For** operators connecting Gmail, Calendar and Drive

Each Google account is its own connection of the **Google Workspace** bundle.
Arc talks to Google directly over its REST APIs. Arc holds the sign-in (a
refresh token) in its own sealed storage and renews the short-lived access
token itself. No helper program and no keyring are involved.

Arc checks each connection in the background with a real read-only Gmail call
made as that account, and keeps the answer in one health record per connection.

## What the card tells you

| Card says | What it means | What to do |
|---|---|---|
| **Healthy · checked 4 min ago** | Arc's last check read this account's mailbox successfully. | Nothing. |
| **Needs you: Google sign-in expired or was revoked** | Google no longer accepts the saved sign-in (`invalid_grant`). | Click **Reconnect Google**. See [honest limits](#honest-limits) for why it happens. |
| **Needs you: Not connected yet** | No sign-in is stored for this connection. | Click **Reconnect Google**. |
| **Needs you: signed in as a different account** | Google signed in an address other than the one the connection names. | Click **Reconnect Google** and pick the right account in Google's chooser. |
| **Syncing** | An agent is indexing this account right now. | Nothing. |
| **Error: Google is not answering** | Google, or the network, failed three checks in a row. Arc keeps retrying. | Usually nothing. Use **Advanced > Check now**. |
| **Not checked yet** | Arc has not looked at this connection yet. | Wait a minute, or use **Advanced > Check now**. |

One chip, one button. The chip is a stored fact: opening the Connections page
never contacts Google and never reads a credential. Arc re-checks every 30
minutes, every 10 while the status is **Error**, and every 5 while it is
**Needs you**. When a connection starts needing you, Arc sends you one message
on the channel you last used, and one more when it works again.

## One-time setup (Google Cloud console)

Do this once per Arc install. All accounts share the one client.

1. Open the Google Cloud console and pick (or create) a project.
2. **APIs & Services > Library**: enable the **Gmail API**, the **Google
   Calendar API** and the **Google Drive API**.
3. **OAuth consent screen** (Branding and Audience):
    - User type **External**. Choose **Internal** only if every account is in
      one Google Workspace organization.
    - Add these scopes: `openid`, `email`, `gmail.readonly`,
      `calendar.readonly`, `drive.readonly`.
    - Also add `gmail.modify`, `gmail.compose` and `gmail.send` if any
      connection will use `read_only = no`.
    - **Publish the app to Production.** An app left in **Testing** has its
      sign-ins expire after 7 days.
4. **Clients > Create client**:
    - Choose **Desktop app** when you reach ArcUI on the same machine at
      `http://127.0.0.1:<port>`. Desktop clients accept any loopback port and
      path, so there is nothing to register.
    - Choose **Web application** when `[ui] public_base_url` is set. Under
      **Authorized redirect URIs** add exactly
      `https://<public_base_url host>/oauth/callback`. A Web client may also
      register `http://127.0.0.1:8420/oauth/callback` for local use.
5. Copy the **client ID** and **client secret**.

## Give Arc the client

In ArcUI open **Connections**, find the Google card, and click **Set up Google
sign-in**. The panel shows the redirect address Arc will use (with a **Copy**
button). Paste the client ID and client secret and click **Save app**. The
secret is stored once and never shown again.

From a terminal on the host: `arc connector oauth-app google`.

## Add an account

1. On the **Google Workspace** bundle card click **Add an account**.
2. Fill in:

    | Field | Type exactly | Meaning |
    |---|---|---|
    | name | `hello` | The connection's short name. |
    | account | `hello@joshuaschultz.com` | The Google address this connection reads. |
    | read_only | `yes` | Read mail, calendar and Drive only (recommended). Choose `no` only if an agent must draft or send mail. |

    Choose which agents may use it. Save.
3. On the new connection's card click **Connect** (one click). Google opens in
   a new tab.
4. Sign in **as that exact address** and click **Allow**.
5. The tab says **Connected. You can close this tab.** The card turns
   **Healthy** by itself.

If the browser is on another machine than Arc, or the card does not update:
open the card's **Didn't come back? Paste the address you landed on** box and
paste the whole address from the browser bar. From a terminal, run
`arc connector authorize <name>`; it prints the Google link and asks for that
address.

Accounts are independent: each has its own connection, its own sign-in and its
own check.

What the sign-in asks Google for:

| read_only | Google permissions requested |
|---|---|
| `yes` (default) | Gmail read-only, Calendar read-only, Drive read-only |
| `no` | Gmail read, modify, compose and send; Calendar read-only; Drive read-only |

Nothing else. Changing `read_only` takes effect at the next **Reconnect**. With
`yes`, the draft, label, trash and send tools refuse with a plain sentence and
make no request to Google.

Rules Arc enforces:

- If Google signs in a different address than the connection names, the
  sign-in is refused. Start again and pick the right account.
- A sign-in link and its returned address are single-use and expire after a
  few minutes. After a failed finish, click **Connect** again.

## Reconnect an account

Click **Reconnect Google** on the card, then **Reconnect**. Knowledge sync for
that account resumes on its own: a source stopped by a dead credential is
rechecked about once an hour. To sync at once, use **Sync now** under
**Knowledge > Connections**.

## How agents use several accounts

An agent granted several Google connections gets **one** set of Google tools,
not one per account. Each call names the account it is for:

- The tools take an optional `account` argument (the address).
- An agent granted exactly one Google account may leave it out.
- An agent granted several must name one. If it does not, the call fails and
  lists the accounts that agent may use.
- The address must match a connection **granted to that agent**. Case does not
  matter; spaces, look-alike letters and connection names do not match. An
  address the agent was not granted, or one with no connection at all, is
  refused by name in the tool result and audited (`connector.account.denied`).
- The call always runs as the matched connection's own account and sign-in.
- Every answer says which connection and account it came from, and the audit
  record (`connector.account.routed`) names the same.

To keep an agent from writing at all, leave the connection's `read_only` at
`yes`. To stop one agent from using a single tool (for example
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
| `google_gmail_attachment` | Download one attachment (max 25 MB) into the agent's workspace downloads folder | read |
| `google_gmail_drafts`, `google_gmail_draft_get` | List drafts; read one | read |
| `google_gmail_draft`, `google_gmail_draft_update`, `google_gmail_draft_delete` | Create, change, delete a draft | write |
| `google_gmail_modify`, `google_gmail_thread_modify` | Add or remove labels (star, archive, read state) | write |
| `google_gmail_mark_read`, `google_gmail_mark_unread`, `google_gmail_archive` | Common label changes | write |
| `google_gmail_trash`, `google_gmail_untrash` | Move to Trash and back | write |
| `google_gmail_send`, `google_gmail_draft_send`, `google_gmail_reply`, `google_gmail_reply_all`, `google_gmail_forward` | Send mail | send (egress) |
| `google_drive_list` | List or filter Drive files | read |
| `google_drive_files`, `google_drive_changes`, `google_drive_file`, `google_drive_drives`, `google_drive_read` | Drive indexing: page files, read the change feed, read one file as text (Docs, Sheets, Slides exported; pdf, docx, xlsx, md, txt, html downloaded; 10 MB cap) | read |
| `google_calendar_list`, `google_calendar_events`, `google_calendar_freebusy` | Calendars, events, availability | read |

Drive is indexed as its own knowledge source beside Gmail (`<connection>:drive`).
In ArcUI, open the connection's Knowledge card for Drive and choose All of Drive, a
shared drive, or top-level folders (a folder covers its whole subtree), then approve
the mapping. Sync is incremental through the Drive change feed. Trashed files and
files that are no longer shared with the account leave the index. A file over 10 MB
or of a type no extractor reads is skipped, with a per-file finding. A folder moved
or trashed as a whole does not retract its files until each file itself changes.

Mail, thread, draft and search results are framed as untrusted content: an
agent must never follow instructions found in them. A recipient or subject with
a line break is refused before anything is sent.

## After an update: Approve each Google connection

Arc pins each connection's tool list (its "contract"). When an update adds or
changes Google tools, those tools stay switched off for a connection until you
approve them once:

- **Arc web:** Connections > the Google connection's card > **Approve** (with
  operator controls on). Repeat for each Google connection.
- **Terminal:** `arc connector approve <connection>`.

Until you approve, the audit log shows `connector.tool.contract.unapproved` (a
new tool) or `connector.tool.contract.suspend` (a changed tool) for that
connection, and a call to a switched-off tool is refused.

## Moving an existing install off `gog` (DGX)

Connections made before native sign-in have no stored sign-in, so they show
**Reconnect Google**. That is expected.

1. Do the [one-time setup](#one-time-setup-google-cloud-console). The existing
   `arc` client works: a Desktop app client needs nothing registered.
2. **Set up Google sign-in** on the card with that client's ID and secret.
3. Click **Reconnect Google** on each connection (for example `blackarc` and
   `systems`). Wait for both to read **Healthy**.
4. Then clean up the host:
    - remove `GOG_KEYRING_PASSWORD` from `arc.env`;
    - delete `~/.local/share/gogcli` and `~/.config/gogcli` (they hold the old
      sign-ins);
    - uninstall `gog`.
5. Bind or retire `hello@joshuaschultz.com`. An account with no connection is
   refused by name in the tool result: add a connection for it, or fix the job
   prompt that names it.

## Dropbox

Dropbox uses the same flow. Once, click **Set up Dropbox sign-in** on the
Dropbox card and paste the app key and app secret. A connection that already
has a refresh token keeps working as soon as the app slot exists. New
connections use **Connect**; Dropbox shows a code on its own page, so the card
asks you to paste that code.

## Honest limits

- **Unverified-app screen.** Gmail scopes are sensitive or restricted. Until
  Google verifies your app, each person signing in sees a "Google hasn't
  verified this app" warning and must click through it.
- **100-user cap.** An unverified app can have at most 100 signed-in users.
  Going beyond that needs Google's verification (and, for restricted Gmail
  scopes, a security assessment).
- **Sign-ins can still die.** Google revokes a refresh token when:
    - the account password changes (for Gmail scopes);
    - the user removes Arc's access in their Google account;
    - it is unused for 6 months;
    - the app is still in **Testing** status (7 days);
    - the account already has more than 100 live tokens for this client.

  The card then reads **Needs you**; click **Reconnect Google**.

## Troubleshooting

| What you see | Cause | Fix |
|---|---|---|
| The Google card has **Set up Google sign-in** but no **Connect** | No client is stored yet. | Paste the client ID and secret. |
| Google shows `redirect_uri_mismatch` | A Web client does not list the redirect address Arc used. | Add the address the panel shows to **Authorized redirect URIs**, or use a Desktop client. |
| Google shows `access_denied` or "app not verified" with no Continue | The account is not a test user and the app is in Testing, or the 100-user cap is reached. | Publish to Production, or add the account as a test user. |
| The tab says the link expired | The sign-in took too long. | Click **Connect** again. |
| **Needs you: signed in as a different account** | You picked another address in Google. | Reconnect and choose the named account. |
| Sign-in dies about a week after connecting | The app is in Testing. | Publish to Production, then reconnect. |
| The browser is on another machine and the tab fails to load | The redirect points at an address only Arc's machine reaches. | Copy the full address from the browser bar into **Didn't come back?** or `arc connector authorize <name>`. |
