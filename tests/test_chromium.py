"""Real Chromium tests against intercepted pages; no university account required."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from lectureparse.browser import BrowserError
from lectureparse.chromium import ChromiumBrowser, PLAYER_URL


EPISODE = PLAYER_URL + '/episode/sample'
CAPTION = 'WEBVTT\n\n00:00.000 --> 00:02.000\nλ → résumé.\n'
HTML = '''<html><head><title>Sample lecture</title></head><body>
<h1>Sample lecture</h1><video src="/video.mp4" preload="none">
<track src="/caption.vtt" label="English"></video></body></html>'''


@unittest.skipUnless(os.environ.get('LECTUREPARSE_BROWSER_TESTS') == '1',
                     'Set LECTUREPARSE_BROWSER_TESTS=1 after installing Playwright Chromium.')
class ChromiumTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.browser = ChromiumBrowser(self.temp.name, headless=True).start()
        self.addCleanup(self.browser.close)
        self.html = HTML
        self.browser.context.route('**/*', self.route)

    def route(self, route):
        if route.request.url.endswith('/caption.vtt'):
            if 'sample_auth=yes' in route.request.headers.get('cookie', ''):
                route.fulfill(status=200, content_type='text/vtt', body=CAPTION)
            else:
                route.fulfill(status=403, body='Sign in')
        elif route.request.url.endswith('/video.mp4'):
            route.fulfill(status=200, content_type='video/mp4', body=b'')
        else:
            route.fulfill(status=200, content_type='text/html', body=self.html)

    def test_discovery_and_authenticated_caption_fetch(self):
        self.browser.context.add_cookies([{'name': 'sample_auth', 'value': 'yes', 'url': PLAYER_URL}])
        snapshot = self.browser.attach(EPISODE).discover()
        self.assertEqual(snapshot['url'], EPISODE)
        self.assertEqual(snapshot['title'], 'Sample lecture')
        captions = [s for s in snapshot['sources'] if s['kind'] == 'caption']
        self.assertEqual(len(captions), 1)
        self.assertEqual(self.browser.fetch_text(captions[0]['url']), CAPTION)

    def test_login_restores_session_cookie_and_last_lecture(self):
        def login_in_test_browser(_):
            self.browser.context.add_cookies([{'name': 'sample_auth', 'value': 'yes', 'url': PLAYER_URL}])
            self.browser.page.goto(EPISODE)
            return ''
        with patch('builtins.input', side_effect=login_in_test_browser):
            self.browser.login()
        self.browser.close()
        self.browser.start()
        self.browser.context.route('**/*', self.route)
        snapshot = self.browser.attach().discover()
        self.assertEqual(snapshot['url'], EPISODE)
        self.assertEqual(self.browser.fetch_text('https://lecturecapture.la.utexas.edu/caption.vtt'), CAPTION)

    def test_expired_session_reports_relogin(self):
        self.html = '<html><body>Your session has expired.</body></html>'
        self.browser.attach(EPISODE)
        with self.assertRaisesRegex(BrowserError, 'lectureparse login'):
            self.browser.discover(timeout=2)

    def test_non_player_destination_is_rejected(self):
        with self.assertRaisesRegex(BrowserError, 'Use a https'):
            self.browser.attach('https://example.test/player')

    def test_login_requires_a_lecture_page(self):
        with patch('builtins.input', return_value=''):
            with self.assertRaisesRegex(BrowserError, 'No Lectures Online'):
                self.browser.login()
        self.assertFalse((Path(self.temp.name) / 'session.json').exists())

    def test_login_finds_popup_opened_while_terminal_input_blocks(self):
        def wait_for_user(_):
            self.browser.page.evaluate('(url) => setTimeout(() => window.open(url), 50)', EPISODE)
            time.sleep(.5)
            return ''
        with patch('builtins.input', side_effect=wait_for_user):
            self.browser.login()
        self.assertTrue((Path(self.temp.name) / 'player.json').exists())
