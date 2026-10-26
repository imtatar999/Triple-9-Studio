"""Transcribe a read-only audio excerpt into a reusable replay trace."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "SYNCED-LYRICS-DOWNLOADER-MAIN"))
import subprocess
import tempfile
import syncalong
import Triplenine_Hybrid_Sessions_Lyrics_Builder as builder

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('audio')
    parser.add_argument('output')
    parser.add_argument('--start', type=float, required=True)
    parser.add_argument('--end', type=float, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='triplenine_check_') as folder:
        audio = Path(folder) / 'excerpt.wav'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-ss', str(args.start), '-i', args.audio,
                        '-t', str(args.end - args.start), '-ar', '16000', '-ac', '1', str(audio)],
                       check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        words = syncalong.Transcriber(model_name='turbo').transcribe(audio, language='en', initial_prompt=None)
    lines = builder.group_whisper_words(words)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n'.join(f"[WHISPER {i:03d}] {line['start'] + args.start:.2f}s - {line['end'] + args.start:.2f}s | {line['text']}"
                             for i, line in enumerate(lines, 1)), encoding='utf-8')
    print(f'Saved {len(lines)} excerpt transcript lines to {out}')
