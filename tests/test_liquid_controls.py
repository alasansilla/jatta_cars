"""The ride form and driver sign-in controls: liquid styling without losing
accessibility or behaviour."""
import re

from tests.test_marketplace import MarketplaceCase

CSS_PATH = "app/static/css/style.css"


def _luminance(hex_colour):
    hex_colour = hex_colour.lstrip("#")
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a, b):
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def mix(a, b):
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join(f"{(int(a[i:i + 2], 16) + int(b[i:i + 2], 16)) // 2:02x}" for i in (0, 2, 4))


class LiquidControlTests(MarketplaceCase):
    def setUp(self):
        super().setUp()
        with open(CSS_PATH, encoding="utf-8") as handle:
            self.css = handle.read()

    def variable(self, name):
        return re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", self.css).group(1)

    def test_ride_form_controls_use_liquid_buttons(self):
        page = self.client.get("/ride").get_data(as_text=True)
        locate = re.search(r'<button[^>]*id="pickup-locate"[^>]*>', page).group(0)
        self.assertIn("btn-liquid", locate)
        for which in ("pickup", "dropoff"):
            choose = re.search(rf'<button[^>]*class="btn-liquid js-choose" data-which="{which}" '
                               rf'aria-pressed="false"[^>]*>', page)
            self.assertIsNotNone(choose, which)
        estimate = re.search(r'<button[^>]*id="estimate"[^>]*>', page).group(0)
        self.assertIn("btn-liquid--primary", estimate)
        self.assertIn('type="button"', estimate)

    def test_passenger_stepper_is_labelled_and_bounded(self):
        page = self.client.get("/ride").get_data(as_text=True)
        self.assertIn('<div class="stepper" role="group" aria-labelledby="passengers-label">', page)
        self.assertIn('<label for="passengers" id="passengers-label"', page)
        self.assertRegex(page, r'data-step="-1" aria-controls="passengers"\s+aria-label="Fewer passengers"')
        self.assertRegex(page, r'data-step="1" aria-controls="passengers"\s+aria-label="More passengers"')
        self.assertRegex(page, r'<input type="number" id="passengers" min="1" max="8" value="1"')
        # The buttons fire the field's own change event, so the estimate resets as before.
        self.assertIn('passengers.dispatchEvent(new Event("change", { bubbles: true }))', page)
        self.assertIn("reset();", page[page.index('passengers.addEventListener("change"'):])

    def test_busy_location_button_keeps_its_icon_and_announces_busy(self):
        page = self.client.get("/ride").get_data(as_text=True)
        self.assertIn('<span id="pickup-locate-label">Use my location</span>', page)
        handler = page[page.index("setLocating: function"):page.index("changed: reset")]
        self.assertIn('$("pickup-locate-label").textContent', handler)
        self.assertIn('setAttribute("aria-busy"', handler)
        self.assertNotIn("button.textContent", handler)

    def test_send_me_a_code_is_a_liquid_primary_button_on_both_driver_pages(self):
        for path in ("/driver/join", "/driver/sign-in"):
            page = self.client.get(path).get_data(as_text=True)
            button = re.search(r'<button[^>]*type="submit"[^>]*>\s*Send me a code', page)
            self.assertIsNotNone(button, path)
            self.assertIn("btn-liquid--primary", button.group(0), path)
            self.assertNotIn("disabled", button.group(0), path)

    def test_states_are_defined(self):
        for selector in (".btn-liquid:focus-visible", ".btn-liquid:active", ".btn-liquid:disabled",
                         '.btn-liquid[aria-pressed="true"]', ".btn-liquid--primary:focus-visible",
                         ".btn-liquid--primary:disabled", "@media (prefers-reduced-motion: reduce)",
                         "@media (forced-colors: active)", "@media (max-width: 560px)"):
            self.assertIn(selector, self.css, selector)
        block = self.css[self.css.index(".btn-liquid {"):]
        self.assertIn("min-height: 48px", block[:block.index("}")])

    def test_text_contrast_meets_wcag_aa(self):
        top, bottom = self.variable("liquid-coral-top"), self.variable("liquid-coral-bottom")
        self.assertGreaterEqual(contrast("#ffffff", mix(top, bottom)), 4.5, "white on the primary button")
        self.assertGreaterEqual(contrast("#ffffff", bottom), 4.5)
        self.assertGreaterEqual(contrast(self.variable("liquid-coral-text"), "#ffffff"), 4.5, "secondary label")
        self.assertGreaterEqual(contrast(self.variable("liquid-coral-ink"), "#ffe7e1"), 4.5, "pressed label")
        self.assertGreaterEqual(contrast("#605f69", "#f1f1f3"), 4.5, "disabled label")
        self.assertGreaterEqual(contrast("#7a3322", "#f5d6ce"), 4.5, "disabled primary label")
