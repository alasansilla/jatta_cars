"""The header has to fit the screen it is on.

It is sticky, so anything that wraps in it follows the reader down every page.
On a phone the logo tagline used to take a second line and the bar stood 167px
tall — a fifth of the screen. Measured in a browser at 320, 375, 768, 1100,
1280 and 1500 CSS pixels, the bar is now one row everywhere: logo, the ride
button, and either the menu or the full list of links.
"""
import re

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
        self.assertIn(".hero .wrap { grid-template-columns: 1fr; }", layout)
        self.assertIn(".footer-grid { grid-template-columns: 1fr 1fr; }", layout)
        self.assertNotIn(".hero .wrap", block(self.css, "max-width: 1240px"))
