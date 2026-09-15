"""Run the ride-picker JavaScript tests in a real headless browser.

The picker is plain JavaScript with its map, geolocation and reverse geocoder
passed in, so tests/js/ride_picker_test.html can drive it with fakes: map
selection, location permission denied, reverse geocoding failing, and the exact
coordinates kept. Skipped when no Chromium-based browser is installed.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, "tests", "js", "ride_picker_test.html")
CANDIDATES = [
    os.environ.get("JATTA_TEST_BROWSER", ""),
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    shutil.which("chromium") or "", shutil.which("google-chrome") or "",
]
BROWSER = next((path for path in CANDIDATES if path and os.path.exists(path)), None)


@unittest.skipIf(BROWSER is None, "no Chromium-based browser for JavaScript tests")
class RidePickerBrowserTests(unittest.TestCase):
    maxDiff = None

    def run_browser(self):
        """Headless Chromium on macOS can print the DOM and then not exit, so the
        output is read as it arrives and the browser is stopped once it is in."""
        profile = tempfile.mkdtemp()
        chunks = []
        process = subprocess.Popen(
            [BROWSER, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", f"--user-data-dir={profile}",
             "--allow-file-access-from-files", "--virtual-time-budget=5000",
             "--dump-dom", "file://" + HARNESS],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        reader = threading.Thread(target=lambda: chunks.extend(iter(process.stdout.readline, "")),
                                  daemon=True)
        reader.start()
        deadline = time.time() + 90
        try:
            while time.time() < deadline:
                if "</html>" in "".join(chunks) or process.poll() is not None:
                    break
                time.sleep(0.2)
        finally:
            process.kill()
            process.wait(timeout=10)
            shutil.rmtree(profile, ignore_errors=True)
        return "".join(chunks)

    def test_picker_behaviour_in_a_real_browser(self):
        output = self.run_browser()
        match = re.search(r'<pre id="results">(.*?)</pre>', output, re.S)
        self.assertIsNotNone(match, output[-500:])
        text = match.group(1)
        self.assertNotEqual(text, "running", "the browser tests did not finish")
        results = json.loads(text.replace("&lt;", "<").replace("&gt;", ">")
                             .replace("&quot;", '"').replace("&amp;", "&"))
        self.assertGreaterEqual(len(results), 40)
        failures = [f"{r['name']}: {r['detail']}" for r in results if not r["ok"]]
        self.assertEqual(failures, [])

if __name__ == "__main__":
    unittest.main()
