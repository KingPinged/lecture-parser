"""Read HLS manifests and remux an entire VOD stream with FFmpeg."""
import json
import re
import shutil
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


def fetch_text(url, browser=None, referer=None):
    if urlparse(url).scheme not in ('http', 'https'):
        raise ValueError('Only HTTP(S) media URLs are supported.')
    headers = {'User-Agent': 'LectureParser/1.0'}
    if referer:
        headers['Referer'] = referer
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            data = response.read(10_000_001)
        if len(data) > 10_000_000:
            raise ValueError('Caption/manifest exceeds the 10 MB text limit.')
        text = data.decode('utf-8-sig')
        if '<html' in text[:1000].lower() or '<!doctype html' in text[:1000].lower():
            raise ValueError('Server returned an HTML/login page instead of captions or a manifest.')
        return text
    except (HTTPError, URLError, TimeoutError, ValueError):
        if browser is None:
            raise
        return browser.fetch_text(url)


def attributes(line):
    return {m.group(1): m.group(2).strip('"') for m in re.finditer(
        r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(':', 1)[-1])}


def parse_hls(text, base_url):
    if not text.lstrip('\ufeff').startswith('#EXTM3U'):
        raise ValueError('Invalid HLS playlist (possibly an expired media link).')
    lines = [s.strip() for s in text.splitlines() if s.strip()]
    variants, renditions, durations, keys = [], [], [], []
    pending = None
    for line in lines:
        if line.startswith('#EXT-X-STREAM-INF:'):
            pending = attributes(line)
        elif pending is not None and not line.startswith('#'):
            variants.append({**pending, 'url': urljoin(base_url, line)})
            pending = None
        elif line.startswith('#EXT-X-MEDIA:'):
            attrs = attributes(line)
            if 'URI' in attrs:
                attrs['url'] = urljoin(base_url, attrs['URI'])
            renditions.append(attrs)
        elif line.startswith('#EXTINF:'):
            durations.append(float(line.split(':', 1)[1].split(',', 1)[0]))
        elif line.startswith('#EXT-X-KEY:'):
            keys.append(attributes(line))
    return {'variants': variants, 'renditions': renditions, 'duration': sum(durations),
            'segments': len(durations), 'complete': '#EXT-X-ENDLIST' in lines, 'keys': keys}


def select_media(sources):
    candidates = [s for s in sources if s['kind'] == 'media'
                  and urlparse(s['url']).scheme in ('http', 'https')
                  and re.search(r'\.(m3u8|mp4)([?#]|$)', s['url'], re.I)]
    if not candidates:
        raise ValueError('No HTTP HLS or MP4 source found in the player.')
    # Prefer the master seen in <source>, not a low-resolution chunklist request.
    return sorted(candidates, key=lambda s: ('chunklist' in s['url'], '.m3u8' not in s['url']))


def resolve_hls(url, get_text, quality='best', depth=0):
    if depth > 4:
        raise ValueError('HLS playlist nesting is too deep.')
    playlist = parse_hls(get_text(url), url)
    for key in playlist['keys']:
        if key.get('METHOD', 'NONE') not in ('NONE', 'AES-128') or key.get('KEYFORMAT', 'identity') != 'identity':
            raise ValueError('This stream uses DRM unsupported by this exporter.')
    if not playlist['variants']:
        if not playlist['complete']:
            raise ValueError('This is a live/incomplete playlist. Export a completed lecture recording.')
        if not playlist['segments']:
            raise ValueError('HLS playlist contains no media segments.')
        return {'url': url, 'duration': playlist['duration'], 'segments': playlist['segments']}
    variants = sorted(playlist['variants'], key=lambda v: int(v.get('BANDWIDTH', 0)))
    choice = variants[0] if quality == 'lowest' else variants[-1]
    result = resolve_hls(choice['url'], get_text, quality, depth + 1)
    result['resolution'] = choice.get('RESOLUTION')
    result['bandwidth'] = choice.get('BANDWIDTH')
    audio = [r for r in playlist['renditions'] if r.get('TYPE') == 'AUDIO'
             and r.get('GROUP-ID') == choice.get('AUDIO') and r.get('url')]
    if audio:
        track = next((r for r in audio if r.get('DEFAULT') == 'YES'), audio[0])
        audio_info = resolve_hls(track['url'], get_text, quality, depth + 1)
        result['audio_url'] = audio_info['url']
    result['subtitle_urls'] = [r['url'] for r in playlist['renditions']
                               if r.get('TYPE') == 'SUBTITLES' and r.get('url')]
    return result


def probe(path):
    if not shutil.which('ffprobe'):
        raise RuntimeError('ffprobe is required. Install FFmpeg first.')
    process = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                              '-of', 'json', str(path)], capture_output=True, text=True, timeout=60)
    if process.returncode:
        raise RuntimeError(f'ffprobe could not validate {path.name}: {process.stderr[-1000:]}')
    return json.loads(process.stdout)


def validate_video(path, duration=None):
    info = probe(path)
    kinds = {stream['codec_type'] for stream in info['streams']}
    if 'video' not in kinds or 'audio' not in kinds:
        raise RuntimeError('Downloaded lecture must contain both video and audio.')
    actual = float(info['format'].get('duration', 0))
    if duration and abs(actual - duration) > max(2, duration * .005):
        raise RuntimeError(f'Incomplete recording: expected {duration:.1f}s; got {actual:.1f}s.')
    if not actual:
        raise RuntimeError('Downloaded video has no duration.')
    return info


def download_mp4(media, destination, referer, overwrite=False):
    if not shutil.which('ffmpeg'):
        raise RuntimeError('ffmpeg is required. Install FFmpeg first.')
    if destination.exists() and not overwrite:
        raise FileExistsError(f'{destination} already exists; use --overwrite to replace it.')
    temporary = destination.with_name(destination.stem + '.partial.mp4')
    log_path = destination.with_name('ffmpeg.log')
    args = ['ffmpeg', '-hide_banner', '-nostdin', '-y', '-loglevel', 'warning',
            '-progress', 'pipe:1', '-nostats']
    for url in [media['url']] + ([media['audio_url']] if media.get('audio_url') else []):
        args += ['-rw_timeout', '30000000', '-reconnect', '1', '-reconnect_streamed', '1',
                 '-reconnect_delay_max', '5', '-referer', referer, '-i', url]
    args += ['-map', '0:v:0', '-map', '1:a:0' if media.get('audio_url') else '0:a:0',
             '-c', 'copy', '-movflags', '+faststart', str(temporary)]
    try:
        with log_path.open('w') as log:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=log, text=True)
            last_report = 0
            try:
                for line in process.stdout:
                    if line.startswith('out_time_us=') and time.monotonic() - last_report > 10:
                        try:
                            exported = int(line.split('=', 1)[1]) / 1_000_000
                            print(f'Video: {exported:.0f}/{media.get("duration") or 0:.0f} seconds remuxed', flush=True)
                            last_report = time.monotonic()
                        except ValueError:
                            pass
                code = process.wait()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
            finally:
                process.stdout.close()
        if code:
            raise RuntimeError(f'FFmpeg failed; see {log_path}. Reopen the lecture if its media URL expired.')
        info = validate_video(temporary, media.get('duration'))
        temporary.replace(destination)
        return info
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
