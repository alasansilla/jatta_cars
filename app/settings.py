"""Editable site settings and page copy.

Everything the staff area can change is declared here as a schema. The schema
supplies the default value, the label and the widget type, so the admin forms
are generated rather than hand-written, and a setting that has never been edited
simply falls back to its default.

Stored values live in the `settings` table as strings; `coerce` turns them back
into the right Python type on the way out.
"""


class Field:
    def __init__(self, key, label, type="text", default="", help="", rows=4, choices=None):
        self.key = key
        self.label = label
        self.type = type  # text | textarea | number | boolean | lines | image | choice
        self.default = default
        self.help = help
        self.rows = rows
        self.choices = choices or []

    def coerce(self, raw):
        """Turn a stored string into the value templates and views expect."""
        if self.type == "number":
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return self.default
            return int(value) if value.is_integer() else value
        if self.type == "boolean":
            return str(raw).lower() in ("1", "true", "on", "yes")
        if self.type == "lines":
            return [line.strip() for line in str(raw).splitlines() if line.strip()]
        return raw

    def to_storage(self, raw):
        """Normalise a submitted form value before it is written to the database."""
        if self.type == "boolean":
            return "true" if raw else "false"
        if self.type == "lines":
            lines = [line.strip() for line in str(raw or "").splitlines() if line.strip()]
            return "\n".join(lines)
        return (raw or "").strip()


class Group:
    def __init__(self, key, label, description, fields):
        self.key = key
        self.label = label
        self.description = description
        self.fields = fields


ICON_CHOICES = [
    ("coin", "Coin / pricing"),
    ("shield", "Shield / cover"),
    ("road", "Road / distance"),
    ("clock", "Clock / hours"),
    ("check_circle", "Tick / included"),
    ("car", "Car"),
    ("pin", "Map pin"),
    ("building", "Building"),
]

# Text that still has to be replaced with real business detail is prefixed with
# this marker. The staff area lists every setting that still contains it, so a
# draft cannot quietly go live with invented facts in it.
PLACEHOLDER_MARKER = "[TBC]"

TBC = PLACEHOLDER_MARKER

SCHEMA = [
    Group(
        "business", "Business details",
        "Your name and contact details. These appear all over the site. "
        "GoGo Taxi connects customers with independent drivers, so there is no "
        "address or opening hours to state unless you really have them.",
        [
            Field("company_name", "Company name", default="GoGo Taxi"),
            Field("brand_name", "Logo name", default="GoGo Taxi",
                  help="The bold word in the logo, top left."),
            Field("brand_sub", "Logo tagline", default="Rides & car rentals in The Gambia"),
            Field("company_tagline", "Tagline",
                  default="Rides & car rentals in The Gambia"),
            Field("company_email", "Email address",
                  default=f"{TBC} add the email address enquiries should go to",
                  help="Shown on the site and used as the reply address customers write to."),
            Field("company_phone", "Phone number",
                  default=f"{TBC} add your phone number",
                  help="Include the country code, for example +220 …"),
            Field("company_address", "Address", type="textarea", rows=2, default="",
                  help="Only if customers can genuinely come to you. Drivers arrange "
                       "where to meet, so this is normally left empty and no address "
                       "is shown anywhere."),
            Field("currency", "Currency symbol", default="D",
                  help="Shown in front of every price. D is the usual short form for "
                       "the Gambian dalasi; change it if you quote in something else."),
            Field("locations", "Pick-up points", type="lines", rows=6,
                  default="Kololi\n"
                          "Somewhere else — agreed with your driver",
                  help="One per line. Customers choose from these when booking a car, and "
                       "settle the exact spot with the driver. Add the places drivers "
                       "regularly meet customers, such as the airport or a hotel."),
            Field("opening_hours", "Opening hours", default="",
                  help="The hours someone here answers enquiries, if you want to state "
                       "them. Left empty, no hours are shown. Drivers keep their own "
                       "hours and agree times with the customer."),
            Field("seo_description", "Search-engine description", type="textarea", rows=2,
                  default="Rides, airport transfers and car rental in The Gambia. Compare drivers "
                          "and cars by price, profile and reviews — nothing to pay on the website.",
                  help="The sentence search engines show under your site's name."),
        ],
    ),
    Group(
        "publishing", "Publishing",
        "Whether the public can see the site. Switch it off while you are still "
        "filling things in, or before it goes on a real domain.",
        [
            Field("site_live", "Show the site to the public", type="boolean",
                  default=True,
                  help="Switch this off and visitors get a short holding page with your "
                       "contact details, while you carry on seeing the real site. Worth "
                       "doing before the site goes on a public address with [TBC] "
                       "wording still in it."),
            Field("holding_heading", "Holding page — heading",
                  default="Our website is nearly ready"),
            Field("holding_image", "Holding page — image", type="image",
                  default="img/car-economy.svg",
                  help="The picture on the holding page."),
            Field("holding_body", "Holding page — text", type="textarea", rows=4,
                  default="We are putting the last details in place. Come back shortly to "
                          "find a driver for a ride, an airport transfer or a car to rent."),
        ],
    ),
    Group(
        "booking", "Booking rules",
        "The limits applied when someone requests a car, and what customers are told "
        "about paying and collecting. The driver sets the terms for their own car; "
        "this wording tells the customer what to settle with them.",
        [
            Field("promo_message", "Ribbon on the booking panel", type="text",
                  default="",
                  help="Shown above the home page booking panel. Leave it empty until "
                       "you have an offer you can actually honour."),
            Field("min_rental_days", "Minimum hire (days)", type="number", default=1),
            Field("max_rental_days", "Maximum hire (days)", type="number", default=90),
            Field("max_advance_days", "How far ahead bookings open (days)", type="number", default=365),
            Field("default_excess", "Insurance excess", type="number", default=0,
                  help="Set this once you know your insurance terms. Left at 0 it is "
                       "treated as not yet decided and is not quoted anywhere."),
            Field("fx_eur_rate", "Dalasi per €1", type="number", default=0,
                  help="Set this to show an approximate euro price beside each dalasi "
                       "price. 0 hides it. Update it when the rate moves — nothing "
                       "fetches a live rate."),
            Field("fx_gbp_rate", "Dalasi per £1", type="number", default=0,
                  help="The same for pounds. 0 hides it."),
            Field("fx_disclaimer", "Note under converted prices",
                  default="Approximate, for guidance only. You pay in dalasi.",
                  help="Shown wherever a converted price appears."),
            Field("deposit_policy", "Deposit and what it depends on", type="textarea", rows=4,
                  default="The deposit is set by the driver who owns the car, and the amount "
                          "for this car is shown on its page. It is refundable, and the "
                          "driver agrees with you when it is paid, what it covers and when "
                          "it comes back. Nothing is taken on this website, and the deposit "
                          "is never part of our commission.",
                  help="The amount is set per car by its driver; this is the wording "
                       "customers read beside it."),
            Field("insurance_note", "Insurance", type="textarea", rows=3,
                  default="Any cover on this car is the driver's own. Ask them what their "
                          "policy covers, what the excess is and what you would be liable "
                          "for, and get it confirmed before you drive away. GoGo Taxi does "
                          "not provide insurance, and a refundable deposit is not cover.",
                  help="Customers rely on this. Say what a customer should ask the driver "
                       "— do not describe cover that is not yours to promise."),
            Field("payment_note", "How customers pay", type="textarea", rows=3,
                  default="Nothing is charged on this website. You pay the driver directly, "
                          "and they confirm with you what they accept and when it is due.",
                  help="This site cannot take payments, and each driver is paid directly. "
                       "Say how payment actually happens."),
            Field("booking_collect_note", "What to bring at collection", type="textarea", rows=3,
                  default="Your driver confirms where to meet and what to bring. Expect to "
                          "need your driving licence, photo identification and the deposit "
                          "shown on the car's page — ask them whether they need anything "
                          "else before the day.",
                  help="Shown on a confirmed booking. Each driver sets their own "
                       "requirements, so this says what to settle with them."),
            Field("booking_change_note", "Changing or cancelling", type="textarea", rows=3,
                  default="Cancellation terms are set by the driver, so ask about them "
                          "before you book. If your plans change, tell your driver as early "
                          "as you can. If you cannot reach them, send us a message and we "
                          "will help.",
                  help="Shown at the bottom of a booking page."),
        ],
    ),
    Group(
        "home", "Home page quotes",
        "The home page itself is built from the Marketplace group. This is the "
        "strip of customer quotes below it, which stays hidden until you have "
        "real ones to show. Reviews left after a trip appear on each driver's "
        "own page and need nothing here.",
        [
            Field("review_1_quote", "Review 1 — quote", type="textarea", rows=3,
                  default="",
                  help="A real review, used with the customer's permission. Empty until then: nothing is shown."),
            Field("review_1_name", "Review 1 — name", default=""),
            Field("review_2_quote", "Review 2 — quote", type="textarea", rows=3,
                  default="",
                  help="A real review, used with the customer's permission. Empty until then: nothing is shown."),
            Field("review_2_name", "Review 2 — name", default=""),
            Field("review_3_quote", "Review 3 — quote", type="textarea", rows=3,
                  default="",
                  help="A real review, used with the customer's permission. Empty until then: nothing is shown."),
            Field("review_3_name", "Review 3 — name", default=""),
            Field("show_reviews", "Show the reviews section", type="boolean", default=True,
                  help="Turn this off until you have real reviews to show."),

        ],
    ),
    Group(
        "about", "About page",
        "The whole of the About page.",
        [
            Field("about_eyebrow", "Small label", default="About us"),
            Field("about_heading", "Page heading",
                  default="Drivers you choose, journeys you arrange with them"),
            Field("about_section1_heading", "First section heading", default="What GoGo Taxi is"),
            Field("about_section1_body", "First section text", type="textarea", rows=8,
                  default="GoGo Taxi is an online platform for The Gambia. It puts you in "
                          "touch with independent drivers who offer rides, airport transfers "
                          "and cars to rent. We do not own the cars and we do not drive "
                          "them.\n\n"
                          "You choose the driver yourself, by their price, their profile and "
                          "the reviews left by people they have already driven. A driver "
                          "appears here only after they have confirmed their phone number "
                          "and been approved by us, and any car they rent out is checked "
                          "before it is listed.",
                  help="Say what the platform does. Do not claim premises, staff or a "
                       "fleet of your own."),
            Field("about_section2_heading", "Second section heading",
                  default="Prices, deposits and terms"),
            Field("about_section2_body", "Second section text", type="textarea", rows=8,
                  default="Every price you see is set by the driver offering it, and it is "
                          "shown in full before you request anything. Nothing is charged on "
                          "this website: you settle up with the driver directly.\n\n"
                          "A car's deposit is shown on its page, and insurance, mileage, "
                          "extra drivers and cancellation terms are the driver's own. Agree "
                          "them with your driver before you pay anything or drive away. If "
                          "something goes wrong, contact your driver first; if you cannot "
                          "reach them, send us a message and we will help."),
            Field("about_included_heading", "Included box — heading",
                  default="What to check with the driver"),
            Field("about_included_items", "Included box — items", type="lines", rows=7,
                  default="The daily price, and what the total covers\n"
                          "The deposit, and when it comes back\n"
                          "What their insurance covers, and any excess\n"
                          "Any limit on the distance you may drive\n"
                          "Whether anyone else may drive the car\n"
                          "What to do if the car breaks down\n"
                          "Their terms if you have to cancel",
                  help="One per line. These are the questions a customer should put to "
                       "the driver before booking, not promises made on their behalf."),
            Field("about_requirements_heading", "Requirements box — heading",
                  default="What a driver will usually ask for"),
            Field("about_requirements_intro", "Requirements box — intro",
                  default="Each driver sets their own requirements. Ask yours about:"),
            Field("about_requirements_items", "Requirements box — items", type="lines", rows=6,
                  default="A driving licence they accept, and whether a visitor needs an "
                          "international permit\n"
                          "Any minimum age, and how long you must have held your licence\n"
                          "Photo identification, such as a passport\n"
                          "The deposit for the car, and how they want it paid"),
            Field("about_hours_heading", "Hours box — heading", default="Opening hours"),
            Field("about_hours_body", "Hours box — text", type="textarea", rows=3, default="",
                  help="Optional. Drivers agree times directly with the customer, so the "
                       "box stays hidden unless you put something here."),
            Field("about_cta_heading", "Closing heading", default="Have a look at the cars"),
            Field("about_cta_body", "Closing text", type="textarea", rows=2,
                  default="{cars} listed by drivers right now.",
                  help="{cars} is replaced with the number of cars currently listed, "
                       "as \"1 car\" or \"4 cars\"; {fleet_size} gives the number alone."),
        ],
    ),
    Group(
        "contact", "Contact page",
        "Headings and text on the Contact page.",
        [
            Field("contact_eyebrow", "Small label", default="Contact"),
            Field("contact_heading", "Page heading", default="Talk to a person"),
            Field("contact_intro", "Page intro", type="textarea", rows=2,
                  default="Questions about a booking, about becoming a driver, or anything "
                          "the site does not answer. Your driver is the quickest way to "
                          "settle anything about a journey they are taking you on."),
            Field("contact_form_heading", "Form heading", default="Send us a message"),
            Field("contact_success", "Message shown after sending", type="textarea", rows=2,
                  default="Thanks — we have your message and will come back to you."),
            Field("contact_reply_note", "Note under the form", type="textarea", rows=2,
                  default="Messages reach us here on the site rather than by email, so "
                          "please leave a phone number if it is urgent. To arrange a "
                          "journey, it is quicker to choose a driver and request it.",
                  help="Add how quickly you reply once you know what you can keep to."),
        ],
    ),
    Group(
        "vehicle", "Car rental pages",
        "The rental listing header, plus the standard text shown on every car's page. "
        "The cars belong to the drivers who list them.",
        [
            Field("fleet_page_eyebrow", "Fleet page — small label", default="Cars from independent drivers"),
            Field("fleet_page_heading", "Fleet page — heading", default="Cars available to rent"),
            Field("fleet_page_intro", "Fleet page — intro", type="textarea", rows=2,
                  default="Every car here is rented out by the driver who owns it. Set your "
                          "dates to see what is free and what the rental would come to."),
            Field("vehicle_included_heading", "Included list — heading",
                  default="Good to know"),
            Field("vehicle_included_items", "Included list — items", type="lines", rows=6,
                  default="The price and deposit are set by this car's driver\n"
                          "Nothing is charged here — you pay the driver directly\n"
                          "Ask what their insurance covers, and any excess\n"
                          "Ask about any limit on the distance you may drive\n"
                          "Ask whether anyone else may drive the car\n"
                          "Agree with them where to collect and return it",
                  help="Shown on every car page. Keep these to what is true of every "
                       "listing; anything particular to one car belongs in its own "
                       "description."),
            Field("vehicle_terms_note", "Deposit and licence note", type="textarea", rows=4,
                  default="The driver asks for a refundable deposit of {deposit} for this "
                          "car, and tells you when it is paid and what brings it back in "
                          "full. Confirm with them which licences they accept, any minimum "
                          "age and how long you must have been driving, before you take the "
                          "car.",
                  help="{deposit} is replaced with that car's deposit, as its driver set it."),
            Field("vehicle_booking_note", "Note under the booking button", type="textarea", rows=2,
                  default="Nothing to pay now — this is a request. The driver comes back to "
                          "you to confirm it."),
        ],
    ),
    Group(
        "marketplace", "Marketplace",
        "How the marketplace charges. Commission is taken on the fare of a "
        "completed booking only, and never on a refundable deposit.",
        [
            Field("market_footer_blurb", "Marketplace footer", default="Rides, airport transfers and car rentals in The Gambia."),
            Field("market_home_eyebrow", "Marketplace home eyebrow", default="Across The Gambia"),
            Field("market_home_heading", "Marketplace home heading", default="Your journey starts here."),
            Field("market_home_intro", "Marketplace home introduction", type="textarea", default="Find a ride, arrange an airport transfer or rent a car for your stay. Choose the journey that fits your plans."),
            Field("market_steps_heading", "Marketplace steps heading", default="From pickup to arrival"),
            Field("commission_rate", "Commission rate (%)", type="number", default=5,
                  help="Percentage of the fare on a completed ride or rental. "
                       "Refundable deposits are excluded. An individual driver can "
                       "be given their own rate, which overrides this one. Changing "
                       "this moves future bookings only — commission already earned "
                       "keeps the rate it was recorded at."),
            Field("operator_payout_note", "How drivers settle commission", type="textarea", rows=3,
                  default="Commission is charged on the fare of a completed booking, never "
                          "on a refundable deposit. Nothing is deducted automatically: we "
                          "agree with you directly how and when it is settled.",
                  help="Shown to drivers in their dashboard. Add the method and timing "
                       "once they are agreed."),
        ],
    ),
    Group(
        "rides", "Rides and transfers page",
        "The page where customers book a scheduled ride or an airport transfer.",
        [
            Field("rides_eyebrow", "Small label", default="Rides and transfers"),
            Field("rides_heading", "Page heading",
                  default="Scheduled rides and airport transfers"),
            Field("rides_intro", "Page intro", type="textarea", rows=3,
                  default="Book a driver ahead of time for a single journey. Each price is "
                          "set by the driver and shown in full before you request — there "
                          "is nothing to pay on this website."),
            Field("rides_empty", "Shown when no fares are published yet",
                  type="textarea", rows=3,
                  default="No driver has published a price for a journey yet. Once an "
                          "approved driver adds one it will appear here.",
                  help="Customers see this instead of an empty page. Do not replace it "
                       "with example prices."),
            Field("rides_request_note", "What happens after a request",
                  type="textarea", rows=3,
                  default="Your request goes to the driver, who confirms it with you "
                          "directly. Nothing is charged here, and you settle the fare with "
                          "them.",
                  help="Add how quickly a driver should reply once that is agreed with "
                       "them — do not promise a time on their behalf."),
            Field("transfers_heading", "Airport transfers heading",
                  default="Airport transfers"),
            Field("transfers_intro", "Airport transfers intro", type="textarea", rows=3,
                  default="Each transfer is run by the driver offering it, at the price "
                          "shown. Give them your flight number when they get in touch, and "
                          "ask where they will meet you and what they do if the flight is "
                          "delayed."),
        ],
    ),
    Group(
        "operators", "Become a driver page",
        "The page drivers read when they join with their phone number.",
        [
            Field("operators_eyebrow", "Small label", default="For drivers"),
            Field("operators_heading", "Page heading", default="Become a driver"),
            Field("operators_intro", "Page intro", type="textarea", rows=4,
                  default="Customers choose you for rides, airport transfers and car "
                          "rental. You set your own prices."),
            Field("operators_requirements", "What a driver needs", type="lines", rows=7,
                  default="",
                  help="One per line. These are your rules, so nothing is filled in for "
                       "you; the section stays hidden until you add some."),
            Field("operators_commission_note", "How commission is explained",
                  type="textarea", rows=3,
                  default="Joining is free. We take a commission on the price of each "
                          "completed trip or rental. Refundable deposits are never "
                          "included."),
            Field("operators_apply_note", "Note about approval",
                  type="textarea", rows=3,
                  default="Confirming your phone number does not approve you straight "
                          "away — we check every driver first."),
        ],
    ),
    Group(
        "footer", "Footer",
        "The bottom of every page.",
        [
            Field("footer_company_heading", "First column heading", default="Company"),
            Field("footer_locations_heading", "Second column heading", default="Pick-up points"),
            Field("footer_contact_heading", "Third column heading", default="Get in touch"),
        ],
    ),
]
# Flat lookup by key, built once at import.
FIELDS = {field.key: field for group in SCHEMA for field in group.fields}
GROUPS = {group.key: group for group in SCHEMA}

DEFAULTS = {key: field.coerce(field.default) for key, field in FIELDS.items()}


def current_settings():
    """Every setting, with stored values layered over the schema defaults.

    Cached on the request context so a page render hits the table once.
    """
    from flask import g, has_app_context

    from .models import Setting

    if not has_app_context():
        return dict(DEFAULTS)

    cached = getattr(g, "_site_settings", None)
    if cached is not None:
        return cached

    values = dict(DEFAULTS)
    try:
        for row in Setting.query.all():
            field = FIELDS.get(row.key)
            if field is not None:
                values[row.key] = field.coerce(row.value)
    except Exception:
        # The table does not exist yet (fresh clone, before seed.py). Defaults
        # are perfectly usable until then.
        pass

    g._site_settings = values
    return values


def _forget_cache():
    """Drop the per-request settings cache after a write.

    Without this a save is invisible to anything that reads settings again in
    the same request or app context — including the page rendered straight after
    a settings form is submitted.
    """
    from flask import g, has_app_context

    if has_app_context() and hasattr(g, "_site_settings"):
        del g._site_settings


def save_settings(submitted, group_key=None):
    """Write submitted values back.

    Two different callers, with genuinely different meanings:

    * A full form for one group (`group_key` given). An unticked checkbox is
      simply absent from the submission, so for that group's booleans absence
      means False.
    * A partial update — the inline editor sends only what changed. Here absence
      means "not mentioned", so anything missing must be left exactly as it is.
      Treating it as False would switch off every boolean on the site the moment
      someone edited a sentence.
    """
    from .models import Setting, db

    partial = group_key is None
    keys = FIELDS.keys() if partial else [f.key for f in GROUPS[group_key].fields]
    rows = {row.key: row for row in Setting.query.filter(Setting.key.in_(list(keys))).all()}

    for key in keys:
        field = FIELDS[key]

        if field.type == "boolean" and not partial:
            raw = key in submitted
        elif key not in submitted:
            continue
        elif field.type == "boolean":
            supplied = submitted.get(key)
            raw = (
                supplied
                if isinstance(supplied, bool)
                else str(supplied).strip().lower() in ("1", "true", "on", "yes")
            )
        else:
            raw = submitted.get(key)

        value = field.to_storage(raw)
        row = rows.get(key)
        if row is None:
            db.session.add(Setting(key=key, value=value))
        else:
            row.value = value

    db.session.commit()
    _forget_cache()


def reset_group(group_key):
    """Drop every stored value in a group so its defaults apply again."""
    from .models import Setting, db

    keys = [field.key for field in GROUPS[group_key].fields]
    Setting.query.filter(Setting.key.in_(keys)).delete(synchronize_session=False)
    db.session.commit()
    _forget_cache()


def fill_tokens(text, settings, **extra):
    """Replace the {placeholders} allowed in editable copy.

    Unknown braces are left alone rather than raising, so a typo in the admin
    form shows up as literal text instead of a 500.
    """
    if not text:
        return text

    tokens = {
        "phone": settings.get("company_phone", ""),
        "email": settings.get("company_email", ""),
        "company": settings.get("company_name", ""),
        "address": settings.get("company_address", ""),
        "hours": settings.get("opening_hours", ""),
        "excess": (
            f"{settings.get('currency', '')}{settings.get('default_excess')}"
            if settings.get("default_excess")
            else f"{PLACEHOLDER_MARKER} excess not set"
        ),
    }
    tokens.update(extra)

    for name, value in tokens.items():
        text = text.replace("{" + name + "}", str(value))
    return text


def outstanding_items(settings=None):
    """Every setting still carrying the placeholder marker.

    Drives the setup checklist in the staff area, so it is obvious what has to be
    filled in before the site is shown to customers.
    """
    values = settings if settings is not None else current_settings()

    items = []
    for group in SCHEMA:
        for field in group.fields:
            value = values.get(field.key)
            text = "\n".join(value) if isinstance(value, list) else str(value or "")
            if PLACEHOLDER_MARKER in text:
                items.append({
                    "group": group,
                    "field": field,
                    "lines": [
                        line for line in text.splitlines()
                        if PLACEHOLDER_MARKER in line
                    ] or [text],
                })
    return items
