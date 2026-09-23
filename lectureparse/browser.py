"""Small macOS browser bridge; authentication stays in the browser."""
import json
import subprocess
import sys
import time
import uuid
from urllib.parse import urlparse


class BrowserError(RuntimeError):
    pass


DISCOVER_JS = r"""(() => {
  const urls = [];
  const add = (url, kind, label = '') => {
    if (!url) return;
    try { url = new URL(url, location.href).href; } catch (_) { return; }
    if (!/^https?:/.test(url)) return;
    if (!urls.some(x => x.url === url)) urls.push({url, kind, label});
  };
  document.querySelectorAll('video, audio, source').forEach(e => {
    add(e.currentSrc || e.src, 'media');
    if (e.src !== e.currentSrc) add(e.src, 'media');
  });
  document.querySelectorAll('track').forEach(e => add(e.src, 'caption', e.label));
  const resources = performance.getEntriesByType('resource');
  for (const r of resources) {
    if (/caption_proxy|\.(vtt|srt)([?#]|$)/i.test(r.name)) add(r.name, 'caption');
    else if (/\.(m3u8|mp4)([?#]|$)/i.test(r.name)) add(r.name, 'media');
  }
  if (typeof videojs !== 'undefined') {
    for (const p of Object.values(videojs.getPlayers()).filter(Boolean)) {
      for (const s of p.currentSources()) add(s.src, 'media');
      for (const t of p.options().tracks || []) add(t.src, 'caption', t.label);
    }
  }
  const lectures = Array.from(document.querySelectorAll('a[href*="/player/episode/"]'))
    .map(a => ({url:a.href, title:(a.innerText || a.getAttribute('title') ||
      a.querySelector('img')?.alt || a.parentElement.innerText || '').trim()}))
    .filter((a,i,all) => all.findIndex(b => b.url === a.url) === i);
  const video = document.querySelector('video');
  return JSON.stringify({url:location.href, title:document.title,
    heading:document.querySelector('h1')?.innerText || '',
    duration:video && Number.isFinite(video.duration) ? video.duration : null,
    error:/session has expired|load this page again through Canvas/i.test(document.body.innerText),
    sources:urls, lectures});
})()"""


class Browser:
    def __init__(self, name='Arc'):
        if name not in ('Arc', 'Google Chrome'):
            raise BrowserError('Supported browsers: Arc and Google Chrome on macOS.')
        self.name = name
        self.window = None
        self.tab = None

    def script(self, body):
        if sys.platform != 'darwin':
            raise BrowserError('Arc/Google Chrome tab control requires macOS. Use --browser chromium on this machine.')
        code = f'with timeout of 15 seconds\ntell application {json.dumps(self.name)}\n{body}\nend tell\nend timeout'
        try:
            result = subprocess.run(['osascript', '-e', code], capture_output=True,
                                    text=True, timeout=20)
        except subprocess.TimeoutExpired as exc:
            raise BrowserError(f'{self.name} did not respond within 20 seconds. Retry when the browser is responsive.') from exc
        except OSError as exc:
            raise BrowserError(f'Cannot control {self.name}: {exc}') from exc
        if result.returncode:
            raise BrowserError(result.stderr.strip() + '\nOpen the lecture through Canvas. The browser must permit JavaScript from Apple Events.')
        return result.stdout.strip()

    @property
    def target(self):
        if self.tab is None:
            raise BrowserError('No lecture tab selected.')
        tab = json.dumps(self.tab) if self.name == 'Arc' else str(self.tab)
        window = json.dumps(self.window) if self.name == 'Arc' else str(self.window)
        return f'tab id {tab} of window id {window}'

    def attach(self, preferred_url=None):
        raw = self.script('''set matches to ""
set separator to ASCII character 9
repeat with w in windows
 set tabURLs to URL of every tab of w
 set tabIDs to id of every tab of w
 set windowID to id of w as text
 repeat with i from 1 to count of tabURLs
  try
   set u to item i of tabURLs
   if u contains "lecturecapture.la.utexas.edu/player" then
    set matches to matches & windowID & separator & (item i of tabIDs as text) & separator & u & linefeed
   end if
  end try
 end repeat
end repeat
return matches''')
        rows = [line.split('\t', 2) for line in raw.splitlines() if '\t' in line]
        if not rows:
            raise BrowserError(f'Open Lectures Online through Canvas in {self.name}, then retry.')
        # Reuse the requested episode when it is already open.
        row = next((r for r in rows if preferred_url and r[2].rstrip('/') == preferred_url.rstrip('/')), None)
        row = row or next((r for r in rows if '/episode/' in r[2]), rows[0])
        self.window, self.tab, _ = row
        return self

    def evaluate(self, javascript):
        if self.name == 'Arc':
            # Arc can select a tab by ID but time out executing JS through that
            # same ID. Resolve the active renderer after selecting the exact tab.
            body = (f'activate\ntell {self.target} to select\n'
                    f'if URL of active tab of front window is not URL of {self.target} then error "Could not select the requested lecture tab."\n'
                    f'tell front window to tell active tab to execute javascript {json.dumps(javascript)}')
        else:
            body = f'execute {self.target} javascript {json.dumps(javascript)}'
        raw = self.script(body)
        # Arc returns a JSON-encoded JS string; Chrome returns the JS string directly.
        if self.name == 'Arc':
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                pass
        return raw

    def discover(self, url=None, timeout=25):
        if self.tab is None:
            self.attach(url)
        if self.name == 'Arc':
            # Arc can leave background tabs suspended, including after setting URL.
            self.script(f'activate\ntell {self.target} to select')
        if url:
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.hostname != 'lecturecapture.la.utexas.edu' or not parsed.path.startswith('/player'):
                raise BrowserError('Use a https://lecturecapture.la.utexas.edu/player URL.')
            current_url = self.script(f'return URL of {self.target}')
            if current_url.rstrip('/') != url.rstrip('/'):
                self.script(f'set URL of {self.target} to {json.dumps(url)}')
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = json.loads(self.evaluate(DISCOVER_JS))
            except (ValueError, TypeError):
                time.sleep(.4)
                continue
            if url and last.get('url', '').rstrip('/') != url.rstrip('/'):
                time.sleep(.4)
                continue
            if last.get('error'):
                raise BrowserError('Your player session expired. Reopen Lectures Online through Canvas.')
            if last.get('lectures') or any(s['kind'] == 'media' for s in last.get('sources', [])):
                # Captions and HLS requests appear slightly after the media element.
                time.sleep(1)
                return json.loads(self.evaluate(DISCOVER_JS))
            time.sleep(.4)
        raise BrowserError(f'No lecture media or lecture list appeared within {timeout}s. Open a lecture in the player.')

    def fetch_text(self, url, timeout=30):
        """Fetch small caption/manifest files in the existing browser session."""
        if urlparse(url).scheme not in ('http', 'https'):
            raise BrowserError('Only HTTP(S) resources are supported.')
        key = '__lecture_export_' + uuid.uuid4().hex
        js = f'''(() => {{
          window[{json.dumps(key)}] = {{pending:true}};
          fetch({json.dumps(url)}, {{credentials:'same-origin'}})
            .then(async r => {{
              if (!r.ok) throw new Error('HTTP ' + r.status);
              const text = await r.text();
              if (text.length > 10000000) throw new Error('Text resource too large');
              window[{json.dumps(key)}] = {{text}};
            }}).catch(e => window[{json.dumps(key)}] = {{error:String(e)}});
          return 'started';
        }})()'''
        self.evaluate(js)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                raw = self.evaluate(f'JSON.stringify(window[{json.dumps(key)}] || {{error:"Lecture tab navigated away"}})')
                result = json.loads(raw)
                if 'error' in result:
                    raise BrowserError(result['error'])
                if 'text' in result:
                    return result['text']
                time.sleep(.25)
            raise BrowserError('Timed out fetching the caption/manifest in the browser.')
        finally:
            try:
                self.evaluate(f'delete window[{json.dumps(key)}]')
            except BrowserError:
                pass

    def close(self):
        """The user's existing browser stays open."""
