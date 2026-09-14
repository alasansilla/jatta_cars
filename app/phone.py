"""Phone numbers: one canonical form, so one number is one account.

People type the same number in many ways — "770 1234", "+220 770-1234",
"00220 7701234", "2207701234", and since September 2026 "87 770 1234". Every
one of those must end up as the same E.164 string (+220877701234), because that
string is what the unique index on
operators.phone_e164 compares. If two spellings survived as different strings,
the database would happily give the same phone two accounts.

Parsing is done by `phonenumbers`, a port of Google's libphonenumber, which
knows each country's number lengths and prefixes. Hand-rolled rules would get
some country wrong.

Only numbers that can receive a text are accepted: a fixed landline cannot get
the sign-in code, so saying so up front is kinder than sending nothing.
"""
import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType
from phonenumbers import carrier as _carrier

DEFAULT_COUNTRY_CODE = "+220"  # The Gambia

# On 4 September 2026 The Gambia moved Africell, Comium and QCell numbers from 7
# to 9 digits by putting 87, 86 or 83 in front; Gamcel numbers did not change.
# Both forms reach the phone until 30 November 2026, and only the 9-digit form
# after that (sources: Twilio's Gambia SMS guidelines; Vonage's Gambia SMS
# article, updated 4 Sep 2026). So "+220 770 1234" and "+220 87 770 1234" are
# one phone, and must be one account.
GAMBIA_NEW_PREFIX = {"Africell": "87", "Comium": "86", "QCell": "83"}

_TEXTABLE = {
    PhoneNumberType.MOBILE,
    PhoneNumberType.FIXED_LINE_OR_MOBILE,
}


class InvalidPhone(ValueError):
    """The input is not a mobile number we could text. The message is for people."""


def _country_digits(country_code):
    digits = "".join(ch for ch in str(country_code or "") if ch.isdigit())
    if not digits or len(digits) > 3:
        raise InvalidPhone("Check the country code. For The Gambia it is +220.")
    return int(digits)


def normalise(country_code, number):
    """Return the E.164 form of a textable number, or raise InvalidPhone.

    `number` may itself start with + or 00 and a country code, in which case
    that wins over `country_code` — someone pasting "+44 7911 123456" meant
    the UK even if the box still says +220.
    """
    raw = str(number or "").strip()
    if not any(ch.isdigit() for ch in raw):
        raise InvalidPhone("Enter your phone number.")
    if len(raw) > 32:
        raise InvalidPhone("That number is too long. Check it and try again.")

    calling_code = _country_digits(country_code)
    region = phonenumbers.region_code_for_country_code(calling_code)
    if region in (None, "ZZ"):
        raise InvalidPhone("Check the country code. For The Gambia it is +220.")

    compact = raw.replace(" ", "")
    if compact.startswith("00"):
        raw = "+" + compact[2:]

    candidates = [raw]
    # Some people add a leading 0 out of habit even where the country does not
    # use one (The Gambia does not). Try without it, but only accept the result
    # if it is then a valid number.
    digits_only = "".join(ch for ch in raw if ch.isdigit())
    if not raw.lstrip().startswith("+") and digits_only.startswith("0"):
        candidates.append(digits_only[1:])

    for candidate in candidates:
        try:
            parsed = phonenumbers.parse(candidate, region)
        except NumberParseException:
            continue
        if parsed.extension:
            raise InvalidPhone("Enter the phone number only, without an extension.")
        if not phonenumbers.is_valid_number(parsed):
            continue
        if phonenumbers.number_type(parsed) not in _TEXTABLE:
            raise InvalidPhone("That looks like a landline. Use a mobile number that "
                               "can receive text messages.")
        return phonenumbers.format_number(_gambian_nine_digit(parsed), PhoneNumberFormat.E164)

    raise InvalidPhone("That doesn't look like a mobile number. Check it and try again.")


def _gambian_nine_digit(parsed):
    """The 9-digit form of an old 7-digit Africell, Comium or QCell mobile number.

    Converted only when the library's network data puts the old number on one of
    those networks *and* the 9-digit result is a valid mobile number on the same
    network. Anything else — Gamcel, an unknown prefix — is left as it is.
    """
    if parsed.country_code != 220:
        return parsed
    national = str(parsed.national_number)
    if len(national) != 7:
        return parsed
    network = _carrier.name_for_number(parsed, "en")
    prefix = GAMBIA_NEW_PREFIX.get(network)
    if not prefix:
        return parsed
    try:
        longer = phonenumbers.parse(f"+220{prefix}{national}", None)
    except NumberParseException:
        return parsed
    if (phonenumbers.is_valid_number(longer)
            and phonenumbers.number_type(longer) in _TEXTABLE
            and _carrier.name_for_number(longer, "en") == network):
        return longer
    return parsed


def try_normalise(value, country_code=DEFAULT_COUNTRY_CODE):
    """E.164 or None. For tidying data that may be rubbish, never for sign-in."""
    try:
        return normalise(country_code, value)
    except InvalidPhone:
        return None


def pretty(e164):
    """+220 87 770 1234 — how a person expects to read their own number."""
    if not e164:
        return ""
    try:
        return phonenumbers.format_number(phonenumbers.parse(e164, None),
                                          PhoneNumberFormat.INTERNATIONAL)
    except NumberParseException:
        return e164


def masked(e164):
    """+220 ••• ••34 — enough for staff to tell numbers apart in a report."""
    text = pretty(e164)
    digits_seen = sum(ch.isdigit() for ch in text)
    keep_from = digits_seen - 2
    out, index = [], 0
    code_digits = len(text.split(" ")[0]) - 1 if text.startswith("+") else 0
    for ch in text:
        if ch.isdigit():
            index += 1
            if code_digits < index <= keep_from:
                out.append("•")
                continue
        out.append(ch)
    return "".join(out)
