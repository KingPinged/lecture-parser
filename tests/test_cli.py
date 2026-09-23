"""Exercise packaged CLI exports over local HTTP, with no private lecture data."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from lectureparse.media import download_mp4


CAPTIONS = 'WEBVTT\n\n00:00.000 --> 00:02.000\nλ → résumé: Unicode captions.\n'


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/caption.vtt':
            body = CAPTIONS.encode('utf-8')
        elif self.path == '/video.mp4':
            body = self.server.video
        else:
            body = b'#EXTM3U\n#EXTINF:2,\n0.ts\n#EXT-X-ENDLIST\n'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.prompts.append(request['prompt'])
        body = json.dumps({'response': 'Example study notes: λ → résumé [00:00:00].'}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CLITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        self.server.prompts = []
        self.server.video = b''
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.snapshot = self.folder / 'discovery.json'
        self.data = {'title': 'Lecture λ', 'url': 'https://lecturecapture.la.utexas.edu/player/episode/sample',
                     'duration': 2, 'sources': [{'kind': 'media', 'url': self.base + '/master.m3u8'},
                                               {'kind': 'caption', 'url': self.base + '/caption.vtt'}]}
        self.snapshot.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')
        self.output = self.folder / 'export'

    def cli(self, *args):
        # Run outside the checkout so the installed package and entry point are exercised.
        return subprocess.run([sys.executable, '-m', 'lectureparse', *args], cwd=self.folder,
                              capture_output=True, text=True, encoding='utf-8', timeout=30)

    def test_context_and_summary_export_preserves_unicode(self):
        result = self.cli('export', '--snapshot', str(self.snapshot), '--format', 'context',
                          '--output', str(self.output), '--ollama-model', 'test-model', '--ollama-url', self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads((self.output / 'metadata.json').read_text(encoding='utf-8'))
        self.assertTrue(metadata['complete'])
        self.assertEqual(metadata['cue_count'], 1)
        self.assertEqual(metadata['summary_model'], 'test-model')
        self.assertIn('λ → résumé', (self.output / 'transcript.txt').read_text(encoding='utf-8'))
        self.assertIn('λ → résumé', (self.output / 'summary.md').read_text(encoding='utf-8'))
        self.assertEqual(len(self.server.prompts), 2)
        self.assertIn('Unicode captions', self.server.prompts[0])
        self.assertFalse((self.output / 'lecture.mp4').exists())
        again = self.cli('export', '--snapshot', str(self.snapshot), '--format', 'context',
                         '--output', str(self.output))
        self.assertNotEqual(again.returncode, 0)
        self.assertIn('not empty', again.stderr)

    def test_missing_captions_leave_export_incomplete(self):
        self.data['sources'] = self.data['sources'][:1]
        self.snapshot.write_text(json.dumps(self.data), encoding='utf-8')
        result = self.cli('export', '--snapshot', str(self.snapshot), '--format', 'transcript',
                          '--output', str(self.output))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No caption file', result.stderr)
        metadata = json.loads((self.output / 'metadata.json').read_text(encoding='utf-8'))
        self.assertFalse(metadata['complete'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is optional')
    def test_full_mp4_with_unknown_initial_duration(self):
        source = self.folder / 'source.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'color=c=blue:s=160x120:d=2', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
                        '-c:v', 'mpeg4', '-c:a', 'aac', '-shortest', str(source)], check=True, timeout=30)
        self.server.video = source.read_bytes()
        destination = self.folder / 'download.mp4'
        info = download_mp4({'url': self.base + '/video.mp4', 'duration': None}, destination, self.data['url'])
        self.assertEqual({s['codec_type'] for s in info['streams']}, {'audio', 'video'})
        self.assertAlmostEqual(float(info['format']['duration']), 2, delta=.2)
        self.assertFalse((self.folder / 'download.partial.mp4').exists())
