"""Store verified Gambian sign-in numbers in their 9-digit form.

On 4 September 2026 The Gambia moved Africell, Comium and QCell numbers from 7
to 9 digits (prefixes 87, 86, 83). The old form stops working on 1 December
2026. `app.phone.normalise` now returns the 9-digit form, so a number verified
before that change would no longer match its own account at sign-in.

Each verified number is rewritten to what `normalise` returns today, one row at a
time. If the new form already belongs to a different account, neither row is
touched and nothing is merged: `tools/audit_driver_phones.py` lists it for staff
to settle with the drivers. Unverified contact numbers are left exactly as typed.
"""
from sqlalchemy import text

VERSION = "0009"
NAME = "verified Gambian numbers in 9-digit form"


def upgrade(connection, metadata):
    from app.phone import try_normalise

    rows = connection.execute(text(
        "SELECT id, phone_e164 FROM operators WHERE phone_e164 IS NOT NULL")).fetchall()
    held = {phone: operator_id for operator_id, phone in rows}
    for operator_id, phone in rows:
        canonical = try_normalise(phone)
        if canonical is None or canonical == phone:
            continue
        if held.get(canonical, operator_id) != operator_id:
            continue  # another account already has it; leave both for staff
        connection.execute(text("UPDATE operators SET phone_e164 = :new WHERE id = :id"),
                           {"new": canonical, "id": operator_id})
        held.pop(phone, None)
        held[canonical] = operator_id
