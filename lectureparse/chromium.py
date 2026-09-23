"""Portable browser discovery with a separate, locally saved Canvas session."""
import json
from pathlib import Path
import time
from urllib.parse import urlparse

from .browser import BrowserError, DISCOVER_JS


PLAYER_URL = 'https://lecturecapture.la.utexas.edu/player'
CANVAS_URL = 'https://utexas.instructure.com'


def is_player_url(url):
    parsed = urlparse(url)
    return (parsed.scheme == 'https' and parsed.hostname == 'lecturecapture.la.utexas.edu'
            and (parsed.path == '/player' or parsed.path.startswith('/player/')))


class ChromiumBrowser:
    def __init__(self, profile=None, headless=False):
        self.profile = Path(profile or Path.home() / '.lectureparse').expanduser()
        self.headless = headless
        self.driver = self.browser = self.context = self.page = None

    def start(self):
        if self.context is not None:
            return self
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError('Install the browser extra: python -m pip install ".[browser]"; '
                               'then run python -m playwright install chromium.') from exc
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.driver = sync_playwright().start()
            self.browser = self.driver.chromium.launch(headless=self.headless)
            state = self.profile / 'session.json'
            self.context = self.browser.new_context(storage_state=str(state) if state.exists() else None)
            self.page = self.context.new_page()
        except Exception as exc:
            self.close()
            raise BrowserError('Could not start Chromium. Run python -m playwright install chromium '
                               '(Linux may also need --with-deps). ' + str(exc)) from exc
        return self

    def save_session(self):
        # Explicit storage state also retains session cookies across browser launches.
        state = self.profile / 'session.json'
        temporary = self.profile / 'session.tmp'
        temporary.touch(mode=0o600, exist_ok=True)
        self.context.storage_state(path=str(temporary))
        temporary.replace(state)

    def remember(self, url):
        if is_player_url(url):
            (self.profile / 'player.json').write_text(json.dumps({'url': url}) + '\n', encoding='utf-8')

    def login(self):
        self.start()
        self.page.goto(CANVAS_URL, wait_until='domcontentloaded')
        print('In Chromium, sign in to Canvas, open your course, then Lectures Online and a recording.')
        input('Leave that page open, then return here and press Enter: ')
        # Pump browser events queued while terminal input was blocking (new tabs/frames).
        self.context.cookies()
        # Lectures Online may be embedded in a Canvas frame or opened in a new tab.
        frames = []
        deadline = time.monotonic() + 3
        while not frames and time.monotonic() < deadline:
            pages = self.context.pages
            frames = [frame for page in pages for frame in page.frames if is_player_url(frame.url)]
            if not frames and pages:
                pages[0].wait_for_timeout(100)
            elif not pages:
                break
        if not frames:
            raise BrowserError('No Lectures Online page is open. Run lectureparse login again and open a recording.')
        target = next((f for f in frames if '/episode/' in f.url), frames[-1])
        target.wait_for_load_state('domcontentloaded', timeout=10000)
        snapshot = json.loads(target.evaluate(DISCOVER_JS))
        if snapshot.get('error'):
            raise BrowserError('Player session expired. Reopen Lectures Online through Canvas and retry login.')
        self.save_session()
        self.remember(target.url)
        print(f'Canvas session saved locally in {self.profile}. You can now run lectureparse export.')

    def attach(self, preferred_url=None):
        url = preferred_url
        if url is None and (self.profile / 'player.json').exists():
            url = json.loads((self.profile / 'player.json').read_text(encoding='utf-8'))['url']
        url = url or PLAYER_URL
        if not is_player_url(url):
            raise BrowserError('Use a https://lecturecapture.la.utexas.edu/player URL.')
        self.start()
        self.page.goto(url, wait_until='domcontentloaded')
        return self

    def discover(self, url=None, timeout=25):
        if url and not is_player_url(url):
            raise BrowserError('Use a https://lecturecapture.la.utexas.edu/player URL.')
        if self.page is None:
            self.attach(url)
        elif url and self.page.url.rstrip('/') != url.rstrip('/'):
            self.page.goto(url, wait_until='domcontentloaded')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = json.loads(self.page.evaluate(DISCOVER_JS))
            if snapshot.get('error') or not is_player_url(snapshot['url']):
                raise BrowserError('Player session expired or redirected to login. Run lectureparse login again.')
            if snapshot.get('lectures') or any(s['kind'] == 'media' for s in snapshot.get('sources', [])):
                self.page.wait_for_timeout(1000)
                snapshot = json.loads(self.page.evaluate(DISCOVER_JS))
                self.save_session()
                self.remember(snapshot['url'])
                return snapshot
            self.page.wait_for_timeout(400)
        raise BrowserError('No lecture media appeared. Run lectureparse login and open a recording through Canvas.')

    def fetch_text(self, url, timeout=30):
        if urlparse(url).scheme not in ('http', 'https'):
            raise BrowserError('Only HTTP(S) resources are supported.')
        return self.page.evaluate('''async ({url, timeout}) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), timeout);
          try {
            const response = await fetch(url, {credentials: 'same-origin', signal: controller.signal});
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const text = await response.text();
            if (text.length > 10000000) throw new Error('Text resource too large');
            return text;
          } finally { clearTimeout(timer); }
        }''', {'url': url, 'timeout': timeout * 1000})

    def close(self):
        try:
            if self.browser is not None:
                self.browser.close()
        finally:
            if self.driver is not None:
                self.driver.stop()
            self.driver = self.browser = self.context = self.page = None
