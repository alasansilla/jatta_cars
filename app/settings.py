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
            Field("company_tagline", "Tagline", default="Car hire, made simple."),
            Field("company_email", "Email address", default="hello@jattacars.com"),
            Field("company_phone", "Phone number", default="+49 30 123 4567"),
            Field("company_address", "Address", type="textarea", rows=2,
                  default="Hauptstrasse 1, 10827 Berlin, Germany"),
            Field("currency", "Currency symbol", default="€",
                  help="Used in front of every price, e.g. € or £ or $."),
            Field("locations", "Pick-up points", type="lines", rows=6,
                  default="Berlin City Centre\nBerlin Brandenburg Airport (BER)\n"
                          "Berlin Hauptbahnhof\nPotsdam",
                  help="One per line. These are the options customers pick from when booking."),
            Field("opening_hours", "Opening hours",
                  default="Monday to Sunday, 7am – 9pm"),
        ],
    ),
    Group(
        "booking", "Booking rules",
        "The limits applied when someone requests a car, and the deposit terms shown to them.",
        [
            Field("promo_message", "Ribbon on the booking panel", type="text",
                  default="Free cancellation up to 24 hours before pick-up",
                  help="Shown above the home page booking panel. Leave empty to hide it."),
            Field("min_rental_days", "Minimum hire (days)", type="number", default=1),
            Field("max_rental_days", "Maximum hire (days)", type="number", default=90),
            Field("max_advance_days", "How far ahead bookings open (days)", type="number", default=365),
            Field("default_excess", "Insurance excess", type="number", default=750,
                  help="Quoted on the home page. Just the number — the currency symbol is added."),
        ],
    ),
    Group(
        "home", "Home page",
        "Every piece of text and the main image on the home page.",
        [
            Field("home_eyebrow", "Small label above the headline", default="Plan your trip now"),
            Field("home_heading", "Headline", default="Hire a car without the small print"),
            Field("home_heading_highlight", "Word(s) to colour in the headline", default="without",
                  help="The first match inside the headline is shown in the accent colour. "
                       "Leave empty for a plain headline."),
            Field("home_intro", "Introduction", type="textarea", rows=4,
                  default="A tidy, well-serviced fleet at four pick-up points across Berlin. "
                          "One price covers insurance, breakdown cover and unlimited kilometres "
                          "— what you see at booking is what you pay at the desk."),
            Field("home_hero_image", "Main image", type="image", default="img/car-economy.svg",
                  help="The large picture beside the headline."),
            Field("home_cta_primary", "Primary button", default="Book a car"),
            Field("home_cta_secondary", "Secondary button", default="Learn more"),

            Field("home_search_heading", "Booking panel heading", default="Book a car"),

            Field("home_fleet_eyebrow", "Fleet section label", default="Our fleet"),
            Field("home_fleet_heading", "Fleet section heading",
                  default="Pick the car that fits the trip"),
            Field("home_fleet_intro", "Fleet section intro", type="textarea", rows=2,
                  default="Every car is under four years old, serviced on schedule and "
                          "cleaned between hires."),

            Field("home_why_eyebrow", "Why-choose-us label", default="Why choose us"),
            Field("home_why_heading", "Why-choose-us heading",
                  default="The price you were quoted, and nothing else"),
            Field("home_why_body", "Why-choose-us text", type="textarea", rows=6,
                  default="Most of what makes car hire irritating is added at the counter. "
                          "We took the other route: one rate, everything in it, and a deposit "
                          "that goes back on your card the day the car comes home.\n\n"
                          "Book online in about two minutes. We confirm by email, usually within the hour."),
            Field("home_why_cta", "Why-choose-us button", default="Find a car"),

            Field("reason_1_icon", "Reason 1 — icon", type="choice", default="coin", choices=ICON_CHOICES),
            Field("reason_1_title", "Reason 1 — heading", default="All-inclusive pricing"),
            Field("reason_1_body", "Reason 1 — text", type="textarea", rows=3,
                  default="Insurance, breakdown cover, VAT and unlimited kilometres are in the "
                          "rate. No airport surcharge, no young-driver fee, no fuel-service charge."),
            Field("reason_2_icon", "Reason 2 — icon", type="choice", default="shield", choices=ICON_CHOICES),
            Field("reason_2_title", "Reason 2 — heading", default="Cover that actually covers"),
            Field("reason_2_body", "Reason 2 — text", type="textarea", rows=3,
                  default="Comprehensive insurance comes as standard with a {excess} excess. "
                          "Reduce it to zero at the desk if you would rather not think about it.",
                  help="{excess} is replaced with the insurance excess from Booking rules."),
            Field("reason_3_icon", "Reason 3 — icon", type="choice", default="road", choices=ICON_CHOICES),
            Field("reason_3_title", "Reason 3 — heading", default="Take it where you like"),
            Field("reason_3_body", "Reason 3 — text", type="textarea", rows=3,
                  default="Unlimited kilometres across Germany, and travel into the EU at no "
                          "extra cost — just tell us where you are heading so the paperwork "
                          "travels with you."),

            Field("home_steps_eyebrow", "Steps section label", default="How it works"),
            Field("home_steps_heading", "Steps section heading",
                  default="Three steps and you are driving"),
            Field("step_1_title", "Step 1 — heading", default="Choose your dates"),
            Field("step_1_body", "Step 1 — text", type="textarea", rows=3,
                  default="Tell us when and where. Only cars that are genuinely free for those "
                          "days are shown — no phantom availability."),
            Field("step_2_title", "Step 2 — heading", default="Book in two minutes"),
            Field("step_2_body", "Step 2 — text", type="textarea", rows=3,
                  default="Name, email, phone. Nothing to pay online; we take the deposit when "
                          "you collect the car."),
            Field("step_3_title", "Step 3 — heading", default="Collect and go"),
            Field("step_3_body", "Step 3 — text", type="textarea", rows=3,
                  default="Bring your licence and the card the deposit goes on. Keys in hand in "
                          "about ten minutes."),

            Field("home_about_eyebrow", "About section label", default="About us"),
            Field("home_about_heading", "About section heading",
                  default="You start the engine, your trip starts properly"),
            Field("home_about_body", "About section text", type="textarea", rows=7,
                  default="Jatta Cars is a family-run hire company working out of Berlin. We are "
                          "small enough that the person who answers the phone is the person who "
                          "hands you the keys, and stubborn enough to keep the pricing honest "
                          "while the big chains take the other approach.\n\n"
                          "Every car is bought new or nearly new, kept on a strict service "
                          "schedule and retired before it can become someone's bad day."),
            Field("home_about_cta", "About section button", default="More about us"),
            Field("home_stat3_value", "Third statistic — number", default="7"),
            Field("home_stat3_label", "Third statistic — label", default="Days a week, 7am–9pm"),

            Field("home_included_eyebrow", "Included section label", default="Included as standard"),
            Field("home_included_heading", "Included section heading",
                  default="Everything below is in the price"),
            Field("home_included_items", "Included items", type="lines", rows=9,
                  default="Comprehensive insurance\n24/7 roadside assistance\n"
                          "Unlimited kilometres\nSecond driver at no charge\n"
                          "Winter tyres, October to Easter\nFree cancellation up to 24h before\n"
                          "Child seats on request\nVAT and all local charges",
                  help="One per line."),

            Field("home_final_heading", "Closing heading", default="Ready when you are"),
            Field("home_final_body", "Closing text", type="textarea", rows=2,
                  default="Have a look at what is available, or call us on {phone} and we will "
                          "sort it out over the phone.",
                  help="{phone} is replaced with your phone number."),
        ],
    ),
    Group(
        "about", "About page",
        "The whole of the About page.",
        [
            Field("about_eyebrow", "Small label", default="About us"),
            Field("about_heading", "Page heading", default="A small hire company that likes cars"),
            Field("about_section1_heading", "First section heading", default="Why we started"),
            Field("about_section1_body", "First section text", type="textarea", rows=8,
                  default="Anyone who has hired a car knows the routine: a good price online, "
                          "then forty minutes at a counter while the total quietly doubles. "
                          "Insurance you thought was included. A fee for the second driver. "
                          "A tank of fuel at twice the pump price.\n\n"
                          "Jatta Cars exists because we thought that was a solvable problem. "
                          "One rate with everything in it, a deposit that comes back, and a car "
                          "that has actually been cleaned. It is not a complicated idea — it is "
                          "just easier to run a hire company the other way."),
            Field("about_section2_heading", "Second section heading",
                  default="How we look after the cars"),
            Field("about_section2_body", "Second section text", type="textarea", rows=8,
                  default="Every vehicle is bought new or nearly new and serviced strictly to "
                          "schedule, not when it becomes convenient. Tyres are changed on tread "
                          "depth rather than optimism, winter tyres go on from October, and cars "
                          "leave the fleet at four years old.\n\n"
                          "Between hires each car is cleaned inside and out and checked over — "
                          "fluids, lights, tyres, warning lights. If something is not right, the "
                          "car does not go out."),
            Field("about_included_heading", "Included box — heading",
                  default="What every hire includes"),
            Field("about_included_items", "Included box — items", type="lines", rows=7,
                  default="Comprehensive insurance\nUnlimited kilometres in Germany and the EU\n"
                          "24/7 roadside assistance\nA second driver, free of charge\n"
                          "Winter tyres from October to Easter\n"
                          "Free cancellation up to 24 hours before"),
            Field("about_requirements_heading", "Requirements box — heading",
                  default="The requirements"),
            Field("about_requirements_intro", "Requirements box — intro",
                  default="Not much, but these ones we cannot bend:"),
            Field("about_requirements_items", "Requirements box — items", type="lines", rows=6,
                  default="21 or over, licence held for at least a year\n"
                          "A full licence — non-EU licences need an international permit\n"
                          "A credit or debit card in the driver's name for the deposit\n"
                          "Photo ID or passport"),
            Field("about_hours_heading", "Hours box — heading", default="Opening hours"),
            Field("about_hours_body", "Hours box — text", type="textarea", rows=3,
                  default="Seven days a week, 7am to 9pm, at all our pick-up points. "
                          "Out-of-hours collection can be arranged — just ask when you book."),
            Field("about_cta_heading", "Closing heading", default="Have a look at the fleet"),
            Field("about_cta_body", "Closing text", type="textarea", rows=2,
                  default="{fleet_size} cars, from small runarounds to seven-seat vans.",
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
                  default="Questions about a booking, a long hire, or something the website "
                          "does not cover."),
            Field("contact_form_heading", "Form heading", default="Send us a message"),
            Field("contact_success", "Message shown after sending", type="textarea", rows=2,
                  default="Thanks — your message is with us. We usually reply the same day."),
        ],
    ),
    Group(
        "vehicle", "Vehicle pages",
        "The fleet listing header, plus the standard text shown on every car's page.",
        [
            Field("fleet_page_eyebrow", "Fleet page — small label", default="Our fleet"),
            Field("fleet_page_heading", "Fleet page — heading", default="Cars available to hire"),
            Field("fleet_page_intro", "Fleet page — intro", type="textarea", rows=2,
                  default="Set your dates to see live availability and the total for your trip."),
            Field("vehicle_included_heading", "Included list — heading",
                  default="Included in every hire"),
            Field("vehicle_included_items", "Included list — items", type="lines", rows=6,
                  default="Comprehensive insurance\nUnlimited kilometres\n"
                          "24/7 roadside assistance\nSecond driver at no charge"),
            Field("vehicle_terms_note", "Deposit and licence note", type="textarea", rows=4,
                  default="Refundable deposit of {deposit}, taken when you collect the car and "
                          "returned once it is back with us. Drivers must be 21 or over and have "
                          "held a licence for at least a year.",
                  help="{deposit} is replaced with that car's deposit."),
            Field("vehicle_booking_note", "Note under the booking button", type="textarea", rows=2,
                  default="Nothing to pay now. We confirm by email, usually within the hour."),
        ],
    ),
    Group(
        "footer", "Footer",
        "The bottom of every page.",
        [
            Field("footer_blurb", "Short description", type="textarea", rows=3,
                  default="Well-kept cars, honest prices and keys in your hand in under ten minutes."),
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


def save_settings(submitted, group_key=None):
    """Write submitted values back.

    `submitted` is the raw form mapping. Only keys belonging to `group_key` are
    touched when it is given, so one form cannot clobber another group. A value
    equal to its default is stored anyway, which keeps the admin form honest
    about what it will show next time.
    """
    from .models import Setting, db

    keys = FIELDS.keys() if group_key is None else [f.key for f in GROUPS[group_key].fields]
    rows = {row.key: row for row in Setting.query.filter(Setting.key.in_(list(keys))).all()}

    for key in keys:
        field = FIELDS[key]
        if field.type == "boolean":
            raw = key in submitted
        elif key not in submitted:
            continue
        else:
            raw = submitted.get(key)

        value = field.to_storage(raw)
        row = rows.get(key)
        if row is None:
            db.session.add(Setting(key=key, value=value))
        else:
            row.value = value

    db.session.commit()


def reset_group(group_key):
    """Drop every stored value in a group so its defaults apply again."""
    from .models import Setting, db

    keys = [field.key for field in GROUPS[group_key].fields]
    Setting.query.filter(Setting.key.in_(keys)).delete(synchronize_session=False)
    db.session.commit()


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
        "excess": f"{settings.get('currency', '')}{settings.get('default_excess', '')}",
    }
    tokens.update(extra)

    for name, value in tokens.items():
        text = text.replace("{" + name + "}", str(value))
    return text
