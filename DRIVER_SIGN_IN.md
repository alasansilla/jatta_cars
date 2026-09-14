# Driver phone sign-in

Drivers join and sign in with their phone number. There is no email or password
in this flow.

```
Become a driver / Driver sign in
  → type your phone number (+220 is filled in, and can be changed)
  → type the 6-digit code we text you
  → first time only: type your name
  → your dashboard (approved) or your application status (not approved yet)
```

`/driver/join` and `/driver/sign-in` are the two public entry points. They run
the same steps; only the wording differs. A number that already belongs to an
account signs into that account. A number with no account gets a new one.

## What is enforced

| Rule | Where |
| --- | --- |
| Typing a number proves nothing: no account and no sign-in until the texted code is typed back | `app/driver_auth.py` |
| One verified number opens exactly one account: `operators.phone_e164` has a UNIQUE index; a race is settled by the database and the loser signs into the winner's account | model, migration 0008, `phone_auth.find_or_create_driver` |
| Spacing, `+220`, `00220`, `220…`, a habitual leading `0` all normalise to one E.164 value | `app/phone.py` (libphonenumber) |
| Codes: 6 random digits, stored only as a keyed hash, 10-minute expiry, single use, 5 guesses, bound to the browser that asked, a new code closes the old one | `app/phone_auth.py` |
| Abuse limits: 60 s resend cooldown, 5 texts per number per hour and 10 per day, 10 per IP per hour, 300 site-wide per day, 30 wrong codes per IP per hour locks that IP | `app/phone_auth.py`, `config.py` |
| No phone numbers or codes in logs; counters store keyed hashes of numbers and IPs | `app/phone_auth.py`, `app/sms.py` |
| Sessions: cleared and given a new CSRF token at sign-in, HttpOnly, SameSite=Lax, Secure in production, 14 days | `app/operator.py`, `config.py` |
| Verifying a phone never approves anyone. New accounts are `pending` and see only their application status | `app/operator.py` `login_required` |
| Adding or changing a sign-in number needs a sign-in from the last 15 minutes **and** the code texted to the new number; no code is texted to a number another account already uses. There is no other way to put a number on an account | `app/driver_auth.py`, `phone_auth.attach_phone` |
| Staff can only **remove** a number (lost SIM, wrong owner). Staff cannot attach one | `admin.operator_phone_remove` |
| Production fails closed: no SMS backend means phone sign-in says it is unavailable; `JATTA_SMS_BACKEND=fake` in production stops the app from starting | `app/sms.py`, `config.check_production_config` |

## Existing accounts and duplicate numbers

- Accounts that were given an email and password keep working at
  `/operator/login` ("Sign in with email" on the sign-in page). Admin access is
  unchanged.
- Migration 0008 copies **nothing** into the verified number. Old contact
  numbers were typed in, never proved, so they are not a way into any account.
- If someone verifies a number that is written on an older account as its
  contact number, registration stops. It explains how to sign in to that
  account and add the number, or to contact staff. No second account is made
  and nothing is merged.
- `python tools/audit_driver_phones.py` lists clashes: the same number on
  several accounts, a contact number that is another account's verified number,
  invalid numbers, and verified numbers in an old format. Numbers are masked. It
  changes nothing; the recovery steps are at the top of that file.

## Running it locally

```bash
python tools/upgrade_local.py          # backs up instance/jatta.db, then migrates
JATTA_SMS_BACKEND=fake python run.py   # local test transport
```

With `fake`, no text leaves the computer. Codes go to
`instance/dev_sms_outbox.jsonl` (git-ignored, owner-only permissions). Signed-in
staff can also read them under **Admin → Test text messages**, a page that exists
only while the fake transport is on. Without `JATTA_SMS_BACKEND`, the pages say
phone sign-in is not available.

Tests use an in-memory database and the in-process fake outbox. No test sends a
real text.

## The Gambian numbering change (September 2026)

Twilio's Gambia guidelines and Vonage's Gambia article (updated 4 Sep 2026) both
report a change. On 4 September 2026 Africell, Comium and QCell numbers went
from 7 to 9 digits by adding a prefix: 87, 86 and 83. Gamcel numbers did not
change. Both formats reach phones until 30 November 2026; from 1 December 2026
only the 9-digit form works (Vonage).

So `+220 770 1234` and `+220 87 770 1234` are the same phone. The normaliser
turns an old 7-digit Africell, Comium or QCell mobile number into its 9-digit
form. It does this only when libphonenumber's carrier data puts both forms on
the same network. Both spellings therefore land on one account, and codes go
to the number that keeps working after 1 December. Migration 0009 rewrites
verified numbers already stored in the old form. If the new form already
belongs to another account it leaves both alone and never merges them; the
audit tool lists that case.

## Switching on real text messages — not done

Nothing has been bought, no provider account exists, and no real text has been
sent. Phone sign-in is not live anywhere until the owner does the following.

### What was checked (public documentation, 14 Sep 2026)

| Provider | Gambia documented? | Sender notes | Source |
| --- | --- | --- | --- |
| Twilio Programmable Messaging | Yes, country guideline page | Alphanumeric sender, dynamic, no pre-registration listed; Comium rejects international numeric senders; paid account needed for alphanumeric; indicative USD 0.2606/SMS | twilio.com/en-us/guidelines/gm/sms, twilio.com/en-us/sms/pricing/gm |
| Twilio Verify | Indirectly (covers the alphanumeric-sender country list, which includes Gambia) | Gambia not named on the Verify page | twilio.com/docs/verify/verify-countries-and-regions-deliverability |
| Vonage SMS API | Yes, country article | Alphanumeric or numeric; generic senders (INFO, SMS) banned; indicative USD 0.246/SMS | api.support.vonage.com, article 8642866586012 |
| Vonage Verify | Gambia is on the **restricted** list, blocked by default | — | api.support.vonage.com, article 360018406532 |
| AWS End User Messaging SMS | Yes, country table | Sender IDs yes, no registration listed; sandbox until a support case; indicative USD 0.235/SMS | docs.aws.amazon.com/sms-voice/latest/userguide/phone-numbers-sms-by-country.html |
| Infobip | Yes | Says sender **registration required** (about 15–20+ days, business licence) | infobip.com/docs/essentials/getting-started/sms-coverage-and-connectivity |
| Bird | Yes | Alphanumeric, instant, no registration; indicative USD 0.18 | bird.com/products/sms/destinations/gambia |
| Plivo | Yes | Sales-provisioned with a monthly minimum commitment (≥ USD 1,000) | plivo.com/sms/coverage/gm |
| Africa's Talking | Priced for Gamcell, Comium, QCell; **Africell not listed** (the largest network); their help centre omits Gambia | — | archived africastalking.com/pricing (Jun 2026) |
| Gambian carriers directly | No self-service API published (Africell "SMS Bulk" is a sales contact) | — | africell.gm/business/sms-bulk |

Providers disagree on sender registration, and none publishes delivery results
per Gambian carrier. **Delivery is unverified until the owner sends real test
texts to Africell, QCell, Gamcel and Comium SIMs.**

### Supabase phone auth

Supabase Auth does not send texts itself. It needs an outside provider (Twilio,
Twilio Verify, Vonage; its MessageBird option only works with old accounts, and
Textlocal has shut down) or a "Send SMS" hook. It would also bring its own
sessions and user table alongside this app's. It removes neither the provider
account nor the delivery testing, so the app keeps its own codes and sessions
and talks to a provider directly.

### Activation checklist (owner)

1. Choose a provider. The adapter in `app/sms.py` is written for Twilio
   Programmable Messaging, whose Gambia page is the most complete. It is
   unit-tested against a mocked API only.
2. Open and pay for the account (purchase decision for the owner). Enable
   Gambia in Messaging → Geo permissions. Enable alphanumeric sender IDs.
   Create a Messaging Service with an alphanumeric sender such as the brand name.
3. In the host's secret settings (never in the repository), set
   `JATTA_SMS_BACKEND=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
   `TWILIO_MESSAGING_SERVICE_SID` (or `TWILIO_FROM`), and
   `JATTA_TRUSTED_PROXIES=1` on Render. `JATTA_SMS_WEBOTP_DOMAIN` is optional.
4. Apply migrations 0005–0009 to Supabase, after a backup. The remote database
   was last seen at 0004.
5. Send test codes to one SIM on each network (Africell, QCell, Gamcel, Comium),
   in both the old and new number formats before 30 November. Confirm arrival
   and the sender name.
6. Only then tell drivers to use phone sign-in.
