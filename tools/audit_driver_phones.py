"""Report phone numbers that could make driver sign-in ambiguous. Changes nothing.

Before phone sign-in, a driver account could carry a contact number typed in by
the applicant or by staff. Nobody proved those numbers. This report finds:

* the same number (after normalising spacing, prefixes and the September 2026
  Gambian 7-to-9-digit change) written on more than one account;
* a contact number on one account that is another account's verified sign-in
  number;
* contact numbers that are not valid mobile numbers at all.

Numbers are printed masked (+220 ••• ••34) so the report can be shared.

It never merges, moves or deletes anything. Owners are sorted out by people:

1. Decide with the drivers concerned which account the number belongs to.
2. The real owner signs in (email and password, if the account has them) and
   adds the number under "My phone number", which texts them a code. Only
   possession of the phone puts a number on an account.
3. For every other account, staff use "Remove contact number" on the Drivers
   page. That only removes; it never attaches a number anywhere.
4. A driver who lost their SIM: staff "Remove sign-in number"; the driver then
   verifies their new number after signing in, or joins again.

    python tools/audit_driver_phones.py
Exit status 1 when anything needs a decision, 0 when the data is clean.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.models import Operator  # noqa: E402
from app.phone import masked, try_normalise  # noqa: E402


def audit():
    accounts = Operator.query.order_by(Operator.id).all()
    verified = {account.phone_e164: account for account in accounts if account.phone_e164}

    by_number = defaultdict(list)
    invalid = []
    for account in accounts:
        if not account.phone:
            continue
        number = try_normalise(account.phone)
        if number is None:
            invalid.append(account)
        else:
            by_number[number].append(account)

    shared = {number: owners for number, owners in by_number.items() if len(owners) > 1}
    clashes = [(number, owner, verified[number]) for number, owners in by_number.items()
               for owner in owners
               if number in verified and verified[number].id != owner.id]
    # A verified number stored in an older spelling than the canonical one.
    stale = [account for account in verified.values()
             if try_normalise(account.phone_e164) not in (None, account.phone_e164)]
    return accounts, verified, shared, clashes, invalid, stale


def describe(account):
    return f"#{account.id} {account.display_name} ({account.status})"


def main():
    app = create_app()
    with app.app_context():
        accounts, verified, shared, clashes, invalid, stale = audit()
        print(f"Driver accounts: {len(accounts)}")
        print(f"With a verified sign-in number: {len(verified)}")
        print(f"With an unverified contact number: {sum(1 for a in accounts if a.phone)}")

        if shared:
            print("\nThe same contact number is written on more than one account:")
            for number, owners in shared.items():
                print(f"  {masked(number)}: " + ", ".join(describe(o) for o in owners))
        if clashes:
            print("\nA contact number that is another account's verified sign-in number:")
            for number, owner, holder in clashes:
                print(f"  {masked(number)}: written on {describe(owner)}, "
                      f"verified by {describe(holder)}")
        if invalid:
            print("\nContact numbers that are not valid mobile numbers:")
            for account in invalid:
                print(f"  {describe(account)}")
        if stale:
            print("\nVerified numbers stored in an old format (ask the driver to verify again):")
            for account in stale:
                print(f"  {describe(account)}: {masked(account.phone_e164)}")

        needs_decision = bool(shared or clashes or invalid or stale)
        if needs_decision:
            print("\nNothing was changed. See the steps at the top of this file.")
        else:
            print("\nNo conflicts found.")
        return 1 if needs_decision else 0


if __name__ == "__main__":
    sys.exit(main())
