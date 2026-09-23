#!/usr/bin/env python3
"""Export Lectures Online transcripts, MP4 recordings, and AI context."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse

from lectureparse.browser import Browser
from lectureparse.chromium import ChromiumBrowser
from lectureparse.media import download_mp4, fetch_text, resolve_hls, select_media
from lectureparse.summarize import ai_summary
from lectureparse.transcript import Cue, chunks, parse_captions, write_context, write_transcript


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest='command', required=True)
    login = commands.add_parser('login', help='Sign in to Canvas in a dedicated Chromium window (all platforms).')
    login.add_argument('--profile', type=Path, help='Local session folder (default: ~/.lectureparse).')
    for name in ('list', 'inspect', 'export'):
        command = commands.add_parser(name)
        command.add_argument('--browser', choices=['chromium', 'Arc', 'Google Chrome'], default='chromium',
                             help='Default: chromium (all platforms). Arc/Google Chrome reuse macOS tabs.')
        command.add_argument('--profile', type=Path, help='Chromium session folder (default: ~/.lectureparse).')
        command.add_argument('--headless', action='store_true', help='Hide the Chromium window after login.')
        command.add_argument('--url', help='Player or episode URL; omit for the last lecture or an existing macOS tab.')
        command.add_argument('--snapshot', type=Path, help='Use a previously saved discovery JSON instead of a browser.')
        if name == 'export':
            command.add_argument('--format', choices=['transcript', 'mp4', 'context', 'all'], default='all')
            command.add_argument('--output', type=Path, help='Output folder (default: exports/<episode-id>).')
            command.add_argument('--quality', choices=['best', 'lowest'], default='best')
            command.add_argument('--source-index', type=int, default=0, help='Index of the discovered media source, normally 0.')
            command.add_argument('--caption-index', type=int, default=0)
            command.add_argument('--overwrite', action='store_true')
            command.add_argument('--transcribe-missing', action='store_true', help='Run local Whisper when no captions can be read.')
            command.add_argument('--whisper-model', default='base')
            command.add_argument('--ollama-model', help='Also generate summary.md using an installed local model.')
            command.add_argument('--ollama-url', default='http://localhost:11434')
    summary = commands.add_parser('summarize', help='Generate a local AI summary from an existing export.')
    summary.add_argument('directory', type=Path)
    summary.add_argument('--ollama-model', required=True)
    summary.add_argument('--ollama-url', default='http://localhost:11434')
    return root


def get_snapshot(args):
    if args.snapshot:
        return json.loads(args.snapshot.read_text(encoding='utf-8')), None
    browser = (ChromiumBrowser(args.profile, args.headless) if args.browser == 'chromium'
               else Browser(args.browser))
    url = args.url
    if args.command == 'list' and not url:
        url = 'https://lecturecapture.la.utexas.edu/player'
    try:
        browser.attach(url)
        return browser.discover(url), browser
    except BaseException:
        browser.close()
        raise


def local_transcribe(media, directory, model):
    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError('Install openai-whisper for --transcribe-missing, or use provided captions.') from exc
    audio = directory / 'audio.wav'
    source = media.get('audio_url') or media['url']
    process = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-y', '-loglevel', 'error',
                              '-rw_timeout', '30000000', '-i', source, '-vn', '-ac', '1',
                              '-ar', '16000', str(audio)], capture_output=True, text=True)
    if process.returncode:
        audio.unlink(missing_ok=True)
        raise RuntimeError('Audio extraction for Whisper failed: ' + process.stderr[-1000:])
    print(f'Transcribing locally with Whisper {model}; this can take several minutes.', flush=True)
    result = whisper.load_model(model).transcribe(str(audio), fp16=False, verbose=False)
    cues = [Cue(s['start'], s['end'], s['text'].strip()) for s in result['segments'] if s['text'].strip()]
    if not cues:
        raise RuntimeError('Whisper produced no speech segments.')
    return cues


def export(args, snapshot, browser):
    if args.format == 'mp4' and args.ollama_model:
        raise ValueError('AI summarization requires transcript, context, or all format.')
    if '/player/episode/' not in snapshot.get('url', ''):
        raise ValueError('The player is a lecture list. Use list, then export --url <episode-url>.')
    identifier = urlparse(snapshot['url']).path.rstrip('/').split('/')[-1]
    identifier = re.sub(r'[^A-Za-z0-9_-]', '_', identifier)
    output = args.output or Path('exports') / identifier
    if output.exists() and any(output.iterdir()) and not args.overwrite:
        raise FileExistsError(f'{output} is not empty; choose another folder or use --overwrite.')
    if args.source_index < 0 or args.caption_index < 0:
        raise ValueError('Source and caption indexes must be nonnegative.')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'discovery.json').write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    get_text = lambda url: fetch_text(url, browser, snapshot['url'])
    candidates = select_media(snapshot['sources'])
    if args.source_index >= len(candidates):
        raise ValueError(f'Only {len(candidates)} media source(s) were discovered.')
    selected = candidates[args.source_index]['url']
    if '.m3u8' in urlparse(selected).path:
        media = resolve_hls(selected, get_text, args.quality)
    else:
        media = {'url': selected, 'duration': snapshot.get('duration')}
    metadata = {'title': snapshot['title'], 'url': snapshot['url'], 'episode_id': identifier,
                'duration': media.get('duration') or snapshot.get('duration'),
                'exported_at': datetime.now(timezone.utc).isoformat(), 'media': media,
                'files': [], 'complete': False}
    metadata_path = output / 'metadata.json'
    save_metadata = lambda: metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    save_metadata()
    cues, pieces = None, None
    if args.format in ('transcript', 'context', 'all'):
        captions = [s['url'] for s in snapshot['sources'] if s['kind'] == 'caption']
        # Direct caption files may also be declared as HLS subtitle renditions.
        captions += [u for u in media.get('subtitle_urls', []) if re.search(r'\.(vtt|srt)([?#]|$)', u)]
        captions = list(dict.fromkeys(captions))
        failure = 'No caption file was discovered.'
        if captions:
            if args.caption_index >= len(captions):
                raise ValueError(f'Only {len(captions)} caption track(s) were discovered.')
            caption_url = captions[args.caption_index]
            try:
                original = get_text(caption_url)
                cues = parse_captions(original)
                extension = 'vtt' if original.lstrip('\ufeff \r\n').startswith('WEBVTT') else 'srt'
                (output / f'captions.{extension}').write_text(original, encoding='utf-8')
                metadata['caption_source'] = caption_url
            except Exception as exc:
                failure = f'Caption extraction failed: {exc}'
        if cues is None:
            if not args.transcribe_missing:
                raise RuntimeError(failure + ' Use --transcribe-missing for local speech recognition, or --format mp4.')
            cues = local_transcribe(media, output, args.whisper_model)
            metadata['caption_source'] = 'local-whisper:' + args.whisper_model
        write_transcript(output, cues, metadata)
        metadata['cue_count'] = len(cues)
        metadata['word_count'] = sum(len(c.text.split()) for c in cues)
        metadata['caption_start'] = cues[0].start
        metadata['caption_end'] = cues[-1].end
        if metadata['duration'] and cues[-1].end > metadata['duration'] + 10:
            metadata['warning'] = 'Caption timeline exceeds video duration; check synchronization.'
        print(f'Transcript: {len(cues)} timestamped cues, {metadata["word_count"]} words', flush=True)
        if args.format in ('context', 'all') or args.ollama_model:
            pieces = write_context(output, cues, metadata)
            print(f'Context: {len(pieces)} chunks, transcript context, and extractive outline', flush=True)
        save_metadata()
    if args.format in ('mp4', 'all'):
        print(f'Downloading full recording ({media.get("resolution", "source quality")})…', flush=True)
        info = download_mp4(media, output / 'lecture.mp4', snapshot['url'], args.overwrite)
        metadata['video_validation'] = {
            'duration': float(info['format']['duration']), 'bytes': (output / 'lecture.mp4').stat().st_size,
            'streams': [{k: s[k] for k in ('codec_type', 'codec_name', 'width', 'height') if k in s}
                        for s in info['streams']]}
        print(f'MP4 verified: {metadata["video_validation"]["duration"]:.1f} seconds', flush=True)
        save_metadata()
    if args.ollama_model:
        if pieces is None:
            raise ValueError('AI summarization requires transcript, context, or all format.')
        ai_summary(output, pieces, metadata, args.ollama_model, args.ollama_url)
        metadata['summary_model'] = args.ollama_model
    metadata['complete'] = True
    metadata['files'] = sorted(p.name for p in output.iterdir() if p.is_file())
    save_metadata()
    print(f'Export complete: {output.resolve()}', flush=True)


def main(argv=None):
    args = parser().parse_args(argv)
    browser = None
    try:
        if args.command == 'login':
            browser = ChromiumBrowser(args.profile)
            browser.login()
        elif args.command == 'summarize':
            data = json.loads((args.directory / 'transcript.json').read_text(encoding='utf-8'))
            metadata = json.loads((args.directory / 'metadata.json').read_text(encoding='utf-8'))
            cues = [Cue(**s) for s in data['segments']]
            pieces = write_context(args.directory, cues, metadata)
            ai_summary(args.directory, pieces, metadata, args.ollama_model, args.ollama_url)
            metadata['summary_model'] = args.ollama_model
            metadata['files'] = sorted(p.name for p in args.directory.iterdir() if p.is_file())
            (args.directory / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
        else:
            snapshot, browser = get_snapshot(args)
            if args.command == 'list':
                if not snapshot.get('lectures'):
                    raise ValueError('No lecture list found. Open the player index through Canvas.')
                print(json.dumps(snapshot['lectures'], indent=2, ensure_ascii=False))
            elif args.command == 'inspect':
                print(json.dumps(snapshot, indent=2, ensure_ascii=False))
            else:
                export(args, snapshot, browser)
        return 0
    except KeyboardInterrupt:
        print('Interrupted; incomplete exports are not marked complete.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    finally:
        if browser is not None:
            browser.close()


if __name__ == '__main__':
    raise SystemExit(main())
