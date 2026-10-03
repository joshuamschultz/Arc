# Atlassian and GitHub — set up once, connect

> **Runbooks** · Operate · **For** operators connecting Jira, Confluence and GitHub

Jira and Confluence share one Atlassian sign-in app. You set it up once, then
each connection is one click. GitHub uses a token you paste. In both cases Arc
holds the credential in its own sealed storage. No helper program (`acli`) and
no `gh auth login` is involved.

## Atlassian (Jira and Confluence)

### One-time setup (Atlassian developer console)

1. Open [developer.atlassian.com/console/myapps](https://developer.atlassian.com/console/myapps/)
   and create an **OAuth 2.0 (3LO)** app.
2. **Permissions:** add the **Jira API** and the **Confluence API**, and grant
   these scopes:
    - Jira: `read:jira-work`, `read:jira-user`, `write:jira-work`
    - Confluence: `read:confluence-content.all`, `read:confluence-space.summary`,
      `search:confluence`, `write:confluence-content`
3. **Authorization:** set the **Callback URL** to exactly the address ArcUI
   shows in **Set up Atlassian sign-in** (it is
   `http://127.0.0.1:<port>/oauth/callback`, or
   `https://<public_base_url host>/oauth/callback` when `[ui] public_base_url`
   is set). Atlassian matches it exactly. If Atlassian refuses an `http://`
   loopback address, set `[ui] public_base_url` to an `https://` address.
4. **Settings:** copy the **Client ID** and **Secret**.
5. In ArcUI open **Connections**, find the Jira card, click **Set up Atlassian
   sign-in**, paste both, and Save. Jira and Confluence use the same slot.
6. Click **Connect** on the Jira card, then on the Confluence card. Each opens
   Atlassian's consent page. Choose the site and click **Accept**.

Each connection has a **site** field (for example `yourcompany.atlassian.net`).
Leave it blank when your account reaches only one site; Connect fills it in.
When your account reaches several, type the one this connection is for. Arc
refuses a sign-in that does not reach that site and stores nothing.

### How Arc keeps it working

Atlassian rotates refresh tokens: each use issues a new one and retires the
old one. Arc stores the new token before it uses the new access token, and only
one process refreshes at a time, so two agents never spend one token twice.
Atlassian expires a refresh token after 90 days without use.

### Needs you

| Card says | What to do |
|---|---|
| **Needs you: sign-in expired or was revoked** | Click **Reconnect**. |
| **Needs you: signed in as a different account** | Connect again and choose the right site. |
| **Needs you: allow some permissions** | Connect again and leave every permission ticked. |

When you remove a connection, Arc deletes its stored sign-in. Atlassian has no
revocation address, so also remove Arc from your Atlassian account's connected
apps (id.atlassian.com, **Profile > Connected apps**).

## GitHub

GitHub keeps the `gh` program. Arc places your token as `GH_TOKEN` in the
environment of each `gh` call, and nowhere else: not in a file, not on a command
line. `gh` is pointed at an empty config folder of its own, so it can never use
the account you signed `gh` in with on this machine.

1. Open [github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens)
   and create a **fine-grained** token.
2. Choose the repositories it may reach.
3. Give it read-only **Contents**, **Issues**, **Pull requests**, **Actions**
   and **Metadata**. Set an expiry of one year or less.
4. In ArcUI open **Connections**, find the GitHub card, click **Connect**, and
   paste the token.

If you used `gh auth login` on this machine for Arc, you may run `gh auth
logout` there. Arc no longer reads it.

GitHub cannot renew a pasted token. Every check reads the expiry date GitHub
reports. Seven days before it lapses the card turns **Needs you: token expires
in N days**; create a new token and use **Replace credentials**.
