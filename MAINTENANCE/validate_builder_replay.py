"""Offline replay of saved ASR traces; no audio/model invocation."""
import argparse
import importlib.util
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "SYNCED-LYRICS-DOWNLOADER-MAIN"))
import re
import triplenine_reference_queue as queue


def load_trace(path):
    text = path.read_text(encoding="utf-8-sig")
    def rows(engine):
        pattern = rf"^\[{engine} \d+\]\s+([\d.]+)s\s*-\s*([\d.]+)s\s*\| (.*)$"
        return [dict(start=float(a), end=float(b), text=t)
                for a, b, t in re.findall(pattern, text, re.M)]
    return rows("WHISPER"), rows("XINGYU")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("builder")
    parser.add_argument("log")
    parser.add_argument("reference")
    parser.add_argument("output")
    parser.add_argument("--reference-lines", type=int)
    parser.add_argument("--retry-intro-model")
    parser.add_argument("--retry-chorus-model")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("builder", args.builder)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    whisper, xingyu = load_trace(Path(args.log))
    ref = builder.parse_reference(queue.reference_text(args.reference))
    if args.retry_intro_model:
        import syncalong
        audio_match = re.search(r"^Audio:\s*(.+)$", Path(args.log).read_text(encoding="utf-8-sig"), re.M)
        if not audio_match:
            parser.error("Log does not contain an Audio path")
        whisper = builder._retry_missing_intro_audio(
            audio_match.group(1).strip(), ref, whisper, xingyu,
            syncalong.Transcriber(model_name=args.retry_intro_model), "en", print,
        )
    if args.reference_lines:
        limited, count = [], 0
        for item in ref:
            if count == args.reference_lines:
                break
            limited.append(item)
            count += item["kind"] == "lyric"
        ref = limited
    result, diagnostics, *_ = builder.hybridize(ref, whisper, xingyu_lines=xingyu)
    if args.retry_chorus_model:
        import syncalong
        audio_match = re.search(r"^Audio:\s*(.+)$", Path(args.log).read_text(encoding="utf-8-sig"), re.M)
        if not audio_match:
            parser.error("Log does not contain an Audio path")
        whisper = builder._retry_missing_chorus_opening(
            audio_match.group(1).strip(), ref, result, whisper, xingyu,
            syncalong.Transcriber(model_name=args.retry_chorus_model), "en", print,
        )
        result, diagnostics, *_ = builder.hybridize(ref, whisper, xingyu_lines=xingyu)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.txt").write_text("\n".join(result), encoding="utf-8")
    (out / "diagnostics.txt").write_text("\n".join(diagnostics), encoding="utf-8")
    sections = builder._reference_section_blocks(builder.parse_reference("\n".join(result)))
    print("ASR rows:", len(whisper), len(xingyu))
    print([(s["header"], len(s["lines"])) for s in sections])
    print("Fresh up out:", any("Fresh up out the jet" in line for line in result))
