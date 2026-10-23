# Safety and honest limits

PhonePilot moves real posts on real accounts. This page is the short version of
what it will and will not do, and what you are agreeing to when you run it.

## Who this is for

Your own phone, your own accounts, and accounts you are explicitly allowed to
operate. If you run a client's account, get written permission that covers
automation, not just posting.

Do not use this to operate someone else's phone, to run a fleet of fake
accounts, or to test whether a platform's detection works.

## Confirmation is layered on purpose

A `social_post` step has three gates:

1. **Template validation.** `confirm_required` must be `true`. A template with
   `false` does not load.
2. **The terminal.** The engine asks before it sends the job to the board.
   Declining aborts the run and records `declined`.
3. **The phone.** `PhonePilot Sync` asks `Post to <platform>?` and then asks
   whether you actually published. It cannot tap Post for you.

A scheduled run has no terminal, so it declines by default. `autorun --yes`
removes gate 2 for that invocation. It does not remove gate 3, because gate 3
lives in a Shortcut you control. If you want unattended posting, you have to
edit your own phone, and you should think hard before you do.

## Platform terms vary

Every platform has its own automation rules, and they change.

- Instagram has no public posting API for personal accounts. PhonePilot uses
  the share sheet and your hands. Tools that claim fully automatic personal
  posting are usually automating the UI, which is fragile and may violate the
  terms you agreed to.
- X, LinkedIn, TikTok, Facebook, and YouTube each treat automation, scheduling,
  and API access differently. Some allow scheduling through official tools and
  prohibit third party automation. Some require business accounts for API
  posting.
- Rate limits, content rules, and disclosure requirements are yours to check.

This project does not interpret terms of service for you, and nothing here is
legal advice. If your account matters to you, read the current rules for that
platform before you schedule anything.

## What is blocked in code

The validator refuses templates containing:

- `auto_like`, `auto_follow`, `auto_comment`, `auto_dm`
- `engagement_boost`
- `bypass_rate_limit`, `rate_limit_bypass`
- `rotate_proxy`, `spoof_device`
- `retry_on_429`
- `retry_on` with HTTP 403 or 429
- `social_post` with `confirm_required: false`
- `social_post` with `mode: api` in v0.1.x

These are mechanical checks, not a moral guarantee. You can edit the source.
The point is that the default path makes the honest choice, and the dishonest
one takes deliberate work.

## What PhonePilot does not do

- It does not fake engagement, followers, views, or comments.
- It does not rotate proxies or spoof devices to get around limits.
- It does not solve CAPTCHAs.
- It does not scrape other people's accounts.
- It does not use private iOS APIs or jailbreak anything.
- It does not post while you are not looking, unless you explicitly removed
  both the terminal gate and the phone gate.

## Data and secrets

| Data | Where it lives | Notes |
|---|---|---|
| Board token | `config.h`, registry, `PHONEPILOT_TOKEN` | Treat it like a Wi-Fi password. |
| Model key | `PHONEPILOT_LLM_API_KEY` or `OPENAI_API_KEY` | Never written to a template. |
| Captions and media paths | Templates and SQLite run records | Local only. |
| Draft requests | Sent to the model endpoint you configure | Do not paste secrets into a prompt. |

The board serves plain HTTP on your LAN. It has no user accounts and no TLS.
Do not port forward it. If you do not trust the network, put the board on a
separate SSID or VLAN.

Run records include captions and response bodies. `~/.phonepilot/phonepilot.db`
is readable by your user account; treat it accordingly.

## Physical safety

- Long press the button for 1.2 seconds to clear the queue and send Escape.
- `POST /api/v1/panic` does the same from the CLI: `curl` it, or add a shell
  alias.
- The board has no persistent queue. Power cycling is a hard stop.

The button is a convenience, not a dead man's switch. The phone is still yours
to pick up and stop.

## If something goes wrong

1. Long press the board button, or call `/api/v1/panic`.
2. Run `phonepilot runs list` and `phonepilot runs show <id>` to see exactly
   which step ran and what it returned.
3. Delete the post from the platform if it should not be there.
4. Fix the template, or remove the schedule with
   `phonepilot schedule remove <name>`.

If you find a security problem in this project, open an issue with the details
and no secrets. There is no bounty and no formal disclosure process yet.
