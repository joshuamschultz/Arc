---
name: onepassword
description: When and how to use the 1Password connector — listing what credentials exist, reading an item's non-secret fields, and resolving a single op:// reference when a secret value is genuinely needed. TRIGGER when a request needs to know which credential exists, or needs one specific secret to complete a task the operator asked for. SKIP for storing, changing, or deleting anything; this connection only reads, and only one vault.
version: 2.0.0
---

# 1Password

## Contract

Given a request that touches credentials, this skill answers it with the least
disclosure that works: identity before fields, fields before values. Success is: a
secret value enters the transcript only when a specific operator request required
that specific value.

## Resources

Three read verbs against one vault.

| Verb | What it discloses |
|---|---|
| `onepassword_list_items` | Titles, ids, categories. No values. |
| `onepassword_get_item` | Non-secret fields; concealed fields named, values withheld |
| `onepassword_resolve_secret` | One secret value, for one `op://` reference |

## Knowledge

**The three verbs are a ladder, and you climb only as far as the question.** Most
requests stop at the first rung: "do we have a credential for X" is answered by
listing. "What username is on that login" is answered by the second. Only "give me
the password so I can do Y" reaches the third.

**Every call is gated.** This connection's approval mode is `all`, not `outbound`,
because the consequential act here is reading rather than sending. Expect a human
prompt on every verb, and make the prompt legible: the operator should recognise
the item you named.

**A resolved secret is in the transcript forever.** It goes into the session, may
reach the model provider, and lands in the audit record. Resolve one reference,
use it, and never resolve "the whole vault to have it handy".

**References are `op://vault/item/field`.** Build one from what the listing showed
you. A reference in any other shape is refused before it reaches 1Password.

**One vault, and it is not a parameter.** The vault is fixed in configuration and
scoped in 1Password itself by the service-account token. There is no argument that
widens it, and asking for another vault means asking the operator for another
connection.

**Never write a secret anywhere.** Not into a file, a commit, a ticket, a message,
or a summary. If a task needs the secret in a file, the operator puts it there.

## Steps

1. Ask what the request actually needs: existence, a non-secret field, or a value.
2. `onepassword_list_items` to find the item and its id — it takes no
   arguments, because the vault it reads is the operator's choice and not yours.
3. `onepassword_get_item(item=...)` when a username, URL, or note answers the
   question. It takes the item's title or its id, and returns concealed fields as
   labels with the values withheld — so it is safe to call to find out WHETHER a
   password exists.
4. `onepassword_resolve_secret` only when a specific value is required by a
   specific operator request — one reference, once.
5. Use the value for the task and do not repeat it in your answer.

## Red Flags & Rationalizations

- Resolving a secret to "check that it exists". Listing does that.
- Resolving several references because more than one might be needed.
- Echoing a resolved value back in a summary, or writing it into a file or ticket.
- Trying to reach a second vault.
- Reading a credential in the same turn as a send verb without noticing that the
  trifecta gate is about to fire — it is firing for a reason.

| Rationalization | Rebuttal |
|---|---|
| "I need the value to confirm the item is right." | The title and the id confirm that. The value confirms nothing extra. |
| "I'll fetch them all now to save prompts." | Each prompt is the control. Batching is removing it. |
| "Putting it in the config file completes the task." | Writing a secret to disk is the operator's decision, never yours. |

## Validation

Before calling: the rung matches the question, and any `op://` reference names one
field of one item found by listing. After calling: no secret value appears in the
answer, in a file, or in any message, and no more references were resolved than the
task required.

## Examples

Does a credential exist:

    onepassword_list_items()

What account is that login for:

    onepassword_get_item(item="abcd1234...")
    # username and URL come back; the password field is named, not returned.

The operator asked you to call an API that needs its key:

    onepassword_resolve_secret(reference="op://Automation/Acme API/credential")
    # use it in the call; do not repeat it in the answer.
