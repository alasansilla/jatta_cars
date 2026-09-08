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
        "Your name, contact details and where customers collect a car. "
        "These appear all over the site.",
        [
            Field("company_name", "Company name", default="Jatta Cars"),
            Field("brand_name", "Logo — first word", default="Jatta",
                  help="The bold word in the logo, top left."),
            Field("brand_sub", "Logo — second word", default="Cars"),
            Field("company_tagline", "Tagline",
                  default="Self-drive car hire in The Gambia."),
            Field("company_email", "Email address",
                  default=f"{TBC} add the email address enquiries should go to",
                  help="Shown on the site and used as the reply address customers write to."),
            Field("company_phone", "Phone number",
                  default=f"{TBC} add your phone number",
                  help="Include the country code, for example +220 …"),
            Field("company_address", "Address", type="textarea", rows=2,
                  default=f"{TBC} add the address customers should come to"),
            Field("currency", "Currency symbol", default="D",
                  help="Shown in front of every price. D is the usual short form for "
                       "the Gambian dalasi; change it if you quote in something else."),
            Field("locations", "Pick-up points", type="lines", rows=6,
                  default="Kololi\n"
                          "Somewhere else — we agree it when we confirm",
                  help="One per line. Customers choose from these when booking. Add the "
                       "other places you regularly hand cars over, such as the airport or "
                       "a hotel, so people can pick one rather than explaining it."),
            Field("opening_hours", "Opening hours",
                  default=f"{TBC} add the days and hours you are open"),
            Field("seo_description", "Search-engine description", type="textarea", rows=2,
                  default="Self-drive car hire in The Gambia. Book online in a couple of "
                          "minutes — nothing to pay on the website.",
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
            Field("holding_body", "Holding page — text", type="textarea", rows=4,
                  default="We are putting the last details in place. In the meantime, "
                          "get in touch and we will sort out a car for you."),
        ],
    ),
    Group(
        "booking", "Booking rules",
        "The limits applied when someone requests a car, and what they are told about "
        "paying and collecting.",
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
                  default=f"A refundable deposit is held on each hire. It comes back to you "
                          f"when the car is returned undamaged and with at least half a tank "
                          f"of fuel.\n\n{TBC} confirm the amount, whether it is per hire or "
                          f"per day, and what is deducted if the car comes back below half a "
                          f"tank.",
                  help="Set the amount per car under Fleet. This describes the terms."),
            Field("insurance_note", "Insurance", type="textarea", rows=3,
                  default=f"{TBC} say whether the car carries an insurance policy, what it "
                          f"covers and what the driver is liable for. A refundable deposit "
                          f"is not insurance, so do not describe it as cover.",
                  help="Customers rely on this. Leave it as a placeholder until you can "
                       "state the real position."),
            Field("payment_note", "How customers pay", type="textarea", rows=3,
                  default=f"Nothing is charged on this website. You settle up with us "
                          f"directly when you collect the car. {TBC} confirm which payment "
                          f"methods you accept — cash, mobile money or card.",
                  help="This site cannot take payments. Say how payment actually happens."),
            Field("booking_collect_note", "What to bring at collection", type="textarea", rows=3,
                  default=f"{TBC} list what a driver must bring — driving licence, ID or "
                          f"passport, and the deposit.",
                  help="Shown on a confirmed booking."),
            Field("booking_change_note", "Changing or cancelling", type="textarea", rows=3,
                  default=f"{TBC} set out your cancellation terms — how much notice you "
                          f"need and whether anything is charged.",
                  help="Shown at the bottom of a booking page."),
        ],
    ),
    Group(
        "home", "Home page",
        "Every piece of text and the main image on the home page.",
        [
            Field("home_eyebrow", "Small label above the headline", default="Plan your trip"),
            Field("home_heading", "Headline", default="Explore The Gambia at your own pace"),
            Field("home_heading_highlight", "Word(s) to colour in the headline",
                  default="your own pace",
                  help="The first match inside the headline is shown in the accent colour. "
                       "Leave empty for a plain headline."),
            Field("home_intro", "Introduction", type="textarea", rows=4,
                  default=f"A small, locally run fleet of self-drive cars. Choose your dates, "
                          f"send a request in about two minutes, and we come back to you to "
                          f"confirm. There is nothing to pay on this website.\n\n"
                          f"{TBC} add a sentence about what makes hiring from you different."),
            Field("home_hero_image", "Main image", type="image", default="img/car-economy.svg",
                  help="The large picture beside the headline. A photo of one of your own "
                       "cars works far better than the drawing that ships with the site."),
            Field("home_cta_primary", "Primary button", default="Book a car"),
            Field("home_cta_secondary", "Secondary button", default="Learn more"),

            Field("home_search_heading", "Booking panel heading", default="Book a car"),

            Field("home_fleet_eyebrow", "Fleet section label", default="Our cars"),
            Field("home_fleet_heading", "Fleet section heading",
                  default="Choose the car that suits the trip"),
            Field("home_fleet_intro", "Fleet section intro", type="textarea", rows=2,
                  default=f"{TBC} add a line about how your cars are looked after and "
                          f"prepared between hires."),

            Field("home_why_eyebrow", "Why-choose-us label", default="Why book with us"),
            Field("home_why_heading", "Why-choose-us heading",
                  default="Booked direct, straight with you"),
            Field("home_why_body", "Why-choose-us text", type="textarea", rows=6,
                  default=f"You are dealing with us, not an agency. Every request comes "
                          f"straight to our own team, and a person checks the car and comes "
                          f"back to you.\n\n"
                          f"{TBC} replace this with what you want customers to know about "
                          f"your service."),
            Field("home_why_cta", "Why-choose-us button", default="See the cars"),

            Field("reason_1_icon", "Reason 1 — icon", type="choice", default="coin", choices=ICON_CHOICES),
            Field("reason_1_title", "Reason 1 — heading", default="Pay when you collect"),
            Field("reason_1_body", "Reason 1 — text", type="textarea", rows=3,
                  default=f"Nothing is charged on this website. You settle up with us when "
                          f"you pick the car up. {TBC} confirm which payment methods you take."),
            Field("reason_2_icon", "Reason 2 — icon", type="choice", default="shield", choices=ICON_CHOICES),
            Field("reason_2_title", "Reason 2 — heading", default="A deposit you get back"),
            Field("reason_2_body", "Reason 2 — text", type="textarea", rows=3,
                  default=f"We hold a refundable deposit for the hire and return it when the "
                          f"car comes back undamaged with at least half a tank. {TBC} confirm "
                          f"the amount and what else the daily rate covers."),
            Field("reason_3_icon", "Reason 3 — icon", type="choice", default="pin", choices=ICON_CHOICES),
            Field("reason_3_title", "Reason 3 — heading", default=f"{TBC} where we can meet you"),
            Field("reason_3_body", "Reason 3 — text", type="textarea", rows=3,
                  default=f"{TBC} describe your pick-up points, whether you deliver to hotels "
                          f"or the airport, and what happens outside opening hours."),

            Field("home_steps_eyebrow", "Steps section label", default="How it works"),
            Field("home_steps_heading", "Steps section heading",
                  default="Three steps and you are driving"),
            Field("step_1_title", "Step 1 — heading", default="Choose your dates"),
            Field("step_1_body", "Step 1 — text", type="textarea", rows=3,
                  default="Tell us when and where. Only cars that are genuinely free for "
                          "those days are shown."),
            Field("step_2_title", "Step 2 — heading", default="Send your request"),
            Field("step_2_body", "Step 2 — text", type="textarea", rows=3,
                  default="Your name, phone and email. Nothing is charged online — this is a "
                          "request, and we come back to you to confirm it."),
            Field("step_3_title", "Step 3 — heading", default="Collect and drive"),
            Field("step_3_body", "Step 3 — text", type="textarea", rows=3,
                  default=f"{TBC} say what happens on the day — where to come, what to bring "
                          f"and how long the handover takes."),

            Field("home_about_eyebrow", "About section label", default="About us"),
            Field("home_about_heading", "About section heading",
                  default="A local fleet, run by people you can reach"),
            Field("home_about_body", "About section text", type="textarea", rows=7,
                  default=f"{TBC} introduce your business — who runs it, how long you have "
                          f"been hiring cars, and where you are based.\n\n"
                          f"{TBC} add a second paragraph about how you look after the cars."),
            Field("home_about_cta", "About section button", default="More about us"),
            Field("home_stat3_value", "Third statistic — number", default=f"{TBC}"),
            Field("home_stat3_label", "Third statistic — label",
                  default=f"{TBC} e.g. years hiring cars"),

            Field("home_included_eyebrow", "Included section label", default="Good to know"),
            Field("home_included_heading", "Included section heading",
                  default="What to check before you book"),
            Field("home_included_items", "Included items", type="lines", rows=9,
                  default=f"{TBC} what the insurance covers, and the excess\n"
                          f"{TBC} mileage — a daily limit, or unlimited\n"
                          f"{TBC} the fuel policy\n"
                          f"{TBC} whether extra drivers are allowed, and any charge\n"
                          f"{TBC} the deposit, and when it comes back\n"
                          f"{TBC} minimum age and how long a licence must be held\n"
                          f"{TBC} what happens if the car breaks down\n"
                          f"{TBC} your cancellation terms",
                  help="One per line. Replace each with the real answer — these are the "
                       "questions customers ask before booking."),

            Field("home_reviews_eyebrow", "Reviews section label", default="What customers say"),
            Field("home_reviews_heading", "Reviews section heading", default="In their words"),
            Field("home_reviews_intro", "Reviews section intro", type="textarea", rows=2,
                  default=f"{TBC} replace the three quotes below with real reviews, and only "
                          f"use them with the customer's permission."),
            Field("review_1_quote", "Review 1 — quote", type="textarea", rows=3,
                  default=f"{TBC} paste a real customer review here."),
            Field("review_1_name", "Review 1 — name", default=f"{TBC} customer name"),
            Field("review_1_place", "Review 1 — where from", default=f"{TBC} where they came from"),
            Field("review_2_quote", "Review 2 — quote", type="textarea", rows=3,
                  default=f"{TBC} paste a real customer review here."),
            Field("review_2_name", "Review 2 — name", default=f"{TBC} customer name"),
            Field("review_2_place", "Review 2 — where from", default=f"{TBC} where they came from"),
            Field("review_3_quote", "Review 3 — quote", type="textarea", rows=3,
                  default=f"{TBC} paste a real customer review here."),
            Field("review_3_name", "Review 3 — name", default=f"{TBC} customer name"),
            Field("review_3_place", "Review 3 — where from", default=f"{TBC} where they came from"),
            Field("show_reviews", "Show the reviews section", type="boolean", default=True,
                  help="Turn this off until you have real reviews to show."),

            Field("home_final_heading", "Closing heading", default="Ready when you are"),
            Field("home_final_body", "Closing text", type="textarea", rows=2,
                  default="Have a look at the cars, or call us on {phone} and we will sort "
                          "it out over the phone.",
                  help="{phone} is replaced with your phone number."),
        ],
    ),
    Group(
        "about", "About page",
        "The whole of the About page.",
        [
            Field("about_eyebrow", "Small label", default="About us"),
            Field("about_heading", "Page heading",
                  default="Self-drive hire, arranged with a person"),
            Field("about_section1_heading", "First section heading", default="Who we are"),
            Field("about_section1_body", "First section text", type="textarea", rows=8,
                  default=f"{TBC} write a short introduction — who runs Jatta Cars, where you "
                          f"are based, and how long you have been hiring cars.\n\n"
                          f"{TBC} add a second paragraph about the kind of trips your "
                          f"customers take and how you help them plan."),
            Field("about_section2_heading", "Second section heading",
                  default="How we look after the cars"),
            Field("about_section2_body", "Second section text", type="textarea", rows=8,
                  default=f"{TBC} describe how the cars are serviced and checked, who does "
                          f"the work, and what happens between one hire and the next.\n\n"
                          f"{TBC} say what a customer should do if something goes wrong while "
                          f"they have the car."),
            Field("about_included_heading", "Included box — heading",
                  default="What a day's hire includes"),
            Field("about_included_items", "Included box — items", type="lines", rows=7,
                  default=f"{TBC} insurance — what type, and the excess\n"
                          f"{TBC} mileage limit, or unlimited\n"
                          f"{TBC} fuel policy\n"
                          f"{TBC} extra drivers\n"
                          f"{TBC} breakdown help\n"
                          f"{TBC} cancellation terms"),
            Field("about_requirements_heading", "Requirements box — heading",
                  default="What we need from you"),
            Field("about_requirements_intro", "Requirements box — intro",
                  default="Before you can drive away:"),
            Field("about_requirements_items", "Requirements box — items", type="lines", rows=6,
                  default=f"{TBC} minimum age, and how long the licence must be held\n"
                          f"{TBC} which licences you accept, and whether visitors need an "
                          f"international permit\n"
                          f"{TBC} the deposit, and how it is paid\n"
                          f"{TBC} ID or passport"),
            Field("about_hours_heading", "Hours box — heading", default="Opening hours"),
            Field("about_hours_body", "Hours box — text", type="textarea", rows=3,
                  default=f"{TBC} add your days and hours, and say whether collection can be "
                          f"arranged outside them."),
            Field("about_cta_heading", "Closing heading", default="Have a look at the cars"),
            Field("about_cta_body", "Closing text", type="textarea", rows=2,
                  default="{fleet_size} cars available to hire right now.",
                  help="{fleet_size} is replaced with the number of cars currently listed."),
        ],
    ),
    Group(
        "contact", "Contact page",
        "Headings and text on the Contact page.",
        [
            Field("contact_eyebrow", "Small label", default="Contact"),
            Field("contact_heading", "Page heading", default="Talk to a person"),
            Field("contact_intro", "Page intro", type="textarea", rows=2,
                  default="Questions about a booking, a longer hire, or anything the site "
                          "does not answer."),
            Field("contact_form_heading", "Form heading", default="Send us a message"),
            Field("contact_success", "Message shown after sending", type="textarea", rows=2,
                  default="Thanks — we have your message and will come back to you."),
            Field("contact_reply_note", "Note under the form", type="textarea", rows=2,
                  default=f"Messages reach us here on the site rather than by email, so "
                          f"please leave a phone number if it is urgent. {TBC} say how "
                          f"quickly you usually reply."),
        ],
    ),
    Group(
        "vehicle", "Fleet and car pages",
        "The fleet listing header, plus the standard text shown on every car's page.",
        [
            Field("fleet_page_eyebrow", "Fleet page — small label", default="Our cars"),
            Field("fleet_page_heading", "Fleet page — heading", default="Cars available to hire"),
            Field("fleet_page_intro", "Fleet page — intro", type="textarea", rows=2,
                  default="Set your dates to see what is free and what the hire would come to."),
            Field("vehicle_included_heading", "Included list — heading",
                  default="Good to know"),
            Field("vehicle_included_items", "Included list — items", type="lines", rows=6,
                  default=f"{TBC} what the insurance covers\n"
                          f"{TBC} mileage limit, or unlimited\n"
                          f"{TBC} fuel policy\n"
                          f"{TBC} extra drivers",
                  help="Shown on every car page. Replace each line with the real answer."),
            Field("vehicle_terms_note", "Deposit and licence note", type="textarea", rows=4,
                  default=f"A refundable deposit of {{deposit}} is taken when you collect the "
                          f"car. {TBC} confirm the minimum age, how long a licence must have "
                          f"been held, and which licences you accept.",
                  help="{deposit} is replaced with that car's deposit."),
            Field("vehicle_booking_note", "Note under the booking button", type="textarea", rows=2,
                  default="Nothing to pay now — this is a request. We come back to you to "
                          "confirm it."),
        ],
    ),
    Group(
        "footer", "Footer",
        "The bottom of every page.",
        [
            Field("footer_blurb", "Short description", type="textarea", rows=3,
                  default=f"Self-drive car hire in The Gambia. {TBC} add a line about your "
                          f"business."),
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
