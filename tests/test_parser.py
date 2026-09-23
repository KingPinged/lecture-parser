import json
from pathlib import Path
import tempfile
import unittest

from lectureparse.media import attributes, parse_hls, resolve_hls, select_media
from lectureparse.transcript import Cue, chunks, parse_captions, seconds, stamp, write_context, write_transcript


class CaptionsTest(unittest.TestCase):
    def test_vtt_identifiers_settings_speakers_and_metadata(self):
        text = '''WEBVTT

NOTE generated captions
This is not speech.

STYLE
::cue { color: white }

cue-1
00:00:01.250 --> 00:00:03.750 align:start position:0%
<v Lecturer>Data &amp; <b>tables</b>.</v>

00:03.750 --> 00:05.000
Second line
continues.
'''
        self.assertEqual(parse_captions(text), [Cue(1.25, 3.75, 'Lecturer: Data & tables.'),
                                               Cue(3.75, 5, 'Second line continues.')])

    def test_srt_and_exact_duplicate_removal(self):
        text = '1\n00:01:00,500 --> 00:01:02,000\nHello.\n\n2\n00:01:00,500 --> 00:01:02,000\nHello.\n'
        self.assertEqual(parse_captions(text), [Cue(60.5, 62, 'Hello.')])

    def test_repeated_speech_at_different_times_is_preserved(self):
        text = 'WEBVTT\n\n00:01.000 --> 00:02.000\nYes.\n\n00:05.000 --> 00:06.000\nYes.'
        self.assertEqual(len(parse_captions(text)), 2)

    def test_invalid_caption_and_reversed_time(self):
        for text in ['<html>Sign in</html>', 'WEBVTT\n\n00:05.000 --> 00:04.000\nBad', 'WEBVTT\n\nNOTE no cues']:
            with self.assertRaises(ValueError):
                parse_captions(text)

    def test_timestamp_rounding_and_hours(self):
        self.assertEqual(seconds('01:15:57.530'), 4557.53)
        self.assertEqual(stamp(59.9996, True), '00:01:00.000')
        self.assertEqual(stamp(3600.005, True, True), '01:00:00,005')

    def test_chunks_cover_every_cue_once(self):
        cues = [Cue(i * 10, i * 10 + 9, 'three simple words') for i in range(20)]
        pieces = chunks(cues, max_words=10, max_seconds=25)
        flattened = [Cue(**cue) for p in pieces for cue in p['cues']]
        self.assertEqual(flattened, cues)
        self.assertTrue(all(len(p['text'].split()) <= 10 for p in pieces))

    def test_exports_roundtrip_and_context(self):
        cues = [Cue(1.001, 3, 'A & B'), Cue(61, 65, 'A different statement: λ → résumé.')]
        meta = {'title': 'Sample', 'url': 'https://example.test/lecture', 'duration': 70,
                'caption_source': 'sample.vtt'}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_transcript(folder, cues, meta)
            write_context(folder, cues, meta)
            self.assertEqual(parse_captions((folder / 'transcript.srt').read_text(encoding='utf-8')), cues)
            self.assertEqual(len(json.loads((folder / 'transcript.json').read_text(encoding='utf-8'))['segments']), 2)
            self.assertIn('not a visual analysis', (folder / 'context.md').read_text(encoding='utf-8'))
            self.assertIn('not an AI-written summary', (folder / 'outline.md').read_text(encoding='utf-8'))


class HLSTest(unittest.TestCase):
    def test_quoted_attributes_and_relative_urls(self):
        text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=123,CODECS="avc1.123,mp4a.40.2",RESOLUTION=960x540\n../high.m3u8?token=abc\n'
        parsed = parse_hls(text, 'https://example.test/video/master.m3u8')
        self.assertEqual(parsed['variants'][0]['CODECS'], 'avc1.123,mp4a.40.2')
        self.assertEqual(parsed['variants'][0]['url'], 'https://example.test/high.m3u8?token=abc')

    def test_best_variant_and_alternate_audio(self):
        master = '''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100,RESOLUTION=640x360,AUDIO="a"
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=300,RESOLUTION=960x540,AUDIO="a"
high.m3u8
'''
        child = '#EXTM3U\n#EXTINF:10.0,\n0.ts\n#EXTINF:9.5,\n1.ts\n#EXT-X-ENDLIST\n'
        def get(url):
            return master if url.endswith('master.m3u8') else child
        result = resolve_hls('https://example.test/master.m3u8', get)
        self.assertEqual(result['url'], 'https://example.test/high.m3u8')
        self.assertEqual(result['audio_url'], 'https://example.test/audio.m3u8')
        self.assertEqual(result['duration'], 19.5)
        self.assertEqual(resolve_hls('https://example.test/master.m3u8', get, 'lowest')['resolution'], '640x360')

    def test_live_invalid_and_drm_fail_explicitly(self):
        for playlist in ['#EXTM3U\n#EXTINF:10,\n0.ts', '<html>Expired</html>',
                         '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES\n#EXTINF:10,\n0.ts\n#EXT-X-ENDLIST']:
            with self.assertRaises(ValueError):
                resolve_hls('https://example.test/a.m3u8', lambda _: playlist)

    def test_master_is_preferred_to_observed_variant(self):
        found = select_media([{'kind': 'media', 'url': 'https://example.test/chunklist.m3u8'},
                              {'kind': 'caption', 'url': 'https://example.test/caption.vtt'},
                              {'kind': 'media', 'url': 'https://example.test/playlist.m3u8'}])
        self.assertTrue(found[0]['url'].endswith('playlist.m3u8'))


if __name__ == '__main__':
    unittest.main()
