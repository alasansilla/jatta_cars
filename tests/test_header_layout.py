"""The header has to fit the screen it is on.

It is sticky, so anything that wraps in it follows the reader down every page.
On a phone the logo tagline used to take a second line and the bar stood 167px
tall — a fifth of the screen. Measured in a browser at 320, 375, 768, 1100,
1280 and 1500 CSS pixels, the bar is now one row everywhere: logo, the ride
button, and either the menu or the full list of links.
"""
import re

from app.models import Vehicle, db
from tests.test_marketplace import MarketplaceCase

CSS_PATH = "app/static/css/style.css"
DESTINATIONS = ["Home", "Ride", "Airport transfer", "Car rental", "Drivers", "My booking",
                "Become a driver", "Driver sign in"]


def block(css, query):
    """The body of one @media block, by its condition."""
    start = css.index(f"@media ({query})")
    depth, index = 0, css.index("{", start)
    for position in range(index, len(css)):
        depth += css[position] == "{"
        depth -= css[position] == "}"
        if depth == 0:
            return css[index:position]
    raise AssertionError(f"unclosed @media ({query})")


class HeaderLayoutTests(MarketplaceCase):
    def setUp(self):
        super().setUp()
        with open(CSS_PATH, encoding="utf-8") as handle:
            self.css = handle.read()
        self.header = re.search(r'<header class="site-header">.*?</header>',
                                self.client.get("/").get_data(as_text=True), re.S).group(0)

    def test_the_ride_button_sits_in_the_bar_not_in_the_menu(self):
        nav = re.search(r"<nav class=\"nav\">.*?</nav>", self.header, re.S).group(0)
        self.assertNotIn("header-cta", nav)
        self.assertEqual(self.header.count("header-cta\""), 1)
        cta = re.search(r'<a class="btn btn--primary header-cta".*?</a>', self.header, re.S).group(0)
        self.assertIn('aria-label="Request a ride"', cta)
        self.assertIn("Request a ride", cta)
        # The short label is decoration; the accessible name stays the full phrase.
        self.assertIn('<span class="header-cta__brief" aria-hidden="true">Ride</span>', cta)

    def test_a_phone_shows_the_short_label_and_hides_the_tagline(self):
        phone = block(self.css, "max-width: 640px")
        self.assertIn(".brand__sub { display: none; }", phone)
        self.assertIn(".header-cta__full { display: none; }", phone)
        self.assertIn(".header-cta__brief { display: inline; }", phone)
        # Staff editing the site still need to reach the tagline.
        self.assertIn("body.is-editing .brand__sub { display: block; }", phone)
        self.assertIn(".brand__sub { display: none; }", block(self.css, "max-width: 400px")
                      + phone)

    def test_every_destination_is_still_reachable(self):
        for label in DESTINATIONS:
            self.assertIn(f">{label}</a>", self.header, label)
        # The menu is a checkbox and a label: it opens with no script at all.
        self.assertIn('<input type="checkbox" id="nav-toggle" class="nav-toggle">', self.header)
        self.assertIn('<label for="nav-toggle" class="nav-burger" aria-label="Show menu">',
                      self.header)
        self.assertIn(".nav-toggle:checked ~ .nav { display: flex; }",
                      block(self.css, "max-width: 1240px"))

    def test_the_menu_folds_away_until_the_links_genuinely_fit(self):
        self.assertIn(".nav-burger { display: none;", self.css)      # wide screens
        folded = block(self.css, "max-width: 1240px")
        self.assertIn(".nav-burger { display: block; }", folded)
        self.assertIn("flex-direction: column", folded)
        self.assertIn(".header-cta { order: 2; margin-left: auto; }", folded)

    def test_the_bar_may_be_wider_than_the_text_column(self):
        rule = re.search(r"\.site-header \.wrap \{[^}]*\}", self.css).group(0)
        self.assertIn("max-width: 1400px", rule)
        self.assertIn("display: flex", rule)

    def test_page_layout_keeps_its_own_breakpoint(self):
        """Folding the menu earlier must not stack the hero or footer earlier."""
        layout = block(self.css, "max-width: 1000px")
        self.assertRegex(layout, r"\.hero \.wrap \{ grid-template-columns: minmax\(0, 1fr\); \}")
        self.assertRegex(layout, r"\.footer-grid \{ grid-template-columns: repeat\(2, minmax\(0, 1fr\)\); \}")
        self.assertNotIn(".hero .wrap", block(self.css, "max-width: 1240px"))


class NarrowPhoneLayoutTests(MarketplaceCase):
    """Rules that keep every page inside a narrow screen with larger text.

    Measured in a browser across the home, ride, transfer, rental, car, driver,
    About, Contact and join pages at 320, 360 and 390 CSS pixels, with text at
    100%, 130% and 150% (what Android's font-size setting does): no sideways
    scroll, nothing past the edge, nothing clipped. These assertions keep the
    rules that made that true from quietly going away.
    """

    def setUp(self):
        super().setUp()
        with open(CSS_PATH, encoding="utf-8") as handle:
            self.css = handle.read()

    def test_page_layouts_can_shrink_below_their_content(self):
        stacked = block(self.css, "max-width: 900px") + block(self.css, "max-width: 1000px")
        self.assertIn(".fleet-layout, .detail-layout { grid-template-columns: minmax(0, 1fr); }", stacked)
        self.assertIn(".split { grid-template-columns: minmax(0, 1fr);", stacked)
        self.assertIn(".hero .wrap { grid-template-columns: minmax(0, 1fr); }", stacked)
        self.assertIn(".split > *, .fleet-layout > *, .detail-layout > *, .booking-grid > *,", self.css)
        # No layout column left that refuses to shrink.
        self.assertNotRegex(self.css, r"grid-template-columns:\s*1fr\s*;")

    def test_buttons_wrap_on_a_phone_except_the_one_word_header_button(self):
        phone = block(self.css, "max-width: 640px")
        self.assertIn(".btn { white-space: normal; max-width: 100%; text-align: center; }", phone)
        self.assertIn(".header-cta { white-space: nowrap; }", phone)

    def test_car_cards_put_the_price_and_button_on_two_lines_when_needed(self):
        footer = re.search(r"\.card__footer \{[^}]*\}", self.css).group(0)
        self.assertIn("flex-wrap: wrap", footer)

    def test_a_long_word_breaks_rather_than_widening_the_page(self):
        body = re.search(r"\nbody \{[^}]*\}", self.css).group(0)
        self.assertIn("overflow-wrap: break-word", body)


class CompactPhoneDesignTests(MarketplaceCase):
    """A phone gets a compact layout, not the desktop one shrunk.

    Measured in a browser at 360 and 390 CSS pixels with default text: a
    rental card is 355-368px tall with its price and Book button inside it; all
    three journey choices sit within the first 525px of the page; with 130%
    text the card grows to 406-484px and still holds price and Book.
    """

    def setUp(self):
        super().setUp()
        with open(CSS_PATH, encoding="utf-8") as handle:
            self.css = handle.read()
        compact_start = self.css.index("/* --- Compact phone layout")
        self.phone = block(self.css[compact_start:], "max-width: 640px")

    def test_headings_and_spacing_step_down_on_a_phone(self):
        self.assertIn("h1 { font-size: clamp(1.75rem, 8vw, 2.2rem); }", self.phone)
        self.assertIn("h2 { font-size: clamp(1.35rem, 6vw, 1.6rem); }", self.phone)
        self.assertIn(".section { padding: 36px 0; }", self.phone)
        # Body text is left at its readable size: nothing here shrinks it.
        self.assertNotRegex(self.phone, r"\bbody \{[^}]*font-size")

    def test_the_car_card_is_a_short_summary_with_price_and_book_in_view(self):
        self.assertIn(".card__media { aspect-ratio: 16 / 7; }", self.phone)
        self.assertIn('.card__specs li + li::before { content: "·"; margin: 0 7px; }', self.phone)
        self.assertIn(".card__footer .btn { min-height: 44px;", self.phone)
        # Nothing is given a fixed height, so larger text makes it taller, not cut off.
        self.assertNotRegex(self.phone, r"\.card[^{]*\{[^}]*(?<![-\w])height:")

    def test_the_card_still_names_the_driver_and_their_reviews(self):
        vehicle = Vehicle(make="Toyota", model="Yaris", year=2020, daily_rate=2500,
                          operator_id=self.operator.id, is_active=True, service_mode="rental")
        db.session.add(vehicle)
        db.session.commit()
        page = self.client.get("/fleet").get_data(as_text=True)
        meta = re.search(r'<p class="card__meta small muted">(.*?)</p>', page, re.S).group(1)
        self.assertIn(self.operator.display_name, meta)
        self.assertIn("no reviews yet", meta)

    def test_finding_a_ride_is_one_panel_on_a_phone(self):
        page = self.client.get("/").get_data(as_text=True)
        # Desktop keeps its two hero buttons; a phone hides them for the panel,
        # which carries the one choice they added as a link.
        self.assertIn('class="hero__actions"', page)
        self.assertIn('<a class="journey-alt" href="/drivers">Or choose a driver yourself →</a>', page)
        self.assertIn(".hero__actions { display: none; }", self.phone)
        self.assertIn(".journey-alt { display: block;", self.phone)
        self.assertIn(".journey-alt { display: none; }", self.css.split("@media (max-width: 640px)")[0][-2000:]
                      + self.css[self.css.index("/* --- Compact phone layout"):])
        self.assertIn(".hero .booking-panel .btn { min-height: 48px;", self.phone)

    def test_zoom_is_never_disabled(self):
        base = open("app/templates/base.html", encoding="utf-8").read()
        viewport = re.search(r'<meta name="viewport" content="([^"]+)"', base).group(1)
        self.assertNotIn("user-scalable=no", viewport)
        self.assertNotIn("maximum-scale", viewport)
